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
import random
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

DB_PATH = PROJECT_ROOT / "autotube.db"

CHANNEL_ORDER = ["canal2", "canal3", "canal4", "canal5"]
STATE_CURRENT = "ia_backfill_current_channel"
STATE_DONE_TOTAL = "ia_backfill_done_total"
STATE_PENDING = "ia_backfill_pending"
STATE_LAST_RUN = "ia_backfill_last_run"
STATE_FINISHED_AT = "ia_backfill_finished_at"

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


# ── DB ──────────────────────────────────────────────────────────────

def get_pending(canal: str | None = None) -> list[dict]:
    """Items sin marcar, más recientes primero (videos + shorts)."""
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    items: list[dict] = []

    vq = """
        SELECT v.yt_video_id AS yt_id, c.slug AS canal, 'video' AS kind, v.id AS db_id,
               COALESCE(v.uploaded_at, v.published_at, v.created_at) AS ts
        FROM videos v JOIN channels c ON c.id = v.channel_id
        WHERE v.yt_video_id IS NOT NULL AND v.yt_video_id != ''
          AND COALESCE(v.manual_altered_content_done, 0) = 0
    """
    sq = """
        SELECT s.youtube_id AS yt_id, c.slug AS canal, 'short' AS kind, s.id AS db_id,
               COALESCE(s.actual_published_at, s.published_at, s.created_at) AS ts
        FROM shorts s JOIN channels c ON c.id = s.channel_id
        WHERE s.youtube_id IS NOT NULL AND s.youtube_id != ''
          AND COALESCE(s.manual_altered_content_done, 0) = 0
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
    """Marca los pendientes de un canal. Returns (done, finished_channel)."""
    from pipeline.youtube_browser import get_browser, get_account_for_channel, close_all_browsers

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
    consec = 0
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
        except Exception as exc:  # noqa: BLE001
            logger.warning("[%s] mark error: %s", canal, exc)
            ok = False

        if ok:
            mark_in_db(item["kind"], item["db_id"], item["yt_id"])
            total_done += 1
            consec = 0
            set_state(db, STATE_DONE_TOTAL, str(total_done))
            # La sesión vuelve a funcionar: cerrar el abort previo de este canal.
            resolve_backfill_aborted(db, canal)
        else:
            failures += 1
            consec += 1
            logger.warning("[%s] FALLO %s (consec=%d)", canal, item["yt_id"], consec)
            if consec >= args.max_consecutive_failures:
                logger.error("[%s] %d fallos seguidos — se aborta el día", canal, consec)
                alert(db, "ia_backfill_aborted", "critical",
                      f"Backfill IA abortado ({canal})",
                      f"{consec} fallos consecutivos. Revisar sesiones de navegador "
                      f"(python3 scripts/yt_browser_login.py).",
                      {"canal": canal, "failures": failures},
                      channel_id=_channel_id(db, canal))
                return total_done, False

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
    parser.add_argument("--max-consecutive-failures", type=int, default=5)
    args = parser.parse_args()

    db = _db()
    pending = get_pending(args.canal)
    total_pending = len(pending)
    set_state(db, STATE_PENDING, str(total_pending))

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

    order = [args.canal] if args.canal else CHANNEL_ORDER
    current = db.get_system_state(STATE_CURRENT) or order[0]
    start_idx = order.index(current) if current in order else 0
    total_done = int(db.get_system_state(STATE_DONE_TOTAL) or 0)

    for canal in order[start_idx:]:
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
