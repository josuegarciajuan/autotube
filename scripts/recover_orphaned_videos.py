#!/usr/bin/env python3
"""
Recover orphaned videos: videos in 'error' status that have a fully-generated
.mp4 file on disk but were abandoned (upload retries exhausted, server restart, etc).

Two modes:
  --reset         Re-enqueues videos: resets status to 'awaiting_upload' so the
                  normal upload_scheduler picks them up (default, safe)
  --upload-now    Uploads them immediately bypassing the scheduler (⚠️ bypasses windows)

Usage:
    # List recoverable videos without changing anything
    python3 scripts/recover_orphaned_videos.py --dry-run

    # Reset to awaiting_upload (let scheduler handle the rest)
    python3 scripts/recover_orphaned_videos.py

    # Filter by channel
    python3 scripts/recover_orphaned_videos.py --channel canal3

    # Only safe tier (generate_only completed, upload_only failed)
    python3 scripts/recover_orphaned_videos.py --only-safe

    # Upload immediately (bypasses upload windows — use with care)
    python3 scripts/recover_orphaned_videos.py --upload-now --max 2

Options:
    --dry-run        Print what would be done without making changes
    --channel CH     Only process videos for a specific channel slug
    --only-safe      Only recover Tier 1 videos (generate_only completed, safe)
    --upload-now     Upload immediately instead of resetting to awaiting_upload
    --max N          Maximum videos to process (default: all)
    --delay-min M    Minimum delay between uploads in minutes (default: 3)
    --delay-max M    Maximum delay between uploads in minutes (default: 8)
"""

import argparse
import json
import logging
import os
import random
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from database.db_extended import ExtendedDatabase
from config.settings import PROJECT_ROOT
from pipeline.youtube_uploader import YouTubeUploader
from pipeline.publish_scheduler import calculate_target_public_time
from config.config_bridge import get_channel_config

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("recover_orphaned")


# ═══════════════════════════════════════════════════════════════
#  Classification
# ═══════════════════════════════════════════════════════════════

def classify_video(db: ExtendedDatabase, video: dict) -> dict:
    """Classify an error video by recovery tier.

    Returns dict with: tier (1=safe, 2=likely, 3=review), reason, has_gen_completed.
    """
    video_id = video["id"]
    progress = video.get("progress", 0) or 0
    progress_phase = video.get("progress_phase", "") or ""
    gen_finished = video.get("generation_finished_at")
    path = video.get("video_path", "")

    # Check file
    exists = path and os.path.exists(path)
    size_mb = os.path.getsize(path) / 1024 / 1024 if exists else 0

    if not exists or size_mb < 10:
        return {"tier": 3, "icon": "⚫", "reason": f"File missing or too small ({size_mb:.0f}MB)", "safe": False, "has_gen_completed": False}

    with db._connect() as conn:
        jobs = conn.execute(
            "SELECT action, status, error_msg FROM generation_jobs WHERE video_id=? ORDER BY created_at DESC",
            (video_id,),
        ).fetchall()

    has_gen_completed = any(j["action"] == "generate_only" and j["status"] == "completed" for j in jobs)
    all_upload_failed = bool(
        [j for j in jobs if j["action"] == "upload_only"]
        and all(j["status"] == "failed" for j in jobs if j["action"] == "upload_only")
    )

    # Tier 1: generate_only completed, upload_only all failed
    if has_gen_completed and all_upload_failed:
        return {
            "tier": 1, "icon": "🟢", "reason": "generate_only OK, upload_only all failed → safe to re-upload",
            "safe": True, "has_gen_completed": True,
        }

    # Tier 2: progress >= 80% and large file and error phase
    if progress >= 80 and size_mb > 50 and progress_phase in ("error", "video", "upload"):
        return {
            "tier": 2, "icon": "🟠", "reason": f"Progress {progress}%, {size_mb:.0f}MB → likely complete",
            "safe": False, "has_gen_completed": False,
        }

    # Tier 3: everything else — needs human review
    return {
        "tier": 3, "icon": "⚫", "reason": f"Phase={progress_phase}, progress={progress}%, {size_mb:.0f}MB → needs review",
        "safe": False, "has_gen_completed": False,
    }


