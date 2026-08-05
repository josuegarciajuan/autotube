"""YouTube Creative Commons video provider using yt-dlp.

Searches YouTube for videos licensed under Creative Commons that match
duration and resolution criteria. Uses yt-dlp (yt-dlp) for search and download.

This provider should be run LAST (lowest priority) because it is the
slowest: yt-dlp must invoke the YouTube search and download infrastructure.
"""

import hashlib
import json
import logging
import time
from pathlib import Path
from typing import Optional

from pipeline.providers.base import BaseVideoProvider, VideoAsset

logger = logging.getLogger(__name__)

# Cache file to avoid repeated yt-dlp searches
_CACHE_DIR = Path(__file__).resolve().parent.parent.parent / "output" / ".yt_cc_cache"
_CACHE_DIR.mkdir(parents=True, exist_ok=True)


class YouTubeCCProvider(BaseVideoProvider):
    """Video provider that searches YouTube for Creative Commons videos.

    Uses yt-dlp for search and download. No API key is needed, but the
    provider is subject to YouTube's rate limiting and scraping restrictions.

    Priority: LOW — should be used as a last resort.
    """

    def __init__(self, api_key: Optional[str] = None) -> None:
        """Initialize the YouTube CC provider (api_key is unused)."""
        super().__init__(api_key=api_key)
        logger.info("YouTubeCCProvider initialized")

    @property
    def name(self) -> str:
        return "youtube_cc"

    def search(
        self,
        query: str,
        min_duration: float,
        max_duration: float,
        resolution: tuple = (1920, 1080),
        liberal_license: bool = False,
    ) -> Optional[VideoAsset]:
        """Search YouTube for Creative Commons videos matching criteria.

        Searches with: "{query} creative commons" by default. When
        *liberal_license* is True, the CC filter is relaxed — any
        publicly available video matching duration/resolution is
        accepted.  Use only as a last resort.

        Extracts up to 5 results and tries each one until a suitable
        match is found.

        Results are cached to the local filesystem to avoid repeated
        yt-dlp invocations.

        Args:
            query: Search keywords (appended with "creative commons" unless liberal).
            min_duration: Minimum acceptable duration in seconds.
            max_duration: Maximum acceptable duration in seconds.
            resolution: Preferred resolution (max height constraint).
            liberal_license: If True, skip the CC license check.

        Returns:
            VideoAsset or None.
        """
        # Check cache first
        cache_key = self._cache_key(query, min_duration, max_duration, resolution, liberal_license)
        cached = self._load_cache(cache_key)
        if cached is not None:
            logger.info("YouTubeCC: using cached result for query=%r", query)
            return cached

        import yt_dlp

        search_query = query if liberal_license else f"{query} creative commons"
        max_height = resolution[1]

        ydl_opts = {
            "quiet": True,
            "no_warnings": True,
            "extract_flat": False,
            "format": f"bestvideo[height<={max_height}]+bestaudio/best[height<={max_height}]",
            "max_filesize": 50 * 1024 * 1024,  # 50 MB
            "noplaylist": True,
            "playlistend": 5,
            "ignoreerrors": True,
            "geo_bypass": True,
        }

        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                # yt-dlp search: use ytsearchN: prefix
                info = ydl.extract_info(f"ytsearch5:{search_query}", download=False)
        except Exception as exc:
            logger.error("YouTubeCC: yt-dlp search failed: %s", exc)
            return None

        entries = info.get("entries", []) if info else []
        if not entries:
            logger.info("YouTubeCC: no results for query=%r", search_query)
            self._save_cache(cache_key, None)
            return None

        logger.info("YouTubeCC: yt-dlp returned %d entries for query=%r", len(entries), search_query)

        for entry in entries:
            if entry is None:
                continue

            dur = entry.get("duration") or 0
            if dur < min_duration or dur > max_duration:
                logger.debug("YouTubeCC: skipping %s dur=%.1fs (need %.1f–%.1f)",
                             entry.get("id", "?"), dur, min_duration, max_duration)
                continue

            # Check license (skip when liberal_license mode)
            if not liberal_license:
                license_str = entry.get("license", "")
                if not self._is_creative_commons(license_str, entry):
                    logger.debug("YouTubeCC: skipping %s — not CC licensed", entry.get("id", "?"))
                    continue

            # Check resolution
            actual_height = entry.get("height") or 0
            actual_width = entry.get("width") or 0
            if not actual_height or not actual_width:
                # Try to get from formats
                formats = entry.get("formats") or []
                for fmt in formats:
                    h = fmt.get("height") or 0
                    w = fmt.get("width") or 0
                    if h and w:
                        actual_height = max(actual_height, h)
                        actual_width = max(actual_width, w)

            web_url = entry.get("webpage_url", entry.get("url", ""))

            if not web_url:
                continue

            asset = VideoAsset(
                url=web_url,
                file_path=Path(),  # placeholder
                duration=dur,
                resolution=(actual_width, actual_height),
                provider=self.name,
            )
            # Store metadata needed for download
            asset._yt_entry = entry  # type: ignore[attr-defined]

            logger.info(
                "YouTubeCC: found video dur=%.1fs res=%dx%d url=%s",
                dur, actual_width, actual_height, web_url,
            )

            self._save_cache(cache_key, asset)
            return asset

        logger.info("YouTubeCC: no CC video matching duration [%.1f–%.1fs] for query=%r",
                     min_duration, max_duration, query)
        self._save_cache(cache_key, None)
        return None

    def search_page(
        self,
        query: str,
        min_duration: float,
        max_duration: float,
        resolution: tuple = (1920, 1080),
        page: int = 1,
        per_page: int = 20,
        liberal_license: bool = False,
    ) -> 'SearchPage':
        """Return ALL matching YouTube CC videos (not just first match).

        Overrides the base class default (1 asset per page) by collecting
        every matching result from yt-dlp and paginating in memory.  This
        gives the exhaustive search in ``_fetch_asset_exhaustive`` more
        candidates to dedup against.
        """
        from pipeline.providers.base import SearchPage
        import yt_dlp

        search_query = query if liberal_license else f"{query} creative commons"
        max_height = resolution[1]

        ydl_opts = {
            "quiet": True,
            "no_warnings": True,
            "extract_flat": True,
            "noplaylist": True,
            "playlistend": 50,
            "ignoreerrors": True,
            "geo_bypass": True,
        }

        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(f"ytsearch50:{search_query}", download=False)
        except Exception as exc:
            logger.error("YouTubeCC: search_page failed: %s", exc)
            return SearchPage(assets=[], page=page, per_page=per_page, total_available=0)

        entries = info.get("entries", []) if info else []
        assets: list[VideoAsset] = []

        for entry in entries:
            if entry is None:
                continue

            dur = entry.get("duration") or 0
            if dur < min_duration or dur > max_duration:
                continue

            # Check license (skip when liberal_license mode)
            if not liberal_license:
                license_str = entry.get("license", "")
                if not self._is_creative_commons(license_str, entry):
                    continue

            # Check resolution
            actual_height = entry.get("height") or 0
            actual_width = entry.get("width") or 0
            if not actual_height or not actual_width:
                formats = entry.get("formats") or []
                for fmt in formats:
                    h = fmt.get("height") or 0
                    w = fmt.get("width") or 0
                    if h and w:
                        actual_height = max(actual_height, h)
                        actual_width = max(actual_width, w)

            if actual_height > max_height:
                continue

            web_url = entry.get("webpage_url", "")
            if not web_url:
                continue

            asset = VideoAsset(
                url=web_url,
                file_path=Path(),
                duration=dur,
                resolution=(actual_width, actual_height),
                provider=self.name,
            )
            asset._yt_entry = entry  # type: ignore[attr-defined]
            assets.append(asset)

        # Paginate from in-memory list
        start = (page - 1) * per_page
        end = start + per_page
        page_assets = assets[start:end]

        logger.info(
            "YouTubeCC: search_page query=%r → %d total, %d on page %d",
            query, len(assets), len(page_assets), page,
        )
        return SearchPage(
            assets=page_assets,
            page=page,
            per_page=per_page,
            total_available=len(assets),
        )

    def download(self, asset: VideoAsset, output_dir: Path) -> Path:
        """Download a YouTube Creative Commons video using yt-dlp.

        Uses caching: if the file already exists in output_dir, the download
        is skipped. For yt-dlp downloads, the filename is derived from the
        video ID extracted from the URL.
        """
        import yt_dlp

        output_dir.mkdir(parents=True, exist_ok=True)

        # Extract video ID from URL
        video_id = self._extract_video_id(asset.url)
        if not video_id:
            video_id = hashlib.md5(asset.url.encode()).hexdigest()[:12]

        # Check if already downloaded (yt-dlp adds extensions)
        existing = list(output_dir.glob(f"*{video_id}*"))
        if existing:
            filepath = existing[0]
            logger.info("YouTubeCC: video already cached at %s", filepath)
            asset.file_path = filepath
            return filepath

        max_height = asset.resolution[1] if asset.resolution[1] else 1080

        ydl_opts = {
            "quiet": True,
            "no_warnings": True,
            "format": f"bestvideo[height<={max_height}]+bestaudio/best[height<={max_height}]",
            "max_filesize": 50 * 1024 * 1024,
            "outtmpl": str(output_dir / f"%(id)s.%(ext)s"),
            "noplaylist": True,
            "ignoreerrors": True,
            "geo_bypass": True,
        }

        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(asset.url, download=True)
                if info is None:
                    raise RuntimeError(f"yt-dlp returned no info for {asset.url}")

                filename = ydl.prepare_filename(info)
                filepath = Path(filename)

                if filepath.exists():
                    asset.file_path = filepath
                    logger.info("YouTubeCC: downloaded video to %s", filepath)
                    return filepath

        except Exception as exc:
            logger.error("YouTubeCC: download failed for %s: %s", asset.url, exc)
            raise

        # Fallback: find the downloaded file
        downloaded = list(output_dir.glob(f"*{video_id}*"))
        if downloaded:
            filepath = downloaded[0]
            asset.file_path = filepath
            return filepath

        raise FileNotFoundError(f"YouTubeCC: download completed but file not found for {asset.url}")

    # ── Internal helpers ─────────────────────────────────────

    @staticmethod
    def _is_creative_commons(license_str: str, entry: dict) -> bool:
        """Check if a YouTube video is Creative Commons licensed.

        YouTube API returns 'creativeCommon' for CC-licensed videos. The
        yt-dlp entry may have 'license' field or it may be in other fields.
        """
        # Direct check
        if license_str and "creative" in license_str.lower():
            return True

        # Check in tags, categories, description
        tags = entry.get("tags") or []
        for tag in tags:
            if "creative commons" in str(tag).lower():
                return True

        # Check description
        desc = entry.get("description") or ""
        if "creative commons" in desc.lower():
            return True

        return False

    @staticmethod
    def _extract_video_id(url: str) -> Optional[str]:
        """Extract YouTube video ID from a URL."""
        import re
        patterns = [
            r"(?:v=|/v/|youtu\.be/)([a-zA-Z0-9_-]{11})",
            r"youtube\.com/embed/([a-zA-Z0-9_-]{11})",
        ]
        for pat in patterns:
            match = re.search(pat, url)
            if match:
                return match.group(1)
        return None

    # ── Cache helpers ────────────────────────────────────────

    @staticmethod
    def _cache_key(
        query: str,
        min_dur: float,
        max_dur: float,
        resolution: tuple,
        liberal_license: bool = False,
    ) -> str:
        """Generate a deterministic cache key for a search query."""
        raw = f"{query}|{min_dur}|{max_dur}|{resolution[0]}x{resolution[1]}|liberal={liberal_license}"
        return hashlib.md5(raw.encode()).hexdigest()

    def _load_cache(self, cache_key: str) -> Optional[VideoAsset]:
        """Load a cached search result, or None if not found."""
        cache_file = _CACHE_DIR / f"{cache_key}.json"
        if not cache_file.exists():
            return None

        try:
            data = json.loads(cache_file.read_text())
            if data.get("result") == "none":
                # Cached negative result — don't re-search for a while
                max_age = 3600  # 1 hour
                age = time.time() - cache_file.stat().st_mtime
                if age < max_age:
                    return None  # cached negative, skip
                # Expired — remove and re-search
                cache_file.unlink(missing_ok=True)
                return None
            # Cached positive result — but we can't fully reconstruct
            # a VideoAsset from JSON alone (no yt_entry), so re-search
            return None
        except (json.JSONDecodeError, OSError):
            return None

    def _save_cache(self, cache_key: str, asset: Optional[VideoAsset]) -> None:
        """Save a search result to cache."""
        cache_file = _CACHE_DIR / f"{cache_key}.json"
        if asset is None:
            cache_file.write_text(json.dumps({"result": "none"}))
        else:
            # We don't cache full assets (they may contain non-serializable yt-dlp entries)
            # Just record a positive hit
            cache_file.write_text(json.dumps({
                "result": "found",
                "url": asset.url,
                "duration": asset.duration,
                "resolution": list(asset.resolution),
                "provider": asset.provider,
            }))
