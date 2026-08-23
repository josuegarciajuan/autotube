"""Guard genérico anti-bucle de reintentos contra YouTube (antiban, ago 2026).

Principio (operativo): NINGUNA acción contra YouTube puede reintentar en bucle
infinito. Es una señal de automatización que YouTube penaliza y una pérdida de
recursos. Las condiciones *transient* (cap diario de cuenta, cuota agotada) se
DIFIEREN al siguiente día PT (reset natural); los fallos persistentes agotan un
presupuesto de intentos y entonces la acción queda en estado ``held`` (retenida,
pendiente de revisión) y se crea una ALERTA para que un humano investigue. Nunca
se reintenta en silencio contra la API.

Uso:
    from api.services.yt_retry_guard import (
        next_pt_day_retry_str, failed_attempts, hold_video,
    )
    # worker de subida, tras denegación transient:
    db.update_video(video_id, status="awaiting_upload",
                    scheduled_upload_at=next_pt_day_retry_str())
    # scan del scheduler, al detectar presupuesto agotado:
    hold_video(db, video_id, slug, "upload", "3 subidas fallidas seguidas")
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

logger = logging.getLogger("autotube.yt_retry_guard")

# Presupuesto de reintentos por acción (fallos sin éxito) antes de HELD + alerta.
DEFAULT_MAX_ATTEMPTS = 3
# Margen (min) tras el reset PT para reintentar (dar margen a que resete la
# cuota/cap sin competir con el primer slot de la mañana).
PT_RESET_BUFFER_MIN = 60


def next_pt_day_retry_str(buffer_min: int = PT_RESET_BUFFER_MIN) -> str:
    """Siguiente reset de día PT + margen, en hora local del servidor.

    El cap de subidas por cuenta Google (ACCOUNT_DAILY_UPLOAD_CAP) y la cuota
    de YouTube Data API resetean a medianoche PT (= 07:00 UTC). Devuelve el
    reset + buffer en el formato local ('YYYY-MM-DD HH:MM:SS') que usa
    ``videos.scheduled_upload_at`` (hora del servidor), de modo que el
    scheduler NO vuelva a elegir el vídeo hasta entonces → fin del bucle.
    """
    try:
        now_utc = datetime.now(timezone.utc)
        pt = now_utc.astimezone(ZoneInfo("America/Los_Angeles"))
        next_midnight_pt = (pt + timedelta(days=1)).replace(
            hour=0, minute=0, second=0, microsecond=0,
        )
        retry_utc = next_midnight_pt.astimezone(timezone.utc) + timedelta(minutes=buffer_min)
        return retry_utc.astimezone().strftime("%Y-%m-%d %H:%M:%S")
    except Exception as exc:
        # Fallback seguro: mañana a la misma hora local (nunca "ya").
        logger.warning("next_pt_day_retry_str fallback: %s", exc)
        return (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")


def failed_attempts(db, video_id: int, action: str = "upload_only") -> int:
    """Intentos fallidos acumulados de una acción sobre un vídeo (0 si error)."""
    try:
        with db._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) c FROM generation_jobs "
                "WHERE video_id = ? AND action = ? AND status = 'failed'",
                (video_id, action),
            ).fetchone()
        return int(row[0]) if row else 0
    except Exception:
        return 0


def hold_video(db, video_id: int, slug: str, action: str, reason: str) -> bool:
    """Marca un vídeo como ``held`` (retenido) + alerta con contexto diagnóstico.

    Principio antibucle: cuando una acción contra YouTube agota su presupuesto
    de intentos se detiene, se deja la acción pendiente (status='held', que
    ningún scheduler recoge) y se crea una alerta RICA EN DATOS para que el
    operador pueda ir a la causa raíz sin cavar: error real del último intento,
    yt_video_id, canal, target_public_at y el log del último job fallido.
    ``create_alert`` deduplica por (entity_type, entity_id, alert_type), así
    que no spamea en cada pasada.

    Returns:
        True si se marcó held.
    """
    # ── 0. Contexto diagnóstico (ANTES de sobrescribir error_message) ──
    meta: dict = {
        "video_id": video_id, "slug": slug, "action": action, "reason": reason,
    }
    try:
        with db._connect() as conn:
            row = conn.execute(
                """SELECT v.titulo_final, v.yt_video_id, v.channel_id,
                          v.error_message, v.target_public_at, v.uploaded_at,
                          (SELECT COUNT(*) FROM generation_jobs gj
                            WHERE gj.video_id = v.id AND gj.action = ?
                              AND gj.status = 'failed') AS failed_count,
                          (SELECT gj.id FROM generation_jobs gj
                            WHERE gj.video_id = v.id AND gj.action = ?
                              AND gj.status = 'failed'
                            ORDER BY gj.id DESC LIMIT 1) AS last_job_id,
                          (SELECT gj.error_msg FROM generation_jobs gj
                            WHERE gj.video_id = v.id AND gj.action = ?
                              AND gj.status = 'failed'
                            ORDER BY gj.id DESC LIMIT 1) AS last_job_error
                   FROM videos v WHERE v.id = ?""",
                (action, action, action, video_id),
            ).fetchone()
        if row:
            meta.update({
                "title": (row["titulo_final"] or "")[:80],
                "yt_video_id": row["yt_video_id"] or "",
                "channel_id": row["channel_id"],
                "last_error": (row["error_message"] or "")[:300],
                "target_public_at": row["target_public_at"],
                "uploaded_at": row["uploaded_at"],
                "failed_count": int(row["failed_count"] or 0),
                "last_job_id": row["last_job_id"],
                "last_job_error": (row["last_job_error"] or "")[:300],
            })
    except Exception as exc:
        logger.debug("hold_video: context fetch failed #%d: %s", video_id, exc)

    # ── 1. Marcar held. error_message conserva el error real (si lo había) ──
    real_error = meta.get("last_error") or reason
    try:
        with db._connect() as conn:
            conn.execute(
                "UPDATE videos SET status='held', progress_phase='held', "
                "error_message = ? WHERE id = ?",
                ((real_error or reason)[:400], video_id),
            )
            conn.commit()
    except Exception as exc:
        logger.warning("hold_video: DB update failed #%d: %s", video_id, exc)
        return False

    # ── 2. Alerta enriquecida ──
    try:
        from api.services.lifecycle_monitor import create_alert
        title_txt = meta.get("title") or f"Vídeo #{video_id}"
        yt_txt = meta.get("yt_video_id") or "—"
        attempts = meta.get("failed_count") or DEFAULT_MAX_ATTEMPTS
        job_txt = ""
        if meta.get("last_job_id"):
            job_txt = (
                f"\nÚltimo job fallido: #{meta['last_job_id']} "
                f"(log: logs/worker_{meta['last_job_id']}.log)"
            )
            if meta.get("last_job_error"):
                job_txt += f" — {meta['last_job_error']}"
        create_alert(
            db,
            entity_type="video",
            entity_id=video_id,
            channel_id=meta.get("channel_id"),
            alert_type=f"{action}_retries_exhausted",
            severity="warning",
            title=f"Vídeo #{video_id} retenido (held): {slug} agotó reintentos de {action}",
            message=(
                f"Vídeo '{title_txt}' (#{video_id}, canal {slug}, channel_id="
                f"{meta.get('channel_id') or '—'}) retenido tras {attempts} intentos "
                f"de '{action}' sin éxito.\n"
                f"Último error real: {real_error}\n"
                f"yt_video_id: {yt_txt}\n"
                f"target_public_at: {meta.get('target_public_at') or '—'}\n"
                f"Motivo del hold: {reason}{job_txt}\n"
                f"El vídeo queda en 'held' y NO se reintenta automáticamente. "
                f"Causas posibles: cuota agotada, cap diario de cuenta, auth, "
                f"contenido rechazado. Revisa la causa raíz y resuélvela antes de retomar."
            ),
            metadata=meta,
        )
    except Exception as exc:
        logger.warning("hold_video: alert failed #%d: %s", video_id, exc)

    logger.warning("⛔ [%s] Video #%d HELD — reintentos de '%s' agotados: %s",
                   slug, video_id, action, reason)
    return True
