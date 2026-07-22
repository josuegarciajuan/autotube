#!/usr/bin/env python3
"""
Cleanup script for videos affected by the black-screen bug (Jul 2026).

Actions:
  1. UNLIST published videos on YouTube (videos 485, 491, 492, 518)
  2. DELETE not-yet-published videos from DB and disk (videos 508, 514, 517, 523, 524, 527)

Usage:
  python3 scripts/cleanup_black_screen_videos.py
  python3 scripts/cleanup_black_screen_videos.py --dry-run
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

# ── Videos to UNLIST (published on YouTube) ──────────────────────
# (video_id, yt_video_id, title)
UNLIST_VIDEOS = [
    (518, "hxOEIbT6ZLs", "La pesadilla que activó su ASCO más profundo"),
    (492, "O3dWIIE9Kn8", "La tragedia de los Andes que Netflix no te contó"),
    (491, "1Fw5ImTE8ys", "El amuleto que abrió las puertas del más allá"),
    (485, "WP5pX8uZ4bk", "Sueños lúcidos: el secreto que la ciencia descubrió"),
]

# ── Videos to DELETE (not yet published) ─────────────────────────
# (video_id, yt_video_id or None, title)
DELETE_VIDEOS = [
    (527, None, "El voluntario que colapsó en oncología"),
    (524, None, "El sueño precognitivo que marcó su infancia"),
    (523, "9uyMi9wGwBc", "El vuelo 240 de Malév que israel derribó en secreto"),
    (517, "WctKO7miwQs", "El catalizador que HARÁ explotar la economía del hidrógeno"),
    (514, "kT3C-g02euE", "El rodaje extremo de 'The way we dance'"),
    (508, "I5ufuYlHFDg", "Sueños de bebés: el caso de la madre que soñó un parto"),
]


def get_channel_slug(db, video_id: int) -> str:
    """Get the channel slug for a video_id."""
    video = db.get_video(video_id)
    if video and video.get("channel_id"):
        channel = db.get_channel(video["channel_id"])
        return channel["slug"] if channel else "unknown"
    return "unknown"


def get_video_file_paths(db, video_id: int) -> dict:
    """Get on-disk file paths for a video."""
    video = db.get_video(video_id)
    if not video:
        return {}
    return {
        "video_path": video.get("video_path"),
        "thumbnail_path": video.get("thumbnail_path"),
        "audio_path": video.get("audio_path"),
    }


def unlist_video(video_id: int, yt_video_id: str, title: str) -> bool:
    """Set a published video to unlisted on YouTube and in DB."""
    db = ExtendedDatabase()
    slug = get_channel_slug(db, video_id)
    logger.info("Unlisting: video_id=%d yt_id=%s channel=%s title=%s",
                video_id, yt_video_id, slug, title[:60])

    if DRY_RUN:
        logger.info("  [DRY RUN] Would unlist %s on channel %s", yt_video_id, slug)
        return True

    try:
        # Instantiate YouTubeUploader for this channel
        from pipeline.youtube_uploader import YouTubeUploader
        uploader = YouTubeUploader(account_name=slug, db=db, channel_slug=slug)
        uploader.authenticate()
        uploader.set_privacy(yt_video_id, "unlisted")
        logger.info("  ✅ YouTube: set to unlisted")
    except Exception as exc:
        logger.error("  ❌ YouTube API failed for %s (channel %s): %s",
                     yt_video_id, slug, exc)
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


def delete_video_complete(video_id: int, yt_video_id: str | None, title: str) -> bool:
    """Delete a video from DB, disk, and optionally YouTube."""
    db = ExtendedDatabase()
    slug = get_channel_slug(db, video_id)
    logger.info("Deleting: video_id=%d yt_id=%s channel=%s title=%s",
                video_id, yt_video_id or "N/A", slug, title[:60])

    # ── Get file paths BEFORE deleting DB records ──
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

    # ── 1. YouTube: delete video if it exists on YT ──
    if yt_video_id:
        try:
            from pipeline.youtube_uploader import YouTubeUploader
            uploader = YouTubeUploader(account_name=slug, db=db, channel_slug=slug)
            uploader.authenticate()
            service = uploader._get_service()
            service.videos().delete(id=yt_video_id).execute()
            logger.info("  ✅ YouTube: deleted %s", yt_video_id)
        except Exception as exc:
            logger.warning("  ⚠️ YouTube delete failed (may not exist): %s", exc)

    # ── 2. DB: delete orphan records (no CASCADE) ──
    try:
        with db._connect() as conn:
            conn.execute("PRAGMA foreign_keys = ON")
            # These tables have NO ACTION FK → must delete manually
            conn.execute("DELETE FROM video_playlists WHERE video_id = ?", (video_id,))
            conn.execute("DELETE FROM video_lifecycle_actions WHERE video_id = ?", (video_id,))
            conn.execute("DELETE FROM comment_log WHERE video_id = ?", (video_id,))
            conn.execute("DELETE FROM shorts_planned_slots WHERE source_video_id = ?", (video_id,))
            # Nullify SET NULL FKs (optional — these get nullified by FK engine)
            conn.execute("UPDATE generation_jobs SET video_id = NULL WHERE video_id = ?", (video_id,))
            conn.execute("UPDATE planned_slots SET video_id = NULL WHERE video_id = ?", (video_id,))
            conn.execute("UPDATE shorts SET source_video_id = NULL WHERE source_video_id = ?", (video_id,))
            # Now delete the video — CASCADE handles video_scenes, stats_history, asset_history
            conn.execute("DELETE FROM videos WHERE id = ?", (video_id,))
            conn.commit()
        logger.info("  ✅ DB: video %d and all related records deleted", video_id)
    except Exception as exc:
        logger.error("  ❌ DB delete failed: %s", exc)
        return False

    # ── 3. Disk: delete files ──
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
    db = ExtendedDatabase()
    success = 0
    fail = 0

    # ── Phase 1: Unlist published videos ──────────────────────
    logger.info("=" * 60)
    logger.info("PHASE 1: Unlisting %d published videos on YouTube", len(UNLIST_VIDEOS))
    logger.info("=" * 60)
    for video_id, yt_id, title in UNLIST_VIDEOS:
        if unlist_video(video_id, yt_id, title):
            success += 1
        else:
            fail += 1

    # ── Phase 2: Delete not-published videos ──────────────────
    logger.info("=" * 60)
    logger.info("PHASE 2: Deleting %d not-published videos", len(DELETE_VIDEOS))
    logger.info("=" * 60)
    for video_id, yt_id, title in DELETE_VIDEOS:
        if delete_video_complete(video_id, yt_id, title):
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
