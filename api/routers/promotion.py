"""Promotion API router — Video lifecycle, playlists, comments, metadata.

Endpoints for managing post-upload promotion:
  - Playlist sync and video assignment
  - Lifecycle action viewing and manual triggering
  - First comment posting and reply automation
  - Metadata reoptimization
"""

import json
import logging
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from api.deps import get_db
from database.db_extended import ExtendedDatabase

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["Promotion"])

# ═══════════════════════════════════════════════════════════════
# Schemas
# ═══════════════════════════════════════════════════════════════

class TriggerActionRequest(BaseModel):
    action_type: str  # playlist_add | first_comment | comment_reply_1 | comment_reply_2 | ctr_check | metadata_reoptimize

class LifecycleActionOut(BaseModel):
    id: int
    video_id: int
    action_type: str
    scheduled_for: Optional[str]
    executed_at: Optional[str]
    status: str
    result_json: Optional[str]
    error_message: Optional[str]
    retry_count: int
    config_json: Optional[str]

    class Config:
        from_attributes = True

class CommentResult(BaseModel):
    yt_comment_id: Optional[str] = None
    text: Optional[str] = None
    pinned_required: bool = True
    skipped: Optional[bool] = None
    reason: Optional[str] = None

class PlaylistOut(BaseModel):
    id: int
    channel_id: int
    slug: str
    name: Optional[str]
    yt_playlist_id: str
    playlist_type: str

class PlaylistSyncResult(BaseModel):
    created: list
    existing: list
    errors: list


# ═══════════════════════════════════════════════════════════════
# Channel-level: Playlists
# ═══════════════════════════════════════════════════════════════

@router.get("/channels/{channel_id}/playlists")
async def get_channel_playlists(channel_id: int):
    """Get all YouTube playlists for a channel (cached from DB)."""
    db = get_db()
    playlists = db.get_channel_youtube_playlists(channel_id)
    return playlists


@router.post("/channels/{channel_id}/playlists/sync")
async def sync_channel_playlists(channel_id: int):
    """Sync playlists from channel config to YouTube (create missing ones)."""
    db = get_db()
    ch = db.get_channel(channel_id)
    if not ch:
        raise HTTPException(404, "Channel not found")

    slug = ch.get("slug")
    if not slug:
        raise HTTPException(400, "Channel has no slug")

    from pipeline.youtube_playlists import YouTubePlaylistManager
    mgr = YouTubePlaylistManager(slug)

    if not mgr.authenticate():
        raise HTTPException(400, "Channel not authenticated with YouTube")

    result = mgr.sync_playlists_from_config()

    # Cache in DB
    for pl in result.get("created", []):
        db.upsert_youtube_playlist(channel_id, pl["slug"], pl["yt_playlist_id"], pl["name"])
    for pl in result.get("existing", []):
        db.upsert_youtube_playlist(channel_id, pl["slug"], pl["yt_playlist_id"], pl["name"])

    return result


@router.get("/channels/{channel_id}/playlists/diagnose")
async def diagnose_channel_playlists(channel_id: int, limit: int = 20):
    """Diagnose playlist assignment health for a channel's recent videos.

    For each recent video, checks:
      - Has target_playlist_slug set on the video record
      - Has entries in the video_playlists join table
      - Whether the target slug exists in youtube_playlists cache
      - Returns a summary of assignment health
    """
    db = get_db()
    ch = db.get_channel(channel_id)
    if not ch:
        raise HTTPException(404, "Channel not found")

    slug = ch.get("slug")
    playlists_cached = db.get_channel_youtube_playlists(channel_id)

    # Get recent videos
    all_videos = db.get_videos(channel_id=channel_id, limit=limit)

    videos_report = []
    assigned_count = 0
    unassigned_count = 0
    slug_not_in_cache = 0

    for v in all_videos:
        vid = v.get("id")
        tgt_slug = v.get("target_playlist_slug")
        tgt_pl_id = v.get("target_playlist_id")
        yt_vid = v.get("yt_video_id")
        status = v.get("status", "")

        assignments = db.get_video_playlists_db(vid) if vid else []
        has_db_assignment = len(assignments) > 0

        # Check if target slug exists in youtube_playlists cache
        slug_in_cache = False
        if tgt_slug:
            for pl in playlists_cached:
                if pl.get("slug") == tgt_slug:
                    slug_in_cache = True
                    break

        if has_db_assignment:
            assigned_count += 1
        elif tgt_slug and yt_vid:
            unassigned_count += 1
            if not slug_in_cache:
                slug_not_in_cache += 1

        videos_report.append({
            "video_id": vid,
            "yt_video_id": yt_vid,
            "status": status,
            "target_playlist_slug": tgt_slug,
            "target_playlist_id": tgt_pl_id,
            "slug_in_cache": slug_in_cache,
            "has_db_assignment": has_db_assignment,
            "db_assignments": [
                {
                    "playlist_slug": a.get("playlist_slug"),
                    "playlist_name": a.get("playlist_name"),
                    "yt_playlist_item_id": a.get("yt_playlist_item_id"),
                    "added_at": str(a.get("added_at", "")),
                }
                for a in assignments
            ],
        })

    return {
        "channel_id": channel_id,
        "channel_slug": slug,
        "playlists_in_cache": len(playlists_cached),
        "playlist_slugs_cached": [p.get("slug") for p in playlists_cached],
        "recent_videos": len(videos_report),
        "assigned_to_playlist": assigned_count,
        "unassigned_to_playlist": unassigned_count,
        "slug_not_in_cache": slug_not_in_cache,
        "health": "healthy" if assigned_count == len(videos_report) or unassigned_count == 0
                  else ("warning" if unassigned_count < len(videos_report) / 2 else "critical"),
        "videos": videos_report,
    }


