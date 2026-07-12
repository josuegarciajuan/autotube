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
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

# Ensure the project root is on sys.path
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


# ── Logging setup ──────────────────────────────────────────────────

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
    for pattern in ("edge-tts", "yt-dlp"):
        try:
            r2 = subprocess.run(
                ["pgrep", "-f", pattern],
                capture_output=True, text=True, timeout=5,
            )
            if r2.stdout.strip():
                for opid in r2.stdout.strip().split():
                    try:
                        os.kill(int(opid), signal.SIGKILL)
                        killed += 1
                    except ProcessLookupError:
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


def _check_ram_gate(logger, timeout_sec: int = 600) -> bool:
    """Check if there's enough RAM to proceed with generation.

    Uses MIN_FREE_FOR_RENDER_MB from config.settings as the threshold
    (default 3000 MB). Delegates to pipeline.ram_governor.wait_for_ram()
    which blocks until enough RAM is free or timeout expires.

    This avoids the previous hard abort — the pipeline now waits for
    memory to free up (e.g. from other processes finishing), matching
    the behaviour of the shorts scheduler.

    Returns True if OK, False if timeout expires.
    """
    from config.settings import MIN_FREE_FOR_RENDER_MB
    from pipeline.ram_governor import wait_for_ram, available_mb

    threshold = MIN_FREE_FOR_RENDER_MB
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


# ── Pre-flight cleanup ────────────────────────────────────────────

def _preflight_cleanup(logger):
    """Clean temp directories before starting the pipeline."""
    import shutil
    cleanup_dirs = [
        _PROJECT_ROOT / "output" / "video_clips",
        _PROJECT_ROOT / "output" / "temp",
    ]
    for d in cleanup_dirs:
        if d.exists():
            try:
                shutil.rmtree(d)
                d.mkdir(parents=True, exist_ok=True)
                logger.info("Cleaned up %s", d)
            except Exception as exc:
                logger.warning("Could not clean %s: %s", d, exc)


# ── Phase order (for checkpoint resume) ──────────────────────────

