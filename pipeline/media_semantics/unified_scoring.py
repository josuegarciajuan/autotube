"""P05-D/E/F/L — one comparable score for every candidate source.

P01 ranks text, P02 measures pixels, P04 contributes routing/source priors and
coherence.  Before P05 those signals lived in separate decisions (and the P04
text-only ranker could discard a visually good, metadata-poor still — the
5/5 → 3/5 control regression).  This module makes every signal an explicit
component of a single score.

Design rules
------------
* ``missing evidence != negative evidence``.  A stage that did not run passes
  ``None`` and is excluded from the denominator (it is not scored as zero).
  An observed mismatch passes an explicit penalty (and possibly a hard reject).
* Priors help exploration but cannot overrule relevance: the sum of routing
  priors is capped (``routing_prior_cap``) and coherence penalties are only
  applied inside a relevance band, also capped (``coherence_cap``).
* No double counting: the P01 text score already contains its small provider
  prior; that prior is *not* added again.  Source/asset/historical priors here
  are the P04 routing signals only.
* One score, one ranking.  There is no per-provider winner before the global
  rerank.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

# Profiles decide which *optional* evidence is collected, not the formula.
PROFILE_TEXT_ONLY = "TEXT_ONLY"
PROFILE_PREVIEW = "PREVIEW_VISUAL"
PROFILE_FULL = "FULL_VISUAL"
VALID_PROFILES = (PROFILE_TEXT_ONLY, PROFILE_PREVIEW, PROFILE_FULL)

HTTP_SAFE = "HTTP_SAFE"


@dataclass
class VisualEvidence:
    """Observed visual evidence for one candidate.

    ``None`` means *not observed* (stage did not run/failed to produce data);
    ``0.0`` means observed and genuinely irrelevant.  This distinction is the
    core of the P03 denominator bug fix and is tested explicitly.
    """

    preview_relevance: float | None = None
    frame_relevance: float | None = None
    vlm_relevance: float | None = None
    technical_quality: float | None = None
    negative_penalty: float = 0.0
    era_penalty: float = 0.0
    must_include_penalty: float = 0.0
    hard_reject_reason: str = ""
    status: str = ""
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def has_visual(self) -> bool:
        return any(
            value is not None
            for value in (self.preview_relevance, self.frame_relevance, self.vlm_relevance)
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "preview_relevance": _round_opt(self.preview_relevance),
            "frame_relevance": _round_opt(self.frame_relevance),
            "vlm_relevance": _round_opt(self.vlm_relevance),
            "technical_quality": _round_opt(self.technical_quality),
            "negative_penalty": round(float(self.negative_penalty), 6),
            "era_penalty": round(float(self.era_penalty), 6),
            "must_include_penalty": round(float(self.must_include_penalty), 6),
            "hard_reject_reason": self.hard_reject_reason,
            "status": self.status,
            "detail": dict(self.detail),
        }


def _round_opt(value: float | None) -> float | None:
    return None if value is None else round(float(value), 6)


def _clamp01(value: float | None, default: float = 0.0) -> float:
    if value is None:
        return default
    return max(0.0, min(1.0, float(value)))


@dataclass
class UnifiedWeights:
    """Positive component weights.  Optional components renormalise away."""

    # P05 empirical calibration (see evidence/p05-20261025):
    # preview weight raised because P03 showed visual relevance is the
    # discriminating signal; text remains the largest single component.
    text: float = 0.40
    preview: float = 0.35
    frame: float = 0.20
    vlm: float = 0.15
    technical_quality: float = 0.05
    # P04 routing priors: small and capped so they cannot mask relevance.
    source_prior: float = 1.0
    historical_prior: float = 1.0
    asset_type_prior: float = 1.0
    routing_prior_cap: float = 0.04
    # Coherence/diversity is applied only near the relevance frontier.
    coherence_cap: float = 0.05
    coherence_band: float = 0.08
    # Actual constraint penalties (not availability).
    negative_penalty: float = 0.60
    era_penalty: float = 0.30
    must_include_penalty: float = 0.25
    # Generated/conceptual fallbacks are explicitly discounted.
    ai_fallback_penalty: float = 0.06
    conceptual_fallback_penalty: float = 0.10
    generic_fallback_penalty: float = 0.14
    # Score scale for eligibility in each profile.
    text_threshold: float = 0.18

    def component_weights(self) -> dict[str, float]:
        return {
            "text": self.text,
            "preview": self.preview,
            "frame": self.frame,
            "vlm": self.vlm,
            "technical_quality": self.technical_quality,
        }

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any] | None) -> "UnifiedWeights":
        data = dict(data or {})
        base = cls()

        def _f(key: str) -> float:
            try:
                return float(data.get(key, getattr(base, key)))
            except (TypeError, ValueError):
                return float(getattr(base, key))

        return cls(**{name: _f(name) for name in cls.__dataclass_fields__})


@dataclass
class ScoreComponents:
    """Serialisable breakdown; every input is explicit to avoid double counting."""

    text_relevance: float = 0.0
    preview_visual_relevance: float | None = None
    frame_relevance: float | None = None
    vlm_relevance: float | None = None
    technical_quality: float | None = None
    source_prior: float = 0.0
    historical_prior: float = 0.0
    asset_type_prior: float = 0.0
    routing_prior_bounded: float = 0.0
    coherence_penalty: float = 0.0
    duplicate_penalty: float = 0.0
    style_penalty: float = 0.0
    shot_diversity: float = 0.0
    negative_penalty: float = 0.0
    era_penalty: float = 0.0
    must_include_penalty: float = 0.0
    fallback_penalty: float = 0.0
    base_score: float = 0.0
    final_score: float = 0.0
    available_components: list[str] = field(default_factory=list)
    denominator: float = 0.0
    hard_reject_reason: str = ""
    profile: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "text_relevance": round(self.text_relevance, 6),
            "preview_visual_relevance": _round_opt(self.preview_visual_relevance),
            "frame_relevance": _round_opt(self.frame_relevance),
            "vlm_relevance": _round_opt(self.vlm_relevance),
            "technical_quality": _round_opt(self.technical_quality),
            "source_prior": round(self.source_prior, 6),
            "historical_prior": round(self.historical_prior, 6),
            "asset_type_prior": round(self.asset_type_prior, 6),
            "routing_prior_bounded": round(self.routing_prior_bounded, 6),
            "coherence_penalty": round(self.coherence_penalty, 6),
            "duplicate_penalty": round(self.duplicate_penalty, 6),
            "style_penalty": round(self.style_penalty, 6),
            "shot_diversity": round(self.shot_diversity, 6),
            "negative_penalty": round(self.negative_penalty, 6),
            "era_penalty": round(self.era_penalty, 6),
            "must_include_penalty": round(self.must_include_penalty, 6),
            "fallback_penalty": round(self.fallback_penalty, 6),
            "base_score": round(self.base_score, 6),
            "final_score": round(self.final_score, 6),
            "available_components": list(self.available_components),
            "denominator": round(self.denominator, 6),
            "hard_reject_reason": self.hard_reject_reason,
            "profile": self.profile,
        }


def score_candidate(
    *,
    text_relevance: float,
    evidence: VisualEvidence | None = None,
    source_prior: float = 0.0,
    historical_prior: float = 0.0,
    asset_type_prior: float = 0.0,
    coherence_penalty: float = 0.0,
    style_penalty: float = 0.0,
    shot_penalty: float = 0.0,
    duplicate_penalty: float = 0.0,
    fallback_type: str = "",
    weights: UnifiedWeights | None = None,
    profile: str = PROFILE_FULL,
) -> ScoreComponents:
    """Compute the single P05 score.

    ``evidence=None`` is treated as *no visual stage ran*; it never lowers the
    score relative to an equally-good observed stage.  Actual mismatches must
    be supplied as explicit penalties.
    """
    w = weights or UnifiedWeights()
    ev = evidence or VisualEvidence()
    text = _clamp01(text_relevance)

    components: dict[str, float | None] = {
        "text": text,
        "preview": None if ev.preview_relevance is None else _clamp01(ev.preview_relevance),
        "frame": None if ev.frame_relevance is None else _clamp01(ev.frame_relevance),
        "vlm": None if ev.vlm_relevance is None else _clamp01(ev.vlm_relevance),
        "technical_quality": (
            None if ev.technical_quality is None else _clamp01(ev.technical_quality)
        ),
    }
    allowed = _profile_components(profile)
    weights_by_name = w.component_weights()
    available = [
        name for name, value in components.items()
        if value is not None and name in allowed
    ]
    # Text is the one component that must always exist (P01 metadata rank).
    if "text" not in available:
        available.insert(0, "text")
    denominator = sum(weights_by_name[name] for name in available) or 1.0
    base = sum(
        weights_by_name[name] * float(components[name] or 0.0)
        for name in available
    ) / denominator

    priors = (
        float(source_prior) * w.source_prior
        + float(historical_prior) * w.historical_prior
        + float(asset_type_prior) * w.asset_type_prior
    )
    routing_bounded = max(-w.routing_prior_cap, min(w.routing_prior_cap, priors))

    # Coherence is bounded and only meaningful near the frontier.  The caller
    # decides which rows are inside the band; this function just caps the sum.
    coherence_total = max(0.0, float(coherence_penalty))
    style_total = max(0.0, float(style_penalty))
    shot_total = max(0.0, float(shot_penalty))
    diversity_total = min(w.coherence_cap, coherence_total + style_total + shot_total)

    constraint_penalty = (
        _clamp01(ev.negative_penalty) * w.negative_penalty
        + _clamp01(ev.era_penalty) * w.era_penalty
        + _clamp01(ev.must_include_penalty) * w.must_include_penalty
    )
    duplicate_total = _clamp01(duplicate_penalty)
    fallback_penalty = 0.0
    fallback_key = str(fallback_type or "").upper()
    if fallback_key == "AI_IMAGE":
        fallback_penalty = w.ai_fallback_penalty
    elif fallback_key == "CONCEPTUAL":
        fallback_penalty = w.conceptual_fallback_penalty
    elif fallback_key == "GENERIC":
        fallback_penalty = w.generic_fallback_penalty

    # Coherence/diversity is capped; constraint evidence is not.
    coherence_applied = min(
        w.coherence_cap, coherence_total + style_total + shot_total,
    )
    final = (
        base
        + routing_bounded
        - coherence_applied
        - constraint_penalty
        - duplicate_total
        - fallback_penalty
    )
    final = max(0.0, min(1.0, final))

    return ScoreComponents(
        text_relevance=text,
        preview_visual_relevance=components["preview"],
        frame_relevance=components["frame"],
        vlm_relevance=components["vlm"],
        technical_quality=components["technical_quality"],
        source_prior=float(source_prior),
        historical_prior=float(historical_prior),
        asset_type_prior=float(asset_type_prior),
        routing_prior_bounded=routing_bounded,
        coherence_penalty=coherence_total,
        duplicate_penalty=duplicate_total,
        style_penalty=style_total,
        shot_diversity=shot_total,
        negative_penalty=_clamp01(ev.negative_penalty),
        era_penalty=_clamp01(ev.era_penalty),
        must_include_penalty=_clamp01(ev.must_include_penalty),
        fallback_penalty=fallback_penalty,
        base_score=base,
        final_score=final,
        available_components=available,
        denominator=denominator,
        hard_reject_reason=ev.hard_reject_reason,
        profile=profile,
    )


def _profile_components(profile: str) -> set[str]:
    profile = str(profile or PROFILE_FULL).upper()
    if profile == PROFILE_TEXT_ONLY:
        return {"text"}
    if profile == PROFILE_PREVIEW:
        return {"text", "preview", "technical_quality"}
    return {"text", "preview", "frame", "vlm", "technical_quality"}


# ── relevance-bounded coherence ─────────────────────────────────────────────


@dataclass
class RerankRow:
    candidate: Any
    score: ScoreComponents
    asset_type: str = ""
    media_type: str = ""
    provider: str = ""
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def final(self) -> float:
        return float(self.score.final_score)

    @property
    def relevance(self) -> float:
        return float(self.score.base_score)

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "provider_asset_id": getattr(self.candidate, "provider_asset_id", ""),
            "asset_type": self.asset_type,
            "media_type": self.media_type,
            "final": round(self.final, 6),
            "relevance": round(self.relevance, 6),
            "score": self.score.to_dict(),
            **dict(self.meta),
        }


def bounded_coherence(
    rows: Iterable[RerankRow],
    *,
    weights: UnifiedWeights | None = None,
    provider_penalty: float = 0.03,
    still_penalty: float = 0.02,
    shot_penalty: float = 0.02,
    provider_share_threshold: float = 0.60,
) -> list[RerankRow]:
    """Apply diversity only where it cannot mask a clearly better asset.

    Rows outside ``best_relevance - band`` keep their score untouched: a
    provider-repeat penalty must never promote a clearly worse asset over a
    clearly better one.
    """
    w = weights or UnifiedWeights()
    ordered = sorted(rows, key=lambda row: (-row.relevance, -row.final, str(getattr(row.candidate, "canonical_key", ""))))
    if not ordered:
        return ordered
    best = ordered[0].relevance
    band = max(0.0, float(w.coherence_band))
    counts: dict[str, int] = {}
    still_run = 0
    for row in ordered:
        if row.relevance < best - band:
            continue
        key = str(row.provider or "")
        counts[key] = counts.get(key, 0) + 1
        penalty = 0.0
        # Rows that already carry a P04 context penalty (provider/still/shot/
        # style/era) must not be penalised twice for the same repetition.
        has_context_penalty = float(
            row.meta.get("coherence_applied_penalty") or 0.0
        ) > 0.0
        if not has_context_penalty and counts[key] > 1:
            penalty += min(provider_penalty, 0.5 * provider_penalty * counts[key])
        if str(row.media_type) == "image":
            still_run += 1
            if not has_context_penalty and still_run > 1:
                penalty += still_penalty
        if penalty > 0:
            row.score.shot_diversity = round(row.score.shot_diversity + penalty, 6)
            row.score.final_score = max(0.0, row.score.final_score - penalty)
    return sorted(
        ordered,
        key=lambda row: (
            -row.final,
            -row.relevance,
            str(getattr(row.candidate, "canonical_key", "")),
        ),
    )


def global_rerank(
    rows: Iterable[RerankRow],
    *,
    weights: UnifiedWeights | None = None,
    apply_coherence: bool = True,
) -> list[RerankRow]:
    """One global ordering for every source/provider/asset type."""
    rows = [row for row in rows if not row.score.hard_reject_reason]
    if apply_coherence:
        return bounded_coherence(rows, weights=weights)
    return sorted(
        rows,
        key=lambda row: (
            -row.final,
            -row.relevance,
            str(getattr(row.candidate, "canonical_key", "")),
        ),
    )


# ── adaptive visual budget (P05-T) ──────────────────────────────────────────


@dataclass
class AdaptiveBudgetConfig:
    # Calibrated on the frozen P03 corpus (text scores max ~0.34).
    strong_text_score: float = 0.28
    strong_margin: float = 0.08
    ambiguous_margin: float = 0.05
    close_preview_margin: float = 0.03
    high_risk_terms: int = 1
    preview_top_n: int = 6
    vlm_top_n: int = 2

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any] | None) -> "AdaptiveBudgetConfig":
        data = dict(data or {})
        base = cls()
        out = {}
        for name in cls.__dataclass_fields__:
            try:
                out[name] = float(data.get(name, getattr(base, name)))
            except (TypeError, ValueError):
                out[name] = getattr(base, name)
        return cls(**out)


def choose_visual_budget(
    *,
    text_score: float,
    text_margin: float,
    negative_hits: int = 0,
    preview_margin: float | None = None,
    config: AdaptiveBudgetConfig | None = None,
) -> str:
    """Return the cheapest evidence stage that is still informative.

    * very strong text + clear margin → no visual inspection;
    * ambiguous text → preview;
    * high risk (negative/era constraints) → frames;
    * near-tie after preview → VLM.
    """
    cfg = config or AdaptiveBudgetConfig()
    if negative_hits >= cfg.high_risk_terms:
        return "frames"
    if float(text_score) >= cfg.strong_text_score and float(text_margin) >= cfg.strong_margin:
        return "text"
    if preview_margin is not None and float(preview_margin) <= cfg.close_preview_margin:
        return "vlm"
    if float(text_margin) <= cfg.ambiguous_margin:
        return "preview"
    return "preview"


def profile_enabled_stages(profile: str) -> dict[str, bool]:
    profile = str(profile or PROFILE_FULL).upper()
    return {
        "text": True,
        "preview": profile in (PROFILE_PREVIEW, PROFILE_FULL),
        "frames": profile == PROFILE_FULL,
        "vlm": profile == PROFILE_FULL,
    }
