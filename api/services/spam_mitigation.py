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


# ── Cap de subidas por cuenta Google (antiban, ago 2026) ──────────
# Los strikes de YouTube se registran por CUENTA/PROYECTO GCP, no por canal.
# Dos canales hermanos que comparten cuenta pueden saturarla aunque cada uno
# cumpla su cap individual. ACCOUNT_DAILY_UPLOAD_CAP (config/defaults.py)
# limita las SUBIDAS TOTALES (long-form + shorts) por cuenta y día.

def get_channel_account(channel_slug: str, db=None) -> str:
    """Cuenta Google del canal (google_account) o '' si no tiene."""
    if db is None:
        from database.db_extended import ExtendedDatabase
        db = ExtendedDatabase()
    try:
        ch = db.get_channel_by_slug(channel_slug)
        return ((ch or {}).get("google_account") or "").strip()
    except Exception:
        return ""


def get_account_upload_cap(db=None) -> int:
    """Tope de subidas/día por cuenta Google (0 = desactivado)."""
    try:
        from config.defaults import ACCOUNT_DAILY_UPLOAD_CAP
        return int(ACCOUNT_DAILY_UPLOAD_CAP or 0)
    except Exception:
        return 0


def get_account_daily_uploads(account: str, db=None) -> int:
    """Subidas de HOY (long-form + shorts, subidos o publicados) de una cuenta.

    Cuenta long-form con uploaded_at/published_at de hoy y shorts con
    published_at de hoy (los status 'generated' en cola NO cuentan).
    """
    if db is None:
        from database.db_extended import ExtendedDatabase
        db = ExtendedDatabase()
    if not account:
        return 0
    try:
        channels = [c for c in (db.get_channels(active_only=False) or [])
                    if ((c.get("google_account") or "").strip()) == account]
    except Exception:
        return 0
    ids = [int(c.get("id") or 0) for c in channels if c.get("id")]
    if not ids:
        return 0
    ids_sql = ",".join(str(i) for i in ids)
    today = __import__("datetime").date.today().isoformat()
    import sqlite3
    try:
        with db._connect() as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                f"""SELECT
                      (SELECT COUNT(*) FROM videos
                        WHERE channel_id IN ({ids_sql})
                          AND status IN ('uploaded','uploaded_private','published','warming')
                          AND (date(uploaded_at) = ? OR date(published_at) = ?)) AS vids,
                      (SELECT COUNT(*) FROM shorts
                        WHERE channel_id IN ({ids_sql})
                          AND status IN ('uploaded','published')
                          AND date(published_at) = ?) AS shs""",
                (today, today, today),
            ).fetchone()
        return int((row["vids"] or 0) + (row["shs"] or 0))
    except Exception:
        return 0


def account_upload_slots_available(account: str, db=None) -> bool:
    """True si la cuenta aún no ha alcanzado su cap diario de subidas.

    cap <= 0 → guard desactivado (siempre disponible).
    """
    cap = get_account_upload_cap(db)
    if cap <= 0:
        return True
    return get_account_daily_uploads(account, db) < cap


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

    # ── Watchdog de detección temprana (antiban) ──
    try:
        check_deleted_yt_watchdog(db)
    except Exception as exc:
        logger.warning("deleted_yt watchdog failed: %s", exc)

    return {"buffer_extended": extended, "held": held_total, "channels": channels_held}


