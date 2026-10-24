"""P02-B/E — bounded preview/thumbnail acquisition.

Only provider-declared thumbnail/poster URLs are used.  The full video is
never downloaded here.  Remote fetches are capped by bytes and timeout, and a
missing preview is reported as ``PREVIEW_UNAVAILABLE`` (not negative evidence).
"""

from __future__ import annotations

import base64
import ipaddress
import logging
import shutil
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

from .config import VisualBudget
from .contracts import Candidate, PREVIEW_AVAILABLE, PREVIEW_UNAVAILABLE
from .visual_cache import sha256_bytes

logger = logging.getLogger(__name__)

_IMAGE_MAGIC = (
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
    (b"BM", "image/bmp"),
)


def _detect_image_mime(data: bytes) -> str:
    for magic, mime in _IMAGE_MAGIC:
        if data.startswith(magic):
            return mime
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return ""


def is_safe_remote_preview_url(url: str) -> bool:
    """Reject non-http(s), localhost and literal private/loopback addresses."""
    try:
        parsed = urlparse(str(url or ""))
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            return False
        host = parsed.hostname.strip().lower()
        if host in ("localhost", "localhost.localdomain") or host.endswith(".local"):
            return False
        try:
            ip = ipaddress.ip_address(host)
            return bool(ip.is_global)
        except ValueError:
            return True
    except Exception:
        return False


@dataclass
class PreviewBudgetState:
    max_candidates: int = 12
    max_total_bytes: int = 6 * 1024 * 1024
    candidates_fetched: int = 0
    bytes_fetched: int = 0
    unavailable: int = 0
    failed: int = 0

    def can_fetch(self) -> bool:
        return (
            self.candidates_fetched < self.max_candidates
            and self.bytes_fetched < self.max_total_bytes
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "max_candidates": self.max_candidates,
            "max_total_bytes": self.max_total_bytes,
            "candidates_fetched": self.candidates_fetched,
            "bytes_fetched": self.bytes_fetched,
            "unavailable": self.unavailable,
            "failed": self.failed,
        }


@dataclass
class PreviewResult:
    candidate_key: str = ""
    status: str = PREVIEW_UNAVAILABLE
    path: str = ""
    url: str = ""
    content_type: str = ""
    bytes: int = 0
    width: int | None = None
    height: int | None = None
    visual_source: str = ""
    error: str = ""
    timing_ms: float = 0.0

    @property
    def available(self) -> bool:
        return self.status == PREVIEW_AVAILABLE and bool(self.path)

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate_key": self.candidate_key,
            "status": self.status,
            "url": self.url,
            "content_type": self.content_type,
            "bytes": self.bytes,
            "width": self.width,
            "height": self.height,
            "visual_source": self.visual_source,
            "path": self.path,
            "error": self.error,
            "timing_ms": round(self.timing_ms, 3),
        }