# ═══════════════════════════════════════════════════════════════
# Video-level: Playlists
# ═══════════════════════════════════════════════════════════════

@router.get("/videos/{video_id}/playlists")
async def get_video_playlists(video_id: int):
    """Get which playlists a video has been added to."""
    db = get_db()
    assignments = db.get_video_playlists_db(video_id)
    return assignments


@router.post("/videos/{video_id}/add-to-playlists")
async def add_video_to_playlists(video_id: int):
    """Add a video to all configured playlists for its channel."""
    db = get_db()
    video = db.get_video(video_id)
    if not video:
        raise HTTPException(404, "Video not found")

    yt_video_id = video.get("yt_video_id")
    if not yt_video_id:
        raise HTTPException(400, "Video not uploaded to YouTube yet")

    channel_id = video.get("channel_id")
    if not channel_id:
        raise HTTPException(400, "Video has no channel assigned")

    ch = db.get_channel(channel_id)
    if not ch:
        raise HTTPException(404, "Channel not found")

    slug = ch.get("slug")
    if not slug:
        raise HTTPException(400, "Channel has no slug")

    from pipeline.youtube_playlists import YouTubePlaylistManager
    mgr = YouTubePlaylistManager(slug)

    if not mgr.authenticate():
        raise HTTPException(400, "Channel not authenticated with YouTube")

    # Sync first
    sync_result = mgr.sync_playlists_from_config()
    for pl in sync_result.get("created", []):
        db.upsert_youtube_playlist(channel_id, pl["slug"], pl["yt_playlist_id"], pl["name"])
    for pl in sync_result.get("existing", []):
        db.upsert_youtube_playlist(channel_id, pl["slug"], pl["yt_playlist_id"], pl["name"])

    # Add video
    result = mgr.add_video_to_all_playlists(yt_video_id)

    # Record in DB
    for slug_key in result.get("added_to", []):
        cached = db.get_playlist_by_slug(channel_id, slug_key)
        if cached:
            db.add_video_to_playlist_db(video_id, cached["id"])
    for slug_key in result.get("already_in", []):
        cached = db.get_playlist_by_slug(channel_id, slug_key)
        if cached:
            db.add_video_to_playlist_db(video_id, cached["id"])

    return result


# ═══════════════════════════════════════════════════════════════
# Video-level: Lifecycle
# ═══════════════════════════════════════════════════════════════

@router.get("/videos/{video_id}/lifecycle")
async def get_video_lifecycle(video_id: int):
    """Get all lifecycle actions for a video."""
    db = get_db()
    actions = db.get_video_lifecycle_actions(video_id)
    return actions


@router.post("/videos/{video_id}/lifecycle/trigger")
async def trigger_lifecycle_action(video_id: int, req: TriggerActionRequest):
    """Manually trigger a lifecycle action for a video."""
    db = get_db()
    video = db.get_video(video_id)
    if not video:
        raise HTTPException(404, "Video not found")

    yt_video_id = video.get("yt_video_id")
    if not yt_video_id:
        raise HTTPException(400, "Video not uploaded to YouTube yet")

    channel_id = video.get("channel_id")
    ch = db.get_channel(channel_id) if channel_id else None
    slug = ch.get("slug") if ch else None
    if not slug:
        raise HTTPException(400, "Video has no channel slug")

    from pipeline.video_lifecycle import VideoLifecycleManager
    mgr = VideoLifecycleManager(slug)

    result = mgr.trigger_action_manual(
        video_id=video_id,
        action_type=req.action_type,
        yt_video_id=yt_video_id,
        channel_id=channel_id,
    )
    return result


@router.get("/channels/{channel_id}/lifecycle/recent")
async def get_channel_lifecycle_recent(channel_id: int, limit: int = 30):
    """Get recent lifecycle actions for a channel."""
    db = get_db()
    actions = db.get_channel_latest_lifecycle(channel_id, limit)
    return actions


