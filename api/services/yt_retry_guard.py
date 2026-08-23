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
    """Marca un vídeo como ``held`` (retenido) + alerta. NO reintenta más.

    Principio antibucle: cuando una acción contra YouTube agota su presupuesto
    de intentos se detiene, se deja la acción pendiente (status='held', que
    ningún scheduler recoge) y se crea una alerta. ``create_alert`` deduplica
    por (entity_type, entity_id, alert_type), así que no spamea en cada pasada.

    Returns:
        True si se marcó held.
    """
    try:
        with db._connect() as conn:
            conn.execute(
                "UPDATE videos SET status='held', progress_phase='held', "
                "error_message = ? WHERE id = ?",
                (reason[:400], video_id),
            )
            conn.commit()
    except Exception as exc:
        logger.warning("hold_video: DB update failed #%d: %s", video_id, exc)
        return False

    try:
        from api.services.lifecycle_monitor import create_alert
        create_alert(
            db,
            entity_type="video",
            entity_id=video_id,
            channel_id=None,
            alert_type=f"{action}_retries_exhausted",
            severity="warning",
            title=f"Vídeo #{video_id} retenido (held): {slug} agotó reintentos de {action}",
            message=(
                f"La acción '{action}' contra YouTube falló {DEFAULT_MAX_ATTEMPTS} veces "
                f"sin éxito para el vídeo #{video_id} ({slug}). Motivo: {reason}. "
                f"El vídeo queda en estado 'held' (pendiente) y NO se reintentará "
                f"automáticamente. Revisa la causa raíz (cuota, cap, auth, contenido) "
                f"y resuélvela manualmente antes de retomar."
            ),
            metadata={"video_id": video_id, "slug": slug, "action": action, "reason": reason},
        )
    except Exception as exc:
        logger.warning("hold_video: alert failed #%d: %s", video_id, exc)

    logger.warning("⛔ [%s] Video #%d HELD — reintentos de '%s' agotados: %s",
                   slug, video_id, action, reason)
    return True
