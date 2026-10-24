"""P02-H/L/M/N — frame aggregation, constraints and explainable combined score."""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

from .config import VisualWeights
from .contracts import SceneSpec
from .visual_vlm import VisualVerificationResult

# Stable machine-readable rejection reasons (P02-Q).
VISUAL_SUBJECT_MISMATCH = "VISUAL_SUBJECT_MISMATCH"
VISUAL_ACTION_MISMATCH = "VISUAL_ACTION_MISMATCH"
VISUAL_ENVIRONMENT_MISMATCH = "VISUAL_ENVIRONMENT_MISMATCH"
VISUAL_ERA_MISMATCH = "VISUAL_ERA_MISMATCH"
MUST_AVOID_VIOLATION = "MUST_AVOID_VIOLATION"
MUST_INCLUDE_MISSING = "MUST_INCLUDE_MISSING"
INSUFFICIENT_VISUAL_EVIDENCE = "INSUFFICIENT_VISUAL_EVIDENCE"
PREVIEW_UNAVAILABLE = "PREVIEW_UNAVAILABLE"
FRAME_EXTRACTION_FAILED = "FRAME_EXTRACTION_FAILED"
VLM_FAILED = "VLM_FAILED"

_TIMELESS_ERAS = {"", "atemporal", "presente", "present", "timeless", "modern", "contemporary"}


def cosine_similarity(left: Sequence[float] | None, right: Sequence[float] | None) -> float:
    if not left or not right:
        return 0.0
    n = min(len(left), len(right))
    dot = sum(float(left[i]) * float(right[i]) for i in range(n))
    lnorm = math.sqrt(sum(float(left[i]) ** 2 for i in range(n)))
    rnorm = math.sqrt(sum(float(right[i]) ** 2 for i in range(n)))
    if lnorm <= 0 or rnorm <= 0:
        return 0.0
    return max(0.0, min(1.0, dot / (lnorm * rnorm)))


def aggregate_frame_similarities(
    similarities: Sequence[float],
    *,
    min_similarity: float = 0.20,
    weights: VisualWeights | None = None,
) -> dict[str, Any]:
    """Aggregate multi-frame evidence without letting one frame dominate.

    ``coverage`` is the fraction of frames above the floor; ``consistency`` is
    driven by the population standard deviation.  A clip with one excellent
    frame out of six still gets moderate coverage/consistency and therefore
    cannot automatically beat a consistently relevant clip.
    """
    values = [max(0.0, min(1.0, float(v))) for v in similarities if v is not None]
    if not values:
        return {
            "count": 0, "max": 0.0, "mean": 0.0, "median": 0.0,
            "coverage": 0.0, "consistency": 0.0, "score": 0.0,
            "values": [],
        }
    w = weights or VisualWeights()
    max_v = max(values)
    mean_v = sum(values) / len(values)
    median_v = float(statistics.median(values))
    coverage = sum(1 for v in values if v >= float(min_similarity)) / len(values)
    std = float(statistics.pstdev(values)) if len(values) > 1 else 0.0
    consistency = max(0.0, 1.0 - min(1.0, std))
    weighted = (
        w.frame_max * max_v
        + w.frame_mean * mean_v
        + w.frame_median * median_v
        + w.frame_coverage * coverage
        + w.frame_consistency * consistency
    ) / w.frame_weights_total()
    return {
        "count": len(values),
        "max": round(max_v, 6),
        "mean": round(mean_v, 6),
        "median": round(median_v, 6),
        "coverage": round(coverage, 6),
        "consistency": round(consistency, 6),
        "score": round(max(0.0, min(1.0, weighted)), 6),
        "values": [round(v, 6) for v in values],
    }


