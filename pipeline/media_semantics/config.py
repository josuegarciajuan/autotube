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

VALID_MODES = ("legacy", "semantic")


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
            semantic=_as_float(data.get("semantic"), defaults.semantic),
            lexical=_as_float(data.get("lexical"), defaults.lexical),
            action=_as_float(data.get("action"), defaults.action),
            subject=_as_float(data.get("subject"), defaults.subject),
            environment=_as_float(
                data.get("environment", data.get("environment_match")),
                defaults.environment,
            ),
            era=_as_float(data.get("era"), defaults.era),
            metadata_quality=_as_float(
                data.get("metadata_quality"), defaults.metadata_quality
            ),
            provider_prior=_as_float(
                data.get("provider_prior"), defaults.provider_prior
            ),
            negative_constraint=_as_float(
                data.get("negative_constraint"), defaults.negative_constraint
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
            max_total=max(1, _as_int(data.get("max_total"), defaults.max_total)),
            max_per_provider=max(
                1, _as_int(data.get("max_per_provider"), defaults.max_per_provider)
            ),
            max_per_query=max(
                1, _as_int(data.get("max_per_query"), defaults.max_per_query)
            ),
            max_pages_per_provider=max(
                1,
                _as_int(
                    data.get("max_pages_per_provider"),
                    defaults.max_pages_per_provider,
                ),
            ),
            max_queries=max(1, _as_int(data.get("max_queries"), defaults.max_queries)),
            top_k=max(1, _as_int(data.get("top_k"), defaults.top_k)),
            top_n_download=max(
                1, _as_int(data.get("top_n_download"), defaults.top_n_download)
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
        if mode not in VALID_MODES:
            mode = "legacy"

        shadow_raw = env.get(
            "MEDIA_SELECTOR_SHADOW", strategy.get("media_selector_shadow", False)
        )
        shadow = _as_bool(shadow_raw, False) and mode == "legacy"

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
        }
