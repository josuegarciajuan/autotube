"""Dashboard router — unified data for the main dashboard."""
from fastapi import APIRouter
from api.deps import get_db

router = APIRouter()


@router.get("/dashboard")
def get_dashboard():
    db = get_db()
    data = db.get_dashboard_data()

    # Serialize datetime objects
    for ch in data.get("channels", []):
        if ch.get("stats_updated"):
            ch["stats_updated"] = str(ch["stats_updated"])

    for v in data.get("pipeline", []):
        if v.get("created_at"):
            v["created_at"] = str(v["created_at"])

    for s in data.get("upcoming", []):
        for k in ("created_at", "next_run_at", "last_run_at"):
            if s.get(k):
                s[k] = str(s[k])

    for v in data.get("top_videos", []):
        for k in ("created_at", "stats_updated"):
            if v.get(k):
                v[k] = str(v[k])

    # Add upcoming milestones
    try:
        from pipeline.milestones import get_upcoming_milestones
        data["upcoming_milestones"] = get_upcoming_milestones(db, limit=6)
    except Exception:
        data["upcoming_milestones"] = []

    return data
