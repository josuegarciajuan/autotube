"""Public YouTube stats scraper — zero Data API quota.

Retrieves public watch-page and channel metrics (viewCount, likeCount,
commentCount, subscriberCount) via yt-dlp (innertube web client).

Used as a RESILIENCE fallback in stats collection: when the YouTube Data API
v3 daily quota is exhausted, the "Recolectar stats" button still refreshes
public metrics instead of failing or zeroing out likes/comments.

It consumes NO YouTube Data API units — only public HTTP requests. It can even
run for channels without an OAuth token (public data needs no auth).
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed

logger = logging.getLogger(__name__)

# Silence yt-dlp's stderr "ERROR: [youtube] ..." noise for unavailable videos
# (we already handle failures ourselves via is_mock=True).
_ytdlp_logger = logging.getLogger("autotube.ytdlp_null")
_ytdlp_logger.addHandler(logging.NullHandler())
_ytdlp_logger.propagate = False
_ytdlp_logger.setLevel(logging.CRITICAL)

_WATCH_URL = "https://www.youtube.com/watch?v={id}"


def _parse_int(value, default: int = 0) -> int:
    try:
        if value is None:
            return default
        return int(value)
    except (TypeError, ValueError):
        return default


class YouTubeStatsScraper:
    """Scrape public YouTube stats without the Data API (no quota)."""

    def __init__(self, channel_slug: str, max_concurrency: int = 6):
        self.slug = channel_slug
        self.max_concurrency = max(1, int(max_concurrency))

    # ── Video stats ──────────────────────────────────────────

    def _scrape_one_video(self, yt_id: str) -> dict:
        import yt_dlp

        ydl_opts = {
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "skip_download": True,
            "ignoreerrors": True,
            "logger": _ytdlp_logger,
        }
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(
                    _WATCH_URL.format(id=yt_id), download=False
                )
        except Exception as exc:
            logger.debug("[%s] scrape failed for %s: %s", self.slug, yt_id, exc)
            info = None

        if not info:
            return {
                "viewCount": "0",
                "likeCount": "0",
                "commentCount": "0",
                "is_mock": True,
            }

        return {
            "viewCount": str(_parse_int(info.get("view_count"))),
            "likeCount": str(_parse_int(info.get("like_count"))),
            "commentCount": str(_parse_int(info.get("comment_count"))),
            "is_mock": False,
        }

    def get_video_stats_batch(self, yt_video_ids: list[str]) -> dict[str, dict]:
        """Scrape public stats for multiple videos in parallel.

        Returns a dict mapping yt_id -> {viewCount, likeCount, commentCount,
        is_mock}. Videos that could not be scraped (private/deleted/
        bot-checked) come back with is_mock=True so callers skip inserting
        them (avoids storing fabricated numbers, unlike the old _mock_stats).
        """
        if not yt_video_ids:
            return {}

        # Preserve order, dedup (a video id repeated would waste requests)
        unique_ids = list(dict.fromkeys(yt_video_ids))
        result: dict[str, dict] = {}
        workers = max(1, min(self.max_concurrency, len(unique_ids)))

        with ThreadPoolExecutor(max_workers=workers) as ex:
            futures = {ex.submit(self._scrape_one_video, vid): vid
                       for vid in unique_ids}
            for fut in as_completed(futures):
                vid = futures[fut]
                try:
                    result[vid] = fut.result()
                except Exception as exc:
                    logger.debug("[%s] scrape error for %s: %s",
                                 self.slug, vid, exc)
                    result[vid] = {
                        "viewCount": "0",
                        "likeCount": "0",
                        "commentCount": "0",
                        "is_mock": True,
                    }
        return result

    # ── Channel stats ────────────────────────────────────────

    def get_channel_stats(self, channel: dict) -> dict:
        """Scrape channel subscribers from the public channel page.

        Args:
            channel: channel row (dict) with at least yt_channel_id and/or
                     yt_channel_url.

        Returns:
            {subscriberCount, title}. total views and video count are NOT
            available from the public page via yt-dlp (they live on the
            About tab), so they are omitted — the caller should carry forward
            the last known values to avoid false valleys in charts.
        """
        import yt_dlp

        channel_url = channel.get("yt_channel_url")
        if not channel_url:
            yt_channel_id = channel.get("yt_channel_id")
            if yt_channel_id:
                channel_url = f"https://www.youtube.com/channel/{yt_channel_id}"
        if not channel_url:
            return {}

        ydl_opts = {
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
            "extract_flat": True,
            "playlistend": 1,
            "ignoreerrors": True,
            "logger": _ytdlp_logger,
        }
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(channel_url, download=False)
        except Exception as exc:
            logger.warning("[%s] channel scrape failed: %s", self.slug, exc)
            return {}

        if not info:
            return {}

        subscribers = _parse_int(info.get("channel_follower_count"))
        return {
            "subscriberCount": str(subscribers),
            "title": info.get("channel") or channel.get("name", ""),
        }
