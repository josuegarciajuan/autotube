"""Video management router."""
import json
import logging
import asyncio
from fastapi import APIRouter, HTTPException, BackgroundTasks
from api.deps import get_db
from api.progress import get_progress_manager
from api.schemas.models import (
    VideoGenerateRequest, VideoUpdate, VideoResponse,
    ScriptGenerateRequest, ScriptResponse, ContentResponse,
)
from api.services.generation_service import start_generation_job, _run_reassembly_job, start_upload_job
from api.services.generation_service import start_generation_job_subprocess, USE_SUBPROCESS_WORKER

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("")
def list_videos(channel_id: int = None, status: str = None, limit: int = 50, offset: int = 0):
    db = get_db()
    videos = db.get_videos(channel_id=channel_id, status=status, limit=limit, offset=offset)
    for v in videos:
        for k in ("created_at", "uploaded_at"):
            if v.get(k):
                v[k] = str(v[k])
        # Deserialize timing_data from JSON string
        if v.get("timing_data") and isinstance(v["timing_data"], str):
            try:
                v["timing_data"] = json.loads(v["timing_data"])
            except (json.JSONDecodeError, TypeError):
                v["timing_data"] = None
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
    # Deserialize timing_data from JSON string
    if v.get("timing_data") and isinstance(v["timing_data"], str):
        try:
            v["timing_data"] = json.loads(v["timing_data"])
        except (json.JSONDecodeError, TypeError):
            v["timing_data"] = None
    # Get scenes
    v["scenes"] = db.get_scenes(video_id)
    for s in v["scenes"]:
        for k in ("created_at", "updated_at"):
            if s.get(k):
                s[k] = str(s[k])
    # Include embeddable status from latest YouTube stats
    latest_stats = db.get_video_latest_stats(video_id)
    v["embeddable"] = bool(latest_stats.get("embeddable", 1)) if latest_stats else True
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
async def generate_video(data: VideoGenerateRequest, background_tasks: BackgroundTasks):
    """Start video generation (async background job).
    
    Rejected with 409 if another generation is already running.
    """
    db = get_db()
    
    ch = db.get_channel(data.channel_id)
    if not ch:
        raise HTTPException(404, "Channel not found")
    
    # Guard: don't start if this channel already has an active job
    active = db.get_active_job_for_channel(data.channel_id)
    if active:
        raise HTTPException(409, "Ya hay una generacion en curso para este canal. Espera a que termine.")
    
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
    
    if USE_SUBPROCESS_WORKER:
        # Spawn independent worker subprocess (survives API restarts)
        asyncio.create_task(
            start_generation_job_subprocess(
                job_id=job_id,
                channel_id=data.channel_id,
                video_id=video_id,
                action=data.action,
                test_mode=data.test_mode,
                upload=data.upload,
            )
        )
    else:
        # Legacy in-process generation
        background_tasks.add_task(
            start_generation_job,
            job_id=job_id,
            channel_id=data.channel_id,
            video_id=video_id,
            action=data.action,
            content_id=data.content_id,
            test_mode=data.test_mode,
            upload=data.upload,
        )
    
    return {"job_id": job_id, "video_id": video_id, "message": "Generation started"}


