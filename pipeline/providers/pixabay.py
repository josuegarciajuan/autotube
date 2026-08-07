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
        page: int = 1,
        per_page: int = 20,
        orientation: str = "landscape",
    ) -> Optional[VideoAsset]:
        """Search Pixabay for the first clip matching all criteria.

        Args:
            query: Search keywords.
            min_duration: Minimum acceptable duration in seconds.
            max_duration: Maximum acceptable duration in seconds.
            resolution: Preferred resolution (width, height).
            page: Page number (1-indexed).
            per_page: Results per page (max 200).
            orientation: 'landscape' or 'portrait'.

        Returns:
            VideoAsset or None.
        """
        target_w, target_h = resolution
        min_w = 720 if orientation == "portrait" else target_w
        min_h = 1080 if orientation == "portrait" else target_h

        params: dict = {
            "key": self._api_key,
            "q": query[:100],  # Pixabay 100-char limit — safety net
            "per_page": min(per_page, 200),
            "page": page,
            "min_width": min_w,
            "min_height": min_h,
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
                file_path=Path(),
                duration=dur,
                resolution=(actual_w, actual_h),
                provider=self.name,
            )

        logger.info("Pixabay: no suitable video for query=%r [%.1f–%.1fs]", query, min_duration, max_duration)
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
        """Search Pixabay Videos for ALL matching clips on a page (paginated).

        Reads totalHits from the API response (capped at 500) and returns all
        candidates that match the duration window.

        Args:
            orientation: 'landscape' (default) or 'portrait' for vertical
                         video search (used for Shorts). When portrait,
                         resolution defaults to (1080, 1920) and min dimensions
                         are relaxed to 720x1080 to match typical stock footage.

        Returns:
            SearchPage with all matching VideoAsset candidates and pagination metadata.
        """
        from pipeline.providers.base import SearchPage

        target_w, target_h = resolution
        # For portrait orientation, use more relaxed minimum dimensions
        # since portrait stock footage is often ~720x1280, not 1080x1920
        min_w = 720 if orientation == "portrait" else target_w
        min_h = 1080 if orientation == "portrait" else target_h

        params: dict = {
            "key": self._api_key,
            "q": query[:100],
            "per_page": min(per_page, 200),
            "page": page,
            "min_width": min_w,
            "min_height": min_h,
        }

        resp = self._request_with_retry(params)
        if resp is None:
            return SearchPage(assets=[], page=page, per_page=min(per_page, 200), total_available=0)

        data = resp.json()
        total_hits = data.get("totalHits", 0)
        hits = data.get("hits", [])
        assets: list[VideoAsset] = []

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
            assets.append(VideoAsset(
                url=download_url,
                file_path=Path(),
                duration=dur,
                resolution=(best.get("width", 0), best.get("height", 0)),
                provider=self.name,
            ))

        return SearchPage(
            assets=assets, page=page, per_page=min(per_page, 200),
            total_available=total_hits,
        )

    def download(self, asset: VideoAsset, output_dir: Path) -> Path:
        """Download the video clip from Pixabay's CDN.

        Uses caching based on a hash of the download URL.
        Retries with exponential backoff on timeouts and server errors.
        """
        output_dir.mkdir(parents=True, exist_ok=True)
        url_hash = hashlib.md5(asset.url.encode()).hexdigest()[:12]
        filename = f"pixabay_{url_hash}.mp4"
        filepath = output_dir / filename

        if filepath.exists():
            logger.info("Pixabay: video already cached at %s", filepath)
            asset.file_path = filepath
            return filepath

        max_retries = 3
        last_exc = None

        for attempt in range(max_retries):
            try:
                resp = requests.get(asset.url, timeout=120, stream=True)
                resp.raise_for_status()
                filepath.write_bytes(resp.content)
                asset.file_path = filepath
                logger.info("Pixabay: downloaded video to %s (%.1f MB)", filepath,
                             len(resp.content) / 1024 / 1024)
                return filepath
            except requests.exceptions.Timeout:
                logger.warning(
                    "Pixabay video download timeout (attempt %d/%d): %s",
                    attempt + 1, max_retries, asset.url[:80],
                )
                last_exc = f"timeout after {max_retries} attempts"
            except requests.exceptions.ConnectionError as exc:
                logger.warning(
                    "Pixabay video download connection error (attempt %d/%d): %s",
                    attempt + 1, max_retries, exc,
                )
                last_exc = exc
            except requests.exceptions.HTTPError as exc:
                status = exc.response.status_code if exc.response is not None else 0
                if status >= 500:
                    logger.warning(
                        "Pixabay video download server error %d (attempt %d/%d)",
                        status, attempt + 1, max_retries,
                    )
                    last_exc = exc
                else:
                    logger.error("Pixabay: download failed for %s: %s", asset.url, exc)
                    raise
            except Exception as exc:
                logger.warning(
                    "Pixabay video download error (attempt %d/%d): %s",
                    attempt + 1, max_retries, exc,
                )
                last_exc = exc

            if attempt < max_retries - 1:
                sleep_time = 2 ** attempt  # 1, 2, 4
                time.sleep(sleep_time)

        logger.error("Pixabay: download failed after %d attempts for %s", max_retries, asset.url)
        raise RuntimeError(f"Pixabay download failed: {last_exc}")

    # ── Internal helpers ─────────────────────────────────────

    def _request_with_retry(self, params: dict) -> Optional[requests.Response]:
        """GET the Pixabay API with exponential-backoff retry.

        Retries on timeouts, connection errors, 5xx, and 429 (rate limit).
        Uses exponential backoff: 1s → 2s → 4s between attempts.
        Respects the Retry-After header for 429 responses.
        """
        timeout = int(os.getenv("PIXABAY_API_TIMEOUT", "30"))
        max_retries = 3

        for attempt in range(max_retries):
            try:
                resp = requests.get(self.BASE_URL, params=params, timeout=timeout)
                resp.raise_for_status()
                return resp
            except requests.exceptions.Timeout:
                logger.warning(
                    "Pixabay API timeout (attempt %d/%d, timeout=%ds)",
                    attempt + 1, max_retries, timeout,
                )
            except requests.exceptions.ConnectionError as exc:
                logger.warning(
                    "Pixabay API connection error (attempt %d/%d): %s",
                    attempt + 1, max_retries, exc,
                )
            except requests.exceptions.HTTPError as exc:
                status = exc.response.status_code if exc.response is not None else 0
                if status == 429:
                    retry_after = exc.response.headers.get("Retry-After", "60")
                    logger.warning(
                        "Pixabay rate limit (429). Retry-After=%s (attempt %d/%d)",
                        retry_after, attempt + 1, max_retries,
                    )
                    time.sleep(min(int(retry_after), 60))
                elif status >= 500:
                    logger.warning(
                        "Pixabay API server error %d (attempt %d/%d)",
                        status, attempt + 1, max_retries,
                    )
                else:
                    logger.error("Pixabay API HTTP error %d: %s", status, exc)
                    return None  # 4xx (non-429) — don't retry
            except requests.RequestException as exc:
                logger.warning(
                    "Pixabay API request failed (attempt %d/%d): %s",
                    attempt + 1, max_retries, exc,
                )

            # Exponential backoff: 1s, 2s, 4s (skip sleep on last attempt)
            if attempt < max_retries - 1:
                sleep_time = 2 ** attempt  # 1, 2, 4
                logger.debug("Retrying in %.0fs...", sleep_time)
                time.sleep(sleep_time)

        logger.error("Pixabay API request failed after %d attempts", max_retries)
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
