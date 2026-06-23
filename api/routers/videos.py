"""Video management router."""
import json
import asyncio
from fastapi import APIRouter, HTTPException, BackgroundTasks
from api.deps import get_db
from api.progress import get_progress_manager
from api.schemas.models import (
    VideoGenerateRequest, VideoUpdate, VideoResponse,
    ScriptGenerateRequest, ScriptResponse, ContentResponse,
)
from api.services.generation_service import start_generation_job

router = APIRouter()


@router.get("")
def list_videos(channel_id: int = None, status: str = None, limit: int = 50, offset: int = 0):
    db = get_db()
    videos = db.get_videos(channel_id=channel_id, status=status, limit=limit, offset=offset)
    for v in videos:
        for k in ("created_at", "uploaded_at"):
            if v.get(k):
                v[k] = str(v[k])
    return videos


@router.get("/{video_id}")
def get_video(video_id: int):
    db = get_db()
    v = db.get_video(video_id)
    if not v:
        raise HTTPException(404, "Video not found")
    for k in ("created_at", "uploaded_at"):
        if v.get(k):
            v[k] = str(v[k])
    # Get scenes
    v["scenes"] = db.get_scenes(video_id)
    for s in v["scenes"]:
        for k in ("created_at", "updated_at"):
            if s.get(k):
                s[k] = str(s[k])
    return v


@router.put("/{video_id}")
def update_video(video_id: int, data: VideoUpdate):
    db = get_db()
    v = db.get_video(video_id)
    if not v:
        raise HTTPException(404, "Video not found")
    kwargs = {k: v for k, v in data.model_dump().items() if v is not None}
    db.update_video(video_id, **kwargs)
    return {"ok": True}


@router.delete("/{video_id}")
def delete_video(video_id: int):
    db = get_db()
    v = db.get_video(video_id)
    if not v:
        raise HTTPException(404, "Video not found")
    db.delete_video(video_id)
    return {"ok": True}


@router.post("/generate")
def generate_video(data: VideoGenerateRequest, background_tasks: BackgroundTasks):
    """Start video generation (async background job)."""
    db = get_db()
    ch = db.get_channel(data.channel_id)
    if not ch:
        raise HTTPException(404, "Channel not found")
    
    import sqlite3
    with db._connect() as conn:
        cursor = conn.execute(
            "INSERT INTO videos (canal, channel_id, status, progress, created_at, video_path) "
            "VALUES (?, ?, 'generating', 0, CURRENT_TIMESTAMP, 'pending')",
            (ch["slug"], data.channel_id),
        )
        conn.commit()
        video_id = cursor.lastrowid
    
    job_id = db.create_job(data.channel_id, data.action, video_id)
    
    background_tasks.add_task(
        start_generation_job,
        job_id=job_id,
        channel_id=data.channel_id,
        video_id=video_id,
        action=data.action,
        content_id=data.content_id,
    )
    
    return {"job_id": job_id, "video_id": video_id, "message": "Generation started"}


@router.post("/{video_id}/upload")
def upload_video(video_id: int, background_tasks: BackgroundTasks):
    """Upload (or re-upload) a video to YouTube."""
    db = get_db()
    v = db.get_video(video_id)
    if not v:
        raise HTTPException(404, "Video not found")
    if not v.get("video_path"):
        raise HTTPException(400, "Video has no file path")
    
    from api.services.generation_service import start_upload_job
    job_id = db.create_job(v["channel_id"] or 1, "upload_only", video_id)
    
    background_tasks.add_task(
        start_upload_job,
        job_id=job_id,
        video_id=video_id,
    )
    
    return {"job_id": job_id, "message": "Upload started"}


@router.post("/{video_id}/regenerate-thumbnail")
def regenerate_thumbnail(video_id: int, background_tasks: BackgroundTasks):
    """Regenerate video thumbnail."""
    db = get_db()
    v = db.get_video(video_id)
    if not v:
        raise HTTPException(404, "Video not found")
    if not v.get("video_path"):
        raise HTTPException(400, "No video file to extract thumbnail from")
    
    from api.services.thumbnail_service import regenerate_thumbnail_for_video
    background_tasks.add_task(regenerate_thumbnail_for_video, video_id)
    
    return {"message": "Thumbnail regeneration started"}


