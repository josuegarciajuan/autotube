"""Video management router."""
import json
import logging
import asyncio
from datetime import datetime
from fastapi import APIRouter, HTTPException, BackgroundTasks
from api.utils import db_now
from api.deps import get_db
from api.progress import get_progress_manager
from api.schemas.models import (
    VideoGenerateRequest, VideoUpdate, VideoResponse,
    ScriptGenerateRequest, ScriptResponse, ContentResponse,
)
from api.services.generation_service import start_generation_job, _run_reassembly_job, start_upload_job
from api.services.generation_service import start_generation_job_subprocess, USE_SUBPROCESS_WORKER, _DISPATCH_LOCK

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("")
def list_videos(channel_id: int = None, status: str = None, limit: int = 50, offset: int = 0,
                playlist_id: int = None):
    db = get_db()
    videos = db.get_videos(channel_id=channel_id, status=status, limit=limit,
                            offset=offset, playlist_id=playlist_id)
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
    
    # ── Global dispatch lock: serialize all generation dispatches ──
    # Prevents TOCTOU race where two concurrent requests both pass
    # the guard checks before either creates a job.
    with _DISPATCH_LOCK:
        # Guard 1: don't start if this channel already has an active job
        active = db.get_active_job_for_channel(data.channel_id)
        if active:
            raise HTTPException(409, "Ya hay una generacion en curso para este canal. Espera a que termine.")
        
        # Guard 2: don't start if ANY generation is running globally
        if db.count_active_jobs() > 0:
            raise HTTPException(409, "Ya hay una generacion en curso en otro canal. Solo una a la vez.")
        
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
        db.update_job(job_id, status="running")  # close TOCTOU race window
    
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
                source_mode=data.source_mode or "original",
                viral_candidate_id=data.viral_candidate_id,
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
            source_mode=data.source_mode or "original",
            viral_candidate_id=data.viral_candidate_id,
        )
    
    return {"job_id": job_id, "video_id": video_id, "message": "Generation started", "source_mode": data.source_mode or "original"}


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
    db.update_job(job_id, status="running")  # close TOCTOU race window
    
    if USE_SUBPROCESS_WORKER:
        # Resume in subprocess mode: the spawned full_pipeline_worker.py
        # loads checkpoint_data and progress_phase from the DB and
        # skips already-completed phases (scrape/script/TTS).
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
    db.update_job(job_id, status="running")  # close TOCTOU race window
    
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
    db.update_job(job_id, status="running")  # close TOCTOU race window
    
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


# ═══════════════════════════════════════════════════════════════════
# Scheduled Publishing endpoints
# ═══════════════════════════════════════════════════════════════════

@router.patch("/{video_id}/manual-checklist")
def update_manual_checklist(video_id: int, data: dict):
    """Mark a manual action as done or undone.

    Body: {"item": "altered_content"|"end_screens", "done": true|false}
    """
    db = get_db()
    v = db.get_video(video_id)
    if not v:
        raise HTTPException(404, "Video not found")

    item = data.get("item", "")
    done = data.get("done", False)

    if item == "altered_content":
        db.update_video(video_id, manual_altered_content_done=int(done))
    elif item == "end_screens":
        db.update_video(video_id, manual_end_screens_done=int(done))
    else:
        raise HTTPException(400, f"Unknown manual checklist item: {item}")

    return {"ok": True, "item": item, "done": done}


@router.get("/manual-actions/pending")
def get_pending_manual_actions(channel_id: int = None):
    """Get aggregate count of pending manual actions, optionally filtered by channel."""
    db = get_db()
    import sqlite3
    conn = db._connect()
    try:
        params = []
        where = "WHERE manual_altered_content_done = 0 OR manual_end_screens_done = 0"
        if channel_id:
            where += " AND channel_id = ?"
            params.append(channel_id)

        rows = conn.execute(
            f"SELECT channel_id, "
            f"SUM(CASE WHEN manual_altered_content_done = 0 THEN 1 ELSE 0 END) as pending_altered, "
            f"SUM(CASE WHEN manual_end_screens_done = 0 THEN 1 ELSE 0 END) as pending_endscreens, "
            f"COUNT(*) as affected_videos "
            f"FROM videos {where} "
            f"AND yt_video_id IS NOT NULL "
            f"GROUP BY channel_id",
            params,
        ).fetchall()

        channels = {}
        total_pending = 0
        total_affected = 0
        for row in rows:
            ch = db.get_channel(row["channel_id"])
            channel_slug = ch["slug"] if ch else f"ch{row['channel_id']}"
            channel_name = ch["name"] if ch else "Unknown"
            pending = (row["pending_altered"] or 0) + (row["pending_endscreens"] or 0)
            channels[channel_slug] = {
                "channel_id": row["channel_id"],
                "channel_name": channel_name,
                "channel_slug": channel_slug,
                "pending_altered": row["pending_altered"] or 0,
                "pending_endscreens": row["pending_endscreens"] or 0,
                "total_pending": pending,
                "affected_videos": row["affected_videos"] or 0,
            }
            total_pending += pending
            total_affected += max(total_affected, row["affected_videos"] or 0)

        return {
            "total_pending": total_pending,
            "total_affected_videos": total_pending,
            "channels": channels,
        }
    finally:
        conn.close()


