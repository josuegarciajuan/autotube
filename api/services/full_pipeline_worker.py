#!/usr/bin/env python3
"""Full pipeline worker — standalone process for video generation.

Executed as an independent subprocess so API restarts (hot-reload, deployment)
do NOT kill in-progress video generation. The worker communicates progress
and final results back to the database; the API polls the DB and broadcasts
to the frontend via WebSocket.

Usage (spawned by API):
    python3 api/services/full_pipeline_worker.py \\
        --job-id 5 --channel-id 1 --video-id 42 \\
        --action generate_and_upload [--test-mode] [--no-upload]

Design principles:
  - Survives parent process death (start_new_session)
  - All state is persisted to the database — nothing in memory is critical
  - FFmpeg orphans are cleaned up on exit to prevent RAM leaks
  - Progress is written to the videos table for API polling
"""

from __future__ import annotations

import argparse
import importlib
import json
import logging
import os
import signal
import subprocess
import sys
import threading
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

# ── Ensure the project root is on sys.path BEFORE any project imports ──
# When invoked as a script (python3 api/services/full_pipeline_worker.py),
# Python places the script's directory on sys.path[0], NOT the CWD.
# The project root (autotube/) must be added so `from api.utils import ...` works.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

# ── Lifecycle monitoring (import AFTER sys.path fix) ──
from api.services.lifecycle_monitor import (
    log_event as log_lifecycle,
    log_phase_start,
    log_phase_end,
    log_phase_error,
    emit_alert,
)
from api.utils import db_now

# ── Maximum hours ahead we allow when seeding scheduled_upload_at from a
# planned slot's target_upload_at. Beyond this, the slot's upload target is
# treated as a far-future buffer slot and the upload scheduler computes a
# fresh time instead (avoiding videos sitting in awaiting_upload for days).
MAX_SEED_UPLOAD_AT_AHEAD_HOURS = 12


def _clamp_seed_upload_at(raw_seed: str, canal: str):
    """Clamp a planned slot's target_upload_at into a sane seed value.

    Returns the raw string if it is within [now, now+12h], else None.
    None means "don't seed" — the upload scheduler will compute a fresh time
    in the next available upload window.
    """
    if not raw_seed:
        return None
    _log = logging.getLogger("autotube.worker")
    try:
        from datetime import datetime as _dt
        raw = str(raw_seed).strip()
        # Accept both naive "YYYY-MM-DD HH:MM:SS" and ISO/T-separated forms.
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
            try:
                seed_dt = _dt.strptime(raw[:19], fmt)
                break
            except ValueError:
                seed_dt = None
        if seed_dt is None:
            return None
        if seed_dt.tzinfo is None:
            seed_dt = seed_dt.replace(tzinfo=timezone.utc)
        from datetime import timedelta as _td
        now = _dt.now(timezone.utc)
        if seed_dt < now:
            _log.info("[%s] Slot upload target %s is in the past — not seeding", canal, raw)
            return None
        if seed_dt > now + _td(hours=MAX_SEED_UPLOAD_AT_AHEAD_HOURS):
            _log.info(
                "[%s] Slot upload target %s is >%dh ahead — not seeding (upload scheduler will reschedule)",
                canal, raw, MAX_SEED_UPLOAD_AT_AHEAD_HOURS,
            )
            return None
        return raw
    except Exception as exc:
        _log.debug("[%s] Could not parse seed upload_at '%s': %s", canal, raw_seed, exc)
        return None


def _alert_nonfatal(db, video_id: int, channel_id: int, phase: str,
                    exc, extra: dict = None):
    """Alert on a non-fatal phase failure (silent degradation).

    Los fallos no-fatales (metadata, thumbnails A/B, cross-platform, stats...)
    degradan la calidad del output pero no matan el pipeline. Antes quedaban en
    logger.warning silencioso; ahora se crean como alerta 'phase_nonfatal'
    (una por video, dedup por entity_type+entity_id+alert_type) para que el
    operador los vea y los solvente a posteriori.
    """
    try:
        emit_alert(
            db, entity_type='video', entity_id=video_id, channel_id=channel_id,
            alert_type='phase_nonfatal', severity='warning',
            title=f"Video #{video_id}: fallo no-fatal en fase '{phase}'",
            message=f"La fase '{phase}' falló pero el pipeline continuó: {exc}",
            metadata={"phase": phase, "detail": str(exc)[:500], **(extra or {})},
        )
    except Exception:
        pass


# ── Logging setup ──────────────────────────────────────────────────

def _retry_end_screens_worker(browser, yt_video_id: str, wlog, max_retries: int = 3) -> bool:
    """Retry end screen configuration with exponential backoff (worker process)."""
    import time as _wt
    import random as _wr
    for attempt in range(1, max_retries + 1):
        success = browser.add_end_screens(yt_video_id)
        if success:
            return True
        if attempt < max_retries:
            wait_s = 30 * (2 ** (attempt - 1)) + _wr.uniform(0, 15)
            wlog.warning(
                "End screens attempt %d/%d failed for %s — retrying in %.0fs",
                attempt, max_retries, yt_video_id, wait_s,
            )
            _wt.sleep(wait_s)
        else:
            wlog.error(
                "End screens exhausted %d retries for %s — giving up",
                max_retries, yt_video_id,
            )
    return False


def _auto_mark_ia_worker(yt_video_id: str, canal: str, account: str, video_id: int):
    """Background thread (worker subprocess): mark video as AI-generated + configure end screens.

    End screens are ALWAYS attempted independently of the IA-mark result.
    """
    wlog = logging.getLogger("autotube.worker")
    import time as _time
    import random
    from pipeline.youtube_browser import cleanup_browser_thread

    try:
        db = None

        # ── Wait for YouTube to finish processing (60s, was 20s) ──
        wlog.info("[%s] Waiting 60s for YouTube processing before Studio automation...", canal)
        _time.sleep(60)

        from pipeline.youtube_browser import get_browser
        browser = get_browser(account)

        # ── Step 1: Mark AI-generated content (best-effort, non-blocking) ──
        try:
            success = browser.mark_altered_content(yt_video_id)
            if success:
                from database.db_extended import ExtendedDatabase
                db = ExtendedDatabase()
                db.update_video(video_id, manual_altered_content_done=1)
                wlog.info("[%s] IA altered content marked for %s", canal, yt_video_id)
            else:
                wlog.warning("[%s] Failed to mark altered content for %s — continuing to end screens anyway", canal, yt_video_id)
                emit_alert(
                    None, entity_type='video', entity_id=video_id,
                    alert_type='phase_nonfatal', severity='warning',
                    title=f"Video #{video_id}: fallo no-fatal en fase 'auto_mark_ia'",
                    message=f"No se marcó 'contenido alterado/IA' para {yt_video_id}. "
                            "Riesgo de strike si el contenido es IA y no está marcado.",
                    metadata={"phase": "auto_mark_ia", "yt_video_id": yt_video_id},
                )
        except Exception as e:
            wlog.warning("[%s] IA-mark error for %s: %s — continuing to end screens anyway", canal, yt_video_id, e)
            try:
                emit_alert(
                    None, entity_type='video', entity_id=video_id,
                    alert_type='phase_nonfatal', severity='warning',
                    title=f"Video #{video_id}: fallo no-fatal en fase 'auto_mark_ia'",
                    message=f"Error marcando 'contenido alterado/IA' para {yt_video_id}: {e}",
                    metadata={"phase": "auto_mark_ia", "yt_video_id": yt_video_id},
                )
            except Exception:
                pass

        # ── Step 2: Configure end screens (always attempted, with retries) ──
        try:
            from config.config_bridge import get_channel_config
            channel_config = get_channel_config(canal)
            if channel_config and getattr(channel_config, "AUTO_END_SCREENS", False):
                # Natural human delay between actions (5-12s)
                delay = random.uniform(5, 12)
                wlog.info("[%s] Waiting %.1fs before end screen config...", canal, delay)
                _time.sleep(delay)

                wlog.info("[%s] 🎬 Attempting end screens for %s (up to 3 retries)", canal, yt_video_id)
                success2 = _retry_end_screens_worker(browser, yt_video_id, wlog, max_retries=3)
                if success2:
                    if db is None:
                        from database.db_extended import ExtendedDatabase
                        db = ExtendedDatabase()
                    db.update_video(video_id, manual_end_screens_done=1)
                    wlog.info("[%s] ✅ End screens configured for %s", canal, yt_video_id)
                else:
                    wlog.warning("[%s] ❌ Failed to configure end screens for %s after all retries", canal, yt_video_id)
                    try:
                        emit_alert(
                            None, entity_type='video', entity_id=video_id,
                            alert_type='phase_nonfatal', severity='warning',
                            title=f"Video #{video_id}: fallo no-fatal en fase 'end_screens'",
                            message=f"No se pudieron configurar las pantallas finales para {yt_video_id} "
                                    "tras 3 reintentos. Configurarlas manualmente en YouTube Studio.",
                            metadata={"phase": "end_screens", "yt_video_id": yt_video_id},
                        )
                    except Exception:
                        pass
            else:
                wlog.debug("[%s] AUTO_END_SCREENS disabled, skipping", canal)
        except Exception as e:
            wlog.warning("[%s] Auto end-screen error for %s: %s", canal, yt_video_id, e)
    finally:
        cleanup_browser_thread()


# ── Shared state for graceful shutdown during critical phases ─────
# These are module-level so both run_job() and main()/signal handler
# can access them.  run_job() sets _in_critical_phase during ffmpeg
# concat to defer SIGTERM until the render completes safely.
_shutdown_requested = threading.Event()
_in_critical_phase = threading.Event()


def _setup_worker_logging(job_id: int):
    """Configure logging for the worker — writes to both stderr and a per-job log file."""
    LOG_DIR = _PROJECT_ROOT / "logs"
    LOG_DIR.mkdir(exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format=f"%(asctime)s [%(levelname)s] worker#{job_id}: %(message)s",
        handlers=[
            logging.StreamHandler(sys.stderr),
            logging.FileHandler(LOG_DIR / f"worker_{job_id}.log"),
        ],
    )
    # Suppress noisy libraries
    for lib in ["urllib3", "googleapiclient", "google.auth", "apscheduler", "PIL",
                "httpx", "httpcore", "openai", "moviepy"]:
        logging.getLogger(lib).setLevel(logging.WARNING)
    return logging.getLogger("autotube.worker")


# ── Memory guard for video assembly ────────────────────────────

