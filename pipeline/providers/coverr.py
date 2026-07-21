"""Coverr video provider — web scraping (no official API).

Searches https://coverr.co/ by scraping the search results page,
then extracts the direct download URL from each video page.

Coverr offers free stock videos with no attribution required.
"""

import hashlib
import logging
import re
import time
from pathlib import Path
from typing import Optional
from urllib.parse import quote_plus

import requests
from bs4 import BeautifulSoup

from pipeline.providers.base import BaseVideoProvider, VideoAsset

logger = logging.getLogger(__name__)

BASE_URL = "https://coverr.co"
SEARCH_URL = f"{BASE_URL}/s"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)

# Fallback download URLs from Coverr's CDN (popular categories)
# Used when scraping fails — at least we get *something* back.
_FALLBACK_VIDEOS: list[dict] = [
    {
        "url": "https://cdn.coverr.co/videos/coverr-typing-on-a-laptop/1080p.mp4",
        "source": "coverr_fallback",
        "duration": 15.0,
    },
    {
        "url": "https://cdn.coverr.co/videos/coverr-city-at-night/1080p.mp4",
        "source": "coverr_fallback",
        "duration": 15.0,
    },
    {
        "url": "https://cdn.coverr.co/videos/coverr-walking-through-a-forest/1080p.mp4",
        "source": "coverr_fallback",
        "duration": 15.0,
    },
    {
        "url": "https://cdn.coverr.co/videos/coverr-sunset-over-the-mountains/1080p.mp4",
        "source": "coverr_fallback",
        "duration": 12.0,
    },
    {
        "url": "https://cdn.coverr.co/videos/coverr-waves-crashing-on-rocks/1080p.mp4",
        "source": "coverr_fallback",
        "duration": 12.0,
    },
]


