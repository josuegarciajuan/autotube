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
_ALERT_DEFICIT_PREFIX = "publish_coverage_deficit_"

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


def _published_today_count(db, channel_id: int, tz) -> int:
    """Nº de vídeos ya publicados HOY (fecha local del canal). 0 cuota.

    La cola pendiente deja de contar un vídeo en cuanto se publica; sin esto,
    un canal cuyo vídeo diario se publica de madrugada (p. ej. 03:00 UTC) aparecía
    con "hueco de hoy" en cada auditoría posterior → alerta de déficit diaria
    espuria (bug canal4, sep 2026).
    """
    try:
        from api.time_utils import parse_utc
        with db._connect() as conn:
            rows = conn.execute(
                """SELECT published_at FROM videos
                   WHERE channel_id = ? AND status = 'published'
                     AND published_at IS NOT NULL
                     AND published_at >= datetime('now', '-2 days')""",
                (channel_id,),
            ).fetchall()
        today = datetime.now(timezone.utc).astimezone(tz).date()
        count = 0
        for row in rows:
            published = parse_utc(row["published_at"])
            if published is not None and published.astimezone(tz).date() == today:
                count += 1
        return count
    except Exception as exc:
        logger.debug("[coverage] published-today scan skipped: %s", exc)
        return 0


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


def _maybe_alert_deficit(db, slug: str, channel_id: int | None,
                         deficit_days: list[str]) -> bool:
    """Alerta de sistema (deduplicada 1/día) cuando un día tiene hueco de
    publicación. AUDITORÍA SOLO: no corrige nada automáticamente (el repack
    automático se eliminó, ago 2026). El operador decide cómo resolverlo."""
    try:
        today = datetime.now().date().isoformat()
        key = f"{_ALERT_DEFICIT_PREFIX}{slug}"
        if db.get_system_state(key) == today:
            return False
        db.set_system_state(key, today)
        from api.services.lifecycle_monitor import create_alert
        create_alert(
            db,
            entity_type="channel", entity_id=channel_id, channel_id=channel_id,
            alert_type="publish_coverage_deficit",
            severity="warning",
            title=f"[{slug}] Cobertura de publicación: días con hueco",
            message=(
                f"[{slug}] Déficit de publicación en {len(deficit_days)} día(s): "
                f"{', '.join(deficit_days)}. Revisar la programación de este canal "
                f"(corregir a mano si procede)."
            ),
            metadata={"slug": slug, "deficit_days": deficit_days},
        )
        return True
    except Exception as exc:
        logger.debug("[%s] deficit alert skip: %s", slug, exc)
        return False


def _resolve_deficit_alert(db, slug: str, channel_id: int | None) -> int:
    """Close this channel's deficit alert once no day is left with a gap."""
    resolved = 0
    try:
        with db._connect() as conn:
            rows = conn.execute(
                """SELECT id, message FROM pipeline_alerts
                   WHERE entity_type='channel' AND entity_id=?
                     AND alert_type='publish_coverage_deficit' AND resolved=0""",
                (channel_id,),
            ).fetchall()
            for row in rows:
                conn.execute(
                    """UPDATE pipeline_alerts SET resolved=1,
                       resolved_at=datetime('now'), acknowledged=1,
                       message=COALESCE(message, '') ||
                       ' [Auto-resuelto: cobertura recuperada]'
                       WHERE id=? AND resolved=0""", (row["id"],),
                )
                resolved += 1
            if resolved:
                conn.commit()
    except Exception as exc:
        logger.debug("[%s] deficit alert resolve skip: %s", slug, exc)
    return resolved


# ── Cumplimiento diario de públicos (regla dura, sep 2026) ─────────────
# Si a lo largo del día el canal va por debajo de su cap de públicos y hay
# vídeos ya subidos (warming) que no están comprometidos para hoy, se adelanta
# su publishAt para CUMPLIR el plan hoy. Es la remediación que rompe el bucle
# "el plan no se cumple y nadie lo arregla".
_CATCHUP_STAGGER_MIN = 10


