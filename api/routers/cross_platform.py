"""Cross-platform publishing API router.

Endpoints for managing cross-platform video distribution:
- Config: enable/disable auto-upload per platform per channel
- Manual: publish/republish individual videos to specific platforms
- Status: view cross-platform publishing status per video
"""

import json
import logging

from fastapi import APIRouter, HTTPException

from api.deps import get_db
from api.schemas.models import (
    CrossPlatformConfigResponse,
    CrossPlatformConfigUpdate,
    PlatformVideoResponse,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["Cross-Platform"])


# ── Channel-level config ──────────────────────────────────


@router.get("/channels/{channel_id}/cross-platform-config")
def get_cross_platform_config(channel_id: int) -> CrossPlatformConfigResponse:
    """Read CROSS_PLATFORM_UPLOAD settings from the channel's config_json."""
    db = get_db()
    ch = db.get_channel(channel_id)
    if not ch:
        raise HTTPException(404, f"Channel {channel_id} not found")

    config_json = ch.get("config_json", "{}")
    if isinstance(config_json, str):
        try:
            config_json = json.loads(config_json)
        except json.JSONDecodeError:
            config_json = {}

    upload_config = config_json.get("CROSS_PLATFORM_UPLOAD", {})
    settings = config_json.get("CROSS_PLATFORM_SETTINGS", {})

    return CrossPlatformConfigResponse(
        facebook=upload_config.get("facebook", False),
        rumble=upload_config.get("rumble", False),
        tiktok=upload_config.get("tiktok", False),
        settings=settings,
    )


@router.put("/channels/{channel_id}/cross-platform-config")
def update_cross_platform_config(channel_id: int, data: CrossPlatformConfigUpdate):
    """Update CROSS_PLATFORM_UPLOAD in the channel's config_json."""
    db = get_db()
    ch = db.get_channel(channel_id)
    if not ch:
        raise HTTPException(404, f"Channel {channel_id} not found")

    config_json = ch.get("config_json", "{}")
    if isinstance(config_json, str):
        try:
            config_json = json.loads(config_json)
        except json.JSONDecodeError:
            config_json = {}

    # Update only provided fields
    upload_config = config_json.get("CROSS_PLATFORM_UPLOAD", {})
    if data.facebook is not None:
        upload_config["facebook"] = data.facebook
    if data.rumble is not None:
        upload_config["rumble"] = data.rumble
    if data.tiktok is not None:
        upload_config["tiktok"] = data.tiktok

    config_json["CROSS_PLATFORM_UPLOAD"] = upload_config
    db.update_channel(channel_id, config=config_json)

    return {"ok": True, "cross_platform_upload": upload_config}


# ── Video-level publishing ────────────────────────────────


@router.get("/videos/{video_id}/platform-status")
def get_video_platform_status(video_id: int) -> list[PlatformVideoResponse]:
    """Get cross-platform publishing status for a video."""
    db = get_db()
    rows = db.get_platform_videos(video_id)
    return [PlatformVideoResponse(**r) for r in rows]


@router.post("/videos/{video_id}/publish-to/{platform}")
def publish_to_platform(video_id: int, platform: str) -> PlatformVideoResponse:
    """Manually publish a video to a specific platform.

    Triggers an immediate upload using the configured credentials.
    Works even if CROSS_PLATFORM_UPLOAD is disabled for the platform
    (this is a manual override).
    """
    VALID = {"facebook", "rumble", "tiktok"}
    if platform not in VALID:
        raise HTTPException(400, f"Invalid platform '{platform}'. Valid: {VALID}")

    db = get_db()
    video = db.get_video(video_id)
    if not video:
        raise HTTPException(404, f"Video {video_id} not found")

    channel_id = video.get("channel_id")
    if not channel_id:
        raise HTTPException(400, f"Video {video_id} has no channel_id")

    ch = db.get_channel(channel_id)
    if not ch:
        raise HTTPException(404, f"Channel {channel_id} not found")

    # Check if account exists
    accounts = db.get_enabled_social_accounts(channel_id)
    account = next((a for a in accounts if a["platform"] == platform), None)
    if not account:
        raise HTTPException(400, f"No enabled {platform} account for channel {ch.get('name', channel_id)}")

    # Check video file
    video_path = video.get("video_path", "")
    import os as _os
    if not video_path or not _os.path.exists(video_path):
        raise HTTPException(400, "Video file not available (may have been deleted after upload). "
                                 "This publish requires the local MP4 file to exist.")

    # Trigger publish
    import asyncio as _asyncio
    from api.services.publishers.platform_manager import PlatformPublishManager

    try:
        import json as _json
        tags = []
        try:
            tags_json = video.get("tags_json", "[]")
            if tags_json:
                tags = _json.loads(tags_json) if isinstance(tags_json, str) else tags_json
        except Exception:
            pass

        yt_url = video.get("yt_url", "") or (
            f"https://youtube.com/watch?v={video.get('yt_video_id', '')}"
            if video.get("yt_video_id") else ""
        )

        mgr = PlatformPublishManager(ch.get("slug", ""), channel_id, db)
        result = _asyncio.run(
            mgr.publish_to_platform(
                video_id=video_id,
                platform=platform,
                video_data={"video_path": video_path},
                metadata={
                    "title": video.get("titulo_final", ""),
                    "description": video.get("description", ""),
                    "tags": tags,
                    "thumbnail_path": video.get("thumbnail_path"),
                },
            )
        )
    except Exception as exc:
        logger.exception("Manual publish to %s failed", platform)
        raise HTTPException(500, f"Publish failed: {exc}")

    # Return updated record
    record = db.get_platform_video(video_id, platform)
    if not record:
        raise HTTPException(500, "Platform video record not found after publish")
    return PlatformVideoResponse(**record)


@router.post("/videos/{video_id}/republish-to/{platform}")
def republish_to_platform(video_id: int, platform: str) -> PlatformVideoResponse:
    """Retry a failed cross-platform publish."""
    VALID = {"facebook", "rumble", "tiktok"}
    if platform not in VALID:
        raise HTTPException(400, f"Invalid platform '{platform}'. Valid: {VALID}")

    db = get_db()
    existing = db.get_platform_video(video_id, platform)
    if existing and existing.get("status") == "published":
        return PlatformVideoResponse(**existing)

    # Reset and retry
    if existing:
        db.update_platform_video(existing["id"], status="uploading", error_message="")
    return publish_to_platform(video_id, platform)


# ── Channel-level stats ────────────────────────────────────


@router.get("/channels/{channel_id}/platform-stats")
def get_channel_platform_stats(channel_id: int) -> list[dict]:
    """Get aggregate cross-platform publish stats for a channel."""
    db = get_db()
    return db.get_channel_platform_stats(channel_id)
