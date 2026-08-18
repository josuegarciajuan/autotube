"""Generation service — orchestrates pipeline execution as async background jobs.

Broadcasts progress via WebSocket to the frontend panel.

v2.2: Restored missing helpers (_get_db, _broadcast_progress, _run_in_executor).
      Added richer feedback details in all progress broadcasts.
v2.3: Added memory guard watcher for video phase to prevent OOM crashes.
"""

import json
import logging
import multiprocessing
import os
import signal
import subprocess
import sys
import threading
import time
import asyncio
import concurrent.futures
import glob as _glob
import importlib
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional
from api.utils import db_now
import config.settings as settings
from database.db_extended import ExtendedDatabase
from config.config_bridge import get_channel_config

# ── Auto-mark altered content helper ──────────────────────────

def _retry_end_screens(browser, yt_video_id: str, max_retries: int = 3) -> bool:
    """Retry end screen configuration with exponential backoff."""
    import random
    for attempt in range(1, max_retries + 1):
        success = browser.add_end_screens(yt_video_id)
        if success:
            return True
        if attempt < max_retries:
            wait_s = 30 * (2 ** (attempt - 1)) + random.uniform(0, 15)
            logger.warning(
                "End screens attempt %d/%d failed for %s — retrying in %.0fs",
                attempt, max_retries, yt_video_id, wait_s,
            )
            time.sleep(wait_s)
        else:
            logger.error(
                "End screens exhausted %d retries for %s — giving up",
                max_retries, yt_video_id,
            )
    return False


def _auto_mark_altered_content(yt_video_id: str, canal: str, account: str, video_id: int):
    """Background thread: mark video as AI-generated + configure end screens.

    End screens are ALWAYS attempted independently of the IA-mark result.
    """
    import random
    from pipeline.youtube_browser import cleanup_browser_thread
    
    try:
        db = ExtendedDatabase()

        # ── Wait for YouTube to finish processing (60s, was 20s) ──
        logger.info("[%s] Waiting 60s for YouTube processing before Studio automation...", canal)
        time.sleep(60)

        from pipeline.youtube_browser import get_browser
        browser = get_browser(account)

        # ── Step 1: Mark AI-generated content (best-effort, non-blocking) ──
        try:
            success = browser.mark_altered_content(yt_video_id)
            if success:
                db.update_video(video_id, manual_altered_content_done=1)
                logger.info("[%s] IA altered content marked for %s", canal, yt_video_id)
            else:
                logger.warning("[%s] Failed to mark altered content for %s — continuing to end screens anyway", canal, yt_video_id)
        except Exception as e:
            logger.warning("[%s] IA-mark error for %s: %s — continuing to end screens anyway", canal, yt_video_id, e)

        # ── Step 2: Configure end screens (always attempted, with retries) ──
        try:
            from config.config_bridge import get_channel_config
            channel_config = get_channel_config(canal)
            if channel_config and getattr(channel_config, "AUTO_END_SCREENS", False):
                # Natural human delay between actions (5-12s)
                delay = random.uniform(5, 12)
                logger.info("[%s] Waiting %.1fs before end screen config...", canal, delay)
                time.sleep(delay)

                logger.info("[%s] 🎬 Attempting end screens for %s (up to 3 retries)", canal, yt_video_id)
                success2 = _retry_end_screens(browser, yt_video_id, max_retries=3)
                if success2:
                    db.update_video(video_id, manual_end_screens_done=1)
                    logger.info("[%s] ✅ End screens configured for %s", canal, yt_video_id)
                else:
                    logger.warning("[%s] ❌ Failed to configure end screens for %s after all retries", canal, yt_video_id)
            else:
                logger.debug("[%s] AUTO_END_SCREENS disabled, skipping", canal)
        except Exception as e:
            logger.warning("[%s] Auto end-screen error for %s: %s", canal, yt_video_id, e)
    finally:
        cleanup_browser_thread()

logger = logging.getLogger("autotube.generation")

# ── Ffmpeg orphan killer ──────────────────────────────────────
# Protects ffmpeg processes belonging to an active render from being
# killed by a concurrent job's pre-phase cleanup.  See _run_in_executor.

_RENDER_ACTIVE: bool = False  # set True during video phase, False otherwise
_TTS_ACTIVE: bool = False     # set True during TTS phase, False otherwise (Bug B fix)


def _kill_orphaned_ffmpeg():
    """Kill any ffmpeg child processes to prevent RAM leaks after job failure.
    
    MoviePy spawns ffmpeg subprocesses that can survive the parent if the
    pipeline crashes. Each orphan consumes 1-2.5 GB RAM decoding source
    videos in raw RGB. Uses a 2-layer cleanup targeting only orphans.
    
    1. Kill children of current PID (immediate children of the API process).
       SKIPPED when a render is active to avoid killing legitimate ffmpeg workers.
    2. Kill ffmpeg processes whose PPID is 1 (true init orphans).
       Always runs — these are truly orphaned regardless of render state.
    
    Also cleans orphaned edge-tts and yt-dlp subprocesses, and reaps
    zombie children to prevent <defunct> process accumulation.
    
    Expected error cases (non-blocking, logged at DEBUG):
      - subprocess.CalledProcessError: pgrep not available (minimal container).
      - subprocess.TimeoutExpired: process table contention causing pgrep hang.
      - ProcessLookupError: process died between pgrep and kill (normal race).
      - ValueError: garbage PID from pgrep output (should never happen).
      - ChildProcessError: no children to reap (normal, no zombies).
    """
    killed = 0
    try:
        # Layer 1: immediate children of this process.
        # Skip when a render or TTS is active — its ffmpeg workers are legitimate.
        if not _RENDER_ACTIVE and not _TTS_ACTIVE:
            pid = os.getpid()
            result = subprocess.run(
                ["pgrep", "-P", str(pid), "-f", "ffmpeg"],
                capture_output=True, text=True, timeout=5,
            )
            if result.stdout.strip():
                child_pids = result.stdout.strip().split()
                for cpid in child_pids:
                    try:
                        os.kill(int(cpid), 9)
                        killed += 1
                    except ProcessLookupError:
                        pass  # expected: process died between pgrep and kill
                    except ValueError as exc:
                        logger.debug(
                            "Orphan ffmpeg: invalid PID '%s' from pgrep: %s",
                            cpid, exc,
                            exc_info=True,
                        )
    except subprocess.TimeoutExpired as exc:
        logger.debug("Orphan ffmpeg layer 1: pgrep timed out: %s", exc, exc_info=True)
    except Exception as exc:
        logger.debug("Orphan ffmpeg layer 1: pgrep failed: %s", exc, exc_info=True)

    try:
        # Layer 2: true orphans (parent is init/pid 1)
        result = subprocess.run(
            ["pgrep", "-P", "1", "-f", "ffmpeg"],
            capture_output=True, text=True, timeout=5,
        )
        if result.stdout.strip():
            orphan_pids = result.stdout.strip().split()
            for opid in orphan_pids:
                try:
                    os.kill(int(opid), 9)
                    killed += 1
                except ProcessLookupError:
                    pass  # expected: process died between pgrep and kill
                except ValueError as exc:
                    logger.debug(
                        "Orphan ffmpeg layer 2: invalid PID '%s' from pgrep: %s",
                        opid, exc,
                        exc_info=True,
                    )
    except subprocess.TimeoutExpired as exc:
        logger.debug("Orphan ffmpeg layer 2: pgrep timed out: %s", exc, exc_info=True)
    except Exception as exc:
        logger.debug("Orphan ffmpeg layer 2: pgrep failed: %s", exc, exc_info=True)

    if killed > 0:
        logger.warning(
            "Killed %d orphaned ffmpeg process(es) (RAM leak prevention)", killed
        )

    # ── Also clean orphaned edge-tts and yt-dlp subprocesses ──
    for pattern, label in [
        ("edge-tts", "edge-tts"),
        ("yt-dlp", "yt-dlp"),
    ]:
        try:
            r2 = subprocess.run(
                ["pgrep", "-f", pattern],
                capture_output=True, text=True, timeout=5,
            )
            if r2.stdout.strip():
                for opid in r2.stdout.strip().split():
                    try:
                        ppid = int(subprocess.run(
                            ["ps", "-o", "ppid=", "-p", opid],
                            capture_output=True, text=True, timeout=3,
                        ).stdout.strip() or "0")
                        # Only kill if parent is init (orphan) or this process
                        if ppid in (1, os.getpid()):
                            os.kill(int(opid), 9)
                            killed += 1
                    except ProcessLookupError:
                        pass  # expected: process died between pgrep and kill
                    except (ValueError, subprocess.TimeoutExpired) as exc:
                        logger.debug(
                            "Orphan %s: PID %s could not be killed: %s",
                            label, opid, exc,
                            exc_info=True,
                        )
        except subprocess.TimeoutExpired as exc:
            logger.debug("Orphan %s: pgrep timed out: %s", label, exc, exc_info=True)
        except Exception as exc:
            logger.debug("Orphan %s: scan failed (non-critical): %s", label, exc, exc_info=True)

    if killed > 0:
        logger.warning(
            "Killed %d orphaned process(es) (ffmpeg + edge-tts + yt-dlp RAM leak prevention)", killed
        )

    # ── Reap zombie children to prevent <defunct> processes ──
    # Expected: ChildProcessError when no children exist, OSError for
    # any other waitpid failure (both normal and non-blocking).
    try:
        while True:
            wpid, _ = os.waitpid(-1, os.WNOHANG)
            if wpid == 0:
                break
    except ChildProcessError:
        pass  # expected: no children to reap
    except OSError as exc:
        logger.debug("Orphan ffmpeg: waitpid failed: %s", exc, exc_info=True)


def _kill_orphaned_workers(logger) -> int:
    """Kill full_pipeline_worker processes whose job is not 'running' in the DB.

    Prevents zombie worker accumulation when orphan detection marks jobs as
    failed in the DB but the OS process keeps running (tight retry loops,
    stuck API calls, etc.).

    Returns the number of workers killed.
    
    Expected error cases (non-blocking, logged at DEBUG):
      - FileNotFoundError: DATABASE_PATH does not exist (DB not initialized).
      - PermissionError: DB file not readable/writable (permissions changed).
      - sqlite3.OperationalError: DB locked or corrupted on connect; retried
        up to 3 times with exponential backoff (0.5s -> 1s -> 2s).
      - subprocess.TimeoutExpired: ps/pgrep hung (process table contention).
      - OSError: kill failed because process already exited (normal race).
    """
    import sqlite3
    import os as _os
    import time as _time
    from config.settings import DATABASE_PATH
    
    killed = 0
    
    # ── Pre-validate DB file existence and permissions ───────
    if not _os.path.exists(DATABASE_PATH):
        logger.debug(
            "Orphan workers: DB file not found at %s — skipping worker check",
            DATABASE_PATH,
        )
        return 0
    if not _os.access(DATABASE_PATH, _os.R_OK | _os.W_OK):
        logger.debug(
            "Orphan workers: DB file not readable/writable at %s — skipping worker check",
            DATABASE_PATH,
        )
        return 0
    
    # ── External connection: sqlite3.connect with retry ──────
    # Retry up to 3 times for transient errors (SQLITE_BUSY, disk I/O).
    db_conn = None
    last_db_exc = None
    for attempt in range(3):
        try:
            db_conn = sqlite3.connect(DATABASE_PATH)
            db_conn.row_factory = sqlite3.Row
            break
        except sqlite3.OperationalError as exc:
            last_db_exc = exc
            if attempt < 2:
                delay = 0.5 * (2 ** attempt)  # 0.5s → 1s → 2s
                logger.debug(
                    "Orphan workers: DB connect retry %d/3: %s",
                    attempt + 1, exc,
                    exc_info=True,
                )
                _time.sleep(delay)
        except Exception as exc:
            last_db_exc = exc
            logger.debug(
                "Orphan workers: DB connect failed (attempt %d/3): %s",
                attempt + 1, exc,
                exc_info=True,
            )
            if attempt < 2:
                _time.sleep(0.5 * (2 ** attempt))
    
    if db_conn is None:
        logger.debug(
            "Orphan workers: DB connect failed after 3 attempts: %s",
            last_db_exc,
        )
        return 0
    
    try:
        cur = db_conn.cursor()
        running_jobs = set(
            row["id"] for row in cur.execute(
                "SELECT id FROM generation_jobs WHERE status='running'"
            ).fetchall()
        )
    except Exception as exc:
        logger.debug(
            "Orphan workers: DB query for running jobs failed: %s",
            exc,
            exc_info=True,
        )
        running_jobs = set()
    finally:
        db_conn.close()

    if not running_jobs:
        logger.debug("No running jobs in DB — will clean any stale worker processes")

    # ── External connection: ps (process listing) with retry ──
    # Find all worker processes. Retry up to 2 times on timeout.
    result = None
    for attempt in range(2):
        try:
            result = subprocess.run(
                ["ps", "-eo", "pid,args", "--no-headers"],
                capture_output=True, text=True, timeout=5,
            )
            break
        except subprocess.TimeoutExpired as exc:
            logger.debug(
                "Orphan workers: ps timed out (attempt %d/2): %s",
                attempt + 1, exc,
                exc_info=True,
            )
        except Exception as exc:
            logger.debug(
                "Orphan workers: ps failed (attempt %d/2): %s",
                attempt + 1, exc,
                exc_info=True,
            )
            if attempt < 1:
                _time.sleep(1)
    if result is None:
        logger.debug("Orphan workers: ps failed after retries — skipping worker check")
        return 0

    for line in result.stdout.strip().split("\n"):
        if not line or "full_pipeline_worker.py" not in line:
            continue
        if "grep" in line:
            continue

        parts = line.strip().split()
        if not parts:
            continue
        try:
            pid = int(parts[0])
        except (ValueError, IndexError) as exc:
            logger.debug(
                "Orphan workers: invalid PID from ps output: %s",
                exc,
                exc_info=True,
            )
            continue

        # Extract job-id from command line
        import re
        job_match = re.search(r"--job-id\s+(\d+)", line)
        if not job_match:
            logger.warning(
                "Worker PID %d has no --job-id in args — killing as unidentifiable orphan",
                pid,
            )
            try:
                _os.kill(pid, signal.SIGTERM)
                killed += 1
            except ProcessLookupError:
                pass  # expected: process died between detection and kill
            except OSError as exc:
                logger.debug(
                    "Orphan workers: kill failed for PID %d: %s",
                    pid, exc,
                    exc_info=True,
                )
            continue

        job_id = int(job_match.group(1))
        if job_id not in running_jobs:
            logger.warning(
                "Killing orphaned worker PID=%d (job %d not in running set)",
                pid, job_id,
            )
            try:
                _os.kill(pid, signal.SIGTERM)
                _time.sleep(2)
                try:
                    _os.kill(pid, 0)
                    _os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass  # expected: process already exited
                killed += 1
            except ProcessLookupError:
                pass  # expected: process died between detection and kill
            except OSError as exc:
                logger.debug(
                    "Orphan workers: kill failed for PID %d (job %d): %s",
                    pid, job_id, exc,
                    exc_info=True,
                )

            # ── Clean up DB state for this orphaned worker ──
            # The worker was killed but the job/video may still be
            # marked as 'running'/'generating'.  Fix both so the UI
            # doesn't show phantom active generations.
            try:
                import sqlite3 as _sql3
                
                # Reuse the pre-validation from above
                if not _os.path.exists(DATABASE_PATH) or not _os.access(DATABASE_PATH, _os.R_OK | _os.W_OK):
                    logger.debug(
                        "Orphan workers: cannot clean DB state for job %d — DB not accessible",
                        job_id,
                    )
                    continue
                
                _conn2 = _sql3.connect(DATABASE_PATH)
                _conn2.row_factory = _sql3.Row
                try:
                    _cur2 = _conn2.cursor()
                    _job_row = _cur2.execute(
                        "SELECT id, status, video_id FROM generation_jobs WHERE id=?",
                        (job_id,),
                    ).fetchone()
                    if _job_row:
                        _job_status = _job_row["status"]
                        _video_id = _job_row["video_id"]
                        if _job_status not in ("completed", "failed", "cancelled"):
                            _cur2.execute(
                                "UPDATE generation_jobs SET status='failed', "
                                "error_msg=?, finished_at=CURRENT_TIMESTAMP WHERE id=?",
                                ("Orphaned: worker killed by pre-spawn cleanup", job_id),
                            )
                        if _video_id:
                            _vid = _cur2.execute(
                                "SELECT status FROM videos WHERE id=?", (_video_id,),
                            ).fetchone()
                            if _vid and _vid["status"] == "generating":
                                _yt = _cur2.execute(
                                    "SELECT yt_video_id FROM videos WHERE id=?", (_video_id,),
                                ).fetchone()
                                if not (_yt and _yt[0]):
                                    _cur2.execute(
                                        "UPDATE videos SET status='error', "
                                        "progress_phase='orphaned' WHERE id=?",
                                        (_video_id,),
                                    )
                                    logger.warning(
                                        "Orphan cleanup: video #%d reset to error/orphaned "
                                        "(job #%d worker killed)",
                                        _video_id, job_id,
                                    )
                    _conn2.commit()
                except sqlite3.OperationalError as exc:
                    logger.debug(
                        "Orphan workers: DB state cleanup failed for job %d: %s",
                        job_id, exc,
                        exc_info=True,
                    )
                finally:
                    _conn2.close()
            except Exception as exc:
                logger.debug(
                    "Orphan workers: DB state cleanup failed for job %d: %s",
                    job_id, exc,
                    exc_info=True,
                )

    if killed > 0:
        logger.warning("Killed %d orphaned worker process(es)", killed)

    return killed


def _get_ffmpeg_pids() -> set[int]:
    """Return PIDs of all running ffmpeg processes right now.
    
    Expected error cases (non-blocking, returns empty set):
      - subprocess.TimeoutExpired: pgrep hung (process table contention).
      - FileNotFoundError: pgrep binary not available (minimal container).
      - ValueError: garbage PID from pgrep output (should never happen).
    """
    try:
        r = subprocess.run(
            ["pgrep", "-f", "ffmpeg"],
            capture_output=True, text=True, timeout=3,
        )
        return {int(p) for p in r.stdout.strip().split() if p}
    except Exception as exc:
        logger.debug("_get_ffmpeg_pids: pgrep failed: %s", exc, exc_info=True)
        return set()


