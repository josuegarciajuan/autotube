#!/usr/bin/env python3
"""Backfill cross-promotion links for already-published shorts.

For every published short on the given channel, this script:
  1. Finds the best long-form video to link
  2. Builds a new description with clickable URLs
  3. Updates the YouTube video's description via the API
  4. Adds the short to the Shorts playlist
  5. (Optional) Posts a first comment with a link

Usage:
  python3 scripts/backfill_shorts_links.py --channel canal3
  python3 scripts/backfill_shorts_links.py --channel canal3 --dry-run
  python3 scripts/backfill_shorts_links.py --channel canal2 --no-comment
  python3 scripts/backfill_shorts_links.py --all  # all channels
"""

import argparse
import logging
import sqlite3
import sys
from pathlib import Path

# Ensure project root in path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.settings import DATABASE_PATH
from config.config_bridge import get_channel_config

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("backfill")


# ═══════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="Backfill short descriptions with long-form links")
    parser.add_argument("--channel", help="Channel slug (e.g. canal3)")
    parser.add_argument("--all", action="store_true", help="Process all channels")
    parser.add_argument("--dry-run", action="store_true", help="Preview only, no API calls")
    parser.add_argument("--no-comment", action="store_true", help="Skip first comment posting")
    parser.add_argument("--no-playlist", action="store_true", help="Skip playlist adding")
    args = parser.parse_args()

    if not args.channel and not args.all:
        parser.error("Must specify --channel <slug> or --all")

    conn = sqlite3.connect(str(DATABASE_PATH))
    conn.row_factory = sqlite3.Row

    # Determine channels to process
    if args.all:
        rows = conn.execute(
            "SELECT id, slug, name FROM channels WHERE active = 1"
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT id, slug, name FROM channels WHERE slug = ? AND active = 1",
            (args.channel,),
        ).fetchall()

    if not rows:
        logger.error("No active channels found")
        sys.exit(1)

    total_updated = 0
    total_skipped = 0
    total_errors = 0

    for ch_row in rows:
        channel_id = ch_row["id"]
        channel_slug = ch_row["slug"]
        channel_name = ch_row["name"]

        logger.info("=" * 60)
        logger.info("Channel: %s (%s)", channel_name, channel_slug)

        ch_config = get_channel_config(channel_slug)
        if not getattr(ch_config, "SHORTS_LONGFORM_LINK_ENABLED", True):
            logger.info("  Cross-promotion disabled in config — skipping")
            continue

        # Get all published shorts with YouTube IDs
        shorts = conn.execute(
            """SELECT s.id, s.youtube_id, s.title, s.hook_text,
                      s.youtube_url, s.source_video_id
               FROM shorts s
               WHERE s.channel_id = ?
                 AND s.status = 'published'
                 AND s.youtube_id IS NOT NULL
                 AND s.youtube_id != ''
               ORDER BY s.published_at DESC""",
            (channel_id,),
        ).fetchall()

        if not shorts:
            logger.info("  No published shorts found")
            continue

        logger.info("  Found %d published shorts", len(shorts))

        # Get best long-form URLs
        from pipeline.shorts_cross_promote import (
            get_best_longform_link,
            build_short_description,
            run_post_publish_promotion,
        )

        for short in shorts:
            yt_id = short["youtube_id"]
            title = short["title"] or "Untitled"

            longform_url = get_best_longform_link(
                channel_id,
                source_video_id=short["source_video_id"],
            )
            if not longform_url:
                logger.warning("  [%s] %s → No long-form video found, skipping",
                               yt_id, title[:50])
                total_skipped += 1
                continue

            hashtags = getattr(ch_config, "SHORTS_HASHTAGS", ["#Shorts"])
            channel_url = getattr(ch_config, "YOUTUBE_CHANNEL_URL", "")

            new_description = build_short_description(
                hook_text=short["hook_text"] or "",
                hashtags=hashtags,
                longform_url=longform_url,
                channel_url=channel_url,
            )

            if args.dry_run:
                logger.info("  [DRY RUN] %s → %s", yt_id, title[:50])
                logger.info("    Link: %s", longform_url)
                logger.info("    Description (%d chars): %s...",
                            len(new_description), new_description[:120])
                total_updated += 1
                continue

            # ── Update description on YouTube ──────────────
            try:
                from pipeline.youtube_uploader import YouTubeUploader
                uploader = YouTubeUploader(
                    account_name=channel_slug,
                    channel_slug=channel_slug,
                )
                if not uploader.authenticate():
                    logger.error("  [%s] Auth failed", yt_id)
                    total_errors += 1
                    continue

                uploader.update_description(yt_id, new_description)
                logger.info("  [%s] %s → Description updated", yt_id, title[:50])

                # ── Add to playlist ───────────────────────
                if not args.no_playlist:
                    from pipeline.youtube_playlists import YouTubePlaylistManager
                    pm = YouTubePlaylistManager(channel_slug)
                    if pm.authenticate():
                        pl_name = getattr(ch_config, "SHORTS_PLAYLIST_NAME", "Shorts")
                        existing = pm.find_playlist_by_title(pl_name)
                        if existing:
                            pm.add_video_to_playlist(existing["yt_playlist_id"], yt_id)
                            logger.info("    → Added to playlist '%s'", pl_name)
                        else:
                            created = pm.create_playlist(pl_name, "Shorts del canal")
                            if created.get("yt_playlist_id"):
                                pm.add_video_to_playlist(created["yt_playlist_id"], yt_id)
                                logger.info("    → Created playlist '%s' and added short", pl_name)

                # ── First comment ─────────────────────────
                if not args.no_comment:
                    run_post_publish_promotion(
                        channel_slug=channel_slug,
                        short_yt_id=yt_id,
                        channel_id=channel_id,
                        source_yt_id=longform_url.split("v=")[-1],
                        source_video_id=short["source_video_id"],
                        channel_config=ch_config,
                    )

                total_updated += 1

            except Exception as exc:
                logger.error("  [%s] Error: %s", yt_id, exc)
                total_errors += 1

    conn.close()

    # ── Summary ──────────────────────────────────────────
    logger.info("=" * 60)
    logger.info("Backfill complete: %d updated, %d skipped, %d errors",
                total_updated, total_skipped, total_errors)

    if args.dry_run:
        logger.info("DRY RUN — no changes were made. Remove --dry-run to execute.")


if __name__ == "__main__":
    main()