def technical_quality_score(
    *,
    width: int | None = None,
    height: int | None = None,
    sharpness: float | None = None,
    min_width: int = 640,
) -> float:
    """0..1 quality proxy, neutral (0.5) when no signal exists."""
    scores: list[float] = []
    try:
        w = int(width or 0)
    except (TypeError, ValueError):
        w = 0
    if w > 0:
        scores.append(0.0 if w < min_width else min(1.0, w / 1920.0))
    if sharpness is not None:
        try:
            scores.append(max(0.0, min(1.0, float(sharpness) / 100.0)))
        except (TypeError, ValueError):
            pass
    if not scores:
        return 0.5
    return round(sum(scores) / len(scores), 6)


@dataclass
class VisualConstraintResult:
    rejection_reason: str = ""
    negative_penalty: float = 0.0
    era_penalty: float = 0.0
    must_include_penalty: float = 0.0
    violations: list[str] = field(default_factory=list)
    era_mismatch: bool = False
    subject_mismatch: bool = False
    action_mismatch: bool = False
    environment_mismatch: bool = False
    must_include_missing: bool = False
    evidence_unavailable: bool = False
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "rejection_reason": self.rejection_reason,
            "negative_penalty": round(float(self.negative_penalty), 6),
            "era_penalty": round(float(self.era_penalty), 6),
            "must_include_penalty": round(float(self.must_include_penalty), 6),
            "violations": list(self.violations),
            "era_mismatch": self.era_mismatch,
            "subject_mismatch": self.subject_mismatch,
            "action_mismatch": self.action_mismatch,
            "environment_mismatch": self.environment_mismatch,
            "must_include_missing": self.must_include_missing,
            "evidence_unavailable": self.evidence_unavailable,
            "details": dict(self.details),
        }


def evaluate_visual_constraints(
    spec: SceneSpec,
    verification: VisualVerificationResult | None,
    *,
    negative_embedding_hits: Iterable[tuple[str, float]] | None = None,
    match_threshold: float = 0.35,
    min_confidence: float = 0.45,
    must_avoid_threshold: float = 0.28,
    reject_on_negative: bool = True,
    reject_on_era: bool = True,
) -> VisualConstraintResult:
    """Turn VLM/embedding evidence into penalties plus explicit reasons.

    Unavailable/failed evidence is recorded but is NOT negative evidence.
    """
    result = VisualConstraintResult()
    hits = [
        (str(term), float(score))
        for term, score in (negative_embedding_hits or [])
        if float(score) >= float(must_avoid_threshold)
    ]
    if hits:
        result.violations.extend(term for term, _ in hits)
        result.details["embedding_negative_hits"] = [
            {"term": term, "similarity": round(score, 6)} for term, score in hits
        ]

    if verification is None or not verification.usable:
        result.evidence_unavailable = True
        result.details["verification_status"] = (
            verification.status if verification is not None else "NOT_RUN"
        )
        if hits and reject_on_negative:
            result.negative_penalty = 1.0
            result.rejection_reason = MUST_AVOID_VIOLATION
        return result

    confident = float(verification.confidence or 0.0) >= float(min_confidence)
    if verification.must_avoid_violation:
        result.violations.extend(verification.must_avoid_violation)
        result.negative_penalty = 1.0
        if reject_on_negative:
            result.rejection_reason = MUST_AVOID_VIOLATION

    era_relevant = str(spec.era or "").strip().lower() not in _TIMELESS_ERAS
    if era_relevant and verification.era_match is not None:
        if float(verification.era_match) < 0.50:
            result.era_mismatch = True
            result.era_penalty = max(
                result.era_penalty, 1.0 - float(verification.era_match)
            )
            if reject_on_era and not result.rejection_reason:
                result.rejection_reason = VISUAL_ERA_MISMATCH

    if verification.subject_match is not None and float(verification.subject_match) < match_threshold:
        result.subject_mismatch = True
    if verification.action_match is not None and float(verification.action_match) < match_threshold:
        result.action_mismatch = True
    if verification.environment_match is not None and float(verification.environment_match) < match_threshold:
        result.environment_mismatch = True
    if spec.must_include and verification.must_include_match is not None:
        if float(verification.must_include_match) < match_threshold:
            result.must_include_missing = True
            result.must_include_penalty = 1.0 - float(verification.must_include_match)

    if confident and verification.decision == "reject" and not result.rejection_reason:
        if result.subject_mismatch:
            result.rejection_reason = VISUAL_SUBJECT_MISMATCH
        elif result.action_mismatch:
            result.rejection_reason = VISUAL_ACTION_MISMATCH
        elif result.environment_mismatch:
            result.rejection_reason = VISUAL_ENVIRONMENT_MISMATCH
        elif result.must_include_missing:
            result.rejection_reason = MUST_INCLUDE_MISSING
        elif float(verification.overall_semantic_match or 0.0) < match_threshold:
            result.rejection_reason = INSUFFICIENT_VISUAL_EVIDENCE

    result.details["verification"] = verification.to_dict()
    return result