def check_deleted_yt_watchdog(db=None) -> dict:
    """Alerta temprana (antiban, ago 2026): si el nº de vídeos borrados por
    YouTube (status='deleted_on_yt') sube, avisa ANTES de que se acumulen
    strikes. Idempotente: guarda la última cifra vista en system_state y solo
    alerta cuando la cifra actual supera el baseline. Se llama en
    ensure_spam_holds (arranque de la API).
    """
    if db is None:
        from database.db_extended import ExtendedDatabase
        db = ExtendedDatabase()
    try:
        with db._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) c FROM videos WHERE status='deleted_on_yt'"
            ).fetchone()
        current = int(row[0])
    except Exception:
        return {"ok": False, "deleted_on_yt": None}

    baseline_key = "deleted_yt_baseline"
    try:
        prev = int(db.get_system_state(baseline_key) or 0)
    except (TypeError, ValueError):
        prev = 0

    if prev == 0 and current > 0:
        db.set_system_state(baseline_key, str(current))
        return {"ok": True, "deleted_on_yt": current, "delta": 0, "alerted": False}
    if current > prev:
        db.set_system_state(baseline_key, str(current))
        try:
            from api.services.lifecycle_monitor import create_alert
            create_alert(
                db,
                entity_type="system", entity_id=None, channel_id=None,
                alert_type="deleted_yt_increased", severity="warning",
                title=f"YouTube eliminó {current - prev} vídeo(s) nuevo(s) (total {current})",
                message=(
                    f"El nº de vídeos borrados por YouTube subió de {prev} a {current}. "
                    f"Es una señal temprana de flag de spam/IA: revisa los canales, "
                    f"verifica el marcado 'contenido alterado/IA' y NO aumentes la "
                    f"frecuencia de publicación."
                ),
                metadata={"prev": prev, "current": current, "delta": current - prev},
            )
        except Exception as exc:
            logger.warning("deleted_yt watchdog alert failed: %s", exc)
        return {"ok": True, "deleted_on_yt": current, "delta": current - prev, "alerted": True}
    if current < prev:
        db.set_system_state(baseline_key, str(current))
    return {"ok": True, "deleted_on_yt": current, "delta": 0, "alerted": False}


# ── Situación del canal + informe LLM (banner / modal) ───────────

SPAM_REPORT_TTL_SEC = 12 * 3600  # caché del informe LLM: 12h

_REPORT_KEY = "spam_report_{channel_id}"


def _pending_publish_all(channel_id: int, db) -> list[dict]:
    """Todos los vídeos programados (private) del canal con publishAt futuro."""
    import sqlite3

    now = time.time()
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
        if dt.timestamp() > now:
            out.append({
                "id": r["id"],
                "video_id": r["yt_video_id"],
                "target_public_at": dt.isoformat(),
            })
    return out


def _why_text(last_removal: dict | None, strikes: int) -> str:
    """Texto legible del porqué del bloqueo."""
    if last_removal:
        vid = last_removal.get("video_id") or "desconocido"
        reason = last_removal.get("reason") or "no especificada"
        return f"strike #{strikes}: YouTube eliminó el contenido ({vid}) — {reason}"
    return f"strike #{strikes} de spam de YouTube (contenido eliminado tras subida)"


def build_spam_situation(channel_id: int, db=None) -> dict:
    """Construye el resumen JSON de la situación de un canal (bloqueado o no)."""
    if db is None:
        from database.db_extended import ExtendedDatabase
        db = ExtendedDatabase()

    ch = db.get_channel(channel_id)
    if not ch:
        return {}
    slug = ch.get("slug", "")
    now = time.time()

    raw_block = db.get_system_state(f"shorts_spam_blocked_until_{channel_id}")
    blocked_until = None
    blocked = False
    restan_h = 0.0
    if raw_block:
        try:
            blocked_until = float(raw_block)
            blocked = now < blocked_until
            restan_h = round((blocked_until - now) / 3600.0, 1) if blocked else 0.0
        except (TypeError, ValueError):
            pass

    strikes = 0
    try:
        strikes = int(db.get_system_state(f"shorts_spam_strikes_{channel_id}") or 0)
    except (TypeError, ValueError):
        strikes = 0

    last_removal = None
    try:
        raw_removal = db.get_system_state(f"shorts_spam_last_removal_{channel_id}")
        if raw_removal:
            last_removal = json.loads(raw_removal)
    except Exception:
        last_removal = None

    pending_all = _pending_publish_all(channel_id, db)
    pending_within_block = []
    if blocked_until:
        for v in pending_all:
            try:
                if datetime.fromisoformat(v["target_public_at"]).timestamp() < blocked_until:
                    pending_within_block.append(v)
            except (ValueError, TypeError):
                continue

    freq_reduced = bool(db.get_system_state(f"spam_freq_restore_{channel_id}"))
    cfg = db.get_channel_planning_config(channel_id) or {}
    sc_list = db.get_shorts_planning_config(channel_id=channel_id) or []
    sc = sc_list[0] if sc_list else {}

    # Alcance: el gate central bloquea shorts + long-form durante la penalización.
    scope = "todo" if blocked else "none"

    return {
        "channel_id": channel_id,
        "slug": slug,
        "name": ch.get("name", ""),
        "blocked": blocked,
        "blocked_until": blocked_until,
        "restan_h": restan_h,
        "strikes": strikes,
        "scope": scope,
        "why": _why_text(last_removal, strikes),
        "last_removal": last_removal,
        "pending_publish": {
            "total": len(pending_all),
            "within_block": pending_within_block,
        },
        "freq_reduced": freq_reduced,
        "current_freq": {
            "videos_per_day": cfg.get("videos_per_day", 2),
            "shorts_native_per_day": sc.get("shorts_native_per_day", 3),
            "shorts_clips_per_long": sc.get("shorts_clips_per_long", 3),
        },
    }


