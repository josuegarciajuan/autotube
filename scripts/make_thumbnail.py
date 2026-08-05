#!/usr/bin/env python3
"""Generate a thumbnail in isolation (no full video needed).

Usage:
    python3 scripts/make_thumbnail.py [--canal canal2] [--video-id 21]

Fetches video title/keywords from the database and runs the full viral
thumbnmail pipeline (style engine → brainstorm → Pollo AI + QC → composition).

Output: output/thumbnails/thumb_v2_{title_slug}.jpg
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

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("make_thumbnail")


def fetch_video_data(db_path: str, canal: str, video_id: int | None = None) -> dict:
    """Return {title, keywords, script_text} from the most recent video."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    if video_id:
        row = conn.execute(
            "SELECT * FROM videos WHERE id=? AND canal=? AND titulo_final IS NOT NULL",
            (video_id, canal),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT * FROM videos WHERE canal=? AND titulo_final IS NOT NULL AND titulo_final!='' "
            "ORDER BY id DESC LIMIT 1",
            (canal,),
        ).fetchone()

    if not row:
        conn.close()
        raise ValueError(f"No video found for canal={canal}")

    video = dict(row)

    # Keywords
    keywords = []
    tags_raw = video.get("tags_json")
    if tags_raw:
        try:
            keywords = json.loads(tags_raw) if isinstance(tags_raw, str) else tags_raw
        except json.JSONDecodeError:
            pass

    # Script text (via script_id)
    script_text = ""
    sid = video.get("script_id")
    if sid:
        srow = conn.execute("SELECT guion FROM scripts WHERE id=?", (sid,)).fetchone()
        if srow:
            script_text = srow["guion"] or ""

    conn.close()

    title = video.get("titulo_final", "Historia Impactante")
    return {
        "title": title,
        "keywords": keywords,
        "script_text": script_text,
        "video_id": video["id"],
        "canal": canal,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate thumbnail for a video in isolation")
    parser.add_argument("--canal", required=True, help="Channel slug")
    parser.add_argument("--video-id", type=int, default=None, help="Specific video ID (default: latest)")
    parser.add_argument("--no-style", action="store_true", help="Skip style engine (use raw defaults)")
    parser.add_argument("--title", default="", help="Override title (testing)")
    parser.add_argument("--overlay", default="", help="Override overlay text")
    args = parser.parse_args()

    db_path = PROJECT_ROOT / "autotube.db"

    # ── Load data ─────────────────────────────────────────────
    video_data = fetch_video_data(str(db_path), args.canal, args.video_id)
    title = args.title or video_data["title"]
    keywords = video_data["keywords"]
    script_text = video_data["script_text"]

    logger.info("Video #%s: %s", video_data["video_id"], title)
    logger.info("Keywords (%d): %s", len(keywords), keywords[:5] if keywords else "none")

    # ── Load channel config ───────────────────────────────────
    cfg = get_channel_config(args.canal)

    channel_display = getattr(cfg, "CANAL_DISPLAY_NAME", args.canal)
    channel_desc = getattr(cfg, "CHANNEL_ABOUT_SECTION", "")
    channel_theme = getattr(cfg, "CANAL_TAGLINE", "")

    logger.info("Channel: %s | Style: %s | Model: %s",
                 channel_display,
                 getattr(cfg, "THUMBNAIL_VISUAL_STYLE", "auto"),
                 getattr(cfg, "POLLO_IMAGE_MODEL", "default"))

    # ── Generate ──────────────────────────────────────────────
    maker = ThumbnailMaker(config=cfg)

    # Collect scene images (images only) for context — empty for this harness
    scene_images: list = []

    try:
        thumb_path = maker.make_viral_thumbnail(
            title=title,
            overlay_text=args.overlay,
            keywords=keywords,
            scene_images=scene_images,
            script_text=script_text[:1500] if script_text else "",
            canal_slug=args.canal,
            channel_display_name=channel_display,
            channel_description=channel_desc,
            channel_theme=channel_theme,
            video_id=video_data.get("video_id", 0),
        )
        logger.info("✅ Thumbnail generated: %s", thumb_path)
        return 0
    except Exception as exc:
        logger.exception("❌ Thumbnail generation failed: %s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
