"""Thumbnail service — regenerates YouTube thumbnails with viral CTR optimization.

Uses the v2 4-phase pipeline: Style Engine → Brainstorming → Pollo AI + QC → Composition.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from database.db_extended import ExtendedDatabase
from config.settings import OUTPUT_DIR


async def regenerate_thumbnail_for_video(video_id: int):
    """Regenerate a viral CTR-optimized thumbnail for an existing video."""
    db = ExtendedDatabase()
    v = db.get_video(video_id)
    if not v:
        return None

    try:
        from pipeline.thumbnail_maker import ThumbnailMaker
        from config.config_bridge import get_channel_config

        # Get channel config for style info
        canal = v.get("canal")
        if not canal:
            logger.warning("Video %d has no canal — cannot regenerate thumbnail", video_id)
            return None
        cfg = get_channel_config(canal)
        maker = ThumbnailMaker(cfg)
        titulo = v.get("titulo_final", "Historia Impactante")
        description = v.get("description", "")
        tags_raw = v.get("tags_json", "[]")
        
        if isinstance(tags_raw, str):
            try:
                tags = json.loads(tags_raw)
            except json.JSONDecodeError:
                tags = []
        else:
            tags = tags_raw or []
        
        # Generate viral thumbnail using v2 4-phase pipeline
        thumbnail_path = maker.make_viral_thumbnail(
            title=titulo,
            overlay_text="",
            keywords=tags[:5],
            scene_images=None,
            script_text=description[:1500],
            canal_slug=canal,
            channel_display_name=getattr(cfg, "CANAL_DISPLAY_NAME", ""),
            channel_description=getattr(cfg, "CHANNEL_ABOUT_SECTION", ""),
            channel_theme=getattr(cfg, "CANAL_TAGLINE", ""),
            video_id=video_id,
        )

        db.update_video(video_id, thumbnail_path=str(thumbnail_path))
        return str(thumbnail_path)
    except Exception as e:
        import logging
        logging.getLogger("autotube").error(f"Viral thumbnail regeneration failed: {e}")
        return None
