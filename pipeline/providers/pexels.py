"""Pexels Videos API provider — free stock video clips.

Endpoint: https://api.pexels.com/videos/search
Auth: PEXELS_API_KEY in Authorization header.
Rate limit: 200 req/h. Handles 429 with Retry-After header.
"""

import logging
import os
import re
import time
from pathlib import Path
from typing import Optional

import requests

from pipeline.providers.base import BaseVideoProvider, VideoAsset

logger = logging.getLogger(__name__)


class PexelsVideoProvider(BaseVideoProvider):
    """Video provider backed by the Pexels Videos API.

    Refactored from the original PexelsVideoProvider in pipeline/media_fetcher.py.
    Now conforms to the BaseVideoProvider interface.
    """

    BASE_URL = "https://api.pexels.com/videos/search"

    def __init__(self, api_key: Optional[str] = None) -> None:
        """Initialize with an optional API key.

        Falls back to the PEXELS_API_KEY environment variable if no key
        is provided.
        """
        super().__init__(api_key=api_key)
        resolved_key = api_key or os.getenv("PEXELS_API_KEY", "")
        if not resolved_key:
            logger.warning("PEXELS_API_KEY not set — PexelsVideoProvider will not work")
        self._api_key = resolved_key

    @property
    def name(self) -> str:
        return "pexels"

    def search(
        self,
        query: str,
        min_duration: float,
        max_duration: float,
        resolution: tuple = (1920, 1080),
        page: int = 1,
        per_page: int = 20,
        orientation: str = "landscape",
    ) -> Optional[VideoAsset]:
        """Search Pexels Videos API for the first clip matching all criteria.

        Args:
            query: Search keywords.
            min_duration: Minimum acceptable duration in seconds.
            max_duration: Maximum acceptable duration in seconds.
            resolution: Preferred resolution (width, height).
            page: Page number (1-indexed).
            per_page: Results per page (max 80).
            orientation: 'landscape' (default) or 'portrait' for vertical
                         video search (used for Shorts).

        Returns:
            VideoAsset with file_path set to Path() (placeholder), or None.
        """
        if not self._api_key:
            logger.error("Cannot search Pexels without an API key")
            return None

        params: dict = {
            "query": query,
            "per_page": min(per_page, 80),
            "page": page,
            "orientation": orientation,
            "size": "medium",
        }

        resp = self._request_with_retry(params)
        if resp is None:
            return None

        data = resp.json()
        videos = data.get("videos", [])

        for video in videos:
            dur = video.get("duration", 0)
            if dur < min_duration or dur > max_duration:
                continue

            video_files = video.get("video_files", [])
            best = self._pick_best_quality(video_files, resolution)
            if not best:
                continue

            video_id = str(video.get("id", ""))
            logger.info(
                "Pexels: found video id=%s dur=%.1fs res=%dx%d for query=%r",
                video_id, dur, best.get("width", 0), best.get("height", 0), query,
            )
            download_url = best.get("link", "")
            if not download_url:
                download_url = video.get("url", "")
                logger.warning(
                    "Pexels: no direct link for video id=%s — using page URL "
                    "as fallback (may fail if provider download cannot scrape)",
                    video_id,
                )
            return VideoAsset(
                url=download_url,
                file_path=Path(),
                duration=dur,
                resolution=(best.get("width", 0), best.get("height", 0)),
                provider=self.name,
                page_url=video.get("url", "") or "",
                tags=self._slug_tags(video.get("url", "") or ""),
            )

        logger.info("Pexels: no suitable video for query=%r [%.1f–%.1fs]", query, min_duration, max_duration)
        return None

    def search_page(
        self,
        query: str,
        min_duration: float,
        max_duration: float,
        resolution: tuple = (1920, 1080),
        page: int = 1,
        per_page: int = 20,
        orientation: str = "landscape",
    ) -> "SearchPage":
        """Search Pexels Videos for ALL matching clips on a page (paginated).

        Reads total_results from the API response and returns all candidates
        that match the duration window.

        Args:
            orientation: 'landscape' (default) or 'portrait' for vertical
                         video search (used for Shorts).

        Returns:
            SearchPage with all matching VideoAsset candidates and pagination metadata.
        """
        from pipeline.providers.base import SearchPage

        if not self._api_key:
            logger.error("Cannot search Pexels without an API key")
            return SearchPage(assets=[], page=page, per_page=per_page, total_available=0)

        params: dict = {
            "query": query,
            "per_page": min(per_page, 80),
            "page": page,
            "orientation": orientation,
            "size": "medium",
        }

        resp = self._request_with_retry(params)
        if resp is None:
            return SearchPage(assets=[], page=page, per_page=per_page, total_available=0)

        data = resp.json()
        total_results = data.get("total_results", 0)
        videos = data.get("videos", [])
        assets: list[VideoAsset] = []

        for video in videos:
            dur = video.get("duration", 0)
            if dur < min_duration or dur > max_duration:
                continue
            video_files = video.get("video_files", [])
            best = self._pick_best_quality(video_files, resolution)
            if not best:
                continue
            download_url = best.get("link", "") or video.get("url", "")
            assets.append(VideoAsset(
                url=download_url,
                file_path=Path(),
                duration=dur,
                resolution=(best.get("width", 0), best.get("height", 0)),
                provider=self.name,
                page_url=video.get("url", "") or "",
                tags=self._slug_tags(video.get("url", "") or ""),
            ))

        # total_results includes all pages; per_page * max_accessible_pages
        # could be capped. Use the API's total_results directly.
        return SearchPage(
            assets=assets, page=page, per_page=min(per_page, 80),
            total_available=total_results,
        )

    def download(self, asset: VideoAsset, output_dir: Path) -> Path:
        """Download the video clip from the Pexels CDN.

        Uses caching: if the file already exists in output_dir, the download
        is skipped.

        Args:
            asset: VideoAsset returned by search(). The url field must contain
                   the direct download link.
            output_dir: Directory where the file will be saved.

        Returns:
            Local filesystem path to the downloaded file.
        """
        output_dir.mkdir(parents=True, exist_ok=True)
        # Generate a stable filename from the asset URL
        import hashlib
        url_hash = hashlib.md5(asset.url.encode()).hexdigest()[:12]
        filename = f"pexels_{url_hash}.mp4"
        filepath = output_dir / filename

        if filepath.exists():
            logger.info("Pexels: video already cached at %s", filepath)
            asset.file_path = filepath
            return filepath

        try:
            resp = requests.get(asset.url, timeout=120, stream=True)
            resp.raise_for_status()
            filepath.write_bytes(resp.content)
            asset.file_path = filepath
            logger.info("Pexels: downloaded video to %s (%.1f MB)", filepath,
                         len(resp.content) / 1024 / 1024)
            return filepath
        except Exception as exc:
            logger.error("Pexels: download failed for %s: %s", asset.url, exc)
            raise

    # ── Internal helpers ─────────────────────────────────────

    def _request_with_retry(self, params: dict) -> Optional[requests.Response]:
        """Make a GET request to the Pexels API with one automatic 429 retry."""
        headers = {"Authorization": self._api_key}
        try:
            resp = requests.get(self.BASE_URL, params=params, headers=headers, timeout=15)
            if resp.status_code == 429:
                retry_after = resp.headers.get("Retry-After", "60")
                logger.warning("Pexels rate limit (429). Retry-After=%s", retry_after)
                time.sleep(min(int(retry_after), 60))
                resp = requests.get(self.BASE_URL, params=params, headers=headers, timeout=15)
            resp.raise_for_status()
            return resp
        except requests.RequestException as exc:
            logger.error("Pexels API request failed: %s", exc)
            return None

    @staticmethod
    def _slug_tags(page_url: str) -> list[str]:
        """Derive content tags from a Pexels video page URL slug.

        Pexels Videos does not expose tags or titles in the API, but the
        page URL embeds a descriptive slug:
        ``https://www.pexels.com/video/aerial-view-of-city-123456/`` →
        ``["aerial", "view", "city"]`` (id suffix and stop-ish short words
        are dropped).
        """
        if not page_url:
            return []
        m = re.search(r"/video/([^/?#]+)", page_url)
        if not m:
            return []
        slug = re.sub(r"-\d+$", "", m.group(1))
        words = [w for w in re.split(r"[-\s]+", slug.lower()) if w and len(w) >= 3]
        # dedupe preserving order
        seen = set()
        result = []
        for w in words:
            if w not in seen:
                seen.add(w)
                result.append(w)
        return result

    @staticmethod
    def _pick_best_quality(
        video_files: list[dict],
        preferred: tuple,
    ) -> Optional[dict]:
        """Select the best quality video file from the available options.

        Priority: exact match > same-or-higher resolution > highest available.
        """
        pw, ph = preferred
        best = None

        for vf in video_files:
            w = vf.get("width", 0)
            h = vf.get("height", 0)

            if w == pw and h == ph:
                return vf  # exact match

            if w >= pw and h >= ph:
                if best is None or w > best.get("width", 0):
                    best = vf

        if best:
            return best

        # Fallback: highest resolution available
        for vf in video_files:
            if best is None or vf.get("width", 0) > best.get("width", 0):
                best = vf

        return best
