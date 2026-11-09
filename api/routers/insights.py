"""AI Self-Optimization router — channel analysis and insight management.

Endpoints:
  POST   /{channel_id}/analyze              — launch async multi-pass LLM analysis
  DELETE /{channel_id}/analyze              — cancel an in-progress analysis
  GET    /{channel_id}/insights/latest       — poll for latest analysis results
  POST   /{channel_id}/insights/{id}/apply   — apply a recommendation to channel config
  POST   /{channel_id}/insights/{id}/validate — validate a code-change recommendation
  POST   /{channel_id}/insights/{id}/discard  — discard/restore a recommendation
  POST   /{channel_id}/insights/{id}/refine   — refine a recommendation via LLM chat
"""

from __future__ import annotations

import concurrent.futures
import json
import logging
import threading
from datetime import datetime, timezone

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


def _deep_merge(base: dict, overlay: dict) -> dict:
    """Recursively merge ``overlay`` into ``base``.

    Nested dict values (e.g. SOCIAL_REDISTRIBUTION) are merged key-by-key so a
    partial recommendation never wipes out subkeys the LLM did not mention.
    Non-dict values in ``overlay`` replace the base value entirely.
    """
    result = dict(base)
    for key, value in overlay.items():
        if (
            isinstance(value, dict)
            and isinstance(result.get(key), dict)
        ):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


# ── Endpoints ────────────────────────────────────────────────────────