@router.post("/{video_id}/resume")
async def resume_video(video_id: int, background_tasks: BackgroundTasks):
    """Resume video generation from the last completed phase.
    
    Loads checkpoint data from the DB and skips already-completed
    phases. Useful when a generation job fails late in the pipeline
    (e.g. video assembly) — no need to regenerate TTS or media.
    
    Rejected with 409 if another generation is already running.
    """
    db = get_db()
    
    v = db.get_video(video_id)
    if not v:
        raise HTTPException(404, "Video not found")
    
    channel_id = v.get("channel_id")
    if not channel_id:
        raise HTTPException(400, "Video has no channel_id")
    
    # Guard: don't start if this channel already has an active job
    active = db.get_active_job_for_channel(channel_id)
    if active:
        raise HTTPException(409, "Ya hay una generacion en curso para este canal. Espera a que termine.")
    
    # Guard: reject resume if video was already uploaded to YouTube.
    # A new generation would overwrite the existing upload status via the
    # orphan-detector / zombie-thread race condition.
    if v.get("yt_video_id"):
        raise HTTPException(
            409,
            f"Este video ya fue subido a YouTube (ID: {v['yt_video_id']}). "
            "Usa el boton Resubir si necesitas reemplazarlo."
        )
    
    # Check if there's checkpoint data to resume from
    checkpoint_raw = v.get("checkpoint_data", "{}")
    try:
        checkpoint = json.loads(checkpoint_raw) if isinstance(checkpoint_raw, str) else (checkpoint_raw or {})
    except (json.JSONDecodeError, TypeError):
        checkpoint = {}
    
    if not checkpoint:
        raise HTTPException(400, "No checkpoint data — start a fresh generation instead")
    
    last_phase = v.get("progress_phase", "")
    logger.info(f"Resuming video {video_id} from phase '{last_phase}' (checkpoint keys: {list(checkpoint.keys())})")
    
    # Reset status to generating
    db.update_video(video_id, status="generating")
    
    job_id = db.create_job(channel_id, "generate_and_upload", video_id)
    
    if USE_SUBPROCESS_WORKER:
        # Note: resume in subprocess mode = full regeneration
        # The worker runs run_full_pipeline which doesn't support partial resume.
        # This is acceptable because the subprocess worker is more robust.
        asyncio.create_task(
            start_generation_job_subprocess(
                job_id=job_id,
                channel_id=channel_id,
                video_id=video_id,
                action="generate_and_upload",
            )
        )
    else:
        background_tasks.add_task(
            start_generation_job,
            job_id=job_id,
            channel_id=channel_id,
            video_id=video_id,
            action="generate_and_upload",
            resume=True,  # ← triggers checkpoint reload
        )
    
    return {
        "job_id": job_id,
        "video_id": video_id,
        "message": f"Resuming from phase '{last_phase}'",
        "last_phase": last_phase,
    }


@router.post("/{video_id}/reassemble")
def reassemble_video(video_id: int, background_tasks: BackgroundTasks):
    """Re-assemble a video from existing checkpoint data.

    Skips scrape/script/tts/media — goes straight to ``VideoEditor.build_video()``
    using the saved checkpoint. Useful when the original render crashed.
    """
    db = get_db()
    
    v = db.get_video(video_id)
    if not v:
        raise HTTPException(404, "Video not found")
    if v.get("status") not in ("error", "ready", "assembled", "reassembling"):
        raise HTTPException(400, "Video must be in error/ready/assembled state")
    
    # Guard: don't start if this channel already has an active job
    active = db.get_active_job_for_channel(v.get("channel_id", 0))
    if active:
        raise HTTPException(409, "Ya hay una generacion en curso para este canal. Espera a que termine.")
    
    # Check we have checkpoint data
    import json
    cp = v.get("checkpoint_data", "{}")
    try:
        checkpoint = json.loads(cp) if isinstance(cp, str) else (cp or {})
    except (json.JSONDecodeError, TypeError):
        raise HTTPException(400, "Invalid checkpoint data")
    
    if not checkpoint.get("tts") or not checkpoint.get("media"):
        raise HTTPException(400, "Checkpoint missing tts/media data — cannot reassemble")
    
    job_id = db.create_job(v["channel_id"] or 1, "reassemble", video_id)
    
    background_tasks.add_task(
        _run_reassembly_job,
        job_id=job_id,
        video_id=video_id,
    )
    
    return {
        "job_id": job_id,
        "video_id": video_id,
        "message": "Reassembly started",
    }


@router.post("/{video_id}/upload")
def upload_video(video_id: int, background_tasks: BackgroundTasks):
    """Upload (or re-upload) a video to YouTube."""
    db = get_db()
    
    v = db.get_video(video_id)
    if not v:
        raise HTTPException(404, "Video not found")
    if not v.get("video_path"):
        raise HTTPException(400, "Video has no file path")
    
    # Guard: don't start if this channel already has an active job
    active = db.get_active_job_for_channel(v.get("channel_id", 0))
    if active:
        raise HTTPException(409, "Ya hay una generacion en curso para este canal. Espera a que termine.")
    
    from api.services.generation_service import start_upload_job
    job_id = db.create_job(v["channel_id"] or 1, "upload_only", video_id)
    
    background_tasks.add_task(
        start_upload_job,
        job_id=job_id,
        video_id=video_id,
    )
    
    return {"job_id": job_id, "message": "Upload started"}


