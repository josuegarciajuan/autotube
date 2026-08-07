"""Analytics router — growth charts, content performance, and detailed video analytics."""
from fastapi import APIRouter, HTTPException, Query
from api.deps import get_db
from config.config_bridge import get_channel_config

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
    """Cross-channel comparison: growth rates, revenue per 1K views, CTR, and retention."""
    db = get_db()
    channels = db.get_channels(active_only=True)
    channel_ids = [ch["id"] for ch in channels]

    # Fetch CTR/retention aggregates for all channels at once
    ctr_retention = {}
    if channel_ids:
        try:
            ctr_retention = db.get_channels_ctr_retention(channel_ids)
        except Exception:
            pass

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

        # CTR and retention from deep analytics
        cr_data = ctr_retention.get(ch["id"], {})

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
            "avg_ctr": cr_data.get("avg_ctr"),
            "avg_retention": cr_data.get("avg_retention"),
            "total_impressions": cr_data.get("total_impressions"),
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


@router.get("/channels/{channel_id}/generation-failures")
def get_generation_failures(channel_id: int, days: int = 7):
    """Get script generation failure patterns for failover analysis.

    Returns aggregated stats: total attempts, failures by model and error type,
    emergency mode activations, and recent attempt history.
    """
    db = get_db()
    ch = db.get_channel(channel_id)
    if not ch:
        raise HTTPException(404, "Channel not found")

    patterns = db.get_generation_failure_patterns(canal=ch["slug"], days=days)
    patterns["channel_id"] = channel_id
    patterns["channel_name"] = ch["name"]
    patterns["channel_slug"] = ch["slug"]
    patterns["days"] = days
    return patterns


# ── Advanced Analytics: CTR, retention, traffic, demographics ──


@router.get("/channels/{channel_id}/analytics/ctr")
def get_channel_ctr(channel_id: int):
    """Get CTR, retention, and impressions summary for a channel.

    Returns avg CTR, avg retention, total impressions, and per-video
    breakdown from the last deep analytics collection.
    """
    db = get_db()
    ch = db.get_channel(channel_id)
    if not ch:
        raise HTTPException(404, "Channel not found")

    summary = db.get_channel_ctr_summary(channel_id)
    summary["channel_name"] = ch["name"]
    summary["channel_slug"] = ch["slug"]
    return summary


@router.get("/channels/{channel_id}/analytics/traffic")
def get_channel_traffic(channel_id: int):
    """Get traffic source breakdown for a channel.

    Aggregates views by source type (YT_SEARCH, SUGGESTED, EXTERNAL, etc.)
    from the last deep analytics collection.
    """
    db = get_db()
    ch = db.get_channel(channel_id)
    if not ch:
        raise HTTPException(404, "Channel not found")

    sources = db.get_channel_traffic_summary(channel_id)
    return {
        "channel_id": channel_id,
        "channel_name": ch["name"],
        "channel_slug": ch["slug"],
        "sources": sources,
    }


@router.get("/channels/{channel_id}/analytics/demographics")
def get_channel_demographics_endpoint(channel_id: int):
    """Get audience demographics (age + gender) for a channel.

    Returns the latest snapshot from the deep analytics collection.
    """
    db = get_db()
    ch = db.get_channel(channel_id)
    if not ch:
        raise HTTPException(404, "Channel not found")

    demographics = db.get_channel_demographics(channel_id)
    fetched_at = demographics[0]["fetched_at"] if demographics else None

    return {
        "channel_id": channel_id,
        "channel_name": ch["name"],
        "channel_slug": ch["slug"],
        "age_gender": [
            {"age_group": d["age_group"], "gender": d["gender"],
             "views_pct": d["views_pct"]}
            for d in demographics
        ],
        "fetched_at": fetched_at,
    }


# ── SEO Research & Scoring ──────────────────────────────────────


