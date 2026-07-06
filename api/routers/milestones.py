"""Milestones router — growth milestones with progress and predictions."""
from fastapi import APIRouter, HTTPException
from api.deps import get_db

router = APIRouter()


@router.get("/channels/{channel_id}/milestones")
def get_channel_milestones(channel_id: int):
    """Get all milestones with progress for a channel."""
    db = get_db()
    ch = db.get_channel(channel_id)
    if not ch:
        raise HTTPException(404, "Channel not found")

    from pipeline.milestones import get_channel_milestones as calc_milestones

    milestones = calc_milestones(db, channel_id)
    return {
        "channel_id": channel_id,
        "channel_name": ch["name"],
        "milestones": milestones,
    }


@router.get("/milestones/overview")
def get_milestones_overview(limit: int = 8):
    """Get upcoming milestones across all channels."""
    db = get_db()
    from pipeline.milestones import get_upcoming_milestones

    upcoming = get_upcoming_milestones(db, limit=limit)
    return {"upcoming": upcoming}


@router.post("/channels/{channel_id}/milestones/check")
def check_milestones_now(channel_id: int):
    """Force a milestone check for a channel (records newly achieved ones)."""
    db = get_db()
    ch = db.get_channel(channel_id)
    if not ch:
        raise HTTPException(404, "Channel not found")

    from pipeline.milestones import check_and_record_milestones

    new_count = check_and_record_milestones(db, channel_id)
    return {"ok": True, "new_milestones": new_count}