def remediate_today_public_deficit(db, slug: str, channel_id: int, tz,
                                   cap: int) -> dict:
    """Fuerza la publicación de warming para cubrir el déficit de HOY.

    Returns: {published, committed, need, forced, reason}.
    """
    from api.time_utils import parse_utc
    out = {"published": 0, "committed": 0, "need": 0, "forced": 0, "reason": ""}
    if not cap or cap <= 0:
        return out
    now = datetime.now(timezone.utc)
    now_local = now.astimezone(tz)
    today_local = now_local.date()

    try:
        with db._connect() as conn:
            rows = conn.execute(
                """SELECT id, status, yt_video_id, target_public_at,
                          published_at, uploaded_at, created_at
                   FROM videos
                   WHERE channel_id=?
                     AND status IN ('published','uploaded_private','warming','scheduled')
                   ORDER BY COALESCE(uploaded_at, created_at) ASC""",
                (channel_id,),
            ).fetchall()
    except Exception as exc:
        out["reason"] = f"scan failed: {exc}"
        return out

    candidates = []
    for r in rows:
        status = r["status"]
        target = parse_utc(r["target_public_at"])
        if status == "published":
            pub = parse_utc(r["published_at"])
            if pub is not None and pub.astimezone(tz).date() == today_local:
                out["published"] += 1
            continue
        if not r["yt_video_id"]:
            continue
        if target is not None and target.astimezone(tz).date() == today_local:
            out["committed"] += 1
            continue
        candidates.append((int(r["id"]), str(r["yt_video_id"]), status))

    out["need"] = max(0, int(cap) - (out["published"] + out["committed"]))
    if out["need"] <= 0:
        out["reason"] = "cobertura de hoy OK"
        return out
    if not candidates:
        out["reason"] = "déficit sin material warming"
        return out

    # Autenticación perezosa (una sola vez por canal)
    try:
        from pipeline.youtube_uploader import YouTubeUploader
        uploader = YouTubeUploader(slug)
        if not uploader.authenticate():
            out["reason"] = "auth fallida"
            return out
    except Exception as exc:
        out["reason"] = f"auth error: {exc}"
        return out

    base = now + timedelta(minutes=5 + (channel_id % 4) * 3)
    for idx, (vid, yt_id, _status) in enumerate(candidates[: out["need"]]):
        target_dt = base + timedelta(minutes=idx * _CATCHUP_STAGGER_MIN)
        target_iso = target_dt.astimezone(timezone.utc).isoformat()
        try:
            res = uploader.set_publish_at(yt_id, target_iso)
            if not res.get("updated"):
                raise RuntimeError(f"respuesta inesperada: {res}")
            with db._connect() as conn:
                conn.execute(
                    "UPDATE videos SET target_public_at=? WHERE id=?",
                    (target_iso, vid),
                )
                conn.commit()
            out["forced"] += 1
            logger.info(
                "[%s] Public catch-up: #%d (%s) publishAt → %s (déficit hoy)",
                slug, vid, yt_id, target_iso[:16],
            )
        except Exception as exc:
            logger.warning(
                "[%s] Public catch-up: no se pudo adelantar #%d (%s): %s",
                slug, vid, yt_id, exc,
            )
    out["reason"] = f"déficit {out['need']} → forzados {out['forced']}"
    return out


