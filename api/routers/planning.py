"""Planning router — dynamic daily video scheduling API."""

import logging
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel
from typing import Optional
from datetime import date as dt_date, timedelta

from api.deps import get_db

logger = logging.getLogger("autotube.planning")

router = APIRouter()


class PlanningConfigUpdate(BaseModel):
    videos_per_day: Optional[int] = None
    planning_enabled: Optional[bool] = None
    viral_per_day: Optional[int] = None
    # ── Generación long-form/día (ago 2026): desacoplado de subida/publicación.
    # >1 encola backlog en awaiting_upload; la válvula publica 1/día.
    longform_generation_per_day: Optional[int] = None
    public_videos_per_day: Optional[int] = None
    upload_capacity_per_day: Optional[int] = None
    # ── Random daily boost weights (v13) ──
    videos_day_boost_weight: Optional[float] = None   # 0.0-1.0, prob of +1 video/day
    viral_day_boost_weight: Optional[float] = None     # 0.0-1.0, prob of +1 viral/day
    # ── Multi-window upload (v11) ──
    upload_windows: Optional[list] = None            # [{"start":10,"end":13},{"start":20,"end":22}]
    publish_window_spread_min: Optional[int] = None   # ±minutes around peak (default 90)


class PreviewOverrides(BaseModel):
    overrides: Optional[dict] = None  # slug → {videos_per_day, planning_enabled}


