"""P01-E — global candidate pool (metadata only).

Retrieval adds candidates from multiple queries/providers/pages before any
download.  The pool enforces configurable budgets, deduplicates deterministically
and preserves the retrieval order so downstream ranking is reproducible.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

from .config import RetrievalBudget
from .contracts import Candidate


def _url_key(url: str) -> str:
    """Normalise a URL for dedup, ignoring volatile query/fragment parts."""
    return str(url or "").split("?", 1)[0].split("#", 1)[0].rstrip("/").lower()


@dataclass
class PoolStats:
    added: int = 0
    duplicates: int = 0
    over_provider_budget: int = 0
    over_query_budget: int = 0
    over_total_budget: int = 0
    no_metadata: int = 0

    def to_dict(self) -> dict[str, int]:
        return {
            "added": self.added,
            "duplicates": self.duplicates,
            "over_provider_budget": self.over_provider_budget,
            "over_query_budget": self.over_query_budget,
            "over_total_budget": self.over_total_budget,
            "no_metadata": self.no_metadata,
        }


@dataclass
class CandidatePool:
    """Bounded, deduplicated, deterministically ordered candidate set."""

    budget: RetrievalBudget = field(default_factory=RetrievalBudget)
    _candidates: list[Candidate] = field(default_factory=list, repr=False)
    _keys: set[str] = field(default_factory=set, repr=False)
    _url_keys: set[str] = field(default_factory=set, repr=False)
    _provider_counts: dict[str, int] = field(default_factory=dict, repr=False)
    _query_counts: dict[str, int] = field(default_factory=dict, repr=False)
    _providers_seen: list[str] = field(default_factory=list, repr=False)
    stats: PoolStats = field(default_factory=PoolStats, repr=False)

    def _note_provider(self, provider: str) -> None:
        if provider not in self._providers_seen:
            self._providers_seen.append(provider)

    def add(self, candidate: Candidate) -> bool:
        """Add one candidate if it fits budgets and is not a duplicate."""
        key = candidate.canonical_key
        url_key = _url_key(candidate.download_url or candidate.asset_url or candidate.page_url)
        if key in self._keys or (url_key and url_key in self._url_keys):
            self.stats.duplicates += 1
            return False

        provider = candidate.provider or "unknown"
        query_key = candidate.query or ""
        if self.stats.added >= self.budget.max_total:
            self.stats.over_total_budget += 1
            return False
        if self._provider_counts.get(provider, 0) >= self.budget.max_per_provider:
            self.stats.over_provider_budget += 1
            return False
        if self._query_counts.get(query_key, 0) >= self.budget.max_per_query:
            self.stats.over_query_budget += 1
            return False

        self._candidates.append(candidate)
        self._keys.add(key)
        if url_key:
            self._url_keys.add(url_key)
        self._provider_counts[provider] = self._provider_counts.get(provider, 0) + 1
        self._query_counts[query_key] = self._query_counts.get(query_key, 0) + 1
        self._note_provider(provider)
        self.stats.added += 1
        if not candidate.has_metadata:
            self.stats.no_metadata += 1
        return True

    def extend(self, candidates: Iterable[Candidate]) -> int:
        added = 0
        for candidate in candidates:
            if self.add(candidate):
                added += 1
        return added

    def candidates(self) -> list[Candidate]:
        """Insertion order is deterministic for a deterministic retrieval."""
        return list(self._candidates)

    def ordered(self) -> list[Candidate]:
        """Explicit deterministic ordering independent of caller iteration."""
        return sorted(
            self._candidates,
            key=lambda c: (
                int(c.query_index),
                str(c.provider),
                int(c.page),
                int(c.provider_rank),
                c.canonical_key,
            ),
        )

    @property
    def providers(self) -> list[str]:
        return list(self._providers_seen)

    def __len__(self) -> int:
        return len(self._candidates)

    def __bool__(self) -> bool:
        return bool(self._candidates)
