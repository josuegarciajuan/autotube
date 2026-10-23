"""P01-D — provider-independent candidate normalisation.

Pexels, Pixabay, Mixkit, Coverr and Unsplash all expose different metadata
shapes.  This module converts the dicts produced by ``MediaFetcher`` into the
canonical :class:`Candidate` without inventing facts.

Mixkit and Coverr only expose a page URL in their search results; the
descriptive slug embedded in that URL is used as title/searchable text.  That
is the provider's own slug, not scraped content, so it is safe and already
available in the current pipeline.
"""

from __future__ import annotations

from typing import Iterable

from .contracts import Candidate, MetadataStatus

# Provider priors encode reliability/curation, not semantic relevance.  They
# are intentionally small so a provider can never outrank a clearly better
# semantic match (P01-G / P01-12).
PROVIDER_PRIORS: dict[str, float] = {
    "pexels": 1.00,
    "pixabay": 0.95,
    "pixabay_photo": 0.95,
    "mixkit": 0.90,
    "coverr": 0.86,
    "unsplash": 0.82,
    "youtube_cc": 0.70,
    "youtube_cc_liberal": 0.60,
    "unknown": 0.50,
}


def provider_prior(provider: str) -> float:
    key = str(provider or "").strip().lower()
    if key in PROVIDER_PRIORS:
        return PROVIDER_PRIORS[key]
    for name, value in PROVIDER_PRIORS.items():
        if name and name in key:
            return value
    return PROVIDER_PRIORS["unknown"]


def normalize_candidate(
    raw: dict,
    *,
    provider: str,
    query: str = "",
    query_tier: str = "",
    query_index: int = 0,
    provider_rank: int = 0,
    page: int = 1,
    media_type: str | None = None,
) -> Candidate:
    """Normalise one provider result into a :class:`Candidate`."""
    candidate = Candidate.from_provider_dict(
        raw,
        provider=provider,
        query=query,
        query_tier=query_tier,
        query_index=query_index,
        provider_rank=provider_rank,
        page=page,
        media_type=media_type,
    )
    # Images from Pixabay/Unsplash carry a description or no text at all; the
    # classification stays explicit so the ranker never confuses it with a
    # known zero score.
    if not candidate.searchable_text:
        candidate.metadata_status = MetadataStatus.NO_METADATA
    return candidate


def normalize_candidates(
    raw_candidates: Iterable[dict],
    *,
    provider: str,
    query: str = "",
    query_tier: str = "",
    query_index: int = 0,
    page: int = 1,
    media_type: str | None = None,
) -> list[Candidate]:
    """Normalise a whole page preserving the provider's own rank order."""
    out: list[Candidate] = []
    for rank, raw in enumerate(raw_candidates or []):
        if not isinstance(raw, dict):
            continue
        out.append(
            normalize_candidate(
                raw,
                provider=provider,
                query=query,
                query_tier=query_tier,
                query_index=query_index,
                provider_rank=rank,
                page=page,
                media_type=media_type,
            )
        )
    return out
