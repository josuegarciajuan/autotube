"""Generation jobs router."""
from fastapi import APIRouter, HTTPException
from api.deps import get_db

router = APIRouter()


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