def _wait_for_ffmpeg_exit(pre_pids: set[int], timeout: int = 30):
    """Wait for ffmpeg processes spawned DURING render to exit naturally.

    Only kills if they hang beyond ``timeout`` seconds.
    Pre-existing zombies (in ``pre_pids``) are ignored — they were
    already there before the render started and are not ours to kill.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        current = _get_ffmpeg_pids()
        new_pids = current - pre_pids   # only the ones MoviePy spawned
        if not new_pids:
            return   # all exited cleanly — success
        # Reap any zombies that finished while we waited
        try:
            while True:
                wpid, _ = os.waitpid(-1, os.WNOHANG)
                if wpid == 0:
                    break
        except (ChildProcessError, OSError):
            pass
        time.sleep(0.5)

    # Timeout — kill what's left with surgical precision
    # Only kill the ffmpeg processes spawned DURING this render,
    # not ALL ffmpeg processes system-wide.
    remaining = _get_ffmpeg_pids() - pre_pids
    if remaining:
        logger.warning(
            "ffmpeg still alive after %ds — killing %d process(es): %s",
            timeout, len(remaining), sorted(remaining),
        )
        for ff_pid in remaining:
            try:
                os.kill(ff_pid, signal.SIGKILL)
                logger.warning("Killed stuck ffmpeg PID %d", ff_pid)
            except ProcessLookupError:
                pass


def _get_available_memory_mb() -> Optional[int]:
    """Return available physical memory in MB, or None if unavailable.
    
    v3 (Jul 2026): Uses MemAvailable from /proc/meminfo instead of
    SC_AVPHYS_PAGES. The latter only counts "free" pages and ignores
    reclaimable page cache (buffers), which can be 10+ GB on Linux.
    This caused false OOM alarms that aborted renders unnecessarily.
    Falls back to sysconf if /proc/meminfo is unavailable.
    """
    try:
        # Primary: /proc/meminfo (includes reclaimable cache)
        with open("/proc/meminfo", "r") as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    kb = int(line.split()[1])
                    return kb // 1024
        # Fallback to sysconf (legacy, known to underreport)
        import os as _os
        avail_bytes = _os.sysconf('SC_AVPHYS_PAGES') * _os.sysconf('SC_PAGE_SIZE')
        return avail_bytes // (1024 * 1024)
    except Exception:
        return None

# Critical memory threshold for video rendering (MB).
# Below this, the memory watcher will abort rendering to prevent OOM kill.
VIDEO_MEMORY_GUARD_MB = settings.VIDEO_MEMORY_GUARD_MB

# Dedicated thread pool for pipeline blocking work — avoids starving
# the default asyncio executor (which has limited threads).
_PIPELINE_EXECUTOR = concurrent.futures.ThreadPoolExecutor(
    max_workers=4,
    thread_name_prefix="autotube-pipeline-",
)

# Phase timeouts in seconds (generous ceilings with cooperative cancellation)
_TTS_PHASE_TIMEOUT = int(os.environ.get("TTS_PHASE_TIMEOUT_SEC", "21600"))  # 6h default
PHASE_TIMEOUTS = {
    "scrape":   None,   # no global limit — each scraper has its own 8s timeout
    "script":   3600,   # 60 min (sequential block generation with stop_event)
    "pre_validate": 30, # 30s (cheap sanity checks, no LLM calls)
    "tts":      _TTS_PHASE_TIMEOUT,   # configurable, default 6h (Kokoro CPU)
    "media":    5400,   # 90 min ceiling (scaled by scene count in media_fetcher; outer timeout is safety net for hung providers)
    "video":    None,   # infinite (no ceiling for MoviePy rendering)
    "metadata": 300,    # 5 min (LLM)
    "post_validate": 180,  # 3 min (ffprobe checks + possible LLM auto-fix regen)
    "upload":   3600,   # 60 min (YouTube resumable upload) — raised from 1800
}

# ── Global render lock (defense in depth) ────────────────────
# v4 (Jul 2026): Only ONE video render at a time across all channels.
# Prevents concurrent ffmpeg instances from causing RAM pressure that
# kills decoders mid-render (the root cause of "all images, no minivideos").
# Per-scene segment rendering already caps RAM per render, but two concurrent
# renders could still exceed total system memory.
_RENDER_SEMAPHORE = asyncio.Semaphore(1)

# ── Global dispatch lock (prevents TOCTOU race) ─────────────────
# v5 (Jul 2026): All dispatch entry points (manual click, planned slots,
# due schedules, priority dispatcher) must acquire this lock BEFORE
# checking count_active_jobs() and creating the job. This closes the
# TOCTOU window where two dispatchers pass the guard simultaneously.
# Uses threading.Lock() because dispatch callers include both sync
# (process_planned_slots) and async (generate_video) functions.
import threading
_DISPATCH_LOCK = threading.Lock()

# Phase order for resume logic (must match execution order)
_PHASE_ORDER = ["scrape", "script", "pre_validate", "tts", "media", "video", "metadata", "post_validate", "upload"]


# ── Helper functions ──────────────────────────────────────────

# Registry of active orchestrators for cooperative cancellation
_active_orchestrators: dict[int, object] = {}  # job_id → orchestrator

# Registry of active renders — prevents test-mode renders from competing
# with production renders for RAM and ffmpeg resources.
_active_renders: dict[int, dict] = {}  # channel_id → {"job_id", "mode", "started_at"}

def register_orchestrator(job_id: int, orch):
    """Register orchestrator for cooperative cancellation."""
    _active_orchestrators[job_id] = orch

def unregister_orchestrator(job_id: int):
    """Remove orchestrator from registry."""
    _active_orchestrators.pop(job_id, None)

def cancel_job(job_id: int) -> bool:
    """Request cooperative cancellation of a running job. Returns True if found."""
    orch = _active_orchestrators.get(job_id)
    if orch and hasattr(orch, 'request_stop'):
        orch.request_stop()
        return True
    return False


def _get_db():
    """Lazy DB connection (cached per-module lifetime)."""
    global _broadcast_db
    if _broadcast_db is None:
        _broadcast_db = ExtendedDatabase()
    return _broadcast_db


_broadcast_db = None  # module-level cached ExtendedDatabase instance


async def _broadcast_progress(job_id: int, progress: int, phase: str,
                               message: str, status: str = "running",
                               video_id: int = None, **kwargs):
    """Broadcast progress via WebSocket and update the job record."""
    from api.progress import get_progress_manager

    # ── Zombie-thread guard ──────────────────────────────────
    # If the error handler already marked this job as failed/completed
    # but a zombie pipeline thread is still emitting progress with
    # status="running", silently ignore the callback.
    if status == "running":
        try:
            db_check = _get_db()
            current = db_check.get_job(job_id)
            if current and current.get("status") in ("failed", "completed"):
                return  # zombie thread cannot resurrect a dead job
        except Exception:
            pass  # if DB check fails, proceed normally (fail-open)

    mgr = get_progress_manager()
    data = {
        "job_id": job_id,
        "status": status,
        "progress": progress,
        "phase": phase,
        "message": message,
    }
    if video_id:
        data["video_id"] = video_id
    for k in ("sub_phase", "detail", "current", "total", "preview_url"):
        if k in kwargs and kwargs[k] is not None:
            data[k] = kwargs[k]
    try:
        await mgr.broadcast(job_id, data)
    except Exception as e:
        logger.warning(f"Broadcast failed for job {job_id}: {e}")
    try:
        db = _get_db()
        db.update_job(job_id, progress=progress, phase=phase, status=status)
    except Exception as e:
        logger.warning(f"DB update_job failed for job {job_id}: {e}")


async def _run_in_executor(fn, *args, timeout: int = None, phase: str = None,
                           memory_guard: bool = True):
    """Run a blocking function in the pipeline thread pool with optional timeout.
    
    Returns (True, result) on success or (False, error_message) on failure/timeout.
    On timeout, attempts to cancel the thread future (best-effort).
    
    When phase='video' and memory_guard=True, enables memory guard monitoring
    that WARNs (but no longer kills ffmpeg) if free RAM drops below
    VIDEO_MEMORY_GUARD_MB.  v4 (Jul 2026): per-scene segment rendering caps
    RAM at ~400 MB, so OOM during render is impossible — the guard is now
    a safety net, not a render-killer.
    """
    loop = asyncio.get_running_loop()
    global _RENDER_ACTIVE
    future = None
    stop_monitor = asyncio.Event()
    monitor_task = None
    start_mem_mb = _get_available_memory_mb()
    
    # Log memory at phase start
    if start_mem_mb is not None:
        phase_label = phase or "unknown"
        logger.info("Phase '%s' starting: %d MB free RAM", phase_label, start_mem_mb)
    
    # Pre-phase orphan cleanup: kill any leftover ffmpeg processes from prior phases
    _kill_orphaned_ffmpeg()
    
    # ── Protect active render ffmpeg workers from concurrent job cleanup ──
    # When a video-phase render is in progress, _kill_orphaned_ffmpeg() skips
    # Layer 1 (children of this PID) to avoid killing legitimate ffmpeg workers
    # that belong to the render (which runs in the same process via thread pool).
    if phase == "video":
        _RENDER_ACTIVE = True

    async def _memory_monitor():
        """Background task: monitor available RAM and warn if critically low.
        
        v4: No longer kills ffmpeg — segmented rendering caps per-scene RAM
        at ~400 MB, making OOM during render impossible.  Warnings are retained
        as a safety net to surface systemic memory issues (e.g. concurrent renders).
        """
        LOW_WARN_MB = settings.LOW_MEMORY_WARN_MB
        while not stop_monitor.is_set():
            await asyncio.sleep(10)
            if stop_monitor.is_set():
                return
            avail_mb = _get_available_memory_mb()
            if avail_mb is None:
                continue
            if avail_mb < VIDEO_MEMORY_GUARD_MB:
                logger.warning(
                    "Low memory: %d MB available (guard=%d MB). Render continues — "
                    "per-scene segment RAM is capped at ~400 MB, OOM impossible.",
                    avail_mb, VIDEO_MEMORY_GUARD_MB,
                )
            elif avail_mb < LOW_WARN_MB:
                logger.warning(
                    "⚠️  Low memory: %d MB free (warning at %d MB, critical at %d MB)",
                    avail_mb, LOW_WARN_MB, VIDEO_MEMORY_GUARD_MB,
                )
    
    try:
        # ── Snapshot pre-render ffmpeg PIDs for video phase ──
        # We'll wait for new ones to exit naturally after success,
        # instead of killing them mid-muxing (which corrupts the output).
        _pre_pids: set[int] = set()
        if phase == "video":
            _pre_pids = _get_ffmpeg_pids()

        if args:
            future = loop.run_in_executor(_PIPELINE_EXECUTOR, fn, *args)
        else:
            future = loop.run_in_executor(_PIPELINE_EXECUTOR, fn)
        
        # Enable memory guard for video phase (most memory-intensive)
        if phase == "video" and memory_guard:
            start_mem = _get_available_memory_mb()
            logger.info(
                "Memory guard active for video phase: %s MB free, guard at %d MB",
                f"{start_mem}" if start_mem else "unknown", VIDEO_MEMORY_GUARD_MB,
            )
            monitor_task = asyncio.create_task(_memory_monitor())
        
        result = await asyncio.wait_for(future, timeout=timeout)
        
        # Log memory at phase end
        end_mem_mb = _get_available_memory_mb()
        if end_mem_mb is not None and start_mem_mb is not None:
            delta = end_mem_mb - start_mem_mb
            logger.info(
                "Phase '%s' complete: %d MB free (delta=%+d MB)",
                phase_label, end_mem_mb, delta,
            )
        
        # Post-phase cleanup: for video, wait for ffmpeg to exit naturally
        # (muxing audio+video into MP4 container can take a few seconds).
        # For other phases, kill orphans immediately as they shouldn't exist.
        if phase == "video":
            _wait_for_ffmpeg_exit(_pre_pids, timeout=30)
        else:
            _kill_orphaned_ffmpeg()
        
        return True, result
        
    except asyncio.CancelledError:
        logger.warning("Video rendering aborted by memory guard")
        _kill_orphaned_ffmpeg()
        return False, "Render abortado: memoria insuficiente (memory guard activated)"
        
    except asyncio.TimeoutError:
        if future is not None:
            cancelled = future.cancel()
            logger.warning(
                "Phase timeout after %ds — attempted thread cancel: %s",
                timeout, "ok" if cancelled else "thread may still be running"
            )
            # Cooperative cancellation: signal orchestrator to stop at next checkpoint
            if not cancelled and hasattr(fn, '__self__'):
                orch = fn.__self__
                if hasattr(orch, 'request_stop'):
                    try:
                        orch.request_stop()
                        logger.info("Cooperative stop requested via orchestrator.request_stop()")
                    except Exception:
                        pass
        # On timeout, kill ffmpeg orphans to free memory
        _kill_orphaned_ffmpeg()
        return False, f"Timeout tras {timeout}s"
        
    except Exception as e:
        if future is not None:
            future.cancel()
        logger.exception(f"Executor task failed: {e}")
        # On failure, kill ffmpeg orphans to free memory
        _kill_orphaned_ffmpeg()
        return False, str(e)[:300]
        
    finally:
        # ── Unprotect ffmpeg workers when render phase ends ──
        if phase == "video":
            _RENDER_ACTIVE = False
        
        stop_monitor.set()
        if monitor_task is not None:
            monitor_task.cancel()
            try:
                await monitor_task
            except (asyncio.CancelledError, Exception):
                pass


async def _run_video_in_subprocess(
    canal: str,
    video_id: int,
    job_id: int,
    script: dict,
    audio_data: dict,
    media_assets: dict,
    test_mode: bool,
    orch = None,
) -> tuple[bool, dict | str]:
    """Run the video render phase in an isolated subprocess.

    The subprocess runs ``pipeline_worker.run_video_render()`` which
    executes MoviePy/FFmpeg rendering in its own address space.  When the
    subprocess dies the OS reclaims **all** memory — no Python heap
    fragmentation accumulates in the long-lived uvicorn server.

    The parent polls the DB for progress updates written by the worker
    and broadcasts them via WebSocket.  After the render completes, the
    parent generates the thumbnail (Pollo AI HTTP call — low memory).

    Returns ``(True, video_data_dict)`` on success or
    ``(False, error_message)`` on failure — compatible with the existing
    ``_run_in_executor`` return convention.
    """
    import json as _json
    from pathlib import Path as _Path

    # ── 1. Serialize data for the subprocess ───────────────────
    # Extract bloques from script (handles both raw JSON string
    # and already-parsed list).
    _bloques_raw = script.get("bloques") or script.get("bloques_json", "[]")
    if isinstance(_bloques_raw, str):
        bloques_json = _bloques_raw
    else:
        bloques_json = _json.dumps(_bloques_raw, ensure_ascii=False)

    # Media assets (always a dict with 'assets' key from checkpoint)
    _assets_list = media_assets.get("assets", []) if isinstance(media_assets, dict) else (media_assets or [])
    media_assets_json = _json.dumps(_assets_list, ensure_ascii=False, default=str)

    # Scene ranges (may be None / present in media checkpoint)
    _ranges = media_assets.get("scene_ranges") if isinstance(media_assets, dict) else None
    scene_ranges_json = _json.dumps(_ranges, ensure_ascii=False) if _ranges else ""

    # Audio + timestamps (use timestamps_path from checkpoint, fall back to file next to audio)
    audio_path = str(audio_data.get("audio_path", ""))
    ts_path = audio_data.get("timestamps_path", "")
    # Invalidate stored path if file doesn't exist (triggers auto-fallback below).
    # Fixes B5/B6: orchestrator stores {stem}.json but TTS writes {stem}_timestamps.json.
    if ts_path and not _Path(ts_path).exists():
        ts_path = ""
    if not ts_path and audio_path:
        _base = _Path(audio_path)
        for _c in (
            _base.with_name(f"{_base.stem}_timestamps.json"),
            _base.with_suffix(".json"),
        ):
            if _c.exists():
                ts_path = str(_c)
                break

    cta_audio = str(audio_data.get("cta_audio_path", "") or "")

    # ── 2. Spawn subprocess ────────────────────────────────────
    _bloque_count = 0
    if bloques_json:
        try:
            _bloque_count = len(_json.loads(bloques_json))
        except Exception:
            pass
    logger.info(
        "Spawning render subprocess: canal=%s video=%d job=%d "
        "bloques=%d assets=%d test_mode=%s",
        canal, video_id, job_id, _bloque_count, len(_assets_list), test_mode,
    )

    ctx = multiprocessing.get_context("spawn")
    # Use a multiprocessing.Queue so the child can send the result back.
    result_queue: multiprocessing.Queue = ctx.Queue(maxsize=1)

    from api.services.pipeline_worker import run_video_render

    p = ctx.Process(
        target=run_video_render,
        args=(
            canal, video_id, job_id,
            bloques_json, media_assets_json,
            audio_path, ts_path,
            scene_ranges_json, cta_audio,
            test_mode, result_queue,
        ),
        daemon=False,  # we join() explicitly
        name=f"autotube-render-{job_id}",
    )
    p.start()
    logger.info("Render subprocess started: PID=%d", p.pid)

    # ── 2b. Release serialized data from parent RAM ────────────
    # After the subprocess starts, the JSON strings and asset list are
    # no longer needed in the parent.  Free them immediately so uvicorn
    # doesn't hold 100+ KB of duplicated data during the render wait.
    try:
        del bloques_json, media_assets_json, scene_ranges_json
        # ⚠️ Keep _assets_list — needed later for thumbnail & scene_images (line ~704)
        try:
            del _bloque_count
        except Exception:
            pass
        import gc
        gc.collect()
        try:
            import ctypes
            ctypes.CDLL("libc.so.6").malloc_trim(0)
        except Exception:
            pass
    except Exception as _cleanup_exc:
        logger.debug("Post-spawn cleanup: %s", _cleanup_exc)

    # ── 3. Poll DB for progress while the subprocess runs ──────
    last_pct = 60
    last_phase = "video"
    db = _get_db()
    stop_poll = asyncio.Event()

    async def _poll_loop():
        nonlocal last_pct, last_phase
        while not stop_poll.is_set():
            try:
                v = db.get_video(video_id)
                if v:
                    pct = v.get("progress", 0) or 60
                    ph = v.get("progress_phase", "") or "video"
                    if pct != last_pct or ph != last_phase:
                        await _broadcast_progress(
                            job_id, pct, ph,
                            f"Renderizando... {pct}%",
                            video_id=video_id,
                        )
                        last_pct, last_phase = pct, ph
            except Exception:
                pass
            await asyncio.sleep(5)

    poll_task = asyncio.ensure_future(_poll_loop())

    # ── 4. Wait for subprocess (non-blocking poll) ─────────────
    _MAX_RENDER_SEC = 28800  # 8h ceiling for very long renders
    _deadline = time.time() + _MAX_RENDER_SEC

    try:
        while p.is_alive():
            if time.time() > _deadline:
                logger.error("Render subprocess timed out after %ds — killing PID %d",
                             _MAX_RENDER_SEC, p.pid)
                p.terminate()
                await asyncio.sleep(5)
                if p.is_alive():
                    p.kill()
                break
            await asyncio.sleep(2)
        p.join(timeout=10)
    except asyncio.CancelledError:
        logger.warning("Job cancelled — terminating render subprocess PID %d", p.pid)
        p.terminate()
        await asyncio.sleep(3)
        if p.is_alive():
            p.kill()
        p.join(timeout=5)
        raise  # re-raise so the caller's async with releases the semaphore
    except Exception:
        pass
    finally:
        stop_poll.set()
        if not poll_task.done():
            poll_task.cancel()
            try:
                await poll_task
            except asyncio.CancelledError:
                pass

    # ── 4b. Read Queue result (IPC, more reliable) ──────────────
    _queue_result = None
    _queue_err = None
    try:
        _queue_result = result_queue.get_nowait()
        if _queue_result:
            _qr = _json.loads(_queue_result)
            if _qr.get("success") and _qr.get("video_path"):
                logger.info("Got video_path from Queue: %s", _qr["video_path"])
            elif _qr.get("error"):
                _queue_err = _qr["error"]
                logger.warning("Render subprocess reported error via Queue: %s",
                               _queue_err[:300])
    except Exception:
        pass

    # ── 5. Collect result ──────────────────────────────────────
    exit_code = p.exitcode if p.exitcode is not None else -1

    def _real_render_error() -> str:
        """Surface the REAL render error instead of a generic message.

        Priority: worker's Queue error → video.error_message (written by
        run_video_render on failure) → generic fallback.
        """
        if _queue_err:
            return f"Render falló: {_queue_err}"
        try:
            v = db.get_video(video_id)
            if v:
                if v.get("error_message"):
                    return f"Render falló: {v['error_message']}"
                if v.get("progress_phase") == "video_failed":
                    return "Render falló (subprocess terminó en estado video_failed)"
        except Exception:
            pass
        return "Subprocess exited OK but no output file found"

    if exit_code == 0:
        # Read result from DB as primary source (survives parent restart).
        try:
            v = db.get_video(video_id)
            ck_raw = v.get("checkpoint_data", "{}") if v else "{}"
            ck = _json.loads(ck_raw) if isinstance(ck_raw, str) else (ck_raw or {})
            video_ck = ck.get("video", {})
            video_path = video_ck.get("video_path", "")
            if video_path and _Path(video_path).exists():
                logger.info("Render subprocess completed: %s", video_path)

                # ── Extract title from script (needed for both paths) ──
                _titulo_raw = script.get("titulo_options", "[]")
                if isinstance(_titulo_raw, str):
                    _titles = _json.loads(_titulo_raw)
                else:
                    _titles = _titulo_raw or []
                _titulo = _titles[0] if _titles else "Sin titulo"

                # ── Return render result (thumbnail generated outside semaphore) ──
                return True, {
                    "video_path": video_path,
                    "thumbnail_path": "",  # thumbnail generated later in start_generation_job
                    "titulo": _titulo,
                }
            else:
                # exit 0 with no output = worker caught an exception but the
                # parent previously masked it as "no video_path". Report the
                # real error (Queue / error_message) now.
                real_err = _real_render_error()
                logger.error("Render subprocess exited 0 but render actually failed: %s", real_err)
                return False, real_err
        except Exception as db_exc:
            logger.exception("Failed to read subprocess result from DB: %s", db_exc)
            return False, f"Render subprocess exited OK but DB read failed: {db_exc}"
    else:
        # Subprocess failed — try to get error from Queue or DB
        error_msg = f"Render subprocess exited with code {exit_code}"
        try:
            v = db.get_video(video_id)
            if v and v.get("progress_phase") == "video_failed":
                error_msg = _real_render_error()
        except Exception:
            pass

        logger.error("Render subprocess failed: exit_code=%d", exit_code)

        # Kill any orphaned ffmpegs left by the dead subprocess
        _kill_orphaned_ffmpeg()

        return False, error_msg


def _phase_index(phase_name: str) -> int:
    """Return the index of a phase in the execution order (-1 if not found)."""
    try:
        return _PHASE_ORDER.index(phase_name)
    except ValueError:
        return -1


def _load_checkpoint(video_id: int) -> tuple[dict, str, int]:
    """Load checkpoint data from a video record.
    
    Returns (checkpoint_dict, last_completed_phase, phase_index).
    """
    db = _get_db()
    video = db.get_video(video_id)
    if not video:
        return {}, "", -1
    
    raw = video.get("checkpoint_data", "{}")
    try:
        checkpoint = json.loads(raw) if isinstance(raw, str) else (raw or {})
    except (json.JSONDecodeError, TypeError):
        checkpoint = {}
    
    last_phase = video.get("progress_phase", "") or ""
    idx = _phase_index(last_phase)
    
    logger.info("📋 Checkpoint: video_id=%d last_phase=%s idx=%d data_keys=%s",
                video_id, last_phase, idx, list(checkpoint.keys()))
    return checkpoint, last_phase, idx


def _save_checkpoint(video_id: int, phase: str, data: dict):
    """Save checkpoint data to the video record after a phase completes."""
    db = _get_db()
    
    existing, _, _ = _load_checkpoint(video_id)
    existing[phase] = data
    
    db.update_video(video_id,
                    progress_phase=phase,
                    checkpoint_data=json.dumps(existing, ensure_ascii=False))
    logger.info("💾 Checkpoint saved: video_id=%d phase=%s keys=%s",
                video_id, phase, list(data.keys())[:5])


# ── Auto-retry for transient failures ────────────────────────

def _auto_retry_if_transient(job_id: int, video_id: int):
    """Requeue a failed job if the error is transient (timeout, OOM, broken pipe).
    
    Only retries up to 3 times. Non-transient errors (API errors, code bugs)
    are left as permanently failed.
    """
    db = _get_db()
    job = db.get_job(job_id)
    if not job or job.get("status") != "failed":
        return  # not failed or already handled
    
    error_msg = (job.get("error_msg") or "").lower()
    TRANSIENT_PATTERNS = [
        "timeout", "memory guard", "broken pipe", "brokenpipe",
        "orphaned: process lost", "memory", "abortado: memoria",
    ]
    is_transient = any(p in error_msg for p in TRANSIENT_PATTERNS)
    
    if not is_transient:
        return  # permanent failure — don't retry
    
    retries = db.increment_retry(job_id)
    max_retries = settings.MAX_RETRY_ATTEMPTS
    if retries >= max_retries:
        logger.warning(
            "Job #%d: %d retries exhausted — giving up. Error: %s",
            job_id, retries, job.get("error_msg", "")[:200],
        )
        return
    
    # Requeue: reset status so queue consumer picks it up
    db.update_job_requeue(job_id, error_msg=job.get("error_msg", "")[:200])
    logger.info(
        "Job #%d auto-requeued (retry %d/%d). Error: %s",
        job_id, retries, max_retries, job.get("error_msg", "")[:150],
    )


# ── Generation Job (with checkpoint/resume) ──────────────────

async def start_generation_job(job_id: int, channel_id: int, video_id: int,
                                 action: str, content_id: int = None,
                                 resume: bool = False, test_mode: bool = False,
                                 upload: bool = True,
                                 source_mode: str = "original",
                                 viral_candidate_id: int = None):
    """Run the full pipeline as an async background job.

    When test_mode=True: low resolution (480x270), no upload, no effects,
    ultrafast preset — for rapid algorithm validation.

    When upload=False: full quality generation but skip YouTube upload.
    Video stays in 'ready' status for manual upload later.
    """
    # ── Test-mode guard: reject tests if a production render is active ──
    if test_mode:
        active_prod = {
            ch_id: info for ch_id, info in _active_renders.items()
            if info.get("mode") == "production"
        }
        if active_prod:
            job_ids = [f"#{info['job_id']}" for info in active_prod.values()]
            msg = (
                f"No se puede ejecutar test: hay {len(active_prod)} render(es) de producción "
                f"activo(s) (job(s) {', '.join(job_ids)}). "
                f"Espera a que termine o cancela el job."
            )
            logger.warning("Test blocked: %s", msg)
            db.update_job(job_id, status="failed", error_msg=msg[:500])
            db.update_video(video_id, status="error", progress_phase="blocked")
            await _broadcast_progress(job_id, 0, "blocked", msg, "failed", video_id,
                                       detail="Render de producción en curso — test bloqueado")
            return
        
        # RAM gate for tests: require at least MIN_FREE_FOR_DISPATCH MB
        avail_mb = _get_available_memory_mb()
        ram_threshold = settings.MIN_FREE_FOR_DISPATCH_MB
        if avail_mb is not None and avail_mb < ram_threshold:
            msg = (
                f"No se puede ejecutar test: solo {avail_mb} MB libres "
                f"(mínimo {ram_threshold} MB requeridos)."
            )
            logger.warning("Test blocked by RAM gate: %s", msg)
            db.update_job(job_id, status="failed", error_msg=msg[:500])
            db.update_video(video_id, status="error", progress_phase="blocked")
            await _broadcast_progress(job_id, 0, "blocked", msg, "failed", video_id,
                                       detail="RAM insuficiente — test bloqueado")
            return
    
    # ── Pre-flight: kill any lingering ffmpeg processes to start with clean RAM ──
    _kill_orphaned_ffmpeg()

    # ── Register this render as active ──
    render_mode = "test" if test_mode else "production"
    _active_renders[channel_id] = {
        "job_id": job_id,
        "mode": render_mode,
        "started_at": time.time(),
    }
    logger.info("📌 Render registered: channel=%d, mode=%s, job=%d", channel_id, render_mode, job_id)
    
    db = _get_db()
    ch = db.get_channel(channel_id)
    if not ch:
        await _broadcast_progress(job_id, 0, "error", "Canal no encontrado", "failed")
        return
    
    canal = ch["slug"]
    channel_name = ch.get("name", canal)
    db.update_job(job_id, status="running", started_at=None)
    db.update_video(video_id, generation_started_at=db_now())
    
    await _broadcast_progress(job_id, 1, "inicio", f"Iniciando generacion para {channel_name}...",
                                video_id=video_id, detail="Preparando pipeline")
    
    checkpoint, last_phase, last_idx = _load_checkpoint(video_id)
    
    if resume or last_idx >= 0:
        start_idx = last_idx + 1
        start_phase = _PHASE_ORDER[start_idx] if start_idx < len(_PHASE_ORDER) else "done"
    else:
        start_idx = 0
        start_phase = "scrape"
    
    logger.info("🚀 Job %d for %s: start_phase=%s (resume=%s, last_phase=%s)",
                job_id, canal, start_phase, resume, last_phase)
    
    if resume or last_idx >= 0:
        await _broadcast_progress(job_id, 2, "inicio", f"Reanudando desde fase: {start_phase}",
                                   video_id=video_id, detail="Cargando checkpoint...")
    
    # Reload cached data from checkpoint
    script = checkpoint.get("script")
    audio_data = checkpoint.get("tts")
    media_assets_raw = checkpoint.get("media", {}) if checkpoint.get("media") else None
    
    # Augment checkpoint script with full DB record so phase_video
    # has access to guion, bloques_json, escenas_json, etc.
    if script and script.get("id") and not script.get("guion"):
        full = db.get_script(script["id"])
        if full:
            script = full
    
    if media_assets_raw:
        media_assets = {
            "assets": media_assets_raw.get("assets", []),
            "scene_ranges": media_assets_raw.get("scene_ranges"),
        }
    else:
        media_assets = None
    video_data = checkpoint.get("video")
    metadata = checkpoint.get("metadata")
    
    try:
        from orchestrator import PipelineOrchestrator
        
        loop = asyncio.get_running_loop()
        
        def _progress_cb(percent: int, phase: str, message: str, **kwargs):
            asyncio.run_coroutine_threadsafe(
                _broadcast_progress(job_id, percent, phase, message, video_id=video_id, **kwargs),
                loop,
            )
        
        orch = PipelineOrchestrator(canal=canal, db_video_id=video_id,
                                      progress_callback=_progress_cb,
                                      source_mode=source_mode,
                                      viral_candidate_id=viral_candidate_id)
        register_orchestrator(job_id, orch)

        # ── Fast-test mode: apply unified test profile ──────────
        if test_mode:
            from config.test_profile import apply_test_profile, get_test_word_targets
            apply_test_profile(orch.config, mode="fast")
            # Word/block targets: the orchestrator config may not have
            # TEST_SCRIPT_* from DB — bridge from the canal config module
            try:
                canal_module = importlib.import_module(f"config.{canal}_config")
                _, _, _, _, dur_target = get_test_word_targets(canal_module, mode="quarter")
                orch.config.TEST_SCRIPT_WORDS_MIN = getattr(canal_module, "TEST_SCRIPT_WORDS_MIN", 300)
                orch.config.TEST_SCRIPT_WORDS_MAX = getattr(canal_module, "TEST_SCRIPT_WORDS_MAX", 700)
                orch.config.TEST_SCRIPT_BLOCKS_MIN = getattr(canal_module, "TEST_SCRIPT_BLOCKS_MIN", 3)
                orch.config.TEST_SCRIPT_BLOCKS_MAX = getattr(canal_module, "TEST_SCRIPT_BLOCKS_MAX", 8)
                orch.config.TEST_VIDEO_DURATION_TARGET = dur_target
            except Exception:
                pass
            cfg = orch.config
            logger.info("Test mode enabled: %dx%d ultrafast no-effects max-%d-blocks no-upload (unified profile)",
                         cfg.VIDEO_RESOLUTION[0], cfg.VIDEO_RESOLUTION[1], cfg.MAX_SCRIPT_BLOCKS)

        # ── Phase 0: Scrape ──────────────────────────────────
        if _phase_index("scrape") < start_idx:
            logger.info("Skipping scrape (already completed)")
        elif test_mode:
            unused = db.get_unused_count(canal)
            if unused > 0:
                logger.info("Test mode: skipping scrape — %d unused items in DB", unused)
                await _broadcast_progress(job_id, 12, "scrape",
                    f"Test mode: scraping omitido. {unused} contenidos disponibles.",
                    video_id=video_id, detail=f"Se usaran contenidos existentes ({unused} items)")
                _save_checkpoint(video_id, "scrape", {"items_added": 0, "skipped": True})
                orch._timing["phases"]["scrape"] = 0
            else:
                logger.info("Test mode: no unused content — running scrape")
                ok, result = await _run_in_executor(orch.phase_scrape, timeout=PHASE_TIMEOUTS["scrape"])
                if not ok:
                    await _broadcast_progress(job_id, 5, "scrape", f"Error en scraping: {result}",
                                               "failed", video_id)
                    db.update_video(video_id, status="error", progress_phase="scrape",
                                    timing_data=orch.collect_timing_json())
                    return
                items = result if isinstance(result, int) else 0
                await _broadcast_progress(job_id, 12, "scrape",
                    "Contenido listo para generar guion",
                    video_id=video_id,
                    detail=f"Se encontraron {items} nuevos items" if items else "Contenido disponible")
                _save_checkpoint(video_id, "scrape", {"items_added": items})
                db.update_video(video_id, timing_data=orch.collect_timing_json())
        else:
            await _broadcast_progress(job_id, 3, "scrape", "Buscando nuevo contenido...",
                                       video_id=video_id, detail="Conectando con fuentes de contenido")
            ok, result = await _run_in_executor(orch.phase_scrape, timeout=PHASE_TIMEOUTS["scrape"])
            if not ok:
                unused = db.get_unused_count(canal)
                if unused > 0:
                    logger.warning("Scrape failed but %d unused items in DB — proceeding", unused)
                    await _broadcast_progress(job_id, 12, "scrape",
                        f"Scraping no disponible. Usando {unused} contenidos existentes.",
                        video_id=video_id, detail=f"{unused} items disponibles en BD")
                else:
                    await _broadcast_progress(job_id, 5, "scrape", f"Error en scraping: {result}",
                                               "failed", video_id)
                    db.update_video(video_id, status="error", progress_phase="scrape",
                                    timing_data=orch.collect_timing_json())
                    return
            else:
                items = result if isinstance(result, int) else 0
                await _broadcast_progress(job_id, 12, "scrape",
                    "Contenido listo para generar guion",
                    video_id=video_id,
                    detail=f"Se encontraron {items} nuevos items" if items else "Contenido disponible")
                _save_checkpoint(video_id, "scrape", {"items_added": items})
                db.update_video(video_id, timing_data=orch.collect_timing_json())
        
        # ── Phase 1: Script ──────────────────────────────────
        if _phase_index("script") < start_idx:
            logger.info("Skipping script (loaded from checkpoint)")
            await _broadcast_progress(job_id, 25, "script", "Guion cargado desde checkpoint",
                                       video_id=video_id)
        else:
            await _broadcast_progress(job_id, 15, "script", "Generando guion con IA...",
                                       video_id=video_id, detail="Consultando modelo de lenguaje...")
            
            ok, script = await _run_in_executor(orch.phase_generate_script,
                                                  timeout=PHASE_TIMEOUTS["script"])
            if not ok:
                await _broadcast_progress(job_id, 15, "script",
                    f"Error en generacion de guion: {script}", "failed", video_id)
                db.update_video(video_id, status="error", progress_phase="script",
                                timing_data=orch.collect_timing_json())
                return
            
            if not script:
                await _broadcast_progress(job_id, 17, "script",
                    "Sin contenido. Reintentando scrape...",
                    video_id=video_id, detail="Reintentando busqueda de contenido...")
                await asyncio.sleep(5)
                ok2, _ = await _run_in_executor(orch.phase_scrape,
                                                  timeout=PHASE_TIMEOUTS["scrape"])
                if ok2:
                    ok, script = await _run_in_executor(orch.phase_generate_script,
                                                          timeout=PHASE_TIMEOUTS["script"])
                    if not ok:
                        await _broadcast_progress(job_id, 17, "script",
                            "Error en retry de guion", "failed", video_id)
                        db.update_video(video_id, status="error", progress_phase="script",
                                        timing_data=orch.collect_timing_json())
                        return
                elif db.get_unused_count(canal) > 0:
                    logger.warning("Scrape retry failed but unused content available")
                    ok, script = await _run_in_executor(orch.phase_generate_script,
                                                          timeout=PHASE_TIMEOUTS["script"])
                    if not ok or not script:
                        await _broadcast_progress(job_id, 17, "script",
                            "Error en retry de guion", "failed", video_id)
                        db.update_video(video_id, status="error", progress_phase="script",
                                        timing_data=orch.collect_timing_json())
                        return
                else:
                    logger.warning("Scrape retry also failed — no content available")
            
            if not script:
                await _broadcast_progress(job_id, 20, "script",
                    "Error: No se pudo generar el guion (sin contenido disponible)",
                    "failed", video_id)
                db.update_video(video_id, status="error", progress_phase="script",
                                timing_data=orch.collect_timing_json())
                return
            
            db.update_video(video_id, progress=25, progress_phase="script",
                            script_id=script.get("id"))
            titulo = script.get('titulo_selected', 'Sin titulo')[:60]
            n_escenas = len(script.get("escenas", [])) if isinstance(script.get("escenas"), list) else 0
            await _broadcast_progress(job_id, 25, "script",
                f"Guion generado: {titulo}",
                video_id=video_id,
                detail=f"Estructura: {n_escenas} escenas" if n_escenas else "Guion completado")
            _save_checkpoint(video_id, "script",
                {"id": script.get("id"), "titulo": titulo,
                 "guion": script.get("guion", ""),
                 "bloques_json": script.get("bloques_json", []),
                 "escenas_json": script.get("escenas_json", []),
                 "titulo_options": script.get("titulo_options", [])})
            db.update_video(video_id, timing_data=orch.collect_timing_json())
        
        # ── Phase 1.5: Pre-validation (early gate) ─────────────
        if _phase_index("pre_validate") < start_idx:
            logger.info("Skipping pre-validation (loaded from checkpoint)")
        else:
            await _broadcast_progress(job_id, 27, "pre_validate",
                "Validando calidad del guion...",
                video_id=video_id, detail="Verificando titulo, estructura y duracion estimada")
            ok, val_result = await _run_in_executor(
                orch.phase_pre_validate, script,
                timeout=PHASE_TIMEOUTS["pre_validate"],
            )
            if not ok:
                await _broadcast_progress(job_id, 27, "pre_validate",
                    f"Pre-validacion fallida: {val_result}",
                    "failed", video_id,
                    detail="El guion no paso los checks de calidad minimos")
                db.update_video(video_id, status="error", progress_phase="pre_validate",
                                timing_data=orch.collect_timing_json())
                return
            _save_checkpoint(video_id, "pre_validate", {"passed": True})

        # ── Phase 2: TTS ─────────────────────────────────────
        if _phase_index("tts") < start_idx:
            logger.info("Skipping TTS (loaded from checkpoint)")
            await _broadcast_progress(job_id, 40, "tts", "Audio cargado desde checkpoint",
                                       video_id=video_id)
        else:
            await _broadcast_progress(job_id, 30, "tts", "Generando voz con IA (TTS)...",
                                        video_id=video_id,
                                        detail="Procesando texto a voz (puede tardar varios minutos)")
            
            _TTS_ACTIVE = True  # Bug B: protect TTS ffmpeg from orphan killer
            try:
                ok, audio_data = await _run_in_executor(orch.phase_tts, script, job_id,
                                                            timeout=PHASE_TIMEOUTS["tts"], phase="tts")
            finally:
                _TTS_ACTIVE = False
            if not ok:
                await _broadcast_progress(job_id, 30, "tts", f"Error TTS: {audio_data}",
                                           "failed", video_id)
                db.update_video(video_id, status="error", progress_phase="tts",
                                timing_data=orch.collect_timing_json())
                return
            
            if not audio_data:
                await _broadcast_progress(job_id, 35, "tts",
                    "Error: Fallo la generacion de voz", "failed", video_id)
                db.update_video(video_id, status="error", progress_phase="tts",
                                timing_data=orch.collect_timing_json())
                return
            
            db.update_video(video_id, progress=40, progress_phase="tts")
            await _broadcast_progress(job_id, 40, "tts", "Audio generado correctamente",
                                       video_id=video_id,
                                       detail="Voz sintetizada y lista para ensamblar")
            _save_checkpoint(video_id, "tts", {
                "audio_path": audio_data.get("audio_path", ""),
                "timestamps_path": audio_data.get("timestamps_path", ""),
                "cta_audio_path": audio_data.get("cta_audio_path", ""),
            })
            db.update_video(video_id, timing_data=orch.collect_timing_json())
        
        # ── Phase 3: Media ───────────────────────────────────
        if _phase_index("media") < start_idx:
            logger.info("Skipping media (loaded from checkpoint)")
            n = len(media_assets) if media_assets else 0
            await _broadcast_progress(job_id, 55, "images",
                "Media cargada desde checkpoint",
                video_id=video_id, detail=f"{n} assets disponibles")
        else:
            await _broadcast_progress(job_id, 45, "images",
                "Buscando imagenes y videos...",
                video_id=video_id, detail="Buscando en multiples proveedores de media")
            
            ok, media_assets = await _run_in_executor(orch.phase_media, script, audio_data,
                                                       timeout=PHASE_TIMEOUTS["media"])
            if not ok:
                await _broadcast_progress(job_id, 45, "images",
                    f"Error en busqueda de media: {media_assets}", "failed", video_id)
                db.update_video(video_id, status="error", progress_phase="media",
                                timing_data=orch.collect_timing_json())
                return
            if not media_assets:
                await _broadcast_progress(job_id, 50, "images",
                    "Error: No se encontraron imagenes ni videos", "failed", video_id)
                db.update_video(video_id, status="error", progress_phase="media",
                                timing_data=orch.collect_timing_json())
                return
            
            db.update_video(video_id, progress=55, progress_phase="media")
            n_assets = len(media_assets)
            await _broadcast_progress(job_id, 55, "images",
                f"Media procesada ({n_assets} assets)",
                video_id=video_id,
                detail=f"{n_assets} recursos visuales listos para el video")
            _save_checkpoint(video_id, "media", {
                "assets": [{"type": a.get("type", "?"),
                            "path": str(a.get("path", "")),
                            "source": a.get("source", "?")}
                           for a in (media_assets["assets"] if isinstance(media_assets, dict) else media_assets)]
                if media_assets else [],
                # Preserve scene_ranges for 1:1 alignment on resume
                "scene_ranges": media_assets.get("scene_ranges") if isinstance(media_assets, dict) else None,
            })
            db.update_video(video_id, timing_data=orch.collect_timing_json())
        
        # ── Phase 4: Video Assembly ──────────────────────────
        if _phase_index("video") < start_idx:
            logger.info("Skipping video (loaded from checkpoint)")
            await _broadcast_progress(job_id, 75, "video",
                "Video cargado desde checkpoint", video_id=video_id)
        else:
            await _broadcast_progress(job_id, 60, "video", "Ensamblando video...",
                                       video_id=video_id,
                                       detail="Combinando audio, imagenes y efectos con MoviePy")
            
            # Pre-render memory gate — check total system RAM before spawning subprocess.
            _est_base_gb = 3.0
            _est_rate_gb_per_sec = 8.0 / 1024
            if audio_data and isinstance(audio_data, dict) and audio_data.get("duration"):
                _est_dur_sec = audio_data["duration"]
                _est_ram_gb = _est_base_gb + _est_dur_sec * _est_rate_gb_per_sec
                _avail_mb = _get_available_memory_mb()
                _avail_gb = _avail_mb / 1024 if _avail_mb else None
                logger.info(
                    "Pre-render estimate: ~%.1f GB needed (%ds audio). Available: %s GB",
                    _est_ram_gb, _est_dur_sec,
                    f"{_avail_gb:.1f}" if _avail_gb else "unknown",
                )
                if _avail_gb and _est_ram_gb > _avail_gb * 0.75:
                    logger.warning(
                        "Pre-render memory gate: estimated %.1f GB needed but only %.1f GB available — "
                        "aborting video phase to prevent OOM",
                        _est_ram_gb, _avail_gb,
                    )
                    await _broadcast_progress(job_id, 60, "video",
                        f"Abortado: memoria insuficiente", "failed", video_id)
                    db.update_video(video_id, status="error", progress_phase="video",
                                    timing_data=orch.collect_timing_json())
                    _kill_orphaned_ffmpeg()
                    return
            
            # v4: Acquire global render lock — only one render subprocess at a time.
            # Per-scene segment rendering caps RAM per render at ~400 MB, but two
            # concurrent renders could still exceed total system memory.
            #
            # ── Pre-lock heartbeat emitter ──────────────────────────
            # The semaphore may be held by another job's render/thumbnail.
            # Pulse heartbeats while waiting so the orphan detector (15 min
            # timeout) doesn't kill this job during lock contention.
            _hb_lock_stop = threading.Event()
            _hb_lock_thread = None
            if job_id is not None:
                def _hb_lock_loop():
                    _db = ExtendedDatabase()
                    while not _hb_lock_stop.is_set():
                        try:
                            _db.update_heartbeat(job_id)
                        except Exception:
                            pass
                        _hb_lock_stop.wait(30)
                _hb_lock_thread = threading.Thread(
                    target=_hb_lock_loop, daemon=True,
                    name=f"hb-lock-wait-{job_id}",
                )
                _hb_lock_thread.start()
            try:
                async with _RENDER_SEMAPHORE:
                    # Stop lock-wait heartbeat — render subprocess has its own
                    _hb_lock_stop.set()
                    logger.info("Render lock ACQUIRED for job %d (channel %d)", job_id, channel_id)
                    ok, video_data = await _run_video_in_subprocess(
                        canal=canal, video_id=video_id, job_id=job_id,
                        script=script, audio_data=audio_data,
                        media_assets=media_assets, test_mode=test_mode,
                        orch=orch,
                    )
                    logger.info("Render lock RELEASED for job %d", job_id)
            finally:
                _hb_lock_stop.set()
                if _hb_lock_thread is not None and _hb_lock_thread.is_alive():
                    _hb_lock_thread.join(timeout=2)
            if not ok:
                await _broadcast_progress(job_id, 60, "video",
                    f"Error en ensamblaje: {video_data}", "failed", video_id)
                db.update_video(video_id, status="error", progress_phase="video",
                                timing_data=orch.collect_timing_json())
                return
            if not video_data or not isinstance(video_data, dict):
                await _broadcast_progress(job_id, 75, "video",
                    "Error: Fallo el ensamblaje del video", "failed", video_id)
                db.update_video(video_id, status="error", progress_phase="video",
                                timing_data=orch.collect_timing_json())
                return
            
            # ── Post-render thumbnail generation (outside semaphore) ──
            # Moved here from _run_video_in_subprocess so the render lock is
            # released before the (potentially slow) Pollo AI HTTP call.
            # This prevents one channel's thumbnail from blocking another
            # channel's render.
            _titulo = video_data.get("titulo", "Sin titulo")
            try:
                _keywords_raw = script.get("keywords_json") or script.get("keywords", "[]")
                if isinstance(_keywords_raw, str):
                    _keywords = json.loads(_keywords_raw)
                else:
                    _keywords = _keywords_raw or []

                _scene_images = []
                _assets_list = media_assets.get("assets", []) if isinstance(media_assets, dict) else []
                for a in _assets_list:
                    if a.get("type") == "image" and a.get("path"):
                        _scene_images.append([a["path"]])

                thumbnail_path = orch.thumbnail_maker.make_viral_thumbnail(
                    title=_titulo,
                    overlay_text="",
                    keywords=_keywords,
                    scene_images=_scene_images or [],
                    script_text=script.get("guion", "")[:1500],
                    canal_slug=canal,
                    channel_display_name=getattr(orch.config, "CANAL_DISPLAY_NAME", ""),
                    channel_description=getattr(orch.config, "CHANNEL_ABOUT_SECTION", ""),
                    channel_theme=getattr(orch.config, "CANAL_TAGLINE", ""),
                ) or ""
                if thumbnail_path:
                    video_data["thumbnail_path"] = thumbnail_path
                    logger.info("Thumbnail generated (post-render): %s", thumbnail_path)
            except Exception as thumb_exc:
                logger.warning("Thumbnail generation failed (non-fatal): %s", thumb_exc)

            db.update_video(video_id, progress=75, progress_phase="video",
                            status="ready")  # video done — panel can display it
            try:
                _save_checkpoint(video_id, "video", {
                    "video_path": str(video_data.get("video_path", "")),
                    "thumbnail_path": str(video_data.get("thumbnail_path", "")),
                    "titulo": str(video_data.get("titulo", "")),
                })
            except Exception as _ck_exc:
                logger.error("Checkpoint save failed after successful render: %s", _ck_exc)
                # Don't let checkpoint failure undo a successful render
                pass
            db.update_video(video_id, timing_data=orch.collect_timing_json())
        
        # Extract duration (always ensure int to avoid format errors downstream)
        try:
            vp = video_data.get("video_path", "") if video_data_ok else ""
            if vp and Path(vp).exists():
                from moviepy import VideoFileClip
                clip = VideoFileClip(vp)
                dur = clip.duration
                duracion = int(dur() if callable(dur) else dur)
                clip.close()
            else:
                duracion = int((script.get("duracion_estimada", 0) * 60) if isinstance(script, dict) else 0)
        except Exception:
            duracion = int((script.get("duracion_estimada", 0) * 60) if isinstance(script, dict) else 0)
        
        # ── Phase 5: Metadata ────────────────────────────────
        if _phase_index("metadata") < start_idx:
            logger.info("Skipping metadata (loaded from checkpoint)")
        else:
            await _broadcast_progress(job_id, 78, "metadata",
                "Generando metadatos SEO con IA...",
                video_id=video_id, detail="Creando titulos, descripcion y tags optimizados")
            
            try:
                ok, metadata = await _run_in_executor(orch.phase_metadata, script, video_data,
                                                        timeout=PHASE_TIMEOUTS["metadata"])
                if not ok:
                    logger.warning(f"Metadata generation failed (non-fatal): {metadata}")
                    metadata = None
            except Exception as e:
                logger.warning(f"Metadata generation failed (non-fatal): {e}")
                metadata = None
            
            if metadata and isinstance(metadata, dict):
                _save_checkpoint(video_id, "metadata", {
                    "selected_title": metadata.get("selected_title", ""),
                    "description": metadata.get("description", ""),
                })
        
        # Validate metadata is a proper dict before using it
        metadata_ok = metadata and isinstance(metadata, dict)
        video_data_ok = video_data and isinstance(video_data, dict)
        
        if metadata_ok:
            n_titles = len(metadata.get('titles', []))
            n_tags = len(metadata.get('tags', []))
            await _broadcast_progress(job_id, 82, "metadata",
                f"Metadatos generados: {n_titles} titulos, {n_tags} tags",
                video_id=video_id, detail="Metadatos SEO listos para YouTube")
            
            vd_title = video_data.get("titulo", "") if video_data_ok else ""
            titulo = metadata.get("selected_title", vd_title or script.get("titulo_selected", "Sin titulo"))
            vd_video = video_data.get("video_path", "") if video_data_ok else script.get("video_path", "")
            vd_thumb = video_data.get("thumbnail_path", "") if video_data_ok else ""
            db.update_video(
                video_id,
                titulo_final=titulo,
                video_path=vd_video,
                thumbnail_path=vd_thumb,
                audio_path=audio_data.get("audio_path", "") if isinstance(audio_data, dict) else "",
                duracion_seg=duracion,
                title_options=json.dumps(metadata.get("titles", [])),
                tags_json=json.dumps(metadata.get("tags", [])),
                description=metadata.get("description", ""),
                timing_data=orch.collect_timing_json(),
            )
        else:
            vd_title = video_data.get("titulo", "") if video_data_ok else ""
            titulo = vd_title or script.get("titulo_selected", "Sin titulo")
            vd_video = video_data.get("video_path", "") if video_data_ok else ""
            vd_thumb = video_data.get("thumbnail_path", "") if video_data_ok else ""
            db.update_video(
                video_id,
                titulo_final=titulo,
                video_path=vd_video,
                thumbnail_path=vd_thumb,
                audio_path=audio_data.get("audio_path", "") if isinstance(audio_data, dict) else "",
                duracion_seg=duracion,
                title_options=script.get("titulo_options", ""),
                tags_json=script.get("keywords_json", ""),
                description="",
                timing_data=orch.collect_timing_json(),
            )
        
        duracion = int(duracion)  # ensure int for format strings below
        dur_min = duracion // 60
        dur_sec = duracion % 60
        dur_str = f"{dur_min}:{dur_sec:02d}" if dur_min > 0 else f"{duracion}s"
        await _broadcast_progress(job_id, 80, "video",
            f"Video ensamblado ({dur_str})",
            video_id=video_id, detail=f"Duracion: {dur_str} — Listo para metadatos y subida")
        
        # ── Phase 5b: Save scenes ────────────────────────────
        await _broadcast_progress(job_id, 83, "scenes", "Guardando escenas...",
                                   video_id=video_id,
                                   detail="Almacenando datos en la base de datos")
        
        try:
            escenas_raw = script.get("escenas") or script.get("escenas_json", "[]")
            if isinstance(escenas_raw, str):
                escenas = json.loads(escenas_raw)
            else:
                escenas = escenas_raw or []
            
            scenes_data = []
            for i, escena in enumerate(escenas):
                img = ""
                if media_assets and i < len(media_assets) and media_assets[i]:
                    asset = media_assets[i]
                    img = str(asset.get("path", "")) if isinstance(asset, dict) else ""
                
                scenes_data.append({
                    "description": escena if isinstance(escena, str) else escena.get("descripcion", str(escena)),
                    "script_text": "",
                    "image_path": img,
                    "duration_ms": (duracion * 1000) // max(len(escenas), 1) if duracion else 0,
                })
            
            if scenes_data:
                db.insert_scenes_batch(video_id, scenes_data)
                await _broadcast_progress(job_id, 85, "scenes",
                    f"{len(scenes_data)} escenas guardadas", video_id=video_id)
        except Exception as e:
            logger.error(f"Error saving scenes: {e}")
        
        # ── Phase 5.5: Post-validation (quality gate — never discards) ──
        if _phase_index("post_validate") < start_idx:
            logger.info("Skipping post-validation (loaded from checkpoint)")
        else:
            await _broadcast_progress(job_id, 87, "post_validate",
                "Validando calidad del video y metadatos...",
                video_id=video_id,
                detail="Verificando archivo, duracion, titulo, descripcion y tags")
            ok, val_result = await _run_in_executor(
                orch.phase_post_validate, video_data, metadata, script,
                timeout=PHASE_TIMEOUTS["post_validate"],
            )
            if not ok:
                # Phase threw RuntimeError → blocking error
                error_detail = str(val_result)
                await _broadcast_progress(job_id, 87, "post_validate",
                    f"Post-validacion bloqueada: {error_detail[:100]}",
                    "failed", video_id,
                    detail="Video preservado para diagnostico — error irrecuperable")
                db.update_video(video_id, status="validation_failed",
                                progress_phase="post_validate",
                                timing_data=orch.collect_timing_json())
                return
            
            # Validation passed (possibly with auto-fixes/warnings)
            if val_result.auto_fixes_applied:
                # Sync auto-fixed metadata back to DB
                try:
                    db.update_video(
                        video_id,
                        titulo_final=val_result.title,
                        description=val_result.description,
                        tags_json=json.dumps(val_result.tags, ensure_ascii=False),
                    )
                except Exception as _sync_exc:
                    logger.warning("Failed to sync auto-fixed metadata: %s", _sync_exc)
                # Update local metadata dict for downstream use (upload, etc.)
                if metadata and isinstance(metadata, dict):
                    metadata["selected_title"] = val_result.title
                    metadata["description"] = val_result.description
                    metadata["tags"] = val_result.tags
                logger.info(
                    "Post-validation auto-fixes: %s",
                    ", ".join(val_result.auto_fixes_applied),
                )
            if val_result.warnings:
                logger.warning(
                    "Post-validation warnings: %s",
                    "; ".join(val_result.warnings),
                )
            _save_checkpoint(video_id, "post_validate", {"passed": True})
        
        await _broadcast_progress(job_id, 88, "thumbnail", "Miniatura generada",
                                    video_id=video_id,
                                    detail="Thumbnail lista para YouTube")
        
        # ── Mark generation complete before upload phase ──
        db.update_video(video_id, generation_finished_at=db_now())
        
        # ── Phase 6: Upload (skip in test mode or when upload=False) ──
        if test_mode or not upload:
            skip_reason = "Modo pruebas: " if test_mode else ""
            await _broadcast_progress(job_id, 95, "upload",
                f"{skip_reason}Upload omitido. Video listo localmente.",
                "completed", video_id,
                detail=("Video generado en modo pruebas (sin subir a YouTube)"
                        if test_mode else
                        "Video generado exitosamente. Listo para subir manualmente."))
            db.update_video(video_id, status="ready", progress=100)
        else:
            await _broadcast_progress(job_id, 90, "upload", "Preparando subida a YouTube...",
                                       video_id=video_id,
                                       detail="Autenticando con YouTube API...")
        
            # Build safe metadata dict for upload (fallback if metadata is broken)
            upload_metadata = metadata if metadata_ok else None
        
            ok, video_yt_id = await _run_in_executor(orch.phase_upload, script, video_data, upload_metadata, job_id,
                                                        timeout=PHASE_TIMEOUTS["upload"])
            if not ok:
                await _broadcast_progress(job_id, 92, "upload",
                    f"Error en subida: {video_yt_id}", "failed", video_id)
                db.update_video(video_id, status="error", progress_phase="upload")
                return
        
            if video_yt_id:
                yt_url = f"https://youtube.com/watch?v={video_yt_id}"
                pub_mode = get_channel_config(canal).PUBLISH_MODE
                upload_status = "uploaded_private" if pub_mode == "scheduled" else "uploaded"
                db.mark_video_uploaded(video_id, video_yt_id, yt_url, status=upload_status)

                # ── Auto-mark altered content (IA) via browser automation ──
                try:
                    channel_config = get_channel_config(canal)
                    if getattr(channel_config, "AUTO_MARK_ALTERED_CONTENT", False):
                        from pipeline.youtube_browser import get_browser, get_account_for_channel
                        import threading as _threading
                        account = get_account_for_channel(canal)
                        if account:
                            _threading.Thread(
                                target=_auto_mark_altered_content,
                                args=(video_yt_id, canal, account, video_id),
                                daemon=True
                            ).start()
                except Exception as e:
                    logger.warning(f"[{canal}] Failed to trigger auto-mark IA: {e}")

                # ── Post-upload: real YouTube stats snapshot ──
                try:
                    from pipeline.youtube_stats import YouTubeStatsFetcher
                    fetcher = YouTubeStatsFetcher(canal)
                    if fetcher.authenticate():
                        real_stats = fetcher.get_video_stats(video_yt_id)
                        if real_stats and not real_stats.get("is_mock"):
                            db.insert_video_stats(
                                video_id=video_id,
                                yt_video_id=video_yt_id,
                                stats=real_stats,
                            )
                            logger.info(f"[{canal}] Real stats collected for video {video_yt_id}: {real_stats.get('viewCount', '?')} views, {real_stats.get('likeCount', '?')} likes")
                        else:
                            db.insert_video_stats(
                                video_id=video_id,
                                yt_video_id=video_yt_id,
                                stats={"viewCount": 0, "likeCount": 0, "commentCount": 0},
                            )
                            logger.info(f"[{canal}] Baseline stats saved for video {video_yt_id} (API returned mock/no data)")
                    else:
                        db.insert_video_stats(
                            video_id=video_id,
                            yt_video_id=video_yt_id,
                            stats={"viewCount": 0, "likeCount": 0, "commentCount": 0},
                        )
                        logger.warning(f"[{canal}] Auth failed for stats fetch, saved baseline for video {video_yt_id}")
                except Exception as stats_exc:
                    logger.warning(f"[{canal}] Failed to collect post-upload stats: {stats_exc}")
                    try:
                        db.insert_video_stats(
                            video_id=video_id,
                            yt_video_id=video_yt_id,
                            stats={"viewCount": 0, "likeCount": 0, "commentCount": 0},
                        )
                    except Exception:
                        pass

                # ── Post-upload: schedule lifecycle promotion actions ──
                try:
                    from pipeline.video_lifecycle import VideoLifecycleManager
                    lifecycle = VideoLifecycleManager(canal)
                    script_text_for_lifecycle = script.get("script_text") or script.get("texto_completo", "")
                    lifecycle.on_video_published(
                        db_video_id=video_id,
                        yt_video_id=video_yt_id,
                        channel_id=channel_id,
                        script_text=script_text_for_lifecycle,
                    )
                    logger.info(f"[{canal}] Lifecycle actions scheduled for video {video_yt_id}")
                except Exception as lifecycle_exc:
                    logger.warning(f"[{canal}] Lifecycle scheduling failed (non-critical): {lifecycle_exc}")
            
                video_path = video_data.get("video_path", "")
                if video_path and Path(video_path).exists():
                    try:
                        Path(video_path).unlink()
                        logger.info(f"Deleted local mp4: {video_path}")
                        db.update_video(video_id, video_path="")
                    except Exception as e:
                        logger.warning(f"Could not delete mp4 {video_path}: {e}")

                # ── Clean up residual files (v9): audio + scene assets ──
                try:
                    from pipeline.cleanup_utils import cleanup_video_residuals
                    cleanup_video_residuals(db, video_id, audio_data=audio_data, log=logger)
                except Exception as e:
                    logger.warning(f"Residual cleanup failed (non-critical): {e}")
            
                await _broadcast_progress(job_id, 98, "upload",
                    "Subida completada. Finalizando...",
                    video_id=video_id, detail="Actualizando registros...")
                await asyncio.sleep(0.5)
                await _broadcast_progress(job_id, 100, "upload",
                    f"Subido! youtube.com/watch?v={video_yt_id}",
                    "completed", video_id,
                    detail="Video publicado exitosamente en YouTube")
                db.update_video(video_id, status="uploaded", progress=100)
            else:
                await _broadcast_progress(job_id, 95, "upload",
                    "Video generado pero fallo la subida a YouTube",
                    "completed", video_id,
                    detail="El video esta listo localmente, pero no se subio a YouTube")
                db.update_video(video_id, status="ready", progress=100)
        
        # Save generation timing to the video record
        try:
            timing_json = orch.collect_timing_json()
            db.update_video(video_id, timing_data=timing_json)
            logger.info("Timing saved for video %d: total=%dms phases=%s",
                        video_id, orch.collect_timing().get("total_duration_ms", 0),
                        list(orch.collect_timing().get("phases", {}).keys()))
        except Exception as e:
            logger.warning(f"Failed to save timing for video {video_id}: {e}")
        
        # Mark script as used — non-critical, must not revert video to error on failure
        try:
            orch.db.mark_script_used(script.get("id"))
        except Exception as e:
            logger.warning("Failed to mark script as used (video was already uploaded): %s", e)
        
    except Exception as e:
        logger.exception(f"Generation job {job_id} failed: {e}")
        await _broadcast_progress(job_id, 0, "error", f"Error: {str(e)[:200]}", "failed", video_id,
                                   detail="Ocurrio un error inesperado durante la generacion")
        try:
            timing_json = orch.collect_timing_json() if 'orch' in dir() else '{}'
            db.update_video(video_id, status="error", progress_phase="error", timing_data=timing_json)
        except Exception:
            db.update_video(video_id, status="error", progress_phase="error")
    finally:
        # ── Unregister active render ──────────────────────────
        _active_renders.pop(channel_id, None)
        
        # ── Auto-retry transient failures (up to MAX_RETRY_ATTEMPTS attempts) ─────
        _auto_retry_if_transient(job_id, video_id)
        
        # Safety net: always attempt to save timing before unregistering
        try:
            if 'orch' in dir():
                db.update_video(video_id, timing_data=orch.collect_timing_json())
        except Exception:
            pass
        unregister_orchestrator(job_id)
        # Kill any orphaned ffmpeg child processes to prevent RAM leaks
        _kill_orphaned_ffmpeg()
        # ── Release all pipeline components to free RAM ──
        import gc
        try:
            if 'orch' in dir():
                orch.cleanup()
                del orch
        except Exception:
            pass
        gc.collect()
        # Force glibc to return freed pages to the OS (Python never does).
        # Without this the RSS of the uvicorn process only grows over time.
        try:
            import ctypes
            ctypes.CDLL("libc.so.6").malloc_trim(0)
        except Exception:
            pass
        logger.info("Job %d cleanup complete: orchestrator released + gc.collect() + malloc_trim", job_id)


async def start_upload_job(job_id: int, video_id: int):
    """Upload only — for re-uploading existing videos."""
    import json, time

    db = _get_db()
    v = db.get_video(video_id)
    if not v:
        await _broadcast_progress(job_id, 0, "upload", "Video no encontrado", "failed")
        return
    
    db.update_job(job_id, status="running")
    channel_id = v.get("channel_id")
    ch = db.get_channel(channel_id) if channel_id else None
    canal = ch["slug"] if ch else v.get("canal")
    if not canal:
        await _broadcast_progress(job_id, 0, "upload", "Error: Canal no identificado", "failed")
        return
    
    await _broadcast_progress(job_id, 5, "upload", "Preparando subida...",
                               video_id=video_id, detail="Cargando datos del video")
    
    try:
        await _broadcast_progress(job_id, 10, "upload", "Autenticando con YouTube...",
                                   video_id=video_id, detail="Verificando credenciales OAuth")
        
        from orchestrator import PipelineOrchestrator
        orch = PipelineOrchestrator(canal=canal, db_video_id=video_id)
        
        # ── Run auth in thread pool to avoid blocking event loop ──
        auth_ok, _ = await _run_in_executor(lambda: orch.uploader.authenticate(), timeout=60)
        if not auth_ok:
            await _broadcast_progress(job_id, 20, "upload",
                "Error: Fallo autenticacion YouTube", "failed", video_id,
                detail="No se pudo autenticar. Verifica las credenciales del canal.")
            return
        
        await _broadcast_progress(job_id, 30, "upload", "Subiendo video...",
                                   video_id=video_id,
                                   detail="Transfiriendo archivo a YouTube (puede tardar)")
        
        upload_start = time.time()
        
        tags_raw = v.get("tags_json", "[]")
        if isinstance(tags_raw, str):
            try:
                tags = json.loads(tags_raw)
            except json.JSONDecodeError:
                tags = []
        else:
            tags = tags_raw or []
        
        # ── Progress callback: maps YouTube 0-100% → our 30-90% ──
        # Called from the upload thread, so only does sync DB updates.
        # The background monitor task (below) handles WebSocket broadcasts.
        def _upload_progress_cb(yt_pct: int):
            """Sync progress callback — safe to call from thread."""
            try:
                our_pct = 30 + int(yt_pct * 0.6)  # map 0-100 → 30-90
                db2 = _get_db()
                db2.update_job(job_id, progress=our_pct, phase="upload")
                db2.update_video(video_id, progress=our_pct, progress_phase="upload")
            except Exception:
                pass

        # ── Background monitor: broadcasts DB progress via WebSocket ──
        monitor_stop = asyncio.Event()

        async def _upload_monitor():
            """Poll DB for upload progress and broadcast to WebSocket."""
            last_pct = 30
            try:
                while not monitor_stop.is_set():
                    await asyncio.sleep(3.0)
                    try:
                        db2 = _get_db()
                        job2 = db2.get_job(job_id)
                        if job2 and job2.get("status") not in ("running",):
                            break  # job finished or failed
                        video2 = db2.get_video(video_id) if video_id else None
                        cur_pct = video2.get("progress", job2.get("progress", 30)) if video2 else (job2.get("progress", 30) or 30)
                        cur_pct = int(cur_pct) if cur_pct else 30
                        if cur_pct != last_pct:
                            last_pct = cur_pct
                            await _broadcast_progress(
                                job_id, cur_pct, "upload",
                                f"Subiendo video... {cur_pct}%",
                                video_id=video_id,
                                detail="Transfiriendo archivo a YouTube"
                            )
                    except Exception:
                        pass
            except asyncio.CancelledError:
                pass

        monitor_task = asyncio.create_task(_upload_monitor())

        # ── Scheduled publishing target (publishAt support) ──
        # Fix: reassembly must respect publish_mode. The previous direct
        # uploader.upload() bypassed scheduled publishing → the video was
        # uploaded as 'unlisted' with no publishAt and never went public
        # (caused the permanent publish_not_detected alert #1575).
        # orch.phase_upload() recalculates a stale target_public_at and forces
        # private + publishAt for scheduled channels, mirroring the full worker.
        target_public_at = v.get("target_public_at")

        def _do_upload():
            video_data = {
                "video_path": v.get("video_path", ""),
                "titulo": v.get("titulo_final", "Video sin titulo"),
                "thumbnail_path": v.get("thumbnail_path", ""),
            }
            metadata = {
                "selected_title": v.get("titulo_final", "Video sin titulo"),
                "description": v.get("description", ""),
                "tags": tags,
            }
            return orch.phase_upload(
                script=None,
                video_data=video_data,
                metadata=metadata,
                job_id=job_id,
                planned_public_at=target_public_at,
                skip_lifecycle_scheduling=True,
            )
        
        ok, result = await _run_in_executor(_do_upload, timeout=PHASE_TIMEOUTS["upload"])
        monitor_stop.set()  # signal monitor to stop
        try:
            await asyncio.wait_for(monitor_task, timeout=3.0)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            monitor_task.cancel()
        
        if not ok:
            await _broadcast_progress(job_id, 30, "upload", f"Error: {result}", "failed", video_id,
                                       detail="La subida fallo. Revisa los logs.")
            return
        
        video_yt_id = result  # phase_upload returns the YouTube video id (str) or None
        if video_yt_id:
            url = f"https://youtube.com/watch?v={video_yt_id}"
            pub_mode = get_channel_config(canal).PUBLISH_MODE
            upload_status = "uploaded_private" if pub_mode == "scheduled" else "uploaded"
            db.mark_video_uploaded(video_id, video_yt_id, url, status=upload_status)
            db.update_video(video_id, progress=100)
            # ── Save upload timing ───────────────────────────
            try:
                _upload_ms = int((time.time() - upload_start) * 1000)
                db.update_video(video_id, timing_data=json.dumps({
                    "phases": {"upload": _upload_ms},
                    "total_duration_ms": _upload_ms,
                }, ensure_ascii=False))
            except Exception:
                pass
            await _broadcast_progress(job_id, 100, "upload", f"Subido: {url}",
                                       "completed", video_id,
                                       detail="Video publicado en YouTube")
        else:
            await _broadcast_progress(job_id, 50, "upload",
                "Error: Fallo la subida", "failed", video_id,
                detail="YouTube no devolvio el ID del video")
            
    except Exception as e:
        logger.exception(f"Upload job {job_id} failed: {e}")
        await _broadcast_progress(job_id, 0, "upload", f"Error: {str(e)[:200]}",
                                   "failed", video_id)


def _is_quota_exhausted(db) -> bool:
    """Check if YouTube API quota is exhausted."""
    try:
        exhausted_at = db.get_system_state("quota_exhausted_at")
        return bool(exhausted_at)
    except Exception:
        return False


async def start_upload_job_from_scheduler(job_id: int, video_id: int, channel_id: int,
                                           source_mode: str = "original"):
    """Upload (F2) a video generated in Phase 1, using scheduled private mode.
    
    Called by upload_scheduler.py when an awaiting_upload video is within
    its channel's upload window.
    
    This is like start_upload_job but:
    - Uses the channel's publish_mode (scheduled = private + lifecycle)
    - Sets target_public_at from the video record
    - Schedules lifecycle actions (go_public, playlist, comments)
    - Cleans up the local mp4 after successful upload
    """
    import asyncio, json, time

    db = _get_db()
    v = db.get_video(video_id)
    if not v:
        db.update_job(job_id, status="failed", error_msg="Video not found")
        return

    # ── v24 (Aug 2026): Guard against duplicate upload ──
    # If the video already has a yt_video_id, the upload already succeeded.
    # This catches race conditions where the scheduler dispatches the same
    # video twice, or a post-timeout recovery before the DB was updated.
    existing_yt_id = v.get("yt_video_id")
    if existing_yt_id and str(existing_yt_id).strip():
        logger.warning(
            "[%s] Video #%d already uploaded to YouTube (yt_video_id=%s) — aborting F2 upload",
            v.get("canal", "?"), video_id, existing_yt_id,
        )
        db.update_job(job_id, status="completed", progress=100,
                      error_msg=f"Already uploaded: {existing_yt_id}")
        # Ensure video status reflects reality
        current_status = v.get("status", "")
        if current_status in ("awaiting_upload", "uploading", "ready"):
            pub_mode = v.get("publish_mode", "immediate")
            corrected_status = "uploaded_private" if pub_mode == "scheduled" else "uploaded"
            db.update_video(video_id, status=corrected_status, progress=100,
                            progress_phase="upload")
        await _broadcast_progress(job_id, 100, "upload",
                                   f"Already uploaded: {existing_yt_id}",
                                   "completed", video_id)
        return

    db.update_job(job_id, status="running")
    ch = db.get_channel(channel_id)
    canal = ch["slug"] if ch else v.get("canal")
    if not canal:
        db.update_job(job_id, status="failed", error_msg="Channel not resolved")

    await _broadcast_progress(job_id, 5, "upload", "F2: Preparando subida programada...",
                               video_id=video_id)

    try:
        await _broadcast_progress(job_id, 10, "upload", "Autenticando con YouTube...",
                                   video_id=video_id)

        from orchestrator import PipelineOrchestrator
        orch = PipelineOrchestrator(canal=canal, db_video_id=video_id)

        # ── Run auth in thread pool to avoid blocking event loop ──
        auth_ok, _ = await _run_in_executor(lambda: orch.uploader.authenticate(), timeout=60)
        if not auth_ok:
            db.update_job(job_id, status="failed", error_msg="YouTube auth failed")
            db.update_video(video_id, status="error", progress_phase="upload")
            return

        # ── Build metadata ──
        tags_raw = v.get("tags_json", "[]")
        if isinstance(tags_raw, str):
            try:
                tags = json.loads(tags_raw)
            except json.JSONDecodeError:
                tags = []
        else:
            tags = tags_raw or []

        # ── Scheduled publishing: use target_public_at from the video record ──
        target_public_at = v.get("target_public_at")
        if target_public_at:
            logger.info("[%s] Upload scheduler: target_public_at=%s", canal, target_public_at)
        else:
            logger.warning("[%s] Upload scheduler: no target_public_at — will upload as private anyway", canal)

        await _broadcast_progress(job_id, 20, "upload", "Subiendo video a YouTube...",
                                   video_id=video_id)

        upload_start = time.time()

        # ── Progress callback ──
        def _upload_progress_cb(yt_pct: int):
            try:
                our_pct = 20 + int(yt_pct * 0.6)
                db2 = _get_db()
                db2.update_job(job_id, progress=our_pct, phase="upload")
                db2.update_video(video_id, progress=our_pct, progress_phase="upload")
                # Broadcast via WebSocket for real-time feedback in the progress bar
                try:
                    loop = asyncio.get_running_loop()
                    loop.create_task(_broadcast_progress(
                        job_id, our_pct, "upload", f"Subiendo... {yt_pct}%", video_id=video_id
                    ))
                except RuntimeError:
                    pass  # no running loop — polling fallback handles it
            except Exception:
                pass

        # ── Use orch.uploader with private mode ──
        # The orchestrator's phase_upload handles scheduled vs immediate
        # Build a minimal video_data dict for the uploader
        video_data = {
            "video_path": v.get("video_path", ""),
            "titulo": v.get("titulo_final", "Video sin titulo"),
            "thumbnail_path": v.get("thumbnail_path", ""),
        }
        metadata = {
            "selected_title": v.get("titulo_final", ""),
            "description": v.get("description", ""),
            "tags": tags,
        }

        # Import lifecycle manager for post-upload scheduling
        from pipeline.video_lifecycle import VideoLifecycleManager

        # ── Upload as private (scheduled mode) via thread pool ──
        # orch.phase_upload is a blocking synchronous call that reads the MP4
        # and uploads chunk-by-chunk over TLS. Running it in _run_in_executor
        # (ThreadPoolExecutor) keeps the asyncio event loop free to process HTTP
        # requests and WebSocket messages during the upload (30-90 min).
        exec_ok, yt_video_id = await _run_in_executor(
            lambda: orch.phase_upload(
                script=None,
                video_data=video_data,
                metadata=metadata,
                job_id=job_id,
                planned_public_at=target_public_at,
                skip_lifecycle_scheduling=True,
            ),
            timeout=PHASE_TIMEOUTS["upload"],
        )
        if not exec_ok:
            # _run_in_executor already logged the error; yt_video_id is the error message
            # ── v24 (Aug 2026): Timeout recovery — check if upload completed despite timeout ──
            # The YouTube API may have accepted the upload but _run_in_executor timed out
            # waiting for the response. Re-read video record to detect successful upload.
            v_check = db.get_video(video_id)
            if v_check and v_check.get("yt_video_id"):
                logger.info(
                    "[%s] F2 upload detected as completed despite timeout "
                    "(yt_video_id=%s) for video %d",
                    canal, v_check["yt_video_id"], video_id,
                )
                yt_video_id = v_check["yt_video_id"]
            else:
                logger.error("[%s] F2 upload failed for video %d: %s", canal, video_id, yt_video_id)
                yt_video_id = None

        if yt_video_id:
            yt_url = f"https://youtube.com/watch?v={yt_video_id}"
            db.mark_video_uploaded(video_id, yt_video_id, yt_url)
            # v26: Check PUBLISH_MODE — immediate channels upload as public,
            # so status should be "uploaded" not "uploaded_private".
            pub_mode = get_channel_config(canal).PUBLISH_MODE
            upload_status = "uploaded_private" if pub_mode == "scheduled" else "uploaded"
            db.update_video(video_id, progress=100, status=upload_status)

            # ── Auto-mark altered content (IA) via browser ──
            try:
                from config.config_bridge import get_channel_config as _get_cc
                channel_config = _get_cc(canal)
                if getattr(channel_config, "AUTO_MARK_ALTERED_CONTENT", False):
                    from pipeline.youtube_browser import get_account_for_channel
                    import threading
                    account = get_account_for_channel(canal)
                    if account:
                        threading.Thread(
                            target=_auto_mark_altered_content,
                            args=(yt_video_id, canal, account, video_id),
                            daemon=True
                        ).start()
            except Exception as e:
                logger.warning("[%s] Failed to trigger auto-mark IA: %s", canal, e)

            # ── Clean up local mp4 ──
            vp = video_data.get("video_path", "")
            if vp and Path(vp).exists():
                try:
                    Path(vp).unlink()
                    db.update_video(video_id, video_path="")
                    logger.info("[%s] Deleted local mp4 after scheduled upload: %s", canal, vp)
                except Exception:
                    pass

            # ── Clean up residual files (v9): audio + scene assets ──
            try:
                from pipeline.cleanup_utils import cleanup_video_residuals
                cleanup_video_residuals(db, video_id, audio_data=None, log=logger)
            except Exception:
                pass

            # ── Schedule lifecycle actions (F3: go_public + playlists + comments) ──
            try:
                # Re-read video record to get potentially updated target_public_at
                # (orchestrator may have recalculated it if stale)
                v_refreshed = db.get_video(video_id)
                effective_target = (v_refreshed.get("target_public_at") if v_refreshed
                                    else target_public_at)
                if effective_target != target_public_at:
                    logger.info(
                        "[%s] target_public_at was recalculated: %s → %s",
                        canal, str(target_public_at)[:19] if target_public_at else "None",
                        str(effective_target)[:19] if effective_target else "None",
                    )

                script_text = None
                lifecycle = VideoLifecycleManager(canal)
                lifecycle.on_video_uploaded_scheduled(
                    db_video_id=video_id,
                    yt_video_id=yt_video_id,
                    channel_id=channel_id,
                    script_text=script_text,
                    target_public_at=effective_target,
                )
                logger.info("[%s] Lifecycle actions scheduled for video %s", canal, yt_video_id)
            except Exception as lc_exc:
                logger.warning("[%s] Failed to schedule lifecycle for %s: %s", canal, yt_video_id, lc_exc)

            # ── Post-upload stats ──
            try:
                from pipeline.youtube_stats import YouTubeStatsFetcher
                fetcher = YouTubeStatsFetcher(canal)
                if fetcher.authenticate():
                    real_stats = fetcher.get_video_stats(yt_video_id)
                    if real_stats and not real_stats.get("is_mock"):
                        db.insert_video_stats(video_id=video_id, yt_video_id=yt_video_id, stats=real_stats)
            except Exception:
                pass

            await _broadcast_progress(job_id, 100, "upload", f"Subido (private): {yt_url}",
                                       "completed", video_id)
            logger.info("[%s] F2 upload complete: %s (pub scheduled for %s)",
                         canal, yt_url, target_public_at)
        else:
            logger.error("[%s] Upload failed — video stays local", canal)
            # ── Quota exhaustion check: don't count as retry ──
            if _is_quota_exhausted(db):
                db.update_video(video_id, status="awaiting_upload",
                                  progress_phase="upload", scheduled_upload_at=None,
                                  error_message="YouTube API quota exhausted")
                db.update_job(job_id, status="failed",
                                error_msg="YouTube API quota exhausted")
                logger.info("[%s] Video %d: quota exhausted — kept in awaiting_upload", canal, video_id)
                return
            
            # ── v24: check retry count before reverting to awaiting_upload ──
            from api.services.upload_scheduler import MAX_UPLOAD_RETRY_PER_VIDEO
            retry_count = 0
            try:
                with db._connect() as _conn:
                    retry_count = _conn.execute(
                        "SELECT COUNT(*) FROM generation_jobs "
                        "WHERE video_id = ? AND action = 'upload_only' AND status = 'failed'",
                        (video_id,),
                    ).fetchone()[0]
            except Exception:
                pass
            if retry_count >= MAX_UPLOAD_RETRY_PER_VIDEO:
                logger.error(
                    "[%s] Video %d: max upload retries exceeded (%d/%d) — marking as error",
                    canal, video_id, retry_count, MAX_UPLOAD_RETRY_PER_VIDEO,
                )
                db.update_video(video_id, status="error", progress_phase="upload",
                                 progress=0, error_message="Max upload retries exceeded")
                db.update_job(job_id, status="failed",
                               error_msg="Max upload retries exceeded")
            else:
                # Fase 0: backoff mínimo 10 min (antes el primer fallo ponía 0s
                # → re-subida inmediata cada 5 min). Cap 12h.
                backoff_sec = min(600 * (2 ** retry_count), 43200)
                sched_at = (datetime.now(timezone.utc) + timedelta(seconds=backoff_sec)
                            ).strftime('%Y-%m-%d %H:%M:%S')
                db.update_video(video_id, status="awaiting_upload",
                                 progress_phase="upload",
                                 scheduled_upload_at=sched_at,
                                 error_message="YouTube upload returned no video ID")
                db.update_job(job_id, status="failed",
                               error_msg="YouTube upload returned no video ID")
                if backoff_sec > 0:
                    logger.info("[%s] Video %d: retry #%d — backoff %ds",
                                canal, video_id, retry_count + 1, backoff_sec)

    except Exception as e:
        logger.exception("[%s] Upload scheduler job %d failed: %s", canal, job_id, e)
        # ── Quota exhaustion check: don't count as retry ──
        if _is_quota_exhausted(db):
            db.update_video(video_id, status="awaiting_upload",
                              progress_phase="upload", scheduled_upload_at=None,
                              error_message=f"YouTube API quota exhausted")
            db.update_job(job_id, status="failed",
                            error_msg=f"YouTube API quota exhausted")
            logger.info("[%s] Video %d: quota exhausted — kept in awaiting_upload", canal, video_id)
            return
        
        # ── v24: check retry count on exception too ──
        from api.services.upload_scheduler import MAX_UPLOAD_RETRY_PER_VIDEO
        retry_count = 0
        try:
            with db._connect() as _conn:
                retry_count = _conn.execute(
                    "SELECT COUNT(*) FROM generation_jobs "
                    "WHERE video_id = ? AND action = 'upload_only' AND status = 'failed'",
                    (video_id,),
                ).fetchone()[0]
        except Exception:
            pass
        if retry_count >= MAX_UPLOAD_RETRY_PER_VIDEO:
            db.update_video(video_id, status="error", progress_phase="upload",
                             progress=0, error_message=f"Max upload retries exceeded: {e}"[:500])
            db.update_job(job_id, status="failed",
                           error_msg=f"Max upload retries exceeded: {e}"[:500])
        else:
            # Fase 0: backoff mínimo 10 min, cap 12h (ver rama no-exception)
            backoff_sec = min(600 * (2 ** retry_count), 43200)
            sched_at = (datetime.now(timezone.utc) + timedelta(seconds=backoff_sec)
                        ).strftime('%Y-%m-%d %H:%M:%S')
            db.update_job(job_id, status="failed", error_msg=str(e)[:500])
            db.update_video(video_id, status="awaiting_upload",
                             progress_phase="upload",
                             scheduled_upload_at=sched_at,
                             error_message=str(e)[:500])


async def regenerate_scene_audio_task(scene_id: int, canal: str):
    """Regenerate TTS for a single scene."""
    db = _get_db()
    import sqlite3
    with db._connect() as conn:
        row = conn.execute("SELECT * FROM video_scenes WHERE id = ?", (scene_id,)).fetchone()
    if not row:
        return
    
    scene = dict(row)
    try:
        from pipeline.tts_engine import TTSEngine
        voice_config = {
            "voice": "es-ES-AlvaroNeural",
            "rate": "-8%",
            "pitch": "-20Hz",
            "volume": "+0%",
        }
        tts = TTSEngine(voice_config)
        audio_path, timestamps = tts.generate(scene["script_text"])
        db.update_scene(scene_id, audio_path=audio_path)
    except Exception as e:
        logger.error(f"Scene audio regeneration failed: {e}")


async def replace_scene_image_task(scene_id: int, description: str, canal: str = None):
    """Replace image for a single scene."""
    db = _get_db()
    try:
        from pipeline.image_fetcher import ImageFetcher
        from pipeline.image_processor import ImageProcessor
        from config.config_bridge import get_channel_config
        
        if canal is None:
            from config import settings
            canal = settings.ACTIVE_CHANNELS[0]
        cfg = get_channel_config(canal)
        
        fetcher = ImageFetcher()
        processor = ImageProcessor(cfg)
        
        image_paths = fetcher.fetch_for_script([description])
        if image_paths and image_paths[0]:
            img = image_paths[0][0]
            processed = processor.process(img)
            db.update_scene(scene_id, image_path=str(processed))
    except Exception as e:
        logger.error(f"Scene image replacement failed: {e}")


# ── Reassembly job ────────────────────────────────────────────

async def _run_reassembly_job(job_id: int, video_id: int):
    """Re-assemble a video from existing checkpoint data (script + audio + media assets).

    Uses ``VideoEditor.build_video()`` directly instead of the full orchestrator
    pipeline, because scrape/script/tts/media phases already completed.
    Broadcasts progress via WebSocket so the frontend global progress bar
    stays live.
    """
    db_guard = _get_db()

    # ── Mark job as running FIRST so the guard counts it ──────────
    # Idempotent: the API endpoints (videos.py) already set 'running' before
    # calling; the queue consumer passes a 'queued' job. Marking here first
    # normalizes both paths.
    db_guard.update_job(job_id, status="running")

    # ── Global concurrency guard: strictly ONE generation at a time ──
    # Prevents reassembly + normal generation from running simultaneously,
    # which causes ffmpeg resource contention and OOM crashes.
    # Uses count_running_longform_jobs() (RUNNING only, self included):
    #   count=1 → only self → ok
    #   count>1 → another job is actually running → blocked
    # The old count_active_longform_jobs() ALSO counted 'queued' jobs, so a
    # batch of freshly-created queued recovery jobs blocked each other
    # (self-blocking: "Global concurrency guard: 8 active job(s)"). Only
    # RUNNING jobs consume ffmpeg/RAM, so queued jobs must not count.
    active_count = db_guard.count_running_longform_jobs()
    if active_count > 1:
        logger.warning(
            "Reassembly job %d blocked: %d active job(s) running globally",
            job_id, active_count,
        )
        await _broadcast_progress(
            job_id, 0, "blocked",
            f"Ya hay {active_count} generacion(es) en curso. Reassembly bloqueado.",
            "failed", video_id,
            detail="Solo se permite una generacion simultanea para evitar conflictos de recursos",
        )
        # Requeue (NOT fail): the queue consumer retries once the running
        # generation finishes. Reset the video to 'error' so the dashboard
        # doesn't show it as active while nothing processes it — the startup
        # recovery or a manual reassemble can pick it up again.
        db_guard.update_job(job_id, status="queued",
                            error_msg=f"Deferred by concurrency guard: {active_count} job(s) running")
        db_guard.update_video(video_id, status="error")
        return

    # ── Pre-flight: kill lingering ffmpegs from prior failed attempts ──
    _kill_orphaned_ffmpeg()
    
    try:
        await _do_reassembly(job_id, video_id)
    finally:
        _kill_orphaned_ffmpeg()


async def _do_reassembly(job_id: int, video_id: int):
    import json, time
    from pathlib import Path
    from config.config_bridge import get_channel_config

    db = _get_db()
    video = db.get_video(video_id)
    if not video:
        await _broadcast_progress(job_id, 0, "error", "Video no encontrado", "failed")
        return

    # ── Track reassembly duration ───────────────────────────
    reassembly_start = time.time()

    # ── Phase 1: Load data ──────────────────────────────────
    await _broadcast_progress(job_id, 5, "load", "Cargando datos del checkpoint...",
                               video_id=video_id, detail="Leyendo script, audio y assets")

    try:
        # Script
        script_id = None
        cp = json.loads(video.get("checkpoint_data", "{}"))
        script_cp = cp.get("script", {})
        if isinstance(script_cp, dict):
            script_id = script_cp.get("id")
        if not script_id:
            script_id = video.get("script_id")
        if not script_id:
            await _broadcast_progress(job_id, 0, "error",
                                       "No se encontro script en el checkpoint", "failed")
            return

        from database.db_extended import ExtendedDatabase
        edb = ExtendedDatabase()
        conn = edb._connect()
        script_row = conn.execute(
            "SELECT * FROM scripts WHERE id = ?", (script_id,)
        ).fetchone()
        if not script_row:
            await _broadcast_progress(job_id, 0, "error",
                                       f"Script #{script_id} no encontrado en BD", "failed")
            return

        script = dict(script_row)
        bloques = json.loads(script["bloques_json"])
        titulo_options = json.loads(script["titulo_options"])
        titulo = titulo_options[0] if titulo_options else "Sin titulo"

        # Audio
        tts_cp = cp.get("tts", {})
        audio_path = tts_cp.get("audio_path")
        if not audio_path or not Path(audio_path).exists():
            await _broadcast_progress(job_id, 0, "error",
                                       f"Audio no encontrado: {audio_path}", "failed")
            return

        # Timestamps (used for detail message and audio duration estimate)
        ts_path = Path(str(audio_path).replace(".mp3", "_timestamps.json"))
        timestamps = []
        try:
            if ts_path.exists():
                timestamps = json.loads(ts_path.read_text())
        except Exception as _ts_exc:
            logger.warning("Failed to load timestamps from %s: %s", ts_path, _ts_exc)

        # Media
        media_cp = cp.get("media", {})
        assets = media_cp.get("assets", [])
        scene_ranges = media_cp.get("scene_ranges")
        if not assets:
            await _broadcast_progress(job_id, 0, "error",
                                       "No hay assets en el checkpoint", "failed")
            return

        # ── Drop missing media files ─────────────────────────────
        # The preflight cleanup (full_pipeline_worker._preflight_cleanup /
        # orchestrator.run_full_pipeline) may have deleted a video's clips
        # while it was in 'error' state. Rendering a missing path produces a
        # placeholder segment, and >30% placeholders aborts the whole video
        # (video_editor placeholder gate). Re-download is not possible here
        # (the checkpoint stores path + source, no URL), so we drop missing
        # assets and let the editor's fallback-image pool cover those scenes.
        _assets_before = len(assets)
        assets = [a for a in assets
                  if a.get("path") and Path(a["path"]).exists()]
        _assets_missing = _assets_before - len(assets)
        if _assets_missing:
            logger.warning(
                "Reassembly: %d/%d assets missing on disk — dropping them "
                "(editor will substitute fallback images)",
                _assets_missing, _assets_before,
            )
        if not assets:
            await _broadcast_progress(job_id, 0, "error",
                                       "No hay assets válidos en disco", "failed")
            return

        # Channel config / slug — resolve from video's channel_id
        channel_id = video.get("channel_id")
        ch = db.get_channel(channel_id) if channel_id else None
        canal = ch["slug"] if ch else video.get("canal")
    except Exception as e:
        logger.exception("Reassembly data load failed")
        await _broadcast_progress(job_id, 0, "error", f"Error cargando datos: {e}", "failed")
        return

    await _broadcast_progress(job_id, 10, "video",
                               f"Re-ensamblando: {len(bloques)} bloques, {len(assets)} assets",
                               video_id=video_id,
                               detail=f'Audio: {len(timestamps)} timestamps, {Path(audio_path).stat().st_size / 1e6:.1f} MB')

    # ── Phase 2: Build video (subprocess, not thread) ─────────
    await _broadcast_progress(job_id, 20, "video", "Construyendo escenas...",
                                video_id=video_id,
                                detail=f"Procesando {len(scene_ranges) if scene_ranges else len(bloques)} escenas")

    try:
        # Build structures compatible with _run_video_in_subprocess
        _script = {
            "bloques": bloques,  # already-parsed list
            "guion": script.get("guion", ""),
            "titulo_options": json.dumps(titulo_options, ensure_ascii=False),
            "keywords_json": script.get("keywords_json", json.dumps([])),
        }
        _audio = {
            "audio_path": audio_path,
            "timestamps_path": "",  # worker auto-detects from audio_path
            "cta_audio_path": tts_cp.get("cta_audio_path", ""),
            "duration": int(timestamps[-1].get("end", 0)) if timestamps else 0,
        }
        _media = {
            "assets": assets,
            "scene_ranges": scene_ranges,
        }

        async with _RENDER_SEMAPHORE:
            ok, video_data = await _run_video_in_subprocess(
                canal=canal, video_id=video_id, job_id=job_id,
                script=_script, audio_data=_audio,
                media_assets=_media, test_mode=False,
                orch=None,  # reassembly — no thumbnail
            )
        if not ok or not video_data or not isinstance(video_data, dict):
            raise RuntimeError(video_data if isinstance(video_data, str) else "Render subprocess failed")

        output_path = video_data.get("video_path", "")
        if not output_path or not Path(output_path).exists():
            raise RuntimeError(f"No output file produced: {output_path}")

        await _broadcast_progress(job_id, 95, "video",
                                    "Video renderizado correctamente",
                                    video_id=video_id,
                                    detail=f"Archivo: {Path(output_path).name}")

    except Exception as e:
        logger.exception("Video build failed")
        await _broadcast_progress(job_id, 0, "error",
                                    f"Error ensamblando video: {e}", "failed",
                                    video_id=video_id)
        db.update_video(video_id, status="error", progress_phase="video")
        # ── Save timing even on failure ─────────────────────
        try:
            _failed_duration_ms = int((time.time() - reassembly_start) * 1000)
            db.update_video(video_id, timing_data=json.dumps({
                "phases": {"reassembly_failed": _failed_duration_ms},
                "total_duration_ms": _failed_duration_ms,
            }, ensure_ascii=False))
        except Exception:
            pass
        return

    # ── Phase 3: Thumbnail + metadata + upload ───────────────
    # Bug #2 fix: after rebuilding the video, generate a thumbnail,
    # populate metadata from the script, and upload to YouTube.
    import subprocess as _sp
    from pathlib import Path as _P

    # 3a. Generate thumbnail (ffmpeg frame grab — fast and reliable)
    thumb_path = None
    try:
        _out_dir = _P(f"output/thumbnails/{canal}")
        _out_dir.mkdir(parents=True, exist_ok=True)
        _thumb_file = _out_dir / f"recover_{video_id}.jpg"
        _sp.run(
            ["ffmpeg", "-y", "-i", str(output_path),
             "-ss", "00:00:15", "-vframes", "1",
             "-q:v", "2", str(_thumb_file)],
            capture_output=True, timeout=30,
        )
        if _thumb_file.exists():
            thumb_path = str(_thumb_file)
            logger.info("Reassembly thumbnail generated: %s", thumb_path)
    except Exception as _te:
        logger.warning("Reassembly thumbnail generation failed (non-fatal): %s", _te)

    # 3b. Populate metadata from script
    _keywords_raw = script.get("keywords_json", "[]")
    if isinstance(_keywords_raw, str):
        try:
            _keywords = json.loads(_keywords_raw)
        except json.JSONDecodeError:
            _keywords = []
    else:
        _keywords = _keywords_raw or []

    # Build a description from the script
    _guion_preview = (script.get("guion") or "")[:500]

    db.update_video(video_id,
        video_path=str(output_path),
        thumbnail_path=thumb_path or "",
        titulo_final=titulo,
        description=_guion_preview,
        tags_json=json.dumps(_keywords, ensure_ascii=False),
        status="ready",
        progress=90,
        progress_phase="upload",
    )

    # 3c. Upload to YouTube
    await _broadcast_progress(job_id, 90, "upload",
                               "Video re-ensamblado — subiendo a YouTube...",
                               video_id=video_id,
                               detail=f"Titulo: {titulo[:60]}")

    try:
        await start_upload_job(job_id, video_id)
        # Mark reassembly job as completed after upload succeeds
        db.update_job(job_id, status="completed", progress=100, phase="done")
        # ── Save reassembly timing ──────────────────────────
        try:
            _reassembly_duration_ms = int((time.time() - reassembly_start) * 1000)
            db.update_video(video_id, timing_data=json.dumps({
                "phases": {"reassembly": _reassembly_duration_ms},
                "total_duration_ms": _reassembly_duration_ms,
            }, ensure_ascii=False))
        except Exception:
            pass
        logger.info("Reassembly + upload completed for video %d (job %d) in %d ms", video_id, job_id, _reassembly_duration_ms)
    except Exception as _ue:
        logger.exception("Reassembly upload failed for video %d: %s", video_id, _ue)
        db.update_video(video_id, status="ready", progress=100, progress_phase="upload_retry")
        db.update_job(job_id, status="completed", progress=100, phase="upload_failed")
        # ── Save timing even on upload failure ──────────────
        try:
            _upload_fail_ms = int((time.time() - reassembly_start) * 1000)
            db.update_video(video_id, timing_data=json.dumps({
                "phases": {"reassembly": _upload_fail_ms},
                "total_duration_ms": _upload_fail_ms,
            }, ensure_ascii=False))
        except Exception:
            pass
        await _broadcast_progress(job_id, 100, "done",
                                   f"Video listo pero subida fallo: {titulo[:60]}", "completed",
                                   video_id=video_id,
                                   detail="Upload fallo — reintentar manualmente desde el panel")

    await _broadcast_progress(job_id, 100, "done",
                               f"Video listo: {titulo[:60]}", "completed",
                               video_id=video_id,
                               detail=str(output_path))


# ── Auto-recovery on startup ─────────────────────────────────

def _is_bug_crash(video_id: int, video_row: dict = None) -> bool:
    """Determine if a failed video was killed by a code bug (True) or interruption.

    Criteria (any match → bug):
    1. A ``*.crash.log`` file exists for the video's output and contains a traceback.
    2. The ``pipeline_log`` contains a known crash signature (e.g. MoviePy error).
    3. The ``progress_phase`` is ``'error'`` (generic catch-all exception handler).

    Non-bug criteria (these are interruptions/restarts):
    - ``progress_phase == 'orphaned'`` (orphan detector marked it).
    - ``progress_phase`` is ``None`` or empty.
    - No crash log and no crash signature in pipeline_log.
    """
    import json
    from pathlib import Path
    from config.settings import VIDEOS_DIR

    # ── Quick exit: orphaned = interruption, not bug ──────────
    if video_row is None:
        db = _get_db()
        video_row = db.get_video(video_id)
    if not video_row:
        return True  # can't determine — assume bug to be safe

    progress_phase = (video_row.get("progress_phase") or "").strip()
    if progress_phase == "orphaned":
        return False  # definite interruption
    if progress_phase == "interrupted":
        return False  # auto-recovery set this phase — API was restarted mid-run
    if progress_phase == "error":
        # Generic catch-all — could be a bug
        return True

    # ── Check 1: crash log with traceback ─────────────────────
    cp_raw = video_row.get("checkpoint_data", "{}")
    try:
        cp = json.loads(cp_raw) if isinstance(cp_raw, str) else (cp_raw or {})
    except (json.JSONDecodeError, TypeError):
        cp = {}

    # Derive audio path from checkpoint or video row
    audio_path = (
        cp.get("tts", {}).get("audio_path")
        or video_row.get("audio_path")
    )
    if audio_path:
        crash_pattern = str(Path(VIDEOS_DIR) / f"*{Path(audio_path).stem}*.crash.log")
    else:
        crash_pattern = str(Path(VIDEOS_DIR) / "*.crash.log")

    crash_files = _glob.glob(crash_pattern)
    if not crash_files:
        # Also try output/videos for absolute paths
        crash_files = _glob.glob(f"/root/autotube/output/videos/*{video_id}*.crash.log")
        if not crash_files:
            # Try by script_id if we can get it
            script_cp = cp.get("script", {})
            script_id = script_cp.get("id") if isinstance(script_cp, dict) else None
            if not script_id:
                script_id = video_row.get("script_id")
            if script_id:
                crash_files = _glob.glob(f"/root/autotube/output/videos/*_1782*.crash.log")

    for cf_path in sorted(crash_files, reverse=True):
        try:
            content = Path(cf_path).read_text()
            if "Traceback (most recent call last)" in content:
                logger.info("Video %d: bug detected — crash log %s", video_id, cf_path)
                return True
        except Exception:
            pass

    # ── Check 2: pipeline_log crash signatures ────────────────
    try:
        db = _get_db()
        conn = db._connect()
        logs = conn.execute(
            "SELECT message FROM pipeline_log WHERE phase = 'video' "
            "AND status = 'error' ORDER BY created_at DESC LIMIT 5"
        ).fetchall()
        crash_signatures = [
            "MoviePy render crashed",
            "unsupported operand type(s)",
            "operands could not be broadcast",
            "only length-1 arrays",
        ]
        for row in logs:
            msg = (row["message"] or "").lower()
            for sig in crash_signatures:
                if sig.lower() in msg:
                    logger.info("Video %d: bug detected — pipeline_log signature: %s", video_id, sig)
                    return True
    except Exception:
        pass

    # ── Not a bug → interruption ──────────────────────────────
    return False


async def auto_recover_on_startup():
    """Scan failed videos on startup and auto-reassemble interrupted ones.

    Called from ``api/main.py`` lifespan block.  Logic:
    1. Mark stale jobs as failed (PID-aware):
         - queued jobs → failed (never started).
         - running jobs → only mark failed if worker process is DEAD.
           Workers that survived the API restart remain 'running'.
    2. Reset stuck video states (PID-aware):
         - Only reset videos to 'error' if their job is not running.
         - Videos from surviving workers stay as 'generating'.
    3. For each video in 'error' with checkpoint data:
         - If the failure was a bug → skip (leave for manual analysis).
         - If the failure was an interruption → create a reassemble job.
         - If >= 3 reassembly attempts already failed → mark as bug_crash, skip.
    """
    import json
    from api.services.upload_scheduler import MAX_UPLOAD_RETRY_PER_VIDEO
    
    MAX_RECOVERY_ATTEMPTS = int(os.getenv("MAX_RECOVERY_ATTEMPTS", "5"))

    log = logging.getLogger("autotube.startup")
    db = _get_db()
    conn = db._connect()

    # ── Step 1: Kill stale jobs ───────────────────────────────
    # queued jobs: never started → always stale → mark failed.
    # EXCEPT 'reassemble' jobs: they are pending recovery work created by this
    # same function (or manually) and are processed by the global _queue_consumer.
    # Killing them on every restart discards pending reassembly — and when the
    # restart is not idle (a generation is running → Step 3 recovery is skipped)
    # they are NOT recreated, stranding the videos in 'error' until an idle
    # restart. Reassembly jobs carry no running process, so they are safe to keep.
    queued_killed = conn.execute(
        "UPDATE generation_jobs SET status='failed', "
        "error_msg='Server restarted — old process no longer exists' "
        "WHERE status = 'queued' AND action != 'reassemble'"
    ).rowcount
    if queued_killed:
        log.info("Marked %d queued job(s) as failed (server restart)", queued_killed)
    preserved_reassemble = conn.execute(
        "SELECT COUNT(*) FROM generation_jobs "
        "WHERE status='queued' AND action='reassemble'"
    ).fetchone()[0]
    if preserved_reassemble:
        log.info("Preserved %d queued reassemble job(s) across restart", preserved_reassemble)

    # running jobs: only mark as failed if the worker process is DEAD.
    # If the subprocess survived the API restart, leave it running so
    # reconnect_active_workers() can pick it up.
    running_rows = conn.execute(
        "SELECT id, video_id FROM generation_jobs WHERE status='running'"
    ).fetchall()
    running_killed = 0
    running_alive = 0
    for rrow in running_rows:
        job_id = rrow["id"]
        worker_alive = False
        try:
            import subprocess as _sp
            result = _sp.run(
                ["pgrep", "-f", f"full_pipeline_worker.*--job-id {job_id}"],
                capture_output=True, text=True, timeout=5,
            )
            if result.stdout.strip():
                worker_alive = True
        except Exception:
            pass  # can't determine → conservatively mark failed
        if worker_alive:
            running_alive += 1
            log.info("Job #%d: worker alive (PID %s) — keeping as running",
                     job_id, result.stdout.strip().split()[0])
        else:
            conn.execute(
                "UPDATE generation_jobs SET status='failed', "
                "error_msg='Server restarted — old process no longer exists' "
                "WHERE id=?", (job_id,)
            )
            conn.execute(
                "UPDATE videos SET status='error', progress_phase='interrupted' "
                "WHERE id=? AND status='generating'",
                (rrow["video_id"],)
            )
            # Recover videos stuck in 'uploading' whose upload job died:
            # revert to 'awaiting_upload' so the upload scheduler can retry.
            # ── v24: check retry count before reverting ──
            retry_count = conn.execute(
                "SELECT COUNT(*) FROM generation_jobs "
                "WHERE video_id = ? AND action = 'upload_only' AND status = 'failed'",
                (rrow["video_id"],)
            ).fetchone()[0]
            if retry_count >= MAX_UPLOAD_RETRY_PER_VIDEO:
                log.warning(
                    "Video #%d: %d failed uploads — NOT reverting to awaiting_upload (marking error)",
                    rrow["video_id"], retry_count,
                )
                conn.execute(
                    "UPDATE videos SET status='error', progress_phase='upload' "
                    "WHERE id=? AND status='uploading'",
                    (rrow["video_id"],)
                )
            else:
                upload_recovered = conn.execute(
                    "UPDATE videos SET status='awaiting_upload', "
                    "progress_phase='upload', scheduled_upload_at=NULL "
                    "WHERE id=? AND status='uploading'",
                    (rrow["video_id"],)
                ).rowcount
                if upload_recovered:
                    log.info("Recovered video #%d: uploading → awaiting_upload (upload job died on restart)",
                             rrow["video_id"])
            running_killed += 1
    if running_killed or running_alive:
        log.info("Running jobs: %d killed (worker dead), %d kept alive (worker survived restart)",
                 running_killed, running_alive)

    total_killed = queued_killed + running_killed
    if total_killed:
        log.info("Total stale jobs killed: %d", total_killed)

    # ── Step 2: Reset stuck video states ──────────────────────
    # Only reset videos whose job is NOT currently running (i.e., worker dead).
    # Videos from surviving workers must stay as 'generating'.
    # 'queued' reassemble jobs are preserved across restart (Step 1), so their
    # 'reassembling' videos must NOT be reset — they are pending work for the
    # queue consumer, and an 'error' status would make the orphan detector
    # (running job + error video) kill them as soon as they start.
    stuck_rows = conn.execute(
        "SELECT v.id, v.canal FROM videos v "
        "JOIN generation_jobs j ON j.video_id = v.id "
        "WHERE v.status IN ('generating','reassembling') "
        "AND j.status NOT IN ('running', 'queued')"
    ).fetchall()
    stuck = 0
    skipped_alive = 0
    for srow in stuck_rows:
        vid = srow["id"]
        conn.execute(
            "UPDATE videos SET status='error', progress_phase='interrupted' "
            "WHERE id=?", (vid,)
        )
        stuck += 1

    # Also catch videos stuck in generating/reassembling with NO job at all
    orphan_videos = conn.execute(
        "SELECT v.id FROM videos v "
        "LEFT JOIN generation_jobs j ON j.video_id = v.id "
        "WHERE v.status IN ('generating','reassembling') "
        "AND j.id IS NULL"
    ).fetchall()
    for orow in orphan_videos:
        conn.execute(
            "UPDATE videos SET status='error', progress_phase='interrupted' "
            "WHERE id=?", (orow["id"],)
        )
        stuck += 1

    if stuck:
        log.info("Reset %d stuck video(s) generating/reassembling → error", stuck)

    # Count videos kept as generating (worker alive — will be reconnected)
    alive_count = conn.execute(
        "SELECT COUNT(*) FROM videos v "
        "JOIN generation_jobs j ON j.video_id = v.id "
        "WHERE v.status IN ('generating','reassembling') "
        "AND j.status='running'"
    ).fetchone()[0]
    if alive_count:
        log.info("Kept %d video(s) as generating (worker survived restart)", alive_count)

    conn.commit()

    # ── Step 2b: Clean up orphaned shorts_planned_slots ───────
    # When a generation_job is killed by server restart, the
    # shorts_planned_slots row stays as 'running' indefinitely.
    # This closes the gap: any slot whose linked job is now failed
    # gets marked as failed too.
    orphaned_slots = conn.execute(
        """UPDATE shorts_planned_slots
           SET status = 'failed',
               error_message = 'Server restart — linked job failed'
           WHERE status = 'running'
             AND job_id IS NOT NULL
             AND job_id IN (SELECT id FROM generation_jobs WHERE status = 'failed')"""
    ).rowcount
    if orphaned_slots:
        conn.commit()
        log.info("Cleaned up %d orphaned shorts slot(s) (job died on restart)", orphaned_slots)

    # ── Step 3: Auto-recover recoverable videos ───────────────
    # ── Concurrency guard: skip non-marathon recovery if a long-form
    # generation is already running. However, marathon videos that died early
    # (before checkpoint) are allowed through so they can be reset to draft
    # and re-dispatched — they don't create reassemble jobs that would block
    # the dispatcher.
    active_gen_count = db.count_active_jobs()
    _marathon_skip_recovery_for_non_marathon = False
    if active_gen_count > 0:
        # Check if there are marathon videos that need recovery
        marathon_pending = conn.execute(
            "SELECT COUNT(*) FROM videos WHERE status='error' AND is_marathon=1 "
            "AND (checkpoint_data IS NULL OR checkpoint_data = '{}')"
        ).fetchone()[0]
        if marathon_pending == 0:
            log.info(
                "Skipping auto-recovery: %d active job(s) running — "
                "no marathon videos pending reset. Will retry on next idle restart.",
                active_gen_count,
            )
            conn.close()
            return
        log.info(
            "Auto-recovery: %d active job(s) running but %d marathon video(s) need reset — "
            "proceeding with marathon-only cleanup (no reassemble jobs created).",
            active_gen_count, marathon_pending,
        )
        _marathon_skip_recovery_for_non_marathon = True  # only process marathon videos
    rows = conn.execute(
        "SELECT * FROM videos WHERE status='error' "
        "AND checkpoint_data IS NOT NULL AND checkpoint_data != '{}' "
        "AND (progress_phase IS NULL OR progress_phase NOT IN ('bug_crash','manual_review')) "
        "ORDER BY created_at DESC"
    ).fetchall()

    bugs_skipped = 0
    recovered = 0
    unrecoverable = 0
    marathon_reset = 0  # marathon videos reset to draft (no checkpoint, fresh start)
    processed_ids: set[int] = set()  # guard against re-processing the same video

    for row in rows:
        video_id = row["id"]
        
        # ── Non-marathon skip: when active jobs are running, only process marathons ──
        video_dict = dict(row)
        if _marathon_skip_recovery_for_non_marathon and video_dict.get("is_marathon") != 1:
            continue

        # ── Dedup guard: skip videos already handled in this recovery run ──
        if video_id in processed_ids:
            continue
        processed_ids.add(video_id)
        
        progress_phase = (row["progress_phase"] or "").strip()

        # Already handled in this run (interrupted → skip existing recoveries)
        if progress_phase == "interrupted":
            pass  # continue to check recoverability

        # ── Parse checkpoint ──────────────────────────────────
        cp_raw = video_dict.get("checkpoint_data", "{}")
        try:
            cp = json.loads(cp_raw) if isinstance(cp_raw, str) else (cp_raw or {})
        except (json.JSONDecodeError, TypeError):
            cp = {}

        # Must have tts + media to reassemble
        if not cp.get("tts") or not isinstance(cp.get("tts"), dict) or not cp.get("media"):
            # Marathon videos that died early (before checkpoint) can't be
            # reassembled, but shouldn't be abandoned forever. Reset to draft
            # so the marathon dispatcher can pick them up on the next cycle.
            if video_dict.get("is_marathon") == 1:
                conn.execute(
                    "UPDATE videos SET status='draft', progress_phase=NULL, progress=0 "
                    "WHERE id=?",
                    (video_id,),
                )
                marathon_reset += 1
                log.info("Video %d: marathon with no checkpoint — reset to draft", video_id)
            else:
                log.info("Video %d: no tts/media in checkpoint — not recoverable", video_id)
                unrecoverable += 1
            continue

        # Must have audio file on disk
        audio_path = cp["tts"].get("audio_path", "")
        if not audio_path or not Path(audio_path).exists():
            log.info("Video %d: audio missing (%s) — not recoverable", video_id, audio_path)
            unrecoverable += 1
            continue

        # ── Max retry check: don't loop forever on broken videos ─
        # Count ALL failed reassembly attempts (including server-restart kills).
        # Previously only counted "real" bugs, which meant server restarts
        # created fresh recovery jobs every time without ever hitting the cap.
        # Now we count total attempts: if a video has been reassembled 3+ times
        # regardless of cause, stop trying — further attempts are unlikely to help.
        failed_reassemblies = conn.execute(
            "SELECT COUNT(*) FROM generation_jobs "
            "WHERE video_id=? AND action='reassemble' AND status='failed'",
            (video_id,),
        ).fetchone()[0]
        if failed_reassemblies >= MAX_RECOVERY_ATTEMPTS:
            log.warning(
                "Video %d: %d failed reassembly attempts (max=%d total) — marking as bug_crash",
                video_id, failed_reassemblies, MAX_RECOVERY_ATTEMPTS,
            )
            conn.execute(
                "UPDATE videos SET progress_phase='bug_crash' WHERE id=?", (video_id,)
            )
            conn.commit()
            bugs_skipped += 1
            continue
        
        # ── Bug check ──────────────────────────────────────────
        if _is_bug_crash(video_id, video_dict):
            log.info("Video %d: FAILURE WAS A BUG — skipping auto-recovery (needs manual analysis)", video_id)
            bugs_skipped += 1
            continue

        # ── Recover ────────────────────────────────────────────
        channel_id = video_dict.get("channel_id")
        if not channel_id:
            log.warning("Video %d: no channel_id — skipping auto-recovery", video_id)
            skipped += 1
            continue

        # Guard: don't create duplicate reassembly jobs
        existing = conn.execute(
            "SELECT id FROM generation_jobs WHERE video_id=? "
            "AND action='reassemble' AND status IN ('queued','running')",
            (video_id,),
        ).fetchone()
        if existing:
            log.info("Video %d: reassembly job #%d already active — skipping duplicate",
                     video_id, existing["id"])
            continue

        try:
            # Use the same conn for INSERT to avoid SQLite lock contention
            # (db.create_job opens a NEW connection which conflicts with conn).
            # ── Create as 'queued' NOT 'running' ──────────────────
            # The deferred dispatcher below will promote to 'running' only if
            # the global concurrency guard allows it (count_active_jobs <= 1).
            # Creating as 'running' bypasses the guard and causes count_active_jobs()
            # to block all dispatches until the next restart.
            cursor = conn.execute(
                "INSERT INTO generation_jobs (channel_id, action, video_id, status, created_at) "
                "VALUES (?, 'reassemble', ?, 'queued', datetime('now', 'localtime'))",
                (channel_id, video_id),
            )
            # Update video to 'reassembling' in the same transaction so the
            # orphan detector (Type 3 zombie-thread check) doesn't kill this
            # freshly-created job. The orphan detector triggers on
            #   jobs.status='running' AND videos.status='error'
            # — a video in 'reassembling' status is naturally excluded.
            conn.execute(
                "UPDATE videos SET status='reassembling' WHERE id=?", (video_id,)
            )
            conn.commit()
            job_id = cursor.lastrowid
            log.info("Video %d: AUTO-RECOVERING → job %d (phase was '%s')",
                     video_id, job_id, progress_phase)
            # The job stays 'queued' — the global _queue_consumer picks it up
            # one-at-a-time (serialized + RAM gate) once startup settles and no
            # other generation is running. No manual dispatch here: launching
            # tasks immediately competes with the API's own startup writes for
            # the SQLite lock, and the old deferred-dispatch guard
            # (count_active_jobs > 1) counted the freshly-created queued jobs
            # against themselves → self-blocking ("Deferred by concurrency
            # guard") with no retry cycle. Relying on _queue_consumer is the
            # canonical path (it already dispatches action='reassemble').
            recovered += 1
        except Exception as exc:
            log.warning("Video %d: recovery dispatch failed — %s", video_id, exc)
            unrecoverable += 1

    log.info("Startup recovery complete: %d bug(s) skipped, %d dispatched, %d unrecoverable, %d marathon(s) reset to draft",
             bugs_skipped, recovered, unrecoverable, marathon_reset)
    conn.close()


# ── Subprocess-based generation (survives API restarts) ──────────

# Tracks running worker subprocesses: {job_id: subprocess.Popen}
_active_workers: dict[int, subprocess.Popen] = {}


def _get_worker_script() -> Path:
    """Return the absolute path to the full pipeline worker script."""
    return Path(__file__).parent / "full_pipeline_worker.py"


async def start_generation_job_subprocess(
    job_id: int, channel_id: int, video_id: int,
    action: str, test_mode: bool = False, upload: bool = True,
    source_mode: str = "original", viral_candidate_id: int = None,
) -> subprocess.Popen:
    """Spawn the pipeline as an independent subprocess that survives API restarts.

    Unlike ``start_generation_job`` which runs the pipeline inside the
    uvicorn process, this spawns ``full_pipeline_worker.py`` as a
    completely independent OS process with ``start_new_session=True``.

    The worker writes progress to the database.  A background monitor task
    polls the DB and broadcasts progress via WebSocket.

    Returns the ``subprocess.Popen`` object for lifecycle tracking.
    """
    db = _get_db()
    ch = db.get_channel(channel_id)
    if not ch:
        await _broadcast_progress(job_id, 0, "error", "Canal no encontrado", "failed")
        raise ValueError(f"Channel {channel_id} not found")

    canal = ch["slug"]
    channel_name = ch.get("name", canal)

    # ── Global guard: strictly ONE job at a time ──
    # Prevents ffmpeg resource contention (concurrent renders cause timeouts).
    # Phase pipelining disabled — all jobs (long-form, shorts, clips, uploads)
    # run concurrently with shorts (2-column model). The current job is
    # already counted by count_active_longform_jobs().
    # count=1 = only self = ok; count=2 = another long-form job active = blocked.
    active_count = db.count_active_longform_jobs()
    if active_count > 1:
        logger.warning(
            "Subprocess spawn blocked: %d active job(s) running globally",
            active_count,
        )
        await _broadcast_progress(
            job_id, 0, "blocked",
            f"Ya hay {active_count} generacion(es) en curso. Solo una a la vez.",
            "failed", video_id,
            detail="Solo se permite una generacion simultanea para evitar conflictos de recursos",
        )
        db.update_job(job_id, status="failed",
                      error_msg=f"Global concurrency guard: {active_count} active job(s)")
        # ── Record slot failure with backoff (v12) ──────────────
        slot_result = db.record_slot_dispatch_failure(job_id)
        if slot_result:
            logger.info(
                "Slot dispatch failure (concurrency guard): %s — backoff=%s",
                job_id, slot_result,
            )
        # ── Cleanup video record ─────────────────────────────────
        try:
            with db._connect() as _conn:
                _conn.execute(
                    "UPDATE videos SET status = 'error', progress_phase = 'blocked' WHERE id = ?",
                    (video_id,),
                )
                _conn.commit()
        except Exception:
            pass
        return None

    # ── RAM-aware guard: if another worker is active, check memory ──
    # A render can consume ~8+ GB. If free RAM is critically low
    # (< 2.5 GB), defer dispatch until the current render finishes to avoid OOM.
    if active_count >= 1:
        try:
            avail_mb = _get_available_memory_mb()
            avail_gb = avail_mb / 1024 if avail_mb else None
        except Exception:
            avail_gb = None
        if avail_gb is None:
            avail_gb = 99  # assume OK if we can't check
        if avail_gb < 2.5:
            logger.warning(
                "Subprocess spawn deferred: %d active + only %.1f GB free RAM (need ≥ 3 GB)",
                active_count, avail_gb,
            )
            db.update_job(job_id, status="failed",
                          error_msg=f"RAM too low: {active_count} active + {avail_gb:.1f} GB free")
            # ── Record slot failure with backoff (v12) ──────────
            slot_result = db.record_slot_dispatch_failure(job_id)
            if slot_result:
                logger.info(
                    "Slot dispatch failure (RAM guard): %s — backoff=%s",
                    job_id, slot_result,
                )
            try:
                with db._connect() as _conn:
                    _conn.execute(
                        "UPDATE videos SET status = 'error', progress_phase = 'blocked' WHERE id = ?",
                        (video_id,),
                    )
                    _conn.commit()
            except Exception:
                pass
            return None

    # ── Guard: don't spawn if a job is already running for THIS channel ──
    active = db.get_active_job_for_channel(channel_id)
    if active and active["id"] != job_id:
        logger.warning(
            "Subprocess spawn blocked: active job #%d already running for channel %d",
            active["id"], channel_id,
        )
        await _broadcast_progress(
            job_id, 0, "blocked",
            f"Ya hay un job activo para este canal (#{active['id']}). Espera a que termine.",
            "failed", video_id,
            detail="No se puede iniciar otro job en el mismo canal mientras hay uno en curso",
        )
        db.update_job(job_id, status="failed",
                      error_msg="Active job already running for this channel")
        # ── Record slot failure with backoff (v12) ──────────────
        slot_result = db.record_slot_dispatch_failure(job_id)
        if slot_result:
            logger.info(
                "Slot dispatch failure (channel guard): %s — backoff=%s",
                job_id, slot_result,
            )
        # ── Cleanup video record ─────────────────────────────────
        try:
            with db._connect() as _conn:
                _conn.execute(
                    "UPDATE videos SET status = 'error', progress_phase = 'blocked' WHERE id = ?",
                    (video_id,),
                )
                _conn.commit()
        except Exception:
            pass
        return None

    # ── Build command ─────────────────────────────────────────
    worker_script = _get_worker_script()
    cmd = [
        sys.executable, str(worker_script),
        "--job-id", str(job_id),
        "--channel-id", str(channel_id),
        "--video-id", str(video_id),
        "--action", action,
    ]
    if test_mode:
        cmd.append("--test-mode")
    if not upload:
        cmd.append("--no-upload")
    if source_mode != "original":
        cmd.extend(["--source-mode", source_mode])
    if viral_candidate_id is not None:
        cmd.extend(["--viral-candidate-id", str(viral_candidate_id)])

    # ── Spawn worker ──────────────────────────────────────────
    log_path = settings.LOGS_DIR / f"worker_{job_id}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)

    # ── Pre-spawn zombie cleanup: kill orphaned worker processes ──
    # Kill any full_pipeline_worker processes whose job is NOT in DB
    # as 'running'. This prevents zombie accumulation when orphan detection
    # marks jobs as failed but processes keep running.
    _kill_orphaned_workers(logger)

    try:
        proc = subprocess.Popen(
            cmd,
            start_new_session=True,     # Survive parent (API) death
            stdout=open(log_path, "w"),
            stderr=subprocess.STDOUT,
        )
    except Exception as exc:
        logger.error("Failed to spawn worker subprocess: %s", exc)
        await _broadcast_progress(
            job_id, 0, "error",
            f"Error al iniciar worker: {exc}", "failed", video_id,
        )
        db.update_job(job_id, status="failed",
                      error_msg=f"Subprocess spawn failed: {exc}")
        return None

    # Track the worker
    _active_workers[job_id] = proc
    logger.info(
        "Worker spawned: job=%d pid=%d channel=%s session=new",
        job_id, proc.pid, canal,
    )

    # ── Update job and video status ─────────────────────────────
    db.update_job(job_id, status="running",
                  started_at=db_now())
    db.update_video(video_id, generation_started_at=db_now())

    await _broadcast_progress(
        job_id, 1, "inicio",
        f"Iniciando generacion para {channel_name} (worker pid={proc.pid})...",
        video_id=video_id,
        detail="Worker independiente iniciado — los cambios en la API no afectaran la generacion",
    )

    # ── Launch progress monitor ───────────────────────────────
    asyncio.create_task(_monitor_worker_progress(
        job_id=job_id, video_id=video_id, proc=proc,
        channel_name=channel_name,
    ))

    return proc


async def _monitor_worker_progress(
    job_id: int, video_id: int, proc: subprocess.Popen,
    channel_name: str = "",
):
    """Poll the database for worker progress and broadcast via WebSocket.

    Runs as a background asyncio task. When the worker completes (process exits
    or job status changes), sends the final status via WebSocket and cleans up.
    """
    db = _get_db()

    last_progress = -1
    last_phase = ""
    poll_interval = 2.0  # seconds — fast enough for responsive UI
    ticks_since_broadcast = 0  # force periodic broadcast during long phases
    BROADCAST_EVERY_N_TICKS = 8  # every ~16s (8 * 2.0s)

    logger.info("Progress monitor started for job #%d (worker pid=%d)", job_id, proc.pid)

    try:
        while True:
            await asyncio.sleep(poll_interval)
            ticks_since_broadcast += 1

            # Check if the worker process is still alive
            poll_result = proc.poll()
            process_alive = poll_result is None

            # Read job status from DB
            try:
                job = db.get_job(job_id)
            except Exception:
                job = None

            # Read video progress from DB
            try:
                video = db.get_video(video_id)
            except Exception:
                video = None

            current_progress = video.get("progress", 0) if video else 0
            current_phase = video.get("progress_phase", "") if video else ""
            current_status = video.get("status", "") if video else ""

            # Detect progress changes and broadcast
            force_broadcast = ticks_since_broadcast >= BROADCAST_EVERY_N_TICKS
            if (current_progress != last_progress or current_phase != last_phase
                    or force_broadcast):
                last_progress = current_progress
                last_phase = current_phase
                ticks_since_broadcast = 0  # reset periodic counter

                # Build a human-readable message
                phase_messages = {
                    "scrape": "Buscando nuevo contenido...",
                    "script": "Generando guion con IA...",
                    "tts": "Generando voz con IA (TTS)...",
                    "media": "Buscando imagenes y videos...",
                    "video": "Ensamblando video (MoviePy)...",
                    "metadata": "Generando metadatos SEO...",
                    "upload": "Subiendo a YouTube...",
                    "inicio": "Iniciando pipeline...",
                    "error": "Error en la generacion",
                }
                message = phase_messages.get(
                    current_phase,
                    f"Procesando: {current_phase}" if current_phase else "Procesando...",
                )

                status = None
                if current_status == "error":
                    status = "failed"
                    message = f"Error: {message}"
                elif current_status == "ready" and current_progress >= 100:
                    status = "completed"
                    message = "Video generado exitosamente (local)"
                elif current_status == "uploaded":
                    status = "completed"
                    message = "Video subido a YouTube exitosamente"

                await _broadcast_progress(
                    job_id, current_progress, current_phase,
                    message, status, video_id,
                    detail=f"Worker pid={proc.pid} | {current_phase}",
                )

            # Check if job is done (terminal status in DB)
            job_status = job.get("status", "") if job else ""
            is_terminal = job_status in ("completed", "failed", "cancelled")

            # If process died but DB doesn't know yet, mark as failed
            if not process_alive and not is_terminal:
                exit_code = proc.returncode
                logger.warning(
                    "Worker pid=%d exited with code %d before DB update — marking as failed",
                    proc.pid, exit_code,
                )
                log_tail = _read_worker_log_tail(job_id)
                error_detail = f"Worker exited with code {exit_code}"
                if log_tail:
                    error_detail += f"\n--- worker log tail ---\n{log_tail}"
                db.update_job(job_id, status="failed",
                              error_msg=error_detail[:1000])
                db.update_video(video_id, status="error", progress_phase="worker_died")
                await _broadcast_progress(
                    job_id, last_progress, "error",
                    f"Worker termino inesperadamente (exit code={exit_code})",
                    "failed", video_id,
                    detail="El proceso de generacion termino de forma inesperada",
                )
                break

            if is_terminal:
                logger.info(
                    "Worker job #%d reached terminal status: %s (exit_code=%s)",
                    job_id, job_status,
                    proc.returncode if not process_alive else "still running",
                )
                # ── Trigger immediate dispatch of next pending slot ──────
                # Bypasses the 5-min checker loop tick so queued slots
                # fire as soon as the active worker releases resources.
                # Uses deterministic interleaving: shorts always tried first
                # because they generate faster (~10 min vs ~45 min).
                try:
                    from api.services.priority_dispatcher import dispatch_next_priority_slot
                    db2 = _get_db()
                    dispatch_next_priority_slot(db=db2)
                except Exception:
                    pass
                break

    except asyncio.CancelledError:
        logger.info("Progress monitor cancelled for job #%d", job_id)
    except Exception as exc:
        logger.error("Progress monitor for job #%d crashed: %s", job_id, exc)
    finally:
        # Cleanup: remove from tracking
        _active_workers.pop(job_id, None)

        # If worker is still running after terminal DB status, log but DON'T kill it.
        # The worker might have updated the DB before the API detected the terminal status,
        # or the DB status might be stale. Let the worker finish on its own.
        if proc.poll() is None:
            logger.info(
                "Worker pid=%d still alive after job #%d monitor exit — letting it finish naturally",
                proc.pid, job_id,
            )

        logger.info("Progress monitor stopped for job #%d", job_id)


def get_active_workers() -> dict:
    """Return snapshot of currently running worker subprocesses.

    Returns dict: ``{job_id: {"pid": int, "alive": bool}}``
    """
    return {
        jid: {
            "pid": proc.pid,
            "alive": proc.poll() is None if proc else False,
        }
        for jid, proc in _active_workers.items()
    }


# ── Config for switching between in-process vs subprocess modes ──
# Set to True to use the independent subprocess worker (survives API restarts).
# Set to False to use the original in-process generation (legacy behavior).
# Default: True (subprocess mode) — safe for development with hot-reload.

USE_SUBPROCESS_WORKER: bool = getattr(
    settings, "USE_SUBPROCESS_WORKER", True
)


# ── Worker reconnection on API restart ────────────────────────────

async def reconnect_active_workers():
    """Re-spawn progress monitors for workers that survived an API restart.

    Called from api/main.py lifespan on startup. Finds jobs with status
    "running" that have an active worker subprocess, and creates a new
    progress monitor for each. This ensures the WebSocket gets progress
    updates even after the API was restarted mid-generation.
    """
    if not USE_SUBPROCESS_WORKER:
        return

    try:
        db = _get_db()
        running_jobs = db.get_active_jobs()
        running_jobs = [j for j in running_jobs if j["status"] == "running"]

        if not running_jobs:
            return

        # Check if there's an actual worker process for each running job
        for job in running_jobs:
            # ── Skip reassembly jobs ─────────────────────────────────
            # Reassembly jobs run as async tasks (asyncio.create_task),
            # NOT as full_pipeline_worker subprocesses.  Trying to pgrep
            # for them will fail, causing the job to be incorrectly
            # marked as failed (Bug #1).
            if job.get("action") == "reassemble":
                logger.debug("Skipping reassembly job #%d (no subprocess to reconnect)", job["id"])
                continue

            job_id = job["id"]
            video_id = job.get("video_id") or 0

            # Look for a running worker process associated with this job
            worker_found = False
            try:
                result = subprocess.run(
                    ["pgrep", "-f", f"full_pipeline_worker.*--job-id {job_id}"],
                    capture_output=True, text=True, timeout=5,
                )
                if result.stdout.strip():
                    worker_pid = int(result.stdout.strip().split()[0])
                    worker_found = True
                    logger.info(
                        "Reconnecting to worker: job=%d pid=%d (survived API restart)",
                        job_id, worker_pid,
                    )

                    # Create a Popen object for the existing process
                    # We can't create a real Popen for an existing process,
                    # but we can track it with a simple wrapper
                    class _ExistingProcess:
                        def __init__(self, pid):
                            self.pid = pid
                        def poll(self):
                            try:
                                os.kill(self.pid, 0)
                                return None  # Still alive
                            except (ProcessLookupError, PermissionError):
                                return -1  # Dead
                        def wait(self, timeout=None):
                            import time as _t
                            for _ in range(int(timeout or 10)):
                                if self.poll() is not None:
                                    return
                                _t.sleep(1)

                    proc = _ExistingProcess(worker_pid)
                    _active_workers[job_id] = proc

                    # Get channel name
                    ch = db.get_channel(job.get("channel_id")) if job.get("channel_id") else None
                    channel_name = ch.get("name", "?") if ch else "?"

                    # Re-spawn the progress monitor
                    asyncio.create_task(_monitor_worker_progress(
                        job_id=job_id, video_id=video_id, proc=proc,
                        channel_name=channel_name,
                    ))

            except Exception as exc:
                logger.debug("Worker reconnection check for job %d: %s", job_id, exc)

            if not worker_found:
                # Job says "running" but no worker process exists
                # Worker might have crashed — mark as failed
                logger.warning(
                    "Job #%d marked as running but no worker process found — marking failed",
                    job_id,
                )
                db.update_job(job_id, status="failed",
                              error_msg="Worker process not found after API restart")
                db.update_video(video_id, status="error", progress_phase="orphaned")

        if _active_workers:
            logger.info("Reconnected to %d active worker(s) after API restart", len(_active_workers))

    except Exception as exc:
        logger.warning("Worker reconnection failed: %s", exc)


# ── Force-cancel and cleanup ────────────────────────────────────────

async def force_cancel_and_cleanup(job_id: int, video_id: int, channel_slug: str) -> dict:
    """Force-kill the worker subprocess and clean up generated files for a cancelled job.

    1. Kills the worker subprocess (SIGTERM → 3s → SIGKILL)
    2. Cleans orphan ffmpeg processes
    3. Deletes generated video files, clips, and temp data
    4. Marks job as 'cancelled' and video as 'error' in DB

    Returns a dict with cleanup summary: {"killed_worker": bool, "files_cleaned": list, "db_updated": bool}
    """
    import shutil
    from pathlib import Path

    result = {"killed_worker": False, "files_cleaned": [], "db_updated": False}
    project_root = Path(__file__).parent.parent  # api/services/ → project root

    # ── 1. Kill the worker subprocess ──
    proc = _active_workers.pop(job_id, None)
    if proc is not None and proc.poll() is None:
        logger.info("Force-cancelling worker for job #%d (pid=%d)", job_id, proc.pid)
        try:
            proc.terminate()
            await asyncio.sleep(3)
            if proc.poll() is None:
                proc.kill()
                logger.warning("Worker pid=%d did not respond to SIGTERM — sent SIGKILL", proc.pid)
            logger.info("Worker pid=%d terminated (exit_code=%s)", proc.pid, proc.returncode)
            result["killed_worker"] = True
        except Exception as exc:
            logger.error("Failed to kill worker pid=%d: %s", proc.pid, exc)

    # If no active worker found, try to kill by pgrep
    if not result["killed_worker"]:
        try:
            pgrep = subprocess.run(
                ["pgrep", "-f", f"full_pipeline_worker.*--job-id {job_id}"],
                capture_output=True, text=True, timeout=5,
            )
            if pgrep.stdout.strip():
                pid = int(pgrep.stdout.strip().split()[0])
                os.kill(pid, 15)  # SIGTERM
                await asyncio.sleep(3)
                try:
                    os.kill(pid, 0)  # Check if still alive
                    os.kill(pid, 9)  # SIGKILL
                except ProcessLookupError:
                    pass
                logger.info("Killed orphan worker pid=%d for job #%d via pgrep", pid, job_id)
                result["killed_worker"] = True
        except Exception as exc:
            logger.debug("pgrep kill fallback for job #%d: %s", job_id, exc)

    # ── 2. Clean orphan ffmpeg / edge-tts processes ──
    _kill_orphaned_ffmpeg()

    # ── 3. Clean up generated files ──
    cleaned = []

    # Delete partial MP4 output for this video
    if channel_slug and video_id:
        video_output_dir = project_root / "output" / "videos" / channel_slug
        if video_output_dir.exists():
            import re
            pattern = re.compile(rf".*_{video_id}\.mp4$|^{video_id}_.*\.mp4$|.*{video_id}.*\.mp4$")
            for f in video_output_dir.iterdir():
                if f.is_file() and pattern.search(f.name):
                    try:
                        f.unlink()
                        cleaned.append(str(f))
                        logger.info("Cleaned generated MP4: %s", f)
                    except Exception as exc:
                        logger.warning("Could not delete %s: %s", f, exc)

        # Delete generated thumbnail for this video
        thumb_dir = project_root / "output" / "thumbnails" / channel_slug
        if thumb_dir.exists():
            thumb_file = thumb_dir / f"thumb_{video_id}.jpg"
            if thumb_file.exists():
                try:
                    thumb_file.unlink()
                    cleaned.append(str(thumb_file))
                    logger.info("Cleaned generated thumbnail: %s", thumb_file)
                except Exception as exc:
                    logger.warning("Could not delete thumbnail %s: %s", thumb_file, exc)

    # Clean temp directories (lock-aware: never delete files owned by other active jobs)
    for temp_dir_name in ("video_clips", "temp"):
        temp_dir = project_root / "output" / temp_dir_name
        if temp_dir.exists():
            from database.db_extended import ExtendedDatabase
            _db = ExtendedDatabase()
            locked = _db.get_locked_file_paths()
            deleted = 0
            for f in temp_dir.iterdir():
                if not f.is_file():
                    continue
                if str(f) in locked:
                    continue
                try:
                    f.unlink()
                    deleted += 1
                except OSError:
                    pass
            cleaned.append(f"output/{temp_dir_name}/ ({deleted} stale files, {len(locked)} locked preserved)")
            logger.info("Cleaned %d stale files from %s (%d locked)", deleted, temp_dir, len(locked))

    result["files_cleaned"] = cleaned

    # ── 4. Mark job as cancelled and video as error in DB ──
    try:
        db = _get_db()
        db.update_job(job_id, status="cancelled",
                      error_msg="Cancelled by user — files cleaned")
        if video_id:
            db.update_video(video_id, status="error",
                            progress_phase="cancelled",
                            progress=0)
        result["db_updated"] = True
        logger.info("DB updated: job #%d → cancelled, video #%d → error", job_id, video_id)
    except Exception as exc:
        logger.error("DB update after cancel failed for job #%d: %s", job_id, exc)


def _read_worker_log_tail(job_id: int, lines: int = 12) -> str:
    """Read last N lines from worker log for diagnostic context in failed alerts."""
    log_path = os.path.join(settings.LOGS_DIR, f"worker_{job_id}.log")
    try:
        with open(log_path, "r") as f:
            all_lines = f.readlines()
            return "".join(all_lines[-lines:]).rstrip()
    except Exception:
        return ""

    return result
