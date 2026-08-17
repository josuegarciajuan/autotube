"""System stabilization router — cleans processes, files, and restarts the API.

POST /api/system/stabilize
    Returns a JSON summary of all cleanup actions performed.

    Steps:
    1. Kill orphaned subprocesses (ffmpeg, edge-tts, yt-dlp, python generation)
    2. Reap zombie/defunct processes
    3. Remove orphaned MoviePy temp files from project root
    4. Purge output/temp/ directory
    5. Purge output/video_clips/ directory (regenerated on next render)
    6. Delete uploaded video MP4s that already live on YouTube
    7. Rotate oversized log files
    8. VACUUM SQLite database
    9. Schedule API restart via background nohup script
"""

import os
import sys
import json
import glob
import shutil
import sqlite3
import subprocess
import logging
from pathlib import Path
from fastapi import APIRouter
from api.deps import get_db

logger = logging.getLogger("autotube.system")

router = APIRouter()

PROJECT_ROOT = Path(__file__).parent.parent.parent
OUTPUT_DIR = PROJECT_ROOT / "output"
LOGS_DIR = PROJECT_ROOT / "logs"
DB_PATH = PROJECT_ROOT / "autotube.db"
LOG_MAX_BYTES = 5 * 1024 * 1024  # Rotate logs > 5 MB


# ── Process killers ─────────────────────────────────────────────

def _kill_by_pattern(pattern: str, label: str) -> int:
    """Kill processes matching a pgrep pattern. Returns count killed."""
    killed = 0
    try:
        r = subprocess.run(
            ["pgrep", "-f", pattern],
            capture_output=True, text=True, timeout=5,
        )
        if r.stdout.strip():
            pids = r.stdout.strip().split()
            for pid_str in pids:
                try:
                    os.kill(int(pid_str), 9)
                    killed += 1
                    logger.info("Killed %s (pid=%s)", label, pid_str)
                except ProcessLookupError:
                    pass
    except Exception as exc:
        logger.warning("Error killing %s: %s", label, exc)
    return killed


def _reap_zombies() -> int:
    """Reap zombie child processes. Returns count reaped."""
    reaped = 0
    try:
        while True:
            wpid, _ = os.waitpid(-1, os.WNOHANG)
            if wpid == 0:
                break
            reaped += 1
    except (ChildProcessError, OSError):
        pass
    return reaped


# ── Directory size helpers ──────────────────────────────────────

def _dir_size(path: Path) -> int:
    """Return total size of a directory in bytes (0 if not found)."""
    if not path.exists():
        return 0
    total = 0
    for f in path.rglob("*"):
        if f.is_file():
            try:
                total += f.stat().st_size
            except OSError:
                pass
    return total


def _fmt_bytes(n: int) -> str:
    """Human-readable byte size."""
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    if n < 1024 * 1024 * 1024:
        return f"{n / (1024 * 1024):.1f} MB"
    return f"{n / (1024 * 1024 * 1024):.2f} GB"


# ── Stabilization endpoint ──────────────────────────────────────

