"""Cobertura diaria de publicación — enforcer de la programación (ago 2026).

Garantiza que cada canal libre tenga ``max_longform_publish_day`` vídeos con
publicación programada en los próximos días (los que le tocan). Es la pieza que
hace que "lo planeado se cumpla": audita la cobertura y, si hay un día con hueco
y existen vídeos pendientes, dispara el repack del canal (``apply_publish_repack``,
que re-espacia 1/día y reprograma en YouTube vía videos.update). Si el canal está
seco (sin vídeos pendientes), crea una alerta 1/día para que la cobertura de
GENERACIÓN (``recovery_planner``) lo resuelva — el enforcer no inventa contenido.

Por qué existe (bug de ago 2026): la publicación depende 100 % del ``publishAt``
de YouTube y no había ningún componente que verificase "¿este canal tiene su
hueco de hoy/mañana cubierto?". El repack solo reaccionaba a síntomas (target
lejano, colisión, retenido) y con starvation de canales; el planning podía dejar
días vacíos sin que nadie los detectara. Este módulo convierte la cobertura en
una invariante auditada periódicamente.

Respetos (heredados): scheduler_paused, spam-block por canal, cuota por proyecto
(quota_gate de apply_publish_repack) y el cap diario del perfil de pacing.
"""

import json
import logging
import threading
from datetime import datetime, timedelta, timezone

logger = logging.getLogger("autotube.publish_coverage")

_PENDING_STATUSES = (
    "uploaded_private", "warming", "scheduled", "awaiting_upload", "ready",
)
_ALERT_DRY_PREFIX = "publish_coverage_dry_"

# Guard contra ejecuciones concurrentes (el repack ya corre cada minuto desde
# upload_scheduler; aquí solo disparamos, nunca en paralelo con nosotros mismos).
_RUN_LOCK = threading.Lock()


def _db_instance():
    from config.settings import DATABASE_PATH
    from database.db_extended import ExtendedDatabase
    return ExtendedDatabase(str(DATABASE_PATH))


def _channel_pending(db, channel_id: int) -> list[dict]:
    """Vídeos pendientes de publicación del canal (scheduled)."""
    try:
        with db._connect() as conn:
            rows = conn.execute(
                """SELECT v.id, v.status, v.target_public_at, v.yt_video_id,
                          v.scheduled_upload_at
                   FROM videos v
                   WHERE v.channel_id = ?
                     AND v.publish_mode = 'scheduled'
                     AND v.status IN ('uploaded_private','warming','scheduled',
                                      'awaiting_upload','ready')
                   ORDER BY COALESCE(v.uploaded_at, v.scheduled_upload_at, v.created_at)
                """, (channel_id,),
            ).fetchall()
        return [dict(r) for r in rows]
    except Exception as exc:
        logger.debug("[coverage] pending scan skipped: %s", exc)
        return []


def _channel_coverage_by_day(pending: list[dict], tz) -> dict:
    """{fecha local: nº de vídeos} para los targets futuros de la cola."""
    from pipeline.publish_scheduler import _parse_target_public_at
    coverage: dict = {}
    for v in pending:
        raw = v.get("target_public_at")
        if not raw:
            continue
        parsed = _parse_target_public_at(str(raw), str(tz))
        if parsed is None:
            continue
        d = parsed.astimezone(tz).date()
        coverage[d] = coverage.get(d, 0) + 1
    return coverage


def _maybe_alert_dry(db, slug: str, channel_id: int | None = None) -> bool:
    """Alerta (deduplicada 1/día) si el canal no tiene nada pendiente de publicar."""
    try:
        today = datetime.now().date().isoformat()
        key = f"{_ALERT_DRY_PREFIX}{slug}"
        if db.get_system_state(key) == today:
            return False
        db.set_system_state(key, today)
        from api.services.lifecycle_monitor import create_alert
        create_alert(
            db,
            entity_type="channel", entity_id=channel_id, channel_id=channel_id,
            alert_type="publish_coverage_dry",
            severity="warning",
            title=f"[{slug}] Cobertura de publicación: canal seco",
            message=(
                f"[{slug}] 0 vídeos pendientes de publicar en los próximos días. "
                f"La publicación no tiene nada que programar: revisar la cobertura "
                f"de GENERACIÓN (recovery_planner) o el backlog de awaiting_upload."
            ),
            metadata={"slug": slug},
        )
        return True
    except Exception as exc:
        logger.debug("[%s] dry alert skip: %s", slug, exc)
        return False


def _resolve_dry_alert(db, slug: str, channel_id: int | None = None) -> int:
    """Close only this channel's dry alert once pending work exists again."""
    resolved = 0
    try:
        with db._connect() as conn:
            rows = conn.execute(
                """SELECT id, message FROM pipeline_alerts
                   WHERE entity_type='channel' AND entity_id=?
                     AND alert_type='publish_coverage_dry' AND resolved=0""",
                (channel_id,),
            ).fetchall()
            for row in rows:
                conn.execute(
                    """UPDATE pipeline_alerts SET resolved=1,
                       resolved_at=datetime('now'), acknowledged=1,
                       message=COALESCE(message, '') ||
                       ' [Auto-resuelto: cobertura de publicación recuperada]'
                       WHERE id=? AND resolved=0""", (row["id"],),
                )
                resolved += 1
            if resolved:
                conn.commit()
    except Exception as exc:
        logger.debug("[%s] dry alert resolve skip: %s", slug, exc)
    return resolved


