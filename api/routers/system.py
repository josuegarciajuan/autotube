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


# ── Spam-block per channel (visibilidad + desbloqueo manual) ──────

@router.get("/system/spam-blocks")
def spam_blocks():
    """Estado de bloqueos por spam de YouTube por canal (solo lee system_state).

    Devuelve por cada canal: id, slug, name, strikes, blocked_until (epoch),
    restan_h (horas restantes de bloqueo), blocked (bool), scope (todo/shorts/
    videos/none), why (razón legible) y pending_publish (vídeos programados con
    publishAt y si alguno cae dentro del bloqueo).
    """
    from database.db_extended import ExtendedDatabase
    from api.services.spam_mitigation import build_spam_situation
    db = ExtendedDatabase()
    channels = db.get_channels(active_only=False) or []
    out = []
    for ch in channels:
        cid = int(ch["id"])
        try:
            sit = build_spam_situation(cid, db) or {}
        except Exception as exc:
            logger.warning("spam-blocks: build_spam_situation failed for #%s: %s", cid, exc)
            sit = {}
        # Frecuencia original antes de la rebaja por spam (para mostrar "antes → ahora")
        original_freq = None
        try:
            import json as _json
            raw_restore = db.get_system_state(f"spam_freq_restore_{cid}")
            if raw_restore:
                original_freq = _json.loads(raw_restore)
        except Exception:
            original_freq = None
        out.append({
            "channel_id": cid,
            "slug": ch.get("slug", ""),
            "name": ch.get("name", ""),
            "strikes": sit.get("strikes", 0),
            "blocked": bool(sit.get("blocked", False)),
            "blocked_until": sit.get("blocked_until"),
            "restan_h": sit.get("restan_h", 0.0),
            "freq_reduced": bool(sit.get("freq_reduced", False)),
            "scope": sit.get("scope", "none"),
            "why": sit.get("why", ""),
            "last_removal": sit.get("last_removal"),
            "current_freq": sit.get("current_freq"),
            "original_freq": original_freq,
            "pending_publish": sit.get("pending_publish", {"total": 0, "within_block": []}),
        })
    return {"ok": True, "channels": out}


@router.get("/system/spam-blocks/{channel_id}/report")
def spam_report(channel_id: int, force: bool = False):
    """Informe LLM de la situación de un canal bloqueado por spam.

    Devuelve explicación, por qué, alcance del bloqueo, estado de las
    publicaciones programadas, pasos de solución, un prompt reutilizable y un
    plan de reanudación gradual. Cacheado 12h en system_state (force=true
    regenera).
    """
    from api.services.spam_mitigation import generate_spam_report
    report = generate_spam_report(channel_id, force=bool(force))
    if not report or report.get("ok") is False:
        return report or {"ok": False, "message": "informe no disponible"}
    return {"ok": True, "report": report}


@router.post("/system/spam-blocks/{channel_id}/unblock")
def spam_unblock(channel_id: int):
    """Desbloqueo manual de un canal penalizado por spam.

    Solo para cuando un humano verifica en YouTube Studio que la penalización
    ya no está activa (o fue un falso positivo). Limpia el bloque, reinicia
    el contador de strikes y deja traza de auditoría en system_state.
    """
    from database.db_extended import ExtendedDatabase
    db = ExtendedDatabase()
    db.set_system_state(f"shorts_spam_blocked_until_{channel_id}", "")
    db.set_system_state(f"shorts_spam_strikes_{channel_id}", "0")
    db.set_system_state(
        f"spam_unblocked_at_{channel_id}",
        __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat(),
    )
    logger.warning("Spam block lifted manually for channel #%s (verificado en YouTube Studio)", channel_id)
    return {"ok": True, "message": f"Bloqueo de spam levantado para el canal #{channel_id}"}


