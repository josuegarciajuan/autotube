"""P01-K — provenance for one scene/asset decision.

Two sinks, both fail-open:

1. ``obs_event`` JSONL (always available, already used by the pipeline).
2. The ``media_selection_decisions`` table (via ``ExtendedDatabase``), flushed by
   the orchestrator once the ``video_id`` exists.

Records are bounded: at most ``provenance_candidate_limit`` candidate scores
and ``provenance_rejected_limit`` rejected candidates are persisted.
"""

from __future__ import annotations

import json
from typing import Any

from .contracts import SelectionDecision


def decision_record(
    decision: SelectionDecision,
    *,
    video_id: int | None = None,
    channel_id: int | None = None,
    scene_idx: int | None = None,
    block_idx: int | None = None,
) -> dict[str, Any]:
    """Convert a decision into a JSON-safe DB/log record."""
    data = decision.to_dict()
    spec = data.get("scene_spec") or {}
    winner = data.get("winner") or {}
    return {
        "video_id": video_id,
        "channel_id": channel_id,
        "scene_idx": int(scene_idx if scene_idx is not None else spec.get("scene_idx", 0)),
        "block_idx": block_idx if block_idx is not None else spec.get("block_idx"),
        "parent_scene_idx": spec.get("parent_scene_idx"),
        "origin": spec.get("origin", "block"),
        "narration_fragment": spec.get("narration_fragment", ""),
        "selector_version": data.get("selector_version", ""),
        "status": data.get("status", ""),
        "threshold": data.get("threshold", 0.0),
        "final_score": data.get("winner_score", 0.0),
        "rejection_reason": data.get("rejection_reason", ""),
        "fallback_used": 1 if data.get("fallback_used") else 0,
        "fallback_reason": data.get("fallback_reason", ""),
        "scene_spec_json": json.dumps(spec, ensure_ascii=False),
        "queries_json": json.dumps(data.get("queries") or [], ensure_ascii=False),
        "providers_json": json.dumps(
            data.get("providers_consulted") or [], ensure_ascii=False
        ),
        "candidates_json": json.dumps(
            data.get("candidate_scores") or [], ensure_ascii=False
        ),
        "rejected_json": json.dumps(data.get("rejected") or [], ensure_ascii=False),
        "winner_json": json.dumps(winner, ensure_ascii=False) if winner else "",
        "winner_provider": winner.get("provider", "") if winner else "",
        "winner_url": (
            winner.get("download_url") or winner.get("asset_url") or ""
        ) if winner else "",
        "candidate_count": int(data.get("candidates_considered") or 0),
        "download_attempts_json": json.dumps(
            data.get("download_attempts") or [], ensure_ascii=False
        ),
        "timing_ms": float(data.get("timing_ms") or 0.0),
        "cost_json": json.dumps(data.get("cost") or {}, ensure_ascii=False),
    }


def emit_decision_event(decision: SelectionDecision, **identity: Any) -> None:
    """Emit one compact obs_event for the decision (never raises)."""
    try:
        from pipeline.observability import obs_event

        spec = decision.scene_spec
        winner = decision.winner
        obs_event(
            "media_selection_decision",
            scene_idx=spec.scene_idx,
            block_idx=spec.block_idx,
            origin=spec.origin,
            narration_fragment=(spec.narration_fragment or "")[:240],
            selector_version=decision.selector_version,
            status=decision.status.value,
            threshold=decision.threshold,
            winner_score=decision.winner_score,
            final_score=decision.winner_score,
            winner_provider=(winner.provider if winner else ""),
            winner_url=((winner.download_url or winner.asset_url) if winner else ""),
            winner_query=(winner.query if winner else ""),
            candidates=decision.candidates_considered,
            rejected=len(decision.rejected),
            fallback_used=decision.fallback_used,
            fallback_reason=decision.fallback_reason,
            rejection_reason=decision.rejection_reason,
            queries=(decision.queries or [])[:12],
            providers=(decision.providers_consulted or [])[:8],
            timing_ms=decision.timing_ms,
            **identity,
        )
    except Exception:
        pass