class PreviewFetcher:
    """Fetch a small still for visual inspection, bounded per budget."""

    def __init__(
        self,
        budget: VisualBudget | None = None,
        *,
        work_dir: str | Path | None = None,
        http_get: Callable[..., Any] | None = None,
        keep_files: bool = True,
        allow_local_paths: bool | None = None,
    ) -> None:
        self.budget = budget or VisualBudget()
        self._work_dir = Path(work_dir) if work_dir else Path(tempfile.mkdtemp(prefix="p02_previews_"))
        self._http_get = http_get
        self._keep_files = keep_files
        self._owned_dir = work_dir is None
        # Local fixture paths are an offline-replay feature.  Production
        # callers (no explicit work_dir) must not read arbitrary local paths
        # proposed by external provider metadata.
        self._allow_local_paths = (
            bool(allow_local_paths) if allow_local_paths is not None
            else work_dir is not None
        )
        self.state = PreviewBudgetState(
            max_candidates=self.budget.preview_candidates,
            max_total_bytes=self.budget.preview_total_bytes,
        )

    def _choose_url(self, candidate: Candidate) -> tuple[str, str]:
        if candidate.thumbnail_url:
            return candidate.thumbnail_url, "thumbnail"
        if candidate.preview_url:
            ptype = str(candidate.preview_type or "").lower()
            if ptype != "video":
                return candidate.preview_url, "preview"
        return "", ""

    def fetch(self, candidate: Candidate) -> PreviewResult:
        started = time.time()
        key = candidate.canonical_key
        if not getattr(candidate, "has_preview", False):
            self.state.unavailable += 1
            return PreviewResult(
                candidate_key=key,
                status=PREVIEW_UNAVAILABLE,
                visual_source=candidate.visual_source,
                error="no_provider_preview",
                timing_ms=(time.time() - started) * 1000,
            )
        if not self.state.can_fetch():
            self.state.unavailable += 1
            return PreviewResult(
                candidate_key=key,
                status=PREVIEW_UNAVAILABLE,
                visual_source=candidate.visual_source,
                error="preview_budget_exhausted",
                timing_ms=(time.time() - started) * 1000,
            )
        url, source = self._choose_url(candidate)
        if not url:
            self.state.unavailable += 1
            return PreviewResult(
                candidate_key=key,
                status=PREVIEW_UNAVAILABLE,
                visual_source=candidate.visual_source,
                error="no_image_preview",
                timing_ms=(time.time() - started) * 1000,
            )
        self.state.candidates_fetched += 1
        try:
            result = self._fetch_one(candidate, key, url, source)
        except Exception as exc:  # noqa: BLE001
            self.state.failed += 1
            result = PreviewResult(
                candidate_key=key, status=PREVIEW_UNAVAILABLE,
                url=url, visual_source=candidate.visual_source,
                error=str(exc)[:200],
            )
        result.timing_ms = (time.time() - started) * 1000
        if not result.available:
            self.state.failed += 1
        else:
            self.state.bytes_fetched += result.bytes
        return result

    def _fetch_one(
        self, candidate: Candidate, key: str, url: str, source: str
    ) -> PreviewResult:
        local = self._local_path(url)
        if local is not None:
            data = local.read_bytes()
            if len(data) > self.budget.preview_bytes:
                return PreviewResult(
                    candidate_key=key, status=PREVIEW_UNAVAILABLE, url=url,
                    visual_source=candidate.visual_source,
                    error="preview_too_large", bytes=len(data),
                )
            mime = _detect_image_mime(data)
            if not mime:
                return PreviewResult(
                    candidate_key=key, status=PREVIEW_UNAVAILABLE, url=url,
                    visual_source=candidate.visual_source,
                    error="not_an_image", bytes=len(data),
                )
            return PreviewResult(
                candidate_key=key, status=PREVIEW_AVAILABLE, path=str(local),
                url=url, content_type=mime, bytes=len(data),
                width=candidate.preview_width, height=candidate.preview_height,
                visual_source=candidate.visual_source or source,
            )

        parsed = urlparse(url)
        if not is_safe_remote_preview_url(url):
            return PreviewResult(
                candidate_key=key, status=PREVIEW_UNAVAILABLE, url=url,
                visual_source=candidate.visual_source,
                error="unsafe_or_unsupported_preview_url",
            )

        getter = self._http_get
        if getter is None:
            import requests

            def getter(u, **kwargs):
                return requests.get(u, **kwargs)

        headers = {
            "User-Agent": "Mozilla/5.0 (compatible; autotube-p02/1.0)",
            "Accept": "image/avif,image/webp,image/png,image/jpeg,image/*;q=0.8",
        }
        if "pixabay" in str(candidate.provider).lower():
            headers["Referer"] = "https://pixabay.com/"
        response = getter(
            url, headers=headers, timeout=self.budget.preview_timeout,
            stream=True, allow_redirects=True,
        )
        try:
            status = int(getattr(response, "status_code", 200))
            if status >= 400:
                return PreviewResult(
                    candidate_key=key, status=PREVIEW_UNAVAILABLE, url=url,
                    visual_source=candidate.visual_source, error=f"http_{status}",
                )
            content_type = str(getattr(response, "headers", {}).get("Content-Type", "") or "").lower()
            if content_type and "image" not in content_type and "octet-stream" not in content_type:
                return PreviewResult(
                    candidate_key=key, status=PREVIEW_UNAVAILABLE, url=url,
                    visual_source=candidate.visual_source,
                    error=f"content_type:{content_type[:60]}",
                )
            chunks: list[bytes] = []
            total = 0
            for chunk in response.iter_content(64 * 1024):
                if not chunk:
                    continue
                total += len(chunk)
                if total > self.budget.preview_bytes:
                    return PreviewResult(
                        candidate_key=key, status=PREVIEW_UNAVAILABLE, url=url,
                        visual_source=candidate.visual_source,
                        error="preview_too_large", bytes=total,
                    )
                chunks.append(chunk)
            data = b"".join(chunks)
        finally:
            try:
                response.close()
            except Exception:
                pass
        mime = _detect_image_mime(data)
        if not mime:
            return PreviewResult(
                candidate_key=key, status=PREVIEW_UNAVAILABLE, url=url,
                visual_source=candidate.visual_source, error="not_an_image", bytes=len(data),
            )
        self._work_dir.mkdir(parents=True, exist_ok=True)
        suffix = {
            "image/jpeg": ".jpg", "image/png": ".png", "image/gif": ".gif",
            "image/webp": ".webp", "image/bmp": ".bmp",
        }.get(mime, ".img")
        safe_key = sha256_bytes(key.encode("utf-8"))[:24]
        path = self._work_dir / f"{safe_key}{suffix}"
        path.write_bytes(data)
        return PreviewResult(
            candidate_key=key, status=PREVIEW_AVAILABLE, path=str(path),
            url=url, content_type=mime, bytes=len(data),
            width=candidate.preview_width, height=candidate.preview_height,
            visual_source=candidate.visual_source or source,
        )

    def _local_path(self, url: str) -> Path | None:
        if not url:
            return None
        if url.startswith("data:image/") and ";base64," in url:
            try:
                raw = url.split(",", 1)[1]
                data = base64.b64decode(raw, validate=True)
                tmp = Path(tempfile.mkstemp(prefix="p02_data_", suffix=".img")[1])
                tmp.write_bytes(data)
                return tmp
            except Exception:
                return None
        if not self._allow_local_paths:
            return None
        if url.startswith("file://"):
            path = Path(url[7:])
            return path if path.is_file() else None
        if url.startswith("/") and Path(url).is_file():
            return Path(url)
        return None

    def cleanup(self) -> None:
        if self._owned_dir and not self._keep_files:
            shutil.rmtree(self._work_dir, ignore_errors=True)
