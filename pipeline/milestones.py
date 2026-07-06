"""Milestone tracking: predefined growth milestones with progress and prediction.

Milestones are defined as constants and calculated on the fly from existing
stats. The DB stores only 'achieved' status and date; everything else is
derived from channel_stats_history and video counts.
"""

import logging

logger = logging.getLogger(__name__)

# Predefined milestones — ordered by increasing difficulty
MILESTONES = [
    # ── Subscribers ──
    {"metric": "subscribers", "target": 100, "label": "100 suscriptores", "tier": "bronze", "order": 1},
    {"metric": "subscribers", "target": 500, "label": "500 suscriptores", "tier": "bronze", "order": 2},
    {"metric": "subscribers", "target": 1000, "label": "1,000 suscriptores (YPP)", "tier": "silver", "order": 3},
    {"metric": "subscribers", "target": 5000, "label": "5,000 suscriptores", "tier": "silver", "order": 4},
    {"metric": "subscribers", "target": 10000, "label": "10,000 suscriptores", "tier": "gold", "order": 5},
    {"metric": "subscribers", "target": 100000, "label": "100,000 suscriptores", "tier": "diamond", "order": 6},
    # ── Watch hours ──
    {"metric": "watch_hours", "target": 1000, "label": "1,000 horas de visualización", "tier": "bronze", "order": 7},
    {"metric": "watch_hours", "target": 4000, "label": "4,000 horas (YPP)", "tier": "silver", "order": 8},
    {"metric": "watch_hours", "target": 10000, "label": "10,000 horas", "tier": "gold", "order": 9},
    {"metric": "watch_hours", "target": 100000, "label": "100,000 horas", "tier": "diamond", "order": 10},
    # ── Videos published ──
    {"metric": "videos_published", "target": 10, "label": "10 videos publicados", "tier": "bronze", "order": 11},
    {"metric": "videos_published", "target": 50, "label": "50 videos publicados", "tier": "silver", "order": 12},
    {"metric": "videos_published", "target": 100, "label": "100 videos publicados", "tier": "gold", "order": 13},
    {"metric": "videos_published", "target": 500, "label": "500 videos publicados", "tier": "diamond", "order": 14},
    # ── Total views ──
    {"metric": "total_views", "target": 10000, "label": "10,000 visualizaciones", "tier": "bronze", "order": 15},
    {"metric": "total_views", "target": 50000, "label": "50,000 visualizaciones", "tier": "silver", "order": 16},
    {"metric": "total_views", "target": 100000, "label": "100,000 visualizaciones", "tier": "gold", "order": 17},
    {"metric": "total_views", "target": 1000000, "label": "1,000,000 visualizaciones", "tier": "diamond", "order": 18},
    # ── Revenue (estimated) ──
    {"metric": "revenue", "target": 100, "label": "$100 ingresos estimados", "tier": "bronze", "order": 19},
    {"metric": "revenue", "target": 1000, "label": "$1,000 ingresos estimados", "tier": "silver", "order": 20},
    {"metric": "revenue", "target": 10000, "label": "$10,000 ingresos estimados", "tier": "gold", "order": 21},
]


def _get_current_metric(db, channel_id: int, metric_type: str) -> float:
    """Get the current value for a metric type from the latest stats."""
    if metric_type == "videos_published":
        with db._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) as cnt FROM videos WHERE channel_id = ? AND yt_video_id IS NOT NULL",
                (channel_id,),
            ).fetchone()
        return float(row["cnt"]) if row else 0.0

    # For subscribers, total_views, watch_hours, revenue: get latest stats
    stats_list = db.get_channel_stats_history(channel_id, days=30)
    if not stats_list:
        return 0.0

    latest = stats_list[-1]
    if metric_type == "subscribers":
        return float(latest.get("subscribers", 0) or 0)
    elif metric_type == "total_views":
        return float(latest.get("total_views", 0) or 0)
    elif metric_type == "watch_hours":
        return round(float(latest.get("estimated_minutes_watched", 0) or 0) / 60.0, 1)
    elif metric_type == "revenue":
        return float(latest.get("estimated_revenue_max", 0) or 0)

    return 0.0


