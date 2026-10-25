"""P01 — Semantic retrieval core for media selection.

This package isolates the provider-agnostic contracts, retrieval pool,
ranking and decision/provenance machinery introduced by P01.  The legacy
selector in :mod:`pipeline.media_fetcher` remains available and is the
default; the semantic selector is opt-in via ``MEDIA_SELECTOR=semantic``.

Public surface (stable for P02/P03):

* :class:`~pipeline.media_semantics.contracts.SceneSpec`
* :class:`~pipeline.media_semantics.contracts.Candidate`
* :class:`~pipeline.media_semantics.contracts.SelectionDecision`
* :class:`~pipeline.media_semantics.selector.SemanticMediaSelector`
* :class:`~pipeline.media_semantics.pool.CandidatePool`
* :class:`~pipeline.media_semantics.replay.ReplayHarness`
* :class:`~pipeline.media_semantics.benchmark.BenchmarkCorpus`

P02 adds a visual extension under this same namespace: preview/thumbnail
funnel, optional CLIP/SigLIP visual embeddings, semantic frame sampling,
structured VLM verification, visual constraints and an explainable combined
score.  It is disabled by default and degrades to P01 text ranking.
"""

from .contracts import (  # noqa: F401
    Candidate,
    MetadataStatus,
    SceneSpec,
    SelectionDecision,
    SelectionStatus,
)
from .selector import SemanticMediaSelector  # noqa: F401
from .visual_backends import (  # noqa: F401
    CLIPVisualEmbeddingBackend,
    NullVisualEmbeddingBackend,
    SigLIPVisualEmbeddingBackend,
    build_visual_backend,
)
from .visual_pipeline import VisualIntelligencePipeline  # noqa: F401
from .visual_vlm import (  # noqa: F401
    NullVLMBackend,
    OpenAICompatibleVLMBackend,
    VisualVerificationResult,
    build_vlm_backend,
    parse_vlm_payload,
)
from .source_router import (  # noqa: F401
    AIPolicy,
    AssetType,
    SceneCategory,
    SourceRoute,
    SourceRouter,
)
from .asset_strategy import AssetStrategy, build_asset_strategy  # noqa: F401
from .sourcing_pipeline import (  # noqa: F401
    SourcedMediaSelector,
    SourcingConfig,
    SourcingResult,
)
from .dedup import (  # noqa: F401
    DuplicateMatch,
    PerceptualDuplicateIndex,
    image_hash,
)
from .coherence import SelectionCoherence  # noqa: F401
from .attribution import (  # noqa: F401
    build_attribution_manifest,
    write_attribution_manifest,
)

from .integrated import (  # noqa: F401
    IntegratedConfig,
    IntegratedSelector,
    describe_universe,
    is_generated_asset,
    is_real_asset,
    stage1_universe,
    stage2_universe,
)
from .unified_scoring import (  # noqa: F401
    PROFILE_FULL,
    PROFILE_PREVIEW,
    PROFILE_TEXT_ONLY,
    ScoreComponents,
    UnifiedWeights,
    VisualEvidence,
    choose_visual_budget,
    score_candidate,
)
from .entity_queries import archive_queries, build_archive_query_plan  # noqa: F401
from .shadow import ShadowRecorder, run_shadow_scene  # noqa: F401

SELECTOR_VERSION = "p01-semantic-1"
VISUAL_SELECTOR_VERSION = "p02-visual-1"
SOURCE_ROUTER_VERSION = "p04-source-router-1"
SOURCING_VERSION = "p04-source-fallback-1"
INTEGRATED_VERSION = "p05-integrated-1"

__all__ = [
    "Candidate",
    "MetadataStatus",
    "SceneSpec",
    "SelectionDecision",
    "SelectionStatus",
    "SemanticMediaSelector",
    "SELECTOR_VERSION",
    "VISUAL_SELECTOR_VERSION",
    "SOURCE_ROUTER_VERSION",
    "SOURCING_VERSION",
    "SceneCategory",
    "AssetType",
    "AIPolicy",
    "SourceRoute",
    "SourceRouter",
    "AssetStrategy",
    "build_asset_strategy",
    "SourcedMediaSelector",
    "SourcingConfig",
    "SourcingResult",
    "DuplicateMatch",
    "PerceptualDuplicateIndex",
    "image_hash",
    "SelectionCoherence",
    "build_attribution_manifest",
    "write_attribution_manifest",
    "VisualIntelligencePipeline",
    "VisualVerificationResult",
    "CLIPVisualEmbeddingBackend",
    "SigLIPVisualEmbeddingBackend",
    "NullVisualEmbeddingBackend",
    "build_visual_backend",
    "OpenAICompatibleVLMBackend",
    "NullVLMBackend",
    "build_vlm_backend",
    "parse_vlm_payload",
    "INTEGRATED_VERSION",
    "IntegratedSelector",
    "IntegratedConfig",
    "UnifiedWeights",
    "VisualEvidence",
    "ScoreComponents",
    "score_candidate",
    "choose_visual_budget",
    "PROFILE_TEXT_ONLY",
    "PROFILE_PREVIEW",
    "PROFILE_FULL",
    "is_real_asset",
    "is_generated_asset",
    "stage1_universe",
    "stage2_universe",
    "describe_universe",
    "archive_queries",
    "build_archive_query_plan",
    "run_shadow_scene",
    "ShadowRecorder",
]
