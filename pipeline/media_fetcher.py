"""Hybrid media fetcher: video (Pexels) with fallback chain to images (Unsplash/Pexels).

Replaces the old ImageFetcher. Fetches ONE media asset per block, preferring
video where configured, falling back through a chain of providers.
"""

import hashlib
import logging
import time
from pathlib import Path

import requests

from config import canal1_config as _default_cfg
from config import settings
from pipeline.image_fetcher import UnsplashProvider, PexelsProvider

logger = logging.getLogger(__name__)

# New output dir for video clips
VIDEO_CLIPS_DIR = settings.OUTPUT_DIR / "video_clips"
VIDEO_CLIPS_DIR.mkdir(parents=True, exist_ok=True)


class PexelsVideoProvider:
    """Pexels Videos API — free stock video clips.

    Endpoint: https://api.pexels.com/videos/search
    Auth: same API key as Pexels Photos.
    Rate limit: 200 req/h.
    """

    BASE_URL = "https://api.pexels.com/videos/search"

    def __init__(self, api_key: str) -> None:
        if not api_key:
            raise ValueError("Pexels API key is required")
        self._session = requests.Session()
        self._session.headers["Authorization"] = api_key
        logger.info("PexelsVideoProvider initialized")

    def search(
        self,
        query: str,
        n: int = 1,
        min_duration: int = 4,
        max_duration: int = 20,
    ) -> list[dict]:
        """Search Pexels Videos API.

        Returns list of dicts with: id, url, download_url, duration, width, height.
        """
        if n < 1:
            return []

        params: dict = {
            "query": query,
            "per_page": min(n, 80),
            "orientation": "landscape",
            "size": "medium",  # medium = 1080p, good balance
        }

        try:
            resp = self._session.get(self.BASE_URL, params=params, timeout=15)
            if resp.status_code == 429:
                retry_after = resp.headers.get("Retry-After", "60")
                logger.warning("Pexels Videos rate limit (429). Retry-After=%s", retry_after)
                time.sleep(min(int(retry_after), 60))
                resp = self._session.get(self.BASE_URL, params=params, timeout=15)
            resp.raise_for_status()
        except requests.RequestException as exc:
            logger.error("Pexels Videos search failed: %s", exc)
            return []

        data = resp.json()
        results: list[dict] = []
        for video in data.get("videos", []):
            dur = video.get("duration", 0)
            if dur < min_duration or dur > max_duration:
                continue

            # Get best quality video file
            video_files = video.get("video_files", [])
            # Prefer: 1920x1080 → 1280x720 → 960x540
            best = None
            for vf in video_files:
                w = vf.get("width", 0)
                h = vf.get("height", 0)
                if w == 1920 and h == 1080:
                    best = vf
                    break
                if w == 1280 and h == 720 and best is None:
                    best = vf
                if best is None:
                    best = vf

            if best:
                results.append({
                    "id": str(video.get("id", "")),
                    "url": video.get("url", ""),
                    "download_url": best.get("link", ""),
                    "duration": dur,
                    "width": best.get("width", 0),
                    "height": best.get("height", 0),
                    "photographer": video.get("user", {}).get("name", "Unknown"),
                })

            if len(results) >= n:
                break

        logger.info("Pexels Videos returned %d results for query=%r", len(results), query)
        return results


