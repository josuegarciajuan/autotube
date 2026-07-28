"""YouTube analytics and statistics fetcher.

Retrieves video and channel stats via YouTube Data API v3 and
optionally the YouTube Analytics API for advanced metrics.
"""

import logging
import pickle
from datetime import datetime, timedelta
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
            start_date = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
            end_date = datetime.now().strftime("%Y-%m-%d")
            resp = (
                self._analytics_service.reports()
                .query(
                    ids=f"channel==MINE",
                    startDate=start_date,
                    endDate=end_date,
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

    def get_all_videos_analytics(self, video_ids: list[str], days: int = 365) -> dict[str, dict]:
        """Get analytics for multiple videos in a single API call.

        Uses dimensions=video to fetch estimatedMinutesWatched,
        averageViewDuration, and subscribersGained for all videos at once.

        Args:
            video_ids: List of YouTube video IDs.
            days: Lookback window in days.

        Returns:
            Dict mapping yt_video_id → {estimatedMinutesWatched, averageViewDuration, subscribersGained}
            Empty dict if no analytics data available.
        """
        if not self._analytics_service:
            logger.debug("Analytics API not available for bulk video analytics")
            return {}

        if not video_ids:
            return {}

        # YouTube Analytics API limit: max 200 videos in a single filter
        MAX_IDS_PER_CALL = 200
        result = {}

        for i in range(0, len(video_ids), MAX_IDS_PER_CALL):
            batch = video_ids[i : i + MAX_IDS_PER_CALL]
            try:
                start_date = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
                end_date = datetime.now().strftime("%Y-%m-%d")
                resp = (
                    self._analytics_service.reports()
                    .query(
                        ids="channel==MINE",
                        startDate=start_date,
                        endDate=end_date,
                        metrics="estimatedMinutesWatched,averageViewDuration,subscribersGained",
                        dimensions="video",
                        filters=f"video=={','.join(batch)}",
                        maxResults=200,
                    )
                    .execute()
                )
                rows = resp.get("rows", [])
                for row in rows:
                    # rows: [video_id, estimatedMinutesWatched, averageViewDuration, subscribersGained]
                    vid = row[0]
                    result[vid] = {
                        "estimatedMinutesWatched": str(row[1]) if row[1] else "0",
                        "averageViewDuration": str(row[2]) if row[2] else "0",
                        "subscribersGained": str(row[3]) if row[3] else "0",
                    }
                logger.debug(
                    "Bulk analytics: %d videos returned for %d requested (batch %d/%d)",
                    len(rows), len(batch), i // MAX_IDS_PER_CALL + 1,
                    (len(video_ids) + MAX_IDS_PER_CALL - 1) // MAX_IDS_PER_CALL,
                )
            except Exception as exc:
                logger.warning(
                    "Bulk analytics API failed for batch (size=%d): %s", len(batch), exc
                )

        return result

    def get_channel_daily_analytics(self, days: int = 365) -> list[dict]:
        """Get daily channel watch time breakdown.

        Uses dimensions=day to fetch daily estimatedMinutesWatched
        and subscribersGained for trend tracking.

        Args:
            days: Lookback window in days.

        Returns:
            List of {date, estimatedMinutesWatched, subscribersGained}.
            Empty list if no data available.
        """
        if not self._analytics_service:
            logger.debug("Analytics API not available for daily analytics")
            return []

        try:
            start_date = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
            end_date = datetime.now().strftime("%Y-%m-%d")
            resp = (
                self._analytics_service.reports()
                .query(
                    ids="channel==MINE",
                    startDate=start_date,
                    endDate=end_date,
                    metrics="estimatedMinutesWatched,subscribersGained",
                    dimensions="day",
                    maxResults=min(days, 200),
                )
                .execute()
            )
            rows = resp.get("rows", [])
            result = []
            for row in rows:
                result.append({
                    "date": row[0],  # YYYY-MM-DD
                    "estimatedMinutesWatched": float(row[1]) if row[1] else 0.0,
                    "subscribersGained": int(row[2]) if row[2] else 0,
                })
            logger.debug("Daily analytics: %d days returned for %s", len(rows), self.slug)
            return result
        except Exception as exc:
            logger.warning("Daily analytics API failed for %s: %s", self.slug, exc)
            return []

    def get_viewer_activity_by_hour(self, days: int = 30) -> dict:
        """Get viewer activity broken down by hour of day via YouTube Analytics API.

        Uses dimensions=hour to fetch views and watch time for each hour (0-23),
        aggregated across the lookback window. This is the best indicator of when
        the audience is actually watching content.

        Args:
            days: Lookback window in days (default 30).

        Returns:
            Dict with keys:
              - 'hourly': list of 24 dicts {hour: int, views: int, watch_minutes: float}
              - 'by_content_type': optional, if creatorContentType dimension available
              - 'sources': list of data source labels that contributed
            Empty dict if no data available.
        """
        result: dict = {"hourly": [], "by_content_type": None, "sources": []}

        if not self._analytics_service:
            logger.debug("Analytics API not available for hourly activity for %s", self.slug)
            return result

        start_date = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
        end_date = datetime.now().strftime("%Y-%m-%d")

        # Try 1: dimensions=hour,creatorContentType — separates VIDEO / SHORTS
        try:
            resp = (
                self._analytics_service.reports()
                .query(
                    ids="channel==MINE",
                    startDate=start_date,
                    endDate=end_date,
                    metrics="views,estimatedMinutesWatched",
                    dimensions="hour,creatorContentType",
                    sort="hour,creatorContentType",
                )
                .execute()
            )
            rows = resp.get("rows", [])
            if rows:
                # Parse into {hour: {type: {views, watch}}}
                by_type: dict = {}
                hourly_agg: dict = {}
                for row in rows:
                    h = int(row[0])
                    ctype = str(row[1])  # 'VIDEO', 'SHORTS', 'LIVE_VIDEO'
                    views = int(row[2]) if row[2] else 0
                    watch = float(row[3]) if row[3] else 0.0
                    if ctype not in by_type:
                        by_type[ctype] = {}
                    by_type[ctype][h] = {"views": views, "watch_minutes": watch}
                    # Aggregate into combined hourly
                    if h not in hourly_agg:
                        hourly_agg[h] = {"hour": h, "views": 0, "watch_minutes": 0.0}
                    hourly_agg[h]["views"] += views
                    hourly_agg[h]["watch_minutes"] += watch

                result["by_content_type"] = by_type
                result["hourly"] = [hourly_agg.get(h, {"hour": h, "views": 0, "watch_minutes": 0.0})
                                    for h in range(24)]
                result["sources"].append("api_hour_creatorContentType")
                logger.debug("Hourly activity by content type: %d rows for %s", len(rows), self.slug)
                return result
        except Exception:
            logger.debug("creatorContentType dimension not available for %s, falling back", self.slug)

        # Try 2: dimensions=hour only (aggregate all content types)
        try:
            resp = (
                self._analytics_service.reports()
                .query(
                    ids="channel==MINE",
                    startDate=start_date,
                    endDate=end_date,
                    metrics="views,estimatedMinutesWatched",
                    dimensions="hour",
                    sort="hour",
                )
                .execute()
            )
            rows = resp.get("rows", [])
            if rows:
                hourly = []
                row_map = {}
                for row in rows:
                    h = int(row[0])
                    row_map[h] = {
                        "hour": h,
                        "views": int(row[1]) if row[1] else 0,
                        "watch_minutes": float(row[2]) if row[2] else 0.0,
                    }
                hourly = [row_map.get(h, {"hour": h, "views": 0, "watch_minutes": 0.0})
                          for h in range(24)]
                result["hourly"] = hourly
                result["sources"].append("api_hour")
                logger.debug("Hourly activity (aggregate): %d rows for %s", len(rows), self.slug)
                return result
        except Exception as exc:
            logger.warning("Hourly activity API failed for %s: %s", self.slug, exc)

        return result

    def get_audience_country_split(self, days: int = 90) -> dict:
        """Get audience geographical split (Spain vs LATAM vs Other) via YouTube Analytics API.

        Uses dimensions=country to fetch views by viewer country. Classifies countries
        into Spain (ES), LATAM (MX, AR, CO, CL, PE, EC, VE, BO, PY, UY, CR, PA, GT,
        SV, HN, NI, DO, CU, PR), and Other.

        Args:
            days: Lookback window in days (default 90).

        Returns:
            Dict with keys:
              - 'spain_pct': float 0-1
              - 'latam_pct': float 0-1
              - 'other_pct': float 0-1
              - 'top_countries': list of {country_code, views, pct} (top 10)
              - 'total_views': int
            Empty dict if no data available.
        """
        latam_codes = {"MX", "AR", "CO", "CL", "PE", "EC", "VE", "BO", "PY", "UY",
                        "CR", "PA", "GT", "SV", "HN", "NI", "DO", "CU", "PR"}

        if not self._analytics_service:
            logger.debug("Analytics API not available for country split for %s", self.slug)
            return {}

        start_date = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
        end_date = datetime.now().strftime("%Y-%m-%d")

        try:
            resp = (
                self._analytics_service.reports()
                .query(
                    ids="channel==MINE",
                    startDate=start_date,
                    endDate=end_date,
                    metrics="views",
                    dimensions="country",
                    maxResults=50,
                    sort="-views",
                )
                .execute()
            )
            rows = resp.get("rows", [])
            if not rows:
                return {}

            total_views = 0
            spain_views = 0
            latam_views = 0
            top = []

            for row in rows:
                country = str(row[0])
                views = int(row[1]) if row[1] else 0
                total_views += views
                if country == "ES":
                    spain_views += views
                elif country in latam_codes:
                    latam_views += views
                if len(top) < 10:
                    top.append({"country_code": country, "views": views})

            if total_views == 0:
                return {}

            # Calculate percentages
            for t in top:
                t["pct"] = round(t["views"] / total_views * 100, 1)

            return {
                "spain_pct": round(spain_views / total_views, 3),
                "latam_pct": round(latam_views / total_views, 3),
                "other_pct": round(1.0 - (spain_views + latam_views) / total_views, 3),
                "top_countries": top,
                "total_views": total_views,
            }
        except Exception as exc:
            logger.warning("Country split API failed for %s: %s", self.slug, exc)
            return {}

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
            start_date = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
            end_date = datetime.now().strftime("%Y-%m-%d")
            resp = (
                self._analytics_service.reports()
                .query(
                    ids="channel==MINE",
                    startDate=start_date,
                    endDate=end_date,
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

        Returns summary dict including analytics_updated count.
        """
        if not self.authenticate():
            return {"error": "Authentication failed"}

        result = {"videos_updated": 0, "channel_updated": False, "analytics_updated": 0}

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

        # Video stats — use high limit to cover all videos, not just the 50 most recent
        videos = db.get_videos(channel_id=channel["id"] if channel else None, limit=10000)
        video_yt_ids: list[str] = []
        for v in videos:
            yt_id = v.get("yt_video_id")
            if not yt_id:
                continue
            video_yt_ids.append(yt_id)
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
        short_yt_ids: list[str] = []
        if channel:
            shorts = db.get_shorts(channel_id=channel["id"], status="published", limit=10000)
            for s in shorts:
                yt_id = s.get("youtube_id")
                if not yt_id:
                    continue
                short_yt_ids.append(yt_id)
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

        # ── Bulk video analytics (1 API call for ALL videos) ──
        if video_yt_ids and self._analytics_service:
            try:
                bulk_analytics = self.get_all_videos_analytics(video_yt_ids)
                if bulk_analytics:
                    # Map yt_video_id → video DB id
                    video_id_map = {v.get("yt_video_id"): v["id"] for v in videos if v.get("yt_video_id")}
                    count = db.batch_update_video_analytics(video_id_map, bulk_analytics)
                    result["analytics_updated"] = count
                    logger.info("Analytics updated for %d videos via bulk query", count)

                    # Also update shorts with analytics data (shorts can have watch time too)
                    if short_yt_ids:
                        short_id_map = {s.get("youtube_id"): s["id"] for s in shorts if s.get("youtube_id")}
                        short_count = db.batch_update_short_analytics(short_id_map, bulk_analytics)
                        result["analytics_updated"] += short_count
                        logger.info("Analytics updated for %d shorts via bulk query", short_count)
            except Exception as exc:
                logger.error("Bulk analytics update failed for %s: %s", self.slug, exc)

        # ── Daily channel analytics ──
        if channel and self._analytics_service:
            try:
                daily = self.get_channel_daily_analytics(days=365)
                if daily:
                    db.upsert_daily_watchtime(channel["id"], daily)
                    logger.info("Daily watchtime stored for %s: %d days", self.slug, len(daily))
            except Exception as exc:
                logger.error("Daily watchtime storage failed for %s: %s", self.slug, exc)

        logger.info(
            "Stats collection: %s videos, %s shorts, %s analytics, channel=%s",
            result["videos_updated"],
            result["shorts_updated"],
            result["analytics_updated"],
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
