"""Analytics router — growth charts, content performance, and detailed video analytics."""
from fastapi import APIRouter, HTTPException
from api.deps import get_db

router = APIRouter()


@router.get("/channels/{channel_id}/analytics/growth")
def get_channel_growth(channel_id: int, days: int = 30):
    """Get daily growth data for a channel (subs, views, watch hours, revenue)."""
    db = get_db()
    ch = db.get_channel(channel_id)
    if not ch:
        raise HTTPException(404, "Channel not found")

    data = db.get_channel_growth_data(channel_id, days=days)
    return {
        "channel_id": channel_id,
        "channel_name": ch["name"],
        "days": days,
        "data": data,
    }


@router.get("/channels/{channel_id}/analytics/content")
def get_channel_content_ranking(channel_id: int, limit: int = 20, sort: str = "views"):
    """Get ranked list of videos with performance and revenue data."""
    db = get_db()
    ch = db.get_channel(channel_id)
    if not ch:
        raise HTTPException(404, "Channel not found")

    content = db.get_channel_content_ranking(channel_id, limit=limit)

    # Calculate revenue per video if CPM is set
    cpm_min = ch.get("cpm_min") or 0
    cpm_max = ch.get("cpm_max") or 0

    result = []
    for v in content:
        views = v.get("views") or 0
        rev_min = round(views / 1000.0 * cpm_min, 2) if cpm_min else 0
        rev_max = round(views / 1000.0 * cpm_max, 2) if cpm_max else 0
        engagement_rate = 0
        if views > 0:
            engagement_rate = round((v.get("likes") or 0) + (v.get("comments") or 0) / views * 100, 2)

        result.append({
            "id": v["id"],
            "title": v.get("titulo_final", "Sin título"),
            "yt_video_id": v.get("yt_video_id"),
            "yt_url": v.get("yt_url"),
            "duracion_seg": v.get("duracion_seg"),
            "views": views,
            "likes": v.get("likes") or 0,
            "comments": v.get("comments") or 0,
            "estimated_minutes_watched": round(v.get("estimated_minutes_watched") or 0, 1),
            "average_view_duration": round(v.get("average_view_duration") or 0, 1),
            "subscribers_gained": v.get("subscribers_gained") or 0,
            "revenue_min": round(v.get("estimated_revenue_min") or rev_min, 2),
            "revenue_max": round(v.get("estimated_revenue_max") or rev_max, 2),
            "engagement_rate": engagement_rate,
            "created_at": str(v.get("created_at", "")),
        })

    # Sort by the requested field
    sort_keys = {
        "views": lambda x: x["views"],
        "likes": lambda x: x["likes"],
        "engagement": lambda x: x["engagement_rate"],
        "revenue": lambda x: x["revenue_max"],
    }
    result.sort(key=sort_keys.get(sort, lambda x: x["views"]), reverse=True)

    return {
        "channel_id": channel_id,
        "channel_name": ch["name"],
        "sort": sort,
        "videos": result,
    }


@router.get("/videos/{video_id}/analytics")
def get_video_analytics(video_id: int):
    """Get detailed analytics for a video (traffic sources, demographics, retention)."""
    db = get_db()
    video = db.get_video(video_id)
    if not video:
        raise HTTPException(404, "Video not found")

    analytics = db.get_video_analytics(video_id)

    # Group by report_type
    grouped = {"traffic_source": [], "demographics": [], "retention": None}
    for row in analytics:
        if row["report_type"] == "retention":
            grouped["retention"] = row["metric_value"]
        else:
            grouped[row["report_type"]].append({
                "dimension": row["dimension"],
                "value": row["metric_value"],
            })

    return {
        "video_id": video_id,
        "yt_video_id": video.get("yt_video_id"),
        "title": video.get("titulo_final"),
        "analytics": grouped,
    }


@router.get("/analytics/comparison")
def get_channels_comparison():
    """Cross-channel comparison: growth rates and revenue per 1K views."""
    db = get_db()
    channels = db.get_channels(active_only=True)

    result = []
    for ch in channels:
        growth = db.get_channel_growth_data(ch["id"], days=30)
        if not growth:
            continue

        first = growth[0]
        last = growth[-1]

        subs_growth = (last.get("subscribers", 0) or 0) - (first.get("subscribers", 0) or 0)
        views_growth = (last.get("total_views", 0) or 0) - (first.get("total_views", 0) or 0)
        watch_growth = round(
            ((last.get("watch_minutes", 0) or 0) - (first.get("watch_minutes", 0) or 0)) / 60.0, 1
        )

        # Revenue per 1K views
        total_views = last.get("total_views", 0) or 1
        total_rev_max = last.get("revenue_max") or 0
        rev_per_1k = round(total_rev_max / (total_views / 1000.0), 2) if total_views > 0 else 0

        result.append({
            "channel_id": ch["id"],
            "name": ch["name"],
            "slug": ch["slug"],
            "cpm_min": ch.get("cpm_min"),
            "cpm_max": ch.get("cpm_max"),
            "subs_30d_growth": subs_growth,
            "views_30d_growth": views_growth,
            "watch_hours_30d_growth": watch_growth,
            "revenue_per_1k_views": rev_per_1k,
            "latest_subs": last.get("subscribers", 0) or 0,
            "latest_views": last.get("total_views", 0) or 0,
        })

    return {"channels": result}


@router.get("/channels/{channel_id}/analytics/watch-time")
def get_channel_watch_time(channel_id: int):
    """Get watch time summary for YPP monetization tracking.

    Returns cumulative watch hours, daily breakdown, daily average,
    projection to 4,000h, and top videos by watch time.
    """
    db = get_db()
    ch = db.get_channel(channel_id)
    if not ch:
        raise HTTPException(404, "Channel not found")

    summary = db.get_channel_watch_time_summary(channel_id)
    summary["channel_name"] = ch["name"]
    summary["channel_slug"] = ch["slug"]
    return summary
