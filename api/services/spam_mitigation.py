"""Rebaja de frecuencia de publicación tras un strike de spam de YouTube.

Se activa desde ``_record_short_spam_strike`` (punto único de detección de
eliminaciones por spam), cubriendo tanto shorts como vídeos long-form.

Al detectar un strike se reduce la frecuencia de publicación (long-form +
shorts) del canal penalizado **y de sus hermanos que comparten el mismo
proyecto GCP**, porque la penalización de spam suele ser por cuenta/proyecto,
no por canal individual.

La restauración es **manual** (conservador): se guardan los valores originales
en ``system_state`` bajo ``spam_freq_restore_{id}`` y un operador los recupera
tras verificar en YouTube Studio que la penalización ha cesado
(``restore_publication_frequency`` / endpoint del panel).
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone

logger = logging.getLogger("autotube.spam_mitigation")

# Factor de rebaja: multiplica la frecuencia por este factor (0.5 = mitad).
SPAM_FREQ_REDUCTION_FACTOR = 0.5
# Suelo para long-form. videos_per_day=0 está RESERVADO para el breaker de
# fallos de generación (semántica "canal pausado"), así que aquí el suelo es 1
# para no confundir ambos mecanismos.
SPAM_FREQ_LONG_MIN = 1
# Suelo para shorts nativos (si el canal los tenía >0); los clips pueden ir a 0.
SPAM_FREQ_SHORTS_NATIVE_MIN = 1

_RESTORE_KEY = "spam_freq_restore_{channel_id}"

# ── Retención de publicaciones ya programadas durante el bloqueo ──
# Un canal bloqueado no debe publicar NADA hasta expirar el bloqueo + colchón.
# Los vídeos ya subidos como 'private' con publishAt nativo se reprograman para
# caer DESPUÉS del fin del bloqueo, conservando un margen y gaps entre vídeos.
SPAM_HOLD_PUBLISH_MARGIN_MIN = 60   # primera publicación >=1h tras el desbloqueo
SPAM_HOLD_PUBLISH_GAP_HOURS = 3     # conservar 3h entre publicaciones del mismo canal
# Diferencia de colchón a añadir a bloques grabados antes del colchón de 6h.
SPAM_BLOCK_BUFFER_DELTA_HOURS = 2


def _floor_half(value) -> int:
    """floor(value * factor), nunca negativo."""
    try:
        return max(0, int(float(value) * SPAM_FREQ_REDUCTION_FACTOR))
    except (TypeError, ValueError):
        return 0


def _project_sibling_channels(slug: str, db) -> list[dict]:
    """Canales activos del mismo proyecto GCP que ``slug`` (lo incluye).

    Fallbacks (por orden): proyecto GCP → google_account → solo el canal.
    """
    from api.services.quota_tracker import get_channel_project

    channels = db.get_channels(active_only=True) or []

    try:
        project = get_channel_project(slug)
    except Exception:
        project = "unknown"

    if project and project != "unknown":
        siblings = []
        for ch in channels:
            try:
                if get_channel_project(ch.get("slug", "")) == project:
                    siblings.append(ch)
            except Exception:
                continue
        if siblings:
            return siblings

    # Fallback: misma cuenta Google.
    struck = next((c for c in channels if c.get("slug") == slug), None)
    account = struck.get("google_account") if struck else None
    if account:
        siblings = [c for c in channels if c.get("google_account") == account]
        if siblings:
            return siblings

    # Fallback final: solo el canal del strike.
    return [c for c in channels if c.get("slug") == slug]


def _reduce_one_channel(cid: int, slug: str, db) -> None:
    """Reduce long-form + shorts de un canal y guarda los valores originales."""
    cfg = db.get_channel_planning_config(cid) or {}
    vpd = int(cfg.get("videos_per_day", 2) or 2)
    boost = float(cfg.get("videos_day_boost_weight", 0.7) or 0.7)

    sc_list = db.get_shorts_planning_config(channel_id=cid) or []
    sc = sc_list[0] if sc_list else {}
    shorts_on = bool(sc.get("shorts_enabled", True))
    native = int(sc.get("shorts_native_per_day", 3) or 3)
    clips = int(sc.get("shorts_clips_per_long", 3) or 3)

    new_vpd = max(SPAM_FREQ_LONG_MIN, _floor_half(vpd)) if vpd > 0 else 0
    new_native = (
        max(SPAM_FREQ_SHORTS_NATIVE_MIN, _floor_half(native))
        if (shorts_on and native > 0)
        else 0
    )
    new_clips = _floor_half(clips)

    # Guardar originales SOLO la primera vez (idempotente ante strikes repetidos).
    restore_key = _RESTORE_KEY.format(channel_id=cid)
    if not db.get_system_state(restore_key):
        db.set_system_state(
            restore_key,
            json.dumps(
                {
                    "videos_per_day": vpd,
                    "videos_day_boost_weight": boost,
                    "shorts_native_per_day": native,
                    "shorts_clips_per_long": clips,
                    "shorts_enabled": shorts_on,
                }
            ),
        )

    # Aplicar (boost a 0 para que el +1 aleatorio no deshaga la rebaja).
    db.update_channel_planning_config(
        cid, videos_per_day=new_vpd, videos_day_boost_weight=0.0
    )
    db.update_shorts_planning_config(
        cid,
        {
            "shorts_native_per_day": new_native,
            "shorts_clips_per_long": new_clips,
        },
    )

    logger.warning(
        "⚠️ SPAM: frecuencia rebajada para %s — long %d→%d, shorts nativos %d→%d, clips %d→%d",
        slug, vpd, new_vpd, native, new_native, clips, new_clips,
    )

    _create_freq_alert(db, cid, slug, new_vpd, new_native, new_clips)


def _create_freq_alert(db, cid: int, slug: str, new_vpd: int, new_native: int,
                       new_clips: int) -> None:
    try:
        from api.services.lifecycle_monitor import create_alert
        create_alert(
            db,
            entity_type="channel",
            entity_id=cid,
            channel_id=cid,
            alert_type="spam_frequency_reduced",
            severity="warning",
            title=f"Canal {slug}: frecuencia de publicación rebajada por strike de spam",
            message=(
                f"YouTube eliminó contenido de {slug} (o de un canal hermano) por spam, "
                f"así que se ha rebajado su ritmo de publicación para no reincidir:\n"
                f"  - long-form: videos_per_day → {new_vpd} (boost diario desactivado)\n"
                f"  - shorts nativos/día → {new_native}\n"
                f"  - clips por long-form → {new_clips}\n\n"
                f"⚠️ RESTAURACIÓN MANUAL: verifica en YouTube Studio que la penalización "
                f"ha cesado y usa 'Restaurar frecuencia' en el panel de canales (o "
                f"POST /api/system/spam-blocks/{cid}/restore-frequency) para volver "
                f"a los valores originales."
            ),
            metadata={
                "new_videos_per_day": new_vpd,
                "new_shorts_native_per_day": new_native,
                "new_shorts_clips_per_long": new_clips,
                "action": "frecuencia rebajada; restauración manual",
            },
        )
    except Exception as exc:
        logger.warning("Spam frequency alert creation failed for %s: %s", slug, exc)


def reduce_publication_frequency_after_strike(channel_id: int, slug: str,
                                              db=None) -> list[int]:
    """Reduce frecuencia long-form + shorts del canal y sus hermanos de proyecto.

    Devuelve la lista de channel_ids afectados (puede estar vacía ante errores).
    """
    if db is None:
        from database.db_extended import ExtendedDatabase
        db = ExtendedDatabase()

    channels = _project_sibling_channels(slug, db)
    if not channels:
        channels = [
            c for c in (db.get_channels(active_only=True) or [])
            if int(c.get("id", 0) or 0) == int(channel_id)
        ]

    affected: list[int] = []
    for ch in channels:
        cid = int(ch["id"])
        cslug = ch.get("slug", slug)
        try:
            _reduce_one_channel(cid, cslug, db)
            affected.append(cid)
        except Exception as exc:
            logger.warning("[%s] spam frequency reduction failed: %s", cslug, exc)
    return affected


def restore_publication_frequency(channel_id: int, db=None) -> bool:
    """Restaura la frecuencia original de un canal tras la penalización.

    Devuelve True si había valores guardados y se restauraron.
    """
    if db is None:
        from database.db_extended import ExtendedDatabase
        db = ExtendedDatabase()

    raw = db.get_system_state(_RESTORE_KEY.format(channel_id=channel_id))
    if not raw:
        return False
    try:
        original = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return False

    db.update_channel_planning_config(
        channel_id,
        videos_per_day=int(original.get("videos_per_day", 2) or 2),
        videos_day_boost_weight=float(original.get("videos_day_boost_weight", 0.7) or 0.7),
    )
    db.update_shorts_planning_config(
        channel_id,
        {
            "shorts_native_per_day": int(original.get("shorts_native_per_day", 3) or 3),
            "shorts_clips_per_long": int(original.get("shorts_clips_per_long", 3) or 3),
        },
    )
    db.set_system_state(_RESTORE_KEY.format(channel_id=channel_id), "")
    logger.warning("Spam frequency restored for channel #%s", channel_id)
    return True


# ── Colchón + retención de publicaciones programadas ─────────────

def apply_spam_block_buffer_backfill(db=None) -> int:
    """Extiende los bloques activos grabados antes del colchón de 6h (idempotente).

    Los bloques previos al despliegue del colchón de 6h se grabaron con 4h.
    Añade las 2h de diferencia UNA sola vez por canal (marcador en system_state).
    Devuelve el número de canales extendidos.
    """
    if db is None:
        from database.db_extended import ExtendedDatabase
        db = ExtendedDatabase()

    now = time.time()
    extended = 0
    for ch in (db.get_channels(active_only=False) or []):
        cid = int(ch.get("id", 0) or 0)
        if not cid:
            continue
        raw = db.get_system_state(f"shorts_spam_blocked_until_{cid}")
        if not raw:
            continue
        try:
            until = float(raw)
        except (TypeError, ValueError):
            continue
        if until <= now:
            continue  # ya expirado
        if db.get_system_state(f"spam_block_buffer6h_{cid}"):
            continue  # ya extendido
        db.set_system_state(
            f"shorts_spam_blocked_until_{cid}",
            str(until + SPAM_BLOCK_BUFFER_DELTA_HOURS * 3600),
        )
        db.set_system_state(f"spam_block_buffer6h_{cid}", "1")
        logger.warning(
            "Spam block buffer backfill: canal #%s extendido +%dh (colchón 6h)",
            cid, SPAM_BLOCK_BUFFER_DELTA_HOURS,
        )
        extended += 1
    return extended


def _pending_publish_videos(channel_id: int, blocked_until: float, db) -> list[dict]:
    """Vídeos programados (private) de un canal cuyo publishAt cae antes del fin del bloqueo."""
    import sqlite3

    with db._connect() as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """SELECT id, yt_video_id, target_public_at FROM videos
               WHERE channel_id = ? AND published_at IS NULL
                 AND status IN ('uploaded_private', 'uploaded', 'warming')
                 AND target_public_at IS NOT NULL
               ORDER BY target_public_at ASC""",
            (channel_id,),
        ).fetchall()

    out = []
    for r in rows:
        tp = r["target_public_at"]
        try:
            dt = datetime.fromisoformat(str(tp).replace("Z", "+00:00").replace(" ", "T"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            continue
        if dt.timestamp() < blocked_until:
            out.append({"id": r["id"], "yt_video_id": r["yt_video_id"], "target_dt": dt})
    return out


def hold_pending_publishes_for_block(channel_id: int, slug: str,
                                     blocked_until: float, db=None) -> int:
    """Reprograma las publicaciones programadas de un canal bloqueado.

    Mueve tanto el ``target_public_at`` en DB como el publishAt nativo de YouTube
    (vía ``set_publish_at``, 50 ud/vídeo, best-effort) a DESPUÉS del fin del
    bloqueo + margen, conservando 3h entre vídeos. Devuelve el nº de vídeos
    reprogramados.
    """
    if db is None:
        from database.db_extended import ExtendedDatabase
        db = ExtendedDatabase()

    pending = _pending_publish_videos(channel_id, blocked_until, db)
    if not pending:
        return 0

    base = blocked_until + SPAM_HOLD_PUBLISH_MARGIN_MIN * 60
    uploader = None
    held = 0

    for i, v in enumerate(pending):
        new_dt = datetime.fromtimestamp(
            base + i * SPAM_HOLD_PUBLISH_GAP_HOURS * 3600, tz=timezone.utc
        )
        new_iso = new_dt.isoformat()

        # 1. DB (siempre).
        try:
            with db._connect() as conn:
                conn.execute(
                    "UPDATE videos SET target_public_at = ?, updated_at = CURRENT_TIMESTAMP "
                    "WHERE id = ?",
                    (new_iso, v["id"]),
                )
                conn.commit()
        except Exception as exc:
            logger.warning("hold publish: DB update failed for video %s: %s", v["id"], exc)
            continue

        # 2. YouTube nativo (best-effort, solo si hay yt_video_id).
        if v["yt_video_id"]:
            try:
                if uploader is None:
                    from pipeline.youtube_uploader import YouTubeUploader
                    uploader = YouTubeUploader(account_name=slug, channel_slug=slug)
                uploader.set_publish_at(v["yt_video_id"], new_iso)
            except Exception as exc:
                logger.warning(
                    "hold publish: set_publish_at failed para %s (%s): %s",
                    v["yt_video_id"], slug, exc,
                )

        held += 1

    if held:
        logger.warning(
            "⚠️ SPAM: %d publicaciones programadas de %s retenidas hasta tras el bloqueo",
            held, slug,
        )
    return held


def ensure_spam_holds(db=None) -> dict:
    """Backfill de colchón + retención de publicaciones de canales bloqueados.

    Idempotente (marcador de backfill; la retención solo toca vídeos con
    publishAt anterior al fin del bloqueo). Se llama al arranque de la API.
    """
    if db is None:
        from database.db_extended import ExtendedDatabase
        db = ExtendedDatabase()

    extended = apply_spam_block_buffer_backfill(db)
    held_total = 0
    channels_held: list[str] = []

    now = time.time()
    for ch in (db.get_channels(active_only=False) or []):
        cid = int(ch.get("id", 0) or 0)
        if not cid:
            continue
        raw = db.get_system_state(f"shorts_spam_blocked_until_{cid}")
        if not raw:
            continue
        try:
            until = float(raw)
        except (TypeError, ValueError):
            continue
        if until <= now:
            continue
        slug = ch.get("slug", "")
        try:
            held = hold_pending_publishes_for_block(cid, slug, until, db)
        except Exception as exc:
            logger.warning("ensure_spam_holds: %s failed: %s", slug, exc)
            held = 0
        if held:
            held_total += held
            channels_held.append(slug)

    return {"buffer_extended": extended, "held": held_total, "channels": channels_held}
