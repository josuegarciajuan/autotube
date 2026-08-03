#!/usr/bin/env python3
"""Fix overlapping scheduled publish times for canal4 (Expediciones sin retorno).

Situation: 6 videos are uploaded_private with the identical target_public_at
of 2026-08-03T09:00:00+00:00. They will all go public at the exact same time.

This script:
1. Detects the 6 overlapping videos
2. Redistributes them with 3h minimum gap:
   09:00 → 12:00 → 15:00 → 18:00 → 21:00 → 00:00+1d (all UTC)
3. Updates YouTube API scheduledPublishTime for each (unless --dry-run)
4. Updates the database and lifecycle actions (unless --dry-run)

Usage:
  python3 scripts/fix_canal4_overlap.py           # Real execution
  python3 scripts/fix_canal4_overlap.py --dry-run # Show what would be done
"""

import argparse
import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

# ── Config ───────────────────────────────────────────────
CHANNEL_SLUG = "canal4"
BATCH_TARGET = "2026-08-03T09:00:00+00:00"
MIN_GAP_HOURS = 3

# New target times (UTC, in order of video id ascending)
# Starting from 09:00 UTC = 11:00 Madrid
NEW_TIMES = [
    "2026-08-03T09:00:00+00:00",  # keep first one at original
    "2026-08-03T12:00:00+00:00",
    "2026-08-03T15:00:00+00:00",
    "2026-08-03T18:00:00+00:00",
    "2026-08-03T21:00:00+00:00",
    "2026-08-04T00:00:00+00:00",  # next day midnight UTC = 02:00 Madrid
]


def main(dry_run: bool = False):
    from database.db_extended import ExtendedDatabase
    from pipeline.youtube_uploader import YouTubeUploader

    mode = "DRY RUN" if dry_run else "LIVE"
    logger.info("=" * 60)
    logger.info("fix_canal4_overlap — %s MODE", mode)
    if dry_run:
        logger.info("No changes will be made to YouTube or DB.")
    logger.info("=" * 60)

    db = ExtendedDatabase()

    # ── 1. Find the overlapping videos ──────────────────
    channel = db.get_channel_by_slug(CHANNEL_SLUG)
    if not channel:
        logger.error("Channel '%s' not found in DB", CHANNEL_SLUG)
        return 1

    channel_id = channel["id"]
    logger.info("Channel: %s (id=%d)", CHANNEL_SLUG, channel_id)

    videos = db.get_overlapping_scheduled_videos(
        channel_id=channel_id,
        status="uploaded_private",
        target_public_at=BATCH_TARGET,
    )

    if not videos:
        logger.info(
            "No overlapping videos found with target_public_at=%s", BATCH_TARGET
        )
        return 0

    logger.info("Found %d overlapping videos:", len(videos))
    for v in videos:
        logger.info(
            "  #%d %s (%s)",
            v["id"],
            (v.get("titulo_final") or "?")[:60],
            v.get("yt_video_id", "?"),
        )

    if len(videos) != len(NEW_TIMES):
        logger.warning(
            "Expected %d videos but found %d — adjusting NEW_TIMES",
            len(NEW_TIMES),
            len(videos),
        )

    # ── 2. Initialize uploader for channel ──────────────
    uploader = YouTubeUploader(
        account_name=channel["slug"],
        channel_slug=channel["slug"],
        db=db,
    )

    # ── 3. Redistribute each video ─────────────────────
    logger.info("=" * 60)
    logger.info("Redistributing %d videos with %dh minimum gap...", len(videos), MIN_GAP_HOURS)

    updated_count = 0
    errors = []

    for i, video in enumerate(videos):
        if i >= len(NEW_TIMES):
            logger.warning("Skipping video #%d: no more time slots", video["id"])
            continue

        new_target = NEW_TIMES[i]
        yt_id = video.get("yt_video_id")
        db_id = video["id"]

        logger.info("-" * 50)
        logger.info(
            "Video #%d (%s): %s UTC → %s UTC",
            db_id,
            yt_id,
            str(video.get("target_public_at"))[:19],
            new_target,
        )

        # ── 3a. Update YouTube scheduledPublishTime ─────
        yt_updated = False
        if yt_id:
            if dry_run:
                logger.info(
                    "  [DRY RUN] Would update YouTube API: publishAt=%s", new_target
                )
                yt_updated = True
            else:
                try:
                    service = uploader._get_service()
                    # Only update the publishAt field — leave privacy as private
                    body = {
                        "id": yt_id,
                        "status": {
                            "privacyStatus": "private",
                            "publishAt": new_target,
                        },
                    }
                    response = service.videos().update(
                        part="status",
                        body=body,
                    ).execute()
                    new_privacy = response.get("status", {}).get("privacyStatus", "?")
                    logger.info(
                        "  ✅ YouTube API: publishAt updated → %s (quota cost: ~50u)",
                        new_target,
                    )
                    yt_updated = True
                except Exception as exc:
                    logger.error(
                        "  ❌ YouTube API update failed: %s", exc
                    )
                    errors.append(f"Video #{db_id} ({yt_id}): YouTube API: {exc}")
        else:
            logger.warning("  ⚠️ No yt_video_id — skipping YouTube API update")

        # ── 3b. Update database ─────────────────────────
        if dry_run:
            logger.info(
                "  [DRY RUN] Would update DB: target_public_at=%s", new_target
            )
            updated_count += 1
        else:
            try:
                # Update target_public_at in videos table
                db.update_video(db_id, target_public_at=new_target)

                # Update planned_slots linked to this video
                with db._connect() as conn:
                    conn.execute(
                        """UPDATE planned_slots
                           SET target_public_at = ?
                           WHERE video_id = ? AND target_public_at = ?""",
                        (new_target, db_id, BATCH_TARGET),
                    )
                    # Update lifecycle actions scheduled relative to target
                    conn.execute(
                        """UPDATE video_lifecycle_actions
                           SET scheduled_for = REPLACE(
                               scheduled_for,
                               ?,
                               ?
                           )
                           WHERE video_id = ?""",
                        (BATCH_TARGET, new_target, db_id),
                    )
                    conn.commit()

                logger.info("  ✅ DB updated: video, planned_slots, lifecycle_actions")
                updated_count += 1
            except Exception as exc:
                logger.error("  ❌ DB update failed: %s", exc)
                errors.append(f"Video #{db_id}: DB: {exc}")

    # ── 4. Summary ─────────────────────────────────────
    logger.info("=" * 60)
    logger.info("SUMMARY:")
    logger.info("  Total videos processed: %d", len(videos))
    logger.info("  Successfully updated: %d", updated_count)
    logger.info("  Errors: %d", len(errors))

    if errors:
        logger.warning("Errors encountered:")
        for e in errors:
            logger.warning("  - %s", e)

    # ── 5. Verify ──────────────────────────────────────
    logger.info("-" * 50)
    logger.info("Verifying updated targets:")
    for video in videos:
        updated = db.get_video(video["id"])
        new_tpa = updated.get("target_public_at", "?") if updated else "?"
        logger.info(
            "  #%d: %s → %s",
            video["id"],
            str(video.get("target_public_at"))[:19],
            str(new_tpa)[:19] if new_tpa else "N/A",
        )

    return 0 if not errors else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Fix overlapping scheduled publish times for canal4"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be done without making changes",
    )
    args = parser.parse_args()
    sys.exit(main(dry_run=args.dry_run))
