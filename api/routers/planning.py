"""Planning router — dynamic daily video scheduling API."""

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel
from typing import Optional
from datetime import date as dt_date, timedelta

from api.deps import get_db

router = APIRouter()


class PlanningConfigUpdate(BaseModel):
    videos_per_day: Optional[int] = None
    planning_enabled: Optional[bool] = None


class PreviewOverrides(BaseModel):
    overrides: Optional[dict] = None  # slug → {videos_per_day, planning_enabled}


# ── Config ────────────────────────────────────────────────────

@router.get("/config")
def get_planning_config():
    """Get planning configuration for all active channels."""
    db = get_db()
    channels = db.get_channels(active_only=True)
    result = []
    for ch in channels:
        cfg = db.get_channel_planning_config(ch["id"])
        result.append(cfg)
    return result


@router.put("/config/{channel_id}")
def update_planning_config(channel_id: int, data: PlanningConfigUpdate):
    """Update planning settings (videos_per_day, planning_enabled) for a channel."""
    db = get_db()
    ch = db.get_channel(channel_id)
    if not ch:
        raise HTTPException(404, "Channel not found")

    ok = db.update_channel_planning_config(
        channel_id,
        videos_per_day=data.videos_per_day,
        planning_enabled=data.planning_enabled,
    )
    if not ok:
        raise HTTPException(500, "Failed to update")

    # Return updated config
    return db.get_channel_planning_config(channel_id)


# ── Slots ─────────────────────────────────────────────────────

@router.get("/slots")
def get_planned_slots(
    date: Optional[str] = Query(None, description="Date key (YYYY-MM-DD), defaults to today"),
    channel_id: Optional[int] = Query(None),
    status: Optional[str] = Query(None),
):
    """Get planned slots with optional filters."""
    db = get_db()
    if date is None:
        date = dt_date.today().isoformat()

    slots = db.get_planned_slots(date_key=date, channel_id=channel_id, status=status)
    
    # Format timestamps for JSON
    for s in slots:
        for k in ("scheduled_at", "target_upload_at", "created_at"):
            if s.get(k):
                s[k] = str(s[k])
    return slots


@router.get("/slots/today")
def get_today_slots():
    """Get all planned slots for today."""
    db = get_db()
    today = dt_date.today().isoformat()
    slots = db.get_planned_slots(date_key=today)
    
    # Compute stats
    pending = sum(1 for s in slots if s["status"] == "pending")
    running = sum(1 for s in slots if s["status"] == "running")
    completed = sum(1 for s in slots if s["status"] == "completed")
    cancelled = sum(1 for s in slots if s["status"] == "cancelled")
    
    for s in slots:
        for k in ("scheduled_at", "target_upload_at", "created_at"):
            if s.get(k):
                s[k] = str(s[k])
    
    return {
        "date": today,
        "total": len(slots),
        "pending": pending,
        "running": running,
        "completed": completed,
        "cancelled": cancelled,
        "slots": slots,
    }


@router.get("/slots/week")
def get_week_slots(
    channel_id: Optional[int] = Query(None),
):
    """Get planned slots for the next 7 days."""
    db = get_db()
    today = dt_date.today()
    start = today.isoformat()
    end = (today + timedelta(days=6)).isoformat()
    
    slots = db.get_planned_slots_week(start, end, channel_id=channel_id)
    
    for s in slots:
        for k in ("scheduled_at", "target_upload_at", "created_at"):
            if s.get(k):
                s[k] = str(s[k])
    
    # Group by date
    grouped = {}
    for s in slots:
        dk = s["date_key"]
        if dk not in grouped:
            grouped[dk] = {
                "date": dk,
                "weekday": dt_date.fromisoformat(dk).strftime("%a"),
                "total": 0,
                "slots": [],
            }
        grouped[dk]["slots"].append(s)
        grouped[dk]["total"] += 1
    
    return {
        "start_date": start,
        "end_date": end,
        "days": list(grouped.values()),
    }