@router.post("/seo/keyword-research")
def keyword_research(topic: str, channel_id: int, geo: str = "ES"):
    """Get trending keywords for a topic.

    Used by frontend for manual keyword research.  Uses the same
    fallback chain as the pipeline (pytrends → YouTube autocomplete
    → static channel keywords).

    Args:
        topic: Search topic (e.g. "civilizaciones antiguas").
        channel_id: Channel ID for config context.
        geo: Google Trends country code (default "ES").

    Returns trending keywords, autocomplete suggestions, and
    optimized tag recommendations.
    """
    db = get_db()
    ch = db.get_channel(channel_id)
    if not ch:
        raise HTTPException(404, "Channel not found")

    cfg = get_channel_config(ch["slug"])
    from pipeline.seo_researcher import SEOResearcher
    seo = SEOResearcher(ch["slug"], cfg)

    return {
        "topic": topic,
        "channel": ch["name"],
        "trending_keywords": seo.get_trending_keywords(topic, geo=geo),
        "autocomplete_suggestions": seo.get_youtube_autocomplete(topic),
        "recommended_tags": seo.optimize_tags(getattr(cfg, "CHANNEL_KEYWORDS", []), topic),
    }


@router.get("/channels/{channel_id}/seo-score")
def get_channel_seo_score(channel_id: int):
    """Calculate SEO health score for a channel.

    Evaluates recent videos (last 30 days) across 5 dimensions:
      - **title_length** (0-3 pts): avg title in 40-65 char sweet spot
      - **power_words** (0-2 pts): % of titles with at least 1 power word
      - **description_length** (0-2 pts): avg description > 1500 chars
      - **timestamps** (0-2 pts): % of videos with chapter timestamps
      - **tag_count** (0-1 pts): avg 7-10 tags per video

    Returns an integer score 0-10 with per-dimension breakdown.
    """
    db = get_db()
    ch = db.get_channel(channel_id)
    if not ch:
        raise HTTPException(404, "Channel not found")

    cfg = get_channel_config(ch["slug"])

    # Fetch recent videos from DB
    videos = db.get_videos(channel_id=channel_id, limit=50) or []
    if not videos:
        return {
            "channel_id": channel_id,
            "channel_name": ch["name"],
            "score": 0,
            "score_max": 10,
            "videos_analyzed": 0,
            "breakdown": {
                "title_length": {"score": 0, "max": 3, "detail": "no videos"},
                "power_words": {"score": 0, "max": 2, "detail": "no videos"},
                "description_length": {"score": 0, "max": 2, "detail": "no videos"},
                "timestamps": {"score": 0, "max": 2, "detail": "no videos"},
                "tag_count": {"score": 0, "max": 1, "detail": "no videos"},
            },
        }

    # ── Analyze videos ─────────────────────────────────────────
    power_words = getattr(cfg, "TITLE_POWER_WORDS", []) or []
    pw_set = set(w.lower() for w in power_words)

    title_lengths: list[int] = []
    title_has_pw: list[bool] = []
    desc_lengths: list[int] = []
    has_timestamps: list[bool] = []
    tag_counts: list[int] = []

    import re as _re

    for v in videos:
        title = (v.get("titulo_final") or v.get("title") or "").strip()
        # Description may be stored in metadata_json or similar fields
        description = (
            v.get("description") or
            (v.get("metadata_json") or "")
        ).strip()
        if isinstance(description, dict):
            description = description.get("description", "")
        # Tags: try tags column, then keywords_json, then extract from metadata
        tags_raw = v.get("tags")
        if not tags_raw:
            tags_raw = v.get("keywords_json") or v.get("metadata_json", {}).get("tags", []) or []

        # Title length
        if title:
            title_lengths.append(len(title))
            # Power word check
            title_lower = title.lower()
            has_pw = any(pw in title_lower for pw in pw_set)
            title_has_pw.append(has_pw)

        # Description length
        if description:
            desc_lengths.append(len(description))
            # Timestamp check: "0:00" or "0:00 -" or "0:00 —" at start
            ts_match = _re.search(r"\b\d{1,2}:\d{2}\b", description)
            has_timestamps.append(bool(ts_match))

        # Tag count
        if tags_raw:
            if isinstance(tags_raw, str):
                try:
                    import json as _json
                    tags_list = _json.loads(tags_raw)
                except Exception:
                    tags_list = [t.strip() for t in tags_raw.split(",") if t.strip()]
            elif isinstance(tags_raw, list):
                tags_list = tags_raw
            else:
                tags_list = []
            tag_counts.append(len(tags_list))

    n = len(videos)
    weights = getattr(cfg, "SEO_SCORE_WEIGHTS", {
        "title_length": 3, "power_words": 2,
        "description_length": 2, "timestamps": 2, "tag_count": 1,
    })

    # ── Score: title_length (3 pts) ──────────────────────────────
    if title_lengths:
        avg_len = sum(title_lengths) / len(title_lengths)
        # Ideal: 40-65 chars. Graded linearly.
        if 40 <= avg_len <= 65:
            tl_score = weights["title_length"]
            tl_detail = f"avg {avg_len:.0f} chars (ideal 40-65)"
        elif avg_len < 40:
            tl_score = round(weights["title_length"] * (avg_len / 40), 1)
            tl_detail = f"avg {avg_len:.0f} chars (too short, target 40-65)"
        else:
            # > 65 — still good but slight penalty above 100
            ratio = max(0, 1 - (avg_len - 65) / 35)
            tl_score = round(weights["title_length"] * ratio, 1)
            tl_detail = f"avg {avg_len:.0f} chars (slightly long, target 40-65)"
    else:
        tl_score = 0
        tl_detail = "no titles analyzed"

    # ── Score: power_words (2 pts) ────────────────────────────────
    if title_has_pw:
        pw_ratio = sum(title_has_pw) / len(title_has_pw)
        pw_score = round(weights["power_words"] * pw_ratio, 1)
        pw_detail = f"{sum(title_has_pw)}/{len(title_has_pw)} titles ({pw_ratio:.0%})"
    else:
        pw_score = 0
        pw_detail = "no titles analyzed"

    # ── Score: description_length (2 pts) ─────────────────────────
    if desc_lengths:
        avg_desc = sum(desc_lengths) / len(desc_lengths)
        # Target: > 1500 chars
        ratio = min(1.0, avg_desc / 1500)
        dl_score = round(weights["description_length"] * ratio, 1)
        dl_detail = f"avg {avg_desc:.0f} chars (target 1500+)"
    else:
        dl_score = 0
        dl_detail = "no descriptions analyzed"

    # ── Score: timestamps (2 pts) ─────────────────────────────────
    if has_timestamps:
        ts_ratio = sum(has_timestamps) / len(has_timestamps)
        ts_score = round(weights["timestamps"] * ts_ratio, 1)
        ts_detail = f"{sum(has_timestamps)}/{len(has_timestamps)} videos ({ts_ratio:.0%})"
    else:
        ts_score = 0
        ts_detail = "no descriptions analyzed"

    # ── Score: tag_count (1 pts) ──────────────────────────────────
    if tag_counts:
        avg_tags = sum(tag_counts) / len(tag_counts)
        # Ideal: 7-10 tags
        if 7 <= avg_tags <= 10:
            tc_score = weights["tag_count"]
            tc_detail = f"avg {avg_tags:.1f} tags (ideal 7-10)"
        elif avg_tags < 7:
            tc_score = round(weights["tag_count"] * (avg_tags / 7), 1)
            tc_detail = f"avg {avg_tags:.1f} tags (low, target 7-10)"
        else:
            tc_score = weights["tag_count"]
            tc_detail = f"avg {avg_tags:.1f} tags (ideal 7-10)"
    else:
        tc_score = 0
        tc_detail = "no tags analyzed"

    total_score = round(tl_score + pw_score + dl_score + ts_score + tc_score, 1)

    return {
        "channel_id": channel_id,
        "channel_name": ch["name"],
        "channel_slug": ch["slug"],
        "score": total_score,
        "score_max": 10,
        "videos_analyzed": n,
        "breakdown": {
            "title_length": {"score": tl_score, "max": weights["title_length"], "detail": tl_detail},
            "power_words": {"score": pw_score, "max": weights["power_words"], "detail": pw_detail},
            "description_length": {"score": dl_score, "max": weights["description_length"], "detail": dl_detail},
            "timestamps": {"score": ts_score, "max": weights["timestamps"], "detail": ts_detail},
            "tag_count": {"score": tc_score, "max": weights["tag_count"], "detail": tc_detail},
        },
    }
