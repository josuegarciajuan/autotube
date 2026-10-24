"""Configuration for the P01 semantic selector.

All values are optional.  Defaults are deliberately conservative: the default
selector remains ``legacy`` and the semantic threshold is low enough to avoid
turning ordinary scenes into placeholders before a benchmark calibrates it.

Resolution order (highest wins):
  1. explicit environment variables (``MEDIA_SELECTOR``,
     ``MEDIA_SELECTOR_SHADOW`` and the ``P01_*`` tuning overrides),
  2. the channel ``MEDIA_STRATEGY`` dict,
  3. defaults in this module.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

VALID_MODES = ("legacy", "semantic", "shadow")


def _cget(obj: Any, key: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _as_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _as_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


@dataclass
class RankingWeights:
    """Weights for the textual/semantic score. Positive weights are normalised."""

    semantic: float = 0.35
    lexical: float = 0.15
    action: float = 0.18
    subject: float = 0.12
    environment: float = 0.08
    era: float = 0.05
    metadata_quality: float = 0.04
    provider_prior: float = 0.03
    negative_constraint: float = 0.50
    no_metadata_ceiling: float = 0.12

    def positive_total(self) -> float:
        total = (
            self.semantic + self.lexical + self.action + self.subject
            + self.environment + self.era + self.metadata_quality
            + self.provider_prior
        )
        return total if total > 0 else 1.0

    @classmethod
    def from_mapping(cls, data: dict[str, Any] | None) -> "RankingWeights":
        data = dict(data or {})
        defaults = cls()
        return cls(
            semantic=_as_float(
                data.get("semantic", data.get("semantic_similarity")),
                defaults.semantic,
            ),
            lexical=_as_float(data.get("lexical"), defaults.lexical),
            action=_as_float(data.get("action", data.get("action_match")), defaults.action),
            subject=_as_float(data.get("subject", data.get("subject_match")), defaults.subject),
            environment=_as_float(
                data.get("environment", data.get("environment_match")),
                defaults.environment,
            ),
            era=_as_float(data.get("era"), defaults.era),
            metadata_quality=_as_float(
                data.get("metadata_quality"), defaults.metadata_quality
            ),
            provider_prior=_as_float(
                data.get("provider_prior", data.get("provider_quality")),
                defaults.provider_prior,
            ),
            negative_constraint=_as_float(
                data.get("negative_constraint", data.get("negative_constraint_penalty")),
                defaults.negative_constraint,
            ),
            no_metadata_ceiling=_as_float(
                data.get("no_metadata_ceiling"), defaults.no_metadata_ceiling
            ),
        )


@dataclass
class RetrievalBudget:
    """Hard limits for the metadata retrieval phase (downloads happen later)."""

    max_total: int = 80
    max_per_provider: int = 30
    max_per_query: int = 24
    max_pages_per_provider: int = 2
    max_queries: int = 10
    top_k: int = 10
    top_n_download: int = 3
    provenance_candidate_limit: int = 12
    provenance_rejected_limit: int = 12

    @classmethod
    def from_mapping(cls, data: dict[str, Any] | None) -> "RetrievalBudget":
        data = dict(data or {})
        defaults = cls()
        return cls(
            max_total=max(
                1,
                _as_int(
                    data.get("max_total", data.get("max_candidates_total")),
                    defaults.max_total,
                ),
            ),
            max_per_provider=max(
                1,
                _as_int(
                    data.get(
                        "max_per_provider", data.get("max_candidates_per_provider")
                    ),
                    defaults.max_per_provider,
                ),
            ),
            max_per_query=max(
                1,
                _as_int(
                    data.get("max_per_query", data.get("max_candidates_per_query")),
                    defaults.max_per_query,
                ),
            ),
            max_pages_per_provider=max(
                1,
                _as_int(
                    data.get(
                        "max_pages_per_provider", data.get("max_pages_per_query")
                    ),
                    defaults.max_pages_per_provider,
                ),
            ),
            max_queries=max(1, _as_int(data.get("max_queries"), defaults.max_queries)),
            top_k=max(1, _as_int(data.get("top_k"), defaults.top_k)),
            top_n_download=max(
                1,
                _as_int(
                    data.get("top_n_download", data.get("max_download_candidates")),
                    defaults.top_n_download,
                ),
            ),
            provenance_candidate_limit=max(
                1,
                _as_int(
                    data.get("provenance_candidate_limit"),
                    defaults.provenance_candidate_limit,
                ),
            ),
            provenance_rejected_limit=max(
                1,
                _as_int(
                    data.get("provenance_rejected_limit"),
                    defaults.provenance_rejected_limit,
                ),
            ),
        )




VALID_VISUAL_MODES = (
    "off", "text_only", "embedding_only", "frame_embedding_only", "full_visual",
)
VALID_VLM_MODES = ("none", "openai_compatible", "local")


@dataclass
class VisualBudget:
    """Hard funnel budgets: previews < downloads < frames < VLM calls."""

    preview_candidates: int = 12
    shortlist: int = 6
    download_candidates: int = 2
    preview_bytes: int = 512 * 1024
    preview_timeout: float = 8.0
    preview_total_bytes: int = 6 * 1024 * 1024
    embedding_batch_size: int = 8
    frame_count: int = 6
    frame_max_count: int = 8
    frame_concurrency: int = 1
    vlm_candidates: int = 2
    vlm_max_frames: int = 4
    vlm_timeout: float = 45.0
    vlm_max_image_bytes: int = 3 * 1024 * 1024

    @classmethod
    def from_mapping(cls, data: dict[str, Any] | None) -> "VisualBudget":
        data = dict(data or {})
        d = cls()
        return cls(
            preview_candidates=max(1, _as_int(data.get("visual_preview_candidates", data.get("preview_candidates")), d.preview_candidates)),
            shortlist=max(1, _as_int(data.get("visual_shortlist", data.get("shortlist")), d.shortlist)),
            download_candidates=max(1, _as_int(data.get("visual_download_candidates", data.get("download_candidates")), d.download_candidates)),
            preview_bytes=max(1024, _as_int(data.get("visual_preview_bytes", data.get("preview_bytes")), d.preview_bytes)),
            preview_timeout=max(0.5, _as_float(data.get("visual_preview_timeout", data.get("preview_timeout")), d.preview_timeout)),
            preview_total_bytes=max(1024, _as_int(data.get("visual_preview_total_bytes", data.get("preview_total_bytes")), d.preview_total_bytes)),
            embedding_batch_size=max(1, _as_int(data.get("visual_embedding_batch_size", data.get("embedding_batch_size")), d.embedding_batch_size)),
            frame_count=max(1, min(24, _as_int(data.get("visual_frame_count", data.get("frame_count")), d.frame_count))),
            frame_max_count=max(1, min(24, _as_int(data.get("visual_frame_max_count", data.get("frame_max_count")), d.frame_max_count))),
            frame_concurrency=max(1, _as_int(data.get("visual_frame_concurrency", data.get("frame_concurrency")), d.frame_concurrency)),
            vlm_candidates=max(1, _as_int(data.get("visual_vlm_candidates", data.get("vlm_candidates")), d.vlm_candidates)),
            vlm_max_frames=max(1, min(12, _as_int(data.get("visual_vlm_max_frames", data.get("vlm_max_frames")), d.vlm_max_frames))),
            vlm_timeout=max(1.0, _as_float(data.get("visual_vlm_timeout", data.get("vlm_timeout")), d.vlm_timeout)),
            vlm_max_image_bytes=max(1024, _as_int(data.get("visual_vlm_max_image_bytes", data.get("vlm_max_image_bytes")), d.vlm_max_image_bytes)),
        )


@dataclass
class VisualWeights:
    """Configurable components; defaults are a starting point, not science."""

    text: float = 0.50
    preview: float = 0.15
    frame: float = 0.20
    vlm: float = 0.15
    technical_quality: float = 0.05
    negative_penalty: float = 0.60
    era_penalty: float = 0.30
    must_include_penalty: float = 0.25
    frame_max: float = 0.25
    frame_mean: float = 0.20
    frame_median: float = 0.25
    frame_coverage: float = 0.20
    frame_consistency: float = 0.10

    def positive_total(self) -> float:
        total = self.text + self.preview + self.frame + self.vlm + self.technical_quality
        return total if total > 0 else 1.0

    def frame_weights_total(self) -> float:
        total = self.frame_max + self.frame_mean + self.frame_median + self.frame_coverage + self.frame_consistency
        return total if total > 0 else 1.0

    @classmethod
    def from_mapping(cls, data: dict[str, Any] | None) -> "VisualWeights":
        data = dict(data or {})
        d = cls()
        return cls(
            text=_as_float(data.get("text", data.get("text_relevance")), d.text),
            preview=_as_float(data.get("preview", data.get("visual_preview_relevance")), d.preview),
            frame=_as_float(data.get("frame", data.get("frame_relevance")), d.frame),
            vlm=_as_float(data.get("vlm", data.get("vlm_relevance")), d.vlm),
            technical_quality=_as_float(data.get("technical_quality"), d.technical_quality),
            negative_penalty=_as_float(data.get("negative_penalty"), d.negative_penalty),
            era_penalty=_as_float(data.get("era_penalty"), d.era_penalty),
            must_include_penalty=_as_float(data.get("must_include_penalty"), d.must_include_penalty),
            frame_max=_as_float(data.get("frame_max"), d.frame_max),
            frame_mean=_as_float(data.get("frame_mean"), d.frame_mean),
            frame_median=_as_float(data.get("frame_median"), d.frame_median),
            frame_coverage=_as_float(data.get("frame_coverage"), d.frame_coverage),
            frame_consistency=_as_float(data.get("frame_consistency"), d.frame_consistency),
        )


@dataclass
class VisualConfig:
    """P02 visual funnel configuration.  Disabled by default."""

    mode: str = "off"
    backend: str = "none"  # none | clip | siglip | auto
    model: str = "openai/clip-vit-base-patch32"
    model_version: str = ""
    device: str = "cpu"
    allow_model_download: bool = False
    cache_enabled: bool = True
    cache_path: str = ""
    preview_fetch_enabled: bool = True
    frames_enabled: bool = True
    vlm_mode: str = "none"  # none | openai_compatible | local
    vlm_model: str = ""
    vlm_base_url: str = ""
    vlm_api_key: str = ""
    min_similarity: float = 0.26
    preview_min_similarity: float = 0.18
    frame_min_similarity: float = 0.20
    combined_threshold: float = 0.26
    min_vlm_confidence: float = 0.45
    ambiguous_margin: float = 0.06
    must_avoid_similarity_threshold: float = 0.28
    budget: VisualBudget = field(default_factory=VisualBudget)
    weights: VisualWeights = field(default_factory=VisualWeights)
    constraints_enabled: bool = True
    era_check_enabled: bool = True
    vlm_enabled: bool = True
    visual_reject_on_negative: bool = True
    visual_reject_on_era: bool = True

    @property
    def enabled(self) -> bool:
        return self.mode not in ("", "off", "text_only")

    @property
    def uses_previews(self) -> bool:
        return self.mode in ("embedding_only", "full_visual")

    @property
    def uses_frames(self) -> bool:
        return self.mode in ("frame_embedding_only", "full_visual")

    @property
    def uses_vlm(self) -> bool:
        return self.mode == "full_visual" and self.vlm_enabled and self.vlm_mode != "none"

    @classmethod
    def from_mapping(
        cls,
        strategy: dict[str, Any] | None,
        env: dict[str, str] | None = None,
    ) -> "VisualConfig":
        strategy = dict(strategy or {})
        env = os.environ if env is None else env
        raw_mode = env.get(
            "P02_VISUAL_MODE",
            strategy.get("visual_mode", strategy.get("media_visual_mode", "off")),
        )
        mode = str(raw_mode or "off").strip().lower()
        if mode in ("legacy", "disabled", "false", "0"):
            mode = "off"
        if mode not in VALID_VISUAL_MODES:
            mode = "off"
        raw_backend = env.get(
            "P02_VISUAL_BACKEND", strategy.get("visual_embedding_backend", "none")
        )
        backend = str(raw_backend or "none").strip().lower()
        if backend not in ("none", "clip", "siglip", "auto"):
            backend = "none"
        raw_vlm = env.get("P02_VLM_MODE", strategy.get("visual_vlm_mode", "none"))
        vlm_mode = str(raw_vlm or "none").strip().lower()
        if vlm_mode == "openai":
            vlm_mode = "openai_compatible"
        if vlm_mode not in VALID_VLM_MODES:
            vlm_mode = "none"
        budget = VisualBudget.from_mapping(strategy.get("visual_budget") or {})
        weights = VisualWeights.from_mapping(strategy.get("visual_weights") or {})
        return cls(
            mode=mode,
            backend=backend,
            model=str(env.get("P02_VISUAL_MODEL", strategy.get("visual_embedding_model", cls.model)) or ""),
            model_version=str(env.get("P02_VISUAL_MODEL_VERSION", strategy.get("visual_model_version", "")) or ""),
            device=str(env.get("P02_VISUAL_DEVICE", strategy.get("visual_device", "cpu")) or "cpu"),
            allow_model_download=_as_bool(env.get("P02_ALLOW_MODEL_DOWNLOAD", strategy.get("visual_allow_model_download")), False),
            cache_enabled=_as_bool(env.get("P02_VISUAL_CACHE_ENABLED", strategy.get("visual_cache_enabled")), True),
            cache_path=str(env.get("P02_VISUAL_CACHE", strategy.get("visual_cache_path", "")) or ""),
            preview_fetch_enabled=_as_bool(env.get("P02_PREVIEW_FETCH", strategy.get("visual_preview_fetch", True)), True),
            frames_enabled=_as_bool(env.get("P02_FRAMES", strategy.get("visual_frames_enabled", True)), True),
            vlm_mode=vlm_mode,
            vlm_model=str(env.get("P02_VLM_MODEL", strategy.get("visual_vlm_model", "")) or ""),
            vlm_base_url=str(env.get("P02_VLM_BASE_URL", strategy.get("visual_vlm_base_url", "")) or ""),
            vlm_api_key=str(env.get("P02_VLM_API_KEY", strategy.get("visual_vlm_api_key", "")) or ""),
            min_similarity=max(0.0, min(1.0, _as_float(env.get("P02_MIN_SIMILARITY", strategy.get("visual_min_similarity")), 0.26))),
            preview_min_similarity=max(0.0, min(1.0, _as_float(env.get("P02_PREVIEW_MIN_SIMILARITY", strategy.get("visual_preview_min_similarity")), 0.18))),
            frame_min_similarity=max(0.0, min(1.0, _as_float(env.get("P02_FRAME_MIN_SIMILARITY", strategy.get("visual_frame_min_similarity")), 0.20))),
            combined_threshold=max(0.0, min(1.0, _as_float(env.get("P02_COMBINED_THRESHOLD", strategy.get("visual_combined_threshold")), 0.26))),
            min_vlm_confidence=max(0.0, min(1.0, _as_float(env.get("P02_VLM_MIN_CONFIDENCE", strategy.get("visual_vlm_min_confidence")), 0.45))),
            ambiguous_margin=max(0.0, _as_float(strategy.get("visual_ambiguous_margin"), 0.06)),
            must_avoid_similarity_threshold=max(0.0, min(1.0, _as_float(strategy.get("visual_must_avoid_similarity_threshold"), 0.28))),
            budget=budget,
            weights=weights,
            constraints_enabled=_as_bool(strategy.get("visual_constraints_enabled"), True),
            era_check_enabled=_as_bool(strategy.get("visual_era_check_enabled"), True),
            vlm_enabled=_as_bool(strategy.get("visual_vlm_enabled"), True),
            visual_reject_on_negative=_as_bool(strategy.get("visual_reject_on_negative"), True),
            visual_reject_on_era=_as_bool(strategy.get("visual_reject_on_era"), True),
        )

    def describe(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "backend": self.backend,
            "model": self.model,
            "model_version": self.model_version,
            "vlm_mode": self.vlm_mode,
            "enabled": self.enabled,
            "budget": {
                "preview_candidates": self.budget.preview_candidates,
                "shortlist": self.budget.shortlist,
                "download_candidates": self.budget.download_candidates,
                "preview_bytes": self.budget.preview_bytes,
                "frame_count": self.budget.frame_count,
                "vlm_candidates": self.budget.vlm_candidates,
            },
        }


@dataclass
class SemanticConfig:
    """Everything the semantic selector needs, resolved once per fetcher."""

    mode: str = "legacy"
    shadow: bool = False
    min_relevance_score: float = 0.26
    embedding_backend: str = "auto"  # auto | hashing | sentence_transformers | none
    embedding_model: str = "sentence-transformers/all-MiniLM-L6-v2"
    embedding_cache_path: str = ""
    weights: RankingWeights = field(default_factory=RankingWeights)
    budget: RetrievalBudget = field(default_factory=RetrievalBudget)
    visual: VisualConfig = field(default_factory=VisualConfig)

    @classmethod
    def from_media_strategy(
        cls,
        strategy: dict[str, Any] | None,
        env: dict[str, str] | None = None,
    ) -> "SemanticConfig":
        strategy = dict(strategy or {})
        env = os.environ if env is None else env

        raw_mode = env.get("MEDIA_SELECTOR", strategy.get("media_selector", "legacy"))
        mode = str(raw_mode or "legacy").strip().lower()
        shadow_alias = mode == "shadow"
        if mode not in VALID_MODES:
            mode = "legacy"
        if shadow_alias:
            # ``MEDIA_SELECTOR=shadow`` is a compatibility alias for
            # legacy runtime + semantic shadow scoring.
            mode = "legacy"

        shadow_raw = env.get(
            "MEDIA_SELECTOR_SHADOW", strategy.get("media_selector_shadow", False)
        )
        shadow = (shadow_alias or _as_bool(shadow_raw, False)) and mode == "legacy"

        weights = RankingWeights.from_mapping(
            strategy.get("semantic_ranking_weights") or {}
        )
        if "P01_MIN_RELEVANCE_SCORE" in env:
            threshold = _as_float(env.get("P01_MIN_RELEVANCE_SCORE"), 0.26)
        else:
            threshold = _as_float(
                strategy.get("min_relevance_score", strategy.get("semantic_min_relevance_score")),
                0.26,
            )

        return cls(
            mode=mode,
            shadow=shadow,
            min_relevance_score=max(0.0, min(1.0, threshold)),
            embedding_backend=str(
                env.get(
                    "P01_EMBEDDING_BACKEND",
                    strategy.get("semantic_embedding_backend", "auto"),
                )
                or "auto"
            ).strip().lower(),
            embedding_model=str(
                env.get(
                    "P01_EMBEDDING_MODEL",
                    strategy.get(
                        "semantic_embedding_model",
                        "sentence-transformers/all-MiniLM-L6-v2",
                    ),
                )
                or ""
            ),
            embedding_cache_path=str(
                env.get(
                    "P01_EMBEDDING_CACHE",
                    strategy.get("semantic_embedding_cache", ""),
                )
                or ""
            ),
            weights=weights,
            budget=RetrievalBudget.from_mapping(
                strategy.get("semantic_retrieval_budget") or {}
            ),
            visual=VisualConfig.from_mapping(strategy, env),
        )

    def describe(self) -> dict[str, Any]:
        """Compact, JSON-safe summary for logs/provenance."""
        return {
            "mode": self.mode,
            "shadow": self.shadow,
            "min_relevance_score": self.min_relevance_score,
            "embedding_backend": self.embedding_backend,
            "embedding_model": self.embedding_model,
            "top_k": self.budget.top_k,
            "top_n_download": self.budget.top_n_download,
            "visual": self.visual.describe(),
        }
