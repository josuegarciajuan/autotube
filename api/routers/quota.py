"""YouTube API Quota dashboard — diagnostic endpoint."""
from __future__ import annotations

from fastapi import APIRouter, Query

router = APIRouter(prefix="/api/quota", tags=["quota"])


@router.get("/daily")
async def get_daily_quota(
    channel_slug: str | None = Query(None, description="Filter by channel slug"),
    date: str | None = Query(None, description="Date in YYYY-MM-DD format (default: today)"),
):
    """Get YouTube Data API quota usage for today (or a specific date).

    Returns usage broken down by channel, operation type, and hour.
    Also includes estimated time to exhaustion based on current burn rate.
    """
    from api.services.quota_tracker import get_daily_usage, get_recent_quota_log
    usage = get_daily_usage(channel_slug=channel_slug, date=date)

    # Add recent log entries for debugging
    recent = get_recent_quota_log(limit=30, channel_slug=channel_slug)

    return {
        **usage,
        "recent_log": recent,
    }


@router.get("/quota/channels")
async def get_channel_quota_summary():
    """Get quota summary across all channels for today."""
    from api.services.quota_tracker import get_daily_usage
    usage = get_daily_usage()
    return {
        "date": usage["date"],
        "quota_limit": usage["quota_limit"],
        "total_used": usage["total_units"],
        "remaining": usage["remaining"],
        "by_channel": usage["by_channel"],
        "by_operation": usage["by_operation"],
        "exhausted_estimated_at": usage["exhausted_estimated_at"],
    }