_PHASE_ORDER = ["scrape", "script", "tts", "media", "video", "metadata", "upload"]

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
        db.update_video(video_id, status="error", progress_phase="error")
        return False

    canal = ch["slug"]
    channel_name = ch.get("name", canal)
    
    # ── 3. Load checkpoint (resume support) ───────────────────
    checkpoint, last_phase, last_idx = _load_checkpoint(video_id, db)
    
    if last_idx >= 0:
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
    db.update_job(job_id, status="running", started_at=datetime.now(timezone.utc).isoformat())
    db.update_video(video_id, status="generating", progress=1 if start_idx == 0 else 2, 
                    progress_phase="inicio" if start_idx == 0 else f"resume_{start_phase}")

    # ── 6. Pre-flight cleanup (skip if resuming — don't delete downloaded clips) ──
    _kill_orphaned_ffmpeg()
    if start_idx == 0:
        _preflight_cleanup(logger)

    # ── 7. Set up progress callback ───────────────────────────
    def _progress_to_db(percent: int, phase: str, message: str, **kwargs):
        try:
            db.update_video(video_id, progress=percent, progress_phase=phase)
        except Exception as exc:
            logger.debug("Progress DB write failed (non-fatal): %s", exc)

    # ── 8. Load channel config ─────────────────────────────────
    try:
        from config.config_bridge import get_channel_config
        config = get_channel_config(canal)
    except Exception as exc:
        logger.error("Failed to load config for %s: %s", canal, exc)
        db.update_job(job_id, status="failed", error_msg=f"Config error: {exc}")
        db.update_video(video_id, status="error", progress_phase="error")
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
            # Verify at least some asset files exist
            existing = sum(1 for a in assets if isinstance(a, dict) and 
                          a.get("path") and Path(str(a["path"])).exists())
            if existing > 0:
                media_assets = assets
                logger.info("Media checkpoint: %d/%d assets still on disk", 
                           existing, len(assets))
            else:
                logger.warning("Media files missing — will re-fetch")
    
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

        orch = PipelineOrchestrator(
            canal=canal,
            db_video_id=video_id,
            progress_callback=_progress_to_db,
            source_mode=source_mode,
            viral_candidate_id=viral_candidate_id,
        )
        if test_mode:
            orch.config = config

        pipeline_start = time.time()

        # ═══════════════════════════════════════════════════════
        # Phase 0: Scrape
        # ═══════════════════════════════════════════════════════
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
            orch.phase_scrape()
            db.update_video(video_id, progress=12, progress_phase="scrape")
            _save_checkpoint(video_id, "scrape", {"items_added": 0}, db)

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
            
            script = orch.phase_generate_script()
            if not script:
                logger.warning("Script generation failed — retrying after re-scrape...")
                orch.phase_scrape()
                script = orch.phase_generate_script()
            
            if not script:
                error_msg = "No se pudo generar el guion (sin contenido disponible)"
                logger.error(error_msg)
                db.update_job(job_id, status="failed", error_msg=error_msg[:500])
                db.update_video(video_id, status="error", progress_phase="script")
                return False
            
            db.update_video(video_id, progress=25, progress_phase="script",
                            script_id=script.get("id"))
            _save_checkpoint(video_id, "script", {
                "id": script.get("id"),
                "titulo": script.get("titulo_selected", "")[:60],
                "guion": script.get("guion", ""),
                "bloques_json": script.get("bloques_json", []),
                "escenas_json": script.get("escenas_json", []),
                "titulo_options": script.get("titulo_options", []),
            }, db)
        
        titulo = (script.get("titulo_selected") or script.get("titulo") or "Sin titulo")[:60] if script else ""
        logger.info("Script: '%s' (%d words)", titulo,
                    len(script.get("guion", "").split()) if script else 0)

        # ═══════════════════════════════════════════════════════
        # Phase 2: TTS
        # ═══════════════════════════════════════════════════════
        if _phase_index("tts") < start_idx:
            logger.info("Skipping TTS (loaded from checkpoint)")
            db.update_video(video_id, progress=40, progress_phase="tts")
        else:
            # ── RAM gate before TTS (avoid wasting 5-9 min of compute) ──
            if not _check_ram_gate(logger, timeout_sec=300):
                db.update_job(job_id, status="failed", error_msg="RAM insuficiente (pre-TTS gate)")
                db.update_video(video_id, status="error", progress_phase="script")
                return False

            db.update_video(video_id, progress=30, progress_phase="tts")
            logger.info("Phase 2/6: Generating TTS audio...")

            audio_data = orch.phase_tts(script, job_id=job_id)
            if not audio_data:
                error_msg = "Fallo la generacion de voz (TTS)"
                logger.error(error_msg)
                db.update_job(job_id, status="failed", error_msg=error_msg[:500])
                db.update_video(video_id, status="error", progress_phase="tts")
                return False
            
            db.update_video(video_id, progress=40, progress_phase="tts")
            _save_checkpoint(video_id, "tts", {
                "audio_path": audio_data.get("audio_path", ""),
                "timestamps_path": audio_data.get("timestamps_path", ""),
                "cta_audio_path": audio_data.get("cta_audio_path", ""),
            }, db)
        
        audio_dur = 0
        if audio_data and isinstance(audio_data, dict) and audio_data.get("timestamps"):
            ts = audio_data["timestamps"]
            if ts and isinstance(ts[-1], dict):
                audio_dur = int(ts[-1].get("end_ms", 0) / 1000)
        logger.info("TTS: %ds audio", audio_dur)

        # ── RAM gate before heavy phases (media + video assembly) ──
        if not _check_ram_gate(logger):
            db.update_job(job_id, status="failed", error_msg="RAM insuficiente tras timeout (pre-render gate)")
            db.update_video(video_id, status="error", progress_phase="tts")
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
            
            media_assets = orch.phase_media(script, audio_data)
            if not media_assets:
                error_msg = "No se encontraron imagenes ni videos"
                logger.error(error_msg)
                db.update_job(job_id, status="failed", error_msg=error_msg[:500])
                db.update_video(video_id, status="error", progress_phase="media")
                return False
            
            db.update_video(video_id, progress=55, progress_phase="media")
            _save_checkpoint(video_id, "media", {
                "assets": [{"type": a.get("type", "?"), "path": str(a.get("path", "")),
                            "source": a.get("source", "?")}
                           for a in (media_assets if isinstance(media_assets, list) else [])],
            }, db)
        
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
        else:
            db.update_video(video_id, progress=60, progress_phase="video")
            logger.info("Phase 4/6: Assembling video...")
            
            video_data = orch.phase_video(script, audio_data, 
                                          media_assets if isinstance(media_assets, list) else [],
                                          job_id=job_id)
            if not video_data:
                error_msg = "Fallo el ensamblaje del video"
                logger.error(error_msg)
                db.update_job(job_id, status="failed", error_msg=error_msg[:500])
                db.update_video(video_id, status="error", progress_phase="video")
                return False
            
            db.update_video(video_id, progress=75, progress_phase="video", status="ready")
            _save_checkpoint(video_id, "video", {
                "video_path": str(video_data.get("video_path", "")),
                "thumbnail_path": str(video_data.get("thumbnail_path", "")),
                "titulo": str(video_data.get("titulo", "")),
            }, db)
        
        logger.info("Video: %s", video_data.get("video_path", "?") if video_data else "?")

        # ═══════════════════════════════════════════════════════
        # Phase 5: Metadata
        # ═══════════════════════════════════════════════════════
        if _phase_index("metadata") < start_idx:
            logger.info("Skipping metadata (loaded from checkpoint)")
        else:
            db.update_video(video_id, progress=78, progress_phase="metadata")
            logger.info("Phase 5/6: Generating SEO metadata...")
            
            try:
                metadata = orch.phase_metadata(script, video_data)
            except Exception as meta_exc:
                logger.warning("Metadata generation failed (non-fatal): %s", meta_exc)
                metadata = None
            
            if metadata and isinstance(metadata, dict):
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
            else:
                # Save basic info even without metadata
                db.update_video(
                    video_id,
                    titulo_final=video_data.get("titulo", "") if video_data else "",
                    thumbnail_path=video_data.get("thumbnail_path", "") if video_data else "",
                    status="ready",
                )

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

        # ═══════════════════════════════════════════════════════
        # Phase 6: Upload
        # ═══════════════════════════════════════════════════════
        skip_upload = not upload or test_mode
        if skip_upload:
            skip_reason = "Test mode" if test_mode else "Upload disabled"
            logger.info("Phase 6/6: %s — skipping upload", skip_reason)
            db.update_video(video_id, progress=100, status="ready")
        else:
            db.update_video(video_id, progress=90, progress_phase="upload")
            logger.info("Phase 6/6: Uploading to YouTube...")
            
            # ── Read planned target from the video record (set by planning) ──
            planned_public_at = None
            video_record = db.get_video(video_id)
            if video_record and video_record.get("publish_mode") == "scheduled":
                planned_public_at = video_record.get("target_public_at")
                if planned_public_at:
                    logger.info("Using planned public time from slot: %s", planned_public_at)
            
            yt_video_id = orch.phase_upload(script, video_data, metadata, job_id=job_id,
                                             planned_public_at=planned_public_at)
            if yt_video_id:
                yt_url = f"https://youtube.com/watch?v={yt_video_id}"
                db.mark_video_uploaded(video_id, yt_video_id, yt_url)
                db.update_video(video_id, progress=100, status="uploaded")
                
                vp = video_data.get("video_path", "") if video_data else ""
                if vp and Path(vp).exists():
                    try:
                        Path(vp).unlink()
                        db.update_video(video_id, video_path="")
                        logger.info("Deleted local mp4: %s", vp)
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
                    try:
                        db.insert_video_stats(video_id=video_id, yt_video_id=yt_video_id,
                                             stats={"viewCount": 0, "likeCount": 0, "commentCount": 0})
                    except Exception:
                        pass
                logger.info("UPLOADED: %s", yt_url)
            else:
                logger.error("Upload failed — video saved locally")
                db.update_video(video_id, progress=95, status="ready")

        # ── Success ──────────────────────────────────────────
        success = True
        pipeline_duration = int((time.time() - pipeline_start))
        logger.info("PIPELINE COMPLETE in %d seconds", pipeline_duration)
        
        try:
            db.update_video(video_id, timing_data=orch.collect_timing_json())
        except Exception:
            pass
        try:
            if script and script.get("id"):
                db.mark_script_used(script["id"])
        except Exception:
            pass

    except Exception as exc:
        tb = traceback.format_exc()
        error_msg = f"{type(exc).__name__}: {exc}"
        logger.error("Pipeline crashed: %s\n%s", exc, tb)
        db.update_job(job_id, status="failed", error_msg=error_msg[:500])
        db.update_video(video_id, status="error", progress_phase="error")
        success = False

    finally:
        db.update_job(
            job_id,
            status="completed" if success else "failed",
            error_msg=error_msg[:500] if error_msg else None,
            completed_at=datetime.now(timezone.utc).isoformat(),
        )
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
    args = parser.parse_args()

    # Setup logging
    logger = _setup_worker_logging(args.job_id)
    if args.debug:
        logger.setLevel(logging.DEBUG)

    logger.info("Worker started: job=%d channel=%d video=%d action=%s test_mode=%s",
                args.job_id, args.channel_id, args.video_id, args.action, args.test_mode)

    # Register signal handlers for graceful cleanup
    def _signal_handler(signum, frame):
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