@router.post("/system/stabilize")
def stabilize():
    """Run full system stabilization and return summary.
    
    Refuses to run if any generation jobs are currently active,
    to prevent destroying assets of in-progress renders.
    """
    # ── Guard: refuse if active generation jobs exist ──
    from database.db_extended import ExtendedDatabase
    _db = ExtendedDatabase()
    active_jobs = _db.count_active_jobs()
    if active_jobs > 0:
        from fastapi.responses import JSONResponse
        return JSONResponse(
            status_code=409,
            content={
                "error": "Hay jobs de generación activos",
                "detail": f"No se puede estabilizar mientras hay {active_jobs} job(s) activos. Cancela los jobs primero.",
                "active_jobs": active_jobs,
                "steps": [],
            }
        )
    
    steps = []
    total_killed = 0
    total_freed = 0
    total_deleted = 0

    # ── Step 1: Kill orphaned subprocesses ──────────────────────
    patterns = [
        ("ffmpeg", "ffmpeg huérfano"),
        ("edge-tts", "edge-tts huérfano"),
        ("yt-dlp", "yt-dlp huérfano"),
        ("moviepy", "moviepy huérfano"),
        ("test_video\\.py", "test_video.py huérfano"),
        ("main\\.py.*run|main\\.py.*upload|main\\.py.*generate", "pipeline CLI huérfano"),
    ]
    for pattern, label in patterns:
        k = _kill_by_pattern(pattern, label)
        total_killed += k
        if k > 0:
            steps.append(f"Matados {k} procesos {label}")

    # ── Step 2: Reap zombie processes ───────────────────────────
    reaped = _reap_zombies()
    if reaped > 0:
        steps.append(f"Reapados {reaped} procesos zombie")

    # ── Step 3: Remove MoviePy temp files from project root ─────
    temp_mpy_files = list(PROJECT_ROOT.glob("narration_kokoro_*TEMP_MPY_*.mp4"))
    temp_mpy_files += list(PROJECT_ROOT.glob("*TEMP_MPY*.mp4"))
    temp_mpy_files += list(PROJECT_ROOT.glob("*.TEMP_MPY*"))
    mpy_freed = 0
    for f in temp_mpy_files:
        try:
            mpy_freed += f.stat().st_size
            f.unlink()
            total_deleted += 1
        except OSError:
            pass
    total_freed += mpy_freed
    if mpy_freed > 0:
        steps.append(f"Eliminados {_fmt_bytes(mpy_freed)} en temporales MoviePy de raíz")

    # ── Step 4: Purge output/temp/ ──────────────────────────────
    temp_dir = OUTPUT_DIR / "temp"
    temp_freed = _purge_dir(temp_dir)
    total_freed += temp_freed
    if temp_freed > 0:
        steps.append(f"Eliminados {_fmt_bytes(temp_freed)} en output/temp/")

    # ── Step 5: Purge output/video_clips/ ───────────────────────
    vc_dir = OUTPUT_DIR / "video_clips"
    vc_freed = _purge_dir(vc_dir)
    total_freed += vc_freed
    if vc_freed > 0:
        steps.append(f"Eliminados {_fmt_bytes(vc_freed)} en output/video_clips/ (se regenerarán si se necesitan)")

    # ── Step 6: Delete uploaded video MP4s ──────────────────────
    vids_freed, vids_count = _clean_uploaded_videos()
    total_freed += vids_freed
    total_deleted += vids_count
    if vids_freed > 0:
        steps.append(f"Eliminados {vids_count} videos ya subidos a YouTube ({_fmt_bytes(vids_freed)})")

    # ── Step 7: Rotate logs ─────────────────────────────────────
    log_freed = _rotate_logs()
    total_freed += log_freed
    if log_freed > 0:
        steps.append(f"Rotados logs ({_fmt_bytes(log_freed)} liberados)")

    # ── Step 8: VACUUM database ─────────────────────────────────
    db_before = _dir_size(DB_PATH) if DB_PATH.exists() else 0
    _vacuum_db()
    db_after = DB_PATH.stat().st_size if DB_PATH.exists() else 0
    db_saved = db_before - db_after
    if db_saved > 0:
        steps.append(f"Base de datos compactada ({_fmt_bytes(db_before)} → {_fmt_bytes(db_after)})")

    # ── Step 9: Schedule API restart ────────────────────────────
    _schedule_restart()

    # ── Final disk stats ────────────────────────────────────────
    try:
        disk = shutil.disk_usage(str(PROJECT_ROOT))
        disk_free = _fmt_bytes(disk.free)
    except Exception:
        disk_free = "desconocido"

    summary = {
        "success": True,
        "steps": steps,
        "total_killed": total_killed,
        "total_freed": _fmt_bytes(total_freed),
        "total_freed_bytes": total_freed,
        "total_deleted_files": total_deleted,
        "disk_free": disk_free,
        "api_restart_scheduled": True,
        "message": (
            f"Estabilización completada: {total_killed} procesos eliminados, "
            f"{_fmt_bytes(total_freed)} liberados en {total_deleted} archivos. "
            f"La API se reiniciará en 2 segundos."
        ),
    }

    logger.info(
        "Stabilization complete: %d processes killed, %s freed, %d files deleted, API restart scheduled",
        total_killed, _fmt_bytes(total_freed), total_deleted,
    )

    return summary


# ── Scheduler pause/resume endpoints ─────────────────────────────