@router.post("/system/spam-blocks/{channel_id}/restore-frequency")
def spam_restore_frequency(channel_id: int):
    """Restaura la frecuencia de publicación original tras una rebaja por spam.

    Solo para cuando un humano verifica en YouTube Studio que la penalización ha
    cesado. Devuelve ok=False si no había valores guardados (nada que restaurar).
    """
    from api.services.spam_mitigation import restore_publication_frequency
    restored = restore_publication_frequency(channel_id)
    if not restored:
        return {
            "ok": False,
            "message": f"No hay frecuencia rebajada guardada para el canal #{channel_id}",
        }
    return {
        "ok": True,
        "message": f"Frecuencia de publicación restaurada para el canal #{channel_id}",
    }


# ── Reanudación gradual post-strike (UI) ─────────────────────────

@router.get("/system/resume-status")
def resume_status(channel_id: int = None):
    """Estado de la reanudación gradual post-strike por canal.

    Devuelve por canal: fase actual (0=bloqueado, 1=1 long cada 2 días,
    2=1 long/día), fuente del plan (unblock/sibling/permanent), countdown a la
    siguiente fase, frecuencia actual, strikes/bloqueo y publicaciones
    pendientes (esparcido de Fase 1). Lectura ligera (0 cuota de YouTube API).
    """
    from api.services.gradual_resume import resume_status_detailed
    channels = resume_status_detailed()
    if channel_id:
        channels = [c for c in channels if int(c.get("channel_id", 0)) == int(channel_id)]
    return {"ok": True, "channels": channels}


_STUDIO_CRITICAL_KEYWORDS = (
    "strike", "suspend", "desmonetiza", "monetizaci", "violaci",
    "derechos de autor", "reclamaci", "restricción de edad",
    "restriccion de edad", "eliminad", "no cumple", "sintético",
    "sintetico", "engañosa", "enganosa",
)


def _studio_severity(findings) -> str | None:
    """Severidad de los hallazgos de Studio (None si no hay hallazgos)."""
    if not findings:
        return None
    joined = " ".join(f.lower() for f in findings if f)
    if any(k in joined for k in _STUDIO_CRITICAL_KEYWORDS):
        return "critical"
    return "warning"


_DELIVERY_STATE_LABEL = {
    "strike": "Modo strike — cadencia reducida",
    "recovery": "Recuperación — cadencia parcial",
    "normal": "Operativo",
}


def _cadence_text(policy: dict) -> str:
    """Cadencia efectiva resuelta en lenguaje humano."""
    longs = policy.get("public_longform_per_day")
    shorts = policy.get("public_shorts_per_day")
    if longs is None and shorts is None:
        return ""
    bits = []
    if longs is not None:
        bits.append(f"{longs} long{longs != 1 and 's' or ''}/día")
    if shorts is not None:
        bits.append(f"{shorts} short{shorts != 1 and 's' or ''}/día")
    return " · ".join(bits)


