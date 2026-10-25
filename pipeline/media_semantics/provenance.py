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
    cost = dict(data.get("cost") or {})
    if decision.visual_mode:
        # Persisted through the existing v72 cost_json column (no migration).
        cost["visual"] = decision.visual_summary()
    if decision.source_route or decision.asset_type or decision.fallback_type:
        # P04 provenance is JSON-only in the existing cost_json column.
        cost["p04"] = {
            "scene_category": decision.scene_category,
            "asset_type": decision.asset_type,
            "fallback_type": decision.fallback_type,
            "ai_policy": decision.ai_policy,
            "literal_match": decision.literal_match,
            "conceptual_used": decision.conceptual_used,
            "duplicate_rejections": decision.duplicate_rejections,
            "source_route": dict(decision.source_route),
            "routing_budget": dict(decision.routing_budget),
            "coherence": dict(decision.coherence_signals),
            "provenance": dict(decision.provenance),
            # R1 diagnostics (small, JSON-safe): failover floor/cache and the
            # integrated rerank counterfactual.  No candidate payloads here.
            "failover": dict(decision.extra.get("failover") or {}),
            "integrated": dict(decision.extra.get("integrated") or {}),
            "post_failover_rejections": list(
                decision.extra.get("post_failover_rejections") or []
            ),
        }
    return {
        "video_id": video_id,
        "channel_id": channel_id,
        "scene_idx": int(scene_idx if scene_idx is not None else spec.get("scene_idx", 0)),
        "block_idx": block_idx if block_idx is not None else spec.get("block_idx"),
        "parent_scene_idx": spec.get("parent_scene_idx"),
        "media_request_id": spec.get("media_request_id", ""),
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
        "cost_json": json.dumps(cost, ensure_ascii=False),
        # P02 visual provenance is JSON-only (no images/base64), so the
        # existing v72 table can persist it without a schema migration.
        "visual_mode": data.get("visual_mode") or "",
        "visual_backend": data.get("visual_backend") or "",
        "degradation_level": data.get("degradation_level") or "",
        "visual_rejection_reason": data.get("visual_rejection_reason") or "",
        "visual_verification_status": data.get("visual_verification_status") or "",
        "visual_timing_ms": float(data.get("visual_timing_ms") or 0.0),
        "visual_json": json.dumps(decision.visual_summary(), ensure_ascii=False),
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
            visual_mode=decision.visual_mode,
            visual_backend=decision.visual_backend,
            visual_model=decision.visual_model,
            visual_similarity=decision.visual_similarity,
            frames_sampled=decision.frames_sampled,
            visual_verification_status=decision.visual_verification_status,
            visual_rejection_reason=decision.visual_rejection_reason,
            degradation_level=decision.degradation_level,
            combined_score=decision.combined_score,
            visual_timing_ms=decision.visual_timing_ms,
            visual_cost=decision.visual_cost,
            scene_category=decision.scene_category,
            asset_type=decision.asset_type,
            fallback_type=decision.fallback_type,
            literal_match=decision.literal_match,
            conceptual_used=decision.conceptual_used,
            duplicate_rejections=decision.duplicate_rejections,
            **identity,
        )
    except Exception:
        pass
