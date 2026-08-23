"""
Per-channel shorts scheduling engine (v12 — dynamic clip scaling).
Computes publish slots/day/channel based on per-channel
shorts_planning_config (shorts_native_per_day + shorts_clips_per_long).

Key v12 changes:
- Native shorts: always 3 per day (configurable per channel)
- Clip shorts: shorts_clips_per_long × N_long_videos_planned_today (dynamic!)
- Native slots: 3-of-4 optimal franjas (daily rotation), spread across day
- Clip slots: anchored to their source long video's target_public_at,
  spread evenly across the remaining day from (long_publish + 45min) to 23:45
- Minimum 30 min spacing between any same-channel shorts
- ±20 min deterministic jitter per channel
- Fair daily rotation across channels
"""

import hashlib
import logging
import random
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

logger = logging.getLogger("autotube.shorts_scheduler")


def _alert_shorts_dispatch_exhausted(detail: str, max_retries: int = 3) -> None:
    """Alerta (deduplicada) cuando el dispatcher de shorts se rinde.

    Anti-bucle (antiban, ago 2026): ninguna acción contra YouTube se rinde en
    silencio. Al agotar el presupuesto de reintentos se alerta al operador.
    create_alert deduplica por (entity_type, entity_id, alert_type), así que
    hay UNA sola alerta sin resolver por este tipo, sin spam en cada tick.
    """
    try:
        from api.services.lifecycle_monitor import create_alert
        from database.db_extended import ExtendedDatabase
        create_alert(
            ExtendedDatabase(),
            entity_type="system", entity_id=None, channel_id=None,
            alert_type="shorts_dispatch_exhausted",
            severity="warning",
            title="Shorts: presupuesto de reintentos agotado — sin slot despachable",
            message=(
                f"El dispatcher de shorts agotó sus {max_retries} reintentos sin "
                f"encontrar un slot válido. {detail} Los slots afectados quedaron "
                f"pendientes/cancelados. Revisa el log 'Shorts dispatch' para "
                f"identificar la causa."
            ),
            metadata={"max_retries": max_retries, "detail": detail},
        )
    except Exception as _alert_exc:
        logger.warning("shorts dispatch alert failed: %s", _alert_exc)

# ── Module-level state for clip source dedup across scheduler invocations ──
# Tracks source videos confirmed to have no usable script text.
# Cleared each calendar day at first invocation.
_VIDEOS_WITHOUT_SCRIPT: set[int] = set()
_VIDEOS_WITHOUT_SCRIPT_DATE: str = ""
# Cooldown after all slots exhausted to prevent log spam and CPU waste
_LAST_ALL_EXHAUSTED_AT: float = 0.0
_ALL_EXHAUSTED_COOLDOWN_SEC = 300  # 5 minutes

# ── Hard spam filter (ago 2026) ─────────────────────────────────
# YouTube removed shorts of this project as spam (post-upload verification
# confirmed missing / "Deleted video"). Retrying only feeds more spam
# signals, so: slot CANCELED, channel shorts BLOCKED for a cooling period.
# Second strike escalates to a longer block; shorts stay off until manual
# review of the channel content.
SHORTS_SPAM_BLOCK_HOURS = 72             # 1st strike: 3 days
SHORTS_SPAM_BLOCK_HOURS_ESCALATED = 168  # 2nd strike: 7 days
SHORTS_SPAM_MAX_STRIKES = 2
# Colchón extra de reactivación: el bloque se mantiene (hours + buffer) antes
# de desbloquear el canal automáticamente. Ej: 72h → se reactiva a las 78h.
SHORTS_SPAM_BLOCK_BUFFER_HOURS = 6
# Title similarity guard: reject a native short whose title is too similar
# to a recent one (token-overlap ≥ threshold). Near-duplicate titles are a
# classic spam signal.
TITLE_SIMILARITY_THRESHOLD = 0.6
TITLE_SIMILARITY_LOOKBACK_DAYS = 30
TITLE_SIMILARITY_LOOKBACK_LIMIT = 30

# Anti-strike (ago 2026): NUNCA subir un short con el render degradado a fondo
# de color sólido (solid bg = voz TTS + subtítulos sobre color plano = firma
# clásica de "AI slop" que YouTube elimina en segundos). Si la fracción de
# escenas con asset visual real cae por debajo de este umbral, se rechaza el
# render y el slot reintenta (re-genera) con más RAM/assets, en vez de subir.
SHORTS_MIN_VALID_ASSET_RATIO = 0.5
# Cap duro de shorts por canal y día (total native+clip), independiente de la
# config de planning y NO saltable por el force-dispatch/catch-up. El usuario
# pidió explícitamente "1 short al día por canal" tras los strikes.
SHORTS_HARD_PER_CHANNEL_DAILY_CAP = 1


def _safe_publish_at(target_upload_at, channel_slug: str, channel_id: int = None,
                     db=None) -> str | None:
    """Return a future publishAt for a short, or None.

    Guards against a stale target_upload_at (in the past) which YouTube would
    reject. If stale, recalculates to the next peak slot via the publish
    scheduler. Never returns a past publishAt.
    """
    if not target_upload_at:
        return None
    try:
        from pipeline.publish_scheduler import _target_is_stale, ensure_future_target_public_at
        from config.config_bridge import get_channel_config
        ch_cfg = get_channel_config(channel_slug)
        tz = getattr(ch_cfg, "PUBLISH_TIMEZONE", "Europe/Madrid")
        warmup = int(getattr(ch_cfg, "PUBLISH_WARMUP_MIN", 5) or 5)
        if _target_is_stale(target_upload_at, timezone_str=tz, warmup_min=warmup):
            logger.info(
                "[%s] Short publishAt stale (%s) — recalculating to future peak",
                channel_slug, str(target_upload_at)[:19],
            )
            return ensure_future_target_public_at(
                target_upload_at, slug=channel_slug, timezone_str=tz,
                db=db, channel_id=channel_id, warmup_min=warmup,
            )
        return str(target_upload_at)
    except Exception as exc:
        logger.warning("[%s] Short publishAt guard failed; using target as-is: %s",
                       channel_slug, exc)
        return str(target_upload_at)


def _youtube_quota_blocked(db=None, channel_slug: str = "") -> bool:
    """Return True when the channel's PROJECT quota breaker is active.

    Fase cuota (ago 2026): breaker PER PROJECT. Sin channel_slug comprueba si
    TODOS los canales activos tienen su proyecto agotado (un solo proyecto
    agotado ya no paraliza los shorts de los demás canales).

    Anti ping-pong: en los últimos 30 min del día-PT se retienen subidas sin
    fijar breaker (la cuota anterior está agotada con casi total seguridad).
    """
    try:
        from config.settings import YT_REMEDIATION_MODE
        if YT_REMEDIATION_MODE:
            return True
        from api.services.quota_tracker import in_pt_day_end_window
        if in_pt_day_end_window():
            return True
        if db is None:
            from database.db_extended import ExtendedDatabase
            db = ExtendedDatabase()
        if channel_slug:
            return db.is_quota_exhausted_for_channel(channel_slug)
        # Sin canal concreto: solo bloquear si TODOS los canales están agotados
        return db.all_channels_quota_exhausted()
    except Exception as exc:
        logger.debug("Shorts quota guard skipped: %s", exc)
    return False


# ── Hard spam filter helpers ────────────────────────────────────

def _record_short_spam_strike(channel_id: int, channel_slug: str, db=None,
                              video_id: str = None, reason: str = None) -> int:
    """Record a YouTube spam removal for a channel and BLOCK its uploads.

    Returns the new strike count. After SHORTS_SPAM_MAX_STRIKES the block
    escalates. The block key is stored in system_state so it survives
    API restarts (apply_changes.sh).

    ``video_id`` / ``reason`` se guardan (``shorts_spam_last_removal_{id}``)
    para el informe de situación del banner (por qué se bloqueó el canal).

    NOTA: el registro se hace UNA sola vez por eliminación, desde
    `_verify_upload_exists` (punto único de detección), para cubrir tanto
    shorts como vídeos normales sin doble conteo.
    """
    try:
        if db is None:
            from database.db_extended import ExtendedDatabase
            db = ExtendedDatabase()
        strikes_key = f"shorts_spam_strikes_{channel_id}"
        try:
            strikes = int(db.get_system_state(strikes_key) or 0) + 1
        except (TypeError, ValueError):
            strikes = 1
        db.set_system_state(strikes_key, str(strikes))
        hours = SHORTS_SPAM_BLOCK_HOURS_ESCALATED if strikes >= SHORTS_SPAM_MAX_STRIKES \
            else SHORTS_SPAM_BLOCK_HOURS
        total_hours = hours + SHORTS_SPAM_BLOCK_BUFFER_HOURS
        block_until = time.time() + total_hours * 3600
        db.set_system_state(f"shorts_spam_blocked_until_{channel_id}", str(block_until))

        # ── Contexto del strike (para el informe de situación) ──
        try:
            import json as _json
            _now_iso = __import__("datetime").datetime.now(
                __import__("datetime").timezone.utc
            ).isoformat()
            db.set_system_state(
                f"shorts_spam_last_removal_{channel_id}",
                _json.dumps({
                    "video_id": video_id or "",
                    "reason": reason or "eliminado por los sistemas automatizados de YouTube",
                    "detected_at": _now_iso,
                    "strike_count": strikes,
                }),
            )
        except Exception as _ctx_exc:
            logger.warning("[%s] spam removal context store failed: %s", channel_slug, _ctx_exc)

        logger.error(
            "⚠️ SPAM STRIKE #%d para %s — subidas BLOQUEADAS %dh (%s)",
            strikes, channel_slug, total_hours,
            "escalado (revisar contenido en YouTube Studio)" if strikes >= SHORTS_SPAM_MAX_STRIKES else "enfriamiento",
        )

        # ── Fase 4 bis: un strike nuevo DEVUELVE el perfil de pacing a 'strike'.
        # Cierra el bucle adaptativo: strike → (7 días limpios) → recovery →
        # (21 días limpios) → normal; un nuevo strike → strike otra vez.
        try:
            from api.services.pacing_profile import set_pacing_profile, get_active_profile_name
            if get_active_profile_name(db) != "strike":
                set_pacing_profile("strike", db)
                logger.warning(
                    "[%s] Pacing: strike #%d → perfil de pacing restaurado a 'strike'",
                    channel_slug, strikes,
                )
        except Exception as _apt_exc:
            logger.debug("Pacing reset-to-strike skipped: %s", _apt_exc)

        # ── Alerta formal (StatusBar + AlertsPanel) ──
        try:
            from api.services.lifecycle_monitor import create_alert
            create_alert(
                db,
                entity_type="channel",
                entity_id=channel_id,
                channel_id=channel_id,
                alert_type="spam_strike",
                severity="critical",
                title=f"Canal {channel_slug}: strike de spam #{strikes} — subidas bloqueadas",
                message=(
                    f"YouTube eliminó una subida de {channel_slug} por spam/IA/política "
                    f"(strike #{strikes}). Subidas bloqueadas {total_hours}h "
                    f"(shorts + vídeos long-form).\n\n"
                    f"Vídeo afectado: {video_id or 'desconocido'}\n"
                    f"Razón: {reason or 'no especificada'}\n\n"
                    f"Acciones automáticas: frecuencia de publicación rebajada (canal + "
                    f"hermanos de proyecto), publicaciones programadas retenidas hasta el "
                    f"fin del bloqueo. Revisa el contenido en YouTube Studio antes de restaurar."
                ),
                metadata={
                    "strike": strikes,
                    "block_hours": total_hours,
                    "blocked_until": block_until,
                    "video_id": video_id,
                    "reason": reason,
                    "action": "bloqueo + rebaja frecuencia + retención publicaciones",
                },
            )
        except Exception as _alert_exc:
            logger.warning("[%s] spam-strike alert failed: %s", channel_slug, _alert_exc)

        # ── Rebaja de frecuencia (canal + hermanos del mismo proyecto) ──
        # Tras el bloqueo, el canal y sus hermanos publican a menor ritmo para
        # no reincidir en el flag de spam. Restauración manual vía panel.
        try:
            from api.services.spam_mitigation import reduce_publication_frequency_after_strike
            reduce_publication_frequency_after_strike(channel_id, channel_slug, db=db)
        except Exception as _freq_exc:
            logger.error(
                "Spam frequency reduction failed for %s: %s", channel_slug, _freq_exc,
            )
        # ── Retener publicaciones ya programadas del canal bloqueado ──
        # Los vídeos subidos como private con publishAt nativo caerían durante
        # el bloqueo; se reprograman para después del fin del bloqueo + colchón.
        try:
            from api.services.spam_mitigation import hold_pending_publishes_for_block
            hold_pending_publishes_for_block(channel_id, channel_slug, block_until, db=db)
        except Exception as _hold_exc:
            logger.error(
                "Spam hold pending publishes failed for %s: %s", channel_slug, _hold_exc,
            )
        return strikes
    except Exception as exc:
        logger.error("Failed to record spam strike for %s: %s", channel_slug, exc)
        return 0


def _channel_shorts_spam_blocked(channel_id: int, db=None) -> bool:
    """Return True if the channel's shorts are blocked by a spam strike."""
    try:
        if db is None:
            from database.db_extended import ExtendedDatabase
            db = ExtendedDatabase()
        raw = db.get_system_state(f"shorts_spam_blocked_until_{channel_id}")
        if not raw:
            return False
        return time.time() < float(raw)
    except Exception:
        return False


def _shorts_paused(db=None) -> bool:
    """Kill-switch global de shorts (manual). True si `shorts_paused` o
    `scheduler_paused` están activos en system_state."""
    try:
        if db is None:
            from database.db_extended import ExtendedDatabase
            db = ExtendedDatabase()
        return db.get_system_state("shorts_paused") == "true" or \
            db.get_system_state("scheduler_paused") == "true"
    except Exception:
        return False


# Tope global duro de shorts/día (todos los canales sumados). Evita el volumen
# que dispara el spam de YouTube (histórico: 15-40/día → penalización).
GLOBAL_SHORTS_PER_DAY_CAP = 6


def _global_shorts_daily_cap_reached(db=None) -> bool:
    """True si ya se publicaron >= GLOBAL_SHORTS_PER_DAY_CAP shorts hoy (global)."""
    try:
        import sqlite3 as _sql_cap
        from config.settings import DATABASE_PATH as _DBP_CAP
        with _sql_cap.connect(str(_DBP_CAP), timeout=10) as _conn_cap:
            row = _conn_cap.execute(
                """SELECT COUNT(*) FROM shorts
                   WHERE status = 'published'
                     AND date(published_at) = date('now', 'localtime')"""
            ).fetchone()
        return (row[0] if row else 0) >= _global_shorts_daily_cap()
    except Exception:
        return False


def _channel_hard_daily_short_cap_reached(channel_id: int, db=None) -> bool:
    """True si el canal ya SUBIÓ hoy >= SHORTS_HARD_PER_CHANNEL_DAILY_CAP shorts.

    Cap duro por canal (native+clip), independiente de la config de planning y
    NO saltable por force-dispatch/catch-up. Cuenta cualquier short con
    youtube_id asignado hoy (subido, aunque sea private programado), porque lo
    que dispara el flag de YouTube es la SUBIDA, no la publicación.
    """
    try:
        import sqlite3 as _sql_pc
        from config.settings import DATABASE_PATH as _DBP_PC
        with _sql_pc.connect(str(_DBP_PC), timeout=10) as _conn_pc:
            row = _conn_pc.execute(
                """SELECT COUNT(*) FROM shorts
                   WHERE channel_id = ?
                     AND youtube_id IS NOT NULL
                     AND youtube_id != ''
                     AND date(created_at) = date('now', 'localtime')""",
                (channel_id,),
            ).fetchone()
        return (row[0] if row else 0) >= _hard_daily_cap()
    except Exception:
        return False


def _recent_short_titles(channel_id: int,
                         days: int = TITLE_SIMILARITY_LOOKBACK_DAYS,
                         limit: int = TITLE_SIMILARITY_LOOKBACK_LIMIT) -> list[tuple[int, str]]:
    """Return [(short_id, title)] for recent shorts of the channel."""
    import sqlite3 as _sql_titles
    from config.settings import DATABASE_PATH as _DBP_T
    try:
        with _sql_titles.connect(str(_DBP_T), timeout=10) as _conn_t:
            rows = _conn_t.execute(
                """SELECT id, title FROM shorts
                   WHERE channel_id = ? AND title IS NOT NULL AND title != ''
                     AND date(created_at) >= date('now', 'localtime', ?)
                   ORDER BY created_at DESC LIMIT ?""",
                (channel_id, f"-{days} days", limit),
            ).fetchall()
        return [(r[0], r[1]) for r in rows]
    except Exception:
        return []


# Palabras función que NO deben contar como evidencia de título duplicado
# (fix ago 2026). Dos títulos de temas distintos pueden compartir artículos y
# conectores ("el/la/de/que/en/the/of...") y dar un overlap alto sin ser spam.
TITLE_SIMILARITY_STOPWORDS = frozenset({
    "el", "la", "los", "las", "lo", "un", "una", "unos", "unas",
    "y", "o", "u", "e", "de", "del", "que", "quien", "cual", "a", "al",
    "en", "con", "por", "para", "sin", "sobre", "entre", "hasta", "desde",
    "se", "su", "sus", "mi", "tu", "es", "son", "era", "fue", "no", "si",
    "cuando", "como", "mas", "más", "pero", "porque", "también", "hay",
    "the", "and", "of", "to", "in", "for", "on", "with", "at", "from",
    "by", "is", "are", "was", "this", "that", "an",
})


def _titles_too_similar(title_a: str, title_b: str,
                        threshold: float = TITLE_SIMILARITY_THRESHOLD) -> bool:
    """Token-overlap similarity. Near-duplicate titles = spam signal.

    (fix ago 2026) Usa DOS métricas para no debilitar el anti-spam:
    - `meaningful` overlap: solo palabras con contenido (sin stopwords) —
      detecta duplicados reales de contenido y evita falsos positivos por
      palabras función ("EL MISTERIO DE LA CASA..." vs "EL MISTERIO DE LA
      MONTAÑA..." ya NO chocan solo por el patrón de gancho).
    - `raw` overlap ≥ 0.8: red de seguridad para títulos casi idénticos
      literalmente (p. ej. el MISMO título repetido, que daba 1.0 y era el
      caso que se veía en bucle en los logs).
    """
    if not title_a or not title_b:
        return False
    import re as _re_sim
    raw_a = set(_re_sim.findall(r"[a-z0-9áéíóúüñ]+", title_a.lower()))
    raw_b = set(_re_sim.findall(r"[a-z0-9áéíóúüñ]+", title_b.lower()))
    if not raw_a or not raw_b:
        return False
    meaningful_a = raw_a - TITLE_SIMILARITY_STOPWORDS
    meaningful_b = raw_b - TITLE_SIMILARITY_STOPWORDS
    if meaningful_a and meaningful_b:
        meaningful_overlap = len(meaningful_a & meaningful_b) / max(len(meaningful_a), len(meaningful_b))
        if meaningful_overlap >= threshold:
            return True
    raw_overlap = len(raw_a & raw_b) / max(len(raw_a), len(raw_b))
    return raw_overlap >= 0.8


def _recent_longform_titles(channel_id: int,
                            days: int = TITLE_SIMILARITY_LOOKBACK_DAYS,
                            limit: int = TITLE_SIMILARITY_LOOKBACK_LIMIT,
                            exclude_id: Optional[int] = None) -> list[tuple[int, str]]:
    """Return [(video_id, titulo_final)] for recent long-form videos of the channel.

    `exclude_id`: opcional, salta ese vídeo de la lista (evita que un vídeo se
    compare contra su PROPIO título ya guardado en la DB — el bug del falso
    positivo "se parece a long-form #N" donde N es el propio vídeo).
    """
    import sqlite3 as _sql_titles
    from config.settings import DATABASE_PATH as _DBP_T
    try:
        with _sql_titles.connect(str(_DBP_T), timeout=10) as _conn_t:
            sql = """SELECT id, COALESCE(titulo_final, '') FROM videos
                     WHERE channel_id = ? AND titulo_final IS NOT NULL AND titulo_final != ''
                       AND date(created_at) >= date('now', 'localtime', ?)"""
            params: list = [channel_id, f"-{days} days"]
            if exclude_id is not None:
                sql += " AND id != ?"
                params.append(exclude_id)
            sql += " ORDER BY created_at DESC LIMIT ?"
            params.append(limit)
            rows = _conn_t.execute(sql, params).fetchall()
        return [(r[0], r[1]) for r in rows]
    except Exception:
        return []


def _title_similar_to_recent(channel_id: int, title: str,
                             check_shorts: bool = True,
                             check_longform: bool = True,
                             exclude_longform_id: Optional[int] = None) -> tuple[bool, str]:
    """True si el título se parece a un short o long-form reciente del canal.

    (antiban, ago 2026): los títulos casi duplicados entre shorts y long-form
    son una señal clásica de spam de YouTube. Devuelve (es_similar, descripción
    del conflicto) para logs/alertas.

    `exclude_longform_id`: id del vídeo cuyo título se está comprobando; se
    excluye de la comparación contra long-forms para que un vídeo NUNCA se
    flagge a sí mismo (el título ya está guardado en `videos` cuando se chequea).
    """
    if not title:
        return False, ""
    if check_shorts:
        for prev_id, prev_title in _recent_short_titles(channel_id):
            if _titles_too_similar(title, prev_title):
                return True, f"short #{prev_id} '{prev_title[:50]}'"
    if check_longform:
        for prev_id, prev_title in _recent_longform_titles(
                channel_id, exclude_id=exclude_longform_id):
            if _titles_too_similar(title, prev_title):
                return True, f"long-form #{prev_id} '{prev_title[:50]}'"
    return False, ""


def warn_if_title_similar(channel_id: int, channel_slug: str, video_id: int,
                          title: str, db=None) -> bool:
    """Alerta (una vez por vídeo) si un título long-form se parece a contenido
    reciente del mismo canal. NO bloquea la generación: solo avisa al operador
    (antiban, ago 2026). Devuelve True si era similar.
    """
    try:
        similar, what = _title_similar_to_recent(channel_id, title,
                                                 check_shorts=True, check_longform=True,
                                                 exclude_longform_id=video_id)
        if not similar:
            return False
        logger.warning(
            "[%s] Long-form title similar to recent %s: '%s' — señal de spam, revisar",
            channel_slug, what, str(title)[:60],
        )
        if db is None:
            from database.db_extended import ExtendedDatabase
            db = ExtendedDatabase()
        key = f"title_sim_alert_{video_id}"
        if db.get_system_state(key):
            return True
        db.set_system_state(key, "1")
        from api.services.lifecycle_monitor import create_alert
        create_alert(
            db, entity_type="video", entity_id=video_id, channel_id=channel_id,
            alert_type="title_similarity_warning", severity="warning",
            title=f"Título de vídeo similar a contenido reciente ({channel_slug})",
            message=(
                f"El título del vídeo #{video_id} se parece a {what}. "
                f"Los títulos casi duplicados son una señal clásica de spam de "
                f"YouTube: edita el título antes de publicar si es un duplicado."
            ),
            metadata={"video_id": video_id, "conflict": what},
        )
        return True
    except Exception as exc:
        logger.debug("warn_if_title_similar failed: %s", exc)
        return False

# ── Auto-mark altered content helper (shorts) ─────────────────

def _auto_mark_ia_for_short(yt_id: str, channel_slug: str, account: str, short_id: int):
    """Background thread: mark short as AI-generated content.
    
    No end screens — YouTube doesn't support them on Shorts.
    """
    import time as _time
    from pipeline.youtube_browser import cleanup_browser_thread
    
    try:
        _time.sleep(20)  # Wait for YouTube to finish processing
        from pipeline.youtube_browser import get_browser
        browser = get_browser(account)
        success = browser.mark_altered_content(yt_id)
        if success:
            import sqlite3
            from config.settings import DATABASE_PATH
            conn = sqlite3.connect(str(DATABASE_PATH), timeout=10)
            conn.execute(
                "UPDATE shorts SET manual_altered_content_done = 1 WHERE id = ?",
                (short_id,),
            )
            conn.commit()
            conn.close()
            logger.info("[%s] Altered content marked for short %s", channel_slug, yt_id)
        else:
            logger.warning("[%s] Failed to mark altered content for short %s", channel_slug, yt_id)
    except Exception as e:
        logger.warning("[%s] Auto-mark IA error for short %s: %s", channel_slug, yt_id, e)
    finally:
        cleanup_browser_thread()


# ── Auto-link long-form video to short helper ──────────────────

def _processing_poll_delay(attempt: int) -> int:
    """Backoff del poll de procesado: 15s → 30s (4+) → 60s (10+).

    Reduce llamadas videos.list sin penalizar la espera real de encoding.
    """
    if attempt < 4:
        return 15
    if attempt < 10:
        return 30
    return 60


def _wait_until_processed(channel_slug: str, yt_id: str, timeout: int = 300) -> bool:
    """Poll YouTube Data API until the video finishes processing.

    Checks processingDetails.processingStatus via YouTube Data API v3.
    Waits until status transitions from "processing" to "succeeded" (or fails).

    If the API is unavailable, falls back to exponential backoff sleep
    as a best-effort substitute.

    Args:
        channel_slug: Channel slug for auth.
        yt_id: YouTube video ID to check.
        timeout: Maximum total wait time in seconds (default 5 min).

    Returns:
        True when the video is ready (processingStatus != "processing").
        False if timeout is reached or the API consistently fails.
    """
    import time as _time

    start = _time.monotonic()
    api_attempts = 0
    api_failures = 0

    while True:
        elapsed = _time.monotonic() - start
        if elapsed > timeout:
            logger.warning("[%s] Timeout waiting for video %s to finish processing (%.0fs)",
                           channel_slug, yt_id, elapsed)
            return False

        # ── Try YouTube Data API ────────────────────────────────
        try:
            from pipeline.youtube_uploader import YouTubeUploader
            uploader = YouTubeUploader(account_name=channel_slug, channel_slug=channel_slug)
            if not uploader.authenticate():
                raise RuntimeError("Authentication failed for " + channel_slug)

            service = uploader._get_service()
            resp = service.videos().list(
                part="processingDetails", id=yt_id
            ).execute()

            # ── Track quota (diagnostic) ──────────────────────────
            try:
                from api.services.quota_tracker import track_quota
                track_quota(channel_slug, "videos.list", 1,
                            yt_id=yt_id, caller="shorts_wait_processed")
            except Exception:
                pass

            items = resp.get("items", [])
            if not items:
                # Video not yet visible in API — still processing
                logger.debug("[%s] Video %s not yet visible in API (attempt %d, %.0fs elapsed)",
                             channel_slug, yt_id, api_attempts + 1, elapsed)
                _time.sleep(_processing_poll_delay(api_attempts))
                api_attempts += 1
                continue

            processing = items[0].get("processingDetails", {})
            status = processing.get("processingStatus", "unknown")
            progress = processing.get("processingProgress", {})

            if status == "succeeded":
                logger.info("[%s] Video %s processing complete (API confirmed in %.0fs, %d attempts)",
                            channel_slug, yt_id, elapsed, api_attempts + 1)
                return True

            if status == "failed":
                reason = processing.get("processingFailureReason", "unknown")
                logger.error("[%s] Video %s processing FAILED: %s", channel_slug, yt_id, reason)
                return False

            if status == "terminated":
                logger.warning("[%s] Video %s processing terminated — may have been taken down",
                               channel_slug, yt_id)
                return False

            # Still "processing"
            parts_processed = progress.get("partsProcessed", "?")
            parts_total = progress.get("partsTotal", "?")
            logger.debug("[%s] Video %s still processing (%s/%s parts, attempt %d, %.0fs elapsed)",
                         channel_slug, yt_id, parts_processed, parts_total,
                         api_attempts + 1, elapsed)
            _time.sleep(_processing_poll_delay(api_attempts))
            api_attempts += 1

        except Exception as api_err:
            api_failures += 1
            logger.debug("[%s] API polling failed for %s (failure %d): %s",
                         channel_slug, yt_id, api_failures, api_err)

            if api_failures >= 3:
                # Too many API failures — fall back to time-based wait
                logger.warning("[%s] API polling failed %d times for %s — "
                               "falling back to exponential backoff sleep",
                               channel_slug, api_failures, yt_id)
                remaining = timeout - elapsed
                if remaining <= 0:
                    return False
                # Conservative sleep: wait the remaining estimated processing time
                # capped at 120s (typical short processing takes 30-90s)
                wait = min(remaining, 120)
                logger.info("[%s] Waiting %.0fs (backoff fallback) for video %s",
                            channel_slug, wait, yt_id)
                _time.sleep(wait)
                return True  # Assume ready after backoff
            else:
                _time.sleep(15)


