"""Dashboard router — unified data for the main dashboard."""
import time
import threading
from fastapi import APIRouter, Query
from typing import Optional
from api.deps import get_db

router = APIRouter()

# In-memory TTL cache for dashboard responses
_CACHE: dict = {}
_CACHE_TTL = 60  # seconds
_CACHE_LOCK = threading.Lock()


def invalidate_dashboard_cache(channel_id: Optional[int] = None):
    """Invalidate cached dashboard data (called after state-changing operations)."""
    with _CACHE_LOCK:
        if channel_id is not None:
            # Invalidate both the specific channel key and the 'all' key
            _CACHE.pop(f"dashboard:{channel_id}", None)
            _CACHE.pop("dashboard:all", None)
        else:
            _CACHE.clear()


@router.get("/dashboard")
def get_dashboard(channel_id: Optional[int] = Query(None, description="Filter by channel ID")):
    cache_key = f"dashboard:{channel_id or 'all'}"

    with _CACHE_LOCK:
        if cache_key in _CACHE:
            entry = _CACHE[cache_key]
            if time.time() - entry["ts"] < _CACHE_TTL:
                return entry["data"]

    db = get_db()
    data = db.get_dashboard_data(channel_id=channel_id)

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

    # Serialize heatmap dates
    for h in data.get("heatmap_data", []):
        if h.get("date"):
            h["date"] = str(h["date"])

    # Serialize today_actions timestamps
    for a in data.get("today_actions", []):
        if a.get("action_at"):
            a["action_at"] = str(a["action_at"])

    # Add upcoming milestones
    try:
        from pipeline.milestones import get_upcoming_milestones
        data["upcoming_milestones"] = get_upcoming_milestones(db, limit=6)
    except Exception:
        data["upcoming_milestones"] = []

    with _CACHE_LOCK:
        _CACHE[cache_key] = {"data": data, "ts": time.time()}

    return data
