#!/usr/bin/env python3
"""Backfill paulatino del marcado "contenido alterado/IA" (navegador, 0 quota).

Recupera los items que quedaron sin marcar por el bug del thread daemon del
worker (ver `specs/experimento-recuperacion-alcance.md`). Marca vía Playwright
en YouTube Studio (`pipeline.youtube_browser.mark_altered_content`) — **nunca**
usa la Data API, así que no consume quota.

Modelo de ejecución (decidido por el operador):
- **Sesión continua diurna**: arranca y no para hasta que se hace de noche
  (por defecto 23:00 Europe/Madrid) o hasta terminar todos los pendientes.
- **Un canal de principio a fin** (más recientes primero); al terminar, sigue
  con el siguiente canal en la misma sesión.
- **Orden fijo por cuenta**: canal2 → canal3 → canal4 → canal5.
- **Retoma al día siguiente** el canal donde se quedó (estado en system_state).

Uso:
    python3 scripts/yt_mark_pending_ia.py --dry-run
    python3 scripts/yt_mark_pending_ia.py                 # sesión (ventana 09-23)
    python3 scripts/yt_mark_pending_ia.py --canal canal2 --no-window
    python3 scripts/yt_mark_pending_ia.py --max-items 5 --no-window
"""

from __future__ import annotations

import argparse
import logging
import os
import random
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

DB_PATH = Path(os.environ.get("DATABASE_PATH") or (PROJECT_ROOT / "autotube.db"))

# Motivos de fallo que indican un problema técnico de sesión/navegador (no del
# ítem): si se repiten, abortamos la sesión del día en vez de blacklistear ítems.
TECHNICAL_REASONS = {"exception", "navigation_failed"}

CHANNEL_ORDER = ["canal2", "canal3", "canal4", "canal5"]
STATE_CURRENT = "ia_backfill_current_channel"
STATE_DONE_TOTAL = "ia_backfill_done_total"
STATE_PENDING = "ia_backfill_pending"
STATE_LAST_RUN = "ia_backfill_last_run"
STATE_FINISHED_AT = "ia_backfill_finished_at"
STATE_LAST_PROGRESS = "ia_backfill_last_progress"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("ia_backfill")


# ── Utilidades de tiempo ────────────────────────────────────────────

def _now_local() -> datetime:
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("Europe/Madrid"))
    except Exception:
        return datetime.now()


def in_window(now: datetime, start_hour: int = 9, end_hour: int = 23) -> bool:
    """Ventana diurna [start_hour, end_hour) en hora local."""
    return start_hour <= now.hour < end_hour


def channel_sequence(current: str | None) -> list[str]:
    """Orden de canales empezando por ``current``, con wrap-around.

    Sin wrap-around, si el estado quedaba en canal5 (o canal4) la sesión nunca
    volvía a canal2/canal3 y esos canales quedaban starvados indefinidamente.
    """
    if current not in CHANNEL_ORDER:
        return list(CHANNEL_ORDER)
    start = CHANNEL_ORDER.index(current)
    return CHANNEL_ORDER[start:] + CHANNEL_ORDER[:start]


# ── DB ──────────────────────────────────────────────────────────────

def get_pending(canal: str | None = None) -> list[dict]:
    """Items sin marcar, más recientes primero (videos + shorts).

    Excluye ítems ya descartados (``manual_altered_content_skip=1``) y los que
    YouTube ya eliminó/no tiene disponibles: reintentarlos bloqueaba la cola y
    abortaba la sesión diaria sin avanzar.
    """
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    items: list[dict] = []

    vq = """
        SELECT v.yt_video_id AS yt_id, c.slug AS canal, 'video' AS kind, v.id AS db_id,
               COALESCE(v.uploaded_at, v.published_at, v.created_at) AS ts
        FROM videos v JOIN channels c ON c.id = v.channel_id
        WHERE v.yt_video_id IS NOT NULL AND v.yt_video_id != ''
          AND COALESCE(v.manual_altered_content_done, 0) = 0
          AND COALESCE(v.manual_altered_content_skip, 0) = 0
          AND COALESCE(v.status, '') NOT IN ('deleted_on_yt', 'removed')
          AND COALESCE(v.yt_visibility, '') NOT IN ('removed', 'unavailable')
    """
    sq = """
        SELECT s.youtube_id AS yt_id, c.slug AS canal, 'short' AS kind, s.id AS db_id,
               COALESCE(s.actual_published_at, s.published_at, s.created_at) AS ts
        FROM shorts s JOIN channels c ON c.id = s.channel_id
        WHERE s.youtube_id IS NOT NULL AND s.youtube_id != ''
          AND COALESCE(s.manual_altered_content_done, 0) = 0
          AND COALESCE(s.manual_altered_content_skip, 0) = 0
          AND COALESCE(s.yt_visibility, '') NOT IN ('removed', 'unavailable')
    """
    params: list = []
    if canal:
        vq += " AND c.slug = ?"
        sq += " AND c.slug = ?"
        params = [canal]
    for q in (vq, sq):
        for row in conn.execute(q, params):
            items.append(dict(row))
    conn.close()
    items.sort(key=lambda r: (r.get("ts") or ""), reverse=True)
    return items


