#!/usr/bin/env python3
"""Create 10 thematic playlists for all active Autotube channels.

Excludes:
- Channel ID 6 ("Pruebas de algoritmo" / slug "test")
- Inactive channels (active=0)

Uses the LLM-based playlist generator to produce SEO-optimised playlists,
creates them on YouTube via the Data API v3, and caches the IDs locally.

Quota cost: ~50 units per playlist created (10 × 50 = 500 per channel).

Usage:
    python3 scripts/create_all_playlists.py          # dry-run (default)
    python3 scripts/create_all_playlists.py --live   # actually create
    python3 scripts/create_all_playlists.py --live --force  # regenerate all
"""

import argparse
import logging
import sys
from pathlib import Path

# Ensure project root is on sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from config.settings import DATABASE_PATH, LOGS_DIR
from database.db_extended import ExtendedDatabase, migrate_v2
from database.db import init_db

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("create_playlists")


# Channels to ALWAYS skip (test/experimental)
_SKIP_CHANNEL_IDS = {6}  # "Pruebas de algoritmo"


def main():
    parser = argparse.ArgumentParser(
        description="Create playlists for all active Autotube channels"
    )
    parser.add_argument(
        "--live", action="store_true",
        help="Actually create playlists on YouTube (default: dry-run)",
    )
    parser.add_argument(
        "--force", action="store_true",
        help="Regenerate playlists even if channel already has them in DB",
    )
    parser.add_argument(
        "--slug", type=str, default=None,
        help="Only process a specific channel slug (e.g. 'canal2')",
    )
    args = parser.parse_args()

    # Init DB
    init_db()
    migrate_v2()
    db = ExtendedDatabase()

    # Get channels
    if args.slug:
        ch = db.get_channel_by_slug(args.slug)
        channels = [ch] if ch else []
        if not channels:
            logger.error("Channel slug '%s' not found", args.slug)
            sys.exit(1)
    else:
        channels = db.get_channels(active_only=True)

    # Filter out test/algo channels
    channels = [c for c in channels if c["id"] not in _SKIP_CHANNEL_IDS]

    if not channels:
        logger.info("No channels to process")
        return

    logger.info("Found %d channel(s) to process", len(channels))
    if not args.live:
        logger.info("🔍 DRY-RUN mode — use --live to actually create playlists")

    from pipeline.youtube_playlists import create_playlists_for_channel

    results = {}
    for ch in channels:
        slug = ch["slug"]
        name = ch.get("name", slug)
        logger.info("%s Processing channel: %s (%s)", "=" * 40, name, slug)

        try:
            if args.live:
                result = create_playlists_for_channel(slug, force=args.force)
            else:
                # Dry-run: just check what would happen
                existing = db.get_channel_youtube_playlists(ch["id"])
                if existing and not args.force:
                    logger.info("  Would SKIP — already has %d playlists (use --force to regenerate)", len(existing))
                    results[slug] = {"dry_run": True, "existing_count": len(existing)}
                else:
                    logger.info("  Would CREATE 10 playlists (via LLM + YouTube API)")
                    results[slug] = {"dry_run": True, "would_create": 10}
                continue

            if "error" in result:
                logger.error("  ❌ Error: %s", result["error"])
            else:
                logger.info("  ✅ Created: %d, Existing: %d, Errors: %d",
                           result.get("created_count", 0),
                           result.get("existing_count", 0),
                           len(result.get("errors", [])))
                for err in result.get("errors", []):
                    logger.warning("    ⚠️ %s", err)
            results[slug] = result

        except Exception as exc:
            logger.error("  ❌ Failed for channel '%s': %s", slug, exc)
            results[slug] = {"error": str(exc)}

    # Summary
    logger.info("%s Summary %s", "=" * 20, "=" * 20)
    for slug, r in results.items():
        if r.get("dry_run"):
            logger.info("  %s: DRY-RUN (would %s)", slug,
                       "skip" if r.get("existing_count") else "create 10")
        elif "error" in r:
            logger.info("  %s: ❌ %s", slug, r["error"])
        else:
            logger.info("  %s: ✅ %d created", slug, r.get("created_count", 0))


if __name__ == "__main__":
    main()
