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

No visual embeddings (CLIP/SigLIP), frame analysis or new providers are
implemented here.  Text-embedding backends are pluggable so those phases can
add capabilities without changing the decision contract.
"""

from .contracts import (  # noqa: F401
    Candidate,
    MetadataStatus,
    SceneSpec,
    SelectionDecision,
    SelectionStatus,
)
from .selector import SemanticMediaSelector  # noqa: F401

SELECTOR_VERSION = "p01-semantic-1"

__all__ = [
    "Candidate",
    "MetadataStatus",
    "SceneSpec",
    "SelectionDecision",
    "SelectionStatus",
    "SemanticMediaSelector",
    "SELECTOR_VERSION",
]
