"""Generation service — orchestrates pipeline execution as async background jobs.

Broadcasts progress via WebSocket to the frontend panel.

v2.2: Restored missing helpers (_get_db, _broadcast_progress, _run_in_executor).
      Added richer feedback details in all progress broadcasts.
v2.3: Added memory guard watcher for video phase to prevent OOM crashes.
"""

import json
import logging
import os
import signal
import subprocess
import sys
import time
import asyncio
import concurrent.futures
import glob as _glob
import importlib
from typing import Optional
import config.settings as settings
from database.db_extended import ExtendedDatabase

logger = logging.getLogger("autotube.generation")

# ── Ffmpeg orphan killer ──────────────────────────────────────

def _kill_orphaned_ffmpeg():
    """Kill any ffmpeg child processes to prevent RAM leaks after job failure.
    
    MoviePy spawns ffmpeg subprocesses that can survive the parent if the
    pipeline crashes. Each orphan consumes 1-2.5 GB RAM decoding source
    videos in raw RGB. Uses a 2-layer cleanup targeting only orphans.
    
    1. Kill children of current PID (immediate children of the API process).
    2. Kill ffmpeg processes whose PPID is 1 (true init orphans).
    """
    killed = 0
    try:
        pid = os.getpid()
        # Layer 1: immediate children of this process
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
                    pass
    except Exception:
        pass

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
                    pass
    except Exception:
        pass

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
                    except (ProcessLookupError, ValueError):
                        pass
        except Exception:
            pass

    if killed > 0:
        logger.warning(
            "Killed %d orphaned process(es) (ffmpeg + edge-tts + yt-dlp RAM leak prevention)", killed
        )

    # ── Reap zombie children to prevent <defunct> processes ──
    try:
        while True:
            wpid, _ = os.waitpid(-1, os.WNOHANG)
            if wpid == 0:
                break
    except (ChildProcessError, OSError):
        pass


def _get_ffmpeg_pids() -> set[int]:
    """Return PIDs of all running ffmpeg processes right now."""
    try:
        r = subprocess.run(
            ["pgrep", "-f", "ffmpeg"],
            capture_output=True, text=True, timeout=3,
        )
        return {int(p) for p in r.stdout.strip().split() if p}
    except Exception:
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
PHASE_TIMEOUTS = {
    "scrape":   None,   # no global limit — each scraper has its own 8s timeout
    "script":   3600,   # 60 min (sequential block generation with stop_event)
    "tts":      7200,   # 2h (Kokoro on CPU for 12-min video)
    "media":    900,    # 15 min (multi-provider + Pollo AI)
    "video":    None,   # infinite (no ceiling for MoviePy rendering)
    "metadata": 300,    # 5 min (LLM)
    "upload":   1800,   # 30 min (YouTube resumable upload)
}

# ── Global render lock (defense in depth) ────────────────────
# v4 (Jul 2026): Only ONE video render at a time across all channels.
# Prevents concurrent ffmpeg instances from causing RAM pressure that
# kills decoders mid-render (the root cause of "all images, no minivideos").
# Per-scene segment rendering already caps RAM per render, but two concurrent
# renders could still exceed total system memory.
_RENDER_SEMAPHORE = asyncio.Semaphore(1)

