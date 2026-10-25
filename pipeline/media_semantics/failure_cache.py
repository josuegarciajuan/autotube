"""Bounded per-run cache of assets that failed to download.

A failed URL is not a permanent property of the asset: CDNs have transient
errors.  This cache therefore has **per-run/TTL semantics** and a hard bound;
it never writes to the database or a global blacklist.

The key is the normalised candidate identity (provider + provider asset id,
or a stable URL digest) plus every known URL alias for the same asset.  A
later scene that retrieves the same broken asset can therefore skip the
download instead of retrying it three times again.
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field
from typing import Any, Iterable

DEFAULT_FAILURE_TTL_S = 1800.0
DEFAULT_MAX_ENTRIES = 512


def _url_identity(url: str) -> str:
    return hashlib.sha1(str(url or "").strip().encode("utf-8")).hexdigest()[:20]


@dataclass
class FailedAsset:
    provider: str = ""
    candidate_id: str = ""
    url: str = ""
    failure_category: str = ""
    first_failure: float = 0.0
    skip_count: int = 0

    def to_dict(self, *, now: float | None = None) -> dict[str, Any]:
        now = time.time() if now is None else float(now)
        return {
            "provider": self.provider,
            "candidate_id": self.candidate_id,
            "url": self.url,
            "failure_category": self.failure_category,
            "first_failure": round(self.first_failure, 3),
            "first_failure_iso": time.strftime(
                "%Y-%m-%dT%H:%M:%SZ", time.gmtime(self.first_failure or now)
            ),
            "age_s": round(max(0.0, now - (self.first_failure or now)), 3),
            "skip_count": int(self.skip_count),
        }


class FailedAssetCache:
    """Small TTL + LRU cache keyed by stable asset identity and URL aliases."""

    def __init__(
        self,
        *,
        ttl_s: float = DEFAULT_FAILURE_TTL_S,
        max_entries: int = DEFAULT_MAX_ENTRIES,
        clock=time.time,
    ) -> None:
        self.ttl_s = max(1.0, float(ttl_s))
        self.max_entries = max(1, int(max_entries))
        self._clock = clock
        # asset key -> FailedAsset ; url alias -> asset key
        self._entries: dict[str, FailedAsset] = {}
        self._aliases: dict[str, str] = {}
        self.evictions = 0

    # ── keys ────────────────────────────────────────────────────────────
    @staticmethod
    def asset_key(candidate: Any) -> str:
        try:
            key = str(candidate.canonical_key or "")
        except Exception:
            key = ""
        if key:
            return key
        provider = str(getattr(candidate, "provider", "") or "")
        url = (
            getattr(candidate, "download_url", "")
            or getattr(candidate, "asset_url", "")
            or getattr(candidate, "page_url", "")
        )
        return f"{provider}:url:{_url_identity(url)}"

    @staticmethod
    def url_aliases(candidate: Any) -> list[str]:
        seen: set[str] = set()
        out: list[str] = []
        for name in ("download_url", "asset_url", "preview_url", "page_url"):
            value = str(getattr(candidate, name, "") or "").strip()
            if value and value not in seen:
                seen.add(value)
                out.append(value)
        return out

    # ── public API ─────────────────────────────────────────────────────
    def _expire(self) -> None:
        now = self._clock()
        stale = [key for key, entry in self._entries.items()
                 if now - entry.first_failure > self.ttl_s]
        for key in stale:
            self._remove(key)

    def _remove(self, key: str) -> None:
        entry = self._entries.pop(key, None)
        if entry is None:
            return
        for alias in self._aliases_for_key(key):
            if self._aliases.get(alias) == key:
                self._aliases.pop(alias, None)

    def _aliases_for_key(self, key: str) -> list[str]:
        return [alias for alias, owner in self._aliases.items() if owner == key]

    def _lookup_key(self, candidate: Any) -> str:
        key = self.asset_key(candidate)
        if key in self._entries:
            return key
        for alias in self.url_aliases(candidate):
            owner = self._aliases.get(alias)
            if owner and owner in self._entries:
                return owner
        return ""

    def is_failed(self, candidate: Any) -> bool:
        """Return True (and count the skip) when the asset is TTL-failed."""
        self._expire()
        key = self._lookup_key(candidate)
        if not key:
            return False
        entry = self._entries.get(key)
        if entry is None:
            return False
        entry.skip_count += 1
        return True

    def first_failure(self, candidate: Any) -> float | None:
        self._expire()
        key = self._lookup_key(candidate)
        entry = self._entries.get(key)
        return None if entry is None else float(entry.first_failure)

    def record(
        self,
        candidate: Any,
        *,
        failure_category: str = "download_failed",
    ) -> FailedAsset:
        """Record one failure.  Repeated records increment ``skip_count``."""
        self._expire()
        key = self.asset_key(candidate)
        entry = self._entries.get(key)
        if entry is None:
            aliases = self.url_aliases(candidate)
            entry = FailedAsset(
                provider=str(getattr(candidate, "provider", "") or ""),
                candidate_id=str(getattr(candidate, "provider_asset_id", "") or ""),
                url=(
                    str(getattr(candidate, "download_url", "") or "")
                    or str(getattr(candidate, "asset_url", "") or "")
                    or (aliases[0] if aliases else "")
                ),
                failure_category=str(failure_category or "download_failed"),
                first_failure=float(self._clock()),
            )
            self._entries[key] = entry
            for alias in aliases:
                self._aliases[alias] = key
            self._enforce_bound()
        entry.skip_count += 1
        return entry

    def _enforce_bound(self) -> None:
        while len(self._entries) > self.max_entries:
            # dicts preserve insertion order → evict the oldest entry.
            oldest = next(iter(self._entries))
            self._remove(oldest)
            self.evictions += 1

    def clear(self) -> None:
        self._entries.clear()
        self._aliases.clear()

    def stats(self) -> dict[str, Any]:
        self._expire()
        return {
            "entries": len(self._entries),
            "aliases": len(self._aliases),
            "ttl_s": self.ttl_s,
            "max_entries": self.max_entries,
            "evictions": self.evictions,
            "skip_count": sum(e.skip_count for e in self._entries.values()),
        }

    def snapshot(self) -> list[dict[str, Any]]:
        now = self._clock()
        return [entry.to_dict(now=now) for entry in self._entries.values()]