def mark_unavailable_items() -> int:
    """Descarta ítems que YouTube ya eliminó/no tiene disponibles.

    Se ejecuta al inicio de la sesión para que no vuelvan a bloquear la cola.
    Devuelve el número de ítems marcados como skip.
    """
    conn = sqlite3.connect(str(DB_PATH))
    now = _now_local().isoformat(timespec="seconds")
    total = 0
    for table, id_col, extra in (
        ("videos", "yt_video_id",
         "status IN ('deleted_on_yt','removed') OR "
         "yt_visibility IN ('removed','unavailable','age_restricted')"),
        ("shorts", "youtube_id",
         "yt_visibility IN ('removed','unavailable','age_restricted')"),
    ):
        try:
            cur = conn.execute(
                f"UPDATE {table} SET manual_altered_content_skip = 1, "
                f"manual_altered_content_skip_reason = 'unavailable_yt', "
                f"manual_altered_content_skip_at = ? "
                f"WHERE {id_col} IS NOT NULL AND {id_col} != '' "
                f"AND COALESCE(manual_altered_content_done,0) = 0 "
                f"AND COALESCE(manual_altered_content_skip,0) = 0 AND ({extra})",
                (now,),
            )
            total += cur.rowcount or 0
        except sqlite3.OperationalError as exc:
            logger.warning("mark_unavailable_items(%s) failed: %s", table, exc)
    conn.commit()
    conn.close()
    return total



def mark_in_db(kind: str, db_id: int, yt_id: str) -> None:
    conn = sqlite3.connect(str(DB_PATH))
    if kind == "video":
        conn.execute(
            "UPDATE videos SET manual_altered_content_done = 1 "
            "WHERE id = ? AND yt_video_id = ?", (db_id, yt_id))
    else:
        conn.execute(
            "UPDATE shorts SET manual_altered_content_done = 1 "
            "WHERE id = ? AND youtube_id = ?", (db_id, yt_id))
    conn.commit()
    conn.close()


def still_pending(kind: str, db_id: int) -> bool:
    conn = sqlite3.connect(str(DB_PATH))
    if kind == "video":
        row = conn.execute("SELECT manual_altered_content_done FROM videos WHERE id=?",
                           (db_id,)).fetchone()
    else:
        row = conn.execute("SELECT manual_altered_content_done FROM shorts WHERE id=?",
                           (db_id,)).fetchone()
    conn.close()
    return bool(row) and row[0] == 0


def bump_attempt(kind: str, db_id: int) -> int:
    """Incrementa el contador de intentos fallidos y devuelve el nuevo valor."""
    table = "videos" if kind == "video" else "shorts"
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute(
        f"UPDATE {table} SET manual_altered_content_attempts = "
        f"COALESCE(manual_altered_content_attempts, 0) + 1 WHERE id = ?",
        (db_id,),
    )
    row = conn.execute(
        f"SELECT COALESCE(manual_altered_content_attempts, 0) FROM {table} WHERE id = ?",
        (db_id,),
    ).fetchone()
    conn.commit()
    conn.close()
    return int(row[0]) if row else 0


def mark_skip(kind: str, db_id: int, reason: str) -> None:
    """Descarta un ítem como no marcable, con motivo auditable."""
    table = "videos" if kind == "video" else "shorts"
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute(
        f"UPDATE {table} SET manual_altered_content_skip = 1, "
        f"manual_altered_content_skip_reason = ?, manual_altered_content_skip_at = ? "
        f"WHERE id = ?",
        (reason, _now_local().isoformat(timespec="seconds"), db_id),
    )
    conn.commit()
    conn.close()