def _llm_spam_report(situation: dict) -> dict:
    """Llama al LLM para obtener el informe estructurado de la situación."""
    try:
        from config.llm_client import create_llm_client
        from config.llm_helpers import llm_json_call
        from config.settings import LLM_MODEL

        client = create_llm_client()  # thinking disabled: tarea simple
        model = LLM_MODEL

        system = (
            "Eres un asistente de operaciones de canales de YouTube automatizados "
            "(faceless channels). Recibes un JSON con la situación de un canal "
            "bloqueado por spam. Responde SOLO con JSON válido con estas claves:\n"
            "- que_ha_pasado: (string) resumen claro en español.\n"
            "- por_que: (string) causa raíz probable.\n"
            "- alcance_del_bloqueo: (string) si el bloqueo afecta a shorts, vídeos "
            "largos o a todo, explicado para el operador.\n"
            "- publicaciones_pendientes: (string) si los vídeos ya subidos con "
            "publishAt asignado se publicarán o fueron reprogramados, y qué implica.\n"
            "- como_solventar: (array de strings) pasos concretos para resolver.\n"
            "- prompt_reutilizable: (string) un prompt corto en español, listo para "
            "pegar en un agente de automatización (opencode), que ajuste la "
            "configuración del canal para que no vuelva a pasar (bajar frecuencia de "
            "shorts, pacing, revisar títulos duplicados) Y que planifique la "
            "reanudación gradual de la publicación.\n"
            "- reanudacion_gradual: (array de strings) plan por fases para retomar "
            "la publicación poco a poco.\n"
            "Sé concreto y accionable. No inventes datos que no estén en la situación."
        )
        user = json.dumps(situation, ensure_ascii=False, default=str)
        return llm_json_call(
            client, max_retries=2, retry_delay=2.0,
            model=model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            temperature=0.4,
            max_tokens=2500,
            response_format={"type": "json_object"},
        )
    except Exception as exc:
        logger.warning("LLM spam report failed: %s", exc)
        return {}


def generate_spam_report(channel_id: int, db=None, force: bool = False) -> dict:
    """Devuelve el informe LLM de un canal bloqueado (con caché de 12h).

    ``force=True`` regenera ignorando la caché.
    """
    if db is None:
        from database.db_extended import ExtendedDatabase
        db = ExtendedDatabase()

    cache_key = _REPORT_KEY.format(channel_id=channel_id)
    if not force:
        raw = db.get_system_state(cache_key)
        if raw:
            try:
                cached = json.loads(raw)
                gen_at = cached.get("generated_at", "")
                if gen_at:
                    gen_dt = datetime.fromisoformat(gen_at)
                    if (datetime.now(timezone.utc) - gen_dt).total_seconds() < SPAM_REPORT_TTL_SEC:
                        return cached
            except (json.JSONDecodeError, TypeError, ValueError):
                pass

    situation = build_spam_situation(channel_id, db)
    if not situation:
        return {"ok": False, "message": "canal no encontrado"}

    report = _llm_spam_report(situation)
    if not report:
        return {"ok": False, "message": "LLM no disponible (reintenta más tarde)", "situation": situation}

    report["situation"] = situation
    report["generated_at"] = datetime.now(timezone.utc).isoformat()
    try:
        db.set_system_state(cache_key, json.dumps(report))
    except Exception as exc:
        logger.warning("Spam report cache write failed: %s", exc)
    return report
