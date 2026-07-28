#!/usr/bin/env python3
"""
Test: link a long-form video as "Related video" on a YouTube Short.

YouTube Studio browser automation test — uses the same Playwright + Xvfb
infrastructure as the production pipeline. Navigates to YouTube Studio,
finds the Short's edit page, and sets the "Video relacionado" link.

Usage:
    # Link by YouTube video IDs:
    python3 scripts/yt_test_link_short.py \\
        --short-id SHORT_YT_ID \\
        --long-id LONGFORM_YT_ID \\
        --account ACCOUNT_NAME

    # Link by DB short ID (resolves YouTube IDs automatically):
    python3 scripts/yt_test_link_short.py \\
        --db-short-id 42 \\
        --account ACCOUNT_NAME

Examples:
    python3 scripts/yt_test_link_short.py \\
        --short-id abc123defgh \\
        --long-id xyz789abcde \\
        --account tracatrack

    python3 scripts/yt_test_link_short.py \\
        --db-short-id 42 \\
        --account tracatrack
"""

import argparse
import logging
import sys
from pathlib import Path

# ── Python path setup ─────────────────────────────────────────
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger("yt_test_link_short")


def main():
    parser = argparse.ArgumentParser(
        description="Test: link long-form video to a Short in YouTube Studio"
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--short-id", help="YouTube video ID of the Short")
    group.add_argument("--db-short-id", type=int, help="Database ID of the short (resolves YT IDs)")

    parser.add_argument("--long-id", help="YouTube video ID of the long-form video")
    parser.add_argument("--account", required=True, help="Browser account name (e.g. tracatrack)")

    args = parser.parse_args()

    # ── Resolve IDs ────────────────────────────────────────────
    short_yt_id = args.short_id
    longform_yt_id = args.long_id

    if args.db_short_id:
        import sqlite3
        from config.settings import DATABASE_PATH
        conn = sqlite3.connect(str(DATABASE_PATH), timeout=10)
        conn.row_factory = sqlite3.Row

        short_row = conn.execute(
            """SELECT s.*, c.slug as channel_slug
               FROM shorts s
               JOIN channels c ON s.channel_id = c.id
               WHERE s.id = ?""",
            (args.db_short_id,),
        ).fetchone()

        if not short_row:
            print(f"ERROR: Short #{args.db_short_id} not found in database")
            sys.exit(1)

        short_row = dict(short_row)
        short_yt_id = short_row.get("youtube_id")
        if not short_yt_id:
            print(f"ERROR: Short #{args.db_short_id} has no youtube_id (not published?)")
            sys.exit(1)

        source_video_id = short_row.get("source_video_id")
        if not source_video_id:
            print(f"ERROR: Short #{args.db_short_id} has no source_video_id (not a clip?)")
            sys.exit(1)

        video_row = conn.execute(
            "SELECT yt_video_id FROM videos WHERE id = ? AND yt_video_id IS NOT NULL AND yt_video_id != ''",
            (source_video_id,),
        ).fetchone()

        if not video_row or not video_row["yt_video_id"]:
            print(f"ERROR: Source video #{source_video_id} has no YouTube ID")
            sys.exit(1)

        longform_yt_id = video_row["yt_video_id"]
        conn.close()

        print(f"Resolved from DB:")
        print(f"  Short YT ID:      {short_yt_id}")
        print(f"  Long-form YT ID:  {longform_yt_id}")
        print(f"  Channel slug:     {short_row.get('channel_slug')}")
        print(f"  Short status:     {short_row.get('status')}")
        print(f"  Already linked:   {short_row.get('longform_linked')}")
        print()

    elif not args.long_id:
        parser.error("--long-id is required when using --short-id")
        sys.exit(1)

    if not short_yt_id or not longform_yt_id:
        print("ERROR: Both short and long-form YouTube IDs are required")
        sys.exit(1)

    # ── Validate format ────────────────────────────────────────
    if len(short_yt_id) != 11 or len(longform_yt_id) != 11:
        print("WARNING: YouTube video IDs should be exactly 11 characters")
        print(f"  Short:  '{short_yt_id}' ({len(short_yt_id)} chars)")
        print(f"  Long:   '{longform_yt_id}' ({len(longform_yt_id)} chars)")

    # ── Run the linking ────────────────────────────────────────
    print(f"Linking long-form video to Short...")
    print(f"  Short URL:  https://www.youtube.com/shorts/{short_yt_id}")
    print(f"  Long URL:   https://www.youtube.com/watch?v={longform_yt_id}")
    print(f"  Account:    {args.account}")
    print()

    from pipeline.youtube_browser import get_browser

    try:
        browser = get_browser(args.account)
        success = browser.link_longform_video(short_yt_id, longform_yt_id)

        if success:
            print("✅ SUCCESS: Long-form video linked to the Short!")
            print("   Verify at: https://www.youtube.com/shorts/" + short_yt_id)
            # Update DB if we used --db-short-id
            if args.db_short_id:
                import sqlite3
                from config.settings import DATABASE_PATH
                conn = sqlite3.connect(str(DATABASE_PATH), timeout=10)
                conn.execute(
                    "UPDATE shorts SET longform_linked = 1, longform_linked_at = datetime('now','localtime') WHERE id = ?",
                    (args.db_short_id,),
                )
                conn.commit()
                conn.close()
                print("   Updated DB: longform_linked = 1")
        else:
            print("❌ FAILED: Could not link long-form video to the Short")
            print("   Check YouTube Studio manually and verify the selectors.")

    except FileNotFoundError as e:
        print(f"❌ ERROR: {e}")
        print(f"   Run: python3 scripts/yt_browser_login.py --account {args.account}")
        sys.exit(1)
    except Exception as e:
        print(f"❌ UNEXPECTED ERROR: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
