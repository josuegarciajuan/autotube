"""Monetization calculations: revenue estimation, YPP progress tracking.

Revenue is estimated based on CPM ranges configured per channel vertical.
YPP (YouTube Partner Program) requires: 1,000 subscribers + 4,000 watch hours.
"""

import re
from typing import Optional


def parse_cpm(cpm_str: str) -> tuple[float, float]:
    """Parse a CPM string like '$5-$12 USD' or '$8–$18 USD' into (min, max)."""
    if not cpm_str:
        return (0.0, 0.0)
    # Replace unicode dashes with hyphen
    cpm_str = cpm_str.replace("\u2013", "-").replace("\u2014", "-")
    # Extract numbers
    numbers = re.findall(r"[\d.]+", cpm_str)
    if len(numbers) >= 2:
        return (float(numbers[0]), float(numbers[1]))
    if len(numbers) == 1:
        return (float(numbers[0]), float(numbers[0]))
    return (0.0, 0.0)


def calc_video_revenue(views: int, cpm_min: float, cpm_max: float) -> tuple[float, float]:
    """Calculate estimated revenue: views × CPM / 1000.

    Standard YouTube monetization formula:
        revenue = (views / 1000) × CPM
    """
    if not views or views <= 0:
        return (0.0, 0.0)
    rpm = views / 1000.0
    return (round(rpm * cpm_min, 2), round(rpm * cpm_max, 2))


def calc_ypp_progress(
    subscribers: int,
    watch_hours: float,
    weekly_subs_growth: float = 0,
    weekly_hours_growth: float = 0,
) -> dict:
    """Calculate YPP eligibility progress.

    Returns:
        dict with subs_pct, hours_pct, subs_remaining, hours_remaining,
        estimated_days_to_1k, estimated_days_to_4kh
    """
    YPP_SUBS = 1000
    YPP_HOURS = 4000

    subs_pct = min(100, round(subscribers / YPP_SUBS * 100, 1))
    hours_pct = min(100, round(watch_hours / YPP_HOURS * 100, 1))
    subs_remaining = max(0, YPP_SUBS - subscribers)
    hours_remaining = max(0, YPP_HOURS - watch_hours)

    # Estimate days to reach target based on weekly growth rate
    def _estimate_days(remaining, weekly_growth):
        if remaining <= 0:
            return 0
        if weekly_growth <= 0:
            return None  # Cannot predict — no positive growth
        daily_rate = weekly_growth / 7.0
        return round(remaining / daily_rate)

    est_days_subs = _estimate_days(subs_remaining, weekly_subs_growth)
    est_days_hours = _estimate_days(hours_remaining, weekly_hours_growth)

    return {
        "subs_pct": subs_pct,
        "hours_pct": hours_pct,
        "subs_remaining": subs_remaining,
        "hours_remaining": round(hours_remaining, 1),
        "estimated_days_to_1k_subs": est_days_subs,
        "estimated_days_to_4k_hours": est_days_hours,
        "ypp_eligible": subs_pct >= 100 and hours_pct >= 100,
    }


def calc_weekly_growth(current_value: float, previous_value: float) -> float:
    """Calculate absolute weekly growth."""
    if previous_value is None or previous_value <= 0:
        return 0.0
    return max(0, current_value - previous_value)


def calc_channel_revenue_total(db, channel_id: int) -> dict:
    """Calculate total estimated revenue for all videos of a channel."""
    # Use the DB method
    return db.get_channel_revenue_total(channel_id)


def calc_all_channels_ypp_progress(db) -> list[dict]:
    """Calculate YPP progress for all active channels."""
    channels = db.get_channels(active_only=True)
    result = []
    for ch in channels:
        # Get latest stats
        from database.db_extended import ExtendedDatabase
        stats_list = db.get_channel_stats_history(ch["id"], days=30)
        if not stats_list:
            result.append({
                "channel_id": ch["id"],
                "name": ch["name"],
                "slug": ch["slug"],
                "subscribers": 0,
                "watch_hours": 0,
                "subs_pct": 0,
                "hours_pct": 0,
                "estimated_days_to_1k_subs": None,
                "estimated_days_to_4k_hours": None,
                "ypp_eligible": False,
            })
            continue

        # Most recent snapshot
        latest = stats_list[-1]
        subs = latest.get("subscribers", 0)
        watch_minutes = latest.get("estimated_minutes_watched", 0) or 0
        watch_hours = round(watch_minutes / 60.0, 1)

        # Weekly growth: compare most recent with ~7 days ago
        subs_7d_ago = stats_list[0].get("subscribers", 0) if len(stats_list) > 1 else subs
        hours_7d_ago = round((stats_list[0].get("estimated_minutes_watched", 0) or 0) / 60.0, 1) if len(stats_list) > 1 else watch_hours

        weekly_subs_growth = subs - subs_7d_ago
        weekly_hours_growth = watch_hours - hours_7d_ago

        progress = calc_ypp_progress(
            subs, watch_hours,
            weekly_subs_growth=max(0, weekly_subs_growth),
            weekly_hours_growth=max(0, weekly_hours_growth),
        )

        result.append({
            "channel_id": ch["id"],
            "name": ch["name"],
            "slug": ch["slug"],
            "subscribers": subs,
            "watch_hours": watch_hours,
            **progress,
        })

    return result
