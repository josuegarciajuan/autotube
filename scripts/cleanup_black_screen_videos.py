#!/usr/bin/env python3
"""
Cleanup script for videos affected by the black-screen bug (Jul 2026).
Second wave — global video_clips/ rmtree race condition.

Actions:
  1. UNLIST published videos on YouTube (set to private)
  2. DELETE not-yet-published videos from DB and disk

Usage:
  python3 scripts/cleanup_black_screen_videos.py
  python3 scripts/cleanup_black_screen_videos.py --dry-run
  python3 scripts/cleanup_black_screen_videos.py --unlist-only   # only YT actions
  python3 scripts/cleanup_black_screen_videos.py --delete-only   # only DB+disk actions
"""

import os
import sys
import shutil
import logging
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("cleanup")

# ── Load .env ───────────────────────────────────────────────────
env_file = Path(__file__).parent.parent / ".env"
if env_file.exists():
    with open(env_file) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, val = line.split("=", 1)
                os.environ[key.strip()] = val.strip()
    logger.info("Loaded environment from .env")

from database.db_extended import ExtendedDatabase

DRY_RUN = "--dry-run" in sys.argv
UNLIST_ONLY = "--unlist-only" in sys.argv
DELETE_ONLY = "--delete-only" in sys.argv
DO_ALL = not UNLIST_ONLY and not DELETE_ONLY

# ── Videos to UNLIST (published on YouTube, confirmed black screens) ──
# (video_id, yt_video_id, title, channel_slug)
UNLIST_VIDEOS = [
    (286, "Xe1rqPd_NOE", "canal2"),
    (294, "5tu05u6HICQ", "canal5"),
    (317, "e4usN6Q_3U0", "canal5"),
    (471, "yKe8-R8CkI0", "canal3"),
    (473, "nVPWg61OO_c", "canal3"),
    (474, "Idn3pSNM5xc", "canal5"),
    (475, "RqX62z1wYyY", "canal5"),
    (476, "7jKGe1GYMss", "canal2"),
    (541, "av14YjZMxvE", "canal5"),
    (640, "zMjKw3fP0Bc", "canal3"),
    (785, "moBYVTVc0Ww", "canal4"),
    (887, "VWAtZnLT1U4", "canal2"),
    (914, "s7AiA4RkXeI", "canal5"),
    (917, "KoqaSkRdfY8", "canal4"),
    (920, "s1CdW1auYWA", "canal2"),
    (928, "4Njhw9wk6E4", "canal4"),
    (930, "xBcVZI96XLE", "canal2"),
    (932, "38oinPgXlAQ", "canal5"),
]

# ── Videos to DELETE (not yet published or already failed) ──
# (video_id, description)
DELETE_VIDEOS = [
    (490, "canal3 error"),
    (493, "canal2 error"),
    (495, "canal3 error"),
    (496, "canal3 error"),
    (497, "canal3 error"),
    (507, "canal3 error"),
    (509, "canal2 error"),
    (510, "canal2 error"),
    (511, "canal3 error"),
    (512, "canal3 error"),
    (513, "canal3 error"),
    (515, "canal2 error"),
    (516, "canal3 error"),
    (519, "canal3 error"),
    (520, "canal3 error"),
    (521, "canal3 error"),
    (522, "canal3 error"),
    (525, "canal3 error"),
    (526, "canal3 error"),
    (582, "canal3 error"),
    (904, "canal3 error"),
    (907, "canal3 error"),
    (910, "canal2 awaiting_upload"),
    (912, "canal3 error"),
    (918, "canal3 error"),
    (921, "canal3 awaiting_upload"),
    # 922 excluded — currently generating (job 1441 running)
]


def get_video_file_paths(db, video_id: int) -> dict:
    """Get on-disk file paths for a video."""
    video = db.get_video(video_id)
    if not video:
        return {}
    paths = {}
    for k in ["video_path", "thumbnail_path", "audio_path"]:
        v = video.get(k)
        if v:
            paths[k] = str(v)
    return paths