@router.get("/timeline")
def get_timeline():
    """Unified timeline: today's slots + last 5 completed + next 7 days."""
    db = get_db()
    today = dt_date.today()
    
    # Today's slots
    today_slots = db.get_planned_slots(date_key=today.isoformat())
    
    # Yesterday's last 5 completed
    yesterday = (today - timedelta(days=1)).isoformat()
    past_slots = db.get_planned_slots(date_key=yesterday, status="completed")
    past_slots = past_slots[-5:] if len(past_slots) > 5 else past_slots
    
    # Next 7 days
    start = (today + timedelta(days=1)).isoformat()
    end = (today + timedelta(days=7)).isoformat()
    future_slots = db.get_planned_slots_week(start, end)
    
    all_slots = []
    
    for s in past_slots:
        for k in ("scheduled_at", "target_upload_at", "created_at"):
            if s.get(k): s[k] = str(s[k])
        s["type"] = "past"
        all_slots.append(s)
    
    for s in today_slots:
        for k in ("scheduled_at", "target_upload_at", "created_at"):
            if s.get(k): s[k] = str(s[k])
        s["type"] = "today"
        all_slots.append(s)
    
    for s in future_slots:
        for k in ("scheduled_at", "target_upload_at", "created_at"):
            if s.get(k): s[k] = str(s[k])
        s["type"] = "future"
        all_slots.append(s)
    
    return {
        "slots": all_slots,
        "stats": {
            "today": {
                "total": len(today_slots),
                "pending": sum(1 for s in today_slots if s["status"] == "pending"),
                "running": sum(1 for s in today_slots if s["status"] == "running"),
                "completed": sum(1 for s in today_slots if s["status"] == "completed"),
            }
        },
    }


@router.get("/stats")
def get_planning_stats():
    """Quick planning stats for dashboard."""
    db = get_db()
    today = dt_date.today().isoformat()
    slots = db.get_planned_slots(date_key=today)
    
    pending = sum(1 for s in slots if s["status"] == "pending")
    running = sum(1 for s in slots if s["status"] == "running")
    completed = sum(1 for s in slots if s["status"] == "completed")
    
    # Next slot info
    next_slot = None
    pending_slots = [s for s in slots if s["status"] == "pending"]
    if pending_slots:
        ns = sorted(pending_slots, key=lambda x: x["scheduled_at"])[0]
        next_slot = {
            "time": str(ns["scheduled_at"])[11:16] if ns.get("scheduled_at") else None,
            "channel_name": ns.get("channel_name", ""),
        }
    
    return {
        "today_total": len(slots),
        "today_pending": pending,
        "today_running": running,
        "today_completed": completed,
        "is_running": running > 0,
        "next_slot": next_slot,
    }


# ── Actions ───────────────────────────────────────────────────

@router.post("/replan")
def force_replan(date: Optional[str] = None):
    """Force recalculation of planning slots for a date (defaults to today)."""
    from api.services.planning_service import compute_and_store_slots
    if date is None:
        date = dt_date.today().isoformat()
    
    result = compute_and_store_slots(date)
    return result


@router.post("/preview")
def preview_week(data: PreviewOverrides = None):
    """Dry-run: simulate 7 days of planning without persisting.
    
    Send overrides to test config changes: { "canal2": { "videos_per_day": 3 } }
    """
    from api.services.planning_service import preview_week as do_preview
    overrides = data.overrides if data else None
    return do_preview(overrides=overrides)


# ── Shorts Planning ──────────────────────────────────────────────

@router.get("/shorts-config")
def get_shorts_planning():
    """Get shorts planning config for all active channels."""
    from api.services.planning_service import get_shorts_planning_config
    return get_shorts_planning_config()


@router.put("/shorts-config/{channel_id}")
def update_shorts_planning(channel_id: int, data: dict):
    """Update shorts planning config for a channel."""
    from api.services.planning_service import update_shorts_planning_config
    return update_shorts_planning_config(channel_id, data)


@router.post("/shorts-replan")
def replan_shorts():
    """Force replan shorts schedule for the upcoming week."""
    from api.services.planning_service import ensure_shorts_planned
    ensure_shorts_planned()
    return {"ok": True, "message": "Shorts replan triggered"}