class SafeFullReplanApply(BaseModel):
    confirmation_token: str


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
    """Update planning settings (videos_per_day, planning_enabled, viral_per_day) for a channel."""
    db = get_db()
    ch = db.get_channel(channel_id)
    if not ch:
        raise HTTPException(404, "Channel not found")

    # Validate: viral_per_day cannot exceed videos_per_day
    if data.viral_per_day is not None:
        total = data.videos_per_day
        if total is None:
            # Read current videos_per_day from config to validate against
            current_cfg = db.get_channel_planning_config(channel_id)
            total = current_cfg.get("videos_per_day", 0)
        if data.viral_per_day > total:
            raise HTTPException(400, f"viral_per_day ({data.viral_per_day}) cannot exceed videos_per_day ({total})")

    ok = db.update_channel_planning_config(
        channel_id,
        videos_per_day=data.videos_per_day,
        planning_enabled=data.planning_enabled,
        viral_per_day=data.viral_per_day,
        videos_day_boost_weight=data.videos_day_boost_weight,
        viral_day_boost_weight=data.viral_day_boost_weight,
        upload_windows=data.upload_windows,
        publish_window_spread_min=data.publish_window_spread_min,
        longform_generation_per_day=data.longform_generation_per_day,
        public_videos_per_day=data.public_videos_per_day,
        upload_capacity_per_day=data.upload_capacity_per_day,
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


# ── Slot Source Mode ──────────────────────────────────────────────

class SlotSourceModeUpdate(BaseModel):
    source_mode: str = "original"  # "original" | "viral"

@router.put("/slots/{slot_id}/mode")
def update_slot_source_mode(slot_id: int, data: SlotSourceModeUpdate):
    """Change the source_mode of a planned slot ('original' or 'viral')."""
    db = get_db()
    if data.source_mode not in ("original", "viral"):
        raise HTTPException(400, "source_mode must be 'original' or 'viral'")
    db.update_slot_source_mode(slot_id, data.source_mode)
    return {"ok": True, "slot_id": slot_id, "source_mode": data.source_mode}


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
    shorts_clips_per_long: Optional[int] = None
    shorts_per_day: Optional[int] = None          # v2: total daily target
    shorts_native_ratio: Optional[float] = None   # v2: native/clip ratio


@router.put("/shorts-config/{channel_id}")
def update_shorts_planning(channel_id: int, data: ShortsConfigUpdate):
    """Update shorts planning config for a channel.

    Accepts: shorts_enabled, shorts_native_per_day, shorts_clip_per_day,
             shorts_clips_per_long, shorts_per_day, shorts_native_ratio.
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
    if data.shorts_clips_per_long is not None:
        update_data["shorts_clips_per_long"] = max(0, min(5, data.shorts_clips_per_long))
    if data.shorts_per_day is not None:
        update_data["shorts_per_day"] = max(1, min(15, data.shorts_per_day))
    if data.shorts_native_ratio is not None:
        update_data["shorts_native_ratio"] = max(0.10, min(0.90, data.shorts_native_ratio))

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
    """Get today's shorts slots with KPIs + la cola de shorts nativos generados.

    (fix ago 2026) La cola real de shorts 'generated' (renderizados pero sin
    subir, p. ej. durante bloqueos de spam o cuota) vive en la tabla `shorts`
    y antes NO aparecía en Programación. Se expone como `queued`.
    """
    db = get_db()
    today = dt_date.today().isoformat()
    slots = db.get_shorts_planned_slots(date_key=today)

    # Compute stats
    pending = sum(1 for s in slots if s["status"] == "pending")
    running = sum(1 for s in slots if s["status"] == "running")
    completed = sum(1 for s in slots if s["status"] == "completed")
    cancelled = sum(1 for s in slots if s["status"] == "cancelled")
    generated_slots = sum(1 for s in slots if s["status"] == "generated")

    # ── Cola de shorts nativos generados (status='generated') ──
    queued = []
    try:
        for r in db.get_queued_generated_shorts():
            fp = r.get("file_path") or ""
            queued.append({
                "short_id": r["id"],
                "channel_id": r["channel_id"],
                "channel_name": r.get("channel_name") or "",
                "channel_slug": r.get("channel_slug") or "",
                "title": (r.get("title") or "")[:100],
                "created_at": str(r.get("created_at") or ""),
                "file_exists": bool(fp) and Path(fp).exists(),
            })
    except Exception as exc:
        logger.warning("shorts-slots/today: queued lookup failed: %s", exc)

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
        "generated": generated_slots,
        "queued": queued,
        "queued_count": len(queued),
        "slots": slots,
    }


@router.post("/shorts-queue/upload/{short_id}")
def upload_queued_short(short_id: int):
    """Sube AHORA un short nativo en cola (status='generated') manualmente.

    (fix ago 2026) Acción manual para drenar la cola de shorts generados que
    quedaron esperando (p. ej. tras expirar bloqueo de spam / cuota).
    """
    from api.services.shorts_scheduler import _upload_queued_native_short
    from database.db_extended import ExtendedDatabase
    db = ExtendedDatabase()
    try:
        queued = db.get_queued_generated_shorts()
        rec = next((q for q in queued if int(q.get("id") or 0) == short_id), None)
    except Exception as exc:
        logger.warning("shorts-queue/upload: lookup failed: %s", exc)
        rec = None
    if not rec:
        raise HTTPException(404, f"Short #{short_id} no está en cola (status='generated')")
    ok = _upload_queued_native_short(rec, db=db)
    return {"ok": bool(ok), "short_id": short_id, "uploaded": bool(ok)}


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


@router.get("/pipeline-status")
def get_pipeline_status():
    """Get full pipeline status for the visual scheduling view.

    Returns: planned, generating, awaiting_upload, warming, published_24h, shorts.
    - planned:          video slots pending today (not yet dispatched)
    - generating:       long-form videos currently being generated with progress
    - awaiting_upload:  videos generated locally, waiting for F2 upload window
    - warming:          videos uploaded as private waiting to go public
    - published_24h:    videos & shorts published in the last 24 hours
    - shorts.pending:   shorts slots not yet dispatched
    - shorts.generating:shorts slots running with job progress
    - shorts.completed: shorts slots completed today
    """
    db = get_db()
    data = db.get_pipeline_status()

    # Convert any non-serializable datetime objects to strings
    for section in ("planned", "generating", "awaiting_upload", "warming", "published_24h"):
        for item in data[section]:
            for key, val in list(item.items()):
                if hasattr(val, "strftime"):
                    item[key] = str(val)
    # Shorts subsections
    for sub in ("pending", "generating", "completed", "ready_to_upload"):
        for item in data["shorts"][sub]:
            for key, val in list(item.items()):
                if hasattr(val, "strftime"):
                    item[key] = str(val)

    return data


@router.post("/shorts-replan")
def replan_shorts():
    """Force replan shorts schedule for the upcoming week."""
    from api.services.shorts_scheduler import generate_upcoming_shorts
    result = generate_upcoming_shorts(days=7)
    return {"ok": True, "message": "Shorts replan triggered", "result": result}


# ── Optimal Publish Slots (v10) ─────────────────────────────────

@router.post("/recalculate-optimal-slots")
async def recalculate_optimal_slots_all():
    """Force recalculation of optimal publish slots for all active channels.

    Runs the full calculation pipeline: YouTube Analytics hourly activity,
    audience country split, DB historical performance, top-3 peak detection.
    If slots change significantly, triggers replanning of pending long-form
    and shorts slots across the 7-day horizon.
    """
    from api.services.optimal_slots_calculator import calculate_and_replan_all
    from database.db_extended import ExtendedDatabase
    result = calculate_and_replan_all(ExtendedDatabase())
    return {"ok": True, **result}


# ═══════════════════════════════════════════════════════════════
#  Full Replan — "Reprogramar Ahora"
# ═══════════════════════════════════════════════════════════════

@router.post("/full-replan/preflight")
def safe_full_replan_preflight(horizon_days: int = Query(7, ge=1, le=31)):
    """Preview a safe long-form + Shorts replan and return a one-time token."""
    from api.services.planning_service import safe_full_replan_preflight as do_preflight
    return do_preflight(db=get_db(), horizon_days=horizon_days)


@router.post("/full-replan/apply")
def safe_full_replan_apply(data: SafeFullReplanApply):
    """Apply a long-form + Shorts preflight without deleting slots or jobs."""
    from api.services.planning_service import safe_full_replan_apply as do_apply
    try:
        return do_apply(data.confirmation_token, db=get_db())
    except ValueError as exc:
        message = str(exc)
        if "expired" in message:
            code = "SAFE_REPLAN_EXPIRED"
        elif "already used" in message:
            code = "SAFE_REPLAN_USED"
        elif "invalid" in message:
            code = "SAFE_REPLAN_INVALID"
        else:
            code = "SAFE_REPLAN_STALE"
        raise HTTPException(409, {"code": code, "message": message}) from exc


@router.post("/full-replan")
def full_replan():
    """Deprecated dangerous reset; clients must use preflight then apply."""
    raise HTTPException(410, "Deprecated: use /full-replan/preflight then /full-replan/apply")
