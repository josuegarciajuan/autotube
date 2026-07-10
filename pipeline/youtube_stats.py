"""YouTube analytics and statistics fetcher.

Retrieves video and channel stats via YouTube Data API v3 and
optionally the YouTube Analytics API for advanced metrics.
"""

import logging
import pickle
from pathlib import Path
from typing import Optional, Any

from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from config.settings import TOKENS_DIR

logger = logging.getLogger(__name__)


class YouTubeStatsFetcher:
    """Fetch YouTube stats for videos and channels."""

    def __init__(self, channel_slug: str):
        self.slug = channel_slug
        self._token_path = TOKENS_DIR / f"{channel_slug}.pickle"
        self._service: Any = None
        self._analytics_service: Any = None

    # ── Authentication ─────────────────────────────────────────

    def authenticate(self) -> bool:
        """Load and refresh channel token."""
        if not self._token_path.exists():
            logger.warning("No token for %s at %s", self.slug, self._token_path)
            return False

        try:
            with open(self._token_path, "rb") as f:
                creds = pickle.load(f)
        except Exception as exc:
            logger.error("Cannot load token: %s", exc)
            return False

        if creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
                with open(self._token_path, "wb") as f:
                    pickle.dump(creds, f)
            except Exception as exc:
                logger.error("Token refresh failed: %s", exc)
                return False

        if not creds.valid:
            return False

        self._service = build("youtube", "v3", credentials=creds, cache_discovery=False)

        try:
            self._analytics_service = build(
                "youtubeAnalytics", "v2", credentials=creds, cache_discovery=False
            )
        except Exception:
            self._analytics_service = None

        return True

    # ── Video Stats ────────────────────────────────────────────

    def get_video_stats(self, yt_video_id: str) -> dict:
        """Get basic stats for a single video.

        Returns {viewCount, likeCount, commentCount, ...}
        Adds is_mock=True when stats are synthetic (not from YouTube API).
        """
        if not self._service:
            if not self.authenticate():
                result = self._mock_stats(yt_video_id)
                result["is_mock"] = True
                return result

        try:
            resp = (
                self._service.videos()
                .list(part="statistics,snippet,contentDetails,status", id=yt_video_id)
                .execute()
            )
            items = resp.get("items", [])
            if not items:
                result = self._mock_stats(yt_video_id)
                result["is_mock"] = True
                return result

            item = items[0]
            stats = item.get("statistics", {})
            status_obj = item.get("status", {})
            return {
                "viewCount": stats.get("viewCount", "0"),
                "likeCount": stats.get("likeCount", "0"),
                "commentCount": stats.get("commentCount", "0"),
                "title": item.get("snippet", {}).get("title", ""),
                "embeddable": status_obj.get("embeddable", True),
                "is_mock": False,
            }
        except HttpError as exc:
            logger.warning("Stats fetch failed for %s: %s", yt_video_id, exc)
            result = self._mock_stats(yt_video_id)
            result["is_mock"] = True
            return result

    def get_video_analytics(self, yt_video_id: str, days: int = 30) -> dict:
        """Get advanced analytics via YouTube Analytics API.

        Returns {estimatedMinutesWatched, averageViewDuration, ...}
        Falls back to basic stats if Analytics API is unavailable.
        """
        if not self._analytics_service:
            return self.get_video_stats(yt_video_id)

        try:
            resp = (
                self._analytics_service.reports()
                .query(
                    ids=f"channel==MINE",
                    startDate=f"{days}dAgo",
                    endDate="today",
                    metrics="estimatedMinutesWatched,averageViewDuration",
                    filters=f"video=={yt_video_id}",
                )
                .execute()
            )
            rows = resp.get("rows", [])
            if rows:
                return {
                    "estimatedMinutesWatched": str(rows[0][0]),
                    "averageViewDuration": str(rows[0][1]),
                }
        except Exception as exc:
            logger.debug("Analytics API unavailable: %s", exc)

        return self.get_video_stats(yt_video_id)

    # ── Channel Stats ──────────────────────────────────────────

    def get_channel_stats(self) -> dict:
        """Get channel-level statistics.

        Returns {subscriberCount, viewCount, videoCount, title}
        """
        if not self._service:
            if not self.authenticate():
                return {}

        try:
            resp = (
                self._service.channels()
                .list(part="statistics,snippet", mine=True)
                .execute()
            )
            items = resp.get("items", [])
            if not items:
                return {}

            item = items[0]
            stats = item.get("statistics", {})
            return {
                "subscriberCount": stats.get("subscriberCount", "0"),
                "viewCount": stats.get("viewCount", "0"),
                "videoCount": stats.get("videoCount", "0"),
                "title": item.get("snippet", {}).get("title", ""),
                "channelId": item.get("id", ""),
            }
        except HttpError as exc:
            logger.error("Channel stats fetch failed: %s", exc)
            return {}

    def get_channel_analytics(self, days: int = 30) -> dict:
        """Get channel-level analytics via YouTube Analytics API.

        Returns {estimatedMinutesWatched} for the channel (lifetime or
        within the supplied window depending on the Analytics API).

        Falls back to an empty dict if the Analytics API is unavailable.
        """
        if not self._analytics_service:
            return {}

        try:
            resp = (
                self._analytics_service.reports()
                .query(
                    ids="channel==MINE",
                    startDate=f"{days}dAgo",
                    endDate="today",
                    metrics="estimatedMinutesWatched",
                )
                .execute()
            )
            rows = resp.get("rows", [])
            if rows and rows[0]:
                return {
                    "estimatedMinutesWatched": str(rows[0][0]),
                }
        except HttpError as exc:
            logger.warning(
                "Channel analytics fetch failed for %s: %s", self.slug, exc
            )
        except Exception as exc:
            logger.debug("Analytics API unavailable for channel %s: %s", self.slug, exc)

        return {}

    # ── Batch collection ───────────────────────────────────────

    def collect_and_store(self, db) -> dict:
        """Collect stats for all uploaded videos and the channel, store in DB.

        Args:
            db: ExtendedDatabase instance.

        Returns summary dict.
        """
        if not self.authenticate():
            return {"error": "Authentication failed"}

        result = {"videos_updated": 0, "channel_updated": False}

        # Channel stats
        channel = db.get_channel_by_slug(self.slug)
        if channel:
            channel_stats = self.get_channel_stats()
            if channel_stats:
                # Merge channel analytics (watch time) into stats dict
                analytics = self.get_channel_analytics()
                if analytics:
                    channel_stats.update(analytics)
                db.insert_channel_stats(channel["id"], channel_stats)
                result["channel_updated"] = True
                result["channel_stats"] = channel_stats

        # Video stats
        videos = db.get_videos(channel_id=channel["id"] if channel else None)
        for v in videos:
            yt_id = v.get("yt_video_id")
            if not yt_id:
                continue
            try:
                stats = self.get_video_stats(yt_id)
                # Skip mock/synthetic stats — only store real YouTube API data
                if stats.get("is_mock"):
                    logger.debug("Skipping mock stats for video %s (%s)", v.get("id"), yt_id)
                    continue
                if "error" not in stats:
                    db.insert_video_stats(v["id"], yt_id, stats)
                    result["videos_updated"] += 1
            except Exception as exc:
                logger.error("Failed to store stats for video %s: %s", v.get("id"), exc)

        # Shorts stats
        result["shorts_updated"] = 0
        if channel:
            shorts = db.get_shorts(channel_id=channel["id"], status="published")
            for s in shorts:
                yt_id = s.get("youtube_id")
                if not yt_id:
                    continue
                try:
                    stats = self.get_video_stats(yt_id)
                    if stats.get("is_mock"):
                        logger.debug("Skipping mock stats for short %s (%s)", s.get("id"), yt_id)
                        continue
                    if "error" not in stats:
                        db.insert_short_stats(s["id"], yt_id, stats)
                        result["shorts_updated"] += 1
                except Exception as exc:
                    logger.error("Failed to store stats for short %s: %s", s.get("id"), exc)

        logger.info(
            "Stats collection: %s videos, %s shorts, channel=%s",
            result["videos_updated"],
            result["shorts_updated"],
            result["channel_updated"],
        )
        return result

    # ── Mock (fallback) ────────────────────────────────────────

    @staticmethod
    def _mock_stats(video_id: str) -> dict:
        """Mock stats when API is unavailable."""
        import hashlib
        if not video_id:
            return {"viewCount": "0", "likeCount": "0", "commentCount": "0"}
        h = int(hashlib.md5(video_id.encode()).hexdigest()[:8], 16)
        return {
            "viewCount": str(1000 + (h % 500000)),
            "likeCount": str(50 + (h % 25000)),
            "commentCount": str(5 + (h % 2000)),
        }