# ── Estado / alertas ────────────────────────────────────────────────

def _db():
    from database.db_extended import ExtendedDatabase
    return ExtendedDatabase()


def set_state(db, key: str, value: str) -> None:
    try:
        db.set_system_state(key, value)
    except Exception as exc:  # noqa: BLE001
        logger.warning("set_system_state(%s) failed: %s", key, exc)


def _channel_id(db, slug: str) -> int | None:
    try:
        ch = db.get_channel_by_slug(slug)
        return ch["id"] if ch else None
    except Exception:
        return None


def alert(db, alert_type: str, severity: str, title: str, message: str,
          metadata: dict | None = None, channel_id: int | None = None) -> None:
    try:
        # ``create_alert`` deduplica por (entity_type, entity_id, alert_type):
        # una alerta info previa sin resolver "traga" las siguientes. Resolvemos
        # las previas del mismo tipo antes de emitir una nueva.
        try:
            with db._connect() as conn:
                conn.execute(
                    "UPDATE pipeline_alerts SET resolved = 1, "
                    "resolved_at = datetime('now'), acknowledged = 1 "
                    "WHERE alert_type = ? AND resolved = 0",
                    (alert_type,),
                )
                conn.commit()
        except Exception as exc:  # noqa: BLE001
            logger.debug("resolve previous %s failed: %s", alert_type, exc)
        from api.services.lifecycle_monitor import emit_alert
        emit_alert(db, entity_type="channel" if channel_id else "system",
                   entity_id=channel_id or 0, channel_id=channel_id,
                   alert_type=alert_type, severity=severity, title=title,
                   message=message, metadata=metadata or {})
    except Exception as exc:  # noqa: BLE001
        logger.warning("alert %s failed: %s", alert_type, exc)


def resolve_backfill_aborted(db, canal: str, channel_id: int | None = None) -> int:
    """Cierra la alerta crítica de backfill abortado de este canal.

    Se llama cuando el canal termina su tanda sin fallos consecutivos: la
    condición que originó la alerta ya no existe y no debe quedar abierta.
    """
    if channel_id is None:
        channel_id = _channel_id(db, canal)
    try:
        with db._connect() as conn:
            if channel_id:
                cur = conn.execute(
                    """UPDATE pipeline_alerts
                       SET resolved = 1, resolved_at = datetime('now'), acknowledged = 1,
                           message = COALESCE(message, '') ||
                             ' [Auto-resuelto: backfill del canal retomado]'
                       WHERE alert_type = 'ia_backfill_aborted' AND resolved = 0
                         AND (channel_id = ? OR entity_id = ?)""",
                    (channel_id, channel_id),
                )
            else:
                cur = conn.execute(
                    """UPDATE pipeline_alerts
                       SET resolved = 1, resolved_at = datetime('now'), acknowledged = 1,
                           message = COALESCE(message, '') ||
                             ' [Auto-resuelto: backfill del canal retomado]'
                       WHERE alert_type = 'ia_backfill_aborted' AND resolved = 0
                         AND metadata_json LIKE ?""",
                    (f'%"canal": "{canal}"%',),
                )
            conn.commit()
            return cur.rowcount
    except Exception as exc:  # noqa: BLE001
        logger.warning("resolve_backfill_aborted(%s) failed: %s", canal, exc)
        return 0


# ── Sesión ──────────────────────────────────────────────────────────