class MediaFetcher:
    """Orchestrates hybrid media fetching with fallback chain.

    Chain per block (when media_tipo == "video"):
      1. Pexels Videos API → download .mp4
      2. Unsplash Photos API → download .jpg
      3. Pexels Photos API → download .jpg
      4. Retry with simplified query (first 3 keywords only)
      5. Type-specific pre-baked fallback query
      6. Generic fallback query
      7. Return placeholder info

    Only ONE asset is downloaded per block — no waste.
    """

    # Pre-baked fallback queries by block type (guaranteed to find results)
    _FALLBACK_BY_TYPE: dict[str, str] = {
        "hook": "dark dramatic mystery atmosphere cinematic",
        "desarrollo": "empty institutional building documentary style",
        "climax": "dark shadow tension dramatic lighting",
        "reflexion": "contemplative silence empty space window light",
        "cierre": "light hope dawn breaking darkness",
    }

    def __init__(self, config=None) -> None:
        self._config = config or _default_cfg
        self._media_strategy = getattr(self._config, "MEDIA_STRATEGY", {})

        # Video provider
        self._video_provider: PexelsVideoProvider | None = None
        if settings.PEXELS_API_KEY:
            self._video_provider = PexelsVideoProvider(settings.PEXELS_API_KEY)
        else:
            logger.warning("PEXELS_API_KEY not set — video fetching disabled")

        # Image providers (reuse from image_fetcher)
        self._unsplash: UnsplashProvider | None = None
        self._pexels: PexelsProvider | None = None

        if settings.UNSPLASH_ACCESS_KEY:
            self._unsplash = UnsplashProvider(settings.UNSPLASH_ACCESS_KEY)
        else:
            logger.warning("UNSPLASH_ACCESS_KEY not set — Unsplash disabled")

        if settings.PEXELS_API_KEY:
            self._pexels = PexelsProvider(settings.PEXELS_API_KEY)
        else:
            logger.warning("PEXELS_API_KEY not set — Pexels photos disabled")

        if self._unsplash is None and self._pexels is None and self._video_provider is None:
            logger.error("No media providers configured! Set UNSPLASH_ACCESS_KEY or PEXELS_API_KEY")

    def fetch_for_block(self, block: dict) -> dict:
        """Fetch ONE media asset for a block. Returns asset info dict."""
        query = block.get("search_query_en", "")
        media_tipo = block.get("media_tipo", "imagen")
        target_dur = block.get("media_duracion", 5)
        block_tipo = block.get("tipo", "desarrollo")

        prefer_video = self._media_strategy.get("prefer_video", True) and media_tipo == "video"

        logger.info("Fetching media for block: tipo=%s query=%r dur=%ds",
                     media_tipo, query[:80], target_dur)

        # ── Step 1: Try video (if preferred) ──────────────
        if prefer_video and self._video_provider:
            result = self._try_video(query, target_dur)
            if result:
                return result

        # ── Step 2: Try Unsplash image ────────────────────
        result = self._try_image_unsplash(query)
        if result:
            return result

        # ── Step 3: Try Pexels image ──────────────────────
        result = self._try_image_pexels(query)
        if result:
            return result

        # ── Step 4: Retry with simplified query ───────────
        simple_query = self._simplify_query(query)
        if simple_query != query:
            logger.info("Retrying with simplified query: %r", simple_query)
            result = self._try_image_unsplash(simple_query)
            if result:
                return result
            result = self._try_image_pexels(simple_query)
            if result:
                return result

        # ── Step 5: Type-specific fallback ────────────────
        type_query = self._FALLBACK_BY_TYPE.get(block_tipo)
        if type_query:
            logger.info("Retrying with type-specific fallback [%s]: %r", block_tipo, type_query)
            result = self._try_image_unsplash(type_query)
            if result:
                return result
            result = self._try_image_pexels(type_query)
            if result:
                return result

        # ── Step 6: Generic fallback ──────────────────────
        fallback = self._media_strategy.get("fallback_query", "dark cinematic atmosphere 16:9")
        logger.info("Retrying with generic fallback: %r", fallback)
        result = self._try_image_unsplash(fallback)
        if result:
            return result
        result = self._try_image_pexels(fallback)
        if result:
            return result

        # ── Step 7: Placeholder ───────────────────────────
        logger.warning("All providers exhausted for block [%s] — using placeholder", block_tipo)
        return {
            "path": None,
            "type": "placeholder",
            "duration": None,
            "source": "placeholder",
        }

    def fetch_for_script(self, bloques: list[dict]) -> list[dict]:
        """Fetch media for every block in a script.

        Args:
            bloques: List of block dicts from LLM output.

        Returns:
            List of asset info dicts (same order as bloques).
        """
        results: list[dict] = []
        for i, bloque in enumerate(bloques):
            logger.info("Fetching media for block %d/%d", i + 1, len(bloques))
            asset = self.fetch_for_block(bloque)
            results.append(asset)
            if i < len(bloques) - 1:
                time.sleep(0.5)  # polite rate limiting
        return results

    # ── Internal: video ───────────────────────────────────

    def _try_video(self, query: str, target_dur: int) -> dict | None:
        """Try to fetch a video from Pexels Videos."""
        if not self._video_provider:
            return None

        try:
            min_dur = self._media_strategy.get("video_min_duration", 4)
            max_dur = self._media_strategy.get("video_max_duration", 20)
            results = self._video_provider.search(
                query, n=1, min_duration=min_dur, max_duration=max_dur
            )
            if results:
                video = results[0]
                path = self._download_video(video["download_url"], f"pexels_video_{video['id']}.mp4")
                if path:
                    return {
                        "path": path,
                        "type": "video",
                        "duration": video["duration"],
                        "source": "pexels_video",
                    }
        except Exception as exc:
            logger.warning("Video fetch failed: %s", exc)

        return None

    def _download_video(self, url: str, filename: str) -> Path | None:
        """Download a video clip to VIDEO_CLIPS_DIR. Cached."""
        filepath = VIDEO_CLIPS_DIR / filename
        if filepath.exists():
            logger.info("Video already cached: %s", filepath)
            return filepath

        try:
            resp = requests.get(url, timeout=60, stream=True)
            resp.raise_for_status()
            filepath.write_bytes(resp.content)
            logger.info("Downloaded video: %s (%.1f MB)",
                         filepath, len(resp.content) / 1024 / 1024)
            return filepath
        except Exception as exc:
            logger.error("Video download failed %s: %s", url, exc)
            return None

    # ── Internal: images ──────────────────────────────────

    def _try_image_unsplash(self, query: str) -> dict | None:
        """Try Unsplash for an image."""
        if not self._unsplash:
            return None
        return self._fetch_image(self._unsplash, query, "unsplash")

    def _try_image_pexels(self, query: str) -> dict | None:
        """Try Pexels for an image."""
        if not self._pexels:
            return None
        return self._fetch_image(self._pexels, query, "pexels_photo")

    def _fetch_image(self, provider, query: str, source: str) -> dict | None:
        """Fetch one image from a provider."""
        try:
            results = provider.search(query, n=1)
            if not results:
                return None

            img = results[0]
            download_url = img.get("download_url", "")
            if not download_url:
                logger.warning("No download_url for %s image id=%s", source, img.get("id"))
                return None

            img_id = str(img.get("id", hashlib.md5(download_url.encode()).hexdigest()[:12]))
            path = self._download_image(download_url, f"{source}_{img_id}.jpg")
            if path:
                return {
                    "path": path,
                    "type": "image",
                    "duration": None,
                    "source": source,
                }
        except Exception as exc:
            logger.warning("%s image fetch failed: %s", source, exc)

        return None

    def _download_image(self, url: str, filename: str) -> Path | None:
        """Download image to IMAGES_DIR. Cached."""
        filepath = settings.IMAGES_DIR / filename
        if filepath.exists():
            logger.info("Image already cached: %s", filepath)
            return filepath

        try:
            resp = requests.get(url, timeout=30, stream=True)
            resp.raise_for_status()
            settings.IMAGES_DIR.mkdir(parents=True, exist_ok=True)
            filepath.write_bytes(resp.content)
            logger.info("Downloaded image: %s (%d bytes)", filepath, len(resp.content))
            return filepath
        except Exception as exc:
            logger.error("Image download failed %s: %s", url, exc)
            return None

    # ── Internal: query simplification ────────────────────

    @staticmethod
    def _simplify_query(query: str) -> str:
        """Take first 3-4 keywords from a query (skip style modifiers)."""
        words = query.split()
        # Skip known style words
        style_words = {
            "cinematic", "photography", "dramatic", "lighting", "atmospheric",
            "16:9", "moody", "high", "contrast", "professional", "dark",
            "atmosphere", "slow", "motion", "tracking", "shot", "aerial",
            "overhead", "style", "film", "video", "stock",
        }
        keywords = [w for w in words if w.lower() not in style_words]
        return " ".join(keywords[:4])