@router.post("/{channel_id}/analyze")
def analyze_channel(channel_id: int):
    """Launch a multi-pass LLM analysis of channel performance data.

    The analysis runs in a background thread with streaming LLM calls.
    Poll ``GET /{channel_id}/insights/latest`` to track progress.

    Returns ``{insight_id, status: "processing"}`` immediately.
    """
    db = get_db()
    ch = db.get_channel(channel_id)
    if not ch:
        raise HTTPException(404, "Channel not found")

    # ── Dedup: if there is already a running analysis for this channel,
    # check whether it's truly alive or a stale orphan.
    import datetime as _dt
    existing = db.get_latest_insight(channel_id)
    if existing and existing.get("status") == "processing":
        # Check heartbeat-based freshness (v20.1)
        is_stale = True
        if existing.get("heartbeat_at"):
            try:
                hb_dt = _dt.datetime.strptime(
                    existing["heartbeat_at"], "%Y-%m-%d %H:%M:%S"
                )
                age_seconds = (_dt.datetime.utcnow() - hb_dt).total_seconds()
                is_stale = age_seconds > 180  # 3 min without heartbeat = dead
            except (ValueError, TypeError):
                pass  # can't parse heartbeat_at → treat as stale

        # Fallback: check generated_at if no heartbeat column (legacy rows)
        if is_stale and existing.get("generated_at"):
            try:
                gen_dt = _dt.datetime.strptime(
                    existing["generated_at"], "%Y-%m-%d %H:%M:%S"
                )
                age_seconds = (_dt.datetime.utcnow() - gen_dt).total_seconds()
                is_stale = age_seconds > 300  # 5 min fallback for legacy rows
            except (ValueError, TypeError):
                is_stale = True

        if is_stale:
            logger.warning(
                "Stale processing insight %d for channel %d — "
                "auto-failing and launching new analysis",
                existing["id"], channel_id,
            )
            db.fail_insight(
                existing["id"],
                "Auto-cleaned: analysis thread died (stale heartbeat)",
            )
        else:
            logger.info(
                "Analysis for channel %d already running (insight %d, heartbeat active) — skipping",
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


@router.delete("/{channel_id}/analyze")
def cancel_analysis(channel_id: int):
    """Cancel an in-progress analysis for this channel.

    Sets a cancel flag that the analysis thread checks before each
    LLM call and at phase boundaries. The row will be marked as
    'failed' with "Cancelado por el usuario".

    Returns 404 if no running analysis found.
    """
    db = get_db()
    ch = db.get_channel(channel_id)
    if not ch:
        raise HTTPException(404, "Channel not found")

    existing = db.get_latest_insight(channel_id)
    if not existing or existing.get("status") != "processing":
        raise HTTPException(404, "No running analysis to cancel")

    from api.services.channel_analyzer import request_cancel

    request_cancel(existing["id"])
    logger.info(
        "Cancel requested for channel %d analysis (insight %d)",
        channel_id, existing["id"],
    )
    return {
        "ok": True,
        "insight_id": existing["id"],
        "message": "Cancel requested — analysis will stop at next checkpoint",
    }


@router.get("/{channel_id}/insights/latest")
def get_latest_insight(channel_id: int):
    """Return the most recent relevant analysis for this channel.

    - If an analysis is currently ``processing``, return it (so the frontend
      shows live progress), but annotate if the heartbeat is stale.
    - If the latest analysis is ``failed``, return the most recent
      ``completed`` instead (don't show a failed state when there's
      a perfectly good completed analysis available).

    Returns 404 if no analysis — completed or in-progress — exists.
    """
    db = get_db()
    ch = db.get_channel(channel_id)
    if not ch:
        raise HTTPException(404, "Channel not found")

    insight = db.get_latest_insight(channel_id)
    if not insight:
        raise HTTPException(404, "No analysis found. Run POST /analyze first.")

    # If the latest row is failed, try to fall back to the last completed one
    if insight.get("status") == "failed":
        completed = db.get_latest_completed_insight(channel_id)
        if completed:
            return _format_insight(completed)

    return _format_insight(insight)


# ── Apply [unchanged] ──────────────────────────────────────────────────

_APPLY_LOCKS_GUARD = threading.Lock()
_APPLY_LOCKS: dict[int, threading.Lock] = {}

# Keys the analyzer can propose but the generation pipeline does not read.
# Applying them is accepted (stored) but cannot change output yet.
_INERT_KEYS = {"CONTENT_PILLARS", "script_end_hook"}

_DURATION_KEYS = {
    "VIDEO_AVERAGE_DURATION_MIN",
    "VIDEO_DURATION_DISCREPANCY_MIN",
    "PROD_VIDEO_DURATION_MIN",
    "PROD_VIDEO_DURATION_MAX",
}


def _apply_lock(channel_id: int) -> threading.Lock:
    """One lock per channel: serialize read-merge-write of config_json."""
    with _APPLY_LOCKS_GUARD:
        lock = _APPLY_LOCKS.get(channel_id)
        if lock is None:
            lock = threading.Lock()
            _APPLY_LOCKS[channel_id] = lock
        return lock


def _json_equal(a, b) -> bool:
    if a == b:
        return True
    try:
        return json.dumps(a, sort_keys=True, ensure_ascii=False) == json.dumps(
            b, sort_keys=True, ensure_ascii=False
        )
    except (TypeError, ValueError):
        return False


def _type_ok(value, reference) -> bool:
    """Reject patches that change the shape of an existing config value."""
    if reference is None:
        return True
    if isinstance(reference, bool):
        return isinstance(value, bool)
    if isinstance(reference, (int, float)):
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if isinstance(reference, str):
        return isinstance(value, str)
    if isinstance(reference, list):
        return isinstance(value, list)
    if isinstance(reference, dict):
        return isinstance(value, dict)
    return True


def _case_insensitive_get(mapping: dict, key: str):
    if key in mapping:
        return mapping[key]
    upper = key.upper()
    for existing, value in mapping.items():
        if existing.upper() == upper:
            return value
    return None


@router.post("/{channel_id}/insights/{insight_id}/apply")
def apply_insight(channel_id: int, insight_id: int, rec_id: str = Query(""),
                  refined_version_index: int = Query(-1)):
    """Apply one recommendation to the channel config.

    The write is normalized (canonical key names), validated (types and
    duration coherence), serialized per channel, and verified against the
    *effective* config before the recommendation is marked as applied.
    """
    db = get_db()

    ch = db.get_channel(channel_id)
    if not ch:
        raise HTTPException(404, "Channel not found")

    insight = db.get_insight(insight_id)
    if not insight:
        raise HTTPException(404, "Insight not found")
    if insight["channel_id"] != channel_id:
        raise HTTPException(400, "Insight does not belong to this channel")

    insights_raw = insight.get("insights_json", "{}")
    if isinstance(insights_raw, str):
        try:
            insights_data = json.loads(insights_raw)
        except json.JSONDecodeError:
            raise HTTPException(500, "Invalid insights_json in DB")
    else:
        insights_data = insights_raw

    recs = insights_data.get("recommendations", [])
    rec = next((r for r in recs if r.get("id") == rec_id), None)
    if not rec:
        raise HTTPException(
            404,
            f"Recommendation '{rec_id}' not found. "
            f"Available: {[r.get('id') for r in recs]}",
        )

    if refined_version_index >= 0:
        refined_versions = rec.get("refined_versions", [])
        if refined_version_index >= len(refined_versions):
            raise HTTPException(
                400,
                f"refined_version_index {refined_version_index} out of range "
                f"(0-{len(refined_versions) - 1})",
            )
        changes = refined_versions[refined_version_index].get("revised_config_changes", {})
        logger.info("Using refined version %d for rec %s", refined_version_index, rec_id)
    else:
        changes = rec.get("config_changes", {})

    if not changes:
        raise HTTPException(400, "Recommendation has no config_changes to apply")

    from config.config_bridge import (
        _config_cache,
        canonical_config_keys,
        get_channel_config,
        normalize_config_patch,
    )

    slug = ch["slug"]
    changes = normalize_config_patch(changes, slug)

    # ── Duration patch: translate a legacy range into the objective ──
    if (
        "PROD_VIDEO_DURATION_MIN" in changes
        and "PROD_VIDEO_DURATION_MAX" in changes
        and "VIDEO_AVERAGE_DURATION_MIN" not in changes
    ):
        try:
            from config.duration_policy import range_to_mean_disc
            mean, disc = range_to_mean_disc(
                changes["PROD_VIDEO_DURATION_MIN"],
                changes["PROD_VIDEO_DURATION_MAX"],
            )
            changes["VIDEO_AVERAGE_DURATION_MIN"] = mean
            changes["VIDEO_DURATION_DISCREPANCY_MIN"] = disc
        except (TypeError, ValueError):
            raise HTTPException(422, "Invalid duration range in recommendation")

    known = canonical_config_keys(slug)
    unknown = [k for k in changes if k.upper() not in known]
    warnings: list[str] = []
    if unknown:
        warnings.append(f"Unknown config keys (preserved): {unknown}")
    inert = [k for k in changes if k in _INERT_KEYS]
    if inert:
        warnings.append(
            f"Keys stored but not consumed by the pipeline yet: {inert}"
        )

    lock = _apply_lock(channel_id)
    if not lock.acquire(timeout=15):
        raise HTTPException(409, "Another apply is in progress for this channel; retry")
    try:
        # Re-read inside the lock: avoid merging over a concurrent write.
        ch = db.get_channel(channel_id)
        current_config_raw = ch.get("config_json", "{}")
        if isinstance(current_config_raw, str):
            try:
                current_config = json.loads(current_config_raw)
            except json.JSONDecodeError:
                current_config = {}
        else:
            current_config = dict(current_config_raw) if current_config_raw else {}

        # Type validation against the current value (case-insensitive).
        for key, value in changes.items():
            reference = _case_insensitive_get(current_config, key)
            if reference is not None and not _type_ok(value, reference):
                raise HTTPException(
                    422,
                    f"Invalid type for '{key}': expected "
                    f"{type(reference).__name__}, got {type(value).__name__}",
                )

        # Duration coherence: block issues introduced by this patch only.
        if any(k in _DURATION_KEYS for k in changes):
            from config.duration_policy import validate_duration_config
            before = set(validate_duration_config(current_config))
            candidate = _deep_merge(current_config, changes)
            new_issues = [i for i in validate_duration_config(candidate) if i not in before]
            if new_issues:
                raise HTTPException(
                    422,
                    "Incoherent duration patch: " + "; ".join(new_issues),
                )

        previous_config = dict(current_config)
        merged_config = _deep_merge(current_config, changes)

        # A case-variant duplicate of a key being changed would shadow the
        # canonical write depending on dict order. Drop the variants.
        changed_upper = {k.upper() for k in changes}
        for key in list(merged_config):
            if key.upper() in changed_upper and key not in changes:
                merged_config.pop(key, None)

        # Save once. Verification below decides whether it stays.
        db.update_channel(channel_id, config=merged_config)
        _config_cache.pop(slug, None)

        try:
            effective_ns = get_channel_config(slug, force_reload=True)
        except Exception as exc:
            db.update_channel(channel_id, config=previous_config)
            _config_cache.pop(slug, None)
            raise HTTPException(500, f"Could not load effective config: {exc}")

        effective_changes: dict = {}
        for key, value in changes.items():
            actual = getattr(effective_ns, key, None)
            if actual is None:
                actual = getattr(effective_ns, key.lower(), None)
            if not _json_equal(actual, value):
                # Roll back: the pipeline would not see this change.
                db.update_channel(channel_id, config=previous_config)
                _config_cache.pop(slug, None)
                raise HTTPException(
                    500,
                    f"Applied config was not effective for '{key}' "
                    f"(requested {value!r}, effective {actual!r}). Rolled back.",
                )
            effective_changes[key] = actual

        applied_status = "partial" if warnings else "effective"
        applied_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        db.mark_insight_applied(insight_id, applied_by="system")
        db.update_insight_recommendation(
            insight_id,
            rec_id,
            {
                "applied": True,
                "applied_at": applied_at,
                "applied_version_index": refined_version_index,
                "applied_changes": changes,
                "effective_changes": effective_changes,
                "applied_status": applied_status,
                "warnings": warnings,
            },
        )

        # Anything previously applied that touches the same keys is superseded.
        fresh = db.get_insight(insight_id) or {}
        raw = fresh.get("insights_json", "{}")
        try:
            fresh_data = json.loads(raw) if isinstance(raw, str) else (raw or {})
        except json.JSONDecodeError:
            fresh_data = {}
        for other in fresh_data.get("recommendations", []):
            if other.get("id") == rec_id or not other.get("applied"):
                continue
            other_changes = other.get("applied_changes") or other.get("config_changes") or {}
            other_changes = normalize_config_patch(other_changes, slug)
            overlap = set(other_changes) & set(changes)
            if not overlap:
                continue
            db.update_insight_recommendation(
                insight_id,
                other.get("id", ""),
                {
                    "applied_status": "superseded",
                    "superseded_by": rec_id,
                    "superseded_at": applied_at,
                    "superseded_keys": sorted(overlap),
                },
            )
            warnings.append(
                f"Superseded recommendation '{other.get('id')}' "
                f"(keys: {sorted(overlap)})"
            )
    finally:
        lock.release()

    logger.info(
        "Applied insight %d rec %s to channel %d: %s",
        insight_id, rec_id, channel_id,
        json.dumps(changes, ensure_ascii=False),
    )

    return {
        "ok": True,
        "applied_status": applied_status,
        "applied_changes": changes,
        "effective_changes": effective_changes,
        "warnings": warnings,
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