@router.post("/system/scheduler-pause")
def scheduler_pause():
    """Pausar todas las subidas programadas (shorts + long-form + planned slots).
    
    Útil cuando se agota la cuota de YouTube API o para mantenimiento.
    Crea una alerta en el dashboard de monitorización.
    """
    from database.db_extended import ExtendedDatabase
    db = ExtendedDatabase()
    
    # Check if already paused
    already_paused = db.get_system_state("scheduler_paused") == "true"
    
    db.set_system_state("scheduler_paused", "true")
    db.set_system_state("quota_exhausted_at", 
                         __import__('datetime').datetime.now(
                             __import__('datetime').timezone.utc).isoformat())
    
    # Create alert if not already paused
    if not already_paused:
        try:
            from api.services.lifecycle_monitor import create_alert
            create_alert(db,
                         entity_type='system', entity_id=None, channel_id=None,
                         alert_type='quota_exhausted', severity='warning',
                         title='Scheduler pausado manualmente',
                         message='Todas las subidas programadas están pausadas. '
                                 'Usa /api/system/scheduler-resume para reanudar.',
                         metadata={'source': 'manual_pause'})
        except Exception:
            pass
    
    logger.info("Scheduler paused manually via API")
    return {"ok": True, "scheduler_paused": True, "message": "Scheduler pausado. Todas las subidas detenidas."}


@router.post("/system/scheduler-resume")
def scheduler_resume():
    """Reanudar las subidas programadas.
    
    Limpia los marcadores de quota agotada y resuelve las alertas relacionadas.
    """
    from database.db_extended import ExtendedDatabase
    db = ExtendedDatabase()
    
    db.set_system_state("scheduler_paused", "false")
    # Limpia TODOS los marcadores de quota agotada (global + claves por
    # proyecto `quota_exhausted_{project_id}`). Antes solo se vaciaban las
    # claves globales y los breakers por proyecto seguían bloqueando los
    # canales tras un resume manual.
    db.clear_quota_exhausted()
    
    # Resolve quota-related alerts
    try:
        with db._connect() as conn:
            conn.execute(
                """UPDATE pipeline_alerts SET resolved = 1, resolved_at = datetime('now')
                   WHERE alert_type IN ('quota_exhausted', 'quota_warning') AND resolved = 0"""
            )
            conn.commit()
    except Exception:
        pass
    
    # Log lifecycle event
    try:
        from api.services.lifecycle_monitor import log_event as _le
        _le(db, entity_type='system', entity_id=0, channel_id=None,
            event='quota_recovered', status='info',
            message='Scheduler reanudado manualmente')
    except Exception:
        pass
    
    logger.info("Scheduler resumed manually via API")
    return {"ok": True, "scheduler_paused": False, "message": "Scheduler reanudado. Subidas activas."}


@router.get("/system/quota-status")
def quota_status():
    """Estado actual de la cuota YouTube Data API v3.

    Devuelve si la cuota está agotada y cuándo se recarga (medianoche PT).
    Ligero — solo lee system_state, sin llamadas a YouTube API.

    La cuota es POR PROYECTO GCP: además del resumen global (legacy, para
    compatibilidad), devuelve `projects` con el estado del breaker de cada
    proyecto/cuenta, para que la UI pinte cada cuenta por separado.
    """
    from database.db_extended import ExtendedDatabase
    db = ExtendedDatabase()
    result = db.get_quota_reset_time()
    try:
        from api.services.quota_tracker import get_projects_status
        result["projects"] = get_projects_status(db)
    except Exception:
        result["projects"] = []
    return result


# ── Helpers ──────────────────────────────────────────────────────

def _purge_dir(dir_path: Path) -> int:
    """Remove all contents of a directory. Returns bytes freed."""
    if not dir_path.exists():
        return 0
    size_before = _dir_size(dir_path)
    try:
        shutil.rmtree(dir_path)
        dir_path.mkdir(parents=True, exist_ok=True)
    except Exception as exc:
        logger.warning("Could not purge %s: %s", dir_path, exc)
        return 0
    return size_before


