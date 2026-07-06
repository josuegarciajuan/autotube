"""Generation jobs router."""
from fastapi import APIRouter, HTTPException
from api.deps import get_db

router = APIRouter()


@router.post("/{job_id}/cancel")
def cancel_job(job_id: int):
    """Request cooperative cancellation of a running generation job.
    
    Sets the orchestrator's stop flag so it halts at the next safe checkpoint.
    The job status will transition to 'failed' once the orchestrator exits.
    """
    from api.services.generation_service import cancel_job as _cancel
    db = get_db()
    job = db.get_job(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    if job["status"] not in ("running", "queued"):
        raise HTTPException(409, f"Job is already {job['status']}, cannot cancel")
    
    # Try cooperative cancellation first
    cancelled = _cancel(job_id)
    
    # Also mark as failed in DB in case the orchestrator is already dead
    if job["status"] == "running":
        db.update_job(job_id, status="failed",
                      error_msg="Cancelled by user" if cancelled else "Cancelled (orchestrator not reachable)")
    
    return {
        "job_id": job_id,
        "cancelled": cancelled,
        "message": "Stop signal sent" if cancelled else "Job marked as failed (orchestrator not found)"
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
    return job
