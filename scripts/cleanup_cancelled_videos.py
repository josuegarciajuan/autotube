#!/usr/bin/env python3
"""One-off cleanup: delete videos with status='cancelled' from the videos table.

Cancelled videos are slots/drafts that never finished generation and were
never uploaded to YouTube (no yt_video_id). They clutter the channel video
listings. This script removes them (and their child rows via ON DELETE CASCADE).

Safe to run with the API up — status='cancelled' means no worker is actively
processing them.

Usage:
    python3 scripts/cleanup_cancelled_videos.py             # dry-run (preview)
    python3 scripts/cleanup_cancelled_videos.py --apply     # actually delete
    python3 scripts/cleanup_cancelled_videos.py --canal canal3 --apply  # one channel
"""

import argparse
import logging
import os
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger("cleanup_cancelled_videos")


def get_db():
    from database.db_extended import ExtendedDatabase, migrate_v2
    migrate_v2()
    return ExtendedDatabase()


def _cancelled_by_channel(conn):
    """Return {channel_slug: count} and total for status='cancelled'."""
    rows = conn.execute(
        """SELECT COALESCE(canal, '?') AS slug, COUNT(*) AS cnt
           FROM videos WHERE status = 'cancelled' GROUP BY canal ORDER BY cnt DESC"""
    ).fetchall()
    return {r["slug"]: r["cnt"] for r in rows}


def _cancelled_ids(conn, canal=None):
    q = "SELECT id FROM videos WHERE status = 'cancelled'"
    params = []
    if canal:
        q += " AND canal = ?"
        params.append(canal)
    rows = conn.execute(q, params).fetchall()
    return [r["id"] for r in rows]


def _total(by_channel):
    return sum(by_channel.values())


def run(canal: str | None, apply: bool) -> int:
    from database.db_extended import ExtendedDatabase
    db = get_db()

    with db._connect() as conn:
        before = _cancelled_by_channel(conn)
        total_before = _total(before)
        ids = _cancelled_ids(conn, canal)

        print("\n=== CANCELLED VIDEOS ===")
        if not before:
            print("  No cancelled videos found.")
            return 0
        for slug, cnt in before.items():
            print(f"  {slug:20} {cnt}")
        print(f"  {'TOTAL':20} {total_before}")

        if not apply:
            print(f"\n[dry-run] Se eliminarían {len(ids)} vídeos con estado 'cancelled'.")
            print("  Ejecuta con --apply para borrarlos de verdad.")
            return 0

        # Delete in one statement; FK ON DELETE CASCADE handles child rows.
        placeholders = ",".join("?" for _ in ids)
        cur = conn.execute(f"DELETE FROM videos WHERE id IN ({placeholders})", ids)
        conn.commit()
        deleted = cur.rowcount

        after = _cancelled_by_channel(conn)
        total_after = _total(after)

        print(f"\n[apply] Eliminados {deleted} vídeos 'cancelled'.")
        print(f"  Restantes: {total_after}")
        return deleted


def main():
    parser = argparse.ArgumentParser(description="Delete cancelled videos from DB.")
    parser.add_argument("--apply", action="store_true", help="Actually delete (default is dry-run).")
    parser.add_argument("--canal", type=str, default=None, help="Optional channel slug to filter.")
    args = parser.parse_args()

    deleted = run(args.canal, args.apply)
    if args.apply and deleted:
        logger.info("Done. Deleted %d cancelled videos.", deleted)


if __name__ == "__main__":
    main()
