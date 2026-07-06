"""Mixkit video provider — web scraping (no official API).

Searches https://mixkit.co/free-stock-video/ by scraping the search results
page, then visits each video page to extract the download URL.

Be polite: adds delays between requests and uses a realistic User-Agent.
Caches downloaded videos to avoid re-scraping on repeated queries.
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

BASE_URL = "https://mixkit.co"
SEARCH_URL = f"{BASE_URL}/free-stock-video/"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)


class MixkitVideoProvider(BaseVideoProvider):
    """Video provider that scrapes Mixkit's free stock video library.

    No API key is needed. Mixkit offers free stock videos with no
    attribution required for commercial use.
    """

    def __init__(self, api_key: Optional[str] = None) -> None:
        """Initialize the Mixkit provider (api_key is unused but accepted)."""
        super().__init__(api_key=api_key)
        self._session = requests.Session()
        self._session.headers.update({"User-Agent": USER_AGENT})
        logger.info("MixkitVideoProvider initialized")

    @property
    def name(self) -> str:
        return "mixkit"

    def search(
        self,
        query: str,
        min_duration: float,
        max_duration: float,
        resolution: tuple = (1920, 1080),
    ) -> Optional[VideoAsset]:
        """Scrape Mixkit search results for a matching video clip.

        Only returns the first result that matches duration criteria.
        Videos on Mixkit are generally 1080p.

        Args:
            query: Search keywords.
            min_duration: Minimum acceptable duration in seconds.
            max_duration: Maximum acceptable duration in seconds.
            resolution: Preferred resolution (for future use; Mixkit is 1080p).

        Returns:
            VideoAsset or None.
        """
        search_url = f"{SEARCH_URL}?q={quote_plus(query)}"
        logger.info("Mixkit: searching %s", search_url)

        # ── Step 1: scrape search results page ──────────────
        try:
            resp = self._session.get(search_url, timeout=15)
            resp.raise_for_status()
        except requests.RequestException as exc:
            logger.error("Mixkit: search page request failed: %s", exc)
            return None

        soup = BeautifulSoup(resp.text, "html.parser")
        video_cards = self._parse_video_cards(soup)

        if not video_cards:
            logger.info("Mixkit: no video cards found for query=%r", query)
            return None

        logger.info("Mixkit: found %d video cards for query=%r", len(video_cards), query)

        # ── Step 2: check each card for duration match ──────
        for card in video_cards:
            dur = self._parse_duration(card.get("duration_text", ""))
            if dur is None:
                continue
            if dur < min_duration or dur > max_duration:
                continue

            page_url = card.get("url", "")
            if not page_url:
                continue

            # Be polite — pause between page visits
            time.sleep(1.5)

            # ── Step 3: visit the video page ───────────────
            download_url = self._extract_download_url(page_url)
            if not download_url:
                continue

            logger.info(
                "Mixkit: found video dur=%.1fs page=%s",
                dur, page_url,
            )
            return VideoAsset(
                url=download_url,
                file_path=Path(),  # placeholder
                duration=dur,
                resolution=resolution,  # Mixkit videos are typically 1080p
                provider=self.name,
            )

        logger.info("Mixkit: no video matching duration [%.1f–%.1fs] for query=%r",
                     min_duration, max_duration, query)
        return None

    def download(self, asset: VideoAsset, output_dir: Path) -> Path:
        """Download the Mixkit video MP4 file.

        Uses caching based on a hash of the download URL.
        """
        output_dir.mkdir(parents=True, exist_ok=True)
        url_hash = hashlib.md5(asset.url.encode()).hexdigest()[:12]
        filename = f"mixkit_{url_hash}.mp4"
        filepath = output_dir / filename

        if filepath.exists():
            logger.info("Mixkit: video already cached at %s", filepath)
            asset.file_path = filepath
            return filepath

        try:
            resp = self._session.get(asset.url, timeout=120, stream=True)
            resp.raise_for_status()
            filepath.write_bytes(resp.content)
            asset.file_path = filepath
            logger.info("Mixkit: downloaded video to %s (%.1f MB)", filepath,
                         len(resp.content) / 1024 / 1024)
            return filepath
        except Exception as exc:
            logger.error("Mixkit: download failed for %s: %s", asset.url, exc)
            raise

    # ── Internal helpers ─────────────────────────────────────

    @staticmethod
    def _parse_video_cards(soup: BeautifulSoup) -> list[dict]:
        """Extract video card data from the search results page.

        Returns list of dicts with keys: url, title, duration_text.
        """
        cards = []
        # Mixkit video cards are typically <div class="item-grid ..."> or similar
        for card in soup.select("div.item-grid-card, div.video-card, article.video-item"):
            link = card.select_one("a[href]")
            if not link:
                link = card.find("a", href=True)
            if not link:
                continue

            href = link.get("href", "")
            if not href.startswith("http"):
                href = BASE_URL + href

            title_elem = card.select_one("h3, .title, .item-title")
            title = title_elem.get_text(strip=True) if title_elem else ""

            duration_elem = card.select_one(".duration, .video-duration, time")
            duration_text = duration_elem.get_text(strip=True) if duration_elem else ""

            cards.append({
                "url": href,
                "title": title,
                "duration_text": duration_text,
            })

        # Fallback: try a more generic selector if nothing found
        if not cards:
            for a_tag in soup.select("a[href*='/free-stock-video/']"):
                href = a_tag.get("href", "")
                if "/free-stock-video/" not in href:
                    continue
                # Avoid non-video links (categories, etc.)
                if href.endswith("/free-stock-video/") or href.endswith("/free-stock-video"):
                    continue
                if not href.startswith("http"):
                    href = BASE_URL + href

                parent = a_tag.find_parent(["div", "article", "li"])
                duration_text = ""
                if parent:
                    dur_elem = parent.select_one(".duration, .video-duration, time, [class*='duration']")
                    duration_text = dur_elem.get_text(strip=True) if dur_elem else ""

                cards.append({
                    "url": href,
                    "title": a_tag.get_text(strip=True),
                    "duration_text": duration_text,
                })

        return cards

    @staticmethod
    def _parse_duration(text: str) -> Optional[float]:
        """Parse a duration string like '0:15', '1:23', '2:05' into seconds.

        Returns:
            Duration in seconds, or None if parsing fails.
        """
        if not text:
            return None
        # Remove surrounding whitespace and optional labels
        text = text.strip()
        # Common format: M:SS or MM:SS
        match = re.match(r"(\d+):(\d{2})", text)
        if match:
            minutes = int(match.group(1))
            seconds = int(match.group(2))
            return minutes * 60 + seconds
        # Alternative: plain seconds as a number
        try:
            return float(text)
        except ValueError:
            pass
        return None

    def _extract_download_url(self, page_url: str) -> Optional[str]:
        """Visit the video page and extract the direct MP4 download URL."""
        try:
            resp = self._session.get(page_url, timeout=15)
            resp.raise_for_status()
        except requests.RequestException as exc:
            logger.warning("Mixkit: failed to load video page %s: %s", page_url, exc)
            return None

        soup = BeautifulSoup(resp.text, "html.parser")

        # Try common patterns for Mixkit download links
        # Pattern 1: <video> tag with a source
        video_tag = soup.find("video")
        if video_tag:
            source = video_tag.find("source", src=True)
            if source:
                return source["src"]

        # Pattern 2: <a> tag pointing to an .mp4
        for a_tag in soup.find_all("a", href=True):
            href = a_tag["href"]
            if href.endswith(".mp4"):
                return href

        # Pattern 3: data attributes on a download button
        download_btn = soup.select_one("[data-download-url], [data-video-url], .download-button")
        if download_btn:
            for attr in ("data-download-url", "data-video-url", "href"):
                url = download_btn.get(attr, "")
                if url and (url.endswith(".mp4") or "video" in url):
                    return url

        # Pattern 4: JavaScript variable containing .mp4 URL
        scripts = soup.find_all("script")
        for script in scripts:
            if script.string:
                match = re.search(r'["\'](https?://[^"\']+\.mp4)["\']', script.string)
                if match:
                    return match.group(1)

        logger.warning("Mixkit: could not extract download URL from %s", page_url)
        return None
