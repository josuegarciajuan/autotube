"""Pixabay Video API provider — free stock footage.

Endpoint: https://pixabay.com/api/videos/
Auth: ?key={api_key} query parameter.
Rate limit: 100 req/h unauthenticated, higher with registered key.
Handles 429 with Retry-After header.
"""

import hashlib
import logging
import os
import time
from pathlib import Path
from typing import Optional

import requests

from pipeline.providers.base import BaseVideoProvider, VideoAsset

logger = logging.getLogger(__name__)


class PixabayVideoProvider(BaseVideoProvider):
    """Video provider backed by the Pixabay Video API.

    Free tier: 100 requests/hour without a registered key, higher with one.
    """

    BASE_URL = "https://pixabay.com/api/videos/"

    def __init__(self, api_key: Optional[str] = None) -> None:
        """Initialize with an optional API key.

        Falls back to the PIXABAY_API_KEY environment variable.
        """
        super().__init__(api_key=api_key)
        resolved_key = api_key or os.getenv("PIXABAY_API_KEY", "")
        if not resolved_key:
            logger.warning(
                "PIXABAY_API_KEY not set — PixabayVideoProvider will use "
                "unauthenticated access (lower rate limit)"
            )
        self._api_key = resolved_key

    @property
    def name(self) -> str:
        return "pixabay"

    def search(
        self,
        query: str,
        min_duration: float,
        max_duration: float,
        resolution: tuple = (1920, 1080),
    ) -> Optional[VideoAsset]:
        """Search Pixabay for the first clip matching all criteria.

        Args:
            query: Search keywords.
            min_duration: Minimum acceptable duration in seconds.
            max_duration: Maximum acceptable duration in seconds.
            resolution: Preferred resolution (width, height).

        Returns:
            VideoAsset or None.
        """
        target_w, target_h = resolution
        params: dict = {
            "key": self._api_key,
            "q": query[:100],  # Pixabay 100-char limit — safety net
            "per_page": 20,
            "min_width": target_w,
            "min_height": target_h,
        }

        resp = self._request_with_retry(params)
        if resp is None:
            return None

        data = resp.json()
        hits = data.get("hits", [])

        for hit in hits:
            dur = hit.get("duration", 0)
            if dur < min_duration or dur > max_duration:
                continue

            videos = hit.get("videos", {})
            best = self._pick_best_quality(videos, resolution)
            if not best:
                continue

            download_url = best.get("url", "")
            if not download_url:
                continue

            video_id = str(hit.get("id", ""))
            actual_w = best.get("width", 0)
            actual_h = best.get("height", 0)
            logger.info(
                "Pixabay: found video id=%s dur=%.1fs res=%dx%d for query=%r",
                video_id, dur, actual_w, actual_h, query,
            )
            return VideoAsset(
                url=download_url,
                file_path=Path(),  # placeholder
                duration=dur,
                resolution=(actual_w, actual_h),
                provider=self.name,
            )

        logger.info("Pixabay: no suitable video for query=%r [%.1f–%.1fs]", query, min_duration, max_duration)
        return None

    def download(self, asset: VideoAsset, output_dir: Path) -> Path:
        """Download the video clip from Pixabay's CDN.

        Uses caching based on a hash of the download URL.
        """
        output_dir.mkdir(parents=True, exist_ok=True)
        url_hash = hashlib.md5(asset.url.encode()).hexdigest()[:12]
        filename = f"pixabay_{url_hash}.mp4"
        filepath = output_dir / filename

        if filepath.exists():
            logger.info("Pixabay: video already cached at %s", filepath)
            asset.file_path = filepath
            return filepath

        try:
            resp = requests.get(asset.url, timeout=120, stream=True)
            resp.raise_for_status()
            filepath.write_bytes(resp.content)
            asset.file_path = filepath
            logger.info("Pixabay: downloaded video to %s (%.1f MB)", filepath,
                         len(resp.content) / 1024 / 1024)
            return filepath
        except Exception as exc:
            logger.error("Pixabay: download failed for %s: %s", asset.url, exc)
            raise

    # ── Internal helpers ─────────────────────────────────────

    def _request_with_retry(self, params: dict) -> Optional[requests.Response]:
        """GET the Pixabay API with one automatic 429 retry."""
        try:
            resp = requests.get(self.BASE_URL, params=params, timeout=15)
            if resp.status_code == 429:
                retry_after = resp.headers.get("Retry-After", "60")
                logger.warning("Pixabay rate limit (429). Retry-After=%s", retry_after)
                time.sleep(min(int(retry_after), 60))
                resp = requests.get(self.BASE_URL, params=params, timeout=15)
            resp.raise_for_status()
            return resp
        except requests.RequestException as exc:
            logger.error("Pixabay API request failed: %s", exc)
            return None

    @staticmethod
    def _pick_best_quality(
        videos: dict,
        preferred: tuple,
    ) -> Optional[dict]:
        """Select the best quality from Pixabay's nested videos dict.

        The API returns sizes keyed as 'large', 'medium', 'small', 'tiny'.
        Priority: exact match > same-or-higher > largest available.
        """
        pw, ph = preferred
        best = None
        best_area = 0

        for label, file_info in videos.items():
            w = file_info.get("width", 0)
            h = file_info.get("height", 0)
            if not w or not h:
                continue

            if w == pw and h == ph:
                return file_info

            if w >= pw and h >= ph:
                area = w * h
                if best is None or area < best_area:
                    best = file_info
                    best_area = area

        if best:
            return best

        # Absolute fallback: pick the largest
        for label, file_info in videos.items():
            area = file_info.get("width", 0) * file_info.get("height", 0)
            if area > best_area:
                best = file_info
                best_area = area

        return best
