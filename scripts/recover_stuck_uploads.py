#!/usr/bin/env python3
"""
Recover stuck videos in 'awaiting_upload' status.

Uploads them one by one as private with re-calculated publishAt times.
Includes random delays (5-15 min) between uploads to avoid bot-like patterns.

Usage:
    python3 scripts/recover_stuck_uploads.py [--dry-run] [--max N] [--delay-min M]

Options:
    --dry-run      Print what would be done without uploading
    --max N        Maximum videos to upload (default: all)
    --delay-min M  Minimum delay between uploads in minutes (default: 5)
    --delay-max M  Maximum delay between uploads in minutes (default: 15)
"""

import argparse
import json
import logging
import random
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Ensure we can import from the project root
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from database.db_extended import ExtendedDatabase
from pipeline.youtube_uploader import YouTubeUploader
from pipeline.publish_scheduler import calculate_target_public_time
from config.config_bridge import get_channel_config

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("recover_uploads")


def build_channel_map(db: ExtendedDatabase) -> dict[int, str]:
    """Build channel_id → slug mapping."""
    channels = db.get_channels()
    return {ch["id"]: ch["slug"] for ch in channels}


def parse_datetime_safe(value) -> datetime | None:
    """Parse ISO8601 datetime string safely, returning None on failure."""
    if not value:
        return None
    try:
        s = str(value).replace("Z", "+00:00").replace(" ", "T")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, TypeError):
        return None


def get_video_keywords(video: dict) -> str:
    """Extract a primary keyword from video tags or title."""
    tags_json = video.get("tags_json")
    if tags_json:
        try:
            tags = json.loads(tags_json)
            if tags and isinstance(tags, list) and len(tags) > 0:
                return tags[0]
        except (json.JSONDecodeError, TypeError):
            pass
    
    title = video.get("titulo_final", "")
    if title:
        words = title.split()
        for word in words[:10]:
            if len(word) > 4:
                return word.lower()
    
    return "documental"


def upload_single_video(
    db: ExtendedDatabase,
    video: dict,
    slug: str,
    channel_id: int,
    dry_run: bool = False,
) -> bool:
    """Upload one video. Returns True on success, False on failure."""
    video_id = video["id"]
    video_path = video.get("video_path", "")
    title = video.get("titulo_final", "Video sin título")
    description = video.get("description", "")
    tags_json = video.get("tags_json", "[]")

    # Parse tags
    try:
        tags = json.loads(tags_json) if isinstance(tags_json, str) else (tags_json or [])
        if not isinstance(tags, list):
            tags = []
    except (json.JSONDecodeError, TypeError):
        tags = []

    # Validate video file exists
    vp = Path(video_path)
    if not vp.is_absolute():
        vp = Path("/root/autotube") / video_path
    if not vp.exists():
        logger.error("[%s] Video #%d: file not found: %s", slug, video_id, vp)
        return False

    # Calculate new publish time
    primary_kw = get_video_keywords(video)
    cfg = get_channel_config(slug)
    warmup_min = getattr(cfg, "PUBLISH_WARMUP_MIN", 60)
    tz_str = getattr(cfg, "PUBLISH_TIMEZONE", "Europe/Madrid")

    try:
        pub_result = calculate_target_public_time(
            slug=slug,
            primary_keyword=primary_kw,
            timezone_str=tz_str,
            warmup_min=warmup_min,
            db=db,
            channel_id=channel_id,
        )
        publish_at = pub_result["target_public_at"]
        peak_source = pub_result.get("peak_source", "heuristic")
        logger.info(
            "[%s] Video #%d: new publishAt=%s (source=%s, peak=%s)",
            slug, video_id, publish_at, peak_source,
            pub_result.get("peak_hour_local", "?"),
        )
    except Exception as e:
        logger.error("[%s] Video #%d: failed to calculate publish time: %s", slug, video_id, e)
        # Fallback: schedule 24h from now at a reasonable hour
        now = datetime.now(timezone.utc)
        tomorrow = now + timedelta(days=1)
        publish_at = tomorrow.replace(hour=18, minute=0, second=0, microsecond=0).isoformat()
        peak_source = "fallback"
        logger.warning("[%s] Video #%d: using fallback publishAt=%s", slug, video_id, publish_at)

    if dry_run:
        logger.info(
            "[DRY-RUN] [%s] Would upload video #%d: '%s' | publishAt=%s",
            slug, video_id, title[:60], publish_at,
        )
        return True

    # Upload
    try:
        uploader = YouTubeUploader(account_name=slug, channel_slug=slug)
        if not uploader.authenticate():
            logger.error("[%s] Video #%d: authentication failed", slug, video_id)
            return False

        logger.info("[%s] 📤 Uploading video #%d: '%s'...", slug, video_id, title[:60])
        result = uploader.upload(
            video_path=vp,
            title=title,
            description=description,
            tags=tags,
            privacy="private",
            publish_at=publish_at,
        )

        yt_id = result.get("video_id")
        yt_url = result.get("url", f"https://youtu.be/{yt_id}")

        # Update DB
        db.update_video(
            video_id,
            status="uploaded_private",
            privacy_status="private",
            target_public_at=publish_at,
            peak_source=peak_source,
            yt_video_id=yt_id,
            yt_url=yt_url,
        )
        # Also mark uploaded
        db.mark_video_uploaded(video_id, yt_id, yt_url, status="uploaded_private")

        logger.info(
            "[%s] ✅ Video #%d uploaded: yt=%s url=%s",
            slug, video_id, yt_id, yt_url,
        )
        return True

    except Exception as e:
        logger.error("[%s] ❌ Video #%d: upload failed: %s", slug, video_id, e)
        return False