def process_channel(db, canal: str, args, total_done: int) -> tuple[int, bool]:
    """Marca los pendientes de un canal. Returns (done, finished_channel).

    Un fallo de ítem ya NO aborta la campaña: se cuenta el intento y, tras
    ``max_attempts``, se descarta con motivo (skip) para no bloquear la cola.
    Solo se aborta la sesión si hay fallos *técnicos* consecutivos (sesión
    rota / login perdido), que no deben blacklistear ítems.
    """
    from pipeline.youtube_browser import get_browser, get_account_for_channel

    pending = get_pending(canal)
    if not pending:
        logger.info("[%s] Sin pendientes", canal)
        return total_done, True

    account = get_account_for_channel(canal)
    if not account:
        logger.warning("[%s] Sin cuenta Google mapeada — se omite", canal)
        return total_done, True

    logger.info("[%s] %d pendientes (cuenta %s)", canal, len(pending), account)
    browser = get_browser(account)
    failures = 0
    skipped = 0
    consec_technical = 0
    batch_size = random.randint(3, 6)
    batch_count = 0

    for idx, item in enumerate(pending, 1):
        now = _now_local()
        if not args.no_window and not in_window(now, args.window_start, args.window_end):
            logger.info("[%s] Fuera de ventana (%s) — se para la sesión", canal, now.strftime("%H:%M"))
            return total_done, False
        if args.max_items and total_done >= args.max_items:
            return total_done, False
        if not still_pending(item["kind"], item["db_id"]):
            continue

        logger.info("[%s %d/%d] Marcando %s:%s", canal, idx, len(pending),
                    item["kind"], item["yt_id"])
        try:
            ok = browser.mark_altered_content(item["yt_id"])
            reason = getattr(browser, "last_mark_reason", "") or "unknown"
        except Exception as exc:  # noqa: BLE001
            logger.warning("[%s] mark error: %s", canal, exc)
            ok, reason = False, "exception"

        if ok:
            mark_in_db(item["kind"], item["db_id"], item["yt_id"])
            total_done += 1
            consec_technical = 0
            set_state(db, STATE_DONE_TOTAL, str(total_done))
            set_state(db, STATE_LAST_PROGRESS, _now_local().isoformat(timespec="seconds"))
            # La sesión vuelve a funcionar: cerrar el abort previo de este canal.
            resolve_backfill_aborted(db, canal)
        else:
            failures += 1
            logger.warning("[%s] FALLO %s (%s)", canal, item["yt_id"], reason)
            if reason in TECHNICAL_REASONS:
                consec_technical += 1
                if consec_technical >= args.max_consecutive_technical_failures:
                    logger.error(
                        "[%s] %d fallos técnicos seguidos (%s) — se aborta el día",
                        canal, consec_technical, reason)
                    alert(db, "ia_backfill_aborted", "critical",
                          f"Backfill IA abortado ({canal})",
                          f"{consec_technical} fallos técnicos consecutivos ({reason}). "
                          f"Revisar sesiones de navegador "
                          f"(python3 scripts/yt_browser_login.py).",
                          {"canal": canal, "failures": failures, "reason": reason},
                          channel_id=_channel_id(db, canal))
                    return total_done, False
            else:
                consec_technical = 0
                attempts = bump_attempt(item["kind"], item["db_id"])
                if attempts >= args.max_attempts:
                    mark_skip(item["kind"], item["db_id"], reason)
                    skipped += 1
                    logger.info("[%s] SKIP %s tras %d intentos (%s)",
                                canal, item["yt_id"], attempts, reason)
                else:
                    logger.info("[%s] reintento %d/%d para %s",
                                canal, attempts, args.max_attempts, item["yt_id"])

        # Pacing human-like
        batch_count += 1
        if batch_count >= batch_size and idx < len(pending):
            pause = random.randint(args.long_pause_min, args.long_pause_max)
            logger.info("Pausa larga de %ss (fin de lote)", pause)
            time.sleep(pause)
            batch_count = 0
            batch_size = random.randint(3, 6)
        elif idx < len(pending):
            time.sleep(random.randint(args.delay_min, args.delay_max))

    if skipped:
        logger.info("[%s] Sesión: %d descartados (skip), %d fallos", canal, skipped, failures)
    set_state(db, STATE_LAST_RUN, _now_local().isoformat(timespec="seconds"))
    return total_done, True