def _get_weekly_growth(db, channel_id: int, metric_type: str) -> float:
    """Calculate weekly growth rate for a metric."""
    if metric_type == "videos_published":
        return 0.0  # Cannot project video cadence from stats alone

    stats_list = db.get_channel_stats_history(channel_id, days=14)
    if len(stats_list) < 2:
        return 0.0

    current = _get_current_metric(db, channel_id, metric_type)
    # Find value ~7 days ago
    prev_val = stats_list[0]  # oldest snapshot in window
    if metric_type == "revenue":
        prev = float(prev_val.get("estimated_revenue_max", 0) or 0)
    elif metric_type == "watch_hours":
        prev = round(float(prev_val.get("estimated_minutes_watched", 0) or 0) / 60.0, 1)
    else:
        prev = float(prev_val.get(metric_type, 0) or 0)

    return max(0, current - prev)


def predict_days(current: float, target: float, weekly_growth: float) -> int | None:
    """Estimate days until a target is reached given weekly growth.

    Returns None if growth is zero or negative (cannot predict).
    """
    if current >= target:
        return 0
    if weekly_growth <= 0:
        return None
    remaining = target - current
    daily_rate = weekly_growth / 7.0
    return round(remaining / daily_rate)


def get_channel_milestones(db, channel_id: int) -> list[dict]:
    """Calculate all milestones for a channel with progress and predictions."""
    # Get achieved milestones from DB
    achieved_map = {}
    try:
        db_milestones = db.get_channel_milestones(channel_id)
        for m in db_milestones:
            key = (m["metric_type"], m["target_value"])
            achieved_map[key] = m
    except Exception:
        pass

    result = []
    for ms in MILESTONES:
        metric = ms["metric"]
        target = ms["target"]
        current = _get_current_metric(db, channel_id, metric)
        weekly_growth = _get_weekly_growth(db, channel_id, metric)
        pct = min(100, round(current / target * 100, 1)) if target > 0 else 0
        predicted_days = predict_days(current, target, weekly_growth)

        key = (metric, target)
        achieved_row = achieved_map.get(key)
        is_achieved = (achieved_row and achieved_row.get("status") == "achieved") or (current >= target)

        result.append({
            "metric_type": metric,
            "target_value": target,
            "label": ms["label"],
            "tier": ms["tier"],
            "sort_order": ms["order"],
            "current_value": round(current, 1) if isinstance(current, float) else int(current),
            "percentage": pct,
            "status": "achieved" if is_achieved else "in_progress",
            "predicted_days": predicted_days if not is_achieved else 0,
            "achieved_at": achieved_row.get("achieved_at") if achieved_row else None,
        })

    return result


def get_upcoming_milestones(db, limit: int = 8) -> list[dict]:
    """Get the next milestones to be reached across all active channels."""
    channels = db.get_channels(active_only=True)
    all_upcoming = []
    for ch in channels:
        milestones = get_channel_milestones(db, ch["id"])
        for m in milestones:
            if m["status"] == "in_progress" and m["predicted_days"] is not None:
                all_upcoming.append({
                    **m,
                    "channel_id": ch["id"],
                    "channel_name": ch["name"],
                    "channel_slug": ch["slug"],
                })

    # Sort by predicted days (closest first), then by tier priority
    tier_order = {"bronze": 1, "silver": 2, "gold": 3, "diamond": 4}
    all_upcoming.sort(key=lambda x: (
        x["predicted_days"] or 99999,
        tier_order.get(x["tier"], 999),
    ))

    return all_upcoming[:limit]


def check_and_record_milestones(db, channel_id: int) -> int:
    """Check for newly achieved milestones and record them in DB.

    Returns number of newly achieved milestones.
    """
    net_new = 0
    milestones = get_channel_milestones(db, channel_id)
    for m in milestones:
        if m["status"] == "achieved":
            try:
                # Only insert if not already recorded
                existing = db.get_channel_milestones(channel_id)
                already_recorded = any(
                    e["metric_type"] == m["metric_type"]
                    and e["target_value"] == m["target_value"]
                    and e["status"] == "achieved"
                    for e in existing
                )
                if not already_recorded:
                    db.upsert_channel_milestone(
                        channel_id,
                        m["metric_type"],
                        m["target_value"],
                        m["label"],
                        m["tier"],
                        m["sort_order"],
                        status="achieved",
                        achieved_at=None,  # SQLite will use CURRENT_TIMESTAMP on new rows
                    )
                    net_new += 1
            except Exception as e:
                logger.warning("Failed to record milestone for channel %d: %s", channel_id, e)

    return net_new