def unlist_video(video_id: int, yt_video_id: str, channel_slug: str) -> bool:
    """Set a published video to private on YouTube and mark in DB."""
    db = ExtendedDatabase()
    logger.info("Unlisting: video_id=%d yt_id=%s channel=%s",
                video_id, yt_video_id, channel_slug)

    if DRY_RUN:
        logger.info("  [DRY RUN] Would unlist %s on channel %s", yt_video_id, channel_slug)
        return True

    try:
        from pipeline.youtube_uploader import YouTubeUploader
        uploader = YouTubeUploader(account_name=channel_slug, db=db, channel_slug=channel_slug)
        uploader.authenticate()
        uploader.set_privacy(yt_video_id, "unlisted")
        logger.info("  ✅ YouTube: set to unlisted")
    except Exception as exc:
        logger.error("  ❌ YouTube API failed for %s (channel %s): %s",
                     yt_video_id, channel_slug, exc)
        return False

    try:
        db.update_video(video_id,
                        privacy_status="unlisted",
                        status="unlisted_quality_issue")
        logger.info("  ✅ DB: marked as unlisted_quality_issue")
    except Exception as exc:
        logger.error("  ❌ DB update failed: %s", exc)
        return False

    return True


def delete_video(video_id: int, desc: str) -> bool:
    """Delete a video from DB and disk."""
    db = ExtendedDatabase()
    logger.info("Deleting: video_id=%d desc=%s", video_id, desc)

    # Get file paths BEFORE deleting DB records
    paths = get_video_file_paths(db, video_id)
    segment_dir = Path("output/videos/segments") / str(video_id)

    if DRY_RUN:
        logger.info("  [DRY RUN] Would delete from DB and disk:")
        for k, v in paths.items():
            if v:
                logger.info("    %s: %s", k, v)
        if segment_dir.exists():
            logger.info("    segments: %s (%d files)", segment_dir,
                        sum(1 for _ in segment_dir.rglob("*")))
        return True

    # ── 1. DB: delete related records ──
    try:
        with db._connect() as conn:
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("DELETE FROM video_playlists WHERE video_id = ?", (video_id,))
            conn.execute("DELETE FROM video_lifecycle_actions WHERE video_id = ?", (video_id,))
            conn.execute("DELETE FROM comment_log WHERE video_id = ?", (video_id,))
            conn.execute("DELETE FROM shorts_planned_slots WHERE source_video_id = ?", (video_id,))
            conn.execute("UPDATE generation_jobs SET video_id = NULL WHERE video_id = ?", (video_id,))
            conn.execute("UPDATE planned_slots SET video_id = NULL WHERE video_id = ?", (video_id,))
            conn.execute("UPDATE shorts SET source_video_id = NULL WHERE source_video_id = ?", (video_id,))
            # CASCADE handles: video_scenes, stats_history, asset_history
            conn.execute("DELETE FROM videos WHERE id = ?", (video_id,))
            conn.commit()
        logger.info("  ✅ DB: video %d and related records deleted", video_id)
    except Exception as exc:
        logger.error("  ❌ DB delete failed: %s", exc)
        return False

    # ── 2. Disk: delete files ──
    files_deleted = 0
    for key in ["video_path", "thumbnail_path", "audio_path"]:
        p = paths.get(key)
        if p and Path(p).exists():
            try:
                Path(p).unlink()
                files_deleted += 1
                logger.info("  🗑️  Deleted: %s", p)
            except OSError as exc:
                logger.warning("  ⚠️  Could not delete %s: %s", p, exc)

    if segment_dir.exists():
        try:
            shutil.rmtree(segment_dir)
            logger.info("  🗑️  Deleted segments dir: %s", segment_dir)
            files_deleted += 1
        except OSError as exc:
            logger.warning("  ⚠️  Could not delete segments dir: %s", exc)

    if files_deleted == 0:
        logger.info("  ℹ️  No on-disk files to delete (already cleaned)")

    return True


def main():
    success = 0
    fail = 0

    if DO_ALL or UNLIST_ONLY:
        logger.info("=" * 60)
        logger.info("PHASE 1: Unlisting %d published videos on YouTube", len(UNLIST_VIDEOS))
        logger.info("=" * 60)
        for video_id, yt_id, channel_slug in UNLIST_VIDEOS:
            if unlist_video(video_id, yt_id, channel_slug):
                success += 1
            else:
                fail += 1

    if DO_ALL or DELETE_ONLY:
        logger.info("=" * 60)
        logger.info("PHASE 2: Deleting %d unpublished videos", len(DELETE_VIDEOS))
        logger.info("=" * 60)
        for video_id, desc in DELETE_VIDEOS:
            if delete_video(video_id, desc):
                success += 1
            else:
                fail += 1

    # ── Summary ───────────────────────────────────────────────
    logger.info("=" * 60)
    logger.info("DONE: %d succeeded, %d failed", success, fail)
    if DRY_RUN:
        logger.info("DRY RUN — no changes made")
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