def main():
    parser = argparse.ArgumentParser(description="Recover stuck awaiting_upload videos")
    parser.add_argument("--dry-run", action="store_true", help="Print what would be done without uploading")
    parser.add_argument("--max", type=int, default=0, dest="max_videos", help="Maximum videos to upload (0 = all)")
    parser.add_argument("--delay-min", type=int, default=5, help="Minimum delay between uploads (minutes)")
    parser.add_argument("--delay-max", type=int, default=15, help="Maximum delay between uploads (minutes)")
    args = parser.parse_args()

    db = ExtendedDatabase()
    ch_map = build_channel_map(db)

    # Fetch stuck videos: awaiting_upload with a video file, sorted by target_public_at ASC
    with db._connect() as conn:
        conn.row_factory = lambda cursor, row: dict(
            zip([col[0] for col in cursor.description], row)
        )
        rows = conn.execute(
            """SELECT v.*, ch.slug as channel_slug
               FROM videos v
               JOIN channels ch ON ch.id = v.channel_id
               WHERE v.status = 'awaiting_upload'
                 AND v.video_path IS NOT NULL
                 AND v.video_path != ''
               ORDER BY v.target_public_at ASC""",
        ).fetchall()

    if not rows:
        logger.info("No stuck videos found. All clear! 🎉")
        return

    logger.info("Found %d stuck videos in awaiting_upload", len(rows))

    if args.max_videos > 0:
        rows = rows[: args.max_videos]
        logger.info("Limited to %d videos", args.max_videos)

    total = len(rows)
    success = 0
    failed = 0

    for i, video in enumerate(rows):
        video_id = video["id"]
        channel_id = video["channel_id"]
        slug = video.get("channel_slug") or ch_map.get(channel_id, "unknown")
        old_target = parse_datetime_safe(video.get("target_public_at"))

        logger.info(
            "── [%d/%d] Video #%d [%s] '%s' (old target=%s) ──",
            i + 1, total, video_id, slug,
            (video.get("titulo_final") or "?")[:50],
            old_target.strftime("%Y-%m-%d %H:%M UTC") if old_target else "N/A",
        )

        ok = upload_single_video(db, video, slug, channel_id, dry_run=args.dry_run)
        if ok:
            success += 1
        else:
            failed += 1

        # Delay between uploads (except after last one)
        if i < total - 1 and not args.dry_run:
            delay = random.randint(args.delay_min, args.delay_max) * 60
            logger.info(
                "⏳ Waiting %d min %d sec before next upload...",
                delay // 60, delay % 60,
            )
            time.sleep(delay)

    logger.info(
        "Done! %d uploaded, %d failed, %d total",
        success, failed, total,
    )


if __name__ == "__main__":
    main()
