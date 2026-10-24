"""P03-C/D — bounded live retrieval, previews and video downloads.

Every network call goes through :class:`RequestLedger`, which records request
count, bytes and downloaded assets per provider.  The benchmark never calls the
production pipeline, never uploads and never publishes.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests

from pipeline.image_fetcher import PixabayImageProvider
from pipeline.media_fetcher import MediaFetcher
from pipeline.media_semantics.config import VisualBudget
from pipeline.media_semantics.contracts import Candidate, PREVIEW_AVAILABLE
from pipeline.media_semantics.normalization import normalize_candidates
from pipeline.media_semantics.pool import CandidatePool
from pipeline.media_semantics.query_generation import generate_scene_query_variants
from pipeline.media_semantics.visual_previews import PreviewFetcher, PreviewResult
from pipeline.providers.coverr import CoverrVideoProvider
from pipeline.providers.mixkit import MixkitVideoProvider
from pipeline.providers.pexels import PexelsVideoProvider
from pipeline.providers.pixabay import PixabayVideoProvider


DEFAULT_ENV = Path("/root/autotube/.env")
MAX_VIDEO_BYTES = 30 * 1024 * 1024


@dataclass
class RequestLedger:
    """Accountable network budget for one benchmark run."""

    requests: int = 0
    bytes: int = 0
    downloads: int = 0
    previews: int = 0
    frames: int = 0
    per_provider: dict[str, dict[str, int]] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)

    def record(
        self,
        provider: str,
        *,
        requests: int = 0,
        bytes: int = 0,
        download: bool = False,
        preview: bool = False,
        frame: bool = False,
    ) -> None:
        stats = self.per_provider.setdefault(
            provider, {"requests": 0, "bytes": 0, "downloads": 0, "previews": 0, "frames": 0}
        )
        stats["requests"] += int(requests)
        stats["bytes"] += int(bytes)
        self.requests += int(requests)
        self.bytes += int(bytes)
        if download:
            stats["downloads"] += 1
            self.downloads += 1
        if preview:
            stats["previews"] += 1
            self.previews += 1
        if frame:
            stats["frames"] += 1
            self.frames += 1

    def error(self, message: str) -> None:
        self.errors.append(str(message)[:300])

    def to_dict(self) -> dict[str, Any]:
        return {
            "requests": self.requests,
            "bytes": self.bytes,
            "downloads": self.downloads,
            "previews": self.previews,
            "frames": self.frames,
            "per_provider": dict(sorted(self.per_provider.items())),
            "errors": list(self.errors),
        }


def load_provider_env(path: str | Path = DEFAULT_ENV) -> dict[str, str]:
    """Read provider keys from the production .env without logging them."""
    values: dict[str, str] = {}
    path = Path(path)
    if path.is_file():
        for line in path.read_text(errors="ignore").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip().strip('"').strip("'")
    for key in (
        "PEXELS_API_KEY", "PIXABAY_API_KEY", "UNSPLASH_ACCESS_KEY",
        "OPENAI_API_KEY", "LLM_API_KEY", "LLM_BASE_URL", "LLM_MODEL",
        "VISION_MODEL", "VISION_BASE_URL",
    ):
        if os.getenv(key):
            values[key] = os.environ[key]
    return values


def provider_presence(env: dict[str, str]) -> dict[str, str]:
    return {
        "pexels": "PRESENT" if env.get("PEXELS_API_KEY") else "ABSENT",
        "pixabay": "PRESENT" if env.get("PIXABAY_API_KEY") else "ABSENT",
        "mixkit": "NO_KEY_REQUIRED",
        "coverr": "NO_KEY_REQUIRED",
        "openai_vision": "PRESENT" if env.get("OPENAI_API_KEY") else "ABSENT",
        "llm_text": "PRESENT" if env.get("LLM_API_KEY") else "ABSENT",
    }


class ProviderRegistry:
    """Construct providers lazily; the API-key ones only when a key exists."""

    def __init__(self, env: dict[str, str]) -> None:
        self.env = env
        self._video: dict[str, Any] = {}
        if env.get("PEXELS_API_KEY"):
            self._video["pexels"] = PexelsVideoProvider(api_key=env["PEXELS_API_KEY"])
        if env.get("PIXABAY_API_KEY"):
            self._video["pixabay"] = PixabayVideoProvider(api_key=env["PIXABAY_API_KEY"])
        self._video["mixkit"] = MixkitVideoProvider()
        self._video["coverr"] = CoverrVideoProvider()
        self._images: dict[str, Any] = {}
        if env.get("PIXABAY_API_KEY"):
            self._images["pixabay_photo"] = PixabayImageProvider(env["PIXABAY_API_KEY"])

    def for_media_type(self, media_type: str, include_scrapers: bool) -> list[str]:
        if media_type == "video":
            names = ["pexels", "pixabay"]
            if include_scrapers:
                names += ["mixkit", "coverr"]
            return [name for name in names if name in self._video]
        return [name for name in ("pixabay_photo",) if name in self._images]

    @property
    def video(self) -> dict[str, Any]:
        return self._video

    @property
    def images(self) -> dict[str, Any]:
        return self._images


def scene_queries(spec, *, max_queries: int = 2) -> list[str]:
    """English, scene-specific queries; never the Spanish narration fallback."""
    variants = generate_scene_query_variants(spec, [spec.extra.get("query_en", "")], max_variants=4)
    queries: list[str] = []
    for item in variants:
        query = str(item.get("query") or "").strip()
        if not query or query in queries:
            continue
        # Keep provider queries English/ASCII-ish: the narration-derived
        # variant is Spanish and only adds noise for stock APIs.
        if any(ch in query for ch in "áéíóúüñ¿¡"):
            continue
        queries.append(query)
        if len(queries) >= max_queries:
            break
    return queries or [spec.extra.get("query_en", "") or spec.subject]


def _raw_for_page(provider: Any, query: str, target_dur: float) -> list[dict]:
    """Reuse the production page normaliser (no network in this helper)."""
    if hasattr(provider, "search_page"):
        # MediaFetcher's method only needs ``_vp_disabled`` and the provider.
        shim = object.__new__(MediaFetcher)
        shim._vp_disabled = set()
        return MediaFetcher._search_video_provider_page(
            shim, provider, query, target_dur, 1
        )
    return []


def _raw_for_image_page(provider: Any, query: str) -> list[dict]:
    shim = object.__new__(MediaFetcher)
    shim._vp_disabled = set()
    return MediaFetcher._search_image_provider_page(shim, provider, query, 1)


def retrieve_scene(
    entry: dict[str, Any],
    spec: Any,
    registry: ProviderRegistry,
    ledger: RequestLedger,
    *,
    include_scrapers: bool = False,
    max_candidates: int = 60,
    log_fn: Any = None,
) -> dict[str, Any]:
    """Retrieve one comparable candidate pool for one scene.

    The same pool is later handed to legacy/text/visual unchanged.
    """
    media_type = entry.get("media_type") or "image"
    names = registry.for_media_type(media_type, include_scrapers)
    queries = scene_queries(spec)
    pool = CandidatePool()
    provider_counts: dict[str, int] = {}
    started = time.time()
    for query_index, query in enumerate(queries):
        for name in names:
            provider = (
                registry.video.get(name)
                if media_type == "video"
                else registry.images.get(name)
            )
            if provider is None:
                continue
            try:
                if media_type == "video":
                    raw = _raw_for_page(provider, query, float(spec.duration or 8.0))
                    ledger.record(name, requests=1)
                else:
                    raw = _raw_for_image_page(provider, query)
                    ledger.record(name, requests=1)
            except Exception as exc:  # fail-open per provider
                ledger.error(f"{entry['scene_id']}/{name}: {exc}")
                if log_fn:
                    log_fn(f"{entry['scene_id']} {name} retrieval failed: {exc}")
                continue
            provider_counts[name] = provider_counts.get(name, 0) + len(raw)
            candidates = normalize_candidates(
                raw,
                provider=name,
                query=query,
                query_index=query_index,
                page=1,
                media_type=media_type,
            )
            pool.extend(candidates)
            if len(pool) >= max_candidates:
                break
        if len(pool) >= max_candidates:
            break
        time.sleep(0.15)

    return {
        "scene_id": entry["scene_id"],
        "queries": queries,
        "media_type": media_type,
        "providers_requested": names,
        "provider_raw_counts": provider_counts,
        "pool_stats": pool.stats.to_dict(),
        "candidates": [c.to_dict() for c in pool.candidates()],
        "retrieval_ms": round((time.time() - started) * 1000, 1),
    }


def fetch_previews(
    scene_id: str,
    candidates: list[Candidate],
    out_dir: Path,
    ledger: RequestLedger,
    *,
    max_candidates: int = 12,
    preview_bytes: int = 512 * 1024,
    total_bytes: int = 4 * 1024 * 1024,
) -> dict[str, dict[str, Any]]:
    """Fetch provider-declared stills for the top candidates (bounded)."""
    work_dir = out_dir / "previews" / scene_id
    work_dir.mkdir(parents=True, exist_ok=True)
    budget = VisualBudget(
        preview_candidates=max_candidates,
        preview_bytes=preview_bytes,
        preview_total_bytes=total_bytes,
        preview_timeout=12.0,
    )
    fetcher = PreviewFetcher(budget, work_dir=work_dir, keep_files=True)
    results: dict[str, dict[str, Any]] = {}
    for candidate in candidates[:max_candidates]:
        started = time.time()
        try:
            result = fetcher.fetch(candidate)
        except Exception as exc:  # fail-open
            result = PreviewResult(
                candidate_key=candidate.canonical_key,
                status="PREVIEW_UNAVAILABLE",
                error=str(exc)[:200],
            )
        results[candidate.canonical_key] = result.to_dict()
        if result.available:
            ledger.record(candidate.provider, bytes=result.bytes, preview=True)
        _ = started
    return results


def download_video_bounded(
    candidate: Candidate,
    out_dir: Path,
    ledger: RequestLedger,
    *,
    max_bytes: int = MAX_VIDEO_BYTES,
) -> dict[str, Any] | None:
    """Download one direct video URL with a hard byte cap."""
    url = str(candidate.download_url or candidate.asset_url or "")
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return None
    suffix = Path(parsed.path).suffix.lower() or ".mp4"
    target = out_dir / "videos" / f"{_safe_name(candidate.canonical_key)}{suffix}"
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_file() and target.stat().st_size > 0:
        return {
            "path": str(target),
            "type": "video",
            "duration": candidate.duration,
            "bytes": target.stat().st_size,
            "cached": True,
        }
    try:
        with requests.get(
            url,
            stream=True,
            timeout=45,
            headers={"User-Agent": "Mozilla/5.0 (compatible; autotube-p03/1.0)"},
            allow_redirects=True,
        ) as response:
            status = int(getattr(response, "status_code", 0) or 0)
            if status >= 400:
                ledger.error(f"download http {status}: {url[:120]}")
                return None
            declared = int(response.headers.get("Content-Length") or 0)
            if declared and declared > max_bytes:
                ledger.error(f"download too large ({declared} bytes): {url[:120]}")
                return None
            total = 0
            tmp = target.with_suffix(target.suffix + ".part")
            with tmp.open("wb") as handle:
                for chunk in response.iter_content(128 * 1024):
                    if not chunk:
                        continue
                    total += len(chunk)
                    if total > max_bytes:
                        handle.close()
                        tmp.unlink(missing_ok=True)
                        ledger.error(f"download exceeded cap: {url[:120]}")
                        return None
                    handle.write(chunk)
            tmp.replace(target)
    except Exception as exc:
        ledger.error(f"download failed: {exc}")
        return None
    ledger.record(candidate.provider, bytes=total, download=True)
    return {
        "path": str(target),
        "type": "video",
        "duration": candidate.duration,
        "bytes": total,
        "cached": False,
    }


def _safe_name(value: str) -> str:
    import hashlib

    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:20]


def load_visual_budget(**kwargs: Any) -> VisualBudget:
    return VisualBudget(**kwargs)
