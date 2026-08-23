"""Platform publish manager — orchestrates cross-platform video distribution.

Called after YouTube upload to dispatch the same video to Facebook, Rumble, TikTok.
Each upload is independent — one failure does not affect others.
"""

import asyncio
import logging
from typing import Optional

from api.services.publishers.base import (
    VideoMetadata,
    UploadResult,
    get_publisher,
)

logger = logging.getLogger(__name__)

# Map config platform keys to publisher platform ids
PLATFORM_MAP = {
    "facebook": "facebook",
    "rumble": "rumble",
    "tiktok": "tiktok",
    "dailymotion": "dailymotion",
}


class PlatformPublishManager:
    """Orchestrates cross-platform publishing after YouTube upload."""

    def __init__(self, canal: str, channel_id: int, db):
        self.canal = canal
        self.channel_id = channel_id
        self.db = db
        self._config = None

    def _get_config(self):
        """Load channel config with cross-platform settings."""
        if self._config is None:
            from config.config_bridge import get_channel_config
            self._config = get_channel_config(self.canal)
        return self._config

    def _get_upload_targets(self) -> dict[str, bool]:
        """Return {platform: enabled} from CROSS_PLATFORM_UPLOAD config."""
        cfg = self._get_config()
        upload_cfg = getattr(cfg, "CROSS_PLATFORM_UPLOAD", None)
        if not upload_cfg or not isinstance(upload_cfg, dict):
            return {}
        return {k: v for k, v in upload_cfg.items() if v}

    def _get_platform_settings(self, platform: str) -> dict:
        """Get per-platform settings from CROSS_PLATFORM_SETTINGS config."""
        cfg = self._get_config()
        settings = getattr(cfg, "CROSS_PLATFORM_SETTINGS", None)
        if not settings or not isinstance(settings, dict):
            return {}
        return settings.get(platform, {})

    async def publish_to_all(self, video_id: int, yt_video_id: str,
                              video_data: dict, metadata: dict) -> dict[str, UploadResult]:
        """Publish a video to all configured platforms in parallel.

        Args:
            video_id: DB videos.id
            yt_video_id: YouTube video ID (for cross-reference)
            video_data: dict with 'video_path' key (local MP4 path)
            metadata: dict with 'title', 'description', 'tags', 'thumbnail_path'

        Returns:
            {platform: UploadResult} — one entry per configured platform
        """
        targets = self._get_upload_targets()
        if not targets:
            logger.debug("[%s] No cross-platform upload targets configured", self.canal)
            return {}

        video_path = video_data.get("video_path", "") if video_data else ""
        if not video_path:
            logger.warning("[%s] No video_path in video_data — skipping cross-platform", self.canal)
            return {}

        yt_url = f"https://youtube.com/watch?v={yt_video_id}"

        # Build VideoMetadata for all platforms
        video_meta = VideoMetadata(
            video_path=video_path,
            title=metadata.get("title", ""),
            description=metadata.get("description", ""),
            tags=metadata.get("tags", []),
            thumbnail_path=metadata.get("thumbnail_path"),
            yt_video_url=yt_url,
            language="es",
        )

        results: dict[str, UploadResult] = {}

        # ── Facebook ─────────────────────────────────────
        if targets.get("facebook"):
            results["facebook"] = await self._publish_one(
                "facebook", video_id, video_meta,
            )

        # ── Rumble ───────────────────────────────────────
        if targets.get("rumble"):
            results["rumble"] = await self._publish_one(
                "rumble", video_id, video_meta,
            )

        # ── TikTok ───────────────────────────────────────
        if targets.get("tiktok"):
            results["tiktok"] = await self._publish_one(
                "tiktok", video_id, video_meta,
            )

        # ── Dailymotion ──────────────────────────────────
        if targets.get("dailymotion"):
            results["dailymotion"] = await self._publish_one(
                "dailymotion", video_id, video_meta,
            )

        return results

    async def _publish_one(self, platform: str, video_id: int,
                           video_meta: VideoMetadata) -> UploadResult:
        """Publish to a single platform with error handling and DB logging.

        Steps:
        1. Check if already published (platform_videos row exists & status='published')
        2. Create/update platform_videos row (status='uploading')
        3. Call publisher.upload()
        4. Update platform_videos row with result
        """
        # Step 1: Check existing
        existing = self.db.get_platform_video(video_id, platform)
        if existing and existing.get("status") == "published":
            logger.info("[%s] Already published to %s — skipping", self.canal, platform)
            return UploadResult(
                success=True, platform=platform,
                platform_video_id=existing.get("platform_video_id"),
                platform_video_url=existing.get("platform_video_url"),
                status="published",
            )

        # Step 2: Log start
        settings = self._get_platform_settings(platform)
        vid_meta = VideoMetadata(
            video_path=video_meta.video_path,
            title=video_meta.title,
            description=video_meta.description,
            tags=video_meta.tags,
            thumbnail_path=video_meta.thumbnail_path,
            privacy=settings.get("privacy", "public"),
            cross_reference_yt=settings.get("cross_reference_yt", True),
            yt_video_url=video_meta.yt_video_url,
            language="es",
        )

        row_id = self.db.create_platform_video(
            video_id=video_id,
            channel_id=self.channel_id,
            platform=platform,
            status="uploading",
            privacy=vid_meta.privacy,
        )
        self.db.update_platform_video(row_id, status="uploading")

        logger.info("[%s] Publishing to %s (row=%d)...", self.canal, platform, row_id)

        # Step 3: Upload
        try:
            publisher = get_publisher(platform)
            result = await publisher.upload(vid_meta)
        except ValueError as exc:
            # Publisher module not found / not yet implemented
            self.db.update_platform_video(
                row_id, status="failed",
                error_message=f"Publisher not available: {exc}",
                attempts=1,
            )
            return UploadResult(
                success=False, platform=platform,
                error=f"Publisher not available: {exc}",
            )
        except Exception as exc:
            logger.exception("[%s] Unexpected error publishing to %s", self.canal, platform)
            self.db.update_platform_video(
                row_id, status="failed",
                error_message=str(exc),
                attempts=1,
            )
            return UploadResult(
                success=False, platform=platform,
                error=str(exc),
            )

        # Step 4: Update DB
        if result.success:
            self.db.update_platform_video(
                row_id, status="published",
                platform_video_id=result.platform_video_id,
                platform_video_url=result.platform_video_url,
            )
            logger.info(
                "[%s] Published to %s: %s",
                self.canal, platform, result.platform_video_url,
            )
        else:
            self.db.update_platform_video(
                row_id, status="failed",
                platform_video_id=result.platform_video_id,
                platform_video_url=result.platform_video_url,
                error_message=result.error,
                attempts=1,
            )
            logger.warning(
                "[%s] Failed to publish to %s: %s",
                self.canal, platform, result.error,
            )

        return result

    async def publish_to_platform(self, video_id: int, platform: str,
                                   video_data: dict = None,
                                   metadata: dict = None) -> UploadResult:
        """Manual trigger: publish a single video to a single platform.

        This is called from the API (POST /api/videos/{id}/publish-to/{platform})
        when the user wants to manually publish or retry.
        """
        # Load video data from DB
        video = self.db.get_video(video_id)
        if not video:
            return UploadResult(
                success=False, platform=platform,
                error=f"Video {video_id} not found",
            )

        # Build metadata from video record
        import json
        video_path = video_data.get("video_path") if video_data else video.get("video_path", "")
        if not video_path:
            return UploadResult(
                success=False, platform=platform,
                error="Video file path not available (may have been deleted after upload)",
            )

        tags = []
        try:
            tags_json = video.get("tags_json", "[]")
            if tags_json:
                tags = json.loads(tags_json) if isinstance(tags_json, str) else tags_json
        except Exception:
            pass

        yt_url = video.get("yt_url", "") or (
            f"https://youtube.com/watch?v={video.get('yt_video_id', '')}"
            if video.get("yt_video_id") else ""
        )

        vid_meta = VideoMetadata(
            video_path=video_path,
            title=metadata.get("title") if metadata else video.get("titulo_final", ""),
            description=metadata.get("description") if metadata else video.get("description", ""),
            tags=metadata.get("tags") if metadata else tags,
            thumbnail_path=metadata.get("thumbnail_path") if metadata else video.get("thumbnail_path"),
            yt_video_url=yt_url,
            language="es",
        )

        return await self._publish_one(platform, video_id, vid_meta)

    def get_video_platform_status(self, video_id: int) -> list[dict]:
        """Get cross-platform status for a video (for UI)."""
        return self.db.get_platform_videos(video_id)