# Phase order for resume logic (must match execution order)
_PHASE_ORDER = ["scrape", "script", "tts", "media", "video", "metadata", "upload"]


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
        stop_monitor.set()
        if monitor_task is not None:
            monitor_task.cancel()
            try:
                await monitor_task
            except (asyncio.CancelledError, Exception):
                pass


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
                                 resume: bool = False, test_mode: bool = False):
    """Run the full pipeline as an async background job.

    When test_mode=True: low resolution (480x270), no upload, no effects,
    ultrafast preset — for rapid algorithm validation.
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
                                     progress_callback=_progress_cb)
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
            
            db.update_video(video_id, progress=25, progress_phase="script")
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
        
        # ── Phase 2: TTS ─────────────────────────────────────
        if _phase_index("tts") < start_idx:
            logger.info("Skipping TTS (loaded from checkpoint)")
            await _broadcast_progress(job_id, 40, "tts", "Audio cargado desde checkpoint",
                                       video_id=video_id)
        else:
            await _broadcast_progress(job_id, 30, "tts", "Generando voz con IA (TTS)...",
                                       video_id=video_id,
                                       detail="Procesando texto a voz (puede tardar varios minutos)")
            
            ok, audio_data = await _run_in_executor(orch.phase_tts, script,
                                                      timeout=PHASE_TIMEOUTS["tts"], phase="tts")
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
            
            # Pre-render memory estimation based on audio duration
            _est_base_gb = 3.0  # base overhead for compositing + ffmpeg
            _est_rate_gb_per_sec = 8.0 / (1024)  # ~8 MB/sec incremental cost
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
                        "⛔ Pre-render memory gate: estimated %.1f GB needed but only %.1f GB available — "
                        "aborting video phase to prevent OOM",
                        _est_ram_gb, _avail_gb,
                    )
                    await _broadcast_progress(job_id, 60, "video",
                        f"Abortado: memoria insuficiente (est. {_est_ram_gb:.1f} GB, disp. {_avail_gb:.1f} GB)",
                        "failed", video_id)
                    db.update_video(video_id, status="error", progress_phase="video",
                                    timing_data=orch.collect_timing_json())
                    _kill_orphaned_ffmpeg()
                    return
            
            # v4: Acquire global render lock — only one render at a time
            # per-scene segment rendering caps RAM per render, but concurrent
            # renders could still exceed total system memory.
            async with _RENDER_SEMAPHORE:
                logger.info("Render lock ACQUIRED for job %d (channel %d)", job_id, channel_id)
                ok, video_data = await _run_in_executor(orch.phase_video, script, audio_data, media_assets, job_id,
                                                            timeout=PHASE_TIMEOUTS["video"], phase="video",
                                                            memory_guard=not test_mode)
                logger.info("Render lock RELEASED for job %d", job_id)
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
            
            db.update_video(video_id, progress=75, progress_phase="video",
                            status="ready")  # video done — panel can display it
            _save_checkpoint(video_id, "video", {
                "video_path": video_data.get("video_path", ""),
                "thumbnail_path": video_data.get("thumbnail_path", ""),
                "titulo": video_data.get("titulo", ""),
            })
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
        
        await _broadcast_progress(job_id, 88, "thumbnail", "Miniatura generada",
                                   video_id=video_id,
                                   detail="Thumbnail lista para YouTube")
        
        # ── Phase 6: Upload (skip in test mode) ──────────────
        if test_mode:
            await _broadcast_progress(job_id, 95, "upload",
                "Modo pruebas: upload omitido. Video listo localmente.",
                "completed", video_id,
                detail="Video generado en modo pruebas (sin subir a YouTube)")
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
                db.mark_video_uploaded(video_id, video_yt_id, yt_url)
            
                video_path = video_data.get("video_path", "")
                if video_path and Path(video_path).exists():
                    try:
                        Path(video_path).unlink()
                        logger.info(f"Deleted local mp4: {video_path}")
                        db.update_video(video_id, video_path="")
                    except Exception as e:
                        logger.warning(f"Could not delete mp4 {video_path}: {e}")
            
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
        logger.info("Job %d cleanup complete: orchestrator released + gc.collect()", job_id)


async def start_upload_job(job_id: int, video_id: int):
    """Upload only — for re-uploading existing videos."""
    db = _get_db()
    v = db.get_video(video_id)
    if not v:
        await _broadcast_progress(job_id, 0, "upload", "Video no encontrado", "failed")
        return
    
    db.update_job(job_id, status="running")
    channel_id = v.get("channel_id") or 1
    ch = db.get_channel(channel_id)
    canal = ch["slug"] if ch else v.get("canal", "canal2")
    
    await _broadcast_progress(job_id, 5, "upload", "Preparando subida...",
                               video_id=video_id, detail="Cargando datos del video")
    
    try:
        await _broadcast_progress(job_id, 10, "upload", "Autenticando con YouTube...",
                                   video_id=video_id, detail="Verificando credenciales OAuth")
        
        from orchestrator import PipelineOrchestrator
        orch = PipelineOrchestrator(canal=canal)
        
        if not orch.uploader.authenticate():
            await _broadcast_progress(job_id, 20, "upload",
                "Error: Fallo autenticacion YouTube", "failed", video_id,
                detail="No se pudo autenticar. Verifica las credenciales del canal.")
            return
        
        await _broadcast_progress(job_id, 30, "upload", "Subiendo video...",
                                   video_id=video_id,
                                   detail="Transfiriendo archivo a YouTube (puede tardar)")
        
        import json
        tags_raw = v.get("tags_json", "[]")
        if isinstance(tags_raw, str):
            try:
                tags = json.loads(tags_raw)
            except json.JSONDecodeError:
                tags = []
        else:
            tags = tags_raw or []
        
        def _do_upload():
            return orch.uploader.upload(
                video_path=Path(v["video_path"]),
                title=v.get("titulo_final", "Video sin titulo"),
                description=v.get("description", ""),
                tags=tags,
                thumbnail_path=Path(v["thumbnail_path"]) if v.get("thumbnail_path") else None,
                privacy=v.get("privacy_status", "unlisted"),
            )
        
        ok, result = await _run_in_executor(_do_upload, timeout=PHASE_TIMEOUTS["upload"])
        
        if not ok:
            await _broadcast_progress(job_id, 30, "upload", f"Error: {result}", "failed", video_id,
                                       detail="La subida fallo. Revisa los logs.")
            return
        
        video_yt_id = result.get("video_id")
        if video_yt_id:
            url = result.get("url", f"https://youtube.com/watch?v={video_yt_id}")
            db.mark_video_uploaded(video_id, video_yt_id, url)
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


async def replace_scene_image_task(scene_id: int, description: str):
    """Replace image for a single scene."""
    db = _get_db()
    try:
        from pipeline.image_fetcher import ImageFetcher
        from pipeline.image_processor import ImageProcessor
        from config import canal2_config as cfg
        
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
    # ── Pre-flight: kill lingering ffmpegs from prior failed attempts ──
    _kill_orphaned_ffmpeg()
    
    try:
        await _do_reassembly(job_id, video_id)
    finally:
        _kill_orphaned_ffmpeg()


async def _do_reassembly(job_id: int, video_id: int):
    import json, time
    from pathlib import Path
    from pipeline.video_editor import VideoEditor
    from config.config_bridge import get_channel_config

    db = _get_db()
    video = db.get_video(video_id)
    if not video:
        await _broadcast_progress(job_id, 0, "error", "Video no encontrado", "failed")
        return

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

        # Timestamps
        ts_path = Path(str(audio_path).replace(".mp3", "_timestamps.json"))
        timestamps = json.loads(ts_path.read_text()) if ts_path.exists() else []

        # Media
        media_cp = cp.get("media", {})
        assets = media_cp.get("assets", [])
        scene_ranges = media_cp.get("scene_ranges")
        if not assets:
            await _broadcast_progress(job_id, 0, "error",
                                       "No hay assets en el checkpoint", "failed")
            return

        # Channel config
        canal = video.get("canal", "canal2")
        config = get_channel_config(canal)
    except Exception as e:
        logger.exception("Reassembly data load failed")
        await _broadcast_progress(job_id, 0, "error", f"Error cargando datos: {e}", "failed")
        return

    await _broadcast_progress(job_id, 10, "video",
                               f"Re-ensamblando: {len(bloques)} bloques, {len(assets)} assets",
                               video_id=video_id,
                               detail=f'Audio: {len(timestamps)} timestamps, {Path(audio_path).stat().st_size / 1e6:.1f} MB')

    # ── Phase 2: Build video ──────────────────────────────────
    await _broadcast_progress(job_id, 20, "video", "Construyendo escenas...",
                               video_id=video_id,
                               detail=f"Procesando {len(scene_ranges) if scene_ranges else len(bloques)} escenas")

    try:
        def _build_video():
            ve = VideoEditor(config)
            return ve.build_video(
                bloques=bloques,
                media_assets=assets,
                audio_path=audio_path,
                timestamps=timestamps,
                scene_ranges=scene_ranges,
                cta_audio_path=tts_cp.get("cta_audio_path"),
            )

        # Run in a thread — build_video() is blocking
        import concurrent.futures
        loop = asyncio.get_running_loop()
        future = loop.run_in_executor(None, _build_video)

        # Poll progress every 10s while rendering
        start = time.time()
        last_pct = 20
        while not future.done():
            try:
                await asyncio.wait_for(asyncio.shield(future), timeout=10)
            except asyncio.TimeoutError:
                elapsed = time.time() - start
                # Estimate: assume ~3 fps for a 24fps video = ~2h for 24k frames
                # Show elapsed time as fallback detail
                await _broadcast_progress(
                    job_id, last_pct, "video",
                    "Renderizando video (MoviePy)...",
                    video_id=video_id,
                    detail=f"Render en progreso ({elapsed:.0f}s transcurridos)"
                )
                last_pct = min(last_pct + 5, 90)

        output_path = future.result()

        elapsed = time.time() - start
        await _broadcast_progress(job_id, 95, "video",
                                   f"Video renderizado en {elapsed:.0f}s",
                                   video_id=video_id,
                                   detail=f"Archivo: {Path(output_path).name}")

    except Exception as e:
        logger.exception("Video build failed")
        await _broadcast_progress(job_id, 0, "error",
                                   f"Error ensamblando video: {e}", "failed",
                                   video_id=video_id)
        db.update_video(video_id, status="error", progress_phase="video")
        return

    # ── Phase 3: Done ────────────────────────────────────────
    db.update_video(video_id, video_path=str(output_path),
                    status="ready", progress=100, progress_phase=None)
    await _broadcast_progress(job_id, 100, "done",
                               f"Video listo: {titulo[:60]}", "completed",
                               video_id=video_id,
                               detail=str(output_path))
    logger.info("Reassembly job %d completed: %s", job_id, output_path)


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
    1. Mark all running/queued jobs as failed (server restarted).
    2. Fix videos stuck in 'generating'/'reassembling' → 'error'.
    3. For each video in 'error' with checkpoint data:
        - If the failure was a bug → skip (leave for manual analysis).
        - If the failure was an interruption → create a reassemble job.
        - If >= 3 reassembly attempts already failed → mark as bug_crash, skip.
    """
    import json
    
    MAX_RECOVERY_ATTEMPTS = 3

    log = logging.getLogger("autotube.startup")
    db = _get_db()
    conn = db._connect()

    # ── Step 1: Kill stale jobs ───────────────────────────────
    killed = conn.execute(
        "UPDATE generation_jobs SET status='failed', "
        "error_msg='Server restarted — old process no longer exists' "
        "WHERE status IN ('running','queued')"
    ).rowcount
    if killed:
        log.info("Marked %d stale job(s) as failed (server restart)", killed)

    # ── Step 2: Reset stuck video states ──────────────────────
    stuck = conn.execute(
        "UPDATE videos SET status='error', progress_phase='interrupted' "
        "WHERE status IN ('generating','reassembling')"
    ).rowcount
    if stuck:
        log.info("Reset %d stuck video(s) generating/reassembling → error", stuck)

    conn.commit()

    # ── Step 3: Auto-recover recoverable videos ───────────────
    rows = conn.execute(
        "SELECT * FROM videos WHERE status='error' "
        "AND checkpoint_data IS NOT NULL AND checkpoint_data != '{}' "
        "ORDER BY created_at DESC"
    ).fetchall()

    bugs_skipped = 0
    recovered = 0
    unrecoverable = 0
    processed_ids: set[int] = set()  # guard against re-processing the same video

    for row in rows:
        video_id = row["id"]
        
        # ── Dedup guard: skip videos already handled in this recovery run ──
        if video_id in processed_ids:
            continue
        processed_ids.add(video_id)
        
        progress_phase = (row["progress_phase"] or "").strip()

        # Already handled in this run (interrupted → skip existing recoveries)
        if progress_phase == "interrupted":
            pass  # continue to check recoverability

        video_dict = dict(row)

        # ── Parse checkpoint ──────────────────────────────────
        cp_raw = video_dict.get("checkpoint_data", "{}")
        try:
            cp = json.loads(cp_raw) if isinstance(cp_raw, str) else (cp_raw or {})
        except (json.JSONDecodeError, TypeError):
            cp = {}

        # Must have tts + media to reassemble
        if not cp.get("tts") or not cp.get("media"):
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
        failed_reassemblies = conn.execute(
            "SELECT COUNT(*) FROM generation_jobs "
            "WHERE video_id=? AND action='reassemble' AND status='failed'",
            (video_id,),
        ).fetchone()[0]
        if failed_reassemblies >= MAX_RECOVERY_ATTEMPTS:
            log.warning(
                "Video %d: %d failed reassembly attempts (max=%d) — marking as bug_crash",
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
        channel_id = video_dict.get("channel_id") or 3

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
            job_id = db.create_job(channel_id, "reassemble", video_id)
            log.info("Video %d: AUTO-RECOVERING → job %d (phase was '%s')",
                     video_id, job_id, progress_phase)
            await _run_reassembly_job(job_id, video_id)  # serial: one at a time
            recovered += 1
        except Exception as exc:
            log.warning("Video %d: recovery failed — %s", video_id, exc)
            unrecoverable += 1

    log.info("Startup recovery complete: %d bug(s) skipped, %d recovered, %d unrecoverable",
             bugs_skipped, recovered, unrecoverable)