def ensure_daily_publish_coverage(db=None, horizon_days: int = 2,
                                  max_channels: int = 8) -> dict:
    """Audita y rellena la cobertura de publicación de todos los canales libres.

    Args:
        db: ExtendedDatabase (o None → lazy).
        horizon_days: días hacia delante a auditar (2 por defecto).
        max_channels: límite de repacks disparados por pasada (cuota).

    Returns:
        dict con {channels: {slug: {...}}, repacked, alerted_dry, skipped}.
    """
    if db is None:
        db = _db_instance()
    if not _RUN_LOCK.acquire(blocking=False):
        logger.debug("Publish coverage: pasada anterior aún en curso — skip")
        return {"channels": {}, "repacked": 0, "alerted_dry": 0, "skipped": 1}

    result: dict = {"channels": {}, "repacked": 0, "alerted_dry": 0, "skipped": 0}
    try:
        now_utc = datetime.now(timezone.utc)

        # ── Gate global ──
        try:
            if db.get_system_state("scheduler_paused") == "true":
                result["skipped"] += 1
                return result
        except Exception:
            pass

        try:
            channels = db.get_channels(active_only=True) or []
        except Exception:
            return result
        channels = [c for c in channels if c.get("slug") != "test"]

        repacked_in_run = 0
        for ch in channels:
            ch_id = int(ch["id"])
            slug = ch.get("slug", f"canal{ch_id}")

            # ── Gates por canal ──
            try:
                if db.is_channel_spam_blocked(ch_id):
                    result["skipped"] += 1
                    continue
            except Exception:
                pass
            try:
                from api.services.quota_tracker import is_quota_exhausted_for_channel
                if is_quota_exhausted_for_channel(slug):
                    result["skipped"] += 1
                    continue
            except Exception:
                pass

            # ── Cuota diaria (perfil pacing) ──
            try:
                from api.services.pacing_profile import get_pacing_value
                n = int(get_pacing_value(
                    "max_longform_publish_day", default=1, db=db,
                ) or 1)
            except Exception:
                n = 1
            n = max(1, n)

            # ── Zona horaria del canal ──
            try:
                cfg = {}
                if ch.get("config_json"):
                    cfg = json.loads(ch["config_json"] or "{}")
                tz_str = cfg.get("PUBLISH_TIMEZONE", "Europe/Madrid")
                import pytz
                tz = pytz.timezone(tz_str)
            except Exception:
                tz = timezone.utc

            # Pico del canal (para no auditar "hoy" si el pico ya pasó: un día
            # cuyo pico ha pasado NO es rellenable y no debe disparar repack).
            peak_hour = None
            try:
                from pipeline.publish_scheduler import get_channel_peak_info
                _info = get_channel_peak_info(cfg)
                peak_hour = int(_info.get("peak_hour", 0) or 0)
            except Exception:
                pass

            pending = _channel_pending(db, ch_id)
            coverage = _channel_coverage_by_day(pending, tz)

            today_local = now_utc.astimezone(tz).date()
            days = []
            for i in range(horizon_days):
                d = today_local + timedelta(days=i)
                if (i == 0 and peak_hour is not None
                        and now_utc.astimezone(tz).hour >= peak_hour):
                    continue  # pico de hoy ya pasado → no rellenable
                days.append((d, coverage.get(d, 0)))

            deficit_days = [str(d) for d, c in days if c < n]
            reason = "cobertura OK"
            triggered = False

            if not pending:
                if _maybe_alert_dry(db, slug, channel_id=ch_id):
                    result["alerted_dry"] += 1
                reason = "seco (sin vídeos pendientes)"
            else:
                _resolve_dry_alert(db, slug, channel_id=ch_id)
            if pending and deficit_days and repacked_in_run < max_channels:
                triggered = True
                reason = f"déficit en {len(deficit_days)} día(s): {deficit_days}"
                try:
                    from api.services.publish_repack import apply_publish_repack
                    res = apply_publish_repack(
                        db, ch_id, slug, dry_run=False, quota_gate=True,
                    )
                    repacked_in_run += 1
                    result["repacked"] += 1
                    reason += (f" | repack: {res.get('rescheduled', 0)} reprogramados, "
                               f"{res.get('no_change', 0)} sin cambio")
                    if res.get("quota_skipped"):
                        reason += " (cuota insuficiente — se reintentará)"
                except Exception as exc:
                    logger.warning("[%s] publish coverage repack failed: %s", slug, exc)
                    reason += " | ERROR repack"
            elif deficit_days:
                reason = f"déficit pero límite de {max_channels} canales/pasada alcanzado"

            result["channels"][slug] = {
                "pending": len(pending),
                "quota_per_day": n,
                "coverage": {str(d): c for d, c in days},
                "deficit_days": deficit_days,
                "triggered": triggered,
                "reason": reason,
            }

        logger.info(
            "Publish coverage: %d canal(es) auditados, %d repackeados, %d alertas secas, %d saltados",
            len(result["channels"]), result["repacked"],
            result["alerted_dry"], result["skipped"],
        )
        return result
    finally:
        _RUN_LOCK.release()
