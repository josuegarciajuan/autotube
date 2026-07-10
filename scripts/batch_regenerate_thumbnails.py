#!/usr/bin/env python3
"""Batch regenerate thumbnails for all uploaded videos of a channel.

Usage:
    python3 scripts/batch_regenerate_thumbnails.py [--canal canal4] [--sleep 60] [--dry-run]

Iterates through all uploaded videos, calls the v2 4-phase thumbnail pipeline
(Style Engine → Brainstorming → Pollo AI + QC → Composition), updates the DB,
and sleeps between videos to avoid Pollo AI rate limiting.
"""
import argparse
import json
import logging
import sqlite3
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from config.config_bridge import get_channel_config
from pipeline.thumbnail_maker import ThumbnailMaker

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("batch_thumbnails")


def fetch_videos(db_path: str, canal: str) -> list[dict]:
    """Return all uploaded videos for the given channel, ordered by id ASC."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT v.* FROM videos v "
        "JOIN channels c ON v.channel_id = c.id "
        "WHERE c.slug = ? AND v.status = 'uploaded' AND v.titulo_final IS NOT NULL AND v.titulo_final != '' "
        "ORDER BY v.id ASC",
        (canal,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def fetch_script_text(db_path: str, script_id: int | None) -> str:
    """Fetch the guion text for a given script_id."""
    if not script_id:
        return ""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT guion FROM scripts WHERE id = ?", (script_id,)).fetchone()
    conn.close()
    return row["guion"] if row and row["guion"] else ""


def fetch_keywords(video: dict) -> list[str]:
    """Extract keywords from tags_json field."""
    tags_raw = video.get("tags_json")
    if not tags_raw:
        return []
    try:
        tags = json.loads(tags_raw) if isinstance(tags_raw, str) else tags_raw
        return tags if isinstance(tags, list) else []
    except (json.JSONDecodeError, TypeError):
        return []


def update_thumbnail(db_path: str, video_id: int, thumbnail_path: str) -> None:
    """Update the thumbnail_path column in the videos table."""
    conn = sqlite3.connect(db_path)
    conn.execute(
        "UPDATE videos SET thumbnail_path = ? WHERE id = ?",
        (thumbnail_path, video_id),
    )
    conn.commit()
    conn.close()


def process_one(
    video: dict,
    cfg,
    maker: ThumbnailMaker,
    db_path: str,
    channel_display: str,
    channel_desc: str,
    channel_theme: str,
) -> bool:
    """Generate a thumbnail for a single video. Returns True on success."""
    video_id = video["id"]
    title = video.get("titulo_final", "Historia Impactante")
    keywords = fetch_keywords(video)
    script_text = fetch_script_text(db_path, video.get("script_id"))

    logger.info("Video #%s: %s", video_id, title)

    thumb_path = maker.make_viral_thumbnail(
        title=title,
        overlay_text="",
        keywords=keywords[:10] if keywords else [],
        scene_images=None,
        script_text=script_text[:1500] if script_text else "",
        canal_slug="canal4",
        channel_display_name=channel_display,
        channel_description=channel_desc,
        channel_theme=channel_theme,
        video_id=video_id,
    )

    update_thumbnail(db_path, video_id, str(thumb_path))
    logger.info("✅ Video #%s thumbnail saved: %s", video_id, thumb_path)
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description="Batch regenerate thumbnails")
    parser.add_argument("--canal", default="canal4", help="Channel slug")
    parser.add_argument("--sleep", type=int, default=60, help="Seconds between videos")
    parser.add_argument("--dry-run", action="store_true", help="List videos without generating")
    args = parser.parse_args()

    db_path = PROJECT_ROOT / "autotube.db"

    # ── Fetch videos ───────────────────────────────────────────
    videos = fetch_videos(str(db_path), args.canal)
    if not videos:
        logger.warning("No uploaded videos found for canal=%s", args.canal)
        return 0

    logger.info("Found %d uploaded videos for %s", len(videos), args.canal)
    for v in videos:
        logger.info("  #%d: %s", v["id"], v.get("titulo_final", "?"))

    if args.dry_run:
        logger.info("Dry run — stopping here.")
        return 0

    # ── Channel config ─────────────────────────────────────────
    cfg = get_channel_config(args.canal)
    channel_display = getattr(cfg, "CANAL_DISPLAY_NAME", args.canal)
    channel_desc = getattr(cfg, "CHANNEL_ABOUT_SECTION", "")
    channel_theme = getattr(cfg, "CANAL_TAGLINE", "")

    logger.info("Channel: %s | Style: %s",
                 channel_display,
                 getattr(cfg, "THUMBNAIL_VISUAL_STYLE", "auto"))

    # ── Thumbnail maker (singleton — reuse across iterations) ──
    maker = ThumbnailMaker(config=cfg)

    # ── Process each video ─────────────────────────────────────
    total = len(videos)
    ok = 0
    fail = 0

    for idx, video in enumerate(videos, start=1):
        video_id = video["id"]
        title = video.get("titulo_final", "")
        logger.info("=" * 60)
        logger.info("[%d/%d] Processing video #%d: %s", idx, total, video_id, title)

        try:
            process_one(
                video=video,
                cfg=cfg,
                maker=maker,
                db_path=str(db_path),
                channel_display=channel_display,
                channel_desc=channel_desc,
                channel_theme=channel_theme,
            )
            ok += 1
        except Exception as exc:
            logger.exception("❌ Video #%d failed: %s", video_id, exc)
            fail += 1

        # Sleep between iterations (skip after the last one)
        if idx < total:
            logger.info("⏳ Sleeping %ds before next video...", args.sleep)
            time.sleep(args.sleep)

    # ── Final summary ──────────────────────────────────────────
    logger.info("=" * 60)
    logger.info("DONE: %d ok, %d failed, %d total", ok, fail, total)
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