def _check_memory_before_video(logger: logging.Logger, min_free_gb: float = 2.5, max_wait_sec: int = 600) -> bool:
    """Wait for free memory before starting memory-intensive video assembly.
    
    ffmpeg xfade concat of many segments can consume several GB of RAM.
    Running it while the system is near OOM triggers the kernel OOM killer
    or crashes with memory errors — both of which lose the entire render.
    
    Umbral rebajado a 2.5 GB (2026-08-22): el concat por lotes pasó de 50 → 25
    segmentos (pico ~1.5 GB), así que el ensamblaje ya no necesita 6 GB libres.
    El pico de 50 segmentos (~3 GB) y Kokoro (~2.8 GB) que motivaron el umbral
    de 6 GB ya no aplican: Kokoro se descarga antes de esta fase y el lote es
    de 25.

    Returns True if memory is sufficient, False if critically low even
    after waiting (caller should fail the job gracefully).
    """
    # Use MemAvailable via ram_governor (includes reclaimable page cache).
    # SC_AVPHYS_PAGES underreports available RAM by 5-10 GB on Linux.
    avail_mb = _get_available_memory_mb()
    if avail_mb < 0:
        try:
            import psutil
            avail_mb = psutil.virtual_memory().available / (1024 ** 2)
        except ImportError:
            return True  # can't check — allow proceeding
    avail_gb = avail_mb / 1024.0
    
    if avail_gb >= min_free_gb:
        logger.info("Memory OK: %.1f GB free (threshold: %.1f GB)", avail_gb, min_free_gb)
        return True
    
    logger.warning(
        "⚠️  LOW MEMORY: only %.1f GB free (need %.1f GB). "
        "Waiting up to %ds for memory to recover...",
        avail_gb, min_free_gb, max_wait_sec,
    )
    waited = 0
    while avail_gb < min_free_gb and waited < max_wait_sec:
        time.sleep(5)
        waited += 5
        avail_mb = _get_available_memory_mb()
        if avail_mb < 0:
            try:
                import psutil
                avail_mb = psutil.virtual_memory().available / (1024 ** 2)
            except ImportError:
                break
        avail_gb = avail_mb / 1024.0
    if avail_gb >= min_free_gb:
        logger.info("Memory recovered: %.1f GB free after %ds wait", avail_gb, waited)
        return True
    else:
        logger.error(
            "❌ CRITICAL: only %.1f GB free after %ds — aborting to prevent OOM kill. "
            "Job will be marked as failed with RAM shortage.",
            avail_gb, waited,
        )
        return False


# ── FFmpeg orphan cleanup ────────────────────────────────────────

def _kill_orphaned_ffmpeg() -> None:
    """Kill orphaned ffmpeg processes before worker exits.

    Three-layer cleanup:
      1. Children of this worker process (immediate ffmpeg children)
      2. True orphans (parent = init/pid 1)
      3. Reap zombies
    """
    killed = 0
    pid = os.getpid()

    # Layer 1: children of this worker
    try:
        r = subprocess.run(
            ["pgrep", "-P", str(pid), "-f", "ffmpeg"],
            capture_output=True, text=True, timeout=5,
        )
        if r.stdout.strip():
            for cpid in r.stdout.strip().split():
                try:
                    os.kill(int(cpid), signal.SIGKILL)
                    killed += 1
                except ProcessLookupError:
                    pass
    except Exception:
        pass

    # Layer 2: true orphans
    try:
        r = subprocess.run(
            ["pgrep", "-P", "1", "-f", "ffmpeg"],
            capture_output=True, text=True, timeout=5,
        )
        if r.stdout.strip():
            for opid in r.stdout.strip().split():
                try:
                    os.kill(int(opid), signal.SIGKILL)
                    killed += 1
                except ProcessLookupError:
                    pass
    except Exception:
        pass

    # Layer 3: edge-tts and yt-dlp orphans
    # SAFE: checks parent PID before killing — only kills processes owned
    # by init (true orphans) or this worker process. Prevents accidentally
    # killing a concurrent short's yt-dlp download or edge-tts synthesis.
    for pattern in ("edge-tts", "yt-dlp"):
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
                        # Only kill if parent is init (true orphan) or this worker
                        if ppid in (1, os.getpid()):
                            os.kill(int(opid), signal.SIGKILL)
                            killed += 1
                    except (ProcessLookupError, ValueError):
                        pass
        except Exception:
            pass

    if killed:
        logging.getLogger("autotube.worker").warning(
            "Killed %d orphaned process(es) (RAM leak prevention)", killed
        )

    # Reap zombies
    try:
        while True:
            wpid, _ = os.waitpid(-1, os.WNOHANG)
            if wpid == 0:
                break
    except (ChildProcessError, OSError):
        pass


# ── RAM gate ──────────────────────────────────────────────────────

def _get_available_memory_mb() -> float:
    """Return available system memory in MB using MemAvailable from /proc/meminfo.

    Delegates to pipeline.ram_governor.available_mb() which reads MemAvailable
    (includes reclaimable page cache), not just MemFree. This avoids false
    positives when ffmpeg/moviepy have saturated the page cache but plenty of
    reclaimable memory is still available.

    Returns -1 on any error.
    """
    try:
        from pipeline.ram_governor import available_mb
        return float(available_mb())
    except (ImportError, Exception):
        # Fallback: sysconf (legacy — underreports available RAM by 5-10 GB)
        try:
            avail_bytes = os.sysconf('SC_AVPHYS_PAGES') * os.sysconf('SC_PAGE_SIZE')
            return avail_bytes / (1024 * 1024)
        except Exception:
            return -1.0


def _check_ram_gate(logger, timeout_sec: int = 600, threshold: int | None = None) -> bool:
    """Check if there's enough RAM to proceed with generation.

    Uses MIN_FREE_FOR_RENDER_MB from config.settings as the threshold
    (default 5000 MB), or an explicit ``threshold`` (e.g. the higher
    MIN_FREE_FOR_TTS_MB for Kokoro/torch). Delegates to
    pipeline.ram_governor.wait_for_ram() which blocks until enough RAM
    is free or timeout expires.

    This avoids the previous hard abort — the pipeline now waits for
    memory to free up (e.g. from other processes finishing), matching
    the behaviour of the shorts scheduler.

    Returns True if OK, False if timeout expires.
    """
    from config.settings import MIN_FREE_FOR_RENDER_MB
    from pipeline.ram_governor import wait_for_ram, available_mb

    threshold = threshold or MIN_FREE_FOR_RENDER_MB
    avail_mb = available_mb()
    if avail_mb >= threshold:
        logger.info("RAM check: %.0f MB free — OK", avail_mb)
        return True

    logger.warning(
        "RAM gate: %.0f MB free < %d MB needed — waiting up to %ds for memory to free",
        avail_mb, threshold, timeout_sec,
    )
    if wait_for_ram(threshold, timeout_sec=timeout_sec):
        logger.info("RAM gate: memory freed — %.0f MB free", available_mb())
        return True

    logger.error(
        "RAM gate: timeout after %ds waiting for %d MB — only %.0f MB free",
        timeout_sec, threshold, available_mb(),
    )
    return False


# ── Phase pipelining: DB-based render slot (macro-fase B) ────────

def _acquire_render_slot(job_id: int, db, timeout_sec: int = 7200) -> bool:
    """Acquire exclusive render access via a DB-based lock.
    
    Used for phase pipelining: worker B does prep phases while worker A 
    renders. Before entering the video (render) phase, the worker must 
    acquire this slot to ensure only ONE render runs at a time.
    
    The lock is implemented as a simple atomic check: count other jobs
    in 'render' phase, and if zero, update this job's pipeline_phase to 
    'render'. The UPDATE + check happens in the same transaction for 
    atomicity.
    
    Returns True if acquired, False on timeout.
    """
    import time as _time
    logger = logging.getLogger("autotube.worker")
    deadline = _time.time() + timeout_sec
    
    while _time.time() < deadline:
        with db._connect() as conn:
            # Atomic check-and-claim: count other active renders
            row = conn.execute(
                """SELECT COUNT(*) as cnt FROM generation_jobs
                   WHERE status IN ('running', 'queued')
                     AND pipeline_phase = 'render'
                     AND id != ?""",
                (job_id,),
            ).fetchone()
            active_renders = row["cnt"] if row else 0
            
            if active_renders == 0:
                # No other render active — claim the slot
                conn.execute(
                    "UPDATE generation_jobs SET pipeline_phase='render' WHERE id=?",
                    (job_id,),
                )
                conn.commit()
                logger.info("Render slot acquired for job #%d", job_id)
                return True
        
        logger.info(
            "Waiting for render slot (job #%d) — %d active render(s)",
            job_id, active_renders,
        )
        _time.sleep(30)
    
    logger.error(
        "Render slot timeout for job #%d after %ds", job_id, timeout_sec,
    )
    return False


# ── Pre-flight cleanup ────────────────────────────────────────────

def _preflight_cleanup(logger):
    """Clean temp directories before starting the pipeline.
    
    Lock-aware: never deletes files owned by another active (running/queued)
    generation_job, preventing race conditions that cause black-screen renders.
    Also preserves clips referenced by recent error-state videos (pending
    reassembly), so a failed video keeps its media for the rebuild.
    """
    from database.db_extended import ExtendedDatabase
    _db = ExtendedDatabase()
    locked = _db.get_locked_file_paths()
    error_paths = _db.get_error_video_media_paths(max_age_hours=48)
    preserved = locked | error_paths
    
    cleanup_dirs = [
        _PROJECT_ROOT / "output" / "video_clips",
        _PROJECT_ROOT / "output" / "temp",
    ]
    for d in cleanup_dirs:
        if not d.exists():
            continue
        deleted = 0
        for f in d.iterdir():
            if not f.is_file():
                continue
            if str(f) in preserved:
                continue
            try:
                f.unlink()
                deleted += 1
            except OSError:
                pass
        logger.info("Cleaned %d stale files from %s (%d locked+error files preserved)",
                     deleted, d, len(preserved))


# ── Phase order (for checkpoint resume) ──────────────────────────

_PHASE_ORDER = ["scrape", "script", "pre_validate", "tts", "media", "video", "metadata", "post_validate", "upload"]

_PHASE_INDEX = {p: i for i, p in enumerate(_PHASE_ORDER)}


# ── Checkpoint helpers ─────────────────────────────────────────────

def _load_checkpoint(video_id: int, db) -> tuple[dict, str, int]:
    """Load checkpoint data from DB. Returns (checkpoint_dict, last_phase, last_idx).
    
    If no checkpoint exists, returns ({}, "", -1).
    """
    v = db.get_video(video_id)
    if not v or not v.get("checkpoint_data"):
        return {}, "", -1
    
    cp_raw = v["checkpoint_data"]
    try:
        cp = json.loads(cp_raw) if isinstance(cp_raw, str) else (cp_raw or {})
    except (json.JSONDecodeError, TypeError):
        return {}, "", -1
    
    if not cp:
        return {}, "", -1
    
    # Find the last completed phase
    last_phase = v.get("progress_phase") or ""
    last_idx = _PHASE_INDEX.get(last_phase, -1)
    
    # If progress_phase is invalid (e.g. "error" set by a prior failed run),
    # scan checkpoint keys for the furthest completed phase
    if last_idx < 0:
        for phase in reversed(_PHASE_ORDER):
            if phase in cp:
                last_phase = phase
                last_idx = _PHASE_INDEX[phase]
                break
    
    return cp, last_phase, last_idx