def main() -> int:
    parser = argparse.ArgumentParser(description="Backfill paulatino del marcado IA")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--canal", help="Solo este canal")
    parser.add_argument("--no-window", action="store_true",
                        help="Ignora la ventana diurna (uso manual)")
    parser.add_argument("--window-start", type=int, default=9)
    parser.add_argument("--window-end", type=int, default=23)
    parser.add_argument("--max-items", type=int, default=0,
                        help="0 = sin límite (solo para de noche)")
    parser.add_argument("--delay-min", type=int, default=30)
    parser.add_argument("--delay-max", type=int, default=120)
    parser.add_argument("--long-pause-min", type=int, default=900)
    parser.add_argument("--long-pause-max", type=int, default=2400)
    parser.add_argument("--max-consecutive-failures", type=int, default=5,
                        help="(legacy) fallos técnicos consecutivos antes de abortar")
    parser.add_argument("--max-consecutive-technical-failures", type=int, default=None,
                        help="Fallos técnicos consecutivos antes de abortar la sesión")
    parser.add_argument("--max-attempts", type=int, default=3,
                        help="Intentos fallidos por ítem antes de descartarlo (skip)")
    args = parser.parse_args()
    if args.max_consecutive_technical_failures is None:
        args.max_consecutive_technical_failures = args.max_consecutive_failures

    db = _db()
    pending = get_pending(args.canal)
    total_pending = len(pending)

    print(f"\n{'='*60}\nPendientes de marcado IA: {total_pending}")
    if args.dry_run:
        print("DRY RUN — sin cambios")
        by = {}
        for it in pending:
            by[it["canal"]] = by.get(it["canal"], 0) + 1
        for ch, n in by.items():
            print(f"  {ch}: {n}")
        print(f"{'='*60}\n")
        return 0
    print(f"{'='*60}\n")

    # Descartar ítems que YouTube ya eliminó/no tiene disponibles: no deben
    # bloquear la cola ni abortar la sesión.
    unavailable = mark_unavailable_items()
    if unavailable:
        logger.info("Descartados %d ítems no disponibles en YouTube (skip)", unavailable)
        pending = get_pending(args.canal)
        total_pending = len(pending)
    set_state(db, STATE_PENDING, str(total_pending))

    if total_pending == 0:
        set_state(db, STATE_FINISHED_AT, _now_local().isoformat(timespec="seconds"))
        alert(db, "ia_backfill_complete", "critical",
              "✅ Backfill marcado IA COMPLETADO",
              "No quedan vídeos/shorts sin marcar como contenido alterado/IA.",
              {"pending": 0})
        return 0

    if not args.no_window and not in_window(_now_local(), args.window_start, args.window_end):
        logger.info("Fuera de ventana diurna — no se inicia sesión.")
        return 0

    if args.canal:
        channels = [args.canal]
    else:
        current = db.get_system_state(STATE_CURRENT)
        channels = channel_sequence(current)
    total_done = int(db.get_system_state(STATE_DONE_TOTAL) or 0)

    for canal in channels:
        set_state(db, STATE_CURRENT, canal)
        total_done, finished = process_channel(db, canal, args, total_done)
        if finished:
            resolve_backfill_aborted(db, canal, _channel_id(db, canal))
            alert(db, "ia_backfill_channel_done", "critical",
                  f"✅ Marcado IA completado en {canal}",
                  f"Canal {canal} ya no tiene pendientes de marcado IA.",
                  {"canal": canal, "done_total": total_done},
                  channel_id=_channel_id(db, canal))
        else:
            # Parada por noche/límite/fallos → se retoma aquí mañana
            return 0

    # Run acotado a un canal: no tocar el estado global ni declarar fin.
    if args.canal:
        return 0

    # Solo declarar COMPLETADO si de verdad no queda ningún pendiente global.
    remaining = len(get_pending())
    if remaining > 0:
        set_state(db, STATE_CURRENT, channels[-1])
        logger.info("Ciclo de canales terminado; quedan %d pendientes globales", remaining)
        return 0

    # Todos los canales terminados
    set_state(db, STATE_CURRENT, "")
    set_state(db, STATE_PENDING, "0")
    set_state(db, STATE_FINISHED_AT, _now_local().isoformat(timespec="seconds"))
    try:
        with db._connect() as conn:
            conn.execute(
                """UPDATE pipeline_alerts
                   SET resolved = 1, resolved_at = datetime('now'), acknowledged = 1,
                       message = COALESCE(message, '') ||
                         ' [Auto-resuelto: backfill IA completado]'
                   WHERE alert_type = 'ia_backfill_aborted' AND resolved = 0"""
            )
            conn.commit()
    except Exception as exc:  # noqa: BLE001
        logger.warning("resolve all backfill aborted failed: %s", exc)
    alert(db, "ia_backfill_complete", "critical",
          "✅ Backfill marcado IA COMPLETADO",
          f"Todos los canales al día. Total marcado en esta campaña: {total_done}.",
          {"done_total": total_done})
    logger.info("Backfill COMPLETADO. Total marcado: %d", total_done)
    return 0


if __name__ == "__main__":
    from pipeline.youtube_browser import close_all_browsers  # noqa: E402
    try:
        raise SystemExit(main())
    finally:
        try:
            close_all_browsers()
        except Exception:
            pass