def _auto_link_longform_for_short(short_yt_id: str, channel_slug: str, account: str,
                                   short_id: int, source_video_id: int):
    """Background thread: link the source long-form video as 'Related video' on a Short.

    YouTube API has no endpoint for this — must use YouTube Studio browser automation.
    Only works for clip-type shorts (source_video_id IS NOT NULL).
    """
    import sqlite3
    from config.settings import DATABASE_PATH
    from pipeline.youtube_browser import cleanup_browser_thread

    try:
        # ── Poll API until the Short finishes processing ──────
        # YouTube needs time to transcode and process the Short before
        # the edit page becomes available. Instead of a hardcoded sleep,
        # we query processingDetails via YouTube Data API.
        if not _wait_until_processed(channel_slug, short_yt_id, timeout=300):
            logger.warning("[%s] Short %s did not finish processing in time — skipping longform link",
                           channel_slug, short_yt_id)
            return

        # Resolve the long-form YouTube video ID
        conn = sqlite3.connect(str(DATABASE_PATH), timeout=10)
        row = conn.execute(
            "SELECT yt_video_id FROM videos WHERE id = ? AND yt_video_id IS NOT NULL AND yt_video_id != ''",
            (source_video_id,),
        ).fetchone()
        conn.close()

        if not row or not row[0]:
            logger.warning("[%s] No YouTube ID for source video #%d — cannot link to short %s",
                           channel_slug, source_video_id, short_yt_id)
            return

        longform_yt_id = row[0]

        from pipeline.youtube_browser import get_browser
        browser = get_browser(account)
        success = browser.link_longform_video(short_yt_id, longform_yt_id)

        conn2 = sqlite3.connect(str(DATABASE_PATH), timeout=10)
        if success:
            conn2.execute(
                "UPDATE shorts SET longform_linked = 1, longform_linked_at = datetime('now','localtime') WHERE id = ?",
                (short_id,),
            )
            logger.info("[%s] ✅ Long-form video %s linked to short %s",
                        channel_slug, longform_yt_id, short_yt_id)
        else:
            logger.warning("[%s] Failed to link long-form %s to short %s",
                           channel_slug, longform_yt_id, short_yt_id)
        conn2.commit()
        conn2.close()
    except Exception as e:
        logger.warning("[%s] Auto-link longform error for short %s → source #%d: %s",
                       channel_slug, short_yt_id, source_video_id, e)
    finally:
        cleanup_browser_thread()


# ── Timezone defaults ─────────────────────────────────────────
DEFAULT_TIMEZONE = ZoneInfo("Europe/Madrid")
UTC = timezone.utc

# ── Spacing constants ─────────────────────────────────────────
MIN_SHORTS_GAP_MINUTES = 20    # Minimum generation gap between any same-channel shorts (was 35)
SAME_TYPE_SHORTS_PUBLISH_GAP_MINUTES = 240  # native↔native or clip↔clip min publish gap (90→240: anti-spam tras penalización YT)
CROSS_TYPE_SHORTS_PUBLISH_GAP_MINUTES = 20  # v10.3: native↔clip min publish gap (was 0 — allowed overlap)
CROSS_TYPE_SHORTS_ALLOW_OVERLAP = False     # v10.3: enforce cross-type publish gap
CATCH_UP_BYPASS_HOURS = 6      # v10.3: only skip gap if slot is >6h past-due (was: any past-due)
CLIP_DELAY_AFTER_LONG_MINUTES = 45  # Wait 45 min after long publish before clip
CLIP_END_OF_DAY = 23           # Latest clip target hour (local)
CLIP_END_MIN = 45              # Latest clip target minute
DAY_START_MINUTES = 0           # Allow 24h slots (LATAM overnight slots like 02:37)
DAY_END_MINUTES = 24 * 60       # End of day cap

# ── Jitter: asymmetric — more room before peak, tight after ──
JITTER_BEFORE_MIN = 25         # max minutes before target slot
JITTER_AFTER_MIN = 5           # max minutes after target slot

# ── Generation lead time: shorts need realistic buffer ──────
# v10.3 (Aug 2026): Differentiated by type:
#   - native: full pipeline (LLM + TTS + render + upload) → ~3-7 min real
#   - clip pre-render: upload only (~30s)
#   - clip on-the-fly: extract + render + upload (~3-5 min)
SHORT_GEN_LEAD_MIN = 15           # native shorts (was 5)
SHORT_GEN_LEAD_MIN_CLIP = 5       # pre-rendered clip: just upload
SHORT_GEN_LEAD_MIN_CLIP_ONTHEFLY = 15  # on-the-fly clip: extract + render + upload

# ── Shorts cooldown: minimum minutes between same-channel shorts ──
SHORTS_COOLDOWN_MINUTES = 180  # min gap between same-channel shorts (60→180: anti-spam tras penalización YT)


# ── Pacing dinámico (perfil central "strike mode") ──────────────
# Las constantes de arriba son el FALLBACK del perfil "strike" (no cambia el
# comportamiento actual). El perfil activo (system_state["pacing_profile"])
# gobierna estas claves; relajar los strikes = cambiar el perfil y todo se
# reajusta. Ver api/services/pacing_profile.py.

def _pacing_int(key: str, default: int) -> int:
    try:
        from api.services.pacing_profile import get_pacing_value
        return int(get_pacing_value(key, default=default) or default)
    except Exception:
        return default


def _hard_daily_cap() -> int:
    """Cap duro de shorts por canal y día (total native+clip)."""
    return _pacing_int("shorts_per_channel_day", SHORTS_HARD_PER_CHANNEL_DAILY_CAP)


def _global_shorts_daily_cap() -> int:
    """Cap global de shorts/día entre todos los canales."""
    return _pacing_int("shorts_global_day", GLOBAL_SHORTS_PER_DAY_CAP)


def _shorts_cooldown_minutes() -> int:
    """Cooldown mínimo entre shorts del mismo canal."""
    return _pacing_int("shorts_cooldown_min", SHORTS_COOLDOWN_MINUTES)


def _same_type_gap_minutes() -> int:
    """Gap mínimo de publicación native↔native / clip↔clip."""
    return _pacing_int("shorts_same_type_gap_min", SAME_TYPE_SHORTS_PUBLISH_GAP_MINUTES)


def _cross_type_gap_minutes() -> int:
    """Gap mínimo de publicación native↔clip."""
    return _pacing_int("shorts_cross_type_gap_min", CROSS_TYPE_SHORTS_PUBLISH_GAP_MINUTES)


def _min_shorts_gap_minutes() -> int:
    """Gap mínimo de generación entre shorts del mismo canal."""
    return _pacing_int("shorts_min_gap_min", MIN_SHORTS_GAP_MINUTES)

# ── Native fallback windows (used when no optimal slots available) ──
NATIVE_WINDOWS = [
    (9, 30),     # morning
    (13, 0),     # midday
    (18, 30),    # evening
    (21, 0),     # prime time
]

# v2 (Aug 2026): expanded from 4 to 7 filler windows to support 10 shorts/day.
FILLER_WINDOWS = [
    (2, 0),     # madrugada
    (4, 0),     # madrugada
    (6, 0),     # amanecer
    (11, 0),    # media mañana
    (14, 0),    # mediodía (post-almuerzo)
    (15, 0),    # mediodía
    (16, 0),    # tarde
]


def _local_to_utc(date_str: str, hour: int, minute: int, tz: ZoneInfo) -> str:
    """Convert a naive local datetime (YYYY-MM-DD HH:MM:SS) to UTC string."""
    dt_local = datetime.strptime(
        f"{date_str} {hour:02d}:{minute:02d}:00", "%Y-%m-%d %H:%M:%S"
    ).replace(tzinfo=tz)
    dt_utc = dt_local.astimezone(UTC)
    return dt_utc.strftime("%Y-%m-%d %H:%M:%S")


def _day_seed(date_str: str, channel_slug: str, slot_idx: int) -> int:
    """Deterministic seed for a date+channel+slot combination."""
    h = hashlib.md5(f"{date_str}::{channel_slug}::{slot_idx}".encode()).hexdigest()
    return int(h[:8], 16)


def _jitter_minutes(date_str: str, channel_slug: str, slot_idx: int,
                    before_min: int = None, after_min: int = None) -> int:
    """Return deterministic asymmetric jitter (-before_min .. +after_min)."""
    if before_min is None:
        before_min = JITTER_BEFORE_MIN
    if after_min is None:
        after_min = JITTER_AFTER_MIN
    seed = _day_seed(date_str, channel_slug, slot_idx)
    return (seed % (before_min + after_min + 1)) - before_min


def _minutes_to_utc_slot(date_str: str, total_min: int,
                          channel_id: int, channel_slug: str,
                          short_type: str, tz: ZoneInfo,
                          long_slot_position: int = None,
                          slot_position: int = 0,
                          gen_lead_min: int = None) -> dict:
    """Convert a minute-of-day (local tz) to a slot dict with UTC timestamps.

    Handles overflow past midnight: target_upload_at / scheduled_at spill
    into the next day, but date_key stays as the original planning day.

    Args:
        gen_lead_min: generation lead time in minutes. Defaults to
            SHORT_GEN_LEAD_MIN (15) for native, pass SHORT_GEN_LEAD_MIN_CLIP (5)
            for pre-rendered clips.
    """
    if gen_lead_min is None:
        gen_lead_min = SHORT_GEN_LEAD_MIN
    # Handle overflow past midnight: adjust effective date for datetime
    # conversion, but keep original date_key (slot was planned for this day)
    overflow_days = total_min // (24 * 60)
    if overflow_days > 0:
        dt_date = date.fromisoformat(date_str)
        dt_date = dt_date + timedelta(days=overflow_days)
        effective_date_str = dt_date.isoformat()
        total_min = total_min % (24 * 60)
    else:
        effective_date_str = date_str
        total_min = max(0, total_min)

    h, m = total_min // 60, total_min % 60
    target_utc = _local_to_utc(effective_date_str, h, m, tz)
    sched_total = max(0, total_min - gen_lead_min)
    sh, sm = sched_total // 60, sched_total % 60
    sched_utc = _local_to_utc(effective_date_str, sh, sm, tz)
    return {
        "channel_id": channel_id,
        "date_key": date_str,  # keep original planning date
        "scheduled_at": sched_utc,
        "target_upload_at": target_utc,
        "short_type": short_type,
        "long_slot_position": long_slot_position,
        "slot_position": slot_position,
        "channel_slug": channel_slug,
    }


def _load_shorts_optimal_windows(db=None, channel_id=None) -> list[tuple]:
    """Load optimal publish windows for shorts (native or clip).

    Returns list of (hour, minute) tuples, sorted by hour.
    Falls back to NATIVE_WINDOWS if no DB or no optimal slots found.
    """
    windows = list(NATIVE_WINDOWS)  # default fallback
    if db is not None and channel_id is not None:
        try:
            optimal_slots = db.get_optimal_publish_slots(
                channel_id, content_type="short",
            )
            if optimal_slots:
                windows = []
                seen = set()
                for s in optimal_slots:
                    h = s.get("target_hour")
                    m = s.get("target_minute", 0)
                    key = (h, m)
                    if h is not None and key not in seen:
                        windows.append((h, m))
                        seen.add(key)
                windows.sort(key=lambda x: (x[0], x[1]))
                if windows:
                    logger.debug(
                        "_load_shorts_optimal_windows: using %d optimal slots for ch=%d",
                        len(windows), channel_id,
                    )
        except Exception:
            pass
    return windows


def _snap_to_optimal_shorts_window(
    anchor_utc: datetime,
    timezone_str: str = "Europe/Madrid",
    db=None,
    channel_id=None,
    min_delay_min: int = 60,
) -> datetime:
    """Snap a UTC datetime to the nearest upcoming optimal shorts publish window.

    v10.3 (Aug 2026): Used by clip pre-render to ensure clips land in
    optimal publishing windows instead of blind 60-min increments.

    Args:
        anchor_utc: UTC datetime to anchor from (e.g., long video target_public_at).
        timezone_str: Channel timezone for local hour calculations.
        db: Database instance for optimal slots lookup.
        channel_id: Channel ID for optimal slots lookup.
        min_delay_min: Minimum delay from anchor before the first clip can publish.
            Default 60 min (to allow pre-render time).

    Returns:
        UTC datetime snapped to the nearest upcoming optimal window.
        If no window is available within 24h, returns anchor + min_delay_min.
    """
    windows = _load_shorts_optimal_windows(db, channel_id)
    if not windows:
        return anchor_utc + timedelta(minutes=min_delay_min)

    tz = ZoneInfo(timezone_str) if timezone_str else UTC
    anchor_local = anchor_utc.astimezone(tz)
    earliest_local = anchor_local + timedelta(minutes=min_delay_min)

    # Try today's windows first, then tomorrow's
    for day_offset in (0, 1):
        for (h, m) in windows:
            candidate = earliest_local.replace(
                hour=h, minute=m, second=0, microsecond=0,
            )
            if day_offset > 0:
                candidate += timedelta(days=day_offset)
            if candidate > earliest_local:
                result_utc = candidate.astimezone(UTC)
                logger.debug(
                    "_snap_to_optimal_shorts_window: snapped %s → %s "
                    "(window %02d:%02d, delay=%d min)",
                    anchor_utc.isoformat()[:16],
                    result_utc.isoformat()[:16],
                    h, m,
                    int((result_utc - anchor_utc).total_seconds() / 60),
                )
                return result_utc

    # Fallback: no window available (shouldn't happen with 4 windows × 2 days)
    return anchor_utc + timedelta(minutes=min_delay_min)


def _build_shorts_slots_for_channel(
    ch: dict,
    date_str: str,
    native_count: int,
    clips_per_long: int,
    long_video_count: int,
    long_target_hours: list[int],
    global_start_pos: int,
) -> tuple[list[dict], int]:
    """Generate shorts slots for ONE channel for a given date.

    v14: Uses optimal publish slots (epsilon-greedy) for native shorts,
    per-channel timezone (PUBLISH_TIMEZONE), asymmetric jitter,
    and records slot usage for performance feedback.
    Clips also prefer optimal short slots within their anchor windows.

    Args:
        ch: channel dict with at least id, slug, name
        date_str: YYYY-MM-DD
        native_count: how many native shorts today
        clips_per_long: multiplier (default 3)
        long_video_count: how many long videos published yesterday
        long_target_hours: target publish hours for those long videos (local tz)
        global_start_pos: starting slot_position for this channel

    Returns (slots_list, next_global_pos).
    """
    slug = ch["slug"]
    channel_id = ch["id"]
    all_slots = []
    pos = global_start_pos

    clip_count = clips_per_long * long_video_count

    # ── Load channel timezone from config ──
    tz = DEFAULT_TIMEZONE
    try:
        from config.config_bridge import get_channel_config
        ch_config = get_channel_config(slug)
        tz_str = getattr(ch_config, "PUBLISH_TIMEZONE", None)
        if tz_str:
            tz = ZoneInfo(tz_str)
    except Exception:
        pass

    # ── Load optimal publish slots for shorts from DB ──
    optimal_franjas = []  # list of (hour, minute, slot_rank)
    try:
        from database.db_extended import ExtendedDatabase
        _db = ExtendedDatabase()
        optimal_slots = _db.get_optimal_slots(channel_id, "short")
        if optimal_slots and len(optimal_slots) >= 1:
            for s in optimal_slots:
                optimal_franjas.append((
                    s["target_hour"],
                    s.get("target_minute", 0),
                    s["slot_rank"],
                ))
            optimal_franjas.sort(key=lambda x: (x[0], x[1]))
            logger.debug("Using %d optimal short slots for %s: %s",
                         len(optimal_franjas), slug,
                         [f"{h:02d}:{m:02d}" for h, m, _ in optimal_franjas])
    except Exception as exc:
        logger.debug("Optimal shorts slots lookup skipped for %s: %s", slug, exc)

    # ── Fallback franjas if no optimal slots ──
    if not optimal_franjas:
        optimal_franjas = [(int(w[0]), int(w[1]), 0)
                           for w in NATIVE_WINDOWS[:3]]
        logger.debug("[%s] Using fallback native windows: %s", slug,
                     [f"{h:02d}:{m:02d}" for h, m, _ in optimal_franjas])

    # ── 1. Native slots: one per optimal franja (epsilon-greedy picks the slot) ──
    for i in range(native_count):
        # Pick a franja: round-robin through available ones
        franja_h, franja_m, slot_rank = optimal_franjas[i % len(optimal_franjas)]
        base_min = franja_h * 60 + franja_m
        jitter = _jitter_minutes(date_str, slug, i)
        total_min = base_min + jitter
        total_min = max(DAY_START_MINUTES, min(total_min, 24 * 60 - 1))
        total_min = int(total_min)
        all_slots.append((total_min, "native", None, slot_rank))

        # Record slot usage for epsilon-greedy feedback
        try:
            _db.record_slot_usage(channel_id, "short", slot_rank)
        except Exception:
            pass

    # ── 2. Clip slots: NO LONGER PRE-PLANNED ─────────────────────────
    # v26: Clip shorts are generated ad-hoc right after long video generation
    # (before cleanup), and go directly to "Pendiente subida" column with
    # scheduled upload times. They do NOT appear in "Planificado".
    # The old pre-planning logic (lines below) is removed.
    # clip_count is kept at 0 — only native slots are pre-planned.
    _clip_count = 0  # explicitly zero

    # ── 3. Sort all slots by time and resolve collisions ──
    #    Same-type (native↔native, clip↔clip) → 60min publish gap enforced
    #    Cross-type (native↔clip) → overlap allowed, only 35min gen gap
    #    Iterates forward through resolved slots; skips later slots to avoid
    #    false-positive negative gaps from reversed-order checking.
    all_slots.sort(key=lambda x: x[0])

    resolved = []
    for total_min, slot_type, long_pos, slot_rank in all_slots:
        pushed_min = total_min
        for prev_min, prev_type, _, _ in resolved:
            if prev_min >= pushed_min:
                continue  # This resolved slot is after us — not a collision
            if slot_type == prev_type:
                # Same type: enforce publish-level gap
                if pushed_min - prev_min < _same_type_gap_minutes():
                    pushed_min = prev_min + _same_type_gap_minutes()
            else:
                # Cross-type (native↔clip): enforce publish gap (v10.3)
                if pushed_min - prev_min < _cross_type_gap_minutes():
                    pushed_min = prev_min + _cross_type_gap_minutes()
        pushed_min = pushed_min  # no end-of-day clamp — _minutes_to_utc_slot handles overflow
        resolved.append((pushed_min, slot_type, long_pos, slot_rank))

    # ── 3b. Dedup same-type slots at the exact same minute ──
    #    When multiple same-type slots collide at the exact same minute
    #    (e.g. end-of-day clamping from earlier collision rounds before
    #    the overflow fix), push later ones forward by the gap.
    #    Overflow past midnight is handled by _minutes_to_utc_slot.
    deduped: list = []
    for minutes_val, stype, long_pos, rank in resolved:
        pushed = minutes_val
        for prev_min, prev_type, _, _ in deduped:
            if prev_type == stype and prev_min == pushed:
                pushed = prev_min + _same_type_gap_minutes()
        deduped.append((pushed, stype, long_pos, rank))
    resolved = deduped

    # ── 4. Build slot dicts ──
    slots = []
    for total_min, slot_type, long_pos, slot_rank in resolved:
        pos += 1
        slot = _minutes_to_utc_slot(
            date_str, total_min, channel_id, slug,
            short_type=slot_type,
            tz=tz,
            long_slot_position=long_pos,
            slot_position=pos,
        )
        slot["channel_name"] = ch.get("name", slug)
        slot["slot_rank"] = slot_rank  # track which optimal slot was used
        slots.append(slot)

    logger.debug(
        "[%s] Built %d shorts slots: %d native + 0 clip "
        "(clips generated ad-hoc after long video)",
        slug, len(slots), native_count,
    )

    return slots, pos


def _build_filler_slots_for_channel(
    ch: dict,
    date_str: str,
    fillers_needed: int,
    existing_slots: list[dict],
    global_start_pos: int,
) -> tuple[list[dict], int]:
    """Create filler shorts slots to meet MIN_DAILY_SHORTS floor.

    Fillers are scheduled in low-audience windows (FILLER_WINDOWS)
    and do not cannibalize optimal franja slots. They use the same
    collision resolution as regular native slots to avoid overcrowding.

    Args:
        ch: channel dict
        date_str: YYYY-MM-DD
        fillers_needed: how many extra filler slots to create
        existing_slots: already-built slot dicts for this channel (from _build_shorts_slots_for_channel)
        global_start_pos: current slot position counter

    Returns (filler_slots, next_global_pos).
    """
    slug = ch["slug"]
    channel_id = ch["id"]
    pos = global_start_pos

    # Load channel timezone from config
    tz = DEFAULT_TIMEZONE
    try:
        from config.config_bridge import get_channel_config
        ch_config = get_channel_config(slug)
        tz_str = getattr(ch_config, "PUBLISH_TIMEZONE", None)
        if tz_str:
            tz = ZoneInfo(tz_str)
    except Exception:
        pass

    # Extract existing slot minute-of-day values to avoid collisions
    existing_minutes = []
    for s in existing_slots:
        # Parse slot's target_upload_at minute
        tu = s.get("target_upload_at", "")
        try:
            parts = str(tu).replace("T", " ").split(" ")
            time_part = parts[1].split(":")
            h, m = int(time_part[0]), int(time_part[1])
            existing_minutes.append(h * 60 + m)
        except (ValueError, IndexError):
            pass

    filler_slots = []
    for i in range(fillers_needed):
        # Round-robin through filler windows
        franja_h, franja_m = FILLER_WINDOWS[i % len(FILLER_WINDOWS)]
        base_min = franja_h * 60 + franja_m
        jitter = _jitter_minutes(date_str, f"{slug}_filler", i,
                                 before_min=15, after_min=15)
        total_min = base_min + jitter
        total_min = max(0, min(total_min, 24 * 60 - 1))

        # Resolve collisions with existing slots (same type = native)
        pushed_min = total_min
        for prev_min in sorted(existing_minutes):
            if pushed_min - prev_min < _same_type_gap_minutes():
                pushed_min = prev_min + _same_type_gap_minutes()

        existing_minutes.append(pushed_min)
        existing_minutes.sort()

        slot = _minutes_to_utc_slot(
            date_str, pushed_min, channel_id, slug,
            short_type="native",
            tz=tz,
            long_slot_position=None,
            slot_position=pos + 1,
        )
        pos += 1
        slot["channel_name"] = ch.get("name", slug)
        slot["slot_rank"] = -1  # filler — no optimal slot rank
        slot["is_filler"] = True  # mark so UI/analytics can distinguish
        filler_slots.append(slot)

    logger.debug(
        "[%s] Added %d filler slots (total now %d)",
        slug, len(filler_slots), len(existing_slots) + len(filler_slots),
    )

    return filler_slots, pos


