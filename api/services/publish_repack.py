"""Publish repack service — reprogramar publicaciones de un canal con gaps >=3h.

(ago 2026) El clamp con cap duro de 24h apilaba muchos vídeos del mismo canal en
el mismo instante cuando el backlog era denso (p. ej. 8 vídeos a la misma hora).
Este servicio recalcula TODAS las publicaciones pendientes del canal en una sola
pasada (repack_channel_publish_times) con separación >= SAME_CHANNEL_PUBLISH_GAP_HOURS
y, si no es dry-run:

1. Reprograma en YouTube (videos().update status.publishAt) los vídeos ya subidos
   como private (uploaded_private / warming / scheduled).
2. Actualiza DB: videos.target_public_at, planned_slots.target_public_at,
   video_lifecycle_actions.go_public.scheduled_for y (para awaiting_upload) el
   scheduled_upload_at para que la subida quepa antes de la publicación.
3. Registra cada cambio en scheduled_publish_logger (event "rescheduled").

Si YouTube rechaza (quota/auth/HTTP) el vídeo se deja como está: publicará solo
a su hora original. Nunca se fuerza una publicación manual.
"""

import json
import logging

from pipeline.publish_scheduler import (
    MAX_PUBLISH_AHEAD_SAFETY_HOURS,
    SAME_CHANNEL_PUBLISH_GAP_HOURS,
)

logger = logging.getLogger("autotube.publish_repack")

# Máximo de llamadas videos.update por invocación (self-heal lo acota; el
# endpoint manual puede pasarlo a None = todas).
DEFAULT_MAX_YT_UPDATES = None

REQUIRES_YT_STATUSES = ("uploaded_private", "warming", "scheduled")


def _channel_cfg(db, channel_id: int) -> dict:
    try:
        ch = db.get_channel(channel_id)
        if ch and ch.get("config_json"):
            return json.loads(ch["config_json"] or "{}")
    except Exception:
        pass
    return {}