def _build_verdict(internal: dict, youtube: dict, studio_scan, delivery_state: str = "normal", policy: dict | None = None) -> dict:
    """Veredicto único por canal (fuente única de verdad para la UI).

    Escalera de prioridad de mayor a menor severidad:
      1. Evidencia EXTERNA real (removed / age_restricted / discrepancias de
         eliminación): algo se borró o restringió en YouTube.
      2. Hallazgos graves de Studio (strikes/avisos/desmonetización).
      3. Bloqueo interno temporal activo.
      4. Hallazgos leves de Studio / discrepancia de publicación (lag).
      5. Estado de entrega autoritativo (channel_delivery_state): normal →
         Operativo, recovery → Recuperación, strike → Modo strike.

    El estado de entrega (delivery_state) es la fuente que refleja la cadencia
    REAL que aplica el pipeline (resolve_channel_policy_values). Los marcadores
    históricos anti-spam (freq_reduced / fase de reanudación / strikes) NO se
    usan como motor del veredicto: son detalle de contexto, no el estado actual.
    """
    removed = youtube.get("removed") or []
    age_restricted = youtube.get("age_restricted") or []
    discrepancies = youtube.get("discrepancies") or []
    removed_disc = [d for d in discrepancies if str(d.get("type", "")).endswith("_removed")]
    sched_disc = [d for d in discrepancies if str(d.get("type", "")) == "bd_published_yt_scheduled"]

    findings = (studio_scan or {}).get("findings") or []
    scanned_at = (studio_scan or {}).get("scanned_at")
    studio_sev = _studio_severity(findings)

    blocked = bool(internal.get("blocked"))
    restan_h = internal.get("restan_h", 0.0)

    def _parts(items, label):
        if not items:
            return ""
        return f"{len(items)} {label}"

    parts = []
    if removed:
        parts.append(_parts(removed, "vídeo(s) no disponibles/eliminado(s) en YouTube"))
    if age_restricted:
        parts.append(_parts(age_restricted, "vídeo(s) con restricción de edad"))
    if removed_disc:
        parts.append(_parts(removed_disc, "discrepancia(s) de eliminación"))

    # 1. Evidencia externa real → algo se borró/restringió en YouTube.
    if removed or age_restricted or removed_disc:
        detail = " · ".join(p for p in parts if p)
        if youtube.get("checked_at"):
            detail += f" · verific. {youtube['checked_at'][:16]}"
        return {"severity": "critical", "label": "Posible eliminación en YouTube", "detail": detail}

    # 2. Hallazgos graves de Studio.
    if studio_sev == "critical":
        d = f"{len(findings)} aviso(s) en Studio (strike/políticas/desmonetización)"
        if scanned_at:
            d += f" · escaneado {scanned_at[:16]}"
        return {"severity": "critical", "label": "Avisos graves en YouTube Studio", "detail": d}

    # 3. Bloqueo interno temporal activo.
    if blocked:
        d = "No publicará contenido hasta el final del bloqueo interno de Autotube"
        if restan_h:
            d += f" (~{restan_h:.0f}h restantes)"
        d += " · no es una sanción confirmada por YouTube"
        return {"severity": "blocked", "label": "Bloqueo interno temporal", "detail": d}

    # 4. Hallazgos leves de Studio / discrepancia de publicación.
    if studio_sev == "warning":
        d = f"{len(findings)} aviso(s) leve(s) en Studio — revisar"
        if scanned_at:
            d += f" · escaneado {scanned_at[:16]}"
        return {"severity": "warning", "label": "Avisos en Studio — revisar", "detail": d}

    if sched_disc:
        return {
            "severity": "warning",
            "label": "Discrepancia de publicación",
            "detail": f"{len(sched_disc)} vídeo(s) que BD da por publicado aún aparecen programado/privado en YouTube (lag de indexado)",
        }

    # 5. Estado de entrega autoritativo.
    state = (delivery_state or "normal").lower()
    if state == "strike":
        return {"severity": "defensive", "label": "Modo strike — cadencia reducida",
                "detail": "Cadencia limitada por el perfil de cadencia (strike)."}
    if state == "recovery":
        return {"severity": "warning", "label": "Recuperación — cadencia parcial",
                "detail": "Cadencia parcial durante la recuperación tras los strikes."}
    # normal (default)
    cadence = _cadence_text(policy or {})
    return {"severity": "ok", "label": "Operativo",
            "detail": f"Estado normal · {cadence}" if cadence else "Estado normal"}


