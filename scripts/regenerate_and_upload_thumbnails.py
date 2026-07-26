#!/usr/bin/env python3
"""Regenerate thumbnails for specific videos and upload them to YouTube.

Usage:
    python3 scripts/regenerate_and_upload_thumbnails.py 914 917 920 928 930 932

For each video ID:
1. Fetch video data from DB (title, keywords, channel slug, YT video ID)
2. Regenerate thumbnail via ThumbnailMaker (Pollo AI + composition)
3. Upload thumbnail to YouTube via YouTube Data API v3
4. Update thumbnail_path in DB

Requirements:
- Valid Pollo AI session cookie (POLLO_SESSION_COOKIE or settings.json)
- YouTube OAuth tokens for each channel (tokens/{slug}.pickle)
"""
import argparse
import json
import logging
import sqlite3
import sys
from pathlib import Path

# Add project root to path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from config.config_bridge import get_channel_config
from pipeline.thumbnail_maker import ThumbnailMaker
from pipeline.youtube_uploader import YouTubeUploader
from googleapiclient.http import MediaFileUpload

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("regen_thumbnails")

DB_PATH = PROJECT_ROOT / "autotube.db"


def fetch_video_data(video_id: int) -> dict | None:
    """Fetch video data from the database."""
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row

    row = conn.execute(
        "SELECT v.*, s.guion as script_text "
        "FROM videos v "
        "LEFT JOIN scripts s ON v.script_id = s.id "
        "WHERE v.id = ?",
        (video_id,),
    ).fetchone()

    if not row:
        logger.error("Video #%s not found in DB", video_id)
        conn.close()
        return None

    video = dict(row)

    # Parse tags
    keywords = []
    tags_raw = video.get("tags_json")
    if tags_raw:
        try:
            keywords = json.loads(tags_raw) if isinstance(tags_raw, str) else tags_raw
        except json.JSONDecodeError:
            pass

    conn.close()
    return {
        "id": video["id"],
        "title": video.get("titulo_final", "Unknown"),
        "keywords": keywords,
        "script_text": video.get("script_text", "") or "",
        "canal": video.get("canal", ""),
        "yt_video_id": video.get("yt_video_id", ""),
        "thumbnail_path": video.get("thumbnail_path", ""),
    }


def upload_thumbnail(channel_slug: str, yt_video_id: str, thumb_path: Path) -> bool:
    """Upload a thumbnail to a YouTube video.
    
    Args:
        channel_slug: Channel slug (e.g. 'canal2')
        yt_video_id: YouTube video ID (e.g. 's1CdW1auYWA')
        thumb_path: Path to the thumbnail image file
    
    Returns:
        True on success, False on failure.
    """
    if not yt_video_id:
        logger.warning("  ⚠️  No YouTube video ID — skipping upload")
        return False

    if not thumb_path.exists():
        logger.error("  ❌ Thumbnail file not found: %s", thumb_path)
        return False

    uploader = YouTubeUploader(account_name=channel_slug, channel_slug=channel_slug)

    if not uploader.authenticate():
        logger.error("  ❌ YouTube auth failed for channel %s", channel_slug)
        return False

    try:
        service = uploader._get_service()
        service.thumbnails().set(
            videoId=yt_video_id,
            media_body=MediaFileUpload(str(thumb_path), mimetype="image/jpeg"),
        ).execute()
        logger.info("  ✅ Thumbnail uploaded to YouTube: %s", yt_video_id)
        return True
    except Exception as exc:
        logger.warning(
            "  ⚠️  Thumbnail upload failed — may need phone verification "
            "at youtube.com/verify. Error: %s", exc
        )
        return False


def process_video(video_id: int) -> int:
    """Regenerate and upload a thumbnail for a single video.
    
    Returns: 0 = success, 1 = regeneration failed, 2 = upload failed
    """
    logger.info("━" * 60)
    logger.info("Video #%s: fetching data...", video_id)

    video = fetch_video_data(video_id)
    if not video:
        return 1

    title = video["title"]
    canal = video["canal"]
    yt_id = video["yt_video_id"]

    logger.info("  Title: %s", title[:80])
    logger.info("  Channel: %s", canal)
    logger.info("  YouTube ID: %s", yt_id)

    # ── Load channel config ─────────────────────────────────
    try:
        cfg = get_channel_config(canal)
    except Exception as exc:
        logger.error("  ❌ Failed to load channel config: %s", exc)
        return 1

    channel_display = getattr(cfg, "CANAL_DISPLAY_NAME", canal)
    channel_desc = getattr(cfg, "CHANNEL_ABOUT_SECTION", "")
    channel_theme = getattr(cfg, "CANAL_TAGLINE", "")

    # ── Regenerate thumbnail ─────────────────────────────────
    logger.info("  Generating thumbnail...")
    maker = ThumbnailMaker(config=cfg)

    try:
        thumb_path = maker.make_viral_thumbnail(
            title=title,
            keywords=video["keywords"],
            scene_images=[],
            script_text=video["script_text"][:1500],
            canal_slug=canal,
            channel_display_name=channel_display,
            channel_description=channel_desc,
            channel_theme=channel_theme,
            video_id=video_id,
        )
        logger.info("  ✅ Thumbnail generated: %s", thumb_path)
    except Exception as exc:
        logger.exception("  ❌ Thumbnail generation failed: %s", exc)
        return 1

    # ── Update DB ────────────────────────────────────────────
    try:
        conn = sqlite3.connect(str(DB_PATH))
        conn.execute(
            "UPDATE videos SET thumbnail_path = ?, updated_at = datetime('now') WHERE id = ?",
            (str(thumb_path), video_id),
        )
        conn.commit()
        conn.close()
        logger.info("  ✅ thumbnail_path updated in DB")
    except Exception as exc:
        logger.error("  ⚠️  Failed to update DB: %s", exc)

    # ── Upload to YouTube ────────────────────────────────────
    logger.info("  Uploading thumbnail to YouTube...")
    upload_ok = upload_thumbnail(canal, yt_id, thumb_path)

    return 0 if upload_ok else 2


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Regenerate thumbnails and upload to YouTube"
    )
    parser.add_argument(
        "video_ids",
        nargs="+",
        type=int,
        help="Video IDs to regenerate thumbnails for",
    )
    parser.add_argument(
        "--skip-upload",
        action="store_true",
        help="Skip YouTube upload (regenerate only)",
    )
    args = parser.parse_args()

    results: dict[str, list[int]] = {"ok": [], "failed_gen": [], "failed_upload": []}

    for video_id in args.video_ids:
        ret = process_video(video_id)
        if ret == 0:
            results["ok"].append(video_id)
        elif ret == 1:
            results["failed_gen"].append(video_id)
        elif ret == 2:
            results["failed_upload"].append(video_id)

    # ── Summary ──────────────────────────────────────────────
    logger.info("═" * 60)
    logger.info("SUMMARY")
    logger.info("  ✅ Regenerated + uploaded: %s", results["ok"] or "none")
    logger.info("  ❌ Generation failed:     %s", results["failed_gen"] or "none")
    logger.info("  ⚠️  Upload failed:         %s", results["failed_upload"] or "none")

    return 0 if not results["failed_gen"] else 1


if __name__ == "__main__":
    sys.exit(main())