def ensure_daily_publish_coverage(db=None, horizon_days: int = 2,
                                  max_channels: int = 8) -> dict:
    """Audita la cobertura de publicación de todos los canales libres.

    AUDITORÍA SOLO (ago 2026): ya NO dispara repack automático (el repack por
    timer se eliminó porque drenaba la cuota). Solo comprueba si cada día
    próximo tiene cobertura y, si hay hueco, genera una alerta de sistema
    (`publish_coverage_deficit`) para revisión posterior. 0 cuota de YouTube.

    Args:
        db: ExtendedDatabase (o None → lazy).
        horizon_days: días hacia delante a auditar (2 por defecto).
        max_channels: conservado por compatibilidad de firma (sin efecto).

    Returns:
        dict con {channels: {slug: {...}}, alerted_deficit, alerted_dry, skipped}.
    """
    if db is None:
        db = _db_instance()
    if not _RUN_LOCK.acquire(blocking=False):
        logger.debug("Publish coverage: pasada anterior aún en curso — skip")
        return {"channels": {}, "alerted_deficit": 0, "alerted_dry": 0, "skipped": 1}

    result: dict = {"channels": {}, "alerted_deficit": 0, "alerted_dry": 0, "skipped": 0}
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

            # ── Cuota diaria por canal (Configuración de Programación) ──
            # Fuente de verdad: el cap configurado del canal (override del
            # panel / perfil), NO el valor global del perfil.
            try:
                from api.services.channel_policy import policy_value
                _cap = policy_value(
                    ch_id, "longform_publish_cap", db=db, default=1,
                )
                # Un cap configurado a 0 es una PAUSA intencional del canal,
                # no un valor ausente: no lo coerciones a 1.
                n = 1 if _cap is None else int(_cap)
            except Exception:
                n = 1
            n = max(0, n)

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
            # HOY ya cubierto por un vídeo publicado hoy cuenta como cobertura:
            # la cola pendiente lo excluye tras publicarse.
            published_today = _published_today_count(db, ch_id, tz)
            days = []
            for i in range(horizon_days):
                d = today_local + timedelta(days=i)
                if (i == 0 and peak_hour is not None
                        and now_utc.astimezone(tz).hour >= peak_hour):
                    continue  # pico de hoy ya pasado → no rellenable
                count = coverage.get(d, 0)
                if i == 0:
                    count += published_today
                days.append((d, count))

            deficit_days = [str(d) for d, c in days if c < n]
            reason = "cobertura OK"
            triggered = False

            if not pending:
                if _maybe_alert_dry(db, slug, channel_id=ch_id):
                    result["alerted_dry"] += 1
                reason = "seco (sin vídeos pendientes)"
            else:
                _resolve_dry_alert(db, slug, channel_id=ch_id)

            # ── Enforcer de cobertura: alerta + repack con gate de cuota ──
            # Si un día próximo queda por debajo del cap configurado y hay cola
            # pendiente, se dispara el repack del canal (apply_publish_repack con
            # quota_gate=True) para CUMPLIR la cadencia configurada. El gate de
            # cuota evita el ping-pong de set_publish_at cuando no hay margen.
            if pending and deficit_days:
                triggered = True
                reason = f"déficit en {len(deficit_days)} día(s): {deficit_days}"
                try:
                    from api.services.publish_repack import apply_publish_repack
                    _rep = apply_publish_repack(db, ch_id, slug, dry_run=False,
                                                quota_gate=True)
                    _moved = int((_rep or {}).get("rescheduled", 0) or 0)
                    if _moved:
                        result["repacked"] = result.get("repacked", 0) + _moved
                        reason += f" — repack: {_moved} reprogramado(s)"
                    elif (_rep or {}).get("quota_skipped"):
                        reason += " — repack omitido por cuota"
                except Exception as exc:
                    logger.debug("[%s] coverage repack skipped: %s", slug, exc)
                if _maybe_alert_deficit(db, slug, channel_id=ch_id, deficit_days=deficit_days):
                    result["alerted_deficit"] = result.get("alerted_deficit", 0) + 1
                    reason += " — alerta generada"
            else:
                _resolve_deficit_alert(db, slug, channel_id=ch_id)

            # ── Cumplimiento diario: forzar warming para cubrir HOY ──
            today_info = {"published": 0, "committed": 0, "need": 0, "forced": 0,
                          "reason": "skip"}
            try:
                today_info = remediate_today_public_deficit(
                    db, slug, ch_id, tz, n,
                )
                if today_info.get("forced"):
                    result["public_catchup_forced"] = (
                        result.get("public_catchup_forced", 0)
                        + int(today_info["forced"])
                    )
            except Exception as exc:
                logger.debug("[%s] today public catch-up skipped: %s", slug, exc)

            result["channels"][slug] = {
                "pending": len(pending),
                "quota_per_day": n,
                "coverage": {str(d): c for d, c in days},
                "deficit_days": deficit_days,
                "triggered": triggered,
                "reason": reason,
                "today": today_info,
            }

        logger.info(
            "Publish coverage: %d canal(es) auditados, %d déficits alertados, %d alertas secas, %d saltados",
            len(result["channels"]), result.get("alerted_deficit", 0),
            result["alerted_dry"], result["skipped"],
        )
        return result
    finally:
        _RUN_LOCK.release()