def _save_checkpoint(video_id: int, phase: str, data: dict, db) -> bool:
    """Save checkpoint data for a phase to the video record. Merges with existing."""
    v = db.get_video(video_id)
    existing = {}
    if v and v.get("checkpoint_data"):
        cp_raw = v["checkpoint_data"]
        try:
            existing = json.loads(cp_raw) if isinstance(cp_raw, str) else (cp_raw or {})
        except (json.JSONDecodeError, TypeError):
            existing = {}
    
    existing[phase] = data
    try:
        db.update_video(
            video_id,
            checkpoint_data=json.dumps(existing, ensure_ascii=False),
        )
        return True
    except Exception:
        return False


# ── Main worker function ──────────────────────────────────────────

def run_job(
    job_id: int,
    channel_id: int,
    video_id: int,
    action: str = "generate_and_upload",
    test_mode: bool = False,
    upload: bool = True,
    source_mode: str = "original",
    viral_candidate_id: int = None,
) -> bool:
    """Run the complete video generation pipeline as a standalone process.

    Supports checkpoint/resume: if the video record already has checkpoint_data
    from a previous interrupted run, completed phases are skipped and the
    pipeline resumes from the last incomplete phase.

    All progress is written to the database for the API to poll.
    The worker survives parent process death (API restart).

    Returns True on success, False on failure.
    """
    logger = logging.getLogger("autotube.worker")

    # ── 1. Initialize DB ──────────────────────────────────────
    from database.db import init_db
    from database.db_extended import migrate_v2, ExtendedDatabase

    init_db()
    migrate_v2()
    db = ExtendedDatabase()

    # ── 2. Load channel info ──────────────────────────────────
    ch = db.get_channel(channel_id)
    if not ch:
        logger.error("Channel %d not found in DB", channel_id)
        db.update_job(job_id, status="failed", error_msg="Canal no encontrado")
        db.update_video(video_id, status="error", progress_phase="error",
                        error_message="Canal no encontrado")
        return False

    canal = ch["slug"]
    channel_name = ch.get("name", canal)
    
    # ── 2b. Ghost-worker guard: verify the job still exists in DB ──
    # Job rows can be deleted externally (channel deletion cascade,
    # manual DB cleanup) while the worker subprocess continues running.
    # Without this check, the worker wastes resources doing work that
    # will never be recorded, and the finally-block UPDATE silently
    # matches zero rows.
    job_row = db.get_job(job_id)
    if job_row is None:
        logger.error(
            "GHOST WORKER DETECTED: job %d not found in generation_jobs table — "
            "row was likely deleted externally. Self-terminating to avoid "
            "wasting resources.", job_id
        )
        db.update_video(video_id, status="error",
                        progress_phase="ghost_job",
                        progress_message=f"Worker {os.getpid()} detected missing job row {job_id}")
        return False
    
    # ── 3. Load checkpoint (resume support) ───────────────────
    checkpoint, last_phase, last_idx = _load_checkpoint(video_id, db)

    # ── Fase 1.3 (ago 2026): upload_only salta TODA la generación ──
    # El vídeo ya está generado (F1) — solo subir, sin re-scrapear/re-renderizar.
    # script/video_data/metadata se cargan del checkpoint (claves 'script',
    # 'video', 'metadata' guardadas por generate_only).
    if action == "upload_only":
        start_idx = _PHASE_INDEX["upload"]
        start_phase = "upload"
        logger.info("upload_only: saltando generación, inicio directo en '%s'", start_phase)
    elif last_idx >= 0:
        start_idx = last_idx + 1
        start_phase = _PHASE_ORDER[start_idx] if start_idx < len(_PHASE_ORDER) else "done"
        logger.info("Checkpoint found: last_phase=%s (idx=%d) → resuming from %s",
                    last_phase, last_idx, start_phase)
    else:
        start_idx = 0
        start_phase = "scrape"
    
    logger.info("Starting pipeline for channel '%s' (id=%d), video_id=%d, action=%s, test_mode=%s, start=%s",
                canal, channel_id, video_id, action, test_mode, start_phase)

    # ── 4. RAM gate ───────────────────────────────────────────
    if not test_mode:
        avail_mb = _get_available_memory_mb()
        if avail_mb >= 0 and avail_mb < 2500:
            logger.warning("Low RAM: only %.0f MB free", avail_mb)
    
    if test_mode:
        avail_mb = _get_available_memory_mb()
        if avail_mb >= 0 and avail_mb < 1000:
            logger.error("Aborting test: only %.0f MB free (need ≥ 1000 MB)", avail_mb)
            db.update_job(job_id, status="failed", error_msg="RAM insuficiente para test")
            db.update_video(video_id, status="error", progress_phase="blocked")
            return False

    # ── 5. Update job and video status ────────────────────────
    db.update_job(job_id, status="running", started_at=db_now(),
                  worker_pid=os.getpid(), pipeline_phase="prep")
    # Fase 1.3: upload_only marca 'uploading' (no 'generating' — el vídeo ya existe)
    _start_status = "uploading" if action == "upload_only" else "generating"
    db.update_video(video_id, status=_start_status, progress=1 if start_idx == 0 else 2,
                    progress_phase="inicio" if start_idx == 0 else f"resume_{start_phase}",
                    generation_started_at=db_now())

    # ── Log lifecycle: generation started ──
    try:
        log_lifecycle(db, entity_type='video', entity_id=video_id, channel_id=channel_id,
                      event='generation_started', status='started',
                      message=f'Pipeline started (action={action}, source_mode={source_mode})',
                      metadata={'job_id': job_id, 'action': action, 'channel': canal})
    except Exception:
        pass

    # ── 6. Pre-flight cleanup (skip if resuming — don't delete downloaded clips) ──
    _kill_orphaned_ffmpeg()
    if start_idx == 0:
        _preflight_cleanup(logger)

    # ── 6b. Global heartbeat daemon — pulses every 30s for the entire
    # pipeline lifetime so orphan detection doesn't rely solely on phase-
    # specific heartbeat emitters (TTS, render, upload). If the worker
    # crashes (OOM, segfault), the heartbeat stops and the orphan detector
    # in the API can declare the job dead within 60 min.
    _heartbeat_stop = threading.Event()
    def _global_heartbeat():
        while not _heartbeat_stop.is_set():
            try:
                # Ghost-worker guard: check job still exists in DB.
                # If row was deleted (e.g., channel deletion cascade),
                # self-terminate immediately to stop wasting resources.
                row = db.get_job(job_id)
                if row is None:
                    logger.error(
                        "GHOST WORKER: job %d disappeared from DB during heartbeat — "
                        "self-terminating", job_id
                    )
                    import os as _os
                    _os._exit(1)  # hard exit, bypass finally blocks
                db.update_heartbeat(job_id)
            except Exception:
                pass  # heartbeat is best-effort; never crash the worker
            _heartbeat_stop.wait(30)
    _heartbeat_thread = threading.Thread(target=_global_heartbeat, daemon=True)
    _heartbeat_thread.start()

    # ── 7. Set up progress callback ───────────────────────────
    _last_detail_write = [0.0]  # throttle detail counters to ~1 write/s

    def _progress_to_db(percent: int, phase: str, message: str, **kwargs):
        try:
            db.update_video(video_id, progress=percent, progress_phase=phase)
        except Exception as exc:
            logger.debug("Progress DB write failed (non-fatal): %s", exc)
        # ── Detail counters (upload bytes / scene x/y) — throttled ──
        cur = kwargs.get("current")
        tot = kwargs.get("total")
        if cur is not None or tot is not None:
            try:
                now = time.time()
                if now - _last_detail_write[0] >= 1.0:
                    _last_detail_write[0] = now
                    db.update_video(
                        video_id,
                        progress_current=cur,
                        progress_total=tot,
                    )
            except Exception:
                pass

    # ── 8. Load channel config ─────────────────────────────────
    try:
        from config.config_bridge import get_channel_config
        config = get_channel_config(canal)
    except Exception as exc:
        logger.error("Failed to load config for %s: %s", canal, exc)
        db.update_job(job_id, status="failed", error_msg=f"Config error: {exc}")
        db.update_video(video_id, status="error", progress_phase="error",
                        error_message=f"Config error: {exc}")
        return False

    if test_mode:
        try:
            from config.test_profile import apply_test_profile
            apply_test_profile(config, mode="fast")
            logger.info("Test mode: unified test profile applied")
        except Exception as exc:
            logger.warning("Test profile not available: %s", exc)

    # ── 9. Restore checkpoint variables ───────────────────────
    # These are loaded from checkpoint if resuming, or populated during phases
    script = checkpoint.get("script")
    if script and isinstance(script, dict) and script.get("id"):
        # Re-hydrate script from DB for full data (bloques_json, escenas_json, etc.)
        full = db.get_script(script["id"])
        if full:
            script = full
            logger.info("Script re-hydrated from DB: %d words", 
                        len(script.get("guion", "").split()))
    
    audio_data = checkpoint.get("tts")
    if audio_data and isinstance(audio_data, dict):
        # Verify audio files still exist
        ap = audio_data.get("audio_path", "")
        if ap and not Path(ap).exists():
            logger.warning("TTS audio file missing: %s — will re-generate", ap)
            audio_data = None
    
    media_cp = checkpoint.get("media", {})
    media_assets = None
    if media_cp and isinstance(media_cp, dict):
        assets = media_cp.get("assets", [])
        scene_ranges = media_cp.get("scene_ranges")
        if assets:
            # Strict checkpoint validation: at least 80% of asset files must
            # still exist on disk. If too many were deleted (e.g. by concurrent
            # preflight cleanup in a previous run), forfeit the checkpoint so
            # media is re-fetched from scratch instead of rendering black
            # placeholder segments for missing files.
            existing = sum(1 for a in assets if isinstance(a, dict) and 
                          a.get("path") and Path(str(a["path"])).exists())
            required = max(1, int(len(assets) * 0.8))
            if existing >= required:
                media_assets = assets
                logger.info("Media checkpoint: %d/%d assets on disk (≥ %d required) — accepted",
                           existing, len(assets), required)
            else:
                logger.warning(
                    "Media checkpoint REJECTED: only %d/%d assets on disk "
                    "(need ≥ %d). Files were likely deleted by a concurrent "
                    "cleanup in a prior run. Will re-fetch media from scratch.",
                    existing, len(assets), required,
                )
                media_assets = None  # force re-fetch
    
    video_data = checkpoint.get("video")
    if video_data and isinstance(video_data, dict):
        vp = video_data.get("video_path", "")
        if vp and not Path(vp).exists():
            logger.warning("Video file missing: %s — will re-render", vp)
            video_data = None
    
    metadata = checkpoint.get("metadata")

    # ── 10. Run the pipeline ──────────────────────────────────
    success = False
    error_msg = ""
    orch = None

    try:
        from orchestrator import PipelineOrchestrator

        # ── Detect marathon mode from video record ──
        is_marathon = False
        marathon_config = None
        try:
            video = db.get_video(video_id)
            if video:
                is_marathon = bool(video.get("is_marathon", False))
                if is_marathon:
                    mc_raw = video.get("marathon_config")
                    if mc_raw:
                        import json as _json_w
                        marathon_config = _json_w.loads(mc_raw) if isinstance(mc_raw, str) else mc_raw
                    else:
                        marathon_config = {}
                    logger.info("[MARATHON][%s] Marathon mode active: %s", canal, marathon_config)
        except Exception as exc:
            logger.debug("Could not read marathon config: %s", exc)

        orch = PipelineOrchestrator(
            canal=canal,
            db_video_id=video_id,
            progress_callback=_progress_to_db,
            source_mode=source_mode,
            viral_candidate_id=viral_candidate_id,
            is_marathon=is_marathon,
            marathon_config=marathon_config,
        )
        if test_mode:
            orch.config = config

        pipeline_start = time.time()

        # ═══════════════════════════════════════════════════════
        # Phase 0: Scrape
        # ═══════════════════════════════════════════════════════
        
        # ── Pick a random playlist BEFORE scraping ──
        if _phase_index("scrape") >= start_idx:
            try:
                channel_db_id = orch._get_channel_id()
                playlists = db.get_channel_youtube_playlists(channel_db_id) if channel_db_id else []
                if playlists:
                    import random as _random
                    target_pl = _random.choice(playlists)
                    db.update_video(video_id,
                                    target_playlist_id=target_pl["id"],
                                    target_playlist_slug=target_pl["slug"])
                    logger.info("[%s] Target playlist: '%s' (slug=%s)",
                               canal, target_pl.get("name"), target_pl.get("slug"))
                    # Inject playlist keywords into orchestrator for viral mode
                    config_json = ch.get("config_json", "{}")
                    if isinstance(config_json, str):
                        # 'json' ya está importado a nivel de módulo (línea 25).
                        # El 'import json' local aquí sombreaba el global y causaba
                        # "local variable 'json' referenced before assignment" cuando
                        # el bloque scrape se saltaba (upload_only / resume).
                        config_json = json.loads(config_json)
                    generated = config_json.get("PLAYLISTS_GENERATED", [])
                    for pl_cfg in generated:
                        if pl_cfg.get("slug") == target_pl["slug"]:
                            orch._target_playlist = target_pl
                            orch._target_playlist_kw = pl_cfg.get("keywords_en", [])
                            break
                else:
                    logger.warning("[%s] No playlists in DB — continuing without playlist", canal)
            except Exception as e:
                logger.warning("[%s] Playlist selection failed (non-critical): %s", canal, e)
        
        if _phase_index("scrape") < start_idx:
            logger.info("Skipping scrape (resuming from %s)", start_phase)
        elif test_mode:
            unused = db.get_unused_count(canal)
            if unused > 0:
                logger.info("Test mode: skipping scrape — %d unused items", unused)
                db.update_video(video_id, progress=12, progress_phase="scrape")
                _save_checkpoint(video_id, "scrape", {"items_added": 0, "skipped": True}, db)
            else:
                orch.phase_scrape()
                db.update_video(video_id, progress=12, progress_phase="scrape")
                _save_checkpoint(video_id, "scrape", {"items_added": 0}, db)
        else:
            log_phase_start(db, entity_type='video', entity_id=video_id, phase='scrape', channel_id=channel_id)
            orch.phase_scrape()
            db.update_video(video_id, progress=12, progress_phase="scrape")
            _save_checkpoint(video_id, "scrape", {"items_added": 0}, db)
            log_phase_end(db, entity_type='video', entity_id=video_id, phase='scrape', channel_id=channel_id)

        # ═══════════════════════════════════════════════════════
        # Phase 1: Script
        # ═══════════════════════════════════════════════════════
        if _phase_index("script") < start_idx:
            logger.info("Skipping script (loaded from checkpoint #%d)", script.get("id", 0) if script else 0)
            db.update_video(video_id, progress=25, progress_phase="script",
                            script_id=script.get("id") if script else None)
        else:
            db.update_video(video_id, progress=15, progress_phase="script")
            logger.info("Phase 1/6: Generating script...")
            log_phase_start(db, entity_type='video', entity_id=video_id, phase='script', channel_id=channel_id)

            script = orch.phase_generate_script()
            if not script:
                logger.warning("Script generation failed — retrying after re-scrape...")
                orch.phase_scrape()
                script = orch.phase_generate_script()
            
            if not script:
                error_msg = "No se pudo generar el guion (sin contenido disponible)"
                logger.error(error_msg)
                db.update_job(job_id, status="failed", error_msg=error_msg[:500])
                db.update_video(video_id, status="error", progress_phase="script",
                                error_message=error_msg)
                log_phase_error(db, entity_type='video', entity_id=video_id, phase='script',
                                error=error_msg, channel_id=channel_id)
                return False
            
            db.update_video(video_id, progress=25, progress_phase="script",
                            script_id=script.get("id"))
            _titulo_opts = script.get("titulo_options") or []
            _save_checkpoint(video_id, "script", {
                "id": script.get("id"),
                "titulo": (script.get("titulo_selected") or (_titulo_opts[0] if _titulo_opts else "") or "")[:60],
                "guion": script.get("guion", ""),
                "bloques_json": script.get("bloques_json", []),
                "escenas_json": script.get("escenas_json", []),
                "titulo_options": _titulo_opts,
            }, db)
            log_phase_end(db, entity_type='video', entity_id=video_id, phase='script', channel_id=channel_id)
        
        titulo = (
            script.get("titulo_selected")
            or (script.get("titulo_options") or [None])[0]
            or script.get("titulo")
            or "Sin titulo"
        )[:60] if script else ""
        logger.info("Script: '%s' (%d words)", titulo,
                    len(script.get("guion", "").split()) if script else 0)

        # ═══════════════════════════════════════════════════════
        # Phase 1.5: Pre-validation (early gate)
        # ═══════════════════════════════════════════════════════
        if _phase_index("pre_validate") < start_idx:
            logger.info("Skipping pre-validation (loaded from checkpoint)")
        else:
            db.update_video(video_id, progress=27, progress_phase="pre_validate")
            logger.info("Phase 1.5/7: Pre-validating script quality...")
            log_phase_start(db, entity_type='video', entity_id=video_id, phase='pre_validate', channel_id=channel_id)
            try:
                orch.phase_pre_validate(script)
                db.update_video(video_id, progress=27, progress_phase="pre_validate")
                _save_checkpoint(video_id, "pre_validate", {"passed": True}, db)
                log_phase_end(db, entity_type='video', entity_id=video_id, phase='pre_validate', channel_id=channel_id)
            except RuntimeError as ve:
                error_msg = str(ve)
                logger.error("Pre-validation FAILED: %s", error_msg)
                db.update_job(job_id, status="failed", error_msg=error_msg[:500])
                db.update_video(video_id, status="error", progress_phase="pre_validate",
                                error_message=error_msg)
                log_phase_error(db, entity_type='video', entity_id=video_id, phase='pre_validate',
                                error=error_msg, channel_id=channel_id)
                return False

        # ═══════════════════════════════════════════════════════
        # Phase 2: TTS
        # ═══════════════════════════════════════════════════════
        if _phase_index("tts") < start_idx:
            logger.info("Skipping TTS (loaded from checkpoint)")
            db.update_video(video_id, progress=40, progress_phase="tts")
        else:
            # ── RAM gate before TTS (avoid wasting 5-9 min of compute) ──
            # Kokoro/torch carga ~3 GB: usar umbral TTS más alto que el
            # genérico de render. edge-tts (WebSocket) apenas consume RAM, así
            # que no debe exigírsele el umbral de Kokoro: se reutiliza el umbral
            # de render, que ya cubre el resto de fases pesadas aguas abajo.
            from config.settings import MIN_FREE_FOR_TTS_MB, MIN_FREE_FOR_RENDER_MB
            try:
                _tts_engine = str(getattr(config, "TTS_ENGINE", "kokoro") or "kokoro").lower()
            except Exception:
                _tts_engine = "kokoro"
            _tts_threshold = MIN_FREE_FOR_TTS_MB if _tts_engine == "kokoro" else MIN_FREE_FOR_RENDER_MB
            if not _check_ram_gate(logger, timeout_sec=300, threshold=_tts_threshold):
                db.update_job(job_id, status="failed", error_msg="RAM insuficiente (pre-TTS gate)")
                db.update_video(video_id, status="error", progress_phase="script",
                                error_message="RAM insuficiente (pre-TTS gate)")
                return False

            db.update_video(video_id, progress=30, progress_phase="tts")
            logger.info("Phase 2/6: Generating TTS audio...")
            log_phase_start(db, entity_type='video', entity_id=video_id, phase='tts', channel_id=channel_id)

            audio_data = orch.phase_tts(script, job_id=job_id)
            if not audio_data:
                error_msg = "Fallo la generacion de voz (TTS)"
                logger.error(error_msg)
                db.update_job(job_id, status="failed", error_msg=error_msg[:500])
                db.update_video(video_id, status="error", progress_phase="tts",
                                error_message=error_msg)
                log_phase_error(db, entity_type='video', entity_id=video_id, phase='tts',
                                error=error_msg, channel_id=channel_id)
                return False
            
            db.update_video(video_id, progress=40, progress_phase="tts")
            _save_checkpoint(video_id, "tts", {
                "audio_path": audio_data.get("audio_path", ""),
                "timestamps_path": audio_data.get("timestamps_path", ""),
                "cta_audio_path": audio_data.get("cta_audio_path", ""),
            }, db)
            log_phase_end(db, entity_type='video', entity_id=video_id, phase='tts', channel_id=channel_id)
        
        audio_dur = 0
        if audio_data and isinstance(audio_data, dict) and audio_data.get("timestamps"):
            ts = audio_data["timestamps"]
            if ts and isinstance(ts[-1], dict):
                audio_dur = int(ts[-1].get("end_ms", 0) / 1000)
        logger.info("TTS: %ds audio", audio_dur)

        # ── RAM gate before heavy phases (media + video assembly) ──
        if not _check_ram_gate(logger):
            error_msg = "RAM insuficiente tras timeout (pre-render gate)"
            db.update_job(job_id, status="failed", error_msg=error_msg)
            db.update_video(video_id, status="error", progress_phase="tts",
                            error_message=error_msg)
            return False

        # ═══════════════════════════════════════════════════════
        # Phase 3: Media
        # ═══════════════════════════════════════════════════════
        if _phase_index("media") < start_idx:
            logger.info("Skipping media (loaded from checkpoint, %d assets)", 
                       len(media_assets) if media_assets else 0)
            db.update_video(video_id, progress=55, progress_phase="media")
        else:
            db.update_video(video_id, progress=45, progress_phase="media")
            logger.info("Phase 3/6: Fetching media assets...")
            log_phase_start(db, entity_type='video', entity_id=video_id, phase='media', channel_id=channel_id)

            media_assets = orch.phase_media(script, audio_data, job_id=job_id)
            if not media_assets:
                if media_assets is None:
                    # Exception/timeout inside orchestrator (real cause already
                    # logged by orchestrator.phase_media → see worker log)
                    error_msg = "Media fetch failed: error de proveedores o timeout — revisar log del worker para causa raíz"
                else:
                    error_msg = "No se encontraron imagenes ni videos"
                logger.error(error_msg)
                db.update_job(job_id, status="failed", error_msg=error_msg[:500])
                db.update_video(video_id, status="error", progress_phase="media",
                                error_message=error_msg)
                log_phase_error(db, entity_type='video', entity_id=video_id, phase='media',
                                error=error_msg, channel_id=channel_id)
                return False
            
            db.update_video(video_id, progress=55, progress_phase="media")
            _save_checkpoint(video_id, "media", {
                "assets": [{"type": a.get("type", "?"), "path": str(a.get("path", "")),
                            "source": a.get("source", "?")}
                           for a in (media_assets if isinstance(media_assets, list) else [])],
            }, db)
            log_phase_end(db, entity_type='video', entity_id=video_id, phase='media', channel_id=channel_id)
        
        n_video = sum(1 for a in (media_assets or []) if isinstance(a, dict) and a.get("type") == "video")
        n_image = sum(1 for a in (media_assets or []) if isinstance(a, dict) and a.get("type") == "image")
        logger.info("Media: %d video + %d image assets", n_video, n_image)

        # ═══════════════════════════════════════════════════════
        # Phase 4: Video Assembly
        # ═══════════════════════════════════════════════════════
        if _phase_index("video") < start_idx:
            logger.info("Skipping video (loaded from checkpoint: %s)", 
                       video_data.get("video_path", "?") if video_data else "?")
            db.update_video(video_id, progress=75, progress_phase="video")
            db.update_job(job_id, pipeline_phase="post")
        else:
            # ── Phase pipelining: acquire exclusive render slot ──
            # Wait until no other job is in the render phase. This allows
            # another worker to do prep phases (scrape→media) while we wait.
            if not _acquire_render_slot(job_id, db, timeout_sec=7200):
                error_msg = "Render slot timeout — otro render activo >2h"
                db.update_job(job_id, status="failed", error_msg=error_msg)
                db.update_video(video_id, status="error", progress_phase="video",
                                error_message=error_msg)
                return False
            
            db.update_video(video_id, progress=60, progress_phase="video")
            logger.info("Phase 4/7: Assembling video...")
            log_phase_start(db, entity_type='video', entity_id=video_id, phase='video', channel_id=channel_id)

            # ── Memory guard: wait if system is critically low on RAM ──
            # ffmpeg xfade concat of 200+ segments with crossfades consumes
            # GBs of memory. If we're already near OOM, the concat will crash
            # or trigger the kernel OOM killer, killing other processes.
            from config.settings import MIN_FREE_FOR_ASSEMBLY_MB
            if not _check_memory_before_video(
                logger, min_free_gb=MIN_FREE_FOR_ASSEMBLY_MB / 1024.0
            ):
                error_msg = "RAM insuficiente para ensamblaje de video — se aborta para prevenir OOM kill"
                logger.error(error_msg)
                db.update_job(job_id, status="failed", error_msg=error_msg, phase="video")
                db.update_video(video_id, status="error", progress_phase="video",
                                error_message=error_msg)
                log_phase_error(db, entity_type='video', entity_id=video_id, phase='video',
                                error=error_msg, channel_id=channel_id)
                return False
            
            # ── CRITICAL SECTION: block SIGTERM during video assembly ──
            # ffmpeg xfade concat of 200+ segments can take 15+ minutes.
            # A SIGTERM during this phase corrupts the render and causes
            # permanent bug_crash after 3 auto-recovery retries.
            _in_critical_phase.set()
            try:
                video_data = orch.phase_video(script, audio_data, 
                                              media_assets if isinstance(media_assets, list) else [],
                                              job_id=job_id)
            finally:
                _in_critical_phase.clear()
                # If shutdown was requested during assembly, exit now
                if _shutdown_requested.is_set():
                    logger.warning("Shutdown deferred during video assembly — exiting gracefully")
                    _kill_orphaned_ffmpeg()
                    sys.exit(0)
            if not video_data:
                # ── Diagnóstico: recolectar métricas del sistema ──
                diag = []
                try:
                    import shutil
                    disk = shutil.disk_usage("/root/autotube/output")
                    diag.append(f"disk free={disk.free / (1024**3):.1f}GB")
                except Exception:
                    pass
                try:
                    import psutil
                    mem = psutil.virtual_memory()
                    diag.append(f"RAM avail={mem.available / (1024**3):.1f}GB used={mem.percent}%")
                except ImportError:
                    pass
                diag_str = ", ".join(diag) if diag else ""
                error_msg = (
                    f"Fallo el ensamblaje del video. "
                    f"({diag_str})" if diag_str else "Fallo el ensamblaje del video"
                )
                logger.error(error_msg)
                db.update_job(job_id, status="failed", error_msg=error_msg[:500])
                db.update_video(video_id, status="error", progress_phase="video",
                                error_message=error_msg[:500])
                log_phase_error(db, entity_type='video', entity_id=video_id, phase='video',
                                error=error_msg, channel_id=channel_id)
                return False
            
            db.update_video(video_id, progress=75, progress_phase="video", status="ready")
            db.update_job(job_id, pipeline_phase="post")  # render done, now metadata+upload
            _save_checkpoint(video_id, "video", {
                "video_path": str(video_data.get("video_path", "")),
                "thumbnail_path": str(video_data.get("thumbnail_path", "")),
                "titulo": str(video_data.get("titulo", "")),
            }, db)
            log_phase_end(db, entity_type='video', entity_id=video_id, phase='video', channel_id=channel_id)
        
        logger.info("Video: %s", video_data.get("video_path", "?") if video_data else "?")

        # ═══════════════════════════════════════════════════════
        # Phase 5: Metadata
        # ═══════════════════════════════════════════════════════
        if _phase_index("metadata") < start_idx:
            logger.info("Skipping metadata (loaded from checkpoint)")
        else:
            db.update_video(video_id, progress=78, progress_phase="metadata")
            logger.info("Phase 5/7: Generating SEO metadata...")
            log_phase_start(db, entity_type='video', entity_id=video_id, phase='metadata', channel_id=channel_id)

            try:
                metadata = orch.phase_metadata(script, video_data)
            except Exception as meta_exc:
                logger.warning("Metadata generation failed (non-fatal): %s", meta_exc)
                _alert_nonfatal(db, video_id, channel_id, "metadata", meta_exc)
                metadata = None
            
            if metadata and isinstance(metadata, dict):
                # ── Marathon title validation ──
                if is_marathon and getattr(config, "MARATHON_VALIDATE_TITLE", True):
                    try:
                        from pipeline.marathon_title_validator import validate_marathon_title
                        import random as _rnd

                        # Get the marathon topic from script content
                        topic = (
                            script.get("titulo", "")
                            or (marathon_config.get("narrative_format", "Documental") if marathon_config else "Documental")
                            or "Documental"
                        )
                        # Prefer a more specific topic from the script's keywords or content
                        if script.get("keywords"):
                            import json as _jkw
                            kws = _jkw.loads(script["keywords"]) if isinstance(script.get("keywords"), str) else script.get("keywords", [])
                            if kws and isinstance(kws, list) and len(kws) > 0:
                                topic = kws[0] if not isinstance(kws[0], dict) else kws[0].get("keyword", topic)

                        content_summary = script.get("guion", "")[:2000] if script.get("guion") else ""

                        formulas = list(getattr(config, "MARATHON_TITLE_FORMULAS", ["{topic}: Documental Completo"]))
                        hook_types = list(getattr(config, "MARATHON_HOOK_TYPES", ["revelacion_impactante"]))

                        title_formula = _rnd.choice(formulas)
                        hook_type = _rnd.choice(hook_types)
                        raw_title = title_formula.replace("{topic}", topic)

                        logger.info(
                            "[MARATHON][%s] Validating marathon title: '%s' (hook=%s, topic=%s)",
                            canal, raw_title[:80], hook_type, topic[:40],
                        )

                        best_title = raw_title
                        best_score = 0.0

                        for attempt in range(3):
                            result = validate_marathon_title(
                                title=raw_title,
                                topic=topic,
                                content_summary=content_summary,
                                hook_type=hook_type,
                            )
                            score = result["final_score"]
                            if score > best_score:
                                best_score = score
                                best_title = raw_title

                            if result["approved"]:
                                logger.info(
                                    "[MARATHON][%s] Marathon title APPROVED on attempt %d: '%s' (score=%.2f, "
                                    "curiosity=%d, precision=%d, power=%d)",
                                    canal, attempt + 1, raw_title[:80], score,
                                    result["curiosity_score"], result["precision_score"],
                                    result["power_score"],
                                )
                                break
                            else:
                                logger.warning(
                                    "[MARATHON][%s] Marathon title REJECTED (attempt %d): '%s' — %s",
                                    canal, attempt + 1, raw_title[:80], result["feedback"],
                                )
                                # Use LLM-suggested alternative for next attempt
                                alts = result.get("alternative_titles", [])
                                if alts and alts[0] != raw_title:
                                    raw_title = alts[0]
                                    best_title = raw_title  # track the alternative
                                else:
                                    # If no good alt, try a different formula
                                    raw_title = _rnd.choice(formulas).replace("{topic}", topic)
                        else:
                            # All 3 attempts failed
                            logger.error(
                                "[MARATHON][%s] Marathon title FAILED validation 3 times — "
                                "using best attempt (score=%.2f): '%s'",
                                canal, best_score, best_title[:80],
                            )

                        # Override the AI-generated title with our validated marathon title
                        metadata["selected_title"] = best_title
                        logger.info(
                            "[MARATHON][%s] Final marathon title: '%s'",
                            canal, best_title[:80],
                        )

                    except Exception as tv_exc:
                        logger.error(
                            "[MARATHON][%s] Title validation crashed: %s — keeping original title",
                            canal, tv_exc,
                        )
                        _alert_nonfatal(
                            db, video_id, channel_id, "marathon_title_validation",
                            tv_exc, {"is_marathon": True},
                        )
                        # Keep the title that phase_metadata() already generated

                db.update_video(video_id, progress=85, progress_phase="metadata")
                db.update_video(
                    video_id,
                    titulo_final=metadata.get("selected_title", video_data.get("titulo", "")),
                    description=metadata.get("description", ""),
                    tags_json=json.dumps(metadata.get("tags", []), ensure_ascii=False),
                    title_options=json.dumps(metadata.get("titles", []), ensure_ascii=False),
                    thumbnail_path=video_data.get("thumbnail_path", ""),
                    status="ready",
                )
                _save_checkpoint(video_id, "metadata", {
                    "selected_title": metadata.get("selected_title", ""),
                    "description": metadata.get("description", ""),
                }, db)

                # ── Antiban (ago 2026): título casi duplicado con contenido reciente ──
                try:
                    from api.services.shorts_scheduler import warn_if_title_similar
                    warn_if_title_similar(
                        channel_id, canal, video_id,
                        metadata.get("selected_title", "") or "",
                        db=db,
                    )
                except Exception:
                    pass
            else:
                # Save basic info even without metadata
                db.update_video(
                    video_id,
                    titulo_final=video_data.get("titulo", "") if video_data else "",
                    thumbnail_path=video_data.get("thumbnail_path", "") if video_data else "",
                    status="ready",
                )

            log_phase_end(db, entity_type='video', entity_id=video_id, phase='metadata', channel_id=channel_id)

        # Save scenes (from media checkpoint or freshly generated)
        if media_assets and video_data:
            try:
                if _phase_index("scenes") < start_idx or True:  # always try to save scenes
                    escenas_raw = script.get("escenas") or script.get("escenas_json", "[]")
                    escenas = json.loads(escenas_raw) if isinstance(escenas_raw, str) else (escenas_raw or [])
                    
                    if video_data.get("video_path") and Path(video_data["video_path"]).exists():
                        from moviepy import VideoFileClip
                        clip = VideoFileClip(video_data["video_path"])
                        dur = clip.duration
                        total_ms = int((dur() if callable(dur) else dur) * 1000)
                        clip.close()
                    else:
                        total_ms = audio_dur * 1000 or 60000
                    
                    assets_list = media_assets if isinstance(media_assets, list) else []
                    scenes_data = []
                    for i, escena in enumerate(escenas):
                        asset = assets_list[i] if i < len(assets_list) else None
                        img = str(asset.get("path", "")) if isinstance(asset, dict) else ""
                        scenes_data.append({
                            "description": escena if isinstance(escena, str)
                            else escena.get("descripcion", str(escena)),
                            "script_text": "",
                            "image_path": img,
                            "duration_ms": total_ms // max(len(escenas), 1),
                        })
                    if scenes_data:
                        db.insert_scenes_batch(video_id, scenes_data)
            except Exception as exc:
                logger.warning("Scene saving failed (non-fatal): %s", exc)
                _alert_nonfatal(db, video_id, channel_id, "scenes", exc)

        # ═══════════════════════════════════════════════════════
        # Phase 5.5: Post-validation (quality gate before upload)
        # ═══════════════════════════════════════════════════════
        if _phase_index("post_validate") < start_idx:
            logger.info("Skipping post-validation (loaded from checkpoint)")
        else:
            db.update_video(video_id, progress=87, progress_phase="post_validate")
            logger.info("Phase 5.5/7: Post-validating video & metadata quality...")
            log_phase_start(db, entity_type='video', entity_id=video_id, phase='post_validate', channel_id=channel_id)
            try:
                val_result = orch.phase_post_validate(video_data, metadata, script)
                # Auto-fixes may have updated metadata — sync to DB
                if val_result.auto_fixes_applied:
                    logger.info(
                        "Post-validate auto-fixes: %s",
                        ", ".join(val_result.auto_fixes_applied),
                    )
                    try:
                        db.update_video(
                            video_id,
                            titulo_final=val_result.title,
                            description=val_result.description,
                            tags_json=json.dumps(val_result.tags, ensure_ascii=False),
                        )
                    except Exception as _sync_exc:
                        logger.warning("Failed to sync auto-fixed metadata to DB: %s", _sync_exc)
                if val_result.warnings:
                    logger.warning(
                        "Post-validate warnings: %s",
                        "; ".join(val_result.warnings),
                    )
                db.update_video(video_id, progress=87, progress_phase="post_validate")
                _save_checkpoint(video_id, "post_validate", {"passed": True}, db)
                log_phase_end(db, entity_type='video', entity_id=video_id, phase='post_validate', channel_id=channel_id)
            except RuntimeError as ve:
                error_msg = str(ve)
                logger.error("Post-validation FAILED: %s", error_msg)
                db.update_job(job_id, status="failed", error_msg=error_msg[:500])
                db.update_video(video_id, status="validation_failed", progress_phase="post_validate")
                log_phase_error(db, entity_type='video', entity_id=video_id, phase='post_validate',
                                error=error_msg, channel_id=channel_id)
                return False

        # ═══════════════════════════════════════════════════════
        # Phase 5.8: A/B Testing — generate thumbnail variants (if enabled)
        # ═══════════════════════════════════════════════════════
        ab_test_variant_paths = []
        try:
            from config.settings import ENABLE_AB_TESTING
        except ImportError:
            ENABLE_AB_TESTING = False
        
        skip_upload = not upload or test_mode or action == "generate_only"

        # Fase 1.3: upload_only NO genera variantes A/B (la miniatura ya existe en F1).
        # Antes el upload in-process (start_upload_job_from_scheduler) tampoco lo hacía —
        # evitamos consumir créditos Pollo/LLM en cada subida F2.
        if ENABLE_AB_TESTING and not skip_upload and action not in ("generate_only", "upload_only"):
            db.update_video(video_id, progress=88, progress_phase="ab_test_thumbnails")
            logger.info("Phase 5.8/7: Generating A/B thumbnail variants...")
            
            try:
                from pipeline.thumbnail_maker import ThumbnailMaker
                from pipeline.thumbnail_brainstorm import ThumbnailBrainstorm
                
                title_for_thumb = (
                    metadata.get("selected_title") if metadata else
                    (video_data.get("titulo", titulo) if video_data else titulo)
                )[:100]
                script_text = script.get("guion", "") if script else ""
                keywords = metadata.get("tags", []) if metadata else []
                
                channel_display = getattr(config, "CANAL_DISPLAY_NAME", canal)
                channel_desc = getattr(config, "CHANNEL_ABOUT_SECTION", "")
                channel_theme = getattr(config, "CANAL_NARRATIVE_STYLE", "")

                # Reuse video scene images for the inset recuadro of each variant
                _scene_images = []
                for a in (media_assets if isinstance(media_assets, list) else []):
                    if isinstance(a, dict) and a.get("type") == "image" and a.get("path"):
                        _scene_images.append([a["path"]])

                maker = ThumbnailMaker(config=config)
                
                ab_test_variant_paths = maker.make_variant_thumbnails(
                    title=title_for_thumb,
                    script_text=script_text,
                    keywords=keywords,
                    canal_slug=canal,
                    channel_display_name=channel_display,
                    channel_description=channel_desc,
                    channel_theme=channel_theme,
                    video_id=video_id,
                    num_variants=3,
                    scene_images=_scene_images or None,
                )
                
                if ab_test_variant_paths:
                    # Update thumbnail_path to the first variant (will be uploaded)
                    video_data["thumbnail_path"] = str(ab_test_variant_paths[0])
                    db.update_video(
                        video_id,
                        thumbnail_path=str(ab_test_variant_paths[0]),
                    )
                    logger.info(
                        "[AB] Generated %d thumbnail variants for video %s",
                        len(ab_test_variant_paths), video_id,
                    )
            except Exception as ab_exc:
                logger.warning("[AB] Thumbnail variant generation failed (non-fatal): %s", ab_exc)
                _alert_nonfatal(db, video_id, channel_id, "ab_thumbnails", ab_exc)
                ab_test_variant_paths = []

        # ═══════════════════════════════════════════════════════
        # Phase 6: Upload
        # ═══════════════════════════════════════════════════════
        if skip_upload:
            skip_reason = "Test mode" if test_mode else ("Phase 1 only (generate_only)" if action == "generate_only" else "Upload disabled")
            logger.info("Phase 7/7: %s — skipping upload (video stays local)", skip_reason)
            # ── generate_only: mark as awaiting_upload for later upload dispatch ──
            gen_status = "awaiting_upload" if action == "generate_only" else "ready"
            # ── Seed scheduled_upload_at from planned slot's target_upload_at ──
            seed_upload_at = None
            stale_public_at = False
            recalculated_target = None
            if action == "generate_only":
                try:
                    slot = db.get_planned_slot_for_video(video_id)
                    if slot and slot.get("target_upload_at"):
                        raw_seed = str(slot["target_upload_at"])
                        # ── v (Aug 2026): clamp far-future / past seed ──
                        # The "pipeline continua" replan can set target_upload_at days
                        # ahead while generation finishes now. Seeding that far-future
                        # value makes the video sit in awaiting_upload for days. If the
                        # slot's upload target is >12h away (or in the past), leave
                        # scheduled_upload_at NULL so the upload scheduler computes a
                        # fresh time in the next upload window.
                        seed_upload_at = _clamp_seed_upload_at(raw_seed, canal)
                    # Check if target_public_at is stale using proper timezone-aware comparison
                    vr = db.get_video(video_id)
                    tpa = vr.get("target_public_at") if vr else None
                    if tpa:
                        from pipeline.publish_scheduler import (
                            _target_is_stale, ensure_future_target_public_at,
                        )
                        ch_cfg = db.get_channel(channel_id)
                        tz_str = "Europe/Madrid"
                        if ch_cfg and ch_cfg.get("config_json"):
                            try:
                                import json as _json_inner
                                cfg = _json_inner.loads(ch_cfg["config_json"])
                                tz_str = cfg.get("PUBLISH_TIMEZONE", "Europe/Madrid")
                            except Exception:
                                pass

                        if _target_is_stale(tpa, timezone_str=tz_str, warmup_min=60):
                            stale_public_at = True
                            # Recalculate instead of nullifying
                            try:
                                recalculated_target = ensure_future_target_public_at(
                                    tpa, slug=canal, timezone_str=tz_str,
                                    db=db, channel_id=channel_id,
                                    warmup_min=60, jitter_min=0,
                                )
                                logger.info(
                                    "[%s] Recalculated stale target_public_at: %s → %s",
                                    canal, str(tpa)[:19], recalculated_target,
                                )
                            except Exception as recalc_exc:
                                logger.warning("[%s] Recalc failed, will nullify: %s", canal, recalc_exc)
                                recalculated_target = None
                except Exception as e:
                    logger.debug("[%s] Could not seed scheduled_upload_at: %s", canal, e)
            update_kwargs = dict(progress=100, status=gen_status,
                                 generation_finished_at=db_now(),
                                 scheduled_upload_at=seed_upload_at)
            if stale_public_at:
                update_kwargs["target_public_at"] = recalculated_target  # new UTC target (or None if recalc failed)
            db.update_video(video_id, **update_kwargs)
            logger.info("Video %d status: %s (mp4 preserved for later upload)", video_id, gen_status)

            # ── v26: Pre-render clip shorts during F1 (generate_only) ──
            # Generate clip shorts from the source MP4 BEFORE cleanup, so they
            # go directly to "Pendiente subida" with scheduled upload times.
            # Flag anti-bucle (antiban, ago 2026): True si el upload falló por
            # condición transient (cap diario / cuota) que se difiere al
            # siguiente día PT. Hace que el job se marque 'failed' (cuenta en
            # el presupuesto de reintentos) y que el scheduler no re-despache
            # el vídeo cada minuto.
            _upload_retryable_fail = False
            if action == "generate_only":
                vp = video_data.get("video_path", "") if video_data else ""
                if vp and Path(vp).exists():
                    try:
                        from api.services.shorts_scheduler import pre_render_clip_shorts_for_video
                        pre_render_clip_shorts_for_video(
                            video_id=video_id,
                            channel_id=channel_id,
                            channel_slug=canal,
                            video_path=vp,
                            script_id=video_data.get("script_id") if video_data else None,
                        )
                    except Exception as _pre_render_err:
                        logger.warning(
                            "[%s] Pre-render clip shorts during F1 failed (non-fatal): %s",
                            canal, _pre_render_err,
                        )
                        _alert_nonfatal(
                            db, video_id, channel_id, "pre_render_clips",
                            _pre_render_err, {"stage": "generate_only"},
                        )
        else:
            db.update_video(video_id, progress=90, progress_phase="upload")
            logger.info("Phase 7/7: Uploading to YouTube...")
            
            # ── Log lifecycle: upload started ──
            try:
                log_lifecycle(db, entity_type='video', entity_id=video_id, channel_id=channel_id,
                              event='upload_started', phase='upload', status='started',
                              message='Uploading to YouTube',
                              metadata={'publish_mode': video_record.get('publish_mode') if video_record else 'immediate'})
            except Exception:
                pass
            
            # ── Read planned target from the video record (set by planning) ──
            planned_public_at = None
            video_record = db.get_video(video_id)
            if video_record and video_record.get("publish_mode") == "scheduled":
                planned_public_at = video_record.get("target_public_at")
                if planned_public_at:
                    # ── Staleness guard: recalculate before upload if target passed ──
                    try:
                        from pipeline.publish_scheduler import (
                            _target_is_stale, ensure_future_target_public_at,
                            clamp_max_ahead_target_public_at,
                        )
                        ch_cfg2 = db.get_channel(channel_id)
                        tz_str2 = "Europe/Madrid"
                        warmup2 = 60
                        if ch_cfg2 and ch_cfg2.get("config_json"):
                            try:
                                cfg2 = json.loads(ch_cfg2["config_json"])
                                tz_str2 = cfg2.get("PUBLISH_TIMEZONE", "Europe/Madrid")
                                warmup2 = int(cfg2.get("PUBLISH_WARMUP_MIN", 60) or 60)
                            except Exception:
                                pass
                        if _target_is_stale(planned_public_at, timezone_str=tz_str2, warmup_min=warmup2):
                            logger.info("[%s] target_public_at is stale before upload — recalculating", canal)
                            planned_public_at = ensure_future_target_public_at(
                                planned_public_at, slug=canal, timezone_str=tz_str2,
                                db=db, channel_id=channel_id,
                                warmup_min=warmup2,
                            )
                            # Persist recalculation immediately
                            db.update_video(video_id, target_public_at=planned_public_at)
                        # ── Clamp far-future SIEMPRE: nunca subir con publishAt >24h ──
                        # (idempotente si ya está dentro del margen; recorta si la
                        # resolución de colisiones empujó más allá del cap)
                        planned_public_at = clamp_max_ahead_target_public_at(
                            planned_public_at, slug=canal, timezone_str=tz_str2,
                            warmup_min=warmup2, db=db, channel_id=channel_id,
                        )
                        if planned_public_at != video_record.get("target_public_at"):
                            db.update_video(video_id, target_public_at=planned_public_at)
                    except Exception as stale_exc:
                        logger.debug("[%s] Pre-upload stale check skipped: %s", canal, stale_exc)
                    logger.info("📅 Publicación programada para: %s UTC", planned_public_at)
            
            yt_video_id = orch.phase_upload(script, video_data, metadata, job_id=job_id,
                                              planned_public_at=planned_public_at,
                                              skip_lifecycle_scheduling=True)
            if yt_video_id:
                yt_url = f"https://youtube.com/watch?v={yt_video_id}"
                # Determine correct upload status based on publish mode
                pub_mode = video_record.get("publish_mode", "immediate") if video_record else "immediate"
                worker_status = "uploaded_private" if pub_mode == "scheduled" else "uploaded"

                # ── v24 (Aug 2026): Persist yt_video_id IMMEDIATELY ──
                # The DB update must succeed before any post-upload operations
                # (cross-platform, lifecycle, etc.) that could crash and cause
                # the yt_video_id to be lost, triggering a duplicate re-upload.
                logger.info("[%s] Persisting yt_video_id=%s for video #%d (status=%s)",
                             canal, yt_video_id, video_id, worker_status)
                try:
                    db.mark_video_uploaded(video_id, yt_video_id, yt_url, status=worker_status)
                    db.update_video(video_id, progress=100, status=worker_status)
                    logger.info("[%s] yt_video_id persisted successfully for video #%d", canal, video_id)
                except Exception as persist_err:
                    logger.critical(
                        "[%s] CRITICAL: Failed to persist yt_video_id for video #%d: %s. "
                        "Re-raising to prevent silent duplicate upload.",
                        canal, video_id, persist_err,
                    )
                    raise  # Fatal — better to crash than lose the YouTube ID

                # ── A/B Testing: create initial test record ──────────
                if ab_test_variant_paths:
                    try:
                        title_v1 = (
                            metadata.get("selected_title") if metadata else
                            (video_data.get("titulo", titulo) if video_data else titulo)
                        )
                        with db._connect() as conn:
                            conn.execute("""
                                INSERT INTO video_ab_tests
                                (video_id, yt_video_id, channel_id, phase, title_v1,
                                 thumbnail_variant_paths, thumbnail_variant_active)
                                VALUES (?, ?, ?, 'pending', ?, ?, 1)
                            """, (
                                video_id, yt_video_id, channel_id,
                                (title_v1 or "")[:100],
                                json.dumps([str(p) for p in ab_test_variant_paths]),
                            ))
                            conn.commit()
                        logger.info("[AB] A/B test record created for video %s (3 thumbnail variants)", video_id)
                    except Exception as ab_record_exc:
                        logger.warning("[AB] Failed to create A/B test record: %s", ab_record_exc)

                # ── Cross-platform video publishing (Facebook, Rumble, TikTok) ──
                # Uploads the same video file to other monetizable platforms.
                # Each platform is independent — failures don't affect others.
                try:
                    from api.services.publishers.platform_manager import PlatformPublishManager
                    cross_mgr = PlatformPublishManager(canal, channel_id, db)
                    import asyncio as _asyncio
                    try:
                        _loop = _asyncio.get_event_loop()
                    except RuntimeError:
                        _loop = _asyncio.new_event_loop()
                        _asyncio.set_event_loop(_loop)
                    cross_results = _loop.run_until_complete(
                        cross_mgr.publish_to_all(
                            video_id=video_id,
                            yt_video_id=yt_video_id,
                            video_data=video_data,
                            metadata={
                                "title": metadata.get("title") if metadata else titulo,
                                "description": metadata.get("description", ""),
                                "tags": metadata.get("tags", []),
                                "thumbnail_path": metadata.get("thumbnail_path"),
                            },
                        )
                    )
                    for platform, result in cross_results.items():
                        if result.success:
                            logger.info("[%s] Published to %s: %s", canal, platform,
                                        result.platform_video_url or result.platform_video_id or "ok")
                        else:
                            logger.warning("[%s] Failed to publish to %s: %s", canal,
                                           platform, result.error)
                except Exception as cross_exc:
                    logger.warning("[%s] Cross-platform publishing skipped: %s", canal, cross_exc)
                    _alert_nonfatal(
                        db, video_id, channel_id, "cross_platform",
                        cross_exc, {"yt_video_id": yt_video_id},
                    )

                # ── Auto-mark altered content (IA) via browser ──
                try:
                    if getattr(config, "AUTO_MARK_ALTERED_CONTENT", False):
                        from pipeline.youtube_browser import get_browser, get_account_for_channel
                        account = get_account_for_channel(canal)
                        if account:
                            import threading as _thr
                            _thr.Thread(
                                target=_auto_mark_ia_worker,
                                args=(yt_video_id, canal, account, video_id),
                                daemon=True
                            ).start()
                except Exception as e:
                    logger.warning("[%s] Failed to trigger auto-mark IA: %s", canal, e)
                    _alert_nonfatal(
                        db, video_id, channel_id, "auto_mark_ia",
                        e, {"yt_video_id": yt_video_id},
                    )

                if pub_mode == "scheduled":
                    tp = video_record.get("target_public_at", "?") if video_record else "?"
                    logger.info("📤 SUBIDO (private + publishAt): %s | se hará público: %s UTC", yt_url, tp)
                    # ── Schedule lifecycle actions (go_public, playlist, comments) ──
                    try:
                        from pipeline.video_lifecycle import VideoLifecycleManager
                        script_text = script.get("guion", "") if script else ""
                        lifecycle = VideoLifecycleManager(canal)
                        lifecycle.on_video_uploaded_scheduled(
                            db_video_id=video_id,
                            yt_video_id=yt_video_id,
                            channel_id=channel_id,
                            script_text=script_text,
                            target_public_at=planned_public_at,
                        )
                        logger.info("[%s] Lifecycle actions scheduled for video %s (target: %s)",
                                     canal, yt_video_id, planned_public_at)
                    except Exception as lc_exc:
                        logger.warning("[%s] Failed to schedule lifecycle actions: %s", canal, lc_exc)
                        _alert_nonfatal(
                            db, video_id, channel_id, "lifecycle_schedule",
                            lc_exc, {"yt_video_id": yt_video_id},
                        )
                else:
                    logger.info("📤 PUBLICADO: %s", yt_url)

                # ── v25: Pre-render clip shorts BEFORE deleting the source MP4 ──
                # This avoids expensive yt-dlp re-downloads later. The source MP4
                # is still on disk, so we can extract all clip shorts in one pass
                # (1 LLM call for all clips). Pre-rendered clips are saved as
                # shorts.status='ready' and uploaded later at their scheduled times.
                vp = video_data.get("video_path", "") if video_data else ""
                if vp and Path(vp).exists():
                    try:
                        from api.services.shorts_scheduler import pre_render_clip_shorts_for_video
                        pre_render_clip_shorts_for_video(
                            video_id=video_id,
                            channel_id=channel_id,
                            channel_slug=canal,
                            video_path=vp,
                            script_id=video_data.get("script_id") if video_data else None,
                        )
                    except Exception as _pre_render_err:
                        logger.warning(
                            "[%s] Pre-render clip shorts failed (non-fatal): %s",
                            canal, _pre_render_err,
                        )
                        _alert_nonfatal(
                            db, video_id, channel_id, "pre_render_clips",
                            _pre_render_err, {"stage": "post_upload"},
                        )

                if vp and Path(vp).exists():
                    try:
                        Path(vp).unlink()
                        db.update_video(video_id, video_path="")
                        logger.info("Deleted local mp4: %s", vp)
                    except Exception:
                        pass

                # ── Clean up residual files (v9): audio + scene assets ──
                try:
                    from pipeline.cleanup_utils import cleanup_video_residuals
                    cleanup_video_residuals(db, video_id, audio_data=audio_data, log=logger)
                except Exception:
                    pass
                
                # ── Post-upload: real YouTube stats snapshot ──
                try:
                    from pipeline.youtube_stats import YouTubeStatsFetcher
                    fetcher = YouTubeStatsFetcher(canal)
                    if fetcher.authenticate():
                        real_stats = fetcher.get_video_stats(yt_video_id)
                        if real_stats and not real_stats.get("is_mock"):
                            db.insert_video_stats(
                                video_id=video_id,
                                yt_video_id=yt_video_id,
                                stats=real_stats,
                            )
                            logger.info("[%s] Real stats collected for video %s: %s views", canal, yt_video_id, real_stats.get('viewCount', '?'))
                        else:
                            db.insert_video_stats(video_id=video_id, yt_video_id=yt_video_id,
                                                 stats={"viewCount": 0, "likeCount": 0, "commentCount": 0})
                            logger.info("[%s] Baseline stats saved (API returned mock/no data)", canal)
                    else:
                        db.insert_video_stats(video_id=video_id, yt_video_id=yt_video_id,
                                             stats={"viewCount": 0, "likeCount": 0, "commentCount": 0})
                        logger.warning("[%s] Auth failed for stats fetch, saved baseline", canal)
                except Exception as stats_exc:
                    logger.warning("[%s] Failed to collect post-upload stats: %s", canal, stats_exc)
                    _alert_nonfatal(
                        db, video_id, channel_id, "post_upload_stats",
                        stats_exc, {"yt_video_id": yt_video_id},
                    )
                    try:
                        db.insert_video_stats(video_id=video_id, yt_video_id=yt_video_id,
                                             stats={"viewCount": 0, "likeCount": 0, "commentCount": 0})
                    except Exception:
                        pass
            else:
                logger.error("Upload failed — video saved locally")
                # ── Quota exhaustion check: keep in awaiting_upload, don't mark as ready ──
                # Per-project breaker: solo se retiene si el proyecto del CANAL
                # está agotado (Fase cuota ago 2026). También se retiene (sin
                # breaker) si el dispatcher local denegó la admisión por una
                # razón no-quota (colisión de reference, presupuesto, proyecto
                # desconocido) — reintentable, NO es cuota agotada.
                try:
                    admission_denied = bool(
                        getattr(orch, "_upload_admission_denied", False)
                    )
                    if db.is_quota_exhausted_for_channel(canal):
                        from api.services.yt_retry_guard import next_pt_day_retry_str
                        retry_at = next_pt_day_retry_str()
                        db.update_video(video_id, progress=0, status="awaiting_upload",
                                        error_message="YouTube API quota exhausted (reintento mañana PT)",
                                        progress_phase="upload", scheduled_upload_at=retry_at)
                        logger.info(
                            "[%s] Upload failed (quota) — diferido a %s (no bucle)",
                            canal, retry_at,
                        )
                        _upload_retryable_fail = True
                    elif admission_denied:
                        from api.services.yt_retry_guard import next_pt_day_retry_str
                        retry_at = next_pt_day_retry_str()
                        db.update_video(video_id, progress=0, status="awaiting_upload",
                                        error_message="Upload admission denied locally (reintento mañana PT)",
                                        progress_phase="upload", scheduled_upload_at=retry_at)
                        logger.info(
                            "[%s] Upload admission denied — diferido a %s (retry mañana, no bucle)",
                            canal, retry_at,
                        )
                        _upload_retryable_fail = True
                    else:
                        db.update_video(video_id, progress=95, status="ready")
                except Exception:
                    db.update_video(video_id, progress=95, status="ready")
                # ── Log lifecycle: upload failed ──
                try:
                    log_lifecycle(db, entity_type='video', entity_id=video_id, channel_id=channel_id,
                                  event='upload_failed', phase='upload', status='failed',
                                  message='Upload to YouTube failed — video saved locally')
                except Exception:
                    pass

        # ── Success ──────────────────────────────────────────
        # Si el upload falló por condición transient (cap/cuota), el job se
        # marca 'failed' para que cuente en el presupuesto anti-bucle.
        success = not _upload_retryable_fail
        pipeline_duration = int((time.time() - pipeline_start))
        logger.info("PIPELINE COMPLETE in %d seconds", pipeline_duration)
        
        # ── Log lifecycle: upload completed ──
        try:
            upload_completed = action != 'generate_only' and upload
            event_type = 'upload_completed' if upload_completed else 'generation_completed'
            pub_mode = video_record.get('publish_mode', 'immediate') if video_record else 'immediate'
            event_msg = f'Uploaded to YouTube (yt_id={yt_video_id})' if upload_completed and yt_video_id else 'Generation completed (no upload)'
            log_lifecycle(db, entity_type='video', entity_id=video_id, channel_id=channel_id,
                          event=event_type, phase='upload', status='completed',
                          message=event_msg,
                          metadata={'duration_ms': pipeline_duration * 1000,
                                    'yt_video_id': yt_video_id,
                                    'publish_mode': pub_mode,
                                    'action': action})
        except Exception:
            pass
        
        try:
            db.update_video(video_id, timing_data=orch.collect_timing_json())
        except Exception:
            pass
        try:
            if script and script.get("id"):
                db.mark_script_used(script["id"])
        except Exception:
            pass
        
        # ── Record marathon completion ──
        if is_marathon:
            try:
                db.record_marathon(channel_id, "completed")
                logger.info("[MARATHON][%s] Marathon completed", canal)
            except Exception as exc_mar:
                logger.warning("[MARATHON][%s] Failed to record completion: %s", canal, exc_mar)

    except Exception as exc:
        tb = traceback.format_exc()
        tb_lines = tb.strip().split('\n')
        tb_tail = '\n'.join(tb_lines[-3:]) if len(tb_lines) >= 3 else tb
        error_msg = f"{type(exc).__name__}: {exc} (trace: {tb_tail})"
        logger.error("Pipeline crashed: %s\n%s", exc, tb)
        db.update_job(job_id, status="failed", error_msg=error_msg[:500])
        db.update_video(video_id, status="error", progress_phase="error",
                        error_message=error_msg[:500])
        
        # ── Record marathon failure ──
        if is_marathon:
            try:
                db.record_marathon(channel_id, "failed")
                logger.warning("[MARATHON][%s] Marathon failed: %s", canal, error_msg[:200])
            except Exception as exc_mar:
                logger.warning("[MARATHON][%s] Failed to record failure: %s", canal, exc_mar)
        
        # ── Log lifecycle: generation failed ──
        try:
            log_lifecycle(db, entity_type='video', entity_id=video_id, channel_id=channel_id,
                          event='generation_failed', status='failed',
                          message=error_msg[:300],
                          metadata={'traceback': tb[:500], 'job_id': job_id})
        except Exception:
            pass
        
        # ── Save timing even on crash ────────────────────────
        try:
            if orch is not None:
                db.update_video(video_id, timing_data=orch.collect_timing_json())
        except Exception:
            pass
        success = False

    finally:
        # ── Stop global heartbeat ───────────────────────────
        _heartbeat_stop.set()
        # ── Final DB update (wrapped in try/except so DB failure
        # doesn't crash the finally block itself) ──────────────
        try:
            db.update_job(
                job_id,
                status="completed" if success else "failed",
                error_msg=error_msg[:500] if error_msg else None,
                completed_at=datetime.now(timezone.utc).isoformat(),
            )
        except Exception as _dbe:
            logger.critical("CRITICAL: could not update final job status: %s", _dbe)
        # ── Release media file locks so future cleanups can reclaim stale files ──
        try:
            db.unlock_media_files(job_id)
        except Exception:
            pass
        try:
            if orch is not None:
                orch.cleanup()
        except Exception:
            pass
        _kill_orphaned_ffmpeg()
        import gc
        gc.collect()
        try:
            import ctypes
            ctypes.CDLL("libc.so.6").malloc_trim(0)
        except Exception:
            pass
        logger.info("Worker exiting: success=%s, job=%d, video=%d", success, job_id, video_id)

    return success