def _get_yesterday_published_count(channel_id: int, date_str: str, db=None) -> int:
    """Count long-form videos published/uploaded on the day before date_str.
    
    Clip shorts are now based on yesterday's published videos (not today's planned).
    Returns the count of videos with status IN ('uploaded','published','uploaded_private')
    for the date_key = (date_str - 1 day).
    """
    from datetime import datetime as _dt, timedelta
    yesterday = (_dt.strptime(date_str, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")
    try:
        if db is None:
            from database.db_extended import ExtendedDatabase
            db = ExtendedDatabase()
        count = db.count_completed_videos_for_date(channel_id, yesterday)
        return count
    except Exception as exc:
        logger.debug("Yesterday published count lookup failed for ch%d date=%s: %s",
                     channel_id, yesterday, exc)
        return 0


def _get_planned_long_video_count(channel_id: int, date_str: str) -> tuple[int, list[int]]:
    """Get how many long-form videos are planned today for a channel.

    Returns (count, target_hours_cest_list).
    Falls back to deterministic calculation if no planned_slots exist yet.
    """
    try:
        from database.db_extended import ExtendedDatabase
        db = ExtendedDatabase()
        # Try planned_slots first (most accurate)
        slots = db.get_planned_slots(date_key=date_str, channel_id=channel_id)
        if slots:
            # Count non-cancelled slots
            active = [s for s in slots if s.get("status") != "cancelled"]
            count = len(active)
            # Extract target upload hours (local tz, parse from DB string)
            hours = []
            for s in active:
                tu = s.get("target_upload_at") or s.get("target_public_at") or s.get("scheduled_at") or ""
                try:
                    parts = tu.replace("T", " ").strip().split(" ")[1].split(":")[:2]
                    h = int(parts[0])
                    hours.append(h)
                except (ValueError, IndexError):
                    pass
            if count > 0:
                logger.debug("Channel %d: %d planned longs today → %s", channel_id, count, hours)
                return count, hours

        # Fallback: compute deterministic videos_per_day from channel config
        ch = db.get_channel(channel_id)
        if not ch:
            return 0, []
        # Use planning_service._resolve_videos_per_day for alternate patterns
        try:
            import json as _json
            config = _json.loads(ch.get("config_json", "{}"))
            pattern = config.get("alternate_pattern")
            if pattern and isinstance(pattern, list) and len(pattern) >= 2:
                day_ordinal = datetime.strptime(date_str, "%Y-%m-%d").toordinal()
                offset = config.get("alternate_offset", 0)
                idx = (day_ordinal + offset) % len(pattern)
                count = pattern[idx]
            else:
                count = config.get("videos_per_day", 0)
        except (ValueError, TypeError, KeyError):
            count = 0

        return max(0, count), []
    except Exception as exc:
        logger.debug("Long video count lookup failed for ch%d: %s", channel_id, exc)
        return 0, []


# ═══════════════════════════════════════════════════════════════════
# v2: Adaptive native/clip distribution
# ═══════════════════════════════════════════════════════════════════

def _load_native_ratio(channel_id: int, db) -> float:
    """Load shorts_native_ratio from DB, falling back to config default."""
    try:
        configs = db.get_shorts_planning_config(channel_id=channel_id)
        if configs and configs[0].get("shorts_native_ratio") is not None:
            ratio = float(configs[0]["shorts_native_ratio"])
            return max(0.10, min(0.90, ratio))
    except Exception:
        pass
    from config.defaults import SHORTS_NATIVE_RATIO
    return SHORTS_NATIVE_RATIO


def _evaluate_adaptive_ratio(channel_id: int, db, current_ratio: float) -> float:
    """Evaluate native vs clip performance and adjust ratio by ±10%.

    Checks every SHORTS_ADAPTIVE_CHECK_DAYS (default 14).
    - If native subs/short > clip * 1.3 → increase native ratio
    - If clip avg_views > native * 1.3 → decrease native ratio
    - Otherwise → maintain
    Clamped to [SHORTS_ADAPTIVE_RATIO_MIN, SHORTS_ADAPTIVE_RATIO_MAX].
    Saves the adjusted ratio back to DB.
    """
    from config.defaults import (
        SHORTS_ADAPTIVE_DISTRIBUTION, SHORTS_ADAPTIVE_CHECK_DAYS,
        SHORTS_ADAPTIVE_RATIO_MIN, SHORTS_ADAPTIVE_RATIO_MAX, SHORTS_ADAPTIVE_STEP,
    )
    if not SHORTS_ADAPTIVE_DISTRIBUTION:
        return current_ratio

    # ── Check if evaluation is due ──
    try:
        last_check_raw = None
        try:
            conn = db._connect()
            row = conn.execute(
                """SELECT value FROM system_state WHERE key = ?""",
                (f"shorts_adaptive_last_check_{channel_id}",),
            ).fetchone()
            conn.close()
            if row:
                last_check_raw = row["value"]
        except Exception:
            pass

        import time as _time
        now_ts = _time.time()
        if last_check_raw:
            last_check_ts = float(last_check_raw)
            days_since = (now_ts - last_check_ts) / 86400
            if days_since < SHORTS_ADAPTIVE_CHECK_DAYS:
                logger.debug(
                    "[shorts_adaptive] ch%d: last check %.1f days ago (<%d) — skipping",
                    channel_id, days_since, SHORTS_ADAPTIVE_CHECK_DAYS,
                )
                return current_ratio
    except Exception:
        pass  # proceed with evaluation if state check fails

    # ── Fetch performance data ──
    try:
        native = db.get_short_type_stats(channel_id, "native", SHORTS_ADAPTIVE_CHECK_DAYS)
        clip = db.get_short_type_stats(channel_id, "clip", SHORTS_ADAPTIVE_CHECK_DAYS)
    except Exception as e:
        logger.warning("[shorts_adaptive] ch%d: stats fetch failed: %s — keeping ratio %.2f",
                       channel_id, e, current_ratio)
        return current_ratio

    # ── Compare ──
    new_ratio = current_ratio
    native_subs = native.get("subs_per_short", 0)
    clip_subs = clip.get("subs_per_short", 0)
    native_views = native.get("avg_views", 0)
    clip_views = clip.get("avg_views", 0)

    logger.info(
        "[shorts_adaptive] ch%d — native: %d shorts, %.1f avg_views, %.1f subs/short | "
        "clip: %d shorts, %.1f avg_views, %.1f subs/short",
        channel_id,
        native.get("total_shorts", 0), native_views, native_subs,
        clip.get("total_shorts", 0), clip_views, clip_subs,
    )

    # Need a minimum of 3 shorts of each type for meaningful comparison
    if native.get("total_shorts", 0) < 3 or clip.get("total_shorts", 0) < 3:
        logger.info(
            "[shorts_adaptive] ch%d: insufficient data (native=%d, clip=%d) — keeping ratio %.2f",
            channel_id,
            native.get("total_shorts", 0), clip.get("total_shorts", 0), current_ratio,
        )
        return current_ratio

    if native_subs > 0 and native_subs > clip_subs * 1.3:
        new_ratio = min(SHORTS_ADAPTIVE_RATIO_MAX, current_ratio + SHORTS_ADAPTIVE_STEP)
        logger.info(
            "[shorts_adaptive] ch%d: native subs/short (%.1f) > clip subs/short (%.1f) ×1.3 "
            "→ INCREASE native ratio %.2f → %.2f",
            channel_id, native_subs, clip_subs, current_ratio, new_ratio,
        )
    elif clip_views > 0 and clip_views > native_views * 1.3:
        new_ratio = max(SHORTS_ADAPTIVE_RATIO_MIN, current_ratio - SHORTS_ADAPTIVE_STEP)
        logger.info(
            "[shorts_adaptive] ch%d: clip avg_views (%.1f) > native avg_views (%.1f) ×1.3 "
            "→ DECREASE native ratio %.2f → %.2f",
            channel_id, clip_views, native_views, current_ratio, new_ratio,
        )
    else:
        logger.info(
            "[shorts_adaptive] ch%d: performance similar — maintaining ratio %.2f",
            channel_id, current_ratio,
        )

    # ── Persist updated ratio and timestamp ──
    if abs(new_ratio - current_ratio) > 0.001:
        try:
            db.update_shorts_planning_config(channel_id, {"shorts_native_ratio": new_ratio})
        except Exception as e:
            logger.warning("[shorts_adaptive] ch%d: failed to persist ratio: %s", channel_id, e)

    try:
        conn = db._connect()
        conn.execute(
            """INSERT OR REPLACE INTO system_state (key, value)
               VALUES (?, ?)""",
            (f"shorts_adaptive_last_check_{channel_id}", str(int(now_ts))),
        )
        conn.commit()
        conn.close()
    except Exception:
        pass

    return new_ratio


def get_shorts_distribution(channel_id: int, db, date_str: str) -> tuple[int, int]:
    """Return (native_count, clip_count) for today based on adaptive ratio.

    Clips are fixed at SHORTS_CLIPS_PER_LONG × yesterday's published long videos.
    Natives fill the rest of the daily quota, floored at SHORTS_MIN_NATIVE_PER_DAY.
    """
    from config.settings import MAX_DAILY_SHORTS
    from config.defaults import SHORTS_CLIPS_PER_LONG, SHORTS_MIN_NATIVE_PER_DAY

    ratio = _evaluate_adaptive_ratio(channel_id, db,
                                    _load_native_ratio(channel_id, db))

    # Clip count is deterministic: clips_per_long × long videos published yesterday
    yesterday_clips = SHORTS_CLIPS_PER_LONG * _get_yesterday_published_count(channel_id, date_str, db)

    total = MAX_DAILY_SHORTS
    # Natives fill the remaining slots (with floor)
    target_natives = max(0, round(total * ratio))
    natives = max(SHORTS_MIN_NATIVE_PER_DAY, target_natives)
    # Real clip capability: don't claim more clips than we can actually produce
    clips = min(yesterday_clips, total)

    logger.info(
        "[shorts_adaptive] ch%d (%s): ratio=%.2f, natives=%d, clips_available=%d, "
        "clips_scheduled=%d, total=%d",
        channel_id, date_str, ratio, natives, yesterday_clips, clips, natives,
    )
    return natives, clips


def compute_daily_shorts_slots(date_str: str, db=None) -> list[dict]:
    """Compute shorts slots per active channel for a given date (YYYY-MM-DD).

    v2: Native count determined by adaptive native_ratio (35% default).
    Clips are 3 × long_videos_published_yesterday, generated ad-hoc.
    Fillers pad to MIN_DAILY_SHORTS (8) using low-audience windows.
    Timestamps are converted to UTC for storage.

    Returns list of dicts sorted by scheduled_at.
    """
    if db is None:
        from database.db_extended import ExtendedDatabase
        db = ExtendedDatabase()

    from config.settings import MIN_DAILY_SHORTS, MAX_DAILY_SHORTS

    # Get active channels
    channels = db.get_channels(active_only=True)
    channels = [ch for ch in channels if ch["slug"] != "test"]

    if len(channels) < 1:
        logger.warning("No active channels found — cannot compute shorts schedule")
        return []

    # Get per-channel shorts configs
    shorts_configs = db.get_shorts_planning_config()
    config_by_chid = {sc["channel_id"]: sc for sc in shorts_configs}

    # ── Quota-aware (ago 2026): cupo de shorts nativos por proyecto GCP ──
    # El reparto del presupuesto automático (6 subidas/día/proyecto) prioriza
    # longs y clips; los nativos se recortan si el proyecto no tiene cupo.
    native_caps: dict[int, int] = {}
    try:
        from api.services.planning_service import compute_daily_upload_allocation
        alloc = compute_daily_upload_allocation(db, date_str)
        for proj, data in alloc.items():
            for slug, caps in data.get("channels", {}).items():
                for ch in channels:
                    if ch.get("slug") == slug:
                        native_caps[int(ch["id"])] = int(caps.get("native", 0))
                        break
    except Exception as exc:
        logger.debug("Shorts planning: quota allocation skipped (%s)", exc)

    all_slots = []
    global_pos = 0

    for ch in channels:
        ch_id = ch["id"]
        slug = ch["slug"]
        sc = config_by_chid.get(ch_id, {})
        if not sc.get("shorts_enabled", True):
            continue

        native_count = sc.get("shorts_native_per_day", 3)
        clips_per_long = sc.get("shorts_clips_per_long", 3)

        # ── v2: Use adaptive distribution if enabled ──
        try:
            v2_native, v2_clips = get_shorts_distribution(ch_id, db, date_str)
            native_count = v2_native
            # clips_per_long stays for _build_shorts_slots_for_channel compat,
            # but actual clip slots are generated ad-hoc (v26)
        except Exception as e:
            logger.warning("[%s] Adaptive distribution failed, using static config: %s", slug, e)

        # ── Quota-aware: recortar nativos al cupo del proyecto ──
        cap = native_caps.get(ch_id)
        if cap is not None:
            if cap <= 0:
                logger.info(
                    "[%s] %s: 0 shorts nativos (cupo del proyecto agotado por longs+clips)",
                    slug, date_str,
                )
                continue
            if native_count > cap:
                logger.info(
                    "[%s] %s: shorts nativos %d → %d (cupo del proyecto)",
                    slug, date_str, native_count, cap,
                )
                native_count = cap

        if native_count == 0 and clips_per_long == 0:
            continue

        # ── Idempotency guard: skip if channel already has enough native slots ──
        # Prevents duplicate slot creation when compute_daily_shorts_slots is
        # called multiple times for the same date (e.g. server restart, recovery).
        try:
            existing_ch_slots = db.get_shorts_planned_slots(
                date_key=date_str, channel_id=ch_id,
            )
            existing_native = [
                s for s in existing_ch_slots
                if s.get("short_type") == "native"
                and s.get("status") in ("pending", "running", "completed")
            ]
            if len(existing_native) >= native_count:
                logger.debug(
                    "[%s] Skipping %s: %d native slots already exist (>=%d configured)",
                    slug, date_str, len(existing_native), native_count,
                )
                continue
        except Exception:
            pass

        # Dynamic: how many long-form videos were published yesterday?
        # Clip slots are now based on YESTERDAY's published videos, not today's planned.
        yesterday_count = _get_yesterday_published_count(ch_id, date_str, db)
        # No real target hours for yesterday's videos → use empty list (defaults apply)
        long_target_hours: list[int] = []

        channel_slots, global_pos = _build_shorts_slots_for_channel(
            ch, date_str, native_count, clips_per_long,
            yesterday_count, long_target_hours, global_pos,
        )

        # ── Filler: disabled during quota remediation ──────────────
        # v2: Native shorts count toward the daily floor.
        # Clips are generated ad-hoc after long videos (v26).
        total_planned = native_count

        from config.settings import YT_REMEDIATION_MODE
        if not YT_REMEDIATION_MODE and total_planned < MIN_DAILY_SHORTS:
            fillers_needed = min(
                MIN_DAILY_SHORTS - total_planned,
                MAX_DAILY_SHORTS - total_planned,
            )
            if fillers_needed > 0:
                logger.info(
                    "[%s] Adding %d filler shorts to reach floor (%d < %d): "
                    "planned=%d native",
                    slug, fillers_needed, total_planned, MIN_DAILY_SHORTS,
                    native_count,
                )
                filler_slots, global_pos = _build_filler_slots_for_channel(
                    ch, date_str, fillers_needed, channel_slots, global_pos,
                )
                channel_slots.extend(filler_slots)

        all_slots.extend(channel_slots)

    # Sort by scheduled_at
    all_slots.sort(key=lambda s: s["scheduled_at"])

    # Re-number slot positions after final sort
    for pos, s in enumerate(all_slots, 1):
        s["slot_position"] = pos

    return all_slots


def persist_daily_shorts_slots(date_str: str, slots: list[dict], db=None) -> int:
    """Store computed shorts slots in shorts_planned_slots table.
    Deletes existing pending slots for this date first.
    Returns count of stored slots.
    """
    if db is None:
        from database.db_extended import ExtendedDatabase
        db = ExtendedDatabase()

    # Delete existing PENDING slots for this date (keep completed/running)
    with db._connect() as conn:
        conn.execute(
            "DELETE FROM shorts_planned_slots WHERE date_key = ? AND status = 'pending'",
            (date_str,),
        )
        conn.commit()

    if not slots:
        return 0

    count = db.create_shorts_planned_slots_batch(slots)
    logger.info("Persisted %d shorts slots for %s", count, date_str)
    return count


def _inject_compensation_native_slots(date_str: str, db) -> int:
    """Inject 2 extra native short slots per channel for today only.
    
    Called from generate_upcoming_shorts() when today's slots already exist
    and would normally be skipped by the early-return guard.
    
    v25: One-day compensation for cancelled clip shorts (2026-07-31).
    Distributes slots across remaining future time windows.
    """
    import sqlite3
    from datetime import datetime, timezone
    from config.settings import DATABASE_PATH
    
    if date_str != '2026-07-31':
        return 0
    
    now_utc = datetime.now(timezone.utc)
    # Future windows from now until 04:00 CEST (02:00 UTC next day)
    # Hour:minute tuples in UTC (CEST = UTC+2)
    future_windows = []
    current_hour = now_utc.hour
    for h in range(current_hour + 1, 24):
        future_windows.append((h, 15))  # hh:15
        future_windows.append((h, 45))  # hh:45
    for h in range(0, 3):  # 00:00–02:00 UTC = 02:00–04:00 CEST
        future_windows.append((h, 15))
        future_windows.append((h, 45))
    
    # Get enabled channels (only active)
    channels = db.get_channels(active_only=True)
    
    conn = sqlite3.connect(str(DATABASE_PATH), timeout=30)
    inserted = 0
    slot_idx = 0
    
    for ch in channels:
        ch_id = ch['id']
        slug = ch.get('slug', 'unknown')
        
        # Check if channel already has native slots pending for today
        existing_pending = conn.execute(
            """SELECT COUNT(*) FROM shorts_planned_slots
               WHERE channel_id = ? AND date_key = ? AND short_type = 'native' AND status = 'pending'""",
            (ch_id, date_str),
        ).fetchone()[0]
        
        if existing_pending >= 2:
            continue  # already has enough
        
        needed = 2 - existing_pending
        for n in range(needed):
            if slot_idx >= len(future_windows):
                break
            h, m = future_windows[slot_idx]
            slot_idx += 1
            
            scheduled = f'{date_str} {h:02d}:{m:02d}:00'
            target = f'{date_str} {h:02d}:{m + 5:02d}:00'
            
            # Get max slot_position for this channel+date
            max_pos = conn.execute(
                """SELECT COALESCE(MAX(slot_position), 0) FROM shorts_planned_slots
                   WHERE channel_id = ? AND date_key = ?""",
                (ch_id, date_str),
            ).fetchone()[0]
            
            conn.execute(
                """INSERT INTO shorts_planned_slots
                   (channel_id, date_key, scheduled_at, target_upload_at, short_type,
                    status, slot_position, slot_rank, source_mode)
                   VALUES (?, ?, ?, ?, 'native', 'pending', ?, ?, 'original')""",
                (ch_id, date_str, scheduled, target, max_pos + 1, 9999),
            )
            inserted += 1
    
    conn.commit()
    conn.close()
    logger.info(
        "One-day native compensation: injected %d extra native slots for %s across %d channels",
        inserted, date_str, len(channels),
    )
    return inserted


def generate_upcoming_shorts(days: int = 7, db=None) -> dict:
    """Generate and persist shorts slots for the next N days (including today).

    Returns summary dict: {date_str: "N slots" | "ERROR: ..."}.
    """
    if db is None:
        from database.db_extended import ExtendedDatabase
        db = ExtendedDatabase()

    today = datetime.now(DEFAULT_TIMEZONE).date()
    results = {}

    for day_offset in range(days):
        day_str = (today + timedelta(days=day_offset)).isoformat()
        try:
            # Skip if date already has PENDING slots — avoids double-generation
            # on server restart. Running/completed/cancelled slots don't block
            # planning: they represent work in-progress or already finished.
            existing_pending = db.get_shorts_planned_slots(date_key=day_str, status="pending")
            if existing_pending and len(existing_pending) > 0:
                # ── v25: One-day native compensation (2026-07-31 only) ──
                # All clip shorts for today were cancelled (pre-render switch).
                # Inject 2 extra native short slots per channel for today,
                # distributed across remaining future windows.
                if day_str == '2026-07-31':
                    _inject_compensation_native_slots(day_str, db)
                results[day_str] = f"{len(existing_pending)} slots (pending, skipped)"
                logger.debug("Shorts slots for %s: %d pending — skipping regeneration", day_str, len(existing_pending))
                continue

            slots = compute_daily_shorts_slots(day_str, db)

            # ── Today: discard past-due slots so they don't fire immediately ──
            # scheduled_at is stored in UTC (see _minutes_to_utc_slot), so the
            # "now" reference MUST be UTC too. Using local time (CEST) here made
            # every replan drop slots that were actually up to 2h in the future,
            # orphaning their pre-rendered shorts (status 'ready' without slot).
            if day_offset == 0 and slots:
                now_utc_str = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S")
                future = [s for s in slots if s.get("scheduled_at", "") >= now_utc_str]
                dropped = len(slots) - len(future)
                if dropped > 0:
                    logger.info(
                        "Dropped %d past-due shorts slots for %s "
                        "(already behind current time %s)",
                        dropped, day_str, now_utc_str[:16],
                    )
                slots = future

            count = persist_daily_shorts_slots(day_str, slots, db) if slots else 0
            results[day_str] = f"{count} slots"
        except Exception as e:
            logger.error("Failed to generate shorts schedule for %s: %s", day_str, e)
            results[day_str] = f"ERROR: {e}"

    total = sum(
        int(v.split()[0]) for v in results.values() if v and v[0].isdigit()
    )
    logger.info(
        "Generated shorts slots for %d days: %d total",
        days, total,
    )
    return results


def ensure_today_shorts_scheduled(db=None) -> bool:
    """Check if today has pending/running shorts slots. If not, generate them.
    
    Only considers 'pending' and 'running' slots as needing to exist.
    Completed, cancelled, and failed slots are ignored - they don't prevent
    regeneration of new pending slots.
    
    BUT: if there are already completed slots for today, the system has been
    active and the recovery planner is managing deficits. Do NOT blindly
    regenerate — that would create a cancel-regenerate loop with the recovery
    planner. Let recovery handle any gaps.
    
    Returns True if slots exist (existing or newly generated).
    """
    if db is None:
        from database.db_extended import ExtendedDatabase
        db = ExtendedDatabase()

    today = datetime.now(DEFAULT_TIMEZONE).date().isoformat()
    existing = db.get_shorts_planned_slots(date_key=today)

    # Only count pending and running slots as "needs no regeneration"
    active_slots = [s for s in existing if s["status"] in ("pending", "running")]
    if len(active_slots) > 0:
        logger.debug("Today's shorts schedule OK: %d pending/running slots", len(active_slots))
        return True

    # Guard: if there are completed slots for today, the system has been
    # working. Don't regenerate — the recovery planner manages gaps.
    completed_slots = [s for s in existing if s["status"] == "completed"]
    if len(completed_slots) > 0:
        logger.info(
            "Today has %d completed shorts slots — no pending/running. "
            "Deferring to recovery planner (avoids cancel-regenerate loop).",
            len(completed_slots),
        )
        return True

    logger.info("No pending/running shorts slots for today — regenerating schedule")
    slots = compute_daily_shorts_slots(today, db)
    count = persist_daily_shorts_slots(today, slots, db)
    return count > 0


def cleanup_excessive_shorts_slots(db=None) -> dict:
    """Cancel excessive pending shorts slots for today.
    
    Ensures per-channel native slots <= shorts_native_per_day and
    per-channel clip slots <= shorts_clips_per_long * completed_longs.
    Called at startup to prevent spam after a period of over-generation.
    
    Returns dict with {cancelled_native, cancelled_clip} counts.
    """
    if db is None:
        from database.db_extended import ExtendedDatabase
        db = ExtendedDatabase()
    
    today = datetime.now(DEFAULT_TIMEZONE).date().isoformat()
    result = {"cancelled_native": 0, "cancelled_clip": 0}
    
    try:
        configs = db.get_shorts_planning_config()
        for cfg in configs:
            ch_id = cfg["channel_id"]
            if not cfg.get("shorts_enabled", True):
                continue
            
            native_max = cfg.get("shorts_native_per_day", 3)
            clips_per_long = cfg.get("shorts_clips_per_long", 3)
            
            # Count completed longs for clip target
            from database.db_extended import ExtendedDatabase as _EDB
            import sqlite3 as _sqlite3
            from config.settings import DATABASE_PATH as _DB_PATH
            conn = _sqlite3.connect(str(_DB_PATH), timeout=10)
            conn.row_factory = _sqlite3.Row
            long_count = conn.execute(
                """SELECT COUNT(*) as cnt FROM videos
                   WHERE channel_id = ?
                     AND status IN ('uploaded', 'published', 'uploaded_private')
                     AND DATE(uploaded_at) >= DATE('now', 'localtime', '-1 day')""",
                (ch_id,),
            ).fetchone()
            clip_max = clips_per_long * (long_count["cnt"] if long_count else 0)
            
            # Cancel excess native pending slots (keep only first N)
            native_pending = conn.execute(
                """SELECT id FROM shorts_planned_slots
                   WHERE date_key = ? AND channel_id = ?
                     AND short_type = 'native' AND status = 'pending'
                   ORDER BY scheduled_at ASC""",
                (today, ch_id),
            ).fetchall()
            native_ids = [r["id"] for r in native_pending]
            if len(native_ids) > native_max:
                to_cancel = native_ids[native_max:]
                conn.executemany(
                    "UPDATE shorts_planned_slots SET status = 'cancelled', "
                    "updated_at = datetime('now') WHERE id = ?",
                    [(i,) for i in to_cancel],
                )
                result["cancelled_native"] += len(to_cancel)
                logger.warning(
                    "Cleaned up %d excess native slots for channel #%d (kept %d/%d)",
                    len(to_cancel), ch_id, native_max, len(native_ids),
                )
            
            # Cancel excess clip pending slots
            clip_pending = conn.execute(
                """SELECT id FROM shorts_planned_slots
                   WHERE date_key = ? AND channel_id = ?
                     AND short_type = 'clip' AND status = 'pending'
                   ORDER BY scheduled_at ASC""",
                (today, ch_id),
            ).fetchall()
            clip_ids = [r["id"] for r in clip_pending]
            if len(clip_ids) > clip_max:
                to_cancel = clip_ids[clip_max:]
                conn.executemany(
                    "UPDATE shorts_planned_slots SET status = 'cancelled', "
                    "updated_at = datetime('now') WHERE id = ?",
                    [(i,) for i in to_cancel],
                )
                result["cancelled_clip"] += len(to_cancel)
                logger.warning(
                    "Cleaned up %d excess clip slots for channel #%d (kept %d/%d)",
                    len(to_cancel), ch_id, clip_max, len(clip_ids),
                )
            
            conn.commit()
        conn.close()
        
        if result["cancelled_native"] > 0 or result["cancelled_clip"] > 0:
            logger.info(
                "Shorts cleanup: cancelled %d native + %d clip excess slots",
                result["cancelled_native"], result["cancelled_clip"],
            )
    except Exception as e:
        logger.error("Shorts cleanup failed: %s", e)
    
    return result


# ── Smart shorts slot dispatcher ───────────────────────────────

def _fill_native_short_queue(db=None, loop=None) -> dict | None:
    """Relleno masivo de la cola de shorts nativos (generar SIN subir, Fase 2).

    Cuando no hay ningún slot nativo DUE para subir, la fábrica de shorts
    genera un nativo más a la cola (``generate_only`` → shorts.status='generated')
    hasta alcanzar MAX_QUEUED_NATIVE_SHORTS_PER_CHANNEL por canal. La válvula
    de goteo (``_upload_queued_native_shorts``) sube la cola paulatinamente
    según el perfil de pacing (1/día por canal en strike), colocando cada
    short en su hora pico.

    Guards: un short a la vez (global), RAM, shorts no pausados. Elige el
    canal con MENOS cola (justicia aproximada). Genera UNO por tick para no
    saturar LLM/RAM — la frecuencia la dicta el tick del scheduler.

    Returns:
        dict con el slot/job despachado, o None si no hay nada que rellenar.
    """
    import random
    from datetime import datetime as _dt_fill, timedelta as _td_fill, timezone as _tz_fill

    if db is None:
        from database.db_extended import ExtendedDatabase
        db = ExtendedDatabase()

    if _shorts_paused(db):
        return None

    # Gobernador de fábrica (Fase 4): disco bajo o créditos LLM → no generar.
    try:
        from api.services.factory_governor import factory_ok
        if not factory_ok(db):
            logger.debug("Fill cola nativos: gobernador de fábrica bloquea generación")
            return None
    except Exception:
        pass

    # Memoria: el render de un short es ffmpeg in-process (pico 2-4 GB).
    from config.settings import MIN_FREE_FOR_SHORTS_DISPATCH_MB
    if not _memory_ok(min_free_gb=MIN_FREE_FOR_SHORTS_DISPATCH_MB / 1024.0):
        return None

    # Un short a la vez (global)
    try:
        active_global = db.get_active_shorts_job()
    except Exception:
        active_global = None
    if active_global:
        return None

    try:
        from config.defaults import MAX_QUEUED_NATIVE_SHORTS_PER_CHANNEL
        target = int(MAX_QUEUED_NATIVE_SHORTS_PER_CHANNEL or 0)
    except Exception:
        target = 60
    if target <= 0:
        return None

    # Canal con MENOS cola por debajo del tope
    best = None
    try:
        channels = db.get_channels(active_only=True) or []
    except Exception:
        return None
    for ch in channels:
        cid = int(ch.get("id", 0) or 0)
        slug = ch.get("slug", "")
        if not cid:
            continue
        try:
            sc_rows = db.get_shorts_planning_config(cid)
            sc = sc_rows[0] if sc_rows else {}
        except Exception:
            sc = {}
        if not sc.get("shorts_enabled", True):
            continue
        # Canales spam-bloqueados ya rellenan su cola por su propia ruta
        # (generate_only durante el bloqueo) — no duplicar aquí.
        if _channel_shorts_spam_blocked(cid, db):
            continue
        try:
            queued = db.count_queued_native_shorts(cid)
        except Exception:
            queued = 0
        if queued >= target:
            continue
        if best is None or queued < best["queued"]:
            best = {"channel_id": cid, "slug": slug, "queued": queued}
    if best is None:
        return None

    cid = best["channel_id"]
    slug = best["slug"]

    # Slot nativo FUTURO (mañana en una ventana pico) — la generación ocurre
    # ahora, la publicación la gobierna la válvula cuando toque.
    try:
        tomorrow = date.today() + timedelta(days=1)
        window = random.choice(NATIVE_WINDOWS)
        total_min = window[0] * 60 + window[1]
        slot_dict = _minutes_to_utc_slot(
            tomorrow.isoformat(), total_min, cid, slug,
            short_type="native", tz=DEFAULT_TIMEZONE,
        )
        target_upload_at = slot_dict.get("target_upload_at")
        scheduled_at = slot_dict.get("scheduled_at") or _dt_fill.now(_tz_fill.utc).strftime("%Y-%m-%d %H:%M:%S")
    except Exception as exc:
        logger.debug("Fill: slot time computation failed (%s) — usando ahora+26h", exc)
        target_upload_at = (_dt_fill.now(_tz_fill.utc) + _td_fill(hours=26)).strftime("%Y-%m-%d %H:%M:%S")
        scheduled_at = _dt_fill.now(_tz_fill.utc).strftime("%Y-%m-%d %H:%M:%S")

    # Crear slot + job y despachar en generate_only (cola, sin subir)
    try:
        slot_id = db.create_shorts_slot(
            cid, tomorrow.isoformat(), scheduled_at,
            target_upload_at=target_upload_at, short_type="native",
        )
        job_id = db.create_job(cid, "generate_native_short")
        db.update_job(job_id, status="running")
        db.update_shorts_slot_status(slot_id, "running", job_id=job_id)

        _fill_coro = _dispatch_short_async(
            slot_id=slot_id,
            job_id=job_id,
            channel_id=cid,
            channel_slug=slug,
            short_type="native",
            target_upload_at=target_upload_at,
            generate_only=True,
        )
        if loop is not None:
            import asyncio as _asyncio_fill
            _asyncio_fill.run_coroutine_threadsafe(_fill_coro, loop)
        else:
            import asyncio as _asyncio_fill2
            _asyncio_fill2.create_task(_fill_coro)
    except Exception as exc:
        logger.warning("[%s] Fill native queue failed: %s", slug, exc)
        return None

    logger.info(
        "[%s] Fill cola nativos: slot #%d en cola (pub %s, cola=%d/%d)",
        slug, slot_id, str(target_upload_at)[:16], best["queued"] + 1, target,
    )
    return {"slot_id": slot_id, "job_id": job_id, "channel_slug": slug,
            "short_type": "native", "fill": True}


def dispatch_next_due_shorts_slot(db=None, loop=None) -> dict | None:
    """Check for due shorts planned slots and dispatch ONE.

    Called every 5 min by the API checker loop.
    Shorts can coexist with long-form generation (AGENTS.md excludes
    shorts from sequential-only limit). Guarded by: one-short-at-a-time,
    per-channel cooldown, and minimum RAM threshold.

    - For native slots: dispatch immediately
    - For clip slots: check if source long video exists (today, completed)
      If not, cancel the slot and retry with the next candidate (up to
      _MAX_CLIP_RETRIES times to avoid wasting scheduler ticks).

    Args:
        db: ExtendedDatabase instance (created if None).
        loop: asyncio event loop for scheduling the async worker. Required
              when called from a thread pool (e.g. via asyncio.to_thread).
              If None, falls back to asyncio.create_task (must be called
              from an active event loop thread).

    Returns:
        dict with dispatched slot info, or None if nothing to do.
    """
    import sqlite3
    from config.settings import DATABASE_PATH

    if db is None:
        from database.db_extended import ExtendedDatabase
        db = ExtendedDatabase()

    if _youtube_quota_blocked(db):
        logger.info("Shorts dispatch: YouTube quota exhausted — uploads paused")
        # La fábrica de shorts NO se detiene con la cuota: genera a la cola
        # (generate_only, sin subir) para drenarla al recuperar cuota.
        try:
            _fill = _fill_native_short_queue(db=db, loop=loop)
            if _fill:
                return _fill
        except Exception as exc:
            logger.warning("Shorts dispatch: fill durante pausa de cuota falló: %s", exc)
        return None

    # Kill-switch global de shorts (manual): shorts_paused / scheduler_paused
    if _shorts_paused(db):
        logger.info("Shorts dispatch: shorts pausados por kill-switch (shorts_paused)")
        return None

    # Tope global duro de shorts/día (anti-spam YouTube)
    if _global_shorts_daily_cap_reached(db):
        logger.info("Shorts dispatch: tope global diario alcanzado (%d shorts) — pausa", _global_shorts_daily_cap())
        # El tope aplica a SUBIDAS; la generación a cola puede seguir.
        try:
            _fill = _fill_native_short_queue(db=db, loop=loop)
            if _fill:
                return _fill
        except Exception as exc:
            logger.warning("Shorts dispatch: fill tras tope global falló: %s", exc)
        return None

    # ── Cola de shorts nativos generados durante bloqueos: subir gradualmente ──
    # Solo canales NO bloqueados; respeta cooldown, tope diario y cuota.
    try:
        _upload_queued_native_shorts(db, max_per_pass=2)
    except Exception as exc:
        logger.warning("Shorts dispatch: queued-native upload pasada falló: %s", exc)

    # 1. Sync running slots: mark completed/failed based on short status
    _sync_running_shorts_slots(db)

    # 2. Cancel stale pending slots (>24h past scheduled_at)
    _cancel_stale_shorts_slots(db)

    # 2b. Daily reset of dedup sets
    global _VIDEOS_WITHOUT_SCRIPT, _VIDEOS_WITHOUT_SCRIPT_DATE
    today = date.today().isoformat()
    if _VIDEOS_WITHOUT_SCRIPT_DATE != today:
        _VIDEOS_WITHOUT_SCRIPT.clear()
        _VIDEOS_WITHOUT_SCRIPT_DATE = today

    # 2c. Cooldown: if all slots were recently exhausted, skip this tick to reduce
    #     log spam and wasted DB queries (9 slots × 3 retries × 2 loops = 54 queries)
    global _LAST_ALL_EXHAUSTED_AT
    if _LAST_ALL_EXHAUSTED_AT > 0:
        elapsed = time.time() - _LAST_ALL_EXHAUSTED_AT
        if elapsed < _ALL_EXHAUSTED_COOLDOWN_SEC:
            logger.debug("Shorts dispatch cooldown: all slots exhausted %.0fs ago, skipping tick",
                         elapsed)
            return None
        _LAST_ALL_EXHAUSTED_AT = 0.0  # reset, cooldown expired

    # 3. Allowed concurrency: shorts coexist with long-form generation and uploads.
    #    Per AGENTS.md, shorts are excluded from the sequential-only limit.
    #    Guards #4 (per-channel short job check), #5 (min 1GB RAM), and #6 (per-channel
    #    cooldown) provide sufficient resource protection for low-footprint shorts.
    #
    #    NOTE: Guard #4 moved INSIDE the retry loop (per-channel check) instead of
    #    globally, so different channels can generate shorts in parallel.

    # 4. Memory gate (shorts render = ffmpeg in-process, peak 2-4 GB).
    #    Threshold raised from 1 GB (2026-08-19): 1 GB allowed dispatching
    #    shorts with the system already under pressure, and the kernel
    #    OOM-killed the API (killing in-process shorts with it).
    from config.settings import MIN_FREE_FOR_SHORTS_DISPATCH_MB
    if not _memory_ok(min_free_gb=MIN_FREE_FOR_SHORTS_DISPATCH_MB / 1024.0):
        logger.warning("Low memory — delaying shorts slot dispatch")
        return None

    # 5. Global concurrency guard: only ONE short at a time globally.
    #    Shorts rendering runs in thread pool threads (via asyncio.to_thread),
    #    each consuming significant CPU/RAM (ffmpeg, Kokoro TTS, LLM).
    #    Multiple concurrent shorts exhaust the thread pool and cause system
    #    contention. Limit to 1 globally, regardless of channel.
    active_global = db.get_active_shorts_job()
    if active_global:
        logger.debug(
            "Shorts dispatch deferred: short job #%d already running (channel=%s)",
            active_global.get("id"), active_global.get("channel_slug", "?"),
        )
        return None

    # ── Core dispatch loop (retries on clip cancellations + cooldown skips) ──
    # v25: Changed from fixed for-loop (3 retries) to while-loop where predictable
    # skips (no source channel / no resolved source video) do NOT consume retries.
    # This prevents a handful of unwinnable clip slots at the front of the queue
    # from consuming all retry iterations and blocking the entire dispatch pipeline.
    _MAX_CLIP_RETRIES = 3  # max effective retries (only counts hard failures)
    _skipped_slot_ids: set[int] = set()  # slots skipped due to cooldown/conflict
    _failed_force_ids: set[int] = set()  # force-dispatch: clip slots without source
    _retry_count = 0  # effective retry counter (only incremented on actual dispatch)

    # ── Pre-filter: cache which channels have completed long videos today ──
    # Clip shorts need a source long video. Pre-computing this avoids wasting
    # retry iterations on clip slots that will never succeed.
    _channels_with_source: set[int] = set()
    _channels_without_source: set[int] = set()
    try:
        today_long = db.get_completed_videos_today_all_channels()
        if today_long:
            for row in today_long:
                _channels_with_source.add(row.get("channel_id", 0))
        # Get all channel IDs that have ANY pending clip slot today
        # to mark channels that are known to lack a source video.
        pending_clip_channels = db.get_channels_with_pending_clip_slots_today()
        for ch_id in pending_clip_channels or []:
            if ch_id not in _channels_with_source:
                _channels_without_source.add(int(ch_id))
    except Exception:
        pass  # non-critical optimization

    while _retry_count < _MAX_CLIP_RETRIES:
        # 6. Get next pending short slot that is due (skip cooldown-blocked slots)
        exclude_list = list(_skipped_slot_ids) if _skipped_slot_ids else None
        next_slot = db.get_next_pending_shorts_slot(exclude_slot_ids=exclude_list)
        if not next_slot:
            if _skipped_slot_ids:
                logger.warning(
                    "No dispatchable shorts: %d slot(s) skipped (cooldown/conflict) "
                    "— all exhausted. Forcing dispatch of most urgent slot.",
                    len(_skipped_slot_ids),
                )
                # ── Fallback: all eligible slots are blocked by guards. ──────────
                # Try dispatching a slot from a DIFFERENT channel first — the
                # guards (cooldown, same-type gap, per-channel concurrency) are
                # per-channel, so another channel's slot should be unblocked.
                # Only if ALL channels' slots fail the guard checks do we
                # resort to bypassing them.
                #
                # Iterate: skip clip slots without source and retry up to
                # _MAX_CLIP_RETRIES times to find a viable slot.
                # v25: while-loop — predictable source-less skips don't consume retries.
                import time as _time
                _force_retry = 0
                _force_bypass_guards = False  # escalate to bypass only as last resort
                while _force_retry < _MAX_CLIP_RETRIES:
                    if _force_retry > 0:
                        _time.sleep(2)  # back off between retries to reduce log spam
                    force_slot = db.get_next_pending_shorts_slot(
                        exclude_slot_ids=list(_failed_force_ids) if _failed_force_ids else None
                    )
                    if not force_slot:
                        # ── Escalation: all slots tried, escalate to bypass mode ──
                        if not _force_bypass_guards and _failed_force_ids:
                            logger.warning(
                                "Force dispatch: %d slot(s) from all channels exhausted "
                                "guard checks — escalating to bypass mode",
                                len(_failed_force_ids),
                            )
                            _force_bypass_guards = True
                            _failed_force_ids.clear()
                            _force_retry = 0
                            continue  # restart loop with guard bypass
                        logger.warning(
                            "Force dispatch: no viable slots after %d attempts "
                            "(bypass=%s)",
                            _force_retry, _force_bypass_guards,
                        )
                        _alert_shorts_dispatch_exhausted(
                            f"Force dispatch sin slots viables tras {_force_retry} intentos "
                            f"(bypass={_force_bypass_guards}). Slots intentados: "
                            f"{list(sorted(_failed_force_ids))[:20] or 'ninguno'}."
                        )
                        # ── Fase 2: sin slots subibles → rellenar cola de nativos ──
                        try:
                            _fill = _fill_native_short_queue(db=db, loop=loop)
                            if _fill:
                                return _fill
                        except Exception as exc:
                            logger.debug("Shorts dispatch: fill fallback falló: %s", exc)
                        return None

                    slot_id = force_slot["id"]
                    channel_id = force_slot["channel_id"]
                    slug = force_slot.get("channel_slug", "")
                    short_type_f = force_slot.get("short_type", "native")
                    scheduled_f = force_slot.get("scheduled_at", "?")
                    slot_rank_f = force_slot.get("slot_rank", 0)
                    source_video_id_f = None

                    # Safety guard: skip inactive channels in force dispatch too.
                    if not force_slot.get("channel_active", 1):
                        logger.warning(
                            "Force dispatch: slot #%d (%s) — channel inactive, skipping",
                            slot_id, slug,
                        )
                        _failed_force_ids.add(slot_id)
                        continue

                    # ── HARD per-channel daily cap — SIEMPRE (incluso en bypass) ──
                    # A diferencia del resto de guards, este NO se salta nunca:
                    # es la garantía de "1 short/día por canal" tras los strikes.
                    if _channel_hard_daily_short_cap_reached(channel_id, db):
                        logger.info(
                            "Force dispatch: slot #%d (%s) — tope duro diario "
                            "(%d short(s)/día) — cancelando slot",
                            slot_id, slug, _hard_daily_cap(),
                        )
                        _failed_force_ids.add(slot_id)
                        db.update_shorts_slot_status(
                            slot_id, "cancelled",
                            error_message=f"hard daily cap ({_hard_daily_cap()}/día)",
                        )
                        continue

                    # ── Hard spam filter: channel blocked by a spam strike ──
                    _spam_gen_only_f = False
                    if _channel_shorts_spam_blocked(channel_id):
                        if short_type_f == "native":
                            _spam_gen_only_f = True
                            logger.info(
                                "Force dispatch: slot #%d (%s) — canal spam-bloqueado, "
                                "generando native en cola (generate_only, sin subir)",
                                slot_id, slug,
                            )
                            # No continuar: el dispatch normal generará sin subir.
                        else:
                            logger.warning(
                                "Force dispatch: slot #%d (%s) — channel spam-blocked, CANCELLING",
                                slot_id, slug,
                            )
                            _failed_force_ids.add(slot_id)
                            db.update_shorts_slot_status(
                                slot_id, "cancelled",
                                error_message="channel spam-blocked (YouTube removal)",
                            )
                            continue

                    # ── v10.5: Apply same-channel guards even in force dispatch ──
                    # Only bypass these guards as absolute last resort (after
                    # exhausting all channels). This prevents the force dispatch
                    # from becoming an avalanche that publishes shorts back-to-back.
                    if not _force_bypass_guards:
                        # Per-channel short job guard
                        active_ch = db.get_active_shorts_job_for_channel(channel_id)
                        if active_ch:
                            logger.info(
                                "Force dispatch: slot #%d (%s) — channel has active "
                                "job #%d, trying next channel",
                                slot_id, slug, active_ch["id"],
                            )
                            _failed_force_ids.add(slot_id)
                            continue

                        # Per-channel cooldown
                        if not _channel_shorts_cooldown_ok(channel_id, db):
                            logger.info(
                                "Force dispatch: slot #%d (%s) — cooldown active "
                                "(< %d min), trying next channel",
                                slot_id, slug, _shorts_cooldown_minutes(),
                            )
                            _failed_force_ids.add(slot_id)
                            continue

                        # Same-type/cross-type collision guard
                        target_up = force_slot.get("target_upload_at")
                        if _same_type_shorts_slot_conflict(channel_id, short_type_f,
                                                            target_up, db,
                                                            exclude_slot_id=slot_id,
                                                            cross_type=True):
                            logger.info(
                                "Force dispatch: slot #%d (%s) — publish conflict, "
                                "trying next channel",
                                slot_id, slug,
                            )
                            _failed_force_ids.add(slot_id)
                            continue

                    if short_type_f == "clip":
                        # Fase 0.2/1.6: clips sin source se CANCELAN (no se saltan en bucle).
                        # Antes quedaban 'pending' para siempre → livelock + spam de logs.
                        if channel_id in _channels_without_source:
                            logger.info(
                                "Force dispatch: clip slot #%d (%s) — channel has "
                                "no completed long videos today, CANCELLING",
                                slot_id, slug,
                            )
                            _failed_force_ids.add(slot_id)
                            db.update_shorts_slot_status(
                                slot_id, "cancelled",
                                error_message="no source: no completed long video today",
                            )
                            continue  # don't consume retry
                        source_video_id_f = _resolve_clip_source(channel_id,
                            force_slot.get("long_slot_position"))
                        if source_video_id_f is None:
                            logger.info(
                                "Force dispatch: clip slot #%d (%s) has no source — CANCELLING",
                                slot_id, slug,
                            )
                            _failed_force_ids.add(slot_id)
                            db.update_shorts_slot_status(
                                slot_id, "cancelled",
                                error_message="no source: clip source video unavailable",
                            )
                            continue  # don't consume retry

                    _force_retry += 1  # only consume retry when we actually dispatch

                    log_msg = (
                        "FORCE DISPATCH: slot #%d %s type=%s (scheduled %s) — "
                        "all other slots blocked by guards"
                    )
                    if _force_bypass_guards:
                        logger.warning(
                            log_msg + " [BYPASSING GUARDS: all channels exhausted]",
                            slot_id, slug, short_type_f, scheduled_f,
                        )
                    else:
                        logger.warning(
                            log_msg + " [different channel — guards passed]",
                            slot_id, slug, short_type_f, scheduled_f,
                        )

                    db.update_shorts_slot_status(slot_id, "running",
                                                  source_video_id=source_video_id_f)
                    conn = sqlite3.connect(str(DATABASE_PATH), timeout=30)
                    job_action_f = "generate_native_short" if short_type_f == "native" else "generate_clip_short"
                    job_id_f = db.create_job(channel_id, job_action_f)
                    db.update_job(job_id_f, status="running")
                    db.update_shorts_slot_status(slot_id, "running", job_id=job_id_f,
                                                  source_video_id=source_video_id_f)
                    conn.close()

                    # ── Schedule async worker safely ──
                    # asyncio.create_task fails with RuntimeError when called
                    # from a thread-pool thread (no running event loop). Use
                    # run_coroutine_threadsafe when a loop is provided by the
                    # caller (main.py passes it via asyncio.get_running_loop).
                    _short_async_coro = _dispatch_short_async(
                        slot_id=slot_id, job_id=job_id_f,
                        channel_id=channel_id, channel_slug=slug,
                        short_type=short_type_f,
                        source_video_id=source_video_id_f,
                        slot_rank=slot_rank_f,
                        target_upload_at=force_slot.get("target_upload_at"),
                        generate_only=_spam_gen_only_f,
                    )
                    if loop is not None:
                        import asyncio as _asyncio_f
                        _asyncio_f.run_coroutine_threadsafe(_short_async_coro, loop)
                    else:
                        import asyncio as _asyncio_f
                        _asyncio_f.create_task(_short_async_coro)
                    return {
                        "slot_id": slot_id, "job_id": job_id_f,
                        "channel_slug": slug, "short_type": short_type_f,
                    }

                logger.warning("Force dispatch: exhausted %d attempts", _MAX_CLIP_RETRIES)
                _LAST_ALL_EXHAUSTED_AT = time.time()
                _alert_shorts_dispatch_exhausted(
                    f"Force dispatch agotó sus {_MAX_CLIP_RETRIES} intentos (todos los "
                    f"slots bloqueados por guards o sin fuente). Slots intentados: "
                    f"{list(sorted(_failed_force_ids))[:20] or 'ninguno'}."
                )
                return None
            else:
                # Check if there are any pending slots at all (even outside window)
                try:
                    total_pending = db.count_shorts_by_status("pending")
                    if total_pending > 0:
                        next_due = db.get_earliest_pending_short()
                        if next_due:
                            logger.info(
                                "No shorts due yet — next slot: #%d at %s (%d total pending)",
                                next_due["id"], next_due.get("scheduled_at", "?")[:16],
                                total_pending,
                            )
                        else:
                            logger.info("No shorts due — %d pending but none in lookahead", total_pending)
                    else:
                        logger.info("No pending shorts slots at all")
                except Exception:
                    logger.info("No pending shorts slots due")
            return None

        slot_id = next_slot["id"]
        channel_id = next_slot["channel_id"]
        slug = next_slot.get("channel_slug", "")
        short_type = next_slot.get("short_type", "native")
        scheduled = next_slot.get("scheduled_at", "?")
        slot_rank = next_slot.get("slot_rank", 0)
        channel_active = next_slot.get("channel_active", 1)   # channels.active column from JOIN

        # Safety guard: skip inactive channels (should be filtered by
        # get_next_pending_shorts_slot SQL but belt-and-suspenders).
        if not channel_active:
            logger.warning(
                "Shorts slot #%d (%s): channel is inactive — skipping",
                slot_id, slug,
            )
            _skipped_slot_ids.add(slot_id)
            continue

        # ── Hard spam filter: channel blocked by a spam strike ──
        # Durante el ban NO se sube nada, pero los NATIVOS se GENERAN y quedan
        # en cola (status='generated') para despacharlos al expirar bloqueo+colchón.
        # Los CLIPS se cancelan (necesitan long-form publicado reciente).
        _spam_gen_only = False
        if _channel_shorts_spam_blocked(channel_id):
            if short_type == "native":
                # Tope de cola: no acumular sin límite (disco/LLM).
                try:
                    from config.defaults import MAX_QUEUED_NATIVE_SHORTS_PER_CHANNEL
                    queued = db.count_queued_native_shorts(channel_id)
                    if queued >= MAX_QUEUED_NATIVE_SHORTS_PER_CHANNEL:
                        logger.info(
                            "Shorts slot #%d (%s): canal spam-bloqueado y cola de "
                            "nativos llena (%d/%d) — cancelando slot",
                            slot_id, slug, queued, MAX_QUEUED_NATIVE_SHORTS_PER_CHANNEL,
                        )
                        _skipped_slot_ids.add(slot_id)
                        db.update_shorts_slot_status(
                            slot_id, "cancelled",
                            error_message="cola nativa llena durante bloqueo",
                        )
                        continue
                except Exception:
                    pass
                _spam_gen_only = True
                logger.info(
                    "Shorts slot #%d (%s): canal spam-bloqueado — generando native "
                    "en cola (generate_only, sin subir)",
                    slot_id, slug,
                )
                # NO continuar: dejar pasar al dispatch normal (generará sin subir).
            else:
                logger.warning(
                    "Shorts slot #%d (%s) skipped: channel spam-blocked — CANCELLING clip slot",
                    slot_id, slug,
                )
                _skipped_slot_ids.add(slot_id)
                db.update_shorts_slot_status(
                    slot_id, "cancelled",
                    error_message="channel spam-blocked (YouTube removal)",
                )
                continue

        # 4. Per-channel short job guard — skip to next slot instead of failing.
        #    Different channels can generate shorts in parallel since each has
        #    its own API keys, TTS resources, and render pipeline.
        active_channel_short = db.get_active_shorts_job_for_channel(channel_id)
        if active_channel_short:
            logger.info(
                "Shorts slot #%d (%s) skipped: channel already has active "
                "short job #%d — trying next channel",
                slot_id, slug, active_channel_short["id"],
            )
            _skipped_slot_ids.add(slot_id)
            continue

        # 5. Per-channel cooldown guard — skip to next slot instead of failing
        if not _channel_shorts_cooldown_ok(channel_id, db):
            logger.info(
                "Shorts slot #%d (%s) skipped: cooldown active "
                "(last short < %d min ago) — trying next channel",
                slot_id, slug, _shorts_cooldown_minutes(),
            )
            _skipped_slot_ids.add(slot_id)
            continue

        # 5a. HARD per-channel daily cap (anti-strike, NO saltable por catch-up
        # ni force-dispatch): máx shorts subidos hoy por canal (perfil de
        # pacing). Lo que dispara el flag de YouTube es la SUBIDA.
        if _channel_hard_daily_short_cap_reached(channel_id, db):
            logger.info(
                "Shorts slot #%d (%s) skipped: tope duro diario alcanzado "
                "(%d short(s) subidos hoy) — cancelando slot",
                slot_id, slug, _hard_daily_cap(),
            )
            db.update_shorts_slot_status(
                slot_id, "cancelled",
                error_message=f"hard daily cap ({_hard_daily_cap()}/día)",
            )
            _skipped_slot_ids.add(slot_id)
            continue

        # 5b. Native shorts daily cap: skip if channel already published
        # enough native shorts today (respects shorts_native_per_day config).
        if short_type == "native":
            try:
                published_native_today = db.count_native_shorts_published_today(channel_id)
                native_daily_max = db.get_native_shorts_per_day(channel_id)
                if published_native_today >= native_daily_max:
                    logger.info(
                        "Shorts slot #%d (%s) skipped: native daily cap reached "
                        "(%d/%d published today) — trying next channel",
                        slot_id, slug, published_native_today, native_daily_max,
                    )
                    # Cancel this slot — we don't need it
                    db.update_shorts_slot_status(slot_id, "cancelled")
                    _skipped_slot_ids.add(slot_id)
                    continue
            except Exception:
                pass  # non-critical guard failure

        # 8. Publish collision guard — same-type (45 min) + cross-type (20 min)
        target_upload = next_slot.get("target_upload_at")
        if _same_type_shorts_slot_conflict(channel_id, short_type, target_upload, db,
                                            exclude_slot_id=slot_id, cross_type=True):
            logger.info(
                "Shorts slot #%d (%s) skipped: publish conflict "
                "(%s within %d min same-type / %d min cross-type) — trying next channel",
                slot_id, slug, short_type,
                _same_type_gap_minutes(),
                _cross_type_gap_minutes(),
            )
            _skipped_slot_ids.add(slot_id)
            continue

        logger.info(
            "Dispatching shorts slot #%d: %s type=%s (scheduled %s)",
            slot_id, slug, short_type, scheduled,
        )

        # 9. For clip slots: check source video dependency (with pre-filter)
        source_video_id = None
        if short_type == "clip":
            # ── v25: Pre-rendered clip check ──
            # If this slot already has a pre-rendered short linked (status='ready',
            # file on disk), skip source resolution entirely. The dispatch path
            # will detect it and upload directly.
            slot_short_id = next_slot.get("short_id")
            if slot_short_id:
                # Verify the short exists and file is on disk
                conn_check = sqlite3.connect(str(DATABASE_PATH), timeout=10)
                conn_check.row_factory = sqlite3.Row
                pre_row = conn_check.execute(
                    "SELECT id, status, file_path FROM shorts WHERE id = ? AND status = 'ready'",
                    (slot_short_id,),
                ).fetchone()
                conn_check.close()
                if pre_row and pre_row["file_path"] and Path(pre_row["file_path"]).exists():
                    source_video_id = next_slot.get("source_video_id")
                    if source_video_id:
                        logger.info(
                            "Shorts slot #%d (%s): pre-rendered clip short #%d ready — "
                            "skipping source resolution, will upload directly",
                            slot_id, slug, slot_short_id,
                        )
                        # Skip the source resolve block below, go directly to dispatch
                    else:
                        logger.warning(
                            "Shorts slot #%d (%s): pre-rendered short #%d but "
                            "no source_video_id on slot — will try normal resolve",
                            slot_id, slug, slot_short_id,
                        )
                        source_video_id = None  # reset, fall through to normal resolve

            # If source_video_id is still None (no pre-render or pre-render check
            # failed), do the normal source resolution.
            if source_video_id is None:
                # Pre-filter: CANCEL clip slots for channels with no completed long
                # videos today (Fase 0.2/1.6) — antes quedaban 'pending' en bucle.
                if channel_id in _channels_without_source:
                    logger.info(
                        "Shorts slot #%d (%s): clip type but channel has "
                        "no completed long videos today — CANCELLING",
                        slot_id, slug,
                    )
                    _skipped_slot_ids.add(slot_id)
                    db.update_shorts_slot_status(
                        slot_id, "cancelled",
                        error_message="no source: no completed long video today",
                    )
                    continue

                long_pos = next_slot.get("long_slot_position")
                source_video_id = _resolve_clip_source(channel_id, long_pos)
                if source_video_id is None:
                    # ── Anti-churn: no reintentar indefinidamente un clip sin fuente.
                    # Si el slot lleva >2h sin poder resolver fuente, se CANCELA
                    # (no se reintenta más); antes quedaba 'pending' en bucle.
                    sched = next_slot.get("scheduled_at")
                    overdue = False
                    try:
                        if sched:
                            from datetime import datetime as _dt_ns
                            _sdt = _dt_ns.strptime(str(sched)[:19], "%Y-%m-%d %H:%M:%S")
                            overdue = (_dt_ns.now() - _sdt).total_seconds() > 7200
                    except Exception:
                        overdue = False
                    if overdue:
                        logger.info(
                            "Shorts slot #%d (%s): clip sin fuente >2h — CANCELLING (anti-churn)",
                            slot_id, slug,
                        )
                        db.update_shorts_slot_status(
                            slot_id, "cancelled",
                            error_message="no source: clip source video unavailable (overdue)",
                        )
                        continue
                    logger.info(
                        "Shorts slot #%d (%s) skipped: clip type but no completed source "
                        "long video available yet (channel=%s, long_slot=%s) — "
                        "keeping pending, retrying later",
                        slot_id, slug, channel_id, long_pos,
                    )
                    _skipped_slot_ids.add(slot_id)
                    continue  # ← retry with next candidate

        # All guards passed — this is a dispatchable slot. Only NOW do we
        # consume a retry count (predictable skips like no-source don't count).
        _retry_count += 1

        # 10. Mark slot as running with source_video_id
        db.update_shorts_slot_status(slot_id, "running", source_video_id=source_video_id)

        # 11. Create job record for tracking
        conn = sqlite3.connect(str(DATABASE_PATH), timeout=30)
        job_action = "generate_native_short" if short_type == "native" else "generate_clip_short"
        job_id = db.create_job(channel_id, job_action)

        # Mark job as running immediately
        db.update_job(job_id, status="running")

        # Link job to slot
        db.update_shorts_slot_status(slot_id, "running", job_id=job_id,
                                      source_video_id=source_video_id)
        conn.close()

        # 12. Dispatch the actual generation (fire and forget)
        # ── Schedule async worker safely ──
        # asyncio.create_task fails with RuntimeError when called
        # from a thread-pool thread (no running event loop). Use
        # run_coroutine_threadsafe when a loop is provided.
        _short_async_coro = _dispatch_short_async(
            slot_id=slot_id,
            job_id=job_id,
            channel_id=channel_id,
            channel_slug=slug,
            short_type=short_type,
            source_video_id=source_video_id,
            slot_rank=slot_rank,
            target_upload_at=next_slot.get("target_upload_at"),
            generate_only=_spam_gen_only,
        )
        if loop is not None:
            import asyncio as _asyncio2
            _asyncio2.run_coroutine_threadsafe(_short_async_coro, loop)
        else:
            import asyncio
            asyncio.create_task(_short_async_coro)

        return {
            "slot_id": slot_id,
            "job_id": job_id,
            "channel_slug": slug,
            "short_type": short_type,
        }

    # Exhausted all retries (e.g. all pending clip slots have no source)
    logger.warning("Shorts dispatch: exhausted %d clip retries — no dispatchable slot", _MAX_CLIP_RETRIES)
    # Anti-bucle (antiban, ago 2026): al agotar el presupuesto de reintentos se
    # alerta al operador en vez de rendirse en silencio (dedup global por tipo).
    _alert_shorts_dispatch_exhausted(
        "Dispatch principal sin slot despachable (p. ej. clips sin vídeo fuente)."
    )
    # ── Fase 2: sin slots subibles → rellenar cola de nativos (generación a cola) ──
    try:
        _fill = _fill_native_short_queue(db=db, loop=loop)
        if _fill:
            return _fill
    except Exception as exc:
        logger.debug("Shorts dispatch: fill fallback falló: %s", exc)
    return None


def _resolve_clip_source(channel_id: int, long_slot_position) -> int | None:
    """Find the completed long video for a clip short.
    
    long_slot_position 1 = first completed video today
    long_slot_position 2 = second completed video today
    
    Returns video_id or None if not available.
    
    v21: Excludes source videos that already have clip shorts published/pending
    today to prevent duplicate clips from the same long-form video.
    """
    import sqlite3
    from database.db_extended import ExtendedDatabase
    from config.settings import DATABASE_PATH
    db = ExtendedDatabase()

    videos = db.get_completed_videos_today(channel_id)
    if not videos:
        return None

    # ── v21: Exclude source videos that already have clip shorts today ──
    conn = sqlite3.connect(str(DATABASE_PATH), timeout=30)
    already_used_ids = set()
    try:
        used_rows = conn.execute(
            """SELECT DISTINCT source_video_id FROM shorts
               WHERE channel_id = ?
                 AND type = 'clip'
                 AND source_video_id IS NOT NULL
                 AND status IN ('published', 'uploading', 'ready', 'rendering', 'extracted')
                 AND date(created_at) = date('now', 'localtime')""",
            (channel_id,),
        ).fetchall()
        already_used_ids = {row[0] for row in used_rows}
    finally:
        conn.close()

    # Find the first available video that hasn't been used for clips today
    pos = long_slot_position or 1
    found = None
    for video in videos:
        if video["id"] not in already_used_ids:
            # v22: skip videos known to have no usable script (avoids retry loops)
            if video["id"] in _VIDEOS_WITHOUT_SCRIPT:
                continue
            # v (Aug 2026): skip sources that can't be accessed. A video in
            # 'uploaded_private' has been uploaded but NOT published yet, and its
            # local mp4 was deleted after upload → yt-dlp can't download a private
            # video, producing endless "No short_id returned" retries. Only use
            # sources that still have a local file OR are already public.
            _src_status = (video.get("status") or "").strip()
            _src_path = (video.get("video_path") or "").strip()
            if _src_status == "uploaded_private":
                _local_ok = False
                if _src_path:
                    for _p in (Path(_src_path), Path("/root/autotube") / _src_path):
                        if _p.exists():
                            _local_ok = True
                            break
                if not _local_ok:
                    logger.info(
                        "_resolve_clip_source: skipping #%d (uploaded_private, no local file)",
                        video["id"],
                    )
                    continue
            found = video
            break

    if found:
        logger.info(
            "_resolve_clip_source: selected video #%d (skipped %d already-used today)",
            found["id"], len(already_used_ids),
        )
        return found["id"]

    return None


async def _dispatch_short_async(slot_id: int, job_id: int, channel_id: int,
                                 channel_slug: str, short_type: str,
                                 source_video_id: int = None,
                                 slot_rank: int = 0,
                                 target_upload_at: str = None,
                                 generate_only: bool = False):
    """Async wrapper that dispatches the actual short generation and updates DB.

    IMPORTANT: _dispatch_native_short() and _dispatch_clip_short() are
    synchronous functions that block for 5+ minutes (LLM, TTS, ffmpeg).
    They MUST be called via asyncio.to_thread() to avoid blocking the
    uvicorn event loop thread. DO NOT call them synchronously here.

    v10.3: target_upload_at enables scheduled publishing (private + publishAt)
    instead of immediate public.

    vX (spam block): generate_only=True deja el native en cola (status='generated')
    sin subir, para despacharlo al expirar el bloqueo.
    """
    import asyncio
    import sqlite3
    from pathlib import Path
    from config.settings import DATABASE_PATH

    try:
        if short_type == "native":
            short_id = await asyncio.to_thread(
                _dispatch_native_short,
                channel_id, channel_slug,
                slot_rank=slot_rank, job_id=job_id,
                target_upload_at=target_upload_at,
                generate_only=generate_only,
                slot_id=slot_id,
            )
        elif short_type == "standalone":
            short_id = await asyncio.to_thread(
                _dispatch_standalone_short,
                channel_id, channel_slug,
                slot_rank=slot_rank, job_id=job_id,
                target_upload_at=target_upload_at,
            )
        else:
            # ── v25: Check for pre-rendered clip ──
            pre_rendered_short_id = None
            pre_rendered_status = None
            try:
                conn_check = sqlite3.connect(str(DATABASE_PATH), timeout=10)
                conn_check.row_factory = sqlite3.Row
                row = conn_check.execute(
                    """SELECT s.id, s.status, s.file_path
                       FROM shorts s
                       JOIN shorts_planned_slots sps ON sps.short_id = s.id
                       WHERE sps.id = ?
                         AND s.type = 'clip'
                         AND s.status = 'ready'""",
                    (slot_id,),
                ).fetchone()
                conn_check.close()
                if row and row["file_path"] and Path(row["file_path"]).exists():
                    pre_rendered_short_id = row["id"]
                    pre_rendered_status = row["status"]
                    logger.info(
                        "Shorts slot #%d: found pre-rendered clip short #%d (status=%s) — "
                        "skipping render, uploading directly",
                        slot_id, pre_rendered_short_id, pre_rendered_status,
                    )
            except Exception as _pr_check_err:
                logger.debug("Pre-render check for slot #%d failed (non-fatal): %s",
                             slot_id, _pr_check_err)

            short_id = await asyncio.to_thread(
                _dispatch_clip_short,
                channel_id, channel_slug, source_video_id,
                slot_rank=slot_rank, job_id=job_id,
                pre_rendered_short_id=pre_rendered_short_id,
                target_upload_at=target_upload_at,
            )

        if short_id:
            # Mark slot as completed
            conn = sqlite3.connect(str(DATABASE_PATH), timeout=30)
            conn.execute(
                "UPDATE shorts_planned_slots SET status = 'completed', short_id = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (short_id, slot_id),
            )
            conn.execute(
                "UPDATE generation_jobs SET status = 'completed' WHERE id = ?",
                (job_id,),
            )
            conn.commit()
            conn.close()
            logger.info("Shorts slot #%d completed: short_id=%d", slot_id, short_id)
        else:
            # ── Retry logic: don't permanently cancel on first failure ──
            # TTS or script failures are often transient (LLM word count,
            # voice speed). Instead of cancelling, set back to 'pending'
            # for up to 2 automatic retries. After 2 failures, cancel permanently.
            conn = sqlite3.connect(str(DATABASE_PATH), timeout=30)
            retries = 0
            try:
                row = conn.execute(
                    "SELECT retry_count FROM shorts_planned_slots WHERE id = ?",
                    (slot_id,),
                ).fetchone()
                retries = (int(row[0]) if row and row[0] else 0)
            except Exception:
                pass

            if retries < 2:
                conn.execute(
                    "UPDATE shorts_planned_slots SET status = 'pending', retry_count = ?, "
                    "error_message = 'Auto-retry after failure (attempt ' || ? || '/2)', "
                    "job_id = NULL, "
                    "scheduled_at = datetime('now', '+10 minutes'), "
                    "updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                    (retries + 1, retries + 1, slot_id),
                )
                conn.execute(
                    "UPDATE generation_jobs SET status = 'failed', "
                    "error_msg = 'No short_id returned (retry ' || ? || '/2)' WHERE id = ?",
                    (retries + 1, job_id),
                )
                conn.commit()
                conn.close()
                logger.warning(
                    "Shorts slot #%d failed (retry %d/2) — rescheduled as pending",
                    slot_id, retries + 1,
                )
            else:
                # Preservar el motivo REAL registrado por _native_fail() (fix ago 2026:
                # antes se pisaba con el genérico y no había alerta de fallo).
                _reason = ""
                try:
                    _row = conn.execute(
                        "SELECT error_message FROM shorts_planned_slots WHERE id = ?",
                        (slot_id,),
                    ).fetchone()
                    _reason = (_row["error_message"] if _row and _row["error_message"] else "")
                except Exception:
                    _reason = ""
                conn.execute(
                    "UPDATE shorts_planned_slots SET status = 'cancelled', "
                    "error_message = COALESCE(error_message, 'Exhausted retries (2/2)'), "
                    "updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                    (slot_id,),
                )
                conn.execute(
                    "UPDATE generation_jobs SET status = 'failed', "
                    "error_msg = COALESCE(error_msg, 'No short_id returned (exhausted retries)') WHERE id = ?",
                    (job_id,),
                )
                conn.commit()
                conn.close()
                logger.warning("Shorts slot #%d exhausted retries — cancelled (%s)",
                               slot_id, _reason[:100] or "sin motivo")
                _alert_short_dispatch_failed(slot_id, channel_id, _reason)
    except __import__("pipeline.youtube_uploader", fromlist=["SpamRemovalError"]).SpamRemovalError as e:
        # ── HARD SPAM FILTER ──
        # YouTube removed the uploaded short. NEVER retry (each retry is a new
        # spam signal). Cancel the slot. El strike se registra UNA sola vez en
        # _verify_upload_exists (punto único de detección), para no doblar el
        # conteo con la ruta long-form.
        logger.error(
            "Shorts slot #%d: YouTube REMOVED the short (%s) — spam strike, cancelling",
            slot_id, str(e)[:200],
        )
        try:
            conn = sqlite3.connect(str(DATABASE_PATH), timeout=30)
            conn.execute(
                "UPDATE shorts_planned_slots SET status = 'cancelled', "
                "error_message = 'YouTube removed short (spam) — no retry', "
                "updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (slot_id,),
            )
            conn.execute(
                "UPDATE generation_jobs SET status = 'failed', "
                "error_msg = ? WHERE id = ?",
                (f"SpamRemovalError: {str(e)[:300]}", job_id),
            )
            conn.commit()
            conn.close()
        except Exception:
            pass
    except __import__("pipeline.youtube_uploader", fromlist=["QuotaExhaustedError"]).QuotaExhaustedError as e:
        logger.warning("Shorts dispatch quota exhausted for slot #%d: %s", slot_id, e)
        try:
            conn = sqlite3.connect(str(DATABASE_PATH), timeout=30)
            conn.execute(
                "UPDATE shorts_planned_slots SET status = 'pending', "
                "error_message = 'YouTube quota exhausted — retry after reset', "
                "job_id = NULL, scheduled_at = datetime('now', '+12 hours'), "
                "updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (slot_id,),
            )
            conn.execute(
                "UPDATE generation_jobs SET status = 'failed', error_msg = ? WHERE id = ?",
                (f"Quota exhausted: {str(e)[:300]}", job_id),
            )
            conn.commit()
            conn.close()
        except Exception:
            pass
    except Exception as e:
        logger.error("Shorts dispatch error for slot #%d: %s", slot_id, e)
        try:
            conn = sqlite3.connect(str(DATABASE_PATH), timeout=30)
            retries = 0
            try:
                row = conn.execute(
                    "SELECT retry_count FROM shorts_planned_slots WHERE id = ?",
                    (slot_id,),
                ).fetchone()
                retries = (int(row[0]) if row and row[0] else 0)
            except Exception:
                pass

            if retries < 2:
                conn.execute(
                    "UPDATE shorts_planned_slots SET status = 'pending', retry_count = ?, "
                    "error_message = ?, job_id = NULL, "
                    "scheduled_at = datetime('now', '+10 minutes'), "
                    "updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                    (retries + 1, f"Auto-retry after error (attempt {retries+1}/2): {str(e)[:200]}", slot_id),
                )
                conn.execute(
                    "UPDATE generation_jobs SET status = 'failed', error_msg = ? WHERE id = ?",
                    (f"Exception (retry {retries+1}/2): {str(e)[:300]}", job_id),
                )
                conn.commit()
                logger.warning(
                    "Shorts slot #%d crashed (retry %d/2) — rescheduled as pending: %s",
                    slot_id, retries + 1, str(e)[:100],
                )
            else:
                conn.execute(
                    "UPDATE shorts_planned_slots SET status = 'cancelled', "
                    "error_message = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                    (f"Exhausted retries after error: {str(e)[:300]}", slot_id),
                )
                conn.execute(
                    "UPDATE generation_jobs SET status = 'failed', error_msg = ? WHERE id = ?",
                    (f"Exception (exhausted retries): {str(e)[:300]}", job_id),
                )
                conn.commit()
                logger.warning("Shorts slot #%d exhausted retries after crash — cancelled: %s", slot_id, str(e)[:100])
                _alert_short_dispatch_failed(slot_id, channel_id, f"Exception: {str(e)[:200]}")
            conn.close()
        except Exception:
            pass
    finally:
        # ── Release memory after EVERY short generation ──────────
        # Without this, Python's heap fragments and the OS never gets
        # pages back. gc.collect() finds unreachable objects;
        # malloc_trim(0) returns freed pages to the OS immediately.
        try:
            import gc
            gc.collect()
            import ctypes
            ctypes.CDLL("libc.so.6").malloc_trim(0)
        except Exception:
            pass

        # ── Dispatch-on-completion: immediately try next due short ──
        # Without this, the next short waits for the schedule checker
        # tick (1-5 min). By triggering now, we chain shorts back-to-back
        # and clear the backlog faster.
        #
        # ── Cleanup: sync running slots before chaining, to prevent ──
        #     orphaned 'running' slots from blocking the dispatch loop.
        try:
            _sync_running_shorts_slots(None)  # creates its own db connection
        except Exception:
            pass

        try:
            from config.settings import SHORTS_CHAIN_DISPATCH_ENABLED
            if not SHORTS_CHAIN_DISPATCH_ENABLED:
                return
            import asyncio as _asyncio
            _chain_loop = _asyncio.get_running_loop()
            async def _chain_next():
                await _asyncio.sleep(2)  # let DB changes settle
                try:
                    from api.services.shorts_scheduler import dispatch_next_due_shorts_slot
                    # Run in thread pool to avoid blocking the event loop.
                    # Pass _chain_loop so internal async scheduling uses
                    # run_coroutine_threadsafe instead of create_task.
                    next_result = await _chain_loop.run_in_executor(
                        None,
                        dispatch_next_due_shorts_slot,
                        None,       # db
                        _chain_loop,  # loop
                    )
                    if next_result:
                        logger.info(
                            "Chained next short: slot=%d channel=%s type=%s",
                            next_result["slot_id"], next_result["channel_slug"],
                            next_result["short_type"],
                        )
                    else:
                        logger.info("Chain dispatch: no due slots within 24h lookahead")
                except Exception as _chain_err:
                    logger.warning("Chain dispatch error: %s", _chain_err)
            _asyncio.create_task(_chain_next())
        except Exception:
            pass  # best-effort, never crash the finally block


# ── Short job progress helper ───────────────────────────────────

def _update_short_job_progress(job_id: int | None, progress: int, phase: str):
    """Update progress and phase for a shorts generation_jobs record.
    
    Called at key milestones during short generation so the pipeline
    view shows real-time progress instead of 0%. No-op when job_id is None
    (manual generation endpoints don't create job records).
    """
    if not job_id:
        return
    try:
        import sqlite3
        from config.settings import DATABASE_PATH
        conn = sqlite3.connect(str(DATABASE_PATH), timeout=15)
        conn.execute(
            "UPDATE generation_jobs SET progress = ?, phase = ? WHERE id = ?",
            (progress, phase, job_id),
        )
        conn.commit()
        conn.close()
        logger.info("Short job #%d progress: %d%% (%s)", job_id, progress, phase)
    except Exception as e:
        logger.warning("Short job progress update failed for #%d: %s", job_id, e)


def _native_fail(slot_id: int | None, job_id: int | None, reason: str) -> None:
    """Registra el MOTIVO REAL de un fallo de native short en slot y job.

    (fix ago 2026) Antes cada fallo hacía solo `return None`: el dispatcher
    veía un genérico "No short_id returned" y no se creaba ninguna alerta —
    la barra de generación "saltaba" en silencio. Con esto, el motivo queda
    en `shorts_planned_slots.error_message` y `generation_jobs.error_msg`
    para que `_dispatch_short_async` pueda alertar al agotar los reintentos.
    """
    if not slot_id and not job_id:
        return
    try:
        import sqlite3
        from config.settings import DATABASE_PATH
        conn = sqlite3.connect(str(DATABASE_PATH), timeout=15)
        if slot_id:
            conn.execute(
                "UPDATE shorts_planned_slots SET error_message = ?, "
                "updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (reason[:300], slot_id),
            )
        if job_id:
            conn.execute(
                "UPDATE generation_jobs SET error_msg = ? WHERE id = ?",
                (reason[:300], job_id),
            )
        conn.commit()
        conn.close()
        logger.warning("[native] short slot #%s job #%s fail: %s", slot_id, job_id, reason)
    except Exception as e:
        logger.warning("Native short fail record failed (slot=%s job=%s): %s",
                       slot_id, job_id, e)


def _alert_short_dispatch_failed(slot_id: int, channel_id: int, reason: str) -> None:
    """Alerta (deduplicada) cuando un slot de short se CANCELA tras agotar reintentos.

    (fix ago 2026) Los fallos de native short eran silenciosos: job 'failed' sin
    alerta. create_alert deduplica por (entity_type, entity_id, alert_type), así
    que hay UNA sola alerta sin resolver por slot.
    """
    try:
        from api.services.lifecycle_monitor import create_alert
        from database.db_extended import ExtendedDatabase
        create_alert(
            ExtendedDatabase(),
            entity_type="short", entity_id=slot_id, channel_id=channel_id,
            alert_type="short_dispatch_failed",
            severity="warning",
            title=f"Short cancelado tras reintentos (slot #{slot_id})",
            message=(
                f"El slot de short #{slot_id} se canceló tras agotar los reintentos "
                f"(2/2). Motivo: {reason or 'desconocido'}. El short NO se publicó."
            ),
            metadata={"slot_id": slot_id, "reason": reason},
        )
    except Exception as _alert_exc:
        logger.warning("short dispatch failed alert error: %s", _alert_exc)


# ── Standalone short generation ──────────────────────────────────

def _dispatch_standalone_short(channel_id: int, channel_slug: str,
                                slot_rank: int = 0, job_id: int = None,
                                target_upload_at: str = None) -> int | None:
    """Generate and publish a standalone Short with trending niche topic.

    Uses topic discovery (YouTube trending + LLM curation) to find
    high-CTR topics, then delegates to StandaloneShortsPipeline for
    script → TTS → media → render → upload.
    """
    import logging
    logger = logging.getLogger("autotube.standalone")

    try:
        from database.db_extended import ExtendedDatabase
        _db_s = ExtendedDatabase()
        # HARD per-channel daily cap (anti-strike).
        if _channel_hard_daily_short_cap_reached(channel_id, _db_s):
            logger.info(
                "[standalone] %s: tope duro diario (%d/día) alcanzado — skip",
                channel_slug, _hard_daily_cap(),
            )
            return None

        from pipeline.shorts_standalone import discover_standalone_topics, run_standalone_short

        # Discover 3 trending topics, pick the best one
        topics = discover_standalone_topics(channel_slug, count=3)
        if not topics:
            logger.warning("[standalone] No topics found for %s", channel_slug)
            return None

        topic = topics[0]  # Best topic first
        logger.info("[standalone] %s: running for topic '%s'", channel_slug, topic.get("title", "?"))

        # Content-safety pre-filter (anti-strike): rechazar temas sensibles.
        try:
            from pipeline.content_safety import classify_topic_safety
            _safety = classify_topic_safety(
                topic=topic.get("title", "") or topic.get("tema", ""),
                title=topic.get("title", ""),
                script_texts=[topic.get("description", ""), topic.get("hook", "")],
            )
            if not _safety.safe:
                logger.warning(
                    "[standalone] %s: tema rechazado por seguridad — %s",
                    channel_slug, _safety.reason,
                )
                return None
        except Exception as _cs_exc:
            logger.warning("[standalone] %s: safety check error (fail-open): %s", channel_slug, _cs_exc)

        short_id = run_standalone_short(
            channel_slug=channel_slug,
            topic=topic,
            channel_id=channel_id,
            job_id=job_id,
            target_upload_at=target_upload_at,
        )

        return short_id

    except Exception as e:
        logger.error("[standalone] Dispatch failed for %s: %s", channel_slug, e)
        return None


# ── Native short generation ────────────────────────────────────

def _dispatch_native_short(channel_id: int, channel_slug: str,
                            slot_rank: int = 0, job_id: int = None,
                            target_upload_at: str = None,
                            generate_only: bool = False,
                            slot_id: int = None) -> int | None:
    """Generate and publish a native Short.

    Uses the existing native short generation pipeline (LLM script → TTS → render → upload).

    v10.3: If target_upload_at is provided, uploads as private + publishAt
    (scheduled publishing) instead of immediate public.

    vX (spam block): si ``generate_only=True`` se genera y renderiza el short
    pero NO se sube: queda con status='generated' (en cola) para despacharlo
    gradualmente cuando expire el bloqueo + colchón.

    Returns short_id or None on failure.
    """
    import json
    import random
    import re
    import subprocess
    import time
    import sqlite3
    from pathlib import Path
    from config.settings import DATABASE_PATH, LLM_MODEL, OUTPUT_DIR
    from config.config_bridge import get_channel_config

    ch_config = get_channel_config(channel_slug)
    hashtags = getattr(ch_config, "SHORTS_HASHTAGS", ["#Shorts"])
    niche = getattr(ch_config, "CANAL_NARRATIVE_STYLE", "documental")
    display_name = getattr(ch_config, "CANAL_DISPLAY_NAME", channel_slug)
    tagline = getattr(ch_config, "CANAL_TAGLINE", "")

    # 0. Fetch recent topics to avoid repetition
    from database.db_extended import ExtendedDatabase
    dbx = ExtendedDatabase(str(DATABASE_PATH))
    recent_topics = dbx.get_recent_short_topics(channel_id, limit=15)
    topic_warning = ""
    if recent_topics:
        topic_list = "\n".join(f'  - "{t}"' for t in recent_topics)
        topic_warning = (
            f"\n\n⚠️ IMPORTANTE: NO repitas NINGUNO de estos temas ya publicados recientemente "
            f"en este canal:\n{topic_list}\n\n"
            f"Elige un tema COMPLETAMENTE DIFERENTE y fresco. "
            f"Incluye en el JSON un campo \"tema\" con una frase corta (max 80 chars) que "
            f"identifique claramente de qué trata este Short.\n"
        )
    else:
        topic_warning = (
            f"\n\nIncluye en el JSON un campo \"tema\" con una frase corta (max 80 chars) "
            f"que identifique claramente de qué trata este Short.\n"
        )

    # 0a. Recent TITLES as negatives (fix ago 2026) — el guard de títulos compara
    #     TÍTULOS (overlap ≥ 60%), pero al LLM solo se le pasaban los TEMAS, así
    #     que regeneraba títulos casi idénticos y el slot reintentaba en bucle.
    #     Ahora se listan los títulos recientes con una regla explícita.
    recent_titles: list[str] = []
    try:
        recent_titles = (
            [t for _, t in _recent_short_titles(channel_id)]
            + [t for _, t in _recent_longform_titles(channel_id)]
        )
        # Dedup preservando orden + cap
        recent_titles = list(dict.fromkeys(t for t in recent_titles if t))[:15]
    except Exception:
        recent_titles = []
    title_warning = ""
    if recent_titles:
        title_warning = (
            "\n\n⚠️ TÍTULOS YA USADOS RECIENTEMENTE (NO los repitas):\n"
            + "\n".join(f'  - "{t[:80]}"' for t in recent_titles)
            + "\n\nRegla DURA: tu campo 'titulo' NO debe compartir más del 50% de "
              "palabras con NINGUNO de los títulos anteriores. Inventa un título y "
              "un ángulo NUEVOS (otro tema, otro gancho, otra perspectiva).\n"
        )
    else:
        title_warning = (
            "\n\nGenera un título ORIGINAL que no repita el patrón de títulos "
            "recientes del canal.\n"
        )

    # 0b. Extract visual theme context (v8) — run BEFORE script generation
    #      so the LLM can generate theme-aware search queries
    theme_context = None
    try:
        from pipeline.theme_extractor import ThemeExtractor
        theme_extractor = ThemeExtractor(config=ch_config)
        # Build a synthetic content text from the channel context and topic
        theme_content = (
            f"Short sobre: {niche}. Tagline: {tagline}."
        )
        theme_context = theme_extractor.extract(
            content_text=theme_content[:2000],
            channel_name=display_name,
            channel_theme=tagline,
            niche_keywords=getattr(ch_config, "NICHE_KEYWORDS_ENG", None),
        )
        if theme_context and theme_context.theme_keywords_en:
            logger.info(
                "Shorts theme extracted for %s: genre=%s keywords=%s",
                channel_slug, theme_context.genre, theme_context.theme_keywords_en,
            )
        else:
            theme_context = None
    except Exception as te:
        logger.debug("Shorts theme extraction skipped (non-fatal): %s", te)
        theme_context = None

    # ── Build theme context block for the LLM prompt (v8) ──────
    theme_block = ""
    if theme_context:
        tl = ["\nCONTEXTO TEMÁTICO DEL SHORT (ancla visual para las escenas):"]
        if theme_context.genre and theme_context.genre != "documental":
            tl.append(f"- Género: {theme_context.genre}")
        if theme_context.era and theme_context.era != "atemporal":
            tl.append(f"- Época: {theme_context.era}")
        if theme_context.primary_subject:
            tl.append(f"- Sujeto visual: {theme_context.primary_subject}")
        if theme_context.key_motifs:
            tl.append(f"- Motivos visuales: {', '.join(theme_context.key_motifs[:4])}")
        if theme_context.mood:
            tl.append(f"- Mood: {theme_context.mood}")
        if theme_context.forbidden_elements:
            tl.append(f"- ⛔ NUNCA incluir en search_query_en: {', '.join(theme_context.forbidden_elements)}")
        tl.append("\nUsa este contexto para generar search_query_en ancladas al MISMO mundo visual.")
        theme_block = "\n".join(tl) + "\n"

    # 1. Script via LLM — bucle anti-título-repetido (fix ago 2026)
    #    El guard de títulos rechaza overlaps ≥ 60% con títulos recientes. Antes
    #    eso descartaba TODO el script y el slot reintentaba desde cero con el
    #    mismo prompt (mismos títulos). Ahora se reintenta el LLM hasta 3 veces
    #    con feedback del conflicto ANTES de rendirse.
    from config.llm_client import create_llm_client
    from config.llm_helpers import llm_json_call
    from pipeline.shorts_tts import validate_short_script, MAX_WORD_COUNT as _MAX_WORDS, MIN_WORD_COUNT as _MIN_WORDS
    client = create_llm_client(enable_thinking=False)

    MAX_SCRIPT_ATTEMPTS = 3
    conflict_feedback = ""
    script = None
    for _attempt in range(MAX_SCRIPT_ATTEMPTS):
        _is_last = (_attempt == MAX_SCRIPT_ATTEMPTS - 1)
        try:
            script = llm_json_call(
                client,
                max_retries=3,
                retry_delay=2.0,
                model=LLM_MODEL,
                messages=[{"role": "user", "content": (
                    f"Genera un Short viral en español de ~50-58 segundos (~70-85 palabras totales, minimo 70). "
                    f"Canal: {display_name} — {niche}. Tagline: {tagline}."
                    f"{topic_warning}"
                    f"{title_warning}"
                    f"{theme_block}"  # v8: visual theme context for query anchoring
                    f"{conflict_feedback}"
                    f"Usa entre 6 y 8 bloques: hook, [desarrollo1, desarrollo2, (desarrollo3 opcional)], climax, cierre. "
                    f"IMPORTANTE: los bloques de desarrollo y climax deben tener 3-4 frases cada uno. "
                    f"Hook y cierre: 2-3 frases. Minimo 12 palabras por bloque, maximo 18. "
                    f"El total debe superar 70 palabras y no exceder 90. "
                    f"Añade desarrollo3 SOLO si el tema lo justifica (mas variedad visual). "
                    f"PARA CADA BLOQUE genera 'search_query_en': 5-8 keywords EN INGLÉS para buscar "
                    f"imagenes y videos de stock que coincidan EXACTAMENTE con lo narrado en ese momento. "
                    f"Incluye tema + detalles visuales (iluminacion, tipo de plano, atmosfera, accion). "
                    f"NO uses espanol (las APIs de stock no lo entienden). "
                    f"Ademas genera 'theme_keywords_en': 5-8 keywords EN INGLES del tema visual GLOBAL "
                    f"del short para mantener coherencia entre escenas. "
                    f"Devuelve SOLO JSON: "
                    f'{{"tema": "frase corta que identifica el tema (max 80 chars)", '
                    f'"titulo": "...", "hook_text": "frase de gancho 8-12 palabras", '
                    f'"theme_keywords_en": ["global", "theme", "keywords"], '
                    f'"bloques": [{{"tipo": "hook", "texto": "1-2 frases", '
                    f'"search_query_en": "english keywords for stock search"}}, '
                    f'{{"tipo": "desarrollo1", "texto": "2-3 frases con contexto y detalle", '
                    f'"search_query_en": "english keywords"}}, '
                    f'{{"tipo": "desarrollo2", "texto": "2-3 frases con dato impactante especifico", '
                    f'"search_query_en": "english keywords"}}, '
                    f'{{"tipo": "desarrollo3", "texto": "2-3 frases con detalle adicional (opcional)", '
                    f'"search_query_en": "english keywords"}}, '
                    f'{{"tipo": "climax", "texto": "2-3 frases con la consecuencia o revelacion", '
                    f'"search_query_en": "english keywords"}}, '
                    f'{{"tipo": "cierre", "texto": "1-2 frases cierre + suscribete", '
                    f'"search_query_en": "english keywords"}}]}}. '
                    f"NADA MAS fuera del JSON. El array bloques debe tener entre 5 y 7 elementos."
                )}],
                temperature=0.9, max_tokens=1800,
            )
        except Exception as e:
            logger.error("Short script generation failed after retries for %s: %s", channel_slug, e)
            _native_fail(slot_id, job_id, f"script LLM error: {str(e)[:200]}")
            return None

        # 1a-bis. Type guard: llm_json_call may return a list (when the LLM
        # outputs a JSON array instead of an object), which would crash
        # validate_short_script below with "'list' has no attribute 'get'".
        if not isinstance(script, dict):
            if not _is_last:
                conflict_feedback = (
                    "❌ La respuesta anterior no era un objeto JSON válido. "
                    "Devuelve SOLO un objeto JSON con el schema indicado.\n"
                )
                continue
            logger.error("Short script generation returned unexpected type %s for %s",
                         type(script).__name__, channel_slug)
            _native_fail(slot_id, job_id, f"script LLM devolvió tipo inesperado: {type(script).__name__}")
            return None

        # 1b. Validate script completeness (with smart truncation for over-long scripts)
        errors = validate_short_script(script)
        if errors:
            # Separate structural errors from word-count issues
            structural_errors = [e for e in errors if "too long" not in e and "too short" not in e
                                 and "words" not in e and "Blocks" not in e and "blocks" not in e]
            if structural_errors:
                if not _is_last:
                    conflict_feedback = (
                        f"❌ El script anterior no cumplía el schema: {structural_errors[:2]}. "
                        f"Regénéralo cumpliendo EXACTAMENTE el schema indicado.\n"
                    )
                    continue
                logger.error("Short script validation failed for %s: %s", channel_slug, structural_errors)
                _native_fail(slot_id, job_id, f"script inválido: {structural_errors[:3]}")
                return None

            bloques_for_trim = script.get("bloques", [])
            total_words = sum(len(b.get("texto", "").split()) for b in bloques_for_trim)

            if total_words > _MAX_WORDS:
                # Trim words from blocks: desarrollo → climax → cierre → hook (last resort)
                trim_order = ["desarrollo3", "desarrollo2", "desarrollo1", "climax", "cierre", "hook"]
                words_to_remove = total_words - _MAX_WORDS

                for block_type in trim_order:
                    if words_to_remove <= 0:
                        break
                    for b in bloques_for_trim:
                        if b.get("tipo") == block_type:
                            words = b.get("texto", "").split()
                            min_words = 5 if block_type not in ("hook", "cierre") else 7
                            if len(words) > min_words:
                                remove_from_block = min(words_to_remove, len(words) - min_words)
                                b["texto"] = " ".join(words[:len(words) - remove_from_block])
                                words_to_remove -= remove_from_block

                logger.warning(
                    "[%s] Script trimmed from %d to ~%d words (LLM exceeded limit of %d)",
                    channel_slug, total_words, _MAX_WORDS, _MAX_WORDS,
                )

                # Re-validate after trimming
                errors2 = validate_short_script(script)
                if errors2:
                    if not _is_last:
                        conflict_feedback = (
                            f"❌ El script anterior seguía inválido tras el recorte: {errors2[:2]}. "
                            f"Genera un script más corto (~70-85 palabras).\n"
                        )
                        continue
                    logger.error(
                        "Short script still invalid after trimming for %s: %s",
                        channel_slug, errors2,
                    )
                    _native_fail(slot_id, job_id, f"script inválido tras recorte: {errors2[:3]}")
                    return None
            elif total_words < _MIN_WORDS:
                logger.warning(
                    "[%s] Script has only %d words (< %d min) — proceeding anyway",
                    channel_slug, total_words, _MIN_WORDS,
                )

        # ── Hard spam filter: title similarity guard (dentro del bucle) ──
        # Near-duplicate titles across shorts AND long-form are a classic spam
        # signal. Si choca y quedan intentos → regenerar con feedback; si no,
        # rendirse (el slot reintentará en otro tick).
        _title_candidate = (script.get("titulo") or script.get("title") or "Short")[:100]
        _sim, _sim_what = _title_similar_to_recent(
            channel_id, _title_candidate, check_shorts=True, check_longform=True,
        )
        if _sim:
            if not _is_last:
                conflict_feedback = (
                    f"\n\n❌ RECHAZADO: tu título '{_title_candidate[:60]}' es demasiado "
                    f"parecido a {_sim_what} (ya publicado). Genera un script NUEVO "
                    f"con un tema y un título COMPLETAMENTE distintos (máx 50% "
                    f"palabras compartidas).\n"
                )
                logger.warning(
                    "[%s] Title similar (intento %d/%d): '%s' ~ %s — regenerando con feedback",
                    channel_slug, _attempt + 1, MAX_SCRIPT_ATTEMPTS,
                    _title_candidate[:60], _sim_what,
                )
                continue
            logger.warning(
                "[%s] Title too similar to recent %s: '%s' ~ '%s' — "
                "rejecting script, slot will retry with different content",
                channel_slug, _sim_what, _title_candidate[:60], _title_candidate[:60],
            )
            _native_fail(slot_id, job_id, f"título similar a {_sim_what} (tras {MAX_SCRIPT_ATTEMPTS} intentos)")
            return None
        break  # script válido y título sin conflicto

    _update_short_job_progress(job_id, 10, "script")

    title = (script.get("titulo") or script.get("title") or "Short")[:100]
    hook_text = (script.get("hook_text") or "")[:100]
    bloques = script.get("bloques", [])
    topic = (script.get("tema") or "")[:200]  # store topic for dedup

    # ── Hard content-safety filter (anti-strike) ─────────────────
    # Rechaza temas sensibles (menores, autolesión, claims médicos, violencia
    # gráfica, desinformación sanitaria) ANTES de gastar TTS/render/upload.
    # Igual que el guard de títulos: return None → el slot reintenta con otro
    # contenido. Evita repetir los strikes de canal5 (casos médicos de menores).
    try:
        from pipeline.content_safety import classify_topic_safety
        _bloque_textos = [b.get("texto", "") for b in bloques if isinstance(b, dict)]
        _safety = classify_topic_safety(
            topic=topic, title=title,
            script_texts=[hook_text] + _bloque_textos,
            config=ch_config,
        )
        if not _safety.safe:
            logger.warning(
                "[%s] Contenido rechazado por filtro de seguridad: '%s' — %s "
                "(slot reintentará con otro tema)",
                channel_slug, title[:60], _safety.reason,
            )
            _native_fail(slot_id, job_id, f"contenido no seguro: {_safety.reason}")
            return None
    except Exception as _cs_exc:
        logger.warning("[%s] Content-safety filter error (fail-open): %s", channel_slug, _cs_exc)

    # 1c. Subscribe CTA (~40% of native shorts) — programmatic append
    has_subscribe_cta = False
    cta_variants = getattr(ch_config, "SHORTS_SUBSCRIBE_CTA_VARIANTS", [])
    if cta_variants and random.random() < 0.4:
        cta_text = random.choice(cta_variants)
        # ── Word budget guard: skip CTA if script is already long ──
        current_words = sum(len(b.get("texto", "").split()) for b in bloques)
        cta_words = len(cta_text.split())
        if current_words + cta_words > 85:  # 85 words ≈ 42s safe under 58s max (aligned with MAX_WORD_COUNT=105)
            logger.info(
                "[%s] Skipping subscribe CTA — script already %d words (+%d CTA would overflow)",
                channel_slug, current_words, cta_words,
            )
        else:
            bloques.append({
                "tipo": "subscribe_cta",
                "texto": cta_text,
                "search_query_en": "subscribe button youtube channel notification bell",
            })
            has_subscribe_cta = True
            logger.info("[%s] Added subscribe CTA to native short: '%s'", channel_slug, cta_text)

    # ── Pre-TTS duration estimation ──────────────────────────
    # Avoid wasting TTS time on scripts that will definitely fail
    # the audio length check. Worst-case voice speed: 0.50 s/word.
    pre_tts_words = sum(len(b.get("texto", "").split()) for b in bloques)
    pre_tts_est = pre_tts_words * 0.50  # worst case for slow voices
    if pre_tts_est > 53.0:  # 5s margin under 58s SHORTS_MAX_DURATION_SEC
        # Re-trim aggressively to ~90 words (safe under 45s)
        target_words = 90
        words_to_remove = pre_tts_words - target_words
        logger.warning(
            "[%s] Pre-TTS: estimated %.1fs from %d words (> 53s) — "
            "re-trimming to ~%d words",
            channel_slug, pre_tts_est, pre_tts_words, target_words,
        )
        trim_order = ["desarrollo3", "desarrollo2", "desarrollo1", "climax", "cierre", "hook"]
        for block_type in trim_order:
            if words_to_remove <= 0:
                break
            for b in bloques:
                if b.get("tipo") == block_type:
                    words = b.get("texto", "").split()
                    if len(words) > 5:
                        remove = min(words_to_remove, len(words) - 5)
                        b["texto"] = " ".join(words[:len(words) - remove])
                        words_to_remove -= remove

        new_total = sum(len(b.get("texto", "").split()) for b in bloques)
        new_est = new_total * 0.50
        logger.info(
            "[%s] Re-trimmed: %d → %d words (est %.1fs → %.1fs)",
            channel_slug, pre_tts_words, new_total, pre_tts_est, new_est,
        )
        if new_est > 55.0:
            logger.error(
                "[%s] Still too long after re-trim (est %.1fs > 55s) — aborting",
                channel_slug, new_est,
            )
            _native_fail(slot_id, job_id, f"guion demasiado largo tras recorte (est {new_est:.1f}s)")
            return None  # will be caught by retry mechanism

    # 2. Segmented TTS
    output_dir = OUTPUT_DIR / "videos" / "shorts"
    output_dir.mkdir(parents=True, exist_ok=True)
    ts = int(time.time())
    audio_path = output_dir / f"sched_audio_{channel_slug}_{ts}.mp3"
    srt_path = output_dir / f"sched_audio_{channel_slug}_{ts}.srt"

    from pipeline.shorts_tts import synthesize_shorts_blocks
    try:
        tts_total = len(bloques)
        def _shorts_tts_progress(i_block: int, total: int):
            if total > 0:
                pct = 15 + int((i_block / total) * 8)
                _update_short_job_progress(job_id, pct, "tts")
        tts_result = synthesize_shorts_blocks(
            bloques=bloques,
            ch_config=ch_config,
            output_audio_path=audio_path,
            output_srt_path=srt_path,
            progress_cb=_shorts_tts_progress,
        )
        audio_duration = tts_result["duration_sec"] + 1.5
    except RuntimeError as e:
        logger.error("Short TTS failed for %s: %s", channel_slug, e)
        _native_fail(slot_id, job_id, f"TTS falló: {str(e)[:200]}")
        return None

    _update_short_job_progress(job_id, 25, "tts")

    # 2b. Compute scene_ranges from TTS timestamps (v9 — sub-scene splitting)
    #     Splits blocks longer than SCENE_DURATION_MAX (10s) into sub-scenes
    #     so each sub-scene gets its own distinct visual asset.  Mirrors the
    #     long-form pipeline logic in orchestrator.phase_media().
    from pipeline.video_editor import VideoEditor
    try:
        editor = VideoEditor(ch_config)
        scene_ranges = editor._compute_block_ranges(
            bloques, tts_result.get("timestamps", [])
        )
        logger.info(
            "[%s] Computed %d scene ranges from %d blocks (TTS=%.1fs)",
            channel_slug, len(scene_ranges), len(bloques),
            tts_result.get("duration_sec", 0),
        )
        # Add 'duracion_sec' for fetch_short_assets_exhaustive compatibility
        for sr in scene_ranges:
            sr["duracion_sec"] = sr.get("duration", 5.0)
    except Exception as e:
        logger.warning(
            "[%s] Scene range computation failed — falling back to raw blocks: %s",
            channel_slug, e,
        )
        scene_ranges = None

    # 3. Fetch assets exhaustively (v2) — one distinct asset per scene
    #    (uses scene_ranges when available so sub-scenes each get their own asset).
    #    50-60% video mix, cross-short dedup, query pool with variations
    from pipeline.shorts_media import (
        fetch_short_assets_exhaustive, render_short_hybrid,
        flush_short_asset_history,
    )
    theme_kw = script.get("theme_keywords_en", [])

    # Use scene_ranges as fetch list if available (one asset per sub-scene),
    # otherwise fall back to raw bloques.
    fetch_list = scene_ranges if scene_ranges else bloques

    asset_items = []
    try:
        media_total = len(fetch_list)
        def _shorts_media_progress(i_fetch: int, total: int):
            if total > 0:
                pct = 26 + int((i_fetch / total) * 22)
                _update_short_job_progress(job_id, pct, "media")
        asset_items = fetch_short_assets_exhaustive(
            fetch_list, ch_config, theme_kw,
            theme_ctx=theme_context,  # v8: pass full ThemeContext
            channel_id=channel_id, channel_slug=channel_slug,
            progress_cb=_shorts_media_progress,
        )
        logger.info("Fetched %d assets for Short (fetch_list=%d)",
                    len(asset_items), len(fetch_list))
    except Exception as e:
        logger.warning("Exhaustive asset fetch failed (will use solid bg): %s", e)

    _update_short_job_progress(job_id, 50, "media")

    # 3b. Align assets with scene_ranges for the renderer.
    #     Pass the FULL lists (including None entries) so the renderer can
    #     insert solid-bg filler segments where assets failed — this keeps
    #     the xfade timeline contiguous and offsets cumulative.
    if scene_ranges and len(scene_ranges) == len(asset_items):
        render_assets = asset_items
        render_ranges = scene_ranges
        valid_count = sum(1 for a in asset_items if a is not None)
        logger.info(
            "[%s] %d valid assets + %d filler (from %d scene_ranges)",
            channel_slug, valid_count, len(scene_ranges) - valid_count,
            len(scene_ranges),
        )
    else:
        render_assets = asset_items
        render_ranges = None

    # ── Anti-strike: rechazar renders degradados (solid bg = AI-spam) ──
    # Si no hay assets reales, o la fracción de escenas con visual real es muy
    # baja, NO subir: el resultado sería fondo liso + TTS + subtítulos, que es
    # exactamente la firma que YouTube elimina a los ~20 s. Se devuelve None
    # para que el slot reintente con otra ventana de RAM/assets.
    _valid_assets_total = sum(1 for a in asset_items if a is not None)
    _asset_positions = len(asset_items) if asset_items else 0
    _degraded = (
        _valid_assets_total == 0
        or (_asset_positions > 0 and (_valid_assets_total / _asset_positions) < SHORTS_MIN_VALID_ASSET_RATIO)
    )
    if _degraded:
        logger.warning(
            "[%s] Short render DEGRADADO (%d/%d assets reales) — rechazado para no "
            "subir solid-bg (riesgo de strike IA). Slot reintentará.",
            channel_slug, _valid_assets_total, _asset_positions,
        )
        _native_fail(
            slot_id, job_id,
            f"render degradado ({_valid_assets_total}/{_asset_positions} assets reales)",
        )
        return None

    # 4. Render hybrid (video + Ken Burns images + xfade)
    video_path = output_dir / f"sched_short_{channel_slug}_{ts}.mp4"

    color_palette = getattr(ch_config, "COLOR_PALETTE", {})
    def _to_hex(c):
        if isinstance(c, (tuple, list)) and len(c) == 3:
            return f"{int(c[0]):02x}{int(c[1]):02x}{int(c[2]):02x}"
        return str(c).lstrip("#").replace("#", "")
    bg_color = _to_hex(color_palette.get("text_shadow", (10, 10, 26)))

    try:
        def _shorts_render_progress(pct: int, phase: str, msg: str):
            _update_short_job_progress(job_id, pct, phase)
        render_short_hybrid(
            asset_items=render_assets,
            audio_path=audio_path,
            output_path=video_path,
            audio_duration=audio_duration,
            bg_color_hex=bg_color,
            srt_path=srt_path if srt_path.exists() else None,
            scene_ranges=render_ranges,
            progress_cb=_shorts_render_progress,
        )
    except Exception as e:
        # Anti-strike: NUNCA subir un render degradado a fondo sólido. Si el
        # render híbrido falla (ffmpeg timeout bajo presión de RAM, etc.), se
        # rechaza y el slot reintenta, en vez de subir solid-bg (AI-spam).
        logger.warning(
            "Hybrid render failed for %s — rejecting slot (no solid-bg upload): %s",
            channel_slug, e,
        )
        _native_fail(slot_id, job_id, f"render híbrido falló: {str(e)[:200]}")
        return None

    if not video_path.exists():
        logger.error("Render produced no output file for %s", channel_slug)
        _native_fail(slot_id, job_id, "render no produjo archivo de salida")
        return None

    _update_short_job_progress(job_id, 75, "render")

    # ── vX: generate_only — dejar el short en cola (sin subir) ──
    # Durante un bloqueo por spam se genera el native y queda status='generated';
    # la subida la hará _upload_queued_native_shorts al expirar bloqueo+colchón.
    if generate_only:
        try:
            conn = sqlite3.connect(str(DATABASE_PATH), timeout=30)
            cursor = conn.execute(
                """INSERT INTO shorts
                   (channel_id, type, title, hook_title, hook_text, topic,
                    status, file_path, has_subscribe_cta)
                   VALUES (?, 'native', ?, ?, ?, ?, 'generated', ?, ?)""",
                (channel_id, title, title[:60], hook_text, topic,
                 str(video_path), int(has_subscribe_cta)),
            )
            short_id = cursor.lastrowid
            conn.commit()
            conn.close()
        except Exception as e:
            logger.error("[%s] Failed to register generated native short: %s", channel_slug, e)
            _native_fail(slot_id, job_id, f"registro de short generado falló: {str(e)[:200]}")
            return None
        # Record assets in short_asset_history for cross-short dedup
        if asset_items:
            try:
                flush_short_asset_history(short_id, channel_id, asset_items)
            except Exception as e:
                logger.warning("[%s] Failed to flush short asset history: %s", channel_slug, e)
        _update_short_job_progress(job_id, 100, "generated")
        logger.info(
            "[%s] Native Short GENERADO y en cola (status=generated): %s",
            channel_slug, title[:40],
        )
        return short_id

    # 5. Upload
    from pipeline.youtube_uploader import YouTubeUploader
    uploader = YouTubeUploader(account_name=channel_slug, channel_slug=channel_slug)
    if not uploader.authenticate():
        logger.error("YouTube auth failed for %s", channel_slug)
        _native_fail(slot_id, job_id, "autenticación YouTube falló")
        return None

    # Cross-promotion
    from pipeline.shorts_cross_promote import (
        get_best_longform_link, build_short_description, run_post_publish_promotion,
        should_cross_promote,
    )
    longform_url = None
    if getattr(ch_config, "SHORTS_DESCRIPTION_LINK_ENABLED", True):
        longform_url = get_best_longform_link(channel_id)

    channel_url = getattr(ch_config, "YOUTUBE_CHANNEL_URL", "")
    description = build_short_description(
        hook_text=hook_text,
        hashtags=hashtags,
        longform_url=longform_url,
        channel_url=channel_url,
    )
    _update_short_job_progress(job_id, 90, "upload")
    if _youtube_quota_blocked(channel_slug=channel_slug):
        from pipeline.youtube_uploader import QuotaExhaustedError
        raise QuotaExhaustedError("YouTube quota exhausted before native short upload")
    # ── v10.3: Scheduled publishing ──
    if target_upload_at:
        privacy_mode = "private"
        publish_at = _safe_publish_at(target_upload_at, channel_slug, channel_id=channel_id)
        logger.info("Native short: scheduled publish at %s", str(publish_at)[:19])
    else:
        privacy_mode = "public"
        publish_at = None
    result = uploader.upload(
        video_path=video_path,
        title=title[:100],
        description=description[:5000],
        tags=hashtags[:60],
        category_id=getattr(ch_config, "YT_CATEGORY_ID", "24"),
        privacy=privacy_mode,
        publish_at=publish_at,
    )

    yt_id = result.get("video_id")
    if not yt_id:
        logger.error("Upload failed for %s: no video ID", channel_slug)
        _native_fail(slot_id, job_id, "subida falló: sin video ID")
        return None

    # 6. Register in DB
    conn = sqlite3.connect(str(DATABASE_PATH), timeout=30)
    cursor = conn.execute(
        """INSERT INTO shorts
           (channel_id, type, title, hook_title, hook_text, topic,
            status, file_path, youtube_id, youtube_url, published_at, has_subscribe_cta,
            longform_linked, longform_linked_at)
           VALUES (?, 'native', ?, ?, ?, ?, 'published', ?, ?, ?, datetime('now','localtime'), ?,
                   1, datetime('now','localtime'))""",
        (channel_id, title, title[:60], hook_text, topic,
         str(video_path), yt_id, result.get("url", ""),
         int(has_subscribe_cta)),
    )
    short_id = cursor.lastrowid
    conn.commit()
    conn.close()

    # 6b. Record assets in short_asset_history for cross-short dedup
    if asset_items:
        try:
            flush_short_asset_history(short_id, channel_id, asset_items)
        except Exception as e:
            logger.warning("[%s] Failed to flush short asset history: %s", channel_slug, e)

    # Auto-mark altered content (IA) via browser
    try:
        if getattr(ch_config, "AUTO_MARK_ALTERED_CONTENT", False):
            from pipeline.youtube_browser import get_account_for_channel
            account = get_account_for_channel(channel_slug)
            if account:
                import threading
                threading.Thread(
                    target=_auto_mark_ia_for_short,
                    args=(yt_id, channel_slug, account, short_id),
                    daemon=True
                ).start()
    except Exception as e:
        logger.warning("[%s] Failed to trigger auto-mark IA for short: %s", channel_slug, e)

    # Post-publish cross-promotion
    run_post_publish_promotion(
        channel_slug=channel_slug,
        short_yt_id=yt_id,
        channel_id=channel_id,
        source_yt_id=longform_url.split("v=")[-1] if longform_url else None,
        channel_config=ch_config,
    )

    logger.info("Scheduled native Short published: %s → %s", title[:40], result.get("url", ""))
    return short_id


# ── Cola de shorts nativos generados (bloqueo por spam) ──────────

def _upload_queued_native_short(short_record: dict, db=None) -> bool:
    """Sube un short nativo en cola (status='generated') y lo marca publicado.

    Solo debe llamarse cuando el canal NO está bloqueado por spam (lo verifica
    el dispatcher _upload_queued_native_shorts). Devuelve True si se subió.
    """
    from pathlib import Path
    from config.config_bridge import get_channel_config
    from pipeline.youtube_uploader import YouTubeUploader

    if db is None:
        from database.db_extended import ExtendedDatabase
        db = ExtendedDatabase()

    short_id = int(short_record["id"])
    channel_id = int(short_record["channel_id"])
    ch = db.get_channel(channel_id)
    if not ch:
        logger.error("Queued short #%d: canal %d no encontrado", short_id, channel_id)
        return False
    slug = ch["slug"]

    file_path = short_record.get("file_path", "")
    if not file_path or not Path(file_path).exists():
        logger.warning("Queued short #%d: archivo no existe (%s) — marcando error", short_id, file_path)
        try:
            with db._connect() as conn:
                conn.execute(
                    "UPDATE shorts SET status='error', error_message='archivo no encontrado' WHERE id=?",
                    (short_id,),
                )
                conn.commit()
        except Exception:
            pass
        return False

    ch_config = get_channel_config(slug)
    hashtags = getattr(ch_config, "SHORTS_HASHTAGS", ["#Shorts"])

    # Rebuild description (long-form link opcional)
    longform_url = None
    try:
        from pipeline.shorts_cross_promote import (
            build_short_description, get_best_longform_link, should_cross_promote,
        )
        if getattr(ch_config, "SHORTS_DESCRIPTION_LINK_ENABLED", True):
            longform_url = get_best_longform_link(channel_id)
        channel_url = getattr(ch_config, "YOUTUBE_CHANNEL_URL", "")
        description = build_short_description(
            hook_text=(short_record.get("hook_text") or "")[:100],
            hashtags=hashtags,
            longform_url=longform_url,
            channel_url=channel_url,
        )
    except Exception as exc:
        logger.warning("Queued short #%d: descripción falló (usa hashtags): %s", short_id, exc)
        description = "\n\n".join(hashtags)

    # Quota guard (proyecto del canal)
    if _youtube_quota_blocked(channel_slug=slug):
        logger.info("[%s] Queued short upload skipped: cuota bloqueada", slug)
        return False

    # HARD per-channel daily cap (anti-strike): no subir si ya se subió hoy.
    if _channel_hard_daily_short_cap_reached(channel_id, db):
        logger.info(
            "[%s] Queued short #%d skipped: tope duro diario (%d/día) — se mantiene en cola",
            slug, short_id, _hard_daily_cap(),
        )
        return False

    uploader = YouTubeUploader(account_name=slug, channel_slug=slug)
    if not uploader.authenticate():
        logger.error("[%s] YouTube auth failed (queued short #%d)", slug, short_id)
        return False

    title = (short_record.get("title") or "Short")[:100]
    # ── Fase 2: la cola respeta la hora pico del slot planificado ──
    # Si el slot del short tiene target_upload_at futuro → subir como private
    # con publishAt (publicación programada en la franja óptima). Si no hay
    # slot o el target ya pasó → publicación inmediata.
    privacy = "public"
    publish_at = None
    try:
        with db._connect() as conn:
            slot_row = conn.execute(
                """SELECT target_upload_at FROM shorts_planned_slots
                   WHERE short_id = ? AND status = 'completed'
                   ORDER BY id DESC LIMIT 1""",
                (short_id,),
            ).fetchone()
        if slot_row and slot_row["target_upload_at"]:
            publish_at = _safe_publish_at(
                slot_row["target_upload_at"], slug, channel_id=channel_id,
            )
            if publish_at:
                privacy = "private"
                logger.info(
                    "[%s] Queued short #%d: publicación programada en hora pico (%s)",
                    slug, short_id, str(publish_at)[:19],
                )
    except Exception as exc:
        logger.debug("[%s] Queued short #%d: slot publish lookup failed: %s", slug, short_id, exc)
    try:
        result = uploader.upload(
            video_path=file_path,
            title=title,
            description=description[:5000],
            tags=hashtags[:60],
            category_id=getattr(ch_config, "YT_CATEGORY_ID", "24"),
            privacy=privacy,
            publish_at=publish_at,
        )
    except Exception as exc:
        logger.warning("[%s] Queued short #%d upload failed: %s", slug, short_id, exc)
        return False

    yt_id = result.get("video_id")
    if not yt_id:
        logger.error("Queued short #%d upload failed: no video ID", short_id)
        return False

    try:
        with db._connect() as conn:
            conn.execute(
                """UPDATE shorts SET status='published', youtube_id=?, youtube_url=?,
                   published_at=datetime('now','localtime'), error_message=''
                   WHERE id=?""",
                (yt_id, result.get("url", ""), short_id),
            )
            conn.commit()
    except Exception as exc:
        logger.warning("Queued short #%d: DB update failed: %s", short_id, exc)

    # Auto-mark altered content (IA) vía browser (best-effort, en segundo plano)
    try:
        if getattr(ch_config, "AUTO_MARK_ALTERED_CONTENT", False):
            from pipeline.youtube_browser import get_account_for_channel
            account = get_account_for_channel(slug)
            if account:
                import threading
                threading.Thread(
                    target=_auto_mark_ia_for_short,
                    args=(yt_id, slug, account, short_id),
                    daemon=True,
                ).start()
    except Exception:
        pass

    logger.info("[%s] Queued native short publicado: %s → %s", slug, title[:40], result.get("url", ""))
    return True


def _upload_queued_native_shorts(db=None, max_per_pass: int = 3) -> int:
    """Sube shorts nativos en cola (status='generated') de forma gradual.

    Gate robusto: SOLO canales NO bloqueados por spam (bloqueo + colchón).
    Respeta el cooldown entre shorts del mismo canal, el tope diario de
    nativos publicados y la cuota del proyecto. Devuelve nº de shorts subidos.
    """
    if db is None:
        from database.db_extended import ExtendedDatabase
        db = ExtendedDatabase()

    if _shorts_paused(db):
        return 0

    try:
        channels = db.get_channels(active_only=True) or []
    except Exception:
        return 0

    uploaded = 0
    for ch in channels:
        if uploaded >= max_per_pass:
            break
        cid = int(ch.get("id", 0) or 0)
        slug = ch.get("slug", "")
        if not cid:
            continue
        # ── Gate robusto: no subir NADA durante el ban (bloqueo + colchón) ──
        if _channel_shorts_spam_blocked(cid, db):
            continue
        # Cooldown entre shorts del mismo canal
        if not _channel_shorts_cooldown_ok(cid, db):
            continue
        # Tope diario de nativos publicados
        try:
            published_today = db.count_native_shorts_published_today(cid)
            native_daily_max = db.get_native_shorts_per_day(cid)
            if published_today >= native_daily_max:
                continue
        except Exception:
            pass
        # Siguiente short en cola (FIFO)
        queued = db.get_queued_native_shorts(cid, limit=1)
        if not queued:
            continue
        try:
            if _upload_queued_native_short(queued[0], db=db):
                uploaded += 1
        except Exception as exc:
            logger.warning("[%s] Queued short upload error: %s", slug, exc)

    if uploaded:
        logger.info("Cola de shorts nativos: %d subido(s) esta pasada", uploaded)
    return uploaded


# ── Clip short generation ──────────────────────────────────────

def _dispatch_clip_short(channel_id: int, channel_slug: str,
                          source_video_id: int, slot_rank: int = 0,
                          job_id: int = None,
                          pre_rendered_short_id: int = None,
                          target_upload_at: str = None) -> int | None:
    """Extract a clip from a long video, render, and publish.

    Uses the ShortsExtractor pipeline pattern from api/routers/shorts.py
    (extract-and-publish endpoint).

    v25: If pre_rendered_short_id is provided, skips the LLM extraction and
    rendering phases (the clip was pre-rendered by pre_render_clip_shorts_for_video()
    right after the long-form upload). Only uploads the existing file.

    v10.3: If target_upload_at is provided, uploads as private + publishAt
    (scheduled publishing) instead of immediate public.

    Returns short_id or None on failure.
    """
    import sqlite3
    import json as _json
    import subprocess
    import tempfile
    import time
    from pathlib import Path
    from config.settings import DATABASE_PATH, OUTPUT_DIR
    from config.config_bridge import get_channel_config

    # ── v25: Fast path for pre-rendered clips ──
    if pre_rendered_short_id:
        conn_pre = sqlite3.connect(str(DATABASE_PATH), timeout=30)
        conn_pre.row_factory = sqlite3.Row
        short_row = conn_pre.execute(
            "SELECT * FROM shorts WHERE id = ? AND type = 'clip' AND status = 'ready'",
            (pre_rendered_short_id,),
        ).fetchone()
        conn_pre.close()

        if short_row and short_row["file_path"] and Path(short_row["file_path"]).exists():
            short_data = dict(short_row)
            output_path = Path(short_data["file_path"])
            logger.info(
                "Clip short #%d: pre-rendered file ready — uploading directly (%s)",
                pre_rendered_short_id, output_path.name,
            )

            # Upload phase (same as original _dispatch_clip_short upload section)
            _update_short_job_progress(job_id, 75, "upload")
            ch_config = get_channel_config(channel_slug)
            hashtags = getattr(ch_config, "SHORTS_HASHTAGS", ["#Shorts"])

            from pipeline.youtube_uploader import YouTubeUploader
            uploader = YouTubeUploader(account_name=channel_slug, channel_slug=channel_slug)
            if not uploader.authenticate():
                logger.error("YouTube auth failed for %s (pre-rendered clip)", channel_slug)
                return None

            title = (short_data.get("hook_title") or short_data.get("title") or "Short")[:100]
            hook_text = short_data.get("hook_text", "")

            from pipeline.shorts_cross_promote import (
                get_best_longform_link, build_short_description, run_post_publish_promotion,
                should_cross_promote,
            )
            longform_url = None
            if getattr(ch_config, "SHORTS_DESCRIPTION_LINK_ENABLED", True):
                longform_url = get_best_longform_link(channel_id, source_video_id=short_data.get("source_video_id"))

            channel_url_value = getattr(ch_config, "YOUTUBE_CHANNEL_URL", "")
            description = build_short_description(
                hook_text=hook_text, hashtags=hashtags,
                longform_url=longform_url, channel_url=channel_url_value,
            )
            _update_short_job_progress(job_id, 90, "upload")
            # ── v10.3: Scheduled publishing ──
            if target_upload_at:
                clip_privacy = "private"
                clip_publish_at = _safe_publish_at(target_upload_at, channel_slug, channel_id=channel_id)
            else:
                clip_privacy = "public"
                clip_publish_at = None
            if _youtube_quota_blocked(channel_slug=channel_slug):
                from pipeline.youtube_uploader import QuotaExhaustedError
                raise QuotaExhaustedError("YouTube quota exhausted before pre-rendered clip upload")
            result = uploader.upload(
                video_path=output_path,
                title=title,
                description=description[:5000],
                tags=hashtags[:60],
                category_id=getattr(ch_config, "YT_CATEGORY_ID", "24"),
                privacy=clip_privacy,
                publish_at=clip_publish_at,
            )

            yt_id = result.get("video_id")
            if not yt_id:
                logger.error("Upload failed for pre-rendered clip short #%d", pre_rendered_short_id)
                return None

            # Update shorts table: mark as published
            conn_upd = sqlite3.connect(str(DATABASE_PATH), timeout=30)
            conn_upd.execute(
                """UPDATE shorts SET status = 'published', youtube_id = ?,
                   youtube_url = ?, published_at = datetime('now','localtime'),
                   updated_at = datetime('now','localtime'),
                   longform_linked = 1, longform_linked_at = datetime('now','localtime')
                   WHERE id = ?""",
                (yt_id, result.get("url", ""), pre_rendered_short_id),
            )
            conn_upd.commit()
            conn_upd.close()

            # Auto-mark altered content (IA) via browser
            try:
                if getattr(ch_config, "AUTO_MARK_ALTERED_CONTENT", False):
                    from pipeline.youtube_browser import get_account_for_channel
                    account = get_account_for_channel(channel_slug)
                    if account:
                        import threading
                        threading.Thread(
                            target=_auto_mark_ia_for_short,
                            args=(yt_id, channel_slug, account, pre_rendered_short_id),
                            daemon=True
                        ).start()
            except Exception as e_mark:
                logger.warning("[%s] Failed to trigger auto-mark IA: %s", channel_slug, e_mark)

            # Auto-link long-form video
            try:
                from pipeline.youtube_browser import get_account_for_channel
                account = get_account_for_channel(channel_slug)
                if account and short_data.get("source_video_id"):
                    import threading
                    threading.Thread(
                        target=_auto_link_longform_for_short,
                        args=(yt_id, channel_slug, account, pre_rendered_short_id,
                              short_data["source_video_id"]),
                        daemon=True
                    ).start()
            except Exception as e_link:
                logger.warning("[%s] Failed to trigger longform link: %s", channel_slug, e_link)

            run_post_publish_promotion(
                channel_slug=channel_slug, short_yt_id=yt_id,
                channel_id=channel_id,
                source_yt_id=longform_url.split("v=")[-1] if longform_url else None,
            )

            # v25: Delete pre-rendered clip MP4 after upload (no longer needed)
            try:
                output_path.unlink(missing_ok=True)
                logger.info("Deleted pre-rendered clip MP4 after upload: %s", output_path)
            except Exception:
                pass

            logger.info("Pre-rendered clip Short published: %s → %s",
                        title[:40], result.get("url", ""))
            return pre_rendered_short_id

        else:
            logger.warning(
                "Pre-rendered short #%d not found or file missing — "
                "falling back to full clip generation",
                pre_rendered_short_id,
            )
            # Fall through to normal flow

    # ── Normal flow (no pre-render, or pre-render failed) ──

    logger.info("Clip extraction: channel=%s source_video=%d", channel_slug, source_video_id)

    conn = sqlite3.connect(str(DATABASE_PATH), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")

    video = conn.execute(
        """SELECT v.*, c.slug as channel_slug
           FROM videos v
           JOIN channels c ON v.channel_id = c.id
           WHERE v.id = ?""",
        (source_video_id,),
    ).fetchone()

    if not video:
        conn.close()
        logger.error("Source video #%d not found for clip short", source_video_id)
        return None

    video = dict(video)

    # ── Phase 1: Script + blocks ──
    script_text = ""
    bloques = []
    if video.get("script_id"):
        script_row = conn.execute(
            "SELECT guion, bloques_json FROM scripts WHERE id = ?",
            (video["script_id"],),
        ).fetchone()
        if script_row:
            script_text = script_row["guion"] or ""
            try:
                bloques = _json.loads(script_row["bloques_json"] or "[]") if script_row["bloques_json"] else []
            except Exception:
                bloques = []

    if not script_text and bloques:
        script_text = " ".join(b.get("texto", "") for b in bloques if b.get("texto"))

    if not script_text:
        bloques_raw = video.get("title_options") or "{}"
        try:
            fallback = _json.loads(bloques_raw) if isinstance(bloques_raw, str) else {}
        except Exception:
            fallback = {}
        # title_options is a JSON array (list of title strings), not a dict
        if not isinstance(fallback, dict):
            fallback = {}
        script_text = str(fallback.get("script", "")) or script_text

    if not script_text:
        conn.close()
        _VIDEOS_WITHOUT_SCRIPT.add(source_video_id)
        logger.warning("Source video #%d has no script text — marked as unusable for clips today",
                       source_video_id)
        return None

    _update_short_job_progress(job_id, 10, "script")

    # ── Phase 2: LLM extracts best clip timecodes ──
    # Build approximate word-level timestamps from blocks
    from pipeline.shorts_extractor import ShortsExtractor
    total_duration = video.get("duracion_seg") or 0
    n_blocks = len(bloques) if bloques else 1
    timestamps = []
    for idx, block in enumerate(bloques if bloques else [{"texto": script_text}]):
        texto = block.get("texto", "")
        if not texto:
            continue
        words = texto.split()
        block_start = (idx / n_blocks) * total_duration
        block_end = ((idx + 1) / n_blocks) * total_duration
        word_dur = (block_end - block_start) / max(len(words), 1)
        for wi, word in enumerate(words):
            ts_start = round(block_start + wi * word_dur, 1)
            ts_end = round(ts_start + word_dur, 1)
            timestamps.append({"word": word, "start": ts_start, "end": ts_end})

    # Extract word-level TTS timestamps for subtitle rendering
    tts_word_ts = []
    try:
        td_raw = video.get("timing_data") or "{}"
        td = _json.loads(td_raw) if isinstance(td_raw, str) else td_raw
        tts_word_ts = td.get("phases", {}).get("tts_timestamps", [])
        if not isinstance(tts_word_ts, list):
            tts_word_ts = []
    except Exception:
        pass

    extractor = ShortsExtractor()

    # ── v21: Query existing clip shorts for this source_video to exclude ──
    exclude_ranges = []
    try:
        existing_clips = conn.execute(
            """SELECT start_time, end_time FROM shorts
               WHERE source_video_id = ?
                 AND type = 'clip'
                 AND status IN ('published', 'uploading', 'ready', 'rendering', 'extracted')
               ORDER BY start_time""",
            (source_video_id,),
        ).fetchall()
        exclude_ranges = [(float(r["start_time"]), float(r["end_time"])) for r in existing_clips]
        if exclude_ranges:
            logger.info(
                "Dedup: excluding %d already-published clip ranges for source_video #%d: %s",
                len(exclude_ranges), source_video_id,
                ", ".join(f"{s:.0f}-{e:.0f}s" for s, e in exclude_ranges),
            )
    except Exception as e:
        logger.warning("Dedup query failed (non-fatal): %s", e)

    clips = extractor.extract(script_text=script_text, timestamps=timestamps,
                              max_clips=3, min_clips=1,
                              exclude_ranges=exclude_ranges)

    conn.close()

    if not clips:
        logger.error("No suitable clip found in video #%d", source_video_id)
        return None

    best_clip = clips[0]
    _update_short_job_progress(job_id, 20, "script")

    # ── Phase 3: Find or download source video ──
    source_path, clip_offset = _resolve_source_video(video, best_clip["start_time"],
                                                      best_clip["end_time"])
    if source_path is None:
        logger.error("Cannot access source video file for #%d", source_video_id)
        return None

    # ── v21: Save original clip times BEFORE clip_offset adjustment for DB ──
    # The DB stores the original time range from the long-form video (used for dedup).
    # best_clip may be modified in-place by clip_offset adjustment for rendering.
    db_start_time = best_clip["start_time"]
    db_end_time = best_clip["end_time"]

    if clip_offset > 0:
        clip_duration = best_clip["end_time"] - best_clip["start_time"]
        best_clip["start_time"] = clip_offset
        best_clip["end_time"] = clip_offset + clip_duration

    _update_short_job_progress(job_id, 30, "media")

    # ── Phase 4: Render → Upload → Promote ──
    _downloaded_temp = source_path
    from pipeline.shorts_renderer import ShortsRenderer
    renderer = ShortsRenderer()
    output_path = None

    try:
        # ── v21: Subtitle fallback ──────────────────────────────────────
        # Build proper word-level timestamps for SRT subtitle rendering.
        # Strategy:
        #   1. Use TTS word timestamps from timing_data if available (best quality)
        #   2. When clip_offset > 0 (YouTube download), adjust coordinates
        #   3. Fallback: use block-based approximate timestamps (interpolated)
        render_word_ts = None

        if tts_word_ts:
            # TTS timestamps available — normalize to start_ms/end_ms format
            normalized = []
            for ts in tts_word_ts:
                start_val = float(ts.get("start_ms", ts.get("start", 0)))
                end_val = float(ts.get("end_ms", ts.get("end", 0)))
                normalized.append({
                    "word": ts.get("word", ""),
                    "start_ms": start_val,
                    "end_ms": end_val,
                })

            if clip_offset == 0:
                render_word_ts = normalized
                logger.debug("Subtitle: using TTS timestamps (local file, offset=0)")
            else:
                # Adjust timestamps for downloaded file with padding
                shift_s = clip_offset - db_start_time
                shift_ms = shift_s * 1000
                adjusted = []
                for ts in normalized:
                    new_start = ts["start_ms"] + shift_ms
                    new_end = ts["end_ms"] + shift_ms
                    adjusted.append({
                        "word": ts["word"],
                        "start_ms": new_start,
                        "end_ms": new_end,
                    })
                render_word_ts = adjusted
                logger.info(
                    "Subtitle: adjusted %d TTS timestamps by +%.0fms (offset=%.1f, original=%.1f)",
                    len(adjusted), shift_ms, clip_offset, db_start_time,
                )
        else:
            # ── v21 Fallback: use block-based approximate timestamps ──
            # Convert from {"start": seconds} to {"start_ms": millis} format
            if timestamps:
                block_ts = []
                for ts in timestamps:
                    block_ts.append({
                        "word": ts.get("word", ""),
                        "start_ms": ts["start"] * 1000,
                        "end_ms": ts["end"] * 1000,
                    })
                # If using downloaded file with offset, adjust coordinates
                if clip_offset > 0:
                    shift_s = clip_offset - db_start_time
                    shift_ms = shift_s * 1000
                    adjusted = []
                    for ts in block_ts:
                        new_start = ts["start_ms"] + shift_ms
                        new_end = ts["end_ms"] + shift_ms
                        adjusted.append({
                            "word": ts["word"],
                            "start_ms": new_start,
                            "end_ms": new_end,
                        })
                    block_ts = adjusted
                render_word_ts = block_ts
                logger.info(
                    "Subtitle: using block-based approximate timestamps (%d words, adjusted=%s)",
                    len(block_ts), clip_offset > 0,
                )
            else:
                logger.warning("Subtitle: no timestamps available — clip short will have NO subtitles")

        _update_short_job_progress(job_id, 45, "render")

        output_path = renderer.render(
            source_path, best_clip, word_timestamps=render_word_ts,
        )
        if not output_path or not output_path.exists():
            logger.error("Render produced no output for clip from video #%d", source_video_id)
            return None

        _update_short_job_progress(job_id, 75, "render")

        ch_config = get_channel_config(channel_slug)
        hashtags = getattr(ch_config, "SHORTS_HASHTAGS", ["#Shorts"])

        from pipeline.youtube_uploader import YouTubeUploader
        uploader = YouTubeUploader(account_name=channel_slug, channel_slug=channel_slug)
        if not uploader.authenticate():
            logger.error("YouTube auth failed for %s", channel_slug)
            return None

        title = best_clip.get("hook_title", "Short")[:100]
        hook_text = best_clip.get("hook_text", "")

        from pipeline.shorts_cross_promote import (
            get_best_longform_link, build_short_description, run_post_publish_promotion,
            should_cross_promote,
        )
        longform_url = None
        if should_cross_promote(ch_config):
            longform_url = get_best_longform_link(channel_id, source_video_id=source_video_id)

        channel_url_value = getattr(ch_config, "YOUTUBE_CHANNEL_URL", "")
        description = build_short_description(
            hook_text=hook_text,
            hashtags=hashtags,
            longform_url=longform_url,
            channel_url=channel_url_value,
        )
        _update_short_job_progress(job_id, 90, "upload")
        # ── v10.3: Scheduled publishing ──
        if target_upload_at:
            clip_privacy2 = "private"
            clip_publish_at2 = _safe_publish_at(target_upload_at, channel_slug, channel_id=channel_id)
        else:
            clip_privacy2 = "public"
            clip_publish_at2 = None
        if _youtube_quota_blocked(channel_slug=channel_slug):
            from pipeline.youtube_uploader import QuotaExhaustedError
            raise QuotaExhaustedError("YouTube quota exhausted before clip upload")
        result = uploader.upload(
            video_path=output_path,
            title=title,
            description=description[:5000],
            tags=hashtags[:60],
            category_id=getattr(ch_config, "YT_CATEGORY_ID", "24"),
            privacy=clip_privacy2,
            publish_at=clip_publish_at2,
        )

        yt_id = result.get("video_id")
        if not yt_id:
            logger.error("Upload failed for clip: no video ID returned")
            return None

        # Register in DB
        conn2 = sqlite3.connect(str(DATABASE_PATH), timeout=30)
        cursor = conn2.execute(
            """INSERT INTO shorts
               (channel_id, source_video_id, type, title, hook_title, hook_text,
                start_time, end_time, status, file_path, youtube_id, youtube_url, published_at,
                longform_linked, longform_linked_at)
               VALUES (?, ?, 'clip', ?, ?, ?, ?, ?, 'published', ?, ?, ?, datetime('now','localtime'),
                       1, datetime('now','localtime'))""",
            (channel_id, source_video_id, title, title[:60], hook_text,
             db_start_time, db_end_time,
             str(output_path), yt_id, result.get("url", "")),
        )
        short_id = cursor.lastrowid
        conn2.commit()
        conn2.close()

        # Auto-mark altered content (IA) via browser
        try:
            if getattr(ch_config, "AUTO_MARK_ALTERED_CONTENT", False):
                from pipeline.youtube_browser import get_account_for_channel
                account = get_account_for_channel(channel_slug)
                if account:
                    import threading
                    threading.Thread(
                        target=_auto_mark_ia_for_short,
                        args=(yt_id, channel_slug, account, short_id),
                        daemon=True
                    ).start()
        except Exception as e:
            logger.warning("[%s] Failed to trigger auto-mark IA for short: %s", channel_slug, e)

        # Auto-link long-form video as "Related video" (only for clip shorts)
        try:
            from pipeline.youtube_browser import get_account_for_channel
            account = get_account_for_channel(channel_slug)
            if account and source_video_id:
                import threading
                threading.Thread(
                    target=_auto_link_longform_for_short,
                    args=(yt_id, channel_slug, account, short_id, source_video_id),
                    daemon=True
                ).start()
                logger.info("[%s] Triggered longform link for short %s → source_video #%d",
                            channel_slug, yt_id, source_video_id)
        except Exception as e:
            logger.warning("[%s] Failed to trigger longform link for short: %s", channel_slug, e)

        run_post_publish_promotion(
            channel_slug=channel_slug,
            short_yt_id=yt_id,
            channel_id=channel_id,
            source_yt_id=longform_url.split("v=")[-1] if longform_url else None,
        )

        logger.info("Scheduled clip Short published: %s → %s", title[:40], result.get("url", ""))
        return short_id

    except Exception as e:
        logger.error("[%s] Clip short generation failed in phase 4: %s", channel_slug, e)
        return None
    finally:
        if _downloaded_temp and str(_downloaded_temp).startswith("/tmp/"):
            try:
                _downloaded_temp.unlink(missing_ok=True)
            except Exception:
                pass


# ═══════════════════════════════════════════════════════════════════
# v2: Emotional arc scoring for clip short selection
# ═══════════════════════════════════════════════════════════════════

# ── v2: Quick LLM hook text generation (3-5 words) ─────────────

def _generate_hook_overlay_text(scene_text: str, max_words: int = 6) -> str:
    """Generate a short hook overlay text (3-6 words) via LLM for clip shorts.

    Falls back to extracting the first short sentence if LLM fails.
    """
    if not scene_text or len(scene_text.strip()) < 20:
        return ""

    try:
        from config.llm_client import create_llm_client
        from config.llm_helpers import llm_json_call
        from config.settings import LLM_MODEL

        client = create_llm_client(enable_thinking=False)
        result = llm_json_call(
            client,
            max_retries=2,
            retry_delay=1.0,
            model=LLM_MODEL,
            messages=[{
                "role": "user",
                "content": (
                    f"Extrae la frase más impactante y corta (máximo {max_words} palabras, en español) "
                    f"de este texto para usarla como hook visual de un YouTube Short. "
                    f"Responde SOLO JSON: {{\"hook\": \"...\"}}. "
                    f"La frase debe generar curiosidad o asombro inmediato.\n\n"
                    f"Texto: {scene_text[:800]}"
                ),
            }],
            temperature=0.8,
            max_tokens=80,
        )
        hook = (result.get("hook") or "").strip()
        if hook and len(hook.split()) <= max_words + 2:
            return hook
    except Exception as e:
        logger.debug("Hook LLM generation failed (non-fatal): %s", e)

    # Fallback: first short sentence
    import re
    sentences = re.split(r'[.!?]', scene_text[:500])
    for s in sentences:
        words = s.strip().split()
        if 3 <= len(words) <= max_words + 2:
            return s.strip()
    return ""

# Key emotion words (Spanish) mapped to scoring weights.
# These appear in SCRIPT_EMOTIONAL_ARC values across all 4 channels.
_EMOTION_WEIGHTS: dict[str, int] = {
    "asombro": 20, "shock": 22, "impacto": 22,
    "revelación": 20, "revelacion": 20,
    "clímax": 25, "climax": 25,
    "misterio": 15, "intriga": 16, "tensión": 16, "tension": 16,
    "curiosidad": 12, "anticipación": 12, "anticipacion": 12,
    "fascinación": 14, "fascinacion": 14,
    "estupefacción": 18, "estupefaccion": 18,
    "horror": 22, "angustia": 18, "desesperación": 18, "desesperacion": 18,
    "inspiración": 10, "inspiracion": 10, "maravilla": 12,
    "admiración": 10, "admiracion": 10,
    "reflexión": 8, "reflexion": 8, "solemnidad": 8,
    "gratitud": 6, "respeto": 6,
    "empatía": 10, "empatia": 10,
    "esperanza": 8, "comprensión": 8, "comprension": 8,
    "alivio": 8, "duelo": 14,
    "instinto": 10,
}


def _extract_emotion_keywords(emotional_arc: dict) -> list[str]:
    """Extract all emotion keywords from a SCRIPT_EMOTIONAL_ARC config.

    Handles both simple values ("asombro") and compound values
    ("esperanza → inspiración", "alivio amargo / duelo").
    """
    keywords = []
    for val in emotional_arc.values():
        if not isinstance(val, str):
            continue
        # Split on arrows, slashes, or commas
        parts = [p.strip().lower() for p in val.replace("→", ",").replace("/", ",").split(",")]
        for p in parts:
            if p:
                keywords.append(p)
    return keywords


def _has_emotion_keywords(text: str, keywords: list[str]) -> tuple[int, list[str]]:
    """Check how many emotion keywords appear in the text.

    Returns (total_score, matched_keywords).
    """
    total = 0
    matched = []
    text_lower = text.lower()
    for kw in keywords:
        if kw in text_lower or kw in _EMOTION_WEIGHTS:
            weight = _EMOTION_WEIGHTS.get(kw, 5)
            if kw in text_lower:
                total += weight
                matched.append(kw)
    return total, matched


def score_blocks_by_emotional_arc(
    bloques: list[dict],
    emotional_arc: dict,
    total_duration: float,
) -> list[tuple[int, dict, float]]:
    """Score each script block by alignment with the channel's emotional arc.

    Scoring criteria:
      - Position in video (0-100%) matched against EMOTIONAL_ARC percent buckets
      - Emotion keywords in block text matched against arc emotion words
      - Duration estimation (30-60s ideal)

    Returns list of (score, block, position_pct) sorted by score descending.
    """
    if not bloques or not emotional_arc:
        return []

    emotion_keywords = _extract_emotion_keywords(emotional_arc)
    n = len(bloques)

    # Parse arc buckets once
    arc_buckets: list[tuple[float, float, list[str]]] = []
    for pct_key, emo_val in emotional_arc.items():
        if not isinstance(emo_val, str):
            continue
        try:
            clean = pct_key.replace("%", "").strip()
            parts = clean.split("-")
            if len(parts) == 2:
                lo = float(parts[0])
                hi = float(parts[1])
                emos = [e.strip().lower() for e in
                        emo_val.replace("→", ",").replace("/", ",").split(",") if e.strip()]
                arc_buckets.append((lo, hi, emos))
        except (ValueError, IndexError):
            continue

    arc_buckets.sort(key=lambda x: x[0])

    # Position-based base scores (clímax zone gets highest)
    def _position_base(pct: float) -> int:
        if 60 <= pct <= 90:
            return 30  # climax zone
        elif 30 <= pct < 60:
            return 15  # development zone
        elif 10 <= pct < 30:
            return 10  # early development
        else:
            return 5   # hook (0-10%) or close (90-100%)

    scored = []
    for idx, block in enumerate(bloques):
        if not block.get("texto"):
            continue
        text = block["texto"]
        score = 0

        # Position
        pos_pct = (idx / max(n - 1, 1)) * 100 if n > 1 else 50
        score += _position_base(pos_pct)

        # Arc bucket bonus: if the position falls in a bucket, score its emotions
        for lo, hi, emos in arc_buckets:
            if lo <= pos_pct <= hi:
                for e in emos:
                    score += _EMOTION_WEIGHTS.get(e, 5)
                break

        # Emotion keywords in text
        kw_score, matched = _has_emotion_keywords(text, emotion_keywords)
        score += kw_score

        # Duration bonus (approximate: 12 words ≈ 5 seconds)
        word_count = len(text.split())
        approx_dur = word_count * 0.42  # ~2.4 words/sec → 0.42s per word
        if 30 <= approx_dur <= 60:
            score += 10
        elif 20 <= approx_dur <= 80:
            score += 5

        scored.append((score, block, pos_pct))

    scored.sort(reverse=True, key=lambda x: x[0])
    return scored


def select_best_clips_from_blocks(
    clips: list[dict],
    bloques: list[dict],
    emotional_arc: dict,
    total_duration: float,
    max_clips: int = 5,
    min_score: int = 15,
) -> list[dict]:
    """Reorder and filter LLM-returned clips by emotional arc alignment.

    Uses score_blocks_by_emotional_arc to re-rank clips so the most
    emotionally impactful ones are rendered first.
    """
    if not emotional_arc or not bloques:
        return clips[:max_clips]

    # Score each clip by finding the nearest block(s) it overlaps with
    scored_blocks = score_blocks_by_emotional_arc(bloques, emotional_arc, total_duration)
    if not scored_blocks:
        return clips[:max_clips]

    # Map block index → score
    block_score_map = {}
    for idx, (score, block, pct) in enumerate(scored_blocks):
        for bi, b in enumerate(bloques):
            if b.get("texto") == block.get("texto"):
                block_score_map[bi] = score
                break

    # Score each clip based on which blocks it overlaps
    n_blocks = len(bloques)
    clip_scores = []
    for c in clips:
        clip_start = c.get("start_time", 0)
        clip_end = c.get("end_time", clip_start + 60)
        # Approximate block range from timecodes
        block_dur = total_duration / max(n_blocks, 1)
        start_block = max(0, int(clip_start / block_dur))
        end_block = min(n_blocks - 1, int(clip_end / block_dur))

        score = 0
        for bi in range(start_block, end_block + 1):
            score += block_score_map.get(bi, 5)
        score += c.get("ranking", 5) * 2  # LLM ranking still counts
        clip_scores.append((score, c))

    clip_scores.sort(reverse=True, key=lambda x: x[0])
    return [c for s, c in clip_scores if s >= min_score][:max_clips]


def pre_render_clip_shorts_for_video(
    video_id: int, channel_id: int, channel_slug: str,
    video_path: str, script_id: int = None,
) -> list[int]:
    """Pre-render all clip shorts for a long-form video right after generation.

    v26: No longer depends on pre-existing shorts_planned_slots entries.
    Instead reads shorts_clips_per_long from channel config, extracts clips
    via LLM (1 call for all), renders them, and CREATES new shorts_planned_slots
    entries with calculated target_upload_at times.

    The pre-rendered clips wait in the 'Pendiente subida' pipeline column until
    their scheduled upload time, at which point _dispatch_clip_short() detects
    the ready file and skips directly to uploading.

    Returns list of short_ids created, or empty list on failure.
    """
    import sqlite3
    import json as _json
    import time
    from datetime import datetime, timezone as _timezone, timedelta as _timedelta
    from pathlib import Path
    from config.settings import DATABASE_PATH, OUTPUT_DIR

    conn = sqlite3.connect(str(DATABASE_PATH), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")

    try:
        # 0. Guard: skip if this video already has ready clips on disk
        existing_ready = conn.execute(
            """SELECT COUNT(*) as cnt FROM shorts
               WHERE source_video_id = ?
                 AND type = 'clip'
                 AND status = 'ready'""",
            (video_id,),
        ).fetchone()
        if existing_ready and existing_ready["cnt"] > 0:
            logger.info(
                "pre_render: video #%d already has %d ready clips — skipping (already pre-rendered)",
                video_id, existing_ready["cnt"],
            )
            return []

        # 0b. Guard: skip if this video already has slots in shorts_planned_slots
        # (prevents duplicate slot creation across restarts or retries)
        existing_slots = conn.execute(
            """SELECT COUNT(*) as cnt FROM shorts_planned_slots
               WHERE source_video_id = ?
                 AND short_type = 'clip'
                 AND status IN ('pending', 'running', 'completed')""",
            (video_id,),
        ).fetchone()
        if existing_slots and existing_slots["cnt"] > 0:
            logger.info(
                "pre_render: video #%d already has %d clip slot(s) — skipping (already planned)",
                video_id, existing_slots["cnt"],
            )
            return []

        # 1. Determine how many clips to generate from channel config
        max_clips = 3  # default
        try:
            sc_row = conn.execute(
                "SELECT shorts_clips_per_long FROM shorts_planning_config WHERE channel_id = ?",
                (channel_id,),
            ).fetchone()
            if sc_row and sc_row["shorts_clips_per_long"]:
                max_clips = int(sc_row["shorts_clips_per_long"])
        except Exception:
            pass

        logger.info(
            "pre_render: video #%d → up to %d clip(s) (channel=%s)",
            video_id, max_clips, channel_slug,
        )

        # Get channel timezone for date calculations
        channel_tz = _timezone.utc
        emotional_arc = {}
        try:
            from config.config_bridge import get_channel_config
            ch_config = get_channel_config(channel_slug)
            tz_str = getattr(ch_config, "PUBLISH_TIMEZONE", None)
            if tz_str:
                from zoneinfo import ZoneInfo
                channel_tz = ZoneInfo(tz_str)
            # v2: Load emotional arc for clip scoring
            emotional_arc = getattr(ch_config, "SCRIPT_EMOTIONAL_ARC", {}) or {}
        except Exception:
            pass

        # 2. Load source video metadata (script + blocks + timing)
        source_video = Path(video_path)
        if not source_video.exists():
            logger.error(
                "pre_render: source video file not found: %s — cannot pre-render",
                video_path,
            )
            return []

        # Fetch video metadata from DB
        video_row = conn.execute(
            "SELECT * FROM videos WHERE id = ?", (video_id,),
        ).fetchone()
        if not video_row:
            logger.error("pre_render: video #%d not found in DB", video_id)
            return []
        video = dict(video_row)

        # ── v27: Calculate clip upload schedule ──
        # Clips are scheduled for TODAY (same day as the long video), snapped to
        # the next optimal shorts windows. They are NOT anchored to the long
        # video's target_public_at, which in scheduled mode sits 2-3 days ahead
        # and left clips stuck in 'ready' while recovery planner created doomed
        # duplicate slots. Same-day clips may publish before the long is public.
        now_utc = datetime.now(_timezone.utc)
        anchor = now_utc
        logger.info("pre_render: anchoring clip uploads to now (same-day) — %s", anchor.isoformat())

        # ── v10.3: Calculate clip upload schedule using optimal shorts windows ──
        # Instead of blind 60-min increments, snap each clip to the nearest
        # upcoming optimal shorts publish window (≥60 min after anchor).
        # windows: (9,30), (13,0), (18,30), (21,0) or DB optimal slots.
        # Subsequent clips spread across consecutive windows.
        upload_times = []
        channel_timezone = "Europe/Madrid"
        try:
            from config.config_bridge import get_channel_config
            ch_cfg = get_channel_config(channel_slug)
            channel_timezone = getattr(ch_cfg, "PUBLISH_TIMEZONE", "Europe/Madrid")
        except Exception:
            pass

        # Load optimal windows (or fallback to NATIVE_WINDOWS)
        windows = _load_shorts_optimal_windows(db=None, channel_id=channel_id)
        windows_used = []  # track which windows we've assigned this batch

        for i in range(max_clips):
            if i == 0:
                # First clip: snap to the nearest upcoming optimal window ≥60 min
                target_upload = _snap_to_optimal_shorts_window(
                    anchor, timezone_str=channel_timezone,
                    db=None, channel_id=channel_id,
                    min_delay_min=60,
                )
                # Find which window was used
                tz_obj = ZoneInfo(channel_timezone)
                target_local = target_upload.astimezone(tz_obj)
                used_hour = target_local.hour
                windows_used.append(used_hour)
            else:
                # Subsequent clips: use the next consecutive optimal window
                # Find the window after the previous clip's window
                prev_local = upload_times[-1].astimezone(tz_obj)
                prev_hour = prev_local.hour
                
                # Find the next window after prev_hour
                found = False
                for (wh, wm) in windows:
                    if wh > prev_hour:
                        candidate = prev_local.replace(
                            hour=wh, minute=wm, second=0, microsecond=0,
                        )
                        # Ensure at least some spacing (avoid same-hour issues)
                        if (candidate - prev_local).total_seconds() > 30 * 60:
                            target_upload = candidate.astimezone(UTC)
                            windows_used.append(wh)
                            found = True
                            break
                
                if not found:
                    # Fallback: anchor + 60*(i+1) with a small offset
                    target_upload = anchor + _timedelta(minutes=75 + 75 * (i - 1))

            # Ensure target_upload is not in the past (for immediate mode)
            if target_upload < now_utc:
                target_upload = now_utc + _timedelta(minutes=30 + 60 * i)
            upload_times.append(target_upload)

        if upload_times:
            logger.info(
                "pre_render: clip upload windows: %s → %s",
                anchor.isoformat()[:16],
                ", ".join(t.isoformat()[:16] for t in upload_times),
            )

        # Script + blocks
        script_text = ""
        bloques = []
        if video.get("script_id"):
            script_row = conn.execute(
                "SELECT guion, bloques_json FROM scripts WHERE id = ?",
                (video["script_id"],),
            ).fetchone()
            if script_row:
                script_text = script_row["guion"] or ""
                try:
                    bloques = _json.loads(script_row["bloques_json"] or "[]") if script_row["bloques_json"] else []
                except Exception:
                    bloques = []

        if not script_text and bloques:
            script_text = " ".join(b.get("texto", "") for b in bloques if b.get("texto"))
        if not script_text:
            bloques_raw = video.get("title_options") or "{}"
            try:
                fallback = _json.loads(bloques_raw) if isinstance(bloques_raw, str) else {}
            except Exception:
                fallback = {}
            # title_options is a JSON array (list of title strings), not a dict
            if not isinstance(fallback, dict):
                fallback = {}
            script_text = str(fallback.get("script", "")) or script_text

        if not script_text:
            _VIDEOS_WITHOUT_SCRIPT.add(video_id)
            logger.warning("pre_render: video #%d has no script text — marked as unusable for clips",
                          video_id)
            return []

        # 3. Build approximate word-level timestamps from blocks
        from pipeline.shorts_extractor import ShortsExtractor
        total_duration = video.get("duracion_seg") or 0
        n_blocks = len(bloques) if bloques else 1
        timestamps = []
        for idx, block in enumerate(bloques if bloques else [{"texto": script_text}]):
            texto = block.get("texto", "")
            if not texto:
                continue
            words = texto.split()
            block_start = (idx / n_blocks) * total_duration
            block_end = ((idx + 1) / n_blocks) * total_duration
            word_dur = (block_end - block_start) / max(len(words), 1)
            for wi, word in enumerate(words):
                ts_start = round(block_start + wi * word_dur, 1)
                ts_end = round(ts_start + word_dur, 1)
                timestamps.append({"word": word, "start": ts_start, "end": ts_end})

        # TTS word timestamps for subtitle rendering
        tts_word_ts = []
        try:
            td_raw = video.get("timing_data") or "{}"
            td = _json.loads(td_raw) if isinstance(td_raw, str) else td_raw
            tts_word_ts = td.get("phases", {}).get("tts_timestamps", [])
            if not isinstance(tts_word_ts, list):
                tts_word_ts = []
        except Exception:
            pass

        # 4. Run LLM extraction ONCE — get up to max_clips clips
        extractor = ShortsExtractor()

        # Query existing clips for this video (in case of retry)
        exclude_ranges = []
        existing_clips = conn.execute(
            """SELECT start_time, end_time FROM shorts
               WHERE source_video_id = ?
                 AND type = 'clip'
                 AND status IN ('published', 'uploading', 'ready', 'rendering', 'extracted')
               ORDER BY start_time""",
            (video_id,),
        ).fetchall()
        exclude_ranges = [(float(r["start_time"]), float(r["end_time"])) for r in existing_clips]

        clips = extractor.extract(
            script_text=script_text, timestamps=timestamps,
            max_clips=max_clips, min_clips=1,
            exclude_ranges=exclude_ranges,
        )

        if not clips:
            logger.error("pre_render: LLM returned no clips for video #%d", video_id)
            return []

        # ── v2: Re-rank clips by emotional arc alignment ──
        if emotional_arc and bloques:
            try:
                reordered = select_best_clips_from_blocks(
                    clips, bloques, emotional_arc, total_duration,
                    max_clips=max_clips, min_score=15,
                )
                if reordered:
                    logger.info(
                        "pre_render: emotional arc scoring — %d clips → %d (top scores from %d candidates)",
                        len(clips), len(reordered), len(clips),
                    )
                    clips = reordered
            except Exception as e_arc:
                logger.debug("pre_render: emotional arc scoring failed (non-fatal): %s", e_arc)

        logger.info(
            "pre_render: LLM returned %d clip(s) for video #%d — rendering now",
            len(clips), video_id,
        )

        # 5. Render each clip and save as 'ready'
        from pipeline.shorts_renderer import ShortsRenderer
        renderer = ShortsRenderer()
        short_ids = []

        for clip_idx, clip in enumerate(clips):
            if clip_idx >= max_clips:
                break

            # Build subtitle timestamps for this clip
            render_word_ts = None
            if tts_word_ts:
                normalized = []
                for ts in tts_word_ts:
                    start_val = float(ts.get("start_ms", ts.get("start", 0)))
                    end_val = float(ts.get("end_ms", ts.get("end", 0)))
                    normalized.append({
                        "word": ts.get("word", ""),
                        "start_ms": start_val,
                        "end_ms": end_val,
                    })
                render_word_ts = normalized
            elif timestamps:
                block_ts = []
                for ts in timestamps:
                    block_ts.append({
                        "word": ts.get("word", ""),
                        "start_ms": ts["start"] * 1000,
                        "end_ms": ts["end"] * 1000,
                    })
                render_word_ts = block_ts

            try:
                # v2: Pass hook_text for 3-second overlay on clip
                hook_txt = clip.get("hook_text", "") or clip.get("hook_title", "")
                # If hook_text is too long (>10 words), generate a short one via LLM
                if hook_txt and len(hook_txt.split()) > 10:
                    short_hook = _generate_hook_overlay_text(hook_txt, max_words=6)
                    if short_hook:
                        hook_txt = short_hook
                elif not hook_txt:
                    # Extract scene text and generate hook
                    scene_words = []
                    for ts in (tts_word_ts if tts_word_ts else timestamps):
                        if ts.get("start_ms", 0) / 1000 >= clip.get("start_time", 0) and \
                           ts.get("end_ms", 0) / 1000 <= clip.get("end_time", 0):
                            scene_words.append(ts.get("word", ""))
                    if scene_words:
                        hook_txt = _generate_hook_overlay_text(" ".join(scene_words[:100]))
                output_path = renderer.render(
                    source_video, clip, word_timestamps=render_word_ts,
                    hook_text=hook_txt if hook_txt else None,
                )
            except Exception as render_err:
                logger.error(
                    "pre_render: render failed for clip %d of video #%d: %s",
                    clip_idx + 1, video_id, render_err,
                )
                continue

            if not output_path or not output_path.exists():
                logger.error(
                    "pre_render: no output for clip %d of video #%d",
                    clip_idx + 1, video_id,
                )
                continue

            # Calculate schedule for this clip
            target_upload = upload_times[clip_idx]
            scheduled_at = target_upload - _timedelta(minutes=SHORT_GEN_LEAD_MIN_CLIP)  # upload lead time
            date_key = scheduled_at.astimezone(channel_tz).strftime("%Y-%m-%d")

            # Save to shorts table as 'ready'
            cursor = conn.execute(
                """INSERT INTO shorts
                   (channel_id, source_video_id, type, title, hook_title, hook_text,
                    start_time, end_time, status, file_path, scheduled_date)
                   VALUES (?, ?, 'clip', ?, ?, ?, ?, ?, 'ready', ?, ?)""",
                (
                    channel_id, video_id,
                    clip.get("hook_title", "Short")[:100],
                    clip.get("hook_title", "Short")[:60],
                    clip.get("hook_text", ""),
                    clip.get("start_time", 0),
                    clip.get("end_time", 0),
                    str(output_path),
                    date_key,
                ),
            )
            short_id = cursor.lastrowid

            # ── v26: Create new shorts_planned_slots entry for scheduling ──
            # This slot acts as the upload trigger — the dispatcher picks it up
            # when target_upload_at is due and uploads the pre-rendered clip.
            conn.execute(
                """INSERT INTO shorts_planned_slots
                   (channel_id, date_key, scheduled_at, target_upload_at,
                    short_type, status, slot_position, long_slot_position,
                    source_video_id, short_id)
                   VALUES (?, ?, ?, ?, 'clip', 'pending', ?, ?, ?, ?)""",
                (
                    channel_id, date_key,
                    scheduled_at.strftime("%Y-%m-%d %H:%M:%S"),
                    target_upload.strftime("%Y-%m-%d %H:%M:%S"),
                    clip_idx + 1,    # slot_position
                    clip_idx + 1,    # long_slot_position
                    video_id,        # source_video_id
                    short_id,        # short_id (links ready short for dispatch)
                ),
            )
            slot_id = cursor.lastrowid

            short_ids.append(short_id)
            logger.info(
                "pre_render: clip %d/%d rendered → short #%d (slot #%d) for %s "
                "| upload at %s",
                clip_idx + 1, len(clips), short_id, slot_id, channel_slug,
                target_upload.strftime("%Y-%m-%d %H:%M:%S"),
            )

        conn.commit()
        logger.info(
            "pre_render: video #%d → %d clip shorts pre-rendered (status=ready)",
            video_id, len(short_ids),
        )
        return short_ids

    except Exception as e:
        logger.error("pre_render: unexpected error for video #%d: %s", video_id, e,
                     exc_info=True)
        return []
    finally:
        conn.close()


def _resolve_source_video(video: dict, clip_start: float, clip_end: float):
    """Find or download the source video for clip extraction.
    Returns (Path, offset_seconds) or (None, None)."""
    import subprocess
    import tempfile
    from pathlib import Path

    # 1. Try local file
    if video.get("video_path"):
        for p in [Path(video["video_path"]),
                  Path("/root/autotube") / str(video["video_path"])]:
            if p.exists():
                return p, 0.0

    # 2. Download clip segment from YouTube
    yt_id = video.get("yt_video_id")
    if not yt_id:
        return None, None

    yt_url = video.get("yt_url") or f"https://www.youtube.com/watch?v={yt_id}"
    padding = 3.0
    section_start = max(0, clip_start - padding)
    section_end = clip_end + padding
    section_spec = f"*{section_start:.1f}-{section_end:.1f}"

    with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as tmp:
        tmp_path = tmp.name

    try:
        result = subprocess.run(
            ["yt-dlp",
             "--download-sections", section_spec,
             "-f", "best[height<=720]/best",
             "--no-playlist",
             "--socket-timeout", "30",
             "--retries", "5",
             "--fragment-retries", "5",
             "--extractor-retries", "3",
             "--file-access-retries", "3",
             "--force-overwrites",
             "--no-warnings",
             "-o", tmp_path,
             yt_url],
            capture_output=True, text=True, timeout=300,
        )
        if result.returncode != 0 or not Path(tmp_path).exists():
            logger.error("yt-dlp failed (code %d): %s",
                         result.returncode,
                         result.stderr[-500:] if result.stderr else "(no output)")
            Path(tmp_path).unlink(missing_ok=True)
            return None, None
        return Path(tmp_path), padding
    except Exception:
        Path(tmp_path).unlink(missing_ok=True)
        return None, None


# ── Internal helpers ───────────────────────────────────────────

def _channel_shorts_cooldown_ok(channel_id: int, db) -> bool:
    """Check if enough time has passed since the channel's last completed short.

    Returns True if the channel is clear to dispatch another short.
    Returns False if the channel's last completed short was less than
    SHORTS_COOLDOWN_MINUTES (o el cooldown del perfil de pacing) ago.
    """
    last_completed = db.get_channel_last_short_completed_at(channel_id)
    if last_completed is None:
        return True  # No completed shorts yet — always ok

    try:
        last_time = datetime.strptime(last_completed, "%Y-%m-%d %H:%M:%S")
        # updated_at in shorts_planned_slots is stored in UTC
        # (CURRENT_TIMESTAMP / datetime('now')). Treating it as local
        # (DEFAULT_TIMEZONE) shifted it -2h and expired the cooldown early.
        last_utc = last_time.replace(tzinfo=UTC)
        elapsed = (datetime.now(UTC) - last_utc).total_seconds()
        return elapsed >= _shorts_cooldown_minutes() * 60
    except (ValueError, TypeError):
        return True  # Can't parse — let it proceed


def _same_type_shorts_slot_conflict(
    channel_id: int, short_type: str,
    target_upload_at: str, db,
    exclude_slot_id: int | None = None,
    cross_type: bool = False,
) -> bool:
    """Check for same-channel shorts publish collisions.

    v10.3 (Aug 2026): Extended to check:
      - shorts_planned_slots (pending/running/completed same-type, ±45 min)
      - shorts_planned_slots (pending/running/completed cross-type, ±20 min)
      - shorts table (already published, ±gap)
      - Catch-up bypass limited to >6h past-due (was: any past-due)

    Returns True if a collision is detected, False if the slot is clear
    or exempted via catch-up bypass.

    Args:
        channel_id: channel to check
        short_type: 'native' or 'clip'
        target_upload_at: ISO8601 timestamp of the candidate slot
        db: database instance
        exclude_slot_id: optional slot ID to exclude from conflict check
        cross_type: if True, also check cross-type collisions (native↔clip)
    """
    if not target_upload_at:
        return False

    try:
        target_dt = datetime.fromisoformat(
            target_upload_at.replace("Z", "+00:00").replace(" ", "T"))
        # target_upload_at is stored in UTC (see _minutes_to_utc_slot).
        # A naive value must be interpreted as UTC, not local time.
        if target_dt.tzinfo is None:
            target_dt = target_dt.replace(tzinfo=UTC)
    except (ValueError, TypeError):
        return False

    # ── v10.3: Limited catch-up bypass ──
    # Only skip gap enforcement if the slot is >6h past-due.
    # Previously any past-due slot was dispatched immediately, causing
    # avalanche clustering. Now we require a meaningful delay.
    now_utc = datetime.now(UTC)
    if target_dt < now_utc:
        hours_late = (now_utc - target_dt).total_seconds() / 3600.0
        if hours_late >= CATCH_UP_BYPASS_HOURS:
            logger.info(
                "Bypassing conflict for severely past-due shorts slot "
                "(target=%s, now=%s, %.1fh late — catch-up mode)",
                target_dt.isoformat()[:16], now_utc.isoformat()[:16], hours_late,
            )
            return False
        else:
            logger.debug(
                "Past-due shorts slot (%sh late) — NOT bypassing, "
                "will enforce spacing (threshold: %sh)",
                f"{hours_late:.1f}", CATCH_UP_BYPASS_HOURS,
            )

    # ── Same-type gap (perfil de pacing) ──
    same_gap = timedelta(minutes=_same_type_gap_minutes())
    # ── Cross-type gap (perfil de pacing) ──
    cross_gap = timedelta(minutes=_cross_type_gap_minutes())

    # ── Check 1: shorts_planned_slots — same-type ──
    window_same_start = (target_dt - same_gap).strftime("%Y-%m-%d %H:%M:%S")
    window_same_end = (target_dt + same_gap).strftime("%Y-%m-%d %H:%M:%S")

    try:
        with db._connect() as conn:
            if exclude_slot_id is not None:
                existing = conn.execute(
                    """SELECT sps.id, sps.short_type, sps.target_upload_at, sps.status
                       FROM shorts_planned_slots sps
                       WHERE sps.channel_id = ?
                         AND sps.short_type = ?
                         AND sps.status IN ('pending', 'running', 'completed')
                         AND sps.target_upload_at IS NOT NULL
                         AND sps.target_upload_at >= ?
                         AND sps.target_upload_at <= ?
                         AND sps.id != ?
                       ORDER BY sps.target_upload_at
                       LIMIT 3""",
                    (channel_id, short_type, window_same_start, window_same_end,
                     exclude_slot_id),
                ).fetchall()
            else:
                existing = conn.execute(
                    """SELECT sps.id, sps.short_type, sps.target_upload_at, sps.status
                       FROM shorts_planned_slots sps
                       WHERE sps.channel_id = ?
                         AND sps.short_type = ?
                         AND sps.status IN ('pending', 'running', 'completed')
                         AND sps.target_upload_at IS NOT NULL
                         AND sps.target_upload_at >= ?
                         AND sps.target_upload_at <= ?
                       ORDER BY sps.target_upload_at
                       LIMIT 3""",
                    (channel_id, short_type, window_same_start, window_same_end),
                ).fetchall()

            if existing:
                logger.debug(
                    "Same-type conflict: %s slot ch=%d has %d nearby same-type "
                    "slots in [%s .. %s]",
                    short_type, channel_id, len(existing),
                    window_same_start, window_same_end,
                )
                return True

            # ── Check 2: shorts_planned_slots — cross-type (if requested) ──
            if cross_type:
                other_type = "clip" if short_type == "native" else "native"
                window_cross_start = (target_dt - cross_gap).strftime("%Y-%m-%d %H:%M:%S")
                window_cross_end = (target_dt + cross_gap).strftime("%Y-%m-%d %H:%M:%S")

                if exclude_slot_id is not None:
                    cross_existing = conn.execute(
                        """SELECT sps.id, sps.short_type, sps.target_upload_at, sps.status
                           FROM shorts_planned_slots sps
                           WHERE sps.channel_id = ?
                             AND sps.short_type = ?
                             AND sps.status IN ('pending', 'running', 'completed')
                             AND sps.target_upload_at IS NOT NULL
                             AND sps.target_upload_at >= ?
                             AND sps.target_upload_at <= ?
                             AND sps.id != ?
                           LIMIT 3""",
                        (channel_id, other_type, window_cross_start, window_cross_end,
                         exclude_slot_id),
                    ).fetchall()
                else:
                    cross_existing = conn.execute(
                        """SELECT sps.id, sps.short_type, sps.target_upload_at, sps.status
                           FROM shorts_planned_slots sps
                           WHERE sps.channel_id = ?
                             AND sps.short_type = ?
                             AND sps.status IN ('pending', 'running', 'completed')
                             AND sps.target_upload_at IS NOT NULL
                             AND sps.target_upload_at >= ?
                             AND sps.target_upload_at <= ?
                           LIMIT 3""",
                        (channel_id, other_type, window_cross_start, window_cross_end),
                    ).fetchall()

                if cross_existing:
                    logger.debug(
                        "Cross-type conflict: %s slot ch=%d has %d nearby %s "
                        "slots in [%s .. %s]",
                        short_type, channel_id, len(cross_existing), other_type,
                        window_cross_start, window_cross_end,
                    )
                    return True

            # ── Check 3: shorts table — already published shorts ──
            # v10.3: Also check recently published shorts. A short that was
            # published 10 min ago with no planned_slot counterpart still
            # occupies the time window and should prevent clustering.
            window_pub_start = (target_dt - same_gap).strftime("%Y-%m-%d %H:%M:%S")
            window_pub_end = (target_dt + same_gap).strftime("%Y-%m-%d %H:%M:%S")

            published = conn.execute(
                """SELECT s.id, s.type, s.published_at
                   FROM shorts s
                   WHERE s.channel_id = ?
                     AND s.status = 'published'
                     AND s.published_at IS NOT NULL
                     AND s.published_at >= ?
                     AND s.published_at <= ?
                   ORDER BY s.published_at
                   LIMIT 5""",
                (channel_id, window_pub_start, window_pub_end),
            ).fetchall()

            for row in published:
                pub_type = row["type"]
                # Same-type check: 45 min gap
                if pub_type == short_type:
                    logger.debug(
                        "Published short conflict: %s slot ch=%d conflicts with "
                        "published %s short #%d at %s",
                        short_type, channel_id, pub_type, row["id"], row["published_at"],
                    )
                    return True
                # Cross-type check: 20 min gap (if cross_type requested)
                elif cross_type:
                    # Parse published_at to compute exact gap
                    try:
                        pub_dt = datetime.fromisoformat(
                            str(row["published_at"]).replace("Z", "+00:00").replace(" ", "T"))
                        if pub_dt.tzinfo is None:
                            pub_dt = pub_dt.replace(tzinfo=DEFAULT_TIMEZONE).astimezone(UTC)
                        gap_min = abs((target_dt - pub_dt).total_seconds()) / 60.0
                        if gap_min < _cross_type_gap_minutes():
                            logger.debug(
                                "Published cross-type conflict: %s slot ch=%d too close "
                                "to published %s short #%d (%.0f min gap < %d min)",
                                short_type, channel_id, pub_type, row["id"],
                                gap_min, _cross_type_gap_minutes(),
                            )
                            return True
                    except (ValueError, TypeError):
                        pass

    except Exception as exc:
        logger.debug("Shorts collision check failed: %s", exc)

    return False


def _sync_running_shorts_slots(db):
    """Check running shorts slots across ALL recent dates: mark completed if their
    short exists, mark failed if their generation job died (e.g. server restart).

    Previously only scanned today's slots, which left server-restart orphans
    stuck in 'running' state indefinitely for previous days.
    """
    today = datetime.now(DEFAULT_TIMEZONE).date()
    all_running = []
    for offset in range(-7, 1):  # scan last 7 days including today
        date_key = (today + timedelta(days=offset)).isoformat()
        slots = db.get_shorts_planned_slots(date_key=date_key, status="running")
        if slots:
            all_running.extend(slots)

    if not all_running:
        return

    for s in all_running:
        slot_id = s["id"]
        short_id = s.get("short_id")
        job_id = s.get("job_id")

        # Case 1: short exists and is published → mark completed
        if short_id:
            short = db.get_short(short_id)
            if short and short.get("status") == "published":
                db.update_shorts_slot_status(slot_id, "completed")
                logger.info("Shorts slot #%d marked completed", slot_id)
                continue

        # Case 2: linked job failed (server restart, error, etc.) → reset to pending
        # Previously these were marked as 'failed' and lost until the recovery
        # planner ran (up to 60 min later, only during active hours). Now they
        # reset to 'pending' with job_id=NULL so the dispatcher picks them up
        # automatically in the next tick.
        if job_id:
            job = db.get_job(job_id)
            if job is None or job.get("status") == "failed":
                # Reset to pending: clear job link + error so the dispatcher
                # sees it as a fresh pending slot. Uses direct SQL because
                # update_shorts_slot_status() skips fields when None is passed.
                import sqlite3 as _sql_sync
                from config.settings import DATABASE_PATH as _DBP
                with _sql_sync.connect(str(_DBP), timeout=30) as _conn:
                    _conn.execute(
                        "UPDATE shorts_planned_slots SET status='pending', "
                        "job_id=NULL, error_message=NULL, updated_at=CURRENT_TIMESTAMP "
                        "WHERE id=?",
                        (slot_id,),
                    )
                    _conn.commit()
                reason = "orphaned after restart" if job is None else "job failed"
                logger.info("Shorts slot #%d reset to pending (%s)", slot_id, reason)


def _cancel_stale_shorts_slots(db):
    """Cancel pending shorts slots that are >24h past their scheduled_at (UTC).
    
    Scans ALL dates (not just today) to catch stuck slots from previous days.
    Threshold is 24h to match the 24h visibility window in
    get_next_pending_shorts_slot(), ensuring slots aren't cancelled before
    they get a fair chance to be dispatched (e.g. after a long-form video
    that took several hours to generate).
    """
    STALE_HOURS = 24
    # Fetch pending slots across all recent dates (past 7 days)
    today = datetime.now(DEFAULT_TIMEZONE).date()
    all_pending = []
    for offset in range(-7, 1):  # from 7 days ago to today
        date_key = (today + timedelta(days=offset)).isoformat()
        pending = db.get_shorts_planned_slots(date_key=date_key, status="pending")
        if pending:
            all_pending.extend(pending)

    if not all_pending:
        return

    now_utc = datetime.now(UTC)
    cancelled = 0
    for s in all_pending:
        try:
            # scheduled_at is stored in UTC (see _minutes_to_utc_slot /
            # pre_render_clip_shorts_for_video). Treating it as local
            # (replace tzinfo=DEFAULT_TIMEZONE) shifted it -2h, so slots were
            # cancelled ~2h before the intended 24h staleness threshold.
            sched = datetime.strptime(
                s["scheduled_at"], "%Y-%m-%d %H:%M:%S"
            ).replace(tzinfo=UTC)
        except (ValueError, TypeError):
            continue
        if (now_utc - sched).total_seconds() > STALE_HOURS * 3600:
            db.update_shorts_slot_status(s["id"], "cancelled")
            cancelled += 1
            # Also cancel the linked pre-rendered clip short, if still 'ready'.
            # Otherwise it becomes an orphaned 'ready' row (no upload trigger)
            # that stays in the 'Pendiente subida' column forever and blocks
            # clip-source dedup for its long video.
            if s.get("short_id"):
                try:
                    short_row = db.get_short(s["short_id"])
                    if short_row and short_row.get("status") == "ready":
                        import sqlite3 as _sql_cancel
                        from config.settings import DATABASE_PATH as _DBP_CANCEL
                        with _sql_cancel.connect(str(_DBP_CANCEL), timeout=30) as _conn_c:
                            _conn_c.execute(
                                "UPDATE shorts SET status = 'cancelled', "
                                "updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                                (s["short_id"],),
                            )
                            _conn_c.commit()
                except Exception:
                    pass

    if cancelled:
        logger.info("Cancelled %d stale pending shorts slots (>%dh past scheduled)", cancelled, STALE_HOURS)


def _memory_ok(min_free_gb: float = 4.0) -> bool:
    """Check if enough RAM is available for dispatch.
    
    Shorts use minimal RAM (1.0 GB), long-form videos need 4.0 GB.
    """
    try:
        from pipeline.ram_governor import available_mb
        avail_mb = available_mb()
        if avail_mb < 0:
            return True  # Can't determine — let it proceed
        min_free_mb = min_free_gb * 1024
        return avail_mb >= min_free_mb
    except ImportError:
        return True


# ═══════════════════════════════════════════════════════════════════
# Standalone shorts auto-dispatch
# ═══════════════════════════════════════════════════════════════════

def dispatch_standalone_shorts_daily() -> dict:
    """Auto-dispatch standalone shorts for all active channels.

    Creates 1-2 standalone short slots per channel per day (NOT pre-planned
    — generated on demand). This function is called periodically from the
    API background loop.

    ANTI-SATURACIÓN / ANTI-SPAM (ago 2026):
      - Kill-switch global (`shorts_paused`/`scheduler_paused`).
      - Solo UN standalone a la vez GLOBAL (registra generation_jobs y usa
        `get_active_shorts_job`, que ahora incluye `generate_standalone_short`).
      - Gate de RAM (TTS/render pesan 2-4 GB).
      - Salta canales bloqueados por spam.
      - Despacha secuencialmente (sin thread por canal) y solo 1 por tick.

    Returns: {"dispatched": N, "errors": N}
    """
    import logging
    logger = logging.getLogger("autotube.standalone")

    result = {"dispatched": 0, "errors": 0}

    try:
        from database.db_extended import ExtendedDatabase
        db = ExtendedDatabase()

        # Kill-switch global de shorts
        if _shorts_paused(db):
            logger.info("[standalone] shorts paused (kill-switch) — skip daily dispatch")
            return result

        # Solo UN short global a la vez (incluye standalone tras el fix de
        # get_active_shorts_job). Evita varios TTS+ffmpeg concurrentes.
        if db.get_active_shorts_job():
            logger.info("[standalone] otro short ya activo — skip daily dispatch")
            return result

        # Gate de RAM: TTS/render necesitan margen.
        from config.settings import MIN_FREE_FOR_TTS_MB
        if not _memory_ok(min_free_gb=MIN_FREE_FOR_TTS_MB / 1024.0):
            logger.warning("[standalone] RAM insuficiente — skip daily dispatch")
            return result

        channels = db.get_channels(active_only=True)
        for ch in channels:
            channel_id = ch["id"]
            slug = ch["slug"]

            # Canal penalizado por spam de YouTube: no tocar
            if _channel_shorts_spam_blocked(channel_id, db):
                logger.info("[standalone] %s spam-blocked — skip", slug)
                continue

            # Tope diario por canal
            today_dt = __import__("datetime").datetime.now().strftime("%Y-%m-%d")
            conn = __import__("sqlite3").connect(
                str(__import__("config.settings", fromlist=["DATABASE_PATH"]).DATABASE_PATH), timeout=10
            )
            today_count = conn.execute(
                """SELECT COUNT(*) FROM generation_jobs
                   WHERE action = 'generate_standalone_short'
                     AND status IN ('completed', 'running', 'queued')
                     AND created_at >= ?""",
                (today_dt,),
            ).fetchone()[0]
            conn.close()

            conn2 = __import__("sqlite3").connect(
                str(__import__("config.settings", fromlist=["DATABASE_PATH"]).DATABASE_PATH), timeout=10
            )
            row = conn2.execute(
                "SELECT shorts_standalone_per_day FROM shorts_planning_config WHERE channel_id = ?",
                (channel_id,),
            ).fetchone()
            conn2.close()
            max_per_day = row[0] if row and row[0] else 2

            if today_count >= max_per_day:
                continue

            # Registra el job para que get_active_shorts_job lo vea como activo
            # y el resto de la cola no despache otro short en paralelo.
            job_id = db.create_job(channel_id, "generate_standalone_short")
            db.update_job(job_id, status="running", progress=0, phase="standalone")

            # Despacha UN standalone (síncrono, secuencial) y sale.
            try:
                from api.services.shorts_scheduler import _dispatch_standalone_short
                short_id = _dispatch_standalone_short(channel_id, slug, job_id=job_id)
                if short_id:
                    db.update_job(job_id, status="completed", progress=100)
                    result["dispatched"] += 1
                    logger.info("[standalone] %s OK (%d/%d today)", slug, today_count + 1, max_per_day)
                else:
                    db.update_job(job_id, status="failed", error_msg="No short_id returned (standalone)")
                    result["errors"] += 1
                    logger.warning("[standalone] %s falló (no short_id)", slug)
            except Exception as e:
                db.update_job(job_id, status="failed", error_msg=str(e)[:300])
                result["errors"] += 1
                logger.warning("[standalone] Dispatch error for %s: %s", slug, e)
            break  # solo un standalone por tick (anti-ráfaga)

    except Exception as e:
        logger.warning("[standalone] Daily dispatch failed: %s", e)
        result["errors"] += 1

    return result