@router.get("/publications/upcoming")
def get_upcoming_publications(channel_id: int = None, days: int = 2):
    """Get upcoming scheduled publications (warming + scheduled status)."""
    db = get_db()
    base_query = (
        "SELECT v.*, ch.slug as channel_slug, ch.name as channel_name "
        "FROM videos v "
        "JOIN channels ch ON v.channel_id = ch.id "
        "WHERE v.status IN ('uploaded_private', 'warming', 'scheduled') "
        "AND v.target_public_at IS NOT NULL "
    )
    params = []
    if channel_id:
        base_query += "AND v.channel_id = ? "
        params.append(channel_id)

    base_query += "ORDER BY v.target_public_at ASC LIMIT 50"

    import sqlite3
    conn = db._connect()
    try:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(base_query, params).fetchall()
    finally:
        conn.close()

    result = []
    for r in rows:
        row = dict(r)
        # Calculate countdown
        remaining_seconds = 0
        if row.get("target_public_at"):
            try:
                target_dt = datetime.fromisoformat(str(row["target_public_at"]).replace("Z", "+00:00"))
                now = datetime.now(timezone.utc)
                remaining_seconds = max(0, (target_dt - now).total_seconds())
            except (ValueError, TypeError):
                pass

        # Determine sub-state
        if row.get("status") == "uploaded_private":
            # Check if still in warmup
            warmup_elapsed = True
            if remaining_seconds > 0:
                warmup_elapsed = False

        result.append({
            "video_id": row["id"],
            "channel_id": row["channel_id"],
            "channel_name": row.get("channel_name", ""),
            "channel_slug": row.get("channel_slug", ""),
            "titulo_final": row.get("titulo_final", ""),
            "status": row.get("status", ""),
            "target_public_at": row.get("target_public_at"),
            "peak_source": row.get("peak_source", ""),
            "published_at": row.get("published_at"),
            "auto_playlist_name": row.get("auto_playlist_name"),
            "remaining_seconds": int(remaining_seconds),
            "pending_altered": int(not row.get("manual_altered_content_done")),
            "pending_endscreens": int(not row.get("manual_end_screens_done")),
            "yt_video_id": row.get("yt_video_id"),
            "yt_url": row.get("yt_url"),
            "uploaded_at": str(row["uploaded_at"]) if row.get("uploaded_at") else None,
        })

    return result


@router.post("/{video_id}/publish-now")
def publish_video_now(video_id: int):
    """Immediately set a scheduled video to public, bypassing the schedule."""
    db = get_db()
    v = db.get_video(video_id)
    if not v:
        raise HTTPException(404, "Video not found")
    if not v.get("yt_video_id"):
        raise HTTPException(400, "Video has no YouTube ID")

    from orchestrator import PipelineOrchestrator
    ch = db.get_channel(v["channel_id"]) if v.get("channel_id") else None
    canal = ch["slug"] if ch else v.get("canal", "canal2")
    orch = PipelineOrchestrator(canal=canal)
    if not orch.uploader.authenticate():
        raise HTTPException(500, "Failed to authenticate")

    result = orch.uploader.set_privacy(v["yt_video_id"], "public")
    db.update_video(video_id, status="published", privacy_status="public",
                     published_at=db_now())
    return {"ok": True, "published": True, "video_id": video_id}


@router.post("/{video_id}/cancel-schedule")
def cancel_scheduled_publish(video_id: int):
    """Cancel scheduled publishing. Video stays private."""
    db = get_db()
    v = db.get_video(video_id)
    if not v:
        raise HTTPException(404, "Video not found")

    db.update_video(video_id, status="uploaded_private", target_public_at=None,
                     peak_source=None, publish_mode="immediate")
    return {"ok": True, "cancelled": True, "video_id": video_id}


@router.post("/{video_id}/force-retry")
def force_retry_video(video_id: int, background_tasks: BackgroundTasks):
    """Force-recover a video stuck in 'bug_crash' / permanent error.
    
    Bypasses the MAX_RECOVERY_ATTEMPTS guard and creates a reassembly job.
    Use when a video was marked bug_crash due to environmental failures
    (server restart, signal kill, OOM) rather than a code defect.
    """
    db = get_db()
    
    v = db.get_video(video_id)
    if not v:
        raise HTTPException(404, "Video not found")
    
    status = v.get("status", "")
    phase = v.get("progress_phase", "")
    
    if status not in ("error",) and phase not in ("bug_crash", "error"):
        raise HTTPException(
            400, 
            f"Video must be in 'error' or 'bug_crash' state. Current: status={status}, phase={phase}"
        )
    
    # Guard 1: don't start if this channel already has an active job
    active = db.get_active_job_for_channel(v.get("channel_id", 0))
    if active:
        raise HTTPException(409, "Ya hay una generacion en curso para este canal. Espera a que termine.")
    
    # Guard 2: don't start if ANY generation is running globally
    if db.count_active_jobs() > 0:
        raise HTTPException(409, "Ya hay una generacion en curso en otro canal. Solo una a la vez.")
    
    # Check we have checkpoint data to reassemble
    import json
    cp = v.get("checkpoint_data", "{}")
    try:
        checkpoint = json.loads(cp) if isinstance(cp, str) else (cp or {})
    except (json.JSONDecodeError, TypeError):
        raise HTTPException(400, "Invalid checkpoint data")
    
    if not checkpoint.get("tts") or not checkpoint.get("media"):
        raise HTTPException(400, "Checkpoint missing tts/media data — cannot reassemble")
    
    # Reset bug_crash flag to allow recovery
    db.update_video(video_id, status="error", progress_phase="error")
    
    # Create job — bypass MAX_RECOVERY_ATTEMPTS by direct insert
    job_id = db.create_job(v.get("channel_id", 0) or 1, "reassemble", video_id)
    db.update_job(job_id, status="running",
                  error_msg="Force-retry bypass (user-initiated)")
    
    background_tasks.add_task(
        _run_reassembly_job,
        job_id=job_id,
        video_id=video_id,
    )
    
    return {
        "job_id": job_id,
        "video_id": video_id,
        "message": "Force-retry recovery started. Previous phase was '{}'".format(phase),
    }
