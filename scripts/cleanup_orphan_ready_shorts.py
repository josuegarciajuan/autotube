#!/usr/bin/env python3
"""One-off cleanup: clip shorts stuck in status='ready' WITHOUT any
shorts_planned_slots entry (or only cancelled/failed slots).

These are pre-rendered clips whose upload trigger (their planned slot) was
cancelled/dropped — e.g. by the timezone bugs in shorts_scheduler that treated
UTC scheduled_at as local time and cancelled/discarded slots prematurely.
A 'ready' short with no pending slot is never uploaded, stays forever in the
'Pendiente subida' column, wastes disk, and blocks clip-source dedup for its
long video (_resolve_clip_source counts 'ready' as already-used).

The timezone + memory fixes in shorts_scheduler prevent NEW orphans; this
script cleans the existing backlog.

Usage:
    python3 scripts/cleanup_orphan_ready_shorts.py              # dry-run (preview)
    python3 scripts/cleanup_orphan_ready_shorts.py --apply      # mark cancelled in DB
    python3 scripts/cleanup_orphan_ready_shorts.py --apply --clean-files  # also delete files
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
logger = logging.getLogger("cleanup_orphan_ready_shorts")


def get_db():
    from database.db_extended import ExtendedDatabase, migrate_v2
    migrate_v2()
    return ExtendedDatabase()


def _orphan_ready_shorts(conn):
    """Return clip shorts with status='ready' that have NO slot linking them.

    A slot links a short via shorts_planned_slots.short_id. Orphans are:
      - no slot at all (short_id never set / slot deleted), OR
      - only cancelled/failed slots (upload trigger gone).
    """
    rows = conn.execute(
        """SELECT s.id, s.channel_id, c.slug AS channel_slug, s.source_video_id,
                  s.file_path, s.created_at,
                  SUM(CASE WHEN sps.status IN ('pending','running','completed')
                           THEN 1 ELSE 0 END) AS live_slots
           FROM shorts s
           LEFT JOIN channels c ON c.id = s.channel_id
           LEFT JOIN shorts_planned_slots sps ON sps.short_id = s.id
           WHERE s.status = 'ready'
             AND s.type = 'clip'
           GROUP BY s.id
           HAVING live_slots IS NULL OR live_slots = 0
           ORDER BY s.created_at"""
    ).fetchall()
    return rows


def _resolve(path):
    """Resolve a possibly-relative file path against the project root."""
    if not path:
        return None
    p = Path(path)
    if p.is_absolute():
        return p
    for base in (Path.cwd(), _PROJECT_ROOT):
        cand = base / p
        if cand.exists():
            return cand
    return _PROJECT_ROOT / p


def _total_size_bytes(paths):
    total = 0
    for p in paths:
        try:
            resolved = _resolve(p)
            if resolved and resolved.exists():
                total += resolved.stat().st_size
        except OSError:
            pass
    return total


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true",
                        help="Actually mark orphans as cancelled (default: dry-run)")
    parser.add_argument("--clean-files", action="store_true",
                        help="Also delete the rendered video files (only with --apply)")
    args = parser.parse_args()

    db = get_db()
    conn = db._connect()
    conn.row_factory = __import__("sqlite3").Row

    orphans = _orphan_ready_shorts(conn)
    if not orphans:
        logger.info("No orphaned 'ready' clip shorts found — nothing to do.")
        conn.close()
        return

    by_channel = {}
    for r in orphans:
        by_channel[r["channel_slug"] or "?"] = by_channel.get(r["channel_slug"] or "?", 0) + 1
    total_mb = _total_size_bytes([r["file_path"] for r in orphans]) / (1024 * 1024)

    logger.info("Found %d orphaned 'ready' clip shorts (~%.1f MB on disk):", len(orphans), total_mb)
    for slug, cnt in sorted(by_channel.items()):
        logger.info("  %-12s %d", slug, cnt)

    if not args.apply:
        logger.info("Dry-run — pass --apply to mark them 'cancelled' in the DB.")
        conn.close()
        return

    # ── Apply: mark cancelled ──
    cancelled = 0
    for r in orphans:
        conn.execute(
            "UPDATE shorts SET status = 'cancelled', updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (r["id"],),
        )
        cancelled += 1
        if args.clean_files and r["file_path"]:
            p = _resolve(r["file_path"])
            try:
                if p and p.exists():
                    p.unlink()
            except OSError as e:
                logger.warning("  could not delete %s: %s", p, e)
    conn.commit()

    logger.info("Marked %d orphaned shorts as cancelled.", cancelled)
    if args.clean_files:
        logger.info("Deleted rendered files for those shorts (where present).")
    logger.info("The fix in shorts_scheduler (UTC comparisons + memory gate) prevents new orphans.")
    conn.close()


if __name__ == "__main__":
    main()