@router.get("/system/channel-restrictions")
def channel_restrictions():
    """Estado consolidado de restricciones por canal, para la barra unificada.

    Por cada canal ACTIVO devuelve tres bloques claramente separados:
      - internal  : protección interna de Autotube (bloqueo, strikes, frecuencia
                    rebajada, fase de reanudación). Es un TEMPORIZADOR interno,
                    NO una sanción confirmada por YouTube.
      - youtube   : verdad externa cacheada (yt_visibility de shorts recientes,
                    estado de vídeos, vídeos removed/age-restricted, y
                    discrepancias BD↔YouTube). Proviene del reconciliador (0 cuota).
      - studio_scan: hallazgos reales del último escaneo de YouTube Studio
                    (strikes/avisos de políticas/monetización a nivel de canal).
      - verdict   : veredicto único por canal (severity + label + detail),
                    calculado en servidor para no duplicar la escalera en la UI.

    Lectura ligera (0 cuota de YouTube API). Excluye canales inactivos/test.
    """
    from database.db_extended import ExtendedDatabase
    from api.services.spam_mitigation import build_spam_situation
    from api.services.gradual_resume import resume_status_detailed

    db = ExtendedDatabase()
    channels = db.get_channels(active_only=True) or []
    resume_map = {}
    try:
        for r in resume_status_detailed():
            resume_map[int(r.get("channel_id", 0))] = r
    except Exception:
        pass

    now_utc = __import__("datetime").datetime.now(__import__("datetime").timezone.utc)
    out = []
    for ch in channels:
        cid = int(ch["id"])
        slug = ch.get("slug", "")
        internal = {}
        try:
            sit = build_spam_situation(cid, db) or {}
            internal = {
                "blocked": bool(sit.get("blocked", False)),
                "blocked_until": sit.get("blocked_until"),
                "restan_h": sit.get("restan_h", 0.0),
                "strikes": sit.get("strikes", 0),
                "scope": sit.get("scope", "none"),
                "why": sit.get("why", ""),
                "freq_reduced": bool(sit.get("freq_reduced", False)),
                "current_freq": sit.get("current_freq"),
                "original_freq": sit.get("original_freq"),
                "pending_publish_total": (sit.get("pending_publish") or {}).get("total", 0),
            }
        except Exception:
            pass
        res = resume_map.get(cid) or {}
        internal["phase"] = res.get("phase_today", 0)
        internal["phase_label"] = res.get("phase_label", "")
        internal["phase_days_remaining"] = res.get("days_remaining_in_phase")
        internal["next_transition_iso"] = res.get("next_transition_iso")

        # ── Verdad externa cacheada (reconciliador) ──
        youtube = {"shorts": [], "videos": [], "age_restricted": [], "removed": [],
                   "discrepancies": [], "checked_at": None}
        try:
            with db._connect() as conn:
                shorts = conn.execute(
                    """SELECT id, youtube_id, title, status, published_at, publish_at,
                              yt_visibility, yt_checked_at
                       FROM shorts
                       WHERE channel_id=? AND youtube_id IS NOT NULL AND youtube_id != ''
                       ORDER BY id DESC LIMIT 8""",
                    (cid,),
                ).fetchall()
                videos = conn.execute(
                    """SELECT yt_video_id, titulo_final, status, uploaded_at
                       FROM videos
                       WHERE channel_id=? AND yt_video_id IS NOT NULL AND yt_video_id != ''
                       ORDER BY id DESC LIMIT 6""",
                    (cid,),
                ).fetchall()
            for s in shorts:
                vis = s["yt_visibility"] or "unknown"
                entry = {
                    "id": s["id"], "youtube_id": s["youtube_id"],
                    "title": (s["title"] or "")[:60], "visibility": vis,
                    "published_at": s["published_at"], "publish_at": s["publish_at"],
                }
                youtube["shorts"].append(entry)
                if vis in ("age_restricted",):
                    youtube["age_restricted"].append(entry)
                # Solo un estado explícitamente "removed" (doble confirmación del
                # reconciliador) es retirada. "unavailable" / "unknown" / "error" son
                # ambigüedades operativas y nunca deben tratarse como eliminación.
                if vis == "removed":
                    youtube["removed"].append(entry)
                # Discrepancia de verdad externa. Con v48, status='published' solo
                # debería darse cuando YouTube confirma público, y 'scheduled' es el
                # estado esperado de un short subido private+publishAt aún no publicado.
                # removed (confirmado) es discrepancia (BD lo daba por publicado o
                # programado pero YT lo eliminó); y published→scheduled/private solo si
                # el reconciliador aún no degradó (retraso puntual). unavailable NO es
                # discrepancia de eliminación: sigue siendo ambiguo.
                if s["status"] == "scheduled":
                    if vis == "removed":
                        youtube["discrepancies"].append({
                            "type": "bd_scheduled_yt_removed",
                            "youtube_id": s["youtube_id"], "title": entry["title"],
                            "publish_at": s["publish_at"],
                        })
                elif s["status"] == "published":
                    if vis in ("scheduled", "private"):
                        youtube["discrepancies"].append({
                            "type": "bd_published_yt_scheduled",
                            "youtube_id": s["youtube_id"], "title": entry["title"],
                            "publish_at": s["publish_at"],
                        })
                    elif vis == "removed":
                        youtube["discrepancies"].append({
                            "type": "bd_published_yt_removed",
                            "youtube_id": s["youtube_id"], "title": entry["title"],
                        })
            youtube["videos"] = [dict(v) for v in videos]
            checked = [s["yt_checked_at"] for s in shorts if s["yt_checked_at"]]
            youtube["checked_at"] = max(checked) if checked else None
        except Exception:
            pass

        # ── Hallazgos del último escaneo de YouTube Studio (real, a nivel de canal) ──
        studio_scan = None
        try:
            raw = db.get_system_state(f"studio_scan_{slug}")
            if raw:
                parsed = json.loads(raw)
                if isinstance(parsed, dict):
                    studio_scan = {
                        "status": parsed.get("status"),
                        "channel": parsed.get("channel"),
                        "findings": parsed.get("findings") or [],
                        "scanned_at": parsed.get("scanned_at"),
                    }
        except Exception:
            studio_scan = None

        # ── Estado de entrega autoritativo + cadencia efectiva resuelta ──
        try:
            from api.services.channel_policy import (
                get_channel_delivery_state, resolve_channel_policy_values,
            )
            delivery_state = get_channel_delivery_state(cid, db)
            policy = resolve_channel_policy_values(cid, db=db) or {}
        except Exception:
            delivery_state = "normal"
            policy = {}

        verdict = _build_verdict(internal, youtube, studio_scan, delivery_state, policy)

        out.append({
            "channel_id": cid, "slug": slug, "name": ch.get("name", ""),
            "internal": internal, "youtube": youtube,
            "studio_scan": studio_scan,
            "delivery_state": delivery_state,
            "policy": {
                "longs_per_day": policy.get("public_longform_per_day"),
                "shorts_per_day": policy.get("public_shorts_per_day"),
            },
            "verdict": verdict,
        })

    return {"ok": True, "generated_at": now_utc.isoformat(), "channels": out}