# ═══════════════════════════════════════════════════════════════
# Video-level: Comments
# ═══════════════════════════════════════════════════════════════

@router.post("/videos/{video_id}/post-first-comment")
async def post_first_comment(video_id: int):
    """Post an engaging first comment on a YouTube video."""
    db = get_db()
    video = db.get_video(video_id)
    if not video:
        raise HTTPException(404, "Video not found")

    yt_video_id = video.get("yt_video_id")
    if not yt_video_id:
        raise HTTPException(400, "Video not uploaded to YouTube yet")

    channel_id = video.get("channel_id")
    ch = db.get_channel(channel_id) if channel_id else None
    slug = ch.get("slug") if ch else None
    if not slug:
        raise HTTPException(400, "Video has no channel slug")

    from pipeline.youtube_comments import YouTubeCommentManager
    mgr = YouTubeCommentManager(slug)

    if not mgr.authenticate():
        raise HTTPException(400, "Channel not authenticated with YouTube")

    result = mgr.post_first_comment(yt_video_id, db_video_id=video_id)

    if result.get("skipped"):
        return CommentResult(skipped=True, reason=result.get("reason"))

    if result.get("yt_comment_id"):
        db.log_comment(video_id, yt_video_id, result["yt_comment_id"],
                       comment_type="first", comment_text=result.get("text"))
        return CommentResult(
            yt_comment_id=result["yt_comment_id"],
            text=result.get("text"),
            pinned_required=True,
        )

    raise HTTPException(500, "Failed to post first comment")


@router.post("/videos/{video_id}/reply-comments")
async def reply_to_comments(video_id: int):
    """Reply to unanswered viewer comments on a video."""
    db = get_db()
    video = db.get_video(video_id)
    if not video:
        raise HTTPException(404, "Video not found")

    yt_video_id = video.get("yt_video_id")
    if not yt_video_id:
        raise HTTPException(400, "Video not uploaded to YouTube yet")

    channel_id = video.get("channel_id")
    ch = db.get_channel(channel_id) if channel_id else None
    slug = ch.get("slug") if ch else None
    if not slug:
        raise HTTPException(400, "Video has no channel slug")

    from pipeline.youtube_comments import YouTubeCommentManager
    from config.settings import COMMENT_REPLY_MAX_PER_VIDEO

    mgr = YouTubeCommentManager(slug)

    if not mgr.authenticate():
        raise HTTPException(400, "Channel not authenticated with YouTube")

    result = mgr.reply_to_comments(yt_video_id, COMMENT_REPLY_MAX_PER_VIDEO, video_id)
    return result


# ═══════════════════════════════════════════════════════════════
# Video-level: Metadata Reoptimization
# ═══════════════════════════════════════════════════════════════

@router.post("/videos/{video_id}/reoptimize-metadata")
async def reoptimize_metadata(video_id: int):
    """Re-optimize video title, description, and tags via LLM."""
    db = get_db()
    video = db.get_video(video_id)
    if not video:
        raise HTTPException(404, "Video not found")

    yt_video_id = video.get("yt_video_id")
    if not yt_video_id:
        raise HTTPException(400, "Video not uploaded to YouTube yet")

    current_title = video.get("titulo_final", "")
    current_description = video.get("description", "")
    if not current_title:
        raise HTTPException(400, "Video has no title")

    channel_id = video.get("channel_id")
    ch = db.get_channel(channel_id) if channel_id else None
    slug = ch.get("slug") if ch else None
    if not slug:
        raise HTTPException(400, "Video has no channel slug")

    # Try to get script text from the video's script record
    script_text = ""
    try:
        with db._connect() as conn:
            row = conn.execute(
                "SELECT s.texto_completo FROM scripts s "
                "JOIN videos v ON v.script_id = s.id "
                "WHERE v.id = ?", (video_id,)
            ).fetchone()
            if row:
                script_text = row["texto_completo"] or ""
    except Exception:
        pass

    from pipeline.metadata_optimizer import MetadataOptimizer
    optimizer = MetadataOptimizer(slug)

    if not optimizer.authenticate():
        raise HTTPException(400, "Channel not authenticated with YouTube")

    result = optimizer.run_full_optimization(
        yt_video_id, script_text,
        current_title, current_description,
    )

    if result and "error" not in result:
        # Update local DB with new title
        if result.get("new_title"):
            db.update_video(video_id, titulo_final=result["new_title"])
        return {"success": True, **result}
    elif result:
        return {"success": False, "error": result.get("error"), "optimized_metadata": result.get("optimized_metadata")}
    else:
        raise HTTPException(500, "Failed to reoptimize metadata")
