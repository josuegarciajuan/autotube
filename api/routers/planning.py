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
    """Unified timeline: today's slots + last 5 completed + next 7 days.

    Includes both long-form video slots and shorts slots.
    Each slot has a 'kind' field: 'video' or 'short'.
    """
    db = get_db()
    today = dt_date.today()
    
    # Today's video slots
    today_video_slots = db.get_planned_slots(date_key=today.isoformat())
    
    # Today's shorts slots
    today_shorts_slots = db.get_shorts_planned_slots(date_key=today.isoformat())
    
    # Yesterday's last 5 completed
    yesterday = (today - timedelta(days=1)).isoformat()
    past_slots = db.get_planned_slots(date_key=yesterday, status="completed")
    past_slots = past_slots[-5:] if len(past_slots) > 5 else past_slots
    
    # Next 7 days (videos + shorts)
    start = (today + timedelta(days=1)).isoformat()
    end = (today + timedelta(days=7)).isoformat()
    future_video_slots = db.get_planned_slots_week(start, end)
    future_shorts_slots = db.get_shorts_planned_slots_week(start, end)
    
    all_slots = []
    
    for s in past_slots:
        for k in ("scheduled_at", "target_upload_at", "created_at"):
            if s.get(k): s[k] = str(s[k])
        s["kind"] = "video"
        s["type"] = "past"
        all_slots.append(s)
    
    for s in today_video_slots:
        for k in ("scheduled_at", "target_upload_at", "created_at"):
            if s.get(k): s[k] = str(s[k])
        s["kind"] = "video"
        s["type"] = "today"
        all_slots.append(s)

    for s in today_shorts_slots:
        for k in ("scheduled_at", "target_upload_at", "created_at"):
            if s.get(k): s[k] = str(s[k])
        s["kind"] = "short"
        s["type"] = "today"
        all_slots.append(s)
    
    for s in future_video_slots:
        for k in ("scheduled_at", "target_upload_at", "created_at"):
            if s.get(k): s[k] = str(s[k])
        s["kind"] = "video"
        s["type"] = "future"
        all_slots.append(s)

    for s in future_shorts_slots:
        for k in ("scheduled_at", "target_upload_at", "created_at"):
            if s.get(k): s[k] = str(s[k])
        s["kind"] = "short"
        s["type"] = "future"
        all_slots.append(s)
    
    return {
        "slots": all_slots,
        "stats": {
            "today": {
                "video_total": len(today_video_slots),
                "shorts_total": len(today_shorts_slots),
                "total": len(today_video_slots) + len(today_shorts_slots),
                "pending": sum(1 for s in today_video_slots if s["status"] == "pending") +
                          sum(1 for s in today_shorts_slots if s["status"] == "pending"),
                "running": sum(1 for s in today_video_slots if s["status"] == "running") +
                           sum(1 for s in today_shorts_slots if s["status"] == "running"),
                "completed": sum(1 for s in today_video_slots if s["status"] == "completed") +
                             sum(1 for s in today_shorts_slots if s["status"] == "completed"),
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
    """Get shorts planning config for all active channels.

    Returns shorts_native_per_day and shorts_clip_per_day per channel.
    """
    db = get_db()
    return db.get_shorts_planning_config()


class ShortsConfigUpdate(BaseModel):
    shorts_enabled: Optional[bool] = None
    shorts_native_per_day: Optional[int] = None
    shorts_clip_per_day: Optional[int] = None


@router.put("/shorts-config/{channel_id}")
def update_shorts_planning(channel_id: int, data: ShortsConfigUpdate):
    """Update shorts planning config for a channel.

    Accepts: shorts_enabled, shorts_native_per_day, shorts_clip_per_day.
    Triggers shorts replan after update.
    """
    db = get_db()
    ch = db.get_channel(channel_id)
    if not ch:
        raise HTTPException(404, "Channel not found")

    update_data = {}
    if data.shorts_enabled is not None:
        update_data["shorts_enabled"] = data.shorts_enabled
    if data.shorts_native_per_day is not None:
        update_data["shorts_native_per_day"] = max(0, min(10, data.shorts_native_per_day))
    if data.shorts_clip_per_day is not None:
        update_data["shorts_clip_per_day"] = max(0, min(10, data.shorts_clip_per_day))

    ok = db.update_shorts_planning_config(channel_id, update_data)
    if not ok:
        raise HTTPException(500, "Failed to update shorts planning config")

    # Replan shorts for the upcoming week
    try:
        from api.services.shorts_scheduler import generate_upcoming_shorts
        generate_upcoming_shorts(days=7, db=db)
    except Exception:
        pass  # Non-fatal: if replan fails, the old slots remain

    # Return updated config
    cfgs = db.get_shorts_planning_config(channel_id=channel_id)
    return cfgs[0] if cfgs else {}


@router.get("/shorts-slots")
def get_shorts_slots(
    date: Optional[str] = Query(None, description="Date key (YYYY-MM-DD), defaults to today"),
    channel_id: Optional[int] = Query(None),
    status: Optional[str] = Query(None),
):
    """Get shorts planned slots with optional filters."""
    db = get_db()
    if date is None:
        date = dt_date.today().isoformat()

    slots = db.get_shorts_planned_slots(date_key=date, channel_id=channel_id, status=status)

    # Format timestamps for JSON
    for s in slots:
        for k in ("scheduled_at", "target_upload_at", "created_at"):
            if s.get(k):
                s[k] = str(s[k])
    return slots


@router.get("/shorts-slots/today")
def get_today_shorts_slots():
    """Get today's shorts slots with KPIs."""
    db = get_db()
    today = dt_date.today().isoformat()
    slots = db.get_shorts_planned_slots(date_key=today)

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


@router.get("/shorts-slots/week")
def get_week_shorts_slots(
    channel_id: Optional[int] = Query(None),
):
    """Get shorts slots for the next 7 days, grouped by date."""
    db = get_db()
    today = dt_date.today()
    start = today.isoformat()
    end = (today + timedelta(days=6)).isoformat()

    slots = db.get_shorts_planned_slots_week(start, end, channel_id=channel_id)

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


@router.post("/shorts-replan")
def replan_shorts():
    """Force replan shorts schedule for the upcoming week."""
    from api.services.shorts_scheduler import generate_upcoming_shorts
    result = generate_upcoming_shorts(days=7)
    return {"ok": True, "message": "Shorts replan triggered", "result": result}
