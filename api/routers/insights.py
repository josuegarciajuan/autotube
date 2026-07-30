"""AI Self-Optimization router — channel analysis and insight management.

Endpoints:
  POST /{channel_id}/analyze              — launch async multi-pass LLM analysis
  GET  /{channel_id}/insights/latest       — poll for latest analysis results
  POST /{channel_id}/insights/{id}/apply   — apply a recommendation to channel config
"""

from __future__ import annotations

import concurrent.futures
import json
import logging

from fastapi import APIRouter, HTTPException, Query

from api.deps import get_db

logger = logging.getLogger(__name__)

router = APIRouter()

# Dedicated single-thread executor for analysis (one at a time to control costs)
_INSIGHTS_EXECUTOR = concurrent.futures.ThreadPoolExecutor(
    max_workers=1,
    thread_name_prefix="autotube-insights-",
)


def _format_insight(row: dict) -> dict:
    """Parse JSON fields for frontend consumption."""
    for field in ("insights_json", "raw_patterns", "raw_hypotheses"):
        val = row.get(field)
        if isinstance(val, str) and val:
            try:
                row[field] = json.loads(val)
            except (json.JSONDecodeError, TypeError):
                pass
    return dict(row)


# ── Endpoints ────────────────────────────────────────────────────────

@router.post("/{channel_id}/analyze")
def analyze_channel(channel_id: int):
    """Launch a multi-pass LLM analysis of channel performance data.

    The analysis runs in a background thread and takes 30-60 seconds.
    Poll ``GET /{channel_id}/insights/latest`` to track progress.

    Returns ``{insight_id, status: "processing"}`` immediately.
    """
    db = get_db()
    ch = db.get_channel(channel_id)
    if not ch:
        raise HTTPException(404, "Channel not found")

    insight_id = db.create_insight(channel_id)

    from api.services.channel_analyzer import run_channel_analysis_sync

    _INSIGHTS_EXECUTOR.submit(
        run_channel_analysis_sync, insight_id, channel_id, ch["slug"]
    )

    logger.info("Analysis launched for channel %d (insight %d)", channel_id, insight_id)
    return {"insight_id": insight_id, "status": "processing"}


@router.get("/{channel_id}/insights/latest")
def get_latest_insight(channel_id: int):
    """Return the most recent analysis for this channel.

    While ``status == "processing"``, intermediate results (raw_patterns,
    raw_hypotheses) may be populated as each phase completes.  The frontend
    polls this endpoint every 3 seconds during analysis.

    Returns 404 if no analysis has ever been run for this channel.
    """
    db = get_db()
    ch = db.get_channel(channel_id)
    if not ch:
        raise HTTPException(404, "Channel not found")

    insight = db.get_latest_insight(channel_id)
    if not insight:
        raise HTTPException(404, "No analysis found. Run POST /analyze first.")

    return _format_insight(insight)


@router.post("/{channel_id}/insights/{insight_id}/apply")
def apply_insight(channel_id: int, insight_id: int, rec_id: str = Query("")):
    """Apply one recommendation from an analysis to the channel config.

    Query params:
        rec_id:  the recommendation UUID to apply (from insights_json.recommendations[].id)

    Reads the recommendation's ``config_changes`` dict, merges them into the
    channel's ``config_json``, saves to DB, and invalidates the config bridge cache.
    Marks the insight row as applied.
    """
    db = get_db()

    # Validate channel
    ch = db.get_channel(channel_id)
    if not ch:
        raise HTTPException(404, "Channel not found")

    # Validate insight
    insight = db.get_insight(insight_id)
    if not insight:
        raise HTTPException(404, "Insight not found")
    if insight["channel_id"] != channel_id:
        raise HTTPException(400, "Insight does not belong to this channel")

    # Parse insights
    insights_raw = insight.get("insights_json", "{}")
    if isinstance(insights_raw, str):
        try:
            insights_data = json.loads(insights_raw)
        except json.JSONDecodeError:
            raise HTTPException(500, "Invalid insights_json in DB")
    else:
        insights_data = insights_raw

    # Find the specific recommendation
    recs = insights_data.get("recommendations", [])
    rec = next((r for r in recs if r.get("id") == rec_id), None)
    if not rec:
        raise HTTPException(
            404,
            f"Recommendation '{rec_id}' not found. "
            f"Available: {[r.get('id') for r in recs]}",
        )

    changes = rec.get("config_changes", {})
    if not changes:
        raise HTTPException(400, "Recommendation has no config_changes to apply")

    # Merge into current config
    current_config_raw = ch.get("config_json", "{}")
    if isinstance(current_config_raw, str):
        try:
            current_config = json.loads(current_config_raw)
        except json.JSONDecodeError:
            current_config = {}
    else:
        current_config = dict(current_config_raw) if current_config_raw else {}

    current_config.update(changes)

    # Save
    db.update_channel(channel_id, config=current_config)
    db.mark_insight_applied(insight_id, applied_by="system")

    # Invalidate config bridge cache so next pipeline run picks up changes
    try:
        from config.config_bridge import _config_cache
        _config_cache.pop(ch["slug"], None)
    except Exception:
        pass

    logger.info(
        "Applied insight %d rec %s to channel %d: %s",
        insight_id, rec_id, channel_id,
        json.dumps(changes, ensure_ascii=False),
    )

    return {
        "ok": True,
        "applied_changes": changes,
        "recommendation_title": rec.get("title", ""),
    }
