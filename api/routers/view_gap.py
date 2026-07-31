"""View Gap REST API — coverage, history, unregistered videos, manual scan."""

from fastapi import APIRouter, HTTPException, Query
from api.deps import get_db
import json
import logging

logger = logging.getLogger("autotube.view_gap")

router = APIRouter()


@router.get("/monitor/view-gap/coverage")
def get_coverage_summary(channel_id: int = Query(None)):
    """Coverage percentage per channel: db_tracked / yt_total * 100.

    Reads the latest gap state persisted by the ViewGapMonitor from
    the system_state table. If no state exists yet, the monitor has
    not run for that channel.

    Without channel_id: returns all channels.
    With channel_id: returns only that channel.
    """
    db = get_db()
    results = []

    channels = db.get_channels(active_only=True)
    for ch in channels:
        cid = ch["id"]
        if channel_id is not None and cid != channel_id:
            continue

        slug = ch.get("slug", "")
        key = f"view_gap_{slug}"
        raw = db.get_system_state(key)

        if raw:
            try:
                data = json.loads(raw)
                results.append({
                    "channel_id": cid,
                    "slug": slug,
                    "name": ch.get("name", slug),
                    "coverage_pct": data.get("coverage_pct", 0),
                    "gap": data.get("gap", 0),
                    "delta_24h": data.get("delta", 0),
                    "yt_total": data.get("yt_total_views", 0),
                    "db_total": data.get("db_total_views", 0),
                    "db_longform": data.get("db_longform_views", 0),
                    "db_shorts": data.get("db_shorts_views", 0),
                    "last_checked": data.get("last_checked", ""),
                })
            except (json.JSONDecodeError, TypeError) as exc:
                logger.warning("Corrupt gap state for %s: %s", slug, exc)
        else:
            # No monitor data yet — calculate from latest stats
            try:
                known = db.get_db_known_views_sum(cid)
                latest = db.get_channel_latest_stats(cid)
                if latest and latest.get("total_views", 0) > 0:
                    yt_t = latest["total_views"]
                    cov = round(known["total"] / yt_t * 100, 1) if yt_t > 0 else 100.0
                    results.append({
                        "channel_id": cid,
                        "slug": slug,
                        "name": ch.get("name", slug),
                        "coverage_pct": cov,
                        "gap": max(0, yt_t - known["total"]),
                        "delta_24h": 0,
                        "yt_total": yt_t,
                        "db_total": known["total"],
                        "db_longform": known["longform_views"],
                        "db_shorts": known["shorts_views"],
                        "last_checked": latest.get("fetched_at", ""),
                    })
            except Exception as exc:
                logger.debug("Coverage fallback failed for %s: %s", slug, exc)

    return {"channels": results}


@router.get("/monitor/view-gap/unregistered")
def get_unregistered_videos(
    channel_id: int = Query(None),
    limit: int = Query(50, ge=1, le=200),
):
    """List videos discovered via YT scan not in our pipeline.

    These are videos registered with source_mode='yt_scan' and
    status='unregistered' — they exist on YouTube but were never
    created by the Autotube pipeline.
    """
    db = get_db()
    rows = db.get_unregistered_videos(channel_id=channel_id, limit=limit)
    return {"videos": rows, "total": len(rows)}


@router.post("/monitor/view-gap/scan/{channel_id}")
def trigger_manual_scan(channel_id: int):
    """Manually trigger a view gap scan + unregistered video discovery.

    Runs synchronously on demand. Use this when you suspect a viral
    video exists but the daily check hasn't fired yet.
    """
    from api.services.view_gap_monitor import ViewGapMonitor

    db = get_db()
    ch = db.get_channel(channel_id)
    if not ch:
        raise HTTPException(404, "Channel not found")

    monitor = ViewGapMonitor()
    try:
        result = monitor.check_channel(db, ch)
    except Exception as exc:
        logger.error("Manual gap scan failed for channel %d: %s", channel_id, exc)
        raise HTTPException(500, f"Scan failed: {exc}")

    return {"ok": True, **result}


@router.post("/monitor/view-gap/scan-all")
def trigger_manual_scan_all():
    """Manually trigger a full view gap scan across all active channels."""
    from api.services.view_gap_monitor import ViewGapMonitor

    db = get_db()
    monitor = ViewGapMonitor()
    try:
        result = monitor.check_all_channels(db)
    except Exception as exc:
        logger.error("Full gap scan failed: %s", exc)
        raise HTTPException(500, f"Scan failed: {exc}")

    return {"ok": True, **result}