def _phase_index(phase: str) -> int:
    """Return the index of a phase in _PHASE_ORDER, or -1 if not found."""
    return _PHASE_INDEX.get(phase, -1)


# ── CLI entry point ───────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Autotube full pipeline worker — runs independently of the API"
    )
    parser.add_argument("--job-id", type=int, required=True,
                        help="generation_jobs.id")
    parser.add_argument("--channel-id", type=int, required=True,
                        help="channels.id")
    parser.add_argument("--video-id", type=int, required=True,
                        help="videos.id (pre-created record)")
    parser.add_argument("--action", type=str, default="generate_and_upload",
                        choices=["generate_and_upload", "generate_only", "upload_only"],
                        help="Action to perform")
    parser.add_argument("--test-mode", action="store_true",
                        help="Fast test mode: low-res, no upload, less content")
    parser.add_argument("--no-upload", action="store_true",
                        help="Skip YouTube upload (video stays local)")
    parser.add_argument("--source-mode", type=str, default="original",
                        choices=["original", "viral"],
                        help="Content source mode: original (AI-generated) or viral (mirrored from YouTube)")
    parser.add_argument("--viral-candidate-id", type=int, default=None,
                        help="raw_content.id for the viral candidate to use")
    parser.add_argument("--debug", action="store_true",
                        help="Enable debug logging")

    # ── Graceful handling of invalid actions ─────────────────
    # Catch argparse SystemExit on invalid choices and write a
    # clear error to the generation_jobs table before exiting,
    # so the UI shows a meaningful message instead of a raw crash.
    try:
        args = parser.parse_args()
    except SystemExit as exc:
        # Extract job_id if possible from raw argv for DB error logging
        import re as _re
        job_id = 0
        for i, arg in enumerate(sys.argv):
            if arg == "--job-id" and i + 1 < len(sys.argv):
                try:
                    job_id = int(sys.argv[i + 1])
                except ValueError:
                    pass
                break
        if job_id:
            try:
                db = get_db("generation_service")
                action_arg = next(
                    (sys.argv[i+1] for i, a in enumerate(sys.argv)
                     if a == "--action" and i+1 < len(sys.argv)), "unknown")
                db.execute(
                    "UPDATE generation_jobs SET status='failed', error_msg=? WHERE id=?",
                    (f"Invalid action: '{action_arg}'. Valid: generate_and_upload, "
                     f"generate_only, upload_only", job_id))
            except Exception:
                pass
        sys.exit(exc.code if exc.code else 2)

    # Setup logging
    logger = _setup_worker_logging(args.job_id)
    if args.debug:
        logger.setLevel(logging.DEBUG)

    logger.info("Worker started: job=%d channel=%d video=%d action=%s test_mode=%s",
                args.job_id, args.channel_id, args.video_id, args.action, args.test_mode)

    # ── Graceful shutdown on SIGTERM ────────────────────────────
    # Worker receives SIGTERM during API restarts / deploys / OOM.
    # Immediate exit during critical phases (ffmpeg concat) corrupts
    # the render and causes permanent bug_crash after 3 retries.
    # We defer shutdown until a safe point (uses module-level Events):
    #   - Non-critical phases: exit immediately
    #   - Video assembly (phase 4): block the signal until ffmpeg finishes
    def _signal_handler(signum, frame):
        if _in_critical_phase.is_set():
            logger.warning(
                "Received signal %d during CRITICAL phase — deferred shutdown. "
                "Will exit after current operation completes (max 300s grace)",
                signum,
            )
            _shutdown_requested.set()
        else:
            logger.warning("Received signal %d — cleaning up...", signum)
            _kill_orphaned_ffmpeg()
            sys.exit(128 + signum)

    signal.signal(signal.SIGTERM, _signal_handler)
    signal.signal(signal.SIGINT, _signal_handler)

    # Run the job
    success = run_job(
        job_id=args.job_id,
        channel_id=args.channel_id,
        video_id=args.video_id,
        action=args.action,
        test_mode=args.test_mode,
        upload=not args.no_upload,
        source_mode=args.source_mode,
        viral_candidate_id=args.viral_candidate_id,
    )

    logger.info("Worker finished: success=%s", success)
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