def _clean_uploaded_videos() -> tuple:
    """Delete local .mp4 files for videos that have been uploaded to YouTube.
    Returns (bytes_freed, files_deleted)."""
    freed = 0
    deleted = 0
    videos_dir = OUTPUT_DIR / "videos"

    if not videos_dir.exists():
        return 0, 0

    try:
        db = get_db()
        # Get all uploaded video_record IDs
        conn = sqlite3.connect(str(DB_PATH), timeout=60)
        c = conn.cursor()
        c.execute("SELECT id, video_path FROM videos WHERE status = 'uploaded' AND video_path IS NOT NULL AND video_path != ''")
        uploaded = c.fetchall()
        conn.close()

        # Build set of basenames to match
        uploaded_paths: set[str] = set()
        for vid_id, vpath in uploaded:
            if vpath:
                # Normalize to just the filename
                fname = Path(vpath).name
                if fname:
                    uploaded_paths.add(fname)

        # Delete matching mp4 files
        for mp4 in sorted(videos_dir.rglob("*.mp4")):
            if mp4.name in uploaded_paths:
                try:
                    freed += mp4.stat().st_size
                    mp4.unlink()
                    deleted += 1
                    logger.info("Deleted uploaded video: %s", mp4)
                except OSError as exc:
                    logger.warning("Could not delete %s: %s", mp4, exc)

        # Also clean empty shorts dirs
        shorts_dir = videos_dir / "shorts"
        if shorts_dir.exists():
            for mp4 in shorts_dir.rglob("*.mp4"):
                fname = mp4.name
                # Check if this short was already uploaded
                try:
                    conn2 = sqlite3.connect(str(DB_PATH), timeout=10)
                    c2 = conn2.cursor()
                    c2.execute(
                        "SELECT id FROM shorts WHERE file_path LIKE ? AND status = 'published'",
                        (f"%{fname}%",),
                    )
                    if c2.fetchone():
                        try:
                            freed += mp4.stat().st_size
                            mp4.unlink()
                            deleted += 1
                            logger.info("Deleted published short: %s", mp4)
                        except OSError:
                            pass
                    conn2.close()
                except Exception:
                    pass

    except Exception as exc:
        logger.warning("Error cleaning uploaded videos: %s", exc)

    return freed, deleted


def _rotate_logs() -> int:
    """Truncate log files that exceed LOG_MAX_BYTES. Returns bytes freed."""
    freed = 0
    if not LOGS_DIR.exists():
        return 0

    for log_file in LOGS_DIR.glob("*.log"):
        try:
            size = log_file.stat().st_size
            if size > LOG_MAX_BYTES:
                freed += size
                # Keep last 1 MB of content
                with open(log_file, "rb") as f:
                    f.seek(max(0, size - 1024 * 1024))
                    tail = f.read()
                with open(log_file, "wb") as f:
                    f.write(f"--- Log rotado: se conservan las últimas 1MB ---\n".encode())
                    f.write(tail)
                new_size = log_file.stat().st_size
                logger.info("Rotated log %s: %s → %s", log_file.name, _fmt_bytes(size), _fmt_bytes(new_size))
        except OSError as exc:
            logger.warning("Could not rotate %s: %s", log_file, exc)

    return freed


def _vacuum_db():
    """Run VACUUM on the SQLite database to reclaim space."""
    try:
        conn = sqlite3.connect(str(DB_PATH), timeout=30)
        conn.execute("PRAGMA journal_mode=DELETE")
        conn.execute("VACUUM")
        conn.close()
        logger.info("Database VACUUM completed")
    except Exception as exc:
        logger.warning("VACUUM failed: %s", exc)


def _schedule_restart():
    """Schedule API restart by launching a background shell script.

    The script waits 2 seconds (giving time for the HTTP response to flush),
    then kills the current uvicorn and starts a new one via nohup.
    """
    pid = os.getpid()
    restart_script = (
        f"#!/bin/bash\n"
        f"sleep 2\n"
        f"kill {pid} 2>/dev/null\n"
        f"sleep 1\n"
        f"cd {PROJECT_ROOT}\n"
        f"nohup python3 -m uvicorn api.main:app --host 0.0.0.0 --port 8000 --log-level info > logs/api.log 2>&1 &\n"
        f"echo 'API restarted at' $(date) >> logs/api_restarts.log\n"
    )

    script_path = Path("/tmp/restart_autotube_api.sh")
    script_path.write_text(restart_script)
    script_path.chmod(0o755)

    try:
        subprocess.Popen(
            ["nohup", str(script_path)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        logger.info("API restart scheduled — will execute in 2 seconds")
    except Exception as exc:
        logger.error("Failed to schedule API restart: %s", exc)
