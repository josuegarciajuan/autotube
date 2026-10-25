"""P05-C/G/H/J — unified candidate universe, global rerank and two-stage fallback.

This module is the decision core used by ``SourcedMediaSelector`` when
``integrated=True``.  It does not retrieve and does not download: it receives
one universe (stock video/image, archive, scientific, generated) and returns
one global ordering.

Two-stage policy
----------------
1. **REAL** assets (stock, archive, scientific) are ranked together.  If one
   clears the real threshold, generated/conceptual fallbacks are never tried.
2. Only when no real asset qualifies do AI/conceptual/generic candidates enter
   a second round.  An AI image must pass the same visual evidence gate as a
   real asset; a prompt is not proof of pixels.

The routing prior is capped (``UnifiedWeights.routing_prior_cap``), so a
preferred source can win a close call but cannot overturn a clearly more
relevant asset.  See ``unified_scoring`` for the formula.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

from .asset_strategy import AssetStrategy
from .coherence import SelectionCoherence
from .contracts import Candidate, SceneSpec
from .ranking import SemanticTextRanker
from .source_router import AssetType, SourceRoute
from .unified_scoring import (
    PROFILE_FULL,
    PROFILE_PREVIEW as _PROFILE_PREVIEW,
    PROFILE_PREVIEW,
    PROFILE_TEXT_ONLY,
    RerankRow,
    ScoreComponents,
    UnifiedWeights,
    VisualEvidence,
    choose_visual_budget,
    global_rerank,
    score_candidate,
)

REAL_ASSET_TYPES = frozenset({
    AssetType.STOCK_VIDEO.value,
    AssetType.STOCK_IMAGE.value,
    AssetType.ARCHIVAL_IMAGE.value,
    AssetType.ARCHIVAL_VIDEO.value,
    AssetType.SCIENTIFIC_IMAGE.value,
})
GENERATED_ASSET_TYPES = frozenset({
    AssetType.AI_IMAGE.value,
    AssetType.CONCEPTUAL_STOCK.value,
    AssetType.GENERIC_FALLBACK.value,
})

EvidenceEvaluator = Callable[[Candidate, SceneSpec], VisualEvidence | dict | None]
IntegratedResult = tuple[list[RerankRow], list[RerankRow], dict[str, Any]]


def is_real_asset(asset_type: str) -> bool:
    return str(asset_type or "") in REAL_ASSET_TYPES


def is_generated_asset(asset_type: str) -> bool:
    return str(asset_type or "") in GENERATED_ASSET_TYPES


def _normalise_evidence(value: VisualEvidence | dict | None) -> VisualEvidence:
    if value is None:
        return VisualEvidence(status="EVIDENCE_UNAVAILABLE")
    if isinstance(value, VisualEvidence):
        return value
    if isinstance(value, dict):
        known = {
            key: value.get(key)
            for key in VisualEvidence.__dataclass_fields__
            if key in value
        }
        known["detail"] = dict(value.get("detail") or {})
        return VisualEvidence(**known)
    return VisualEvidence(status="EVIDENCE_INVALID")


def routing_priors(route: SourceRoute, asset_type: str) -> tuple[float, float, float]:
    """Return (source_prior, historical_prior, asset_type_prior).

    These are *routing* signals.  The provider prior that P01 already applies
    inside the text score is deliberately not repeated here.
    """
    asset_type = str(asset_type or "")
    preferred = set(route.preferred_asset_types or [])
    archive = asset_type in (
        AssetType.ARCHIVAL_IMAGE.value, AssetType.ARCHIVAL_VIDEO.value,
        AssetType.SCIENTIFIC_IMAGE.value,
    )
    source_prior = 0.03 if archive and route.historical_priority else 0.0
    historical_prior = 0.0
    if route.historical_priority:
        historical_prior = 0.05 if archive else -0.02
    if route.scene_type in ("SPACE", "SCIENCE") and asset_type == AssetType.SCIENTIFIC_IMAGE.value:
        historical_prior = max(historical_prior, 0.04)
    asset_type_prior = 0.02 if asset_type in preferred else 0.0
    return source_prior, historical_prior, asset_type_prior


@dataclass
class IntegratedConfig:
    visual_profile: str = PROFILE_PREVIEW
    visual_top_n: int = 8
    visual_archive_all: bool = True
    max_visual_candidates: int = 14
    real_threshold: float = 0.26
    ai_threshold: float = 0.26
    stage2_threshold: float = 0.18
    ai_validation_required: bool = True
    adaptive_budget: bool = True
    weights: UnifiedWeights = field(default_factory=UnifiedWeights)

    @classmethod
    def from_mapping(cls, data: dict[str, Any] | None) -> "IntegratedConfig":
        data = dict(data or {})
        base = cls()

        def _f(name: str, default: float) -> float:
            try:
                return float(data.get(name, default))
            except (TypeError, ValueError):
                return default

        def _i(name: str, default: int) -> int:
            try:
                return int(data.get(name, default))
            except (TypeError, ValueError):
                return default

        def _b(name: str, default: bool) -> bool:
            raw = data.get(name)
            if raw is None:
                return default
            return str(raw).strip().lower() in ("1", "true", "yes", "on")

        profile = str(data.get("visual_profile") or base.visual_profile).upper()
        if profile not in (PROFILE_TEXT_ONLY, PROFILE_PREVIEW, PROFILE_FULL):
            profile = base.visual_profile
        return cls(
            visual_profile=profile,
            visual_top_n=_i("visual_top_n", base.visual_top_n),
            visual_archive_all=_b("visual_archive_all", base.visual_archive_all),
            max_visual_candidates=_i("max_visual_candidates", base.max_visual_candidates),
            real_threshold=_f("real_threshold", base.real_threshold),
            ai_threshold=_f("ai_threshold", base.ai_threshold),
            stage2_threshold=_f("stage2_threshold", base.stage2_threshold),
            ai_validation_required=_b("ai_validation_required", base.ai_validation_required),
            adaptive_budget=_b("adaptive_budget", base.adaptive_budget),
            weights=UnifiedWeights.from_mapping(data.get("weights") or {}),
        )


class IntegratedSelector:
    """One global reranker over one candidate universe."""

    def __init__(
        self,
        *,
        config: IntegratedConfig | None = None,
        ranker: SemanticTextRanker | None = None,
        visual_evaluator: EvidenceEvaluator | None = None,
        coherence: SelectionCoherence | None = None,
    ) -> None:
        self.config = config or IntegratedConfig()
        self.ranker = ranker or SemanticTextRanker()
        self.visual_evaluator = visual_evaluator
        self.coherence = coherence or SelectionCoherence()
        self._evidence_cache: dict[str, VisualEvidence] = {}

    def reset(self) -> None:
        self.coherence.reset()
        self._evidence_cache.clear()

    # ── evidence ───────────────────────────────────────────────
    def _evidence(self, candidate: Candidate, spec: SceneSpec) -> VisualEvidence:
        key = candidate.canonical_key
        if key in self._evidence_cache:
            return self._evidence_cache[key]
        if self.visual_evaluator is None or self.config.visual_profile == PROFILE_TEXT_ONLY:
            evidence = VisualEvidence(status="VISUAL_DISABLED")
        else:
            try:
                evidence = _normalise_evidence(self.visual_evaluator(candidate, spec))
            except Exception as exc:  # fail-open: no evidence is not bad evidence
                evidence = VisualEvidence(
                    status="EVALUATOR_ERROR", detail={"error": str(exc)[:200]},
                )
        self._evidence_cache[key] = evidence
        return evidence

    # ── ranking ────────────────────────────────────────────────
    def rank_universe(
        self,
        spec: SceneSpec,
        universe: Iterable[tuple[Candidate, dict[str, Any]]],
        route: SourceRoute,
        *,
        profile: str | None = None,
        fallback_type: str = "",
    ) -> IntegratedResult:
        profile = str(profile or self.config.visual_profile).upper()
        items = [(candidate, dict(meta or {})) for candidate, meta in universe]
        if not items:
            return [], [], {"profile": profile, "evidence_calls": 0}
        candidates = [candidate for candidate, _ in items]
        breakdowns = self.ranker.score_many(spec, candidates)
        text_scores = {
            candidate.canonical_key: float(breakdown.final)
            for candidate, breakdown in zip(candidates, breakdowns)
        }
        breakdown_by_key = {
            candidate.canonical_key: breakdown
            for candidate, breakdown in zip(candidates, breakdowns)
        }
        effective_profile = profile
        adaptive_stage = ""
        if self.config.adaptive_budget and len(candidates) > 1 and profile != PROFILE_TEXT_ONLY:
            ordered_scores = sorted(text_scores.values(), reverse=True)
            margin = ordered_scores[0] - ordered_scores[1]
            adaptive_stage = choose_visual_budget(
                text_score=ordered_scores[0], text_margin=margin,
            )
            if adaptive_stage == "text":
                effective_profile = PROFILE_TEXT_ONLY
        ordered_by_text = sorted(
            items,
            key=lambda item: (
                -text_scores.get(item[0].canonical_key, 0.0),
                item[0].canonical_key,
            ),
        )
        # Bounded visual inspection: top text candidates plus archive/scientific
        # (which can be visually strong but metadata-poor).  Never unbounded.
        visual_keys: list[str] = []
        budget = max(1, int(self.config.visual_top_n))
        for candidate, meta in ordered_by_text[:budget]:
            visual_keys.append(candidate.canonical_key)
        if self.config.visual_archive_all:
            for candidate, meta in ordered_by_text:
                asset_type = str(meta.get("asset_type") or candidate.asset_type or "")
                if asset_type in (
                    AssetType.ARCHIVAL_IMAGE.value,
                    AssetType.ARCHIVAL_VIDEO.value,
                    AssetType.SCIENTIFIC_IMAGE.value,
                ) and candidate.canonical_key not in visual_keys:
                    visual_keys.append(candidate.canonical_key)
        visual_keys = visual_keys[: max(1, int(self.config.max_visual_candidates))]
        diagnostic = {
            "profile": profile,
            "effective_profile": effective_profile,
            "adaptive_stage": adaptive_stage,
            "visible_candidates": len(visual_keys),
            "universe": len(items),
            "evidence_calls": 0,
            "fallback_type": fallback_type,
        }

        rows: list[RerankRow] = []
        for candidate, meta in items:
            asset_type = str(meta.get("asset_type") or candidate.asset_type or "")
            media_type = str(
                meta.get("media_type")
                or ("video" if asset_type in (
                    AssetType.STOCK_VIDEO.value, AssetType.ARCHIVAL_VIDEO.value,
                ) else "image")
            )
            if candidate.canonical_key in visual_keys:
                evidence = self._evidence(candidate, spec)
                diagnostic["evidence_calls"] += 1
            else:
                # Not selected for inspection: missing, not negative.
                evidence = VisualEvidence(status="NOT_INSPECTED")
            if effective_profile == PROFILE_TEXT_ONLY:
                evidence = VisualEvidence(
                    status="VISUAL_DISABLED",
                    technical_quality=evidence.technical_quality,
                )
            source_prior, historical_prior, asset_type_prior = routing_priors(
                route, asset_type,
            )
            signals = meta.get("coherence") or {}
            coherence_penalty = float(
                signals.get("provider_penalty")
                or signals.get("total_penalty")
                or 0.0
            )
            style_penalty = float(signals.get("style_penalty") or 0.0)
            shot_penalty = float(signals.get("shot_penalty") or 0.0)
            score = score_candidate(
                text_relevance=text_scores.get(candidate.canonical_key, 0.0),
                evidence=evidence,
                source_prior=source_prior,
                historical_prior=historical_prior,
                asset_type_prior=asset_type_prior,
                coherence_penalty=coherence_penalty,
                style_penalty=style_penalty,
                shot_penalty=shot_penalty,
                fallback_type=fallback_type,
                weights=self.config.weights,
                profile=effective_profile,
            )
            row_meta = dict(meta)
            text_breakdown = breakdown_by_key.get(candidate.canonical_key)
            row_meta["text_components"] = (
                text_breakdown.to_dict() if text_breakdown is not None else {}
            )
            row_meta["evidence"] = evidence.to_dict()
            rows.append(
                RerankRow(
                    candidate=candidate,
                    score=score,
                    asset_type=asset_type,
                    media_type=media_type,
                    provider=str(candidate.provider or ""),
                    meta=row_meta,
                )
            )

        ranked = global_rerank(rows, weights=self.config.weights, apply_coherence=True)
        threshold = (
            self.config.stage2_threshold if fallback_type
            else self.config.real_threshold
        )
        approved = [
            row for row in ranked
            if not row.score.hard_reject_reason and row.final >= threshold
        ]
        diagnostic["best_score"] = round(ranked[0].final, 6) if ranked else 0.0
        diagnostic["approved"] = len(approved)
        diagnostic["threshold"] = threshold
        return ranked, approved, diagnostic

    # ── AI validation ──────────────────────────────────────────
    def validate_generated(
        self,
        spec: SceneSpec,
        candidate: Candidate,
        *,
        threshold: float | None = None,
    ) -> tuple[bool, VisualEvidence, str]:
        """An AI image must pass the same visual evidence gate as real assets."""
        if self.visual_profile_is_off():
            return False, VisualEvidence(status="VISUAL_DISABLED"), "ai_visual_validation_unavailable"
        evidence = self._evidence(candidate, spec)
        if not evidence.has_visual:
            return False, evidence, "ai_visual_evidence_unavailable"
        if evidence.hard_reject_reason:
            return False, evidence, evidence.hard_reject_reason
        # Reuse the text score (AI prompt intent) as one component, but the
        # generated pixels must independently clear a visual floor.
        visual_values = [
            value for value in (
                evidence.preview_relevance,
                evidence.frame_relevance,
                evidence.vlm_relevance,
            )
            if value is not None
        ]
        if not visual_values:
            return False, evidence, "ai_visual_evidence_unavailable"
        visual_best = max(float(value) for value in visual_values)
        floor = (
            self.config.weights.text_threshold
            if threshold is None else float(threshold)
        )
        if visual_best < floor:
            return False, evidence, "AI_BAD_OUTPUT"
        return True, evidence, ""

    def visual_profile_is_off(self) -> bool:
        return self.config.visual_profile == PROFILE_TEXT_ONLY or self.visual_evaluator is None


def stage1_universe(
    universe: Iterable[tuple[Candidate, dict[str, Any]]],
) -> list[tuple[Candidate, dict[str, Any]]]:
    return [
        (candidate, meta) for candidate, meta in universe
        if is_real_asset(str(meta.get("asset_type") or candidate.asset_type or ""))
    ]


def stage2_universe(
    universe: Iterable[tuple[Candidate, dict[str, Any]]],
) -> list[tuple[Candidate, dict[str, Any]]]:
    return [
        (candidate, meta) for candidate, meta in universe
        if is_generated_asset(str(meta.get("asset_type") or candidate.asset_type or ""))
    ]


def describe_universe(universe: Iterable[tuple[Candidate, dict[str, Any]]]) -> dict[str, Any]:
    """Audit helper: prove there is one universe, not one per provider."""
    by_type: dict[str, int] = {}
    by_provider: dict[str, int] = {}
    for candidate, meta in universe:
        asset_type = str(meta.get("asset_type") or candidate.asset_type or "UNKNOWN")
        by_type[asset_type] = by_type.get(asset_type, 0) + 1
        by_provider[candidate.provider] = by_provider.get(candidate.provider, 0) + 1
    return {
        "total": sum(by_type.values()),
        "by_asset_type": by_type,
        "by_provider": by_provider,
        "real": sum(count for name, count in by_type.items() if is_real_asset(name)),
        "generated": sum(count for name, count in by_type.items() if is_generated_asset(name)),
    }