# ── Script routes ────────────────────────────────────────────

@router.post("/scripts/generate")
def generate_script(data: ScriptGenerateRequest, background_tasks: BackgroundTasks):
    """Generate a script from content."""
    db = get_db()
    ch = db.get_channel(data.channel_id)
    if not ch:
        raise HTTPException(404, "Channel not found")
    
    from pipeline.script_generator import ScriptGenerator
    from config import canal1_config as cfg
    
    # Use existing script generator
    content = db.get_script(data.content_id)  # reuse get_script to fetch content
    if not content:
        # Try raw_content
        import sqlite3
        with db._connect() as conn:
            row = conn.execute("SELECT * FROM raw_content WHERE id = ?", (data.content_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Content not found")
        content = dict(row)
    
    gen = ScriptGenerator(db, cfg)
    result = gen.generate(content)
    
    if not result:
        raise HTTPException(500, "Script generation failed")
    
    result["created_at"] = str(result.get("created_at", ""))
    return result


# ── Marketing routes ──────────────────────────────────────────

@router.post("/{video_id}/generate-metadata")
async def generate_marketing_metadata(video_id: int):
    """Generate viral-optimized titles, description, tags, and thumbnail text."""
    db = get_db()
    v = db.get_video(video_id)
    if not v:
        raise HTTPException(404, "Video not found")
    
    scenes = db.get_scenes(video_id)
    # Build script text from scenes
    script_text = "\n".join([
        s.get("script_text") or s.get("description") or ""
        for s in scenes
    ])
    if not script_text:
        # Try to get from associated script
        import sqlite3
        with db._connect() as conn:
            row = conn.execute(
                "SELECT guion, keywords_json FROM scripts WHERE id = ?",
                (v.get("script_id"),),
            ).fetchone()
        if row:
            script_text = row["guion"] or ""
            try:
                keywords = __import__("json").loads(row["keywords_json"] or "[]")
            except Exception:
                keywords = []
        else:
            script_text = v.get("titulo_final", "Historia impactante")
            keywords = []
    else:
        keywords = []
    
    from api.services.marketing_service import generate_marketing_content
    channel_name = v.get("channel_name", "")
    
    result = await generate_marketing_content(
        script_text=script_text[:3000],
        keywords=keywords,
        channel_name=channel_name,
    )
    
    return result


# ── YouTube Stats ──────────────────────────────────────────

@router.post("/stats")
def get_video_stats(data: dict):
    """Get YouTube stats for a list of video IDs.

    Tries real YouTube API first, falls back to mock data.
    """
    video_ids = data.get("video_ids", [])
    if not video_ids:
        return {}
    
    # Try to find a valid token from any channel
    from config.settings import TOKENS_DIR
    from pipeline.youtube_stats import YouTubeStatsFetcher
    import pickle
    
    token_slug = None
    for token_file in sorted(TOKENS_DIR.glob("*.pickle")):
        slug = token_file.stem
        if slug in ("canal1_state", "canal2_state"):
            continue
        token_slug = slug
        break
    
    result = {}
    if token_slug:
        fetcher = YouTubeStatsFetcher(token_slug)
        if fetcher.authenticate():
            for vid in video_ids:
                result[vid] = fetcher.get_video_stats(vid)
            return result
    
    # Fallback: mock
    for vid in video_ids:
        result[vid] = _mock_youtube_stats(vid)
    return result


def _mock_youtube_stats(video_id: str) -> dict:
    """Generate realistic mock stats based on video ID hash."""
    import hashlib
    if not video_id: return {"viewCount": "0", "likeCount": "0", "commentCount": "0"}
    h = int(hashlib.md5(video_id.encode()).hexdigest()[:8], 16)
    return {
        "viewCount": str(1000 + (h % 500000)),
        "likeCount": str(50 + (h % 25000)),
        "commentCount": str(5 + (h % 2000)),
    }


@router.get("/{video_id}/stats-history")
def get_video_stats_history(video_id: int, days: int = 30):
    """Get stats history for a video."""
    db = get_db()
    history = db.get_video_stats_history(video_id, days)
    return history
