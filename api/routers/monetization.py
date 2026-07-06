"""Monetization router — revenue estimation and YPP progress."""
from fastapi import APIRouter, HTTPException
from api.deps import get_db

router = APIRouter()


@router.get("/channels/{channel_id}/monetization")
def get_channel_monetization(channel_id: int):
    """Get monetization config and progress for a channel."""
    db = get_db()
    ch_mon = db.get_channel_monetization(channel_id)
    if not ch_mon:
        raise HTTPException(404, "Channel not found")

    from pipeline.monetization import calc_ypp_progress, calc_channel_revenue_total

    # Get latest stats for YPP calculation
    stats_list = db.get_channel_stats_history(channel_id, days=30)
    subs = 0
    watch_hours = 0.0
    weekly_subs_growth = 0.0
    weekly_hours_growth = 0.0
    if stats_list:
        latest = stats_list[-1]
        subs = latest.get("subscribers", 0) or 0
        watch_hours = round((latest.get("estimated_minutes_watched", 0) or 0) / 60.0, 1)
        if len(stats_list) > 1:
            prev = stats_list[0]
            weekly_subs_growth = max(0, subs - (prev.get("subscribers", 0) or 0))
            prev_hours = round((prev.get("estimated_minutes_watched", 0) or 0) / 60.0, 1)
            weekly_hours_growth = max(0, watch_hours - prev_hours)

    ypp = calc_ypp_progress(subs, watch_hours, weekly_subs_growth, weekly_hours_growth)
    revenue = calc_channel_revenue_total(db, channel_id)

    # Get content ranking with revenue
    content = db.get_channel_content_ranking(channel_id, limit=10)

    return {
        "channel_id": channel_id,
        "name": ch_mon["name"],
        "cpm_min": ch_mon.get("cpm_min"),
        "cpm_max": ch_mon.get("cpm_max"),
        "monetization_vertical": ch_mon.get("monetization_vertical"),
        "ypp_status": ch_mon.get("ypp_status"),
        "ypp_progress": ypp,
        "subscribers": subs,
        "watch_hours": watch_hours,
        "revenue_total_min": round(revenue.get("total_min", 0), 2),
        "revenue_total_max": round(revenue.get("total_max", 0), 2),
        "top_revenue_videos": [
            {
                "id": v["id"],
                "title": v["titulo_final"],
                "views": v.get("views") or 0,
                "revenue_min": round(v.get("estimated_revenue_min") or 0, 2),
                "revenue_max": round(v.get("estimated_revenue_max") or 0, 2),
            }
            for v in content[:5]
        ],
    }


@router.get("/monetization/overview")
def get_monetization_overview():
    """Global monetization overview across all channels."""
    db = get_db()
    channels = db.get_channels(active_only=True)

    from pipeline.monetization import calc_channel_revenue_total

    total_min = 0.0
    total_max = 0.0
    per_channel = []

    for ch in channels:
        revenue = calc_channel_revenue_total(db, ch["id"])
        ch_min = round(revenue.get("total_min", 0), 2)
        ch_max = round(revenue.get("total_max", 0), 2)
        total_min += ch_min
        total_max += ch_max
        per_channel.append({
            "channel_id": ch["id"],
            "name": ch["name"],
            "slug": ch["slug"],
            "cpm_min": ch.get("cpm_min"),
            "cpm_max": ch.get("cpm_max"),
            "revenue_min": ch_min,
            "revenue_max": ch_max,
        })

    return {
        "total_revenue_min": round(total_min, 2),
        "total_revenue_max": round(total_max, 2),
        "channels": per_channel,
    }


@router.put("/channels/{channel_id}/monetization")
def update_channel_monetization(channel_id: int, data: dict):
    """Update CPM config for a channel."""
    db = get_db()
    ch = db.get_channel(channel_id)
    if not ch:
        raise HTTPException(404, "Channel not found")

    ok = db.update_channel_monetization(
        channel_id,
        cpm_min=data.get("cpm_min"),
        cpm_max=data.get("cpm_max"),
        vertical=data.get("monetization_vertical"),
    )
    if not ok:
        raise HTTPException(400, "No fields provided")

    ch_mon = db.get_channel_monetization(channel_id)
    return {"ok": True, "channel": ch_mon}
