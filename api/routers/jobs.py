"""Generation jobs router."""
from fastapi import APIRouter, HTTPException
from api.deps import get_db
import asyncio
import logging

logger = logging.getLogger(__name__)

router = APIRouter()


@router.post("/{job_id}/cancel")
async def cancel_job(job_id: int):
    """Cancel a running generation job and clean up generated files.
    
    1. Requests cooperative cancellation (orchestrator stop flag)
    2. Force-kills the worker subprocess if still alive
    3. Deletes generated MP4s, thumbnails, clips, and temp files
    4. Marks job as 'cancelled' and video as 'error' in DB
    """
    from api.services.generation_service import cancel_job as _cancel_cooperative
    from api.services.generation_service import force_cancel_and_cleanup
    db = get_db()
    job = db.get_job(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    if job["status"] not in ("running", "queued"):
        raise HTTPException(409, f"Job is already {job['status']}, cannot cancel")
    
    # Step 1: Cooperative cancellation
    cooperatively_cancelled = _cancel_cooperative(job_id)
    
    # Step 2: Force-kill worker + clean up files
    cleanup_result = {"killed_worker": False, "files_cleaned": [], "db_updated": False}
    video_id = job.get("video_id") or 0
    
    # Get channel slug for file cleanup
    channel_slug = ""
    if channel_id := job.get("channel_id"):
        ch = db.get_channel(channel_id)
        channel_slug = ch.get("slug", "") if ch else ""
    
    if job["status"] == "running" and (video_id or channel_slug):
        try:
            cleanup_result = await force_cancel_and_cleanup(
                job_id=job_id,
                video_id=video_id,
                channel_slug=channel_slug,
            )
        except Exception as exc:
            logger.error("Cleanup after cancel failed for job #%d: %s", job_id, exc)
    
    # Fallback: if cleanup didn't update DB, mark as failed
    if not cleanup_result["db_updated"] and job["status"] == "running":
        db.update_job(job_id, status="failed",
                      error_msg="Cancelled by user" if cooperatively_cancelled else "Cancelled (orchestrator not reachable)")
    
    return {
        "job_id": job_id,
        "cancelled": cooperatively_cancelled or cleanup_result.get("killed_worker", False),
        "cooperatively_cancelled": cooperatively_cancelled,
        "worker_killed": cleanup_result.get("killed_worker", False),
        "files_cleaned": cleanup_result.get("files_cleaned", []),
        "message": (
            "Job cancelled and files cleaned" if cleanup_result["files_cleaned"]
            else "Stop signal sent" if cooperatively_cancelled
            else "Job marked as failed (orchestrator not found)"
        ),
    }


@router.get("")
def list_jobs(status: str = None, channel_id: int = None, limit: int = 30):
    db = get_db()
    if status:
        import sqlite3
        with db._connect() as conn:
            q = "SELECT * FROM generation_jobs WHERE status = ?"
            params = [status]
            if channel_id:
                q += " AND channel_id = ?"
                params.append(channel_id)
            q += " ORDER BY created_at DESC LIMIT ?"
            params.append(limit)
            rows = conn.execute(q, params).fetchall()
        jobs = [dict(r) for r in rows]
    elif channel_id:
        jobs = db.get_channel_jobs(channel_id, limit)
    else:
        import sqlite3
        with db._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM generation_jobs ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        jobs = [dict(r) for r in rows]
    
    for j in jobs:
        for k in ("created_at", "started_at", "finished_at"):
            if j.get(k):
                j[k] = str(j[k])
    return jobs


@router.get("/active")
def active_jobs():
    db = get_db()
    jobs = db.get_active_jobs()
    for j in jobs:
        for k in ("created_at", "started_at", "finished_at"):
            if j.get(k):
                j[k] = str(j[k])
    return jobs


@router.get("/{job_id}")
def get_job(job_id: int):
    db = get_db()
    job = db.get_job(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    for k in ("created_at", "started_at", "finished_at"):
        if job.get(k):
            job[k] = str(job[k])
    # ── Attach progress detail counters from the video row ──
    # Lets the frontend polling fallback render upload MB / scene x/y
    # even when the WebSocket monitor is unavailable.
    if job.get("video_id"):
        try:
            v = db.get_video(job["video_id"])
            if v:
                job["progress_current"] = v.get("progress_current")
                job["progress_total"] = v.get("progress_total")
                job["phase"] = v.get("progress_phase") or job.get("phase")
                if v.get("progress") is not None:
                    job["progress"] = v.get("progress")
        except Exception:
            pass
    return job