def compute_combined_score(
    *,
    text_relevance: float,
    preview_relevance: float = 0.0,
    frame_relevance: float = 0.0,
    vlm_relevance: float = 0.0,
    technical_quality: float = 0.5,
    negative_penalty: float = 0.0,
    era_penalty: float = 0.0,
    must_include_penalty: float = 0.0,
    weights: VisualWeights | None = None,
    available_components: dict[str, bool] | None = None,
) -> dict[str, Any]:
    """Weighted positive score minus explicit constraint penalties.

    ``available_components`` implements the P02 rule that *missing evidence is
    not negative evidence*.  When a mode does not run a stage (e.g. preview-only
    with no frames/VLM), the denominator is the sum of the weights that could
    actually be observed.  The ratios between available components are
    unchanged, so ranking is unaffected; only the score scale (and therefore
    threshold eligibility) becomes comparable across modes.  ``None`` keeps the
    historical behaviour for callers that do not opt in.
    """
    w = weights or VisualWeights()
    components = {
        "text": max(0.0, min(1.0, float(text_relevance))),
        "preview": max(0.0, min(1.0, float(preview_relevance))),
        "frame": max(0.0, min(1.0, float(frame_relevance))),
        "vlm": max(0.0, min(1.0, float(vlm_relevance))),
        "technical_quality": max(0.0, min(1.0, float(technical_quality))),
    }
    component_weights = {
        "text": w.text,
        "preview": w.preview,
        "frame": w.frame,
        "vlm": w.vlm,
        "technical_quality": w.technical_quality,
    }
    if available_components is None:
        denominator = w.positive_total()
    else:
        denominator = sum(
            weight
            for name, weight in component_weights.items()
            if available_components.get(name, name in ("text", "technical_quality"))
        )
        if denominator <= 0:
            denominator = w.positive_total()
    base = sum(
        component_weights[name] * components[name]
        for name in components
        if available_components is None
        or available_components.get(name, name in ("text", "technical_quality"))
    ) / denominator
    penalties = (
        max(0.0, float(negative_penalty)) * w.negative_penalty
        + max(0.0, float(era_penalty)) * w.era_penalty
        + max(0.0, float(must_include_penalty)) * w.must_include_penalty
    )
    final = max(0.0, min(1.0, base - penalties))
    return {
        "components": {k: round(v, 6) for k, v in components.items()},
        "base": round(base, 6),
        "negative_penalty": round(max(0.0, float(negative_penalty)), 6),
        "era_penalty": round(max(0.0, float(era_penalty)), 6),
        "must_include_penalty": round(max(0.0, float(must_include_penalty)), 6),
        "penalty_total": round(penalties, 6),
        "combined_score": round(final, 6),
        "weights": {
            "text": w.text, "preview": w.preview, "frame": w.frame,
            "vlm": w.vlm, "technical_quality": w.technical_quality,
            "negative_penalty": w.negative_penalty, "era_penalty": w.era_penalty,
            "must_include_penalty": w.must_include_penalty,
        },
    }