@router.put("/{video_id}/privacy")
def set_video_privacy(video_id: int, data: dict):
    """Update the privacy status of an already uploaded YouTube video.
    
    Body: {"privacy_status": "public"|"unlisted"|"private"}
    """
    db = get_db()
    v = db.get_video(video_id)
    if not v:
        raise HTTPException(404, "Video not found")
    if not v.get("yt_video_id"):
        raise HTTPException(400, "Video has no YouTube ID — upload it first")
    
    privacy_new = data.get("privacy_status", "")
    if privacy_new not in ("public", "unlisted", "private"):
        raise HTTPException(400, "privacy_status must be 'public', 'unlisted', or 'private'")
    
    channel_id = v.get("channel_id") or 1
    ch = db.get_channel(channel_id)
    canal = ch["slug"] if ch else v.get("canal", "canal2")
    
    from orchestrator import PipelineOrchestrator
    orch = PipelineOrchestrator(canal=canal)
    
    if not orch.uploader.authenticate():
        raise HTTPException(500, "Failed to authenticate with YouTube")
    
    try:
        result = orch.uploader.set_privacy(v["yt_video_id"], privacy_new)
    except Exception as e:
        raise HTTPException(500, f"YouTube API error: {e}")
    
    # Update the privacy_status in our database too
    db.update_video(video_id, privacy_status=privacy_new)
    
    return {
        "updated": True,
        "yt_video_id": v["yt_video_id"],
        "privacy": privacy_new,
        "yt_url": v.get("yt_url", f"https://youtube.com/watch?v={v['yt_video_id']}"),
    }


@router.post("/{video_id}/regenerate-thumbnail")
def regenerate_thumbnail(video_id: int, background_tasks: BackgroundTasks):
    """Regenerate video thumbnail."""
    db = get_db()
    v = db.get_video(video_id)
    if not v:
        raise HTTPException(404, "Video not found")
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
    from config import canal2_config as cfg
    
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
    """Get video stats for a list of YouTube video IDs from DB cache.

    Ya no consume YouTube API quota — siempre lee de video_stats_history.
    Para refrescar los datos, usar el botón 'Recolectar stats' del dashboard.
    """
    video_ids = data.get("video_ids", [])
    if not video_ids:
        return {}

    db = get_db()
    result = {}
    for vid in video_ids:
        # 1) Long-form videos table
        with db._connect() as conn:
            vrow = conn.execute(
                "SELECT id FROM videos WHERE yt_video_id = ? LIMIT 1",
                (vid,),
            ).fetchone()
        if vrow:
            db_stats = db.get_video_latest_stats(vrow["id"])
            if db_stats:
                result[vid] = {
                    "viewCount": str(db_stats.get("views", 0)),
                    "likeCount": str(db_stats.get("likes", 0)),
                    "commentCount": str(db_stats.get("comments", 0)),
                    "embeddable": bool(db_stats.get("embeddable", 1)),
                    "_from_db": True,
                }
                continue

        # 2) Fallback: shorts table (YouTube ID stored in shorts.youtube_id)
        with db._connect() as conn:
            srow = conn.execute(
                "SELECT id FROM shorts WHERE youtube_id = ? LIMIT 1",
                (vid,),
            ).fetchone()
            if srow:
                sstats = conn.execute(
                    "SELECT views, likes, comments, embeddable FROM short_stats "
                    "WHERE short_id = ? ORDER BY fetched_at DESC LIMIT 1",
                    (srow["id"],),
                ).fetchone()
                if sstats:
                    result[vid] = {
                        "viewCount": str(sstats["views"]),
                        "likeCount": str(sstats["likes"]),
                        "commentCount": str(sstats["comments"]),
                        "embeddable": bool(sstats["embeddable"]),
                        "_from_db": True,
                    }
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
