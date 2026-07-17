#!/usr/bin/env python3
"""
Batch script: mark all unmarked videos + shorts as AI-generated content.

Iterates all channels, finds videos/shorts where manual_altered_content_done = 0,
marks them via browser automation with human-like delays.

Usage:
    python3 scripts/yt_mark_all_ia.py              # all channels, all pending
    python3 scripts/yt_mark_all_ia.py --dry-run    # show what would be done
    python3 scripts/yt_mark_all_ia.py --canal canal2  # single channel

Idempotent: re-checks DB before each marking to avoid race conditions.
"""

import argparse
import logging
import random
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
DB_PATH = PROJECT_ROOT / "autotube.db"

CHANNEL_ACCOUNT_MAP = {
    "canal2": "tracatrack",
    "canal3": "tracatrack",
    "canal4": "burrianacasa2026",
    "canal5": "burrianacasa2026",
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("yt_mark_ia")


def get_pending(db_path: Path, canal: str = None) -> list:
    """Return list of (yt_video_id, canal, source_type) for unmarked items."""
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    pending = []

    # Videos
    query = """
        SELECT yt_video_id, canal, 'video' as source_type, id as db_id
        FROM videos
        WHERE yt_video_id IS NOT NULL AND yt_video_id != ''
        AND manual_altered_content_done = 0
    """
    params = []
    if canal:
        query += " AND canal = ?"
        params.append(canal)
    for row in conn.execute(query, params):
        pending.append(dict(row))

    # Shorts
    query = """
        SELECT s.youtube_id as yt_video_id, c.slug as canal, 'short' as source_type, s.id as db_id
        FROM shorts s JOIN channels c ON s.channel_id = c.id
        WHERE s.youtube_id IS NOT NULL AND s.youtube_id != ''
        AND s.manual_altered_content_done = 0
    """
    params = []
    if canal:
        query += " AND c.slug = ?"
        params.append(canal)
    for row in conn.execute(query, params):
        pending.append(dict(row))

    conn.close()
    return pending


def mark_in_db(db_path: Path, source_type: str, db_id: int, yt_video_id: str):
    """Update DB after successful marking."""
    conn = sqlite3.connect(str(db_path))
    if source_type == "video":
        conn.execute(
            "UPDATE videos SET manual_altered_content_done = 1 WHERE id = ? AND yt_video_id = ?",
            (db_id, yt_video_id),
        )
    elif source_type == "short":
        conn.execute(
            "UPDATE shorts SET manual_altered_content_done = 1 WHERE id = ? AND youtube_id = ?",
            (db_id, yt_video_id),
        )
    conn.commit()
    conn.close()


def is_still_pending(db_path: Path, source_type: str, db_id: int) -> bool:
    """Re-check DB before marking to avoid race conditions."""
    conn = sqlite3.connect(str(db_path))
    if source_type == "video":
        row = conn.execute(
            "SELECT manual_altered_content_done FROM videos WHERE id = ?", (db_id,)
        ).fetchone()
    elif source_type == "short":
        row = conn.execute(
            "SELECT manual_altered_content_done FROM shorts WHERE id = ?", (db_id,)
        ).fetchone()
    conn.close()
    return row and row[0] == 0


def main():
    parser = argparse.ArgumentParser(description="Batch mark videos as AI-generated")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be done")
    parser.add_argument("--canal", help="Only process this channel")
    parser.add_argument("--delay-min", type=int, default=30,
                        help="Min seconds between videos (default: 30)")
    parser.add_argument("--delay-max", type=int, default=120,
                        help="Max seconds between videos (default: 120)")
    args = parser.parse_args()

    pending = get_pending(DB_PATH, args.canal)

    if not pending:
        print("No pending videos/shorts to mark.")
        return

    print(f"\n{'='*60}")
    print(f"Pending items to mark: {len(pending)}")
    if args.dry_run:
        print("DRY RUN — no changes will be made")
    print(f"{'='*60}\n")

    # Group by account for efficient browser usage
    by_account = {}
    for item in pending:
        canal = item["canal"]
        account = CHANNEL_ACCOUNT_MAP.get(canal)
        if not account:
            logger.warning(f"No account mapped for canal {canal}, skipping")
            continue
        by_account.setdefault(account, []).append(item)

    total_done = 0
    total_skipped = 0
    total_failed = 0
    total_total = sum(len(v) for v in by_account.values())

    if args.dry_run:
        for account, items in by_account.items():
            for item in items:
                print(f"  [{item['canal']}] {item['source_type']}: {item['yt_video_id']}")
        print(f"\n{total_total} items would be processed.")
        return

    from pipeline.youtube_browser import get_browser

    for account, items in by_account.items():
        logger.info(f"Processing {len(items)} items for account: {account}")
        browser = get_browser(account)

        for i, item in enumerate(items, 1):
            yt_id = item["yt_video_id"]
            canal = item["canal"]
            source_type = item["source_type"]
            db_id = item["db_id"]

            # Re-check — may have been marked by pipeline hook or another run
            if not is_still_pending(DB_PATH, source_type, db_id):
                logger.info(f"[{i}/{len(items)}] SKIP {canal}/{source_type}:{yt_id} — already marked in DB")
                total_skipped += 1
                continue

            logger.info(f"[{total_done + 1}/{total_total}] Marking {canal}/{source_type}:{yt_id}")
            success = browser.mark_altered_content(yt_id)

            if success:
                mark_in_db(DB_PATH, source_type, db_id, yt_id)
                total_done += 1
                logger.info(f"[{total_done}/{total_total}] DONE {canal}:{yt_id}")
            else:
                total_failed += 1
                logger.warning(f"[{total_done}/{total_total}] FAILED {canal}:{yt_id}")

            # Human-like delay between videos (skip on last)
            if i < len(items):
                delay = random.randint(args.delay_min, args.delay_max)
                logger.info(f"Waiting {delay}s before next video...")
                time.sleep(delay)

    print(f"\n{'='*60}")
    print(f"COMPLETE: {total_done} marked, {total_skipped} skipped, {total_failed} failed")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