def find_recoverable_videos(db: ExtendedDatabase, channel_slug: str = None, only_safe: bool = False) -> list[dict]:
    """Find all error videos with .mp4 files on disk.

    Returns list of dicts with video data + classification.
    """
    with db._connect() as conn:
        if channel_slug:
            rows = conn.execute(
                """SELECT v.*, c.slug as channel_slug, c.name as channel_name
                   FROM videos v JOIN channels c ON c.id = v.channel_id
                   WHERE v.status = 'error'
                     AND v.video_path IS NOT NULL AND v.video_path != ''
                     AND c.slug = ?
                   ORDER BY v.created_at DESC""",
                (channel_slug,),
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT v.*, c.slug as channel_slug, c.name as channel_name
                   FROM videos v JOIN channels c ON c.id = v.channel_id
                   WHERE v.status = 'error'
                     AND v.video_path IS NOT NULL AND v.video_path != ''
                   ORDER BY v.created_at DESC""",
            ).fetchall()

    results = []
    for row in rows:
        video = dict(row)
        classification = classify_video(db, video)
        video.update(classification)

        if only_safe and not classification["safe"]:
            continue
        results.append(video)

    return results


# ═══════════════════════════════════════════════════════════════
#  Recovery actions
# ═══════════════════════════════════════════════════════════════

def reset_to_awaiting_upload(db: ExtendedDatabase, video: dict) -> bool:
    """Reset video status to awaiting_upload so the scheduler picks it up."""
    video_id = video["id"]
    slug = video.get("channel_slug", "?")
    title = (video.get("titulo_final") or "?")[:60]

    with db._connect() as conn:
        # Update video
        conn.execute(
            "UPDATE videos SET status='awaiting_upload', progress=5, progress_phase='upload', "
            "scheduled_upload_at=NULL, error_message=NULL "
            "WHERE id=?",
            (video_id,),
        )
        # Reset failed upload_only jobs to cancelled so the retry counter is cleared
        conn.execute(
            "UPDATE generation_jobs SET status='cancelled', "
            "error_msg='Recovered by recover_orphaned_videos.py' "
            "WHERE video_id=? AND action='upload_only' AND status='failed'",
            (video_id,),
        )
        conn.commit()

    logger.info("[%s] ✅ Video #%d reset to awaiting_upload: '%s'", slug, video_id, title)
    return True


def upload_now(db: ExtendedDatabase, video: dict, dry_run: bool = False) -> bool:
    """Upload the video immediately, bypassing the scheduler."""
    video_id = video["id"]
    slug = video.get("channel_slug", "?")
    video_path = video.get("video_path", "")
    title = video.get("titulo_final", "Video sin título")
    description = video.get("description", "")
    tags_json = video.get("tags_json", "[]")
    channel_id = video["channel_id"]

    # Parse tags
    try:
        tags = json.loads(tags_json) if isinstance(tags_json, str) else (tags_json or [])
        if not isinstance(tags, list):
            tags = []
    except (json.JSONDecodeError, TypeError):
        tags = []

    # Validate file
    vp = Path(video_path)
    if not vp.is_absolute():
        vp = PROJECT_ROOT / video_path
    if not vp.exists():
        logger.error("[%s] Video #%d: file not found: %s", slug, video_id, vp)
        with db._connect() as conn:
            conn.execute("UPDATE videos SET status='error', error_message='mp4 file missing' WHERE id=?", (video_id,))
            conn.commit()
        return False

    # Calculate publish time
    primary_kw = "documental"
    if tags:
        primary_kw = tags[0]
    try:
        cfg = get_channel_config(slug)
    except Exception:
        cfg = None

    warmup_min = getattr(cfg, "PUBLISH_WARMUP_MIN", 60) if cfg else 60
    tz_str = getattr(cfg, "PUBLISH_TIMEZONE", "Europe/Madrid") if cfg else "Europe/Madrid"

    try:
        pub_result = calculate_target_public_time(
            slug=slug, primary_keyword=primary_kw, timezone_str=tz_str,
            warmup_min=warmup_min, db=db, channel_id=channel_id,
        )
        publish_at = pub_result["target_public_at"]
        peak_source = pub_result.get("peak_source", "heuristic")
    except Exception:
        now = datetime.now(timezone.utc)
        publish_at = (now + timedelta(days=1)).replace(hour=18, minute=0, second=0, microsecond=0).isoformat()
        peak_source = "fallback"

    if dry_run:
        logger.info("[DRY-RUN] [%s] Would upload #%d: '%s' | publishAt=%s", slug, video_id, title[:60], publish_at)
        return True

    # Upload
    try:
        uploader = YouTubeUploader(account_name=slug, channel_slug=slug)
        if not uploader.authenticate():
            logger.error("[%s] Video #%d: auth failed", slug, video_id)
            return False

        logger.info("[%s] Uploading #%d: '%s'...", slug, video_id, title[:60])
        result = uploader.upload(
            video_path=vp, title=title, description=description, tags=tags,
            privacy="private", publish_at=publish_at,
        )

        yt_id = result.get("video_id")
        yt_url = result.get("url", f"https://youtu.be/{yt_id}")

        db.update_video(
            video_id, status="uploaded_private", privacy_status="private",
            target_public_at=publish_at, peak_source=peak_source,
            yt_video_id=yt_id, yt_url=yt_url,
        )
        db.mark_video_uploaded(video_id, yt_id, yt_url, status="uploaded_private")

        logger.info("[%s] ✅ #%d uploaded: yt=%s", slug, video_id, yt_id)
        return True
    except Exception as e:
        logger.error("[%s] ❌ #%d upload failed: %s", slug, video_id, e)
        return False


# ═══════════════════════════════════════════════════════════════
#  Main
# ═══════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="Recover orphaned videos with .mp4 files on disk")
    parser.add_argument("--dry-run", action="store_true", help="List recoverable videos without making changes")
    parser.add_argument("--channel", type=str, default=None, help="Filter by channel slug (e.g. canal3)")
    parser.add_argument("--only-safe", action="store_true", help="Only process Tier 1 (safe) videos")
    parser.add_argument("--upload-now", action="store_true", help="Upload immediately instead of resetting to awaiting_upload")
    parser.add_argument("--max", type=int, default=0, dest="max_videos", help="Maximum videos to process (0=all)")
    parser.add_argument("--delay-min", type=int, default=3, help="Minimum delay between uploads in minutes")
    parser.add_argument("--delay-max", type=int, default=8, help="Maximum delay between uploads in minutes")
    args = parser.parse_args()

    db = ExtendedDatabase()

    videos = find_recoverable_videos(db, channel_slug=args.channel, only_safe=args.only_safe)

    if not videos:
        logger.info("No orphaned videos found. All clear!")
        return

    # Summary
    tier1 = [v for v in videos if v["tier"] == 1]
    tier2 = [v for v in videos if v["tier"] == 2]
    tier3 = [v for v in videos if v["tier"] == 3]

    total_size = sum(os.path.getsize(v["video_path"]) / 1024 / 1024 for v in videos if v["video_path"])
    print(f"\n{'='*60}")
    print(f"  Orphaned videos found: {len(videos)} (~{total_size:.0f} MB)")
    print(f"    🟢 Tier 1 (safe):     {len(tier1)}")
    print(f"    🟠 Tier 2 (likely):   {len(tier2)}")
    print(f"    ⚫ Tier 3 (review):   {len(tier3)}")
    print(f"  Mode: {'DRY-RUN' if args.dry_run else ('UPLOAD NOW' if args.upload_now else 'RESET → awaiting_upload')}")
    print(f"{'='*60}\n")

    if args.dry_run:
        for v in videos:
            path = v.get("video_path", "")
            size_mb = os.path.getsize(path) / 1024 / 1024 if path and os.path.exists(path) else 0
            print(f"  {v['icon']} vid={v['id']} ch={v.get('channel_slug', '?'):8} {size_mb:5.0f}MB | {v['reason']}")
            print(f"       title: {(v.get('titulo_final') or '?')[:70]}")
        print()
        print(f"To recover these videos, run without --dry-run")
        print(f"  python3 scripts/recover_orphaned_videos.py")
        print(f"  python3 scripts/recover_orphaned_videos.py --only-safe  (safe tier only)")
        return

    if args.max_videos > 0:
        videos = videos[:args.max_videos]

    total = len(videos)
    success = 0
    failed = 0

    for i, video in enumerate(videos):
        vid = video["id"]
        slug = video.get("channel_slug", "?")
        title = (video.get("titulo_final") or "?")[:50]

        print(f"[{i+1}/{total}] {video['icon']} vid={vid} ch={slug} | {title}")
        print(f"       {video['reason']}")

        if args.upload_now:
            ok = upload_now(db, video, dry_run=False)
        else:
            ok = reset_to_awaiting_upload(db, video)

        if ok:
            success += 1
        else:
            failed += 1

        # Delay between operations
        if i < total - 1 and args.upload_now:
            delay = random.randint(args.delay_min, args.delay_max) * 60
            print(f"       ⏳ Waiting {delay//60} min {delay%60} sec...")
            time.sleep(delay)

    print(f"\n{'='*60}")
    print(f"  Done! {success} recovered, {failed} failed, {total} total")
    if not args.upload_now:
        print(f"  Videos reset to 'awaiting_upload' — the upload_scheduler will pick them up.")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