@router.post("/system/studio-scan")
async def studio_scan(channel_id: int):
    """Escaneo on-demand de YouTube Studio para un canal.

    Ejecuta en paralelo:
      1. Reconciliación pasiva inmediata (yt-dlp + RSS, 0 cuota) del estado real
         de los shorts del canal.
      2. Un escaneo de Studio con el perfil del account (lee avisos de políticas,
         strikes, restricciones de edad a nivel de canal) si el perfil está libre.

    Devuelve el resultado del escaneo (o 'in_use'/'skipped' si el perfil está
    ocupado). Los hallazgos de Studio quedan en system_state['studio_scan_<slug>'].
    """
    import asyncio
    from database.db_extended import ExtendedDatabase
    from api.services.yt_state_reconciler import (
        reconcile_recent_shorts, scan_studio_for_channel,
    )

    db = ExtendedDatabase()

    # 1. Reconciliación pasiva inmediata (0 cuota).
    passive = await asyncio.to_thread(reconcile_recent_shorts, db)

    # 2. Escaneo Studio on-demand (solo si el perfil está libre).
    studio = await asyncio.to_thread(scan_studio_for_channel, db, channel_id)

    return {
        "ok": True,
        "channel_id": channel_id,
        "passive_reconcile": passive,
        "studio_scan": studio,
    }


@router.post("/system/resume/apply")
def resume_apply():
    """Re-aplica las fases de reanudación hoy (+ replan del horizonte de 7 días).

    Equivale al CLI `scripts/gradual_resume.py --apply`. Idempotente: solo
    avanza fases y respeta el techo antiban. Devuelve el detalle por canal.
    """
    from api.services.gradual_resume import apply_resume_phases
    return apply_resume_phases(replan=True)


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