def apply_publish_repack(
    db,
    channel_id: int,
    slug: str,
    dry_run: bool = False,
    max_yt_updates: int | None = DEFAULT_MAX_YT_UPDATES,
    quota_gate: bool = True,
    force_yt: bool = False,
    gap_hours: int | None = None,
    safety_ahead_hours: int | None = None,
) -> dict:
    """Repack y (opcionalmente) aplicar el nuevo horario de publicación.

    Args:
        db: ExtendedDatabase.
        channel_id: ID del canal.
        slug: slug del canal (log).
        dry_run: si True, solo calcula y devuelve el plan.
        max_yt_updates: límite de llamadas videos.update (None = todas).
        quota_gate: si True, salta si el proyecto del canal no tiene capacidad
            libre (usado por la autogestión; el endpoint manual lo desactiva).
        force_yt: si True, reprograma en YouTube TODOS los vídeos ya subidos
            aunque su target_public_at en DB ya coincida con el plan (resync
            tras una desincronización DB/YT, p. ej. por límite de updates en
            pasadas anteriores).
        gap_hours: separación mínima entre publicaciones del mismo canal.
            None → SAME_CHANNEL_PUBLISH_GAP_HOURS (3h, histórico). Pasar un
            valor mayor (p. ej. 24) convierte el repack en "máx 1 publicación
            por día" — el techo antiban.
        safety_ahead_hours: horizonte máximo de reprogramación (None → 120h).
            Con gaps grandes y backlogs densos hay que subirlo (p. ej. 720h)
            o el cap recorta y vuelve a apilar vídeos en el mismo instante.

    Returns:
        dict con {channel_id, slug, total, rescheduled, no_change, yt_failed,
        quota_skipped, details: [...]}
    """
    from pipeline.publish_scheduler import repack_channel_publish_times

    cfg = _channel_cfg(db, channel_id)
    tz_str = cfg.get("PUBLISH_TIMEZONE", "Europe/Madrid")
    warmup = int(cfg.get("PUBLISH_WARMUP_MIN", 120) or 120)

    # ── Bloqueo por spam: no reprogramar publicaciones de canales bloqueados ──
    # Un canal penalizado no debe ver su publishAt movido hacia el bloqueo; las
    # publicaciones pendientes ya fueron retenidas (hold) hasta tras el bloqueo.
    try:
        if db.is_channel_spam_blocked(channel_id):
            logger.info("[%s] repack skipped: canal bloqueado por spam (no se reprograma)", slug)
            return {
                "channel_id": channel_id, "slug": slug,
                "total": 0, "rescheduled": 0, "no_change": 0,
                "yt_failed": 0, "quota_skipped": 0, "skipped_spam": 1, "details": [],
            }
    except Exception:
        pass

    # ── Cuota (solo cuando quota_gate=True) ──
    if quota_gate:
        try:
            from api.services.quota_tracker import get_channel_project, project_has_free_capacity
            project = get_channel_project(slug)
            if not project_has_free_capacity(project, min_free_pct=10.0):
                logger.info("[%s] repack skipped: project %s sin capacidad libre", slug, project)
                return {
                    "channel_id": channel_id, "slug": slug,
                    "total": 0, "rescheduled": 0, "no_change": 0,
                    "yt_failed": 0, "quota_skipped": 1, "details": [],
                }
        except Exception:
            pass

    # ── 1. Calcular el plan ──
    plan = repack_channel_publish_times(
        db, channel_id, slug,
        timezone_str=tz_str, warmup_min=warmup,
        gap_hours=gap_hours if gap_hours is not None else SAME_CHANNEL_PUBLISH_GAP_HOURS,
        safety_ahead_hours=safety_ahead_hours if safety_ahead_hours is not None else MAX_PUBLISH_AHEAD_SAFETY_HOURS,
    )
    if not plan:
        return {
            "channel_id": channel_id, "slug": slug,
            "total": 0, "rescheduled": 0, "no_change": 0,
            "yt_failed": 0, "quota_skipped": 0, "details": [],
        }

    # ── 2. Filtrar solo cambios reales ──
    changes = []
    for item in plan:
        new = item["new_target"]
        old = item["old_target"]
        # Normalizar comparación: old puede venir en formato naive local
        if old and _normalize_iso(old) == _normalize_iso(new):
            item["changed"] = False
        else:
            item["changed"] = True
        changes.append(item)

    result = {
        "channel_id": channel_id, "slug": slug,
        "total": len(changes),
        "rescheduled": 0, "no_change": sum(1 for c in changes if not c["changed"]),
        "yt_failed": 0, "quota_skipped": 0,
        "details": [dict(c) for c in changes],
    }

    if dry_run:
        logger.info("[%s] repack DRY-RUN: %d cambios propuestos (%d sin cambio)",
                    slug, result["rescheduled"] + sum(1 for c in changes if c["changed"]),
                    result["no_change"])
        return result

    # ── 3. Aplicar ──
    yt_used = 0
    uploader_cache = {}

    for item in changes:
        # force_yt: aunque la DB ya coincida, resincronizar YouTube
        if not item["changed"] and not (force_yt and item["requires_yt_update"]):
            continue
        video_id = item["video_id"]
        new_target = item["new_target"]
        old_target = item["old_target"] or new_target

        # ── 3a. Reprogramar en YouTube (solo ya subidos) ──
        if item["requires_yt_update"]:
            if max_yt_updates is not None and yt_used >= max_yt_updates:
                logger.info(
                    "[%s] repack: límite de %d updates alcanzado — #%d pospuesto "
                    "(no se toca DB para no desincronizar con YouTube)",
                    slug, max_yt_updates, video_id,
                )
                # NO actualizar DB: si se dejara el nuevo target sin reprogramar
                # YouTube, el vídeo publicaría a la hora antigua mientras la DB
                # mostraría la nueva → desincronización. Se pospone a la
                # siguiente pasada (el canal sigue en _channels_need_repack).
                continue
            else:
                try:
                    from pipeline.youtube_uploader import YouTubeUploader
                    up = uploader_cache.get(slug)
                    if up is None:
                        up = YouTubeUploader(slug)
                        if not up.authenticate():
                            raise RuntimeError("auth fallida")
                        uploader_cache[slug] = up
                    res = up.set_publish_at(item["yt_video_id"], new_target)
                    if not res.get("updated"):
                        raise RuntimeError(f"respuesta inesperada: {res}")
                    yt_used += 1
                except Exception as exc:
                    logger.warning(
                        "[%s] ⚠️ YouTube rechazó reprogramar #%d (%s) — publicará solo a %s",
                        slug, video_id, exc, old_target[:19],
                    )
                    result["yt_failed"] += 1
                    continue  # no tocar DB: YT manda

        # ── 3b. Actualizar DB ──
        try:
            with db._connect() as conn:
                conn.execute(
                    "UPDATE videos SET target_public_at = ? WHERE id = ?",
                    (new_target, video_id),
                )
                if item.get("adjusted_upload_at"):
                    conn.execute(
                        "UPDATE videos SET scheduled_upload_at = ? WHERE id = ?",
                        (item["adjusted_upload_at"], video_id),
                    )
                conn.execute(
                    "UPDATE planned_slots SET target_public_at = ? WHERE video_id = ?",
                    (new_target, video_id),
                )
                conn.execute(
                    """UPDATE video_lifecycle_actions SET scheduled_for = ?
                       WHERE video_id = ? AND action_type = 'go_public'
                         AND status = 'pending'""",
                    (new_target, video_id),
                )
                conn.commit()
        except Exception as exc:
            logger.error("[%s] repack: DB update failed #%d: %s", slug, video_id, exc)
            continue

        # ── 3c. Log ──
        try:
            from api.services.scheduled_publish_logger import log_publish_event
            log_publish_event(
                event="rescheduled",
                slug=slug,
                video_title=f"#{video_id}",
                yt_video_id=item["yt_video_id"] or "-",
                db_video_id=video_id,
                target_public_at=old_target,
                actual_public_at=new_target,
                local_time=f"(repack: {old_target[:19]} → {new_target[:19]})",
            )
        except Exception:
            pass

        result["rescheduled"] += 1
        logger.info(
            "[%s] 🔁 Repack #%d: %s → %s (yt=%s)",
            slug, video_id, old_target[:19], new_target[:19],
            item["yt_video_id"] or "-",
        )

    return result


def _normalize_iso(ts: str) -> str:
    """Normaliza un timestamp a forma ISO UTC comparable (corta a minuto)."""
    from pipeline.publish_scheduler import _parse_target_public_at
    parsed = _parse_target_public_at(ts, "Europe/Madrid")
    if parsed is None:
        return str(ts)[:16]
    return parsed.strftime("%Y-%m-%dT%H:%M")
