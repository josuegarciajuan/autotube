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

    # ── Dedup: if there is already a running analysis for this channel,
    # return it instead of creating a duplicate that will just queue up.
    existing = db.get_latest_insight(channel_id)
    if existing and existing.get("status") == "processing":
        logger.info(
            "Analysis for channel %d already running (insight %d) — skipping new launch",
            channel_id, existing["id"],
        )
        return {
            "insight_id": existing["id"],
            "status": "processing",
            "already_running": True,
        }

    insight_id = db.create_insight(channel_id)

    from api.services.channel_analyzer import run_channel_analysis_sync

    _INSIGHTS_EXECUTOR.submit(
        run_channel_analysis_sync, insight_id, channel_id, ch["slug"]
    )

    logger.info("Analysis launched for channel %d (insight %d)", channel_id, insight_id)
    return {"insight_id": insight_id, "status": "processing", "already_running": False}


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
def apply_insight(channel_id: int, insight_id: int, rec_id: str = Query(""),
                  refined_version_index: int = Query(-1)):
    """Apply one recommendation from an analysis to the channel config.

    Query params:
        rec_id:                  the recommendation UUID to apply
        refined_version_index:   if >= 0, use refined_versions[index].revised_config_changes
                                 instead of the original config_changes
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

    # Determine which config_changes to use
    if refined_version_index >= 0:
        refined_versions = rec.get("refined_versions", [])
        if refined_version_index >= len(refined_versions):
            raise HTTPException(
                400,
                f"refined_version_index {refined_version_index} out of range "
                f"(0-{len(refined_versions) - 1})"
            )
        changes = refined_versions[refined_version_index].get("revised_config_changes", {})
        logger.info("Using refined version %d for rec %s", refined_version_index, rec_id)
    else:
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

    # Persist applied flag on the individual recommendation so it survives reloads
    db.update_insight_recommendation(insight_id, rec_id, {"applied": True})

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


# ── Validate (code-change recommendations) ───────────────────────────

_VALIDATION_EXECUTOR = concurrent.futures.ThreadPoolExecutor(
    max_workers=1,
    thread_name_prefix="autotube-validate-",
)


@router.post("/{channel_id}/insights/{insight_id}/validate")
def validate_insight(channel_id: int, insight_id: int, rec_id: str = Query("")):
    """Validate whether a code-change recommendation's symptoms have been resolved.

    Runs a focused LLM check comparing the original recommendation against
    current channel data. Returns a verdict with evidence.

    Query params:
        rec_id:  the recommendation UUID to validate
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

    # Find the recommendation
    recs = insights_data.get("recommendations", [])
    rec = next((r for r in recs if r.get("id") == rec_id), None)
    if not rec:
        raise HTTPException(
            404,
            f"Recommendation '{rec_id}' not found. "
            f"Available: {[r.get('id') for r in recs]}",
        )

    # Run validation in background thread
    from api.services.channel_analyzer import run_validation_check

    future = _VALIDATION_EXECUTOR.submit(
        run_validation_check, insight_id, channel_id, ch["slug"], rec_id, rec
    )

    try:
        validation = future.result(timeout=120)
    except concurrent.futures.TimeoutError:
        raise HTTPException(504, "Validation timed out")

    # Persist validation result so it survives reloads
    db.update_insight_recommendation(insight_id, rec_id, {"validation": validation})

    return {"ok": True, "validation": validation, "rec_id": rec_id}


# ── Discard / restore recommendation ──────────────────────────────────

@router.post("/{channel_id}/insights/{insight_id}/discard")
def discard_insight_recommendation(channel_id: int, insight_id: int,
                                   rec_id: str = Query(""),
                                   discarded: bool = Query(True)):
    """Persist discarded/restored state for one recommendation inside an insight.

    Query params:
        rec_id:     the recommendation UUID to discard or restore
        discarded:  True to discard, False to restore (default True)
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

    ok = db.update_insight_recommendation(insight_id, rec_id, {"discarded": discarded})
    if not ok:
        raise HTTPException(
            404,
            f"Recommendation '{rec_id}' not found in insight {insight_id}",
        )

    logger.info(
        "Recommendation %s in insight %d %s",
        rec_id, insight_id,
        "discarded" if discarded else "restored",
    )
    return {"ok": True, "rec_id": rec_id, "discarded": discarded}


# ── Refine (config-change recommendations) ───────────────────────────

from pydantic import BaseModel


class RefineRequest(BaseModel):
    rec_id: str
    user_feedback: str
    conversation_history: list[dict] | None = None


@router.post("/{channel_id}/insights/{insight_id}/refine")
def refine_insight(channel_id: int, insight_id: int, body: RefineRequest):
    """Refine a config-change recommendation based on user feedback.

    Takes the original recommendation + user feedback and asks the LLM
    to produce revised config_changes. Stores the refined version back
    into the insight's JSON.

    Body:
        rec_id:                 the recommendation UUID to refine
        user_feedback:          what the user wants changed
        conversation_history:   optional list of prior messages [{role, content}, ...]
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

    # Find the recommendation
    recs = insights_data.get("recommendations", [])
    rec = next((r for r in recs if r.get("id") == body.rec_id), None)
    if not rec:
        raise HTTPException(
            404,
            f"Recommendation '{body.rec_id}' not found. "
            f"Available: {[r.get('id') for r in recs]}",
        )

    is_code_change = rec.get("requires_code", False)

    if not is_code_change and not rec.get("config_changes"):
        raise HTTPException(400, "Recommendation has no config_changes to refine")

    # Get current config for context
    try:
        from config.config_bridge import get_channel_config
        config_ns = get_channel_config(ch["slug"], force_reload=True)
        from api.services.channel_analyzer import _serialize_config
        current_config = _serialize_config(config_ns)
    except Exception:
        current_config = {}

    # Run refinement
    from api.services.channel_analyzer import run_refine_recommendation

    result = run_refine_recommendation(
        rec_id=body.rec_id,
        recommendation=rec,
        user_feedback=body.user_feedback,
        current_config=current_config,
        conversation_history=body.conversation_history,
        is_code_change=is_code_change,
    )

    if result.get("cannot_fulfill"):
        return {
            "ok": True,
            "cannot_fulfill": True,
            "cannot_fulfill_reason": result.get("cannot_fulfill_reason", ""),
            "rec_id": body.rec_id,
        }

    # Store the refined version in the insight's JSON
    refined_entry = {
        "revised_config_changes": result["revised_config_changes"],
        "explanation": result["explanation"],
        "triggered_by": body.user_feedback,
        "refined_at": __import__("time").strftime("%Y-%m-%dT%H:%M:%SZ",
                                                  __import__("time").gmtime()),
    }

    existing_refined = rec.setdefault("refined_versions", [])
    existing_refined.append(refined_entry)

    db.update_insight_recommendation(
        insight_id, body.rec_id,
        {"refined_versions": existing_refined}
    )

    logger.info(
        "Refined rec %s for channel %d: %s",
        body.rec_id, channel_id,
        json.dumps(result["revised_config_changes"], ensure_ascii=False)[:200],
    )

    return {
        "ok": True,
        "explanation": result["explanation"],
        "revised_config_changes": result["revised_config_changes"],
        "rec_id": body.rec_id,
        "refined_at": refined_entry["refined_at"],
    }