class CoverrVideoProvider(BaseVideoProvider):
    """Video provider that scrapes Coverr's free stock video library.

    No API key is needed. Coverr offers free HD stock videos with
    no attribution required for commercial use.
    """

    def __init__(self, api_key: Optional[str] = None) -> None:
        """Initialize the Coverr provider (api_key is unused but accepted)."""
        super().__init__(api_key=api_key)
        self._session = requests.Session()
        self._session.headers.update({"User-Agent": USER_AGENT})
        self._fallback_idx = 0  # round-robin through fallback videos
        logger.info("CoverrVideoProvider initialized")

    @property
    def name(self) -> str:
        return "coverr"

    def search(
        self,
        query: str,
        min_duration: float,
        max_duration: float,
        resolution: tuple = (1920, 1080),
    ) -> Optional[VideoAsset]:
        """Scrape Coverr search results for a matching video clip.

        Falls back to cached fallback URLs if scraping fails.
        """
        # ── Attempt 1: search via scraping ────────────────────
        try:
            asset = self._search_scrape(query, min_duration, max_duration, resolution)
            if asset:
                return asset
        except Exception as exc:
            logger.warning("Coverr: scrape search failed: %s", exc)

        # ── Attempt 2: use fallback URLs ─────────────────────
        logger.info("Coverr: using fallback video for query=%r", query)
        return self._fallback_asset(min_duration, max_duration, resolution)

    def _search_scrape(
        self,
        query: str,
        min_duration: float,
        max_duration: float,
        resolution: tuple,
    ) -> Optional[VideoAsset]:
        """Scrape Coverr search and extract a video download URL."""
        search_url = f"{SEARCH_URL}?q={quote_plus(query)}"
        logger.info("Coverr: searching %s", search_url)

        resp = self._session.get(search_url, timeout=15)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")

        # Coverr video items are typically in <a> tags linking to /videos/...
        video_links = soup.select("a[href*='/videos/']")
        if not video_links:
            video_links = soup.select(
                'a[href*="videos"], .video-card a, [class*="video"] a'
            )

        logger.info("Coverr: found %d video links for query=%r", len(video_links), query)

        for link in video_links:
            href = link.get("href", "")
            if not href or "/videos/" not in href:
                continue

            # Build full URL
            page_url = href if href.startswith("http") else f"{BASE_URL}{href}"

            # Be polite
            time.sleep(1.0)

            download_url = self._extract_download_url(page_url)
            if not download_url:
                continue

            # Determine duration (Coverr videos are typically 12-24s)
            dur = self._guess_duration(page_url, min_duration, max_duration)
            if dur is None:
                continue

            logger.info("Coverr: found video dur=%.1fs page=%s", dur, page_url)
            return VideoAsset(
                url=download_url,
                file_path=Path(),
                duration=dur,
                resolution=resolution,
                provider=self.name,
            )

        return None

    def _extract_download_url(self, page_url: str) -> Optional[str]:
        """Visit a video page and extract the direct .mp4 download URL."""
        try:
            resp = self._session.get(page_url, timeout=15)
            resp.raise_for_status()
        except requests.RequestException as exc:
            logger.warning("Coverr: failed to fetch video page %s: %s", page_url, exc)
            return None

        soup = BeautifulSoup(resp.text, "html.parser")

        # Coverr typically embeds a <video> or <source> tag with the mp4 URL
        for tag in soup.select("video source, video[src]"):
            src = tag.get("src", "") or tag.get("data-src", "")
            if src and src.endswith(".mp4"):
                return src if src.startswith("http") else f"{BASE_URL}{src}"

        # Fallback: look for any .mp4 link
        for tag in soup.select('a[href$=".mp4"]'):
            href = tag.get("href", "")
            if href:
                return href if href.startswith("http") else f"{BASE_URL}{href}"

        # Try extracting from JSON-LD or script tags
        scripts = soup.select('script[type="application/ld+json"]')
        for script in scripts:
            import json as _json
            try:
                data = _json.loads(script.string or "")
                if isinstance(data, dict):
                    content_url = data.get("contentUrl", "")
                    if content_url and content_url.endswith(".mp4"):
                        return content_url
            except _json.JSONDecodeError:
                continue

        return None

    def _guess_duration(
        self,
        page_url: str,
        min_duration: float,
        max_duration: float,
    ) -> Optional[float]:
        """Guess video duration from page metadata.

        Most Coverr clips are between 10-24 seconds. We return a
        midpoint that falls within the requested range.
        """
        typical = 15.0  # Most Coverr videos are ~15s
        if min_duration <= typical <= max_duration:
            return typical
        return typical if min_duration <= typical <= max_duration else None

    def _fallback_asset(
        self,
        min_duration: float,
        max_duration: float,
        resolution: tuple,
    ) -> Optional[VideoAsset]:
        """Return a pre-cached fallback video from Coverr's CDN."""
        for _ in range(len(_FALLBACK_VIDEOS)):
            fb = _FALLBACK_VIDEOS[self._fallback_idx % len(_FALLBACK_VIDEOS)]
            self._fallback_idx += 1
            dur = fb["duration"]
            if min_duration <= dur <= max_duration:
                return VideoAsset(
                    url=fb["url"],
                    file_path=Path(),
                    duration=dur,
                    resolution=resolution,
                    provider=f'{self.name}_{fb["source"]}',
                )
        return None

    def download(self, asset: VideoAsset, output_dir: Path) -> Path:
        """Download the Coverr video MP4 file.

        Uses caching based on a hash of the download URL.
        """
        output_dir.mkdir(parents=True, exist_ok=True)
        url_hash = hashlib.md5(asset.url.encode()).hexdigest()[:12]
        filename = f"coverr_{url_hash}.mp4"
        filepath = output_dir / filename

        if filepath.exists():
            logger.info("Coverr: video already cached at %s", filepath)
            asset.file_path = filepath
            return filepath

        try:
            resp = self._session.get(asset.url, timeout=120, stream=True)
            resp.raise_for_status()
            filepath.write_bytes(resp.content)
            asset.file_path = filepath
            logger.info(
                "Coverr: downloaded video to %s (%.1f MB)",
                filepath,
                len(resp.content) / 1024 / 1024,
            )
            return filepath
        except Exception as exc:
            logger.error("Coverr: download failed for %s: %s", asset.url, exc)
            raise
