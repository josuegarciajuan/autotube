#!/usr/bin/env python3
"""Prune del backlog de shorts — drenaje controlado (anti-spam / anti-churn).

Marca como 'cancelled' los slots de shorts que ya no deben reintentarse, para
que el recovery/scheduler no los re-encuele en bucle:

  1. `shorts_planned_slots` en 'pending' con scheduled_at > 24h en el pasado
     (obsoletos; el propio scheduler ya los cancelaría, esto lo acelera).
  2. Slots 'pending' de tipo 'clip' cuyo canal no tiene vídeo largo completado
     hoy (causa determinista de fallo → no reintentar).
  3. (Opcional, --failed) `generation_jobs` de shorts 'failed' con error de
     "no source"/"exhausted retries" más antiguos de N días → 'cancelled'
     (solo limpieza de reporting; no afecta a la cola).

Uso:
  python3 scripts/prune_shorts_backlog.py            # dry-run (solo informa)
  python3 scripts/prune_shorts_backlog.py --apply    # aplica los cambios
  python3 scripts/prune_shorts_backlog.py --failed   # incluye limpieza de failed
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.settings import DATABASE_PATH  # noqa: E402


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DATABASE_PATH), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def prune_pending_overdue(conn: sqlite3.Connection, apply: bool) -> int:
    rows = conn.execute(
        """SELECT id, channel_id, short_type, scheduled_at
           FROM shorts_planned_slots
           WHERE status = 'pending'
             AND scheduled_at < datetime('now', '-24 hours')"""
    ).fetchall()
    if apply and rows:
        conn.execute(
            """UPDATE shorts_planned_slots
               SET status = 'cancelled',
                   error_message = 'prune: pending >24h (backlog drain)',
                   updated_at = CURRENT_TIMESTAMP
               WHERE status = 'pending'
                 AND scheduled_at < datetime('now', '-24 hours')"""
        )
        conn.commit()
    return len(rows)


def prune_clip_without_source(conn: sqlite3.Connection, apply: bool) -> int:
    rows = conn.execute(
        """SELECT sps.id, sps.channel_id
           FROM shorts_planned_slots sps
           WHERE sps.status = 'pending'
             AND sps.short_type = 'clip'
             AND NOT EXISTS (
                SELECT 1 FROM videos v
                WHERE v.channel_id = sps.channel_id
                  AND COALESCE(date(v.uploaded_at), date(v.created_at))
                      IN (date('now','localtime'), date('now','-1 day','localtime'))
                  AND v.status IN ('uploaded','uploaded_private','published')
             )"""
    ).fetchall()
    if apply and rows:
        conn.execute(
            """UPDATE shorts_planned_slots
               SET status = 'cancelled',
                   error_message = 'prune: clip sin fuente (backlog drain)',
                   updated_at = CURRENT_TIMESTAMP
               WHERE id IN (
                   SELECT sps.id FROM shorts_planned_slots sps
                   WHERE sps.status = 'pending'
                     AND sps.short_type = 'clip'
                     AND NOT EXISTS (
                        SELECT 1 FROM videos v
                        WHERE v.channel_id = sps.channel_id
                          AND COALESCE(date(v.uploaded_at), date(v.created_at))
                              IN (date('now','localtime'), date('now','-1 day','localtime'))
                          AND v.status IN ('uploaded','uploaded_private','published')
                     )
               )"""
        )
        conn.commit()
    return len(rows)


def prune_failed_jobs(conn: sqlite3.Connection, apply: bool, days: int) -> int:
    rows = conn.execute(
        """SELECT id FROM generation_jobs
           WHERE action IN ('generate_clip_short', 'generate_native_short')
             AND status = 'failed'
             AND created_at < datetime('now', ?)""",
        (f"-{days} days",),
    ).fetchall()
    if apply and rows:
        conn.execute(
            """UPDATE generation_jobs SET status = 'cancelled'
               WHERE action IN ('generate_clip_short', 'generate_native_short')
                 AND status = 'failed'
                 AND created_at < datetime('now', ?)""",
            (f"-{days} days",),
        )
        conn.commit()
    return len(rows)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="Aplica los cambios (sin flag = dry-run)")
    parser.add_argument("--failed", action="store_true", help="Incluye limpieza de generation_jobs failed")
    parser.add_argument("--days", type=int, default=7, help="Antigüedad para failed (default 7)")
    args = parser.parse_args()

    conn = _connect()
    mode = "APPLY" if args.apply else "DRY-RUN"

    n1 = prune_pending_overdue(conn, args.apply)
    n2 = prune_clip_without_source(conn, args.apply)
    print(f"[{mode}] pending >24h → cancelar: {n1}")
    print(f"[{mode}] clip sin fuente → cancelar: {n2}")

    if args.failed:
        n3 = prune_failed_jobs(conn, args.apply, args.days)
        print(f"[{mode}] generation_jobs failed (> {args.days}d) → cancelled: {n3}")

    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
