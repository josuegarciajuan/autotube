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

SELECTOR_VERSION = "p01-semantic-1"
VISUAL_SELECTOR_VERSION = "p02-visual-1"

__all__ = [
    "Candidate",
    "MetadataStatus",
    "SceneSpec",
    "SelectionDecision",
    "SelectionStatus",
    "SemanticMediaSelector",
    "SELECTOR_VERSION",
    "VISUAL_SELECTOR_VERSION",
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
]
