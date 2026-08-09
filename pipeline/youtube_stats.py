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


def _set_quota_exhausted_global(channel_slug: str = "stats_collector"):
    """Set global quota exhausted flag so all YT API callers know to pause.
    
    Called from within stats collection when a 403 quotaExceeded is detected.
    This ensures the scheduler and UI both become aware immediately,
    not just when the uploader eventually hits the same error.
    """
    try:
        from database.db_extended import ExtendedDatabase
        _db = ExtendedDatabase()
        _db.set_quota_exhausted(channel_slug=channel_slug)
        logger.warning("YouTube Data API quota exhausted — scheduler paused globally")
    except Exception as exc:
        logger.debug("Could not set global quota flag: %s", exc)


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
            # Detect quota exhaustion early — set global flag
            if "quotaExceeded" in str(exc):
                result["_quota_exhausted"] = True
                _set_quota_exhausted_global("stats_collector")
            return result

    # ── Batch Video Stats (50 IDs per call — 50x less quota usage) ──

    _DATA_API_BATCH_SIZE = 50  # YouTube's max IDs per videos().list() call

    def get_video_stats_batch(self, yt_video_ids: list[str]) -> dict[str, dict]:
        """Get basic stats for multiple videos in a single API call.

        Args:
            yt_video_ids: List of YouTube video IDs (max 50 per call).

        Returns:
            Dict mapping yt_video_id → {viewCount, likeCount, commentCount, title,
                                         embeddable, is_mock}.
            If quota is exhausted, returns {'_quota_exhausted': True} immediately.
        """
        if not yt_video_ids:
            return {}

        if not self._service:
            if not self.authenticate():
                return {"_quota_exhausted": True}

        from googleapiclient.errors import HttpError as _HttpError

        # Split into batches of 50 (API limit)
        result: dict[str, dict] = {}
        for i in range(0, len(yt_video_ids), self._DATA_API_BATCH_SIZE):
            batch = yt_video_ids[i : i + self._DATA_API_BATCH_SIZE]
            try:
                resp = (
                    self._service.videos()
                    .list(
                        part="statistics,snippet,contentDetails,status",
                        id=",".join(batch),
                        maxResults=50,
                    )
                    .execute()
                )
                items = resp.get("items", [])
                returned_ids = set()
                for item in items:
                    vid = item["id"]
                    returned_ids.add(vid)
                    stats = item.get("statistics", {})
                    status_obj = item.get("status", {})
                    result[vid] = {
                        "viewCount": stats.get("viewCount", "0"),
                        "likeCount": stats.get("likeCount", "0"),
                        "commentCount": stats.get("commentCount", "0"),
                        "title": item.get("snippet", {}).get("title", ""),
                        "embeddable": status_obj.get("embeddable", True),
                        "is_mock": False,
                    }
                # Videos not returned by the API (deleted, private, etc.)
                for vid in batch:
                    if vid not in returned_ids:
                        mock = self._mock_stats(vid)
                        mock["is_mock"] = True
                        result[vid] = mock
            except _HttpError as exc:
                if "quotaExceeded" in str(exc):
                    logger.warning(
                        "Data API quota exhausted (batch %d/%d) — stopping individual video calls",
                        i // self._DATA_API_BATCH_SIZE + 1,
                        (len(yt_video_ids) + self._DATA_API_BATCH_SIZE - 1)
                        // self._DATA_API_BATCH_SIZE,
                    )
                    result["_quota_exhausted"] = True
                    _set_quota_exhausted_global("stats_collector")
                    return result
                logger.warning("Batch stats fetch failed: %s", exc)
                for vid in batch:
                    mock = self._mock_stats(vid)
                    mock["is_mock"] = True
                    result[vid] = mock

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

    def get_video_ctr_analytics(
        self,
        yt_video_id: str,
        start_date: str = None,
        end_date: str = None,
        days: int = 30,
    ) -> dict:
        """Fetch impressions + CTR for a single video via YouTube Analytics API.

        Uses the youtubeAnalytics v2 API with metrics impressions and
        impressionsClickThroughRate, filtered to a single video.

        This is separate from get_video_analytics() because CTR/impressions
        are not available from the YouTube Data API v3 (videos().list())
        and require the Analytics API.

        Args:
            yt_video_id: YouTube video ID (e.g. "dQw4w9WgXcQ")
            start_date: Optional start date in YYYY-MM-DD format.
                        If provided, used with end_date for a precise
                        window (e.g. 48h post-title-change).
            end_date: Optional end date in YYYY-MM-DD format.
            days: Fallback window if start_date not provided (default 30).

        Returns:
            Dict with keys:
                impressions: int — total impressions in window
                impressionsClickThroughRate: float — CTR as fraction (0.05 = 5%)
                averageViewDuration: float — seconds
            Empty dict if Analytics API is unavailable or returns no data.
        """
        if not self._analytics_service:
            logger.debug("Analytics API not available for CTR fetch — %s", self.slug)
            return {}

        from datetime import datetime as _dt, timedelta

        if not start_date:
            start_date = (_dt.now() - timedelta(days=days)).strftime("%Y-%m-%d")
        if not end_date:
            end_date = _dt.now().strftime("%Y-%m-%d")

        try:
            resp = (
                self._analytics_service.reports()
                .query(
                    ids="channel==MINE",
                    startDate=start_date,
                    endDate=end_date,
                    metrics="impressions,impressionsClickThroughRate,averageViewDuration",
                    filters=f"video=={yt_video_id}",
                )
                .execute()
            )
            rows = resp.get("rows", [])
            if rows and len(rows[0]) >= 3:
                return {
                    "impressions": int(rows[0][0]) if rows[0][0] else 0,
                    "impressionsClickThroughRate": float(rows[0][1]) if rows[0][1] else 0.0,
                    "averageViewDuration": float(rows[0][2]) if rows[0][2] else 0.0,
                }
        except HttpError as exc:
            logger.debug("CTR analytics fetch failed for %s: %s", yt_video_id, exc)
        except Exception as exc:
            logger.debug("CTR analytics fetch error for %s: %s", yt_video_id, exc)

        return {}

    def get_all_videos_analytics(self, video_ids: list[str], days: int = 365) -> dict[str, dict]:
        """Get analytics for multiple videos in a single API call.

        Uses dimensions=video to fetch estimatedMinutesWatched,
        averageViewDuration, subscribersGained, averageViewPercentage, and
        analytics-sourced views for all videos at once.

        Args:
            video_ids: List of YouTube video IDs.
            days: Lookback window in days.

        Returns:
            Dict mapping yt_video_id → {estimatedMinutesWatched, averageViewDuration,
                                         subscribersGained, averageViewPercentage,
                                         analyticsViews}
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
                        metrics="estimatedMinutesWatched,averageViewDuration,subscribersGained,averageViewPercentage,views",
                        dimensions="video",
                        filters=f"video=={','.join(batch)}",
                        maxResults=200,
                    )
                    .execute()
                )
                rows = resp.get("rows", [])
                for row in rows:
                    # row: [video_id, estMinWatched, avgViewDur, subsGained, avgViewPct, views]
                    vid = row[0]
                    result[vid] = {
                        "estimatedMinutesWatched": str(row[1]) if len(row) > 1 and row[1] else "0",
                        "averageViewDuration": str(row[2]) if len(row) > 2 and row[2] else "0",
                        "subscribersGained": str(row[3]) if len(row) > 3 and row[3] else "0",
                        "averageViewPercentage": str(row[4]) if len(row) > 4 and row[4] else "0",
                        "analyticsViews": str(row[5]) if len(row) > 5 and row[5] else "0",
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
 
    # ── Channel video listing ──────────────────────────────────
 
    def get_upload_playlist_id(self) -> str | None:
        """Get the channel's uploads playlist ID from contentDetails.

        The uploads playlist contains every video ever uploaded to
        the channel (public, unlisted, private — those accessible
        to the authenticated user).
        """
        if not self._service:
            if not self.authenticate():
                return None
        try:
            resp = (
                self._service.channels()
                .list(part="contentDetails", mine=True)
                .execute()
            )
            items = resp.get("items", [])
            if items:
                return items[0]["contentDetails"]["relatedPlaylists"]["uploads"]
        except HttpError as exc:
            logger.warning("Failed to get upload playlist for %s: %s", self.slug, exc)
        except Exception as exc:
            logger.warning("Upload playlist fetch error for %s: %s", self.slug, exc)
        return None

    def list_channel_videos(self, max_results: int = 200) -> list[dict]:
        """List all videos from the channel's uploads playlist.

        Uses playlistItems().list() — 1 quota unit per 50 items.

        Returns list of dicts with keys:
          yt_video_id, title, published_at, thumbnail_url, privacy_status
        """
        if not self._service:
            if not self.authenticate():
                return []

        playlist_id = self.get_upload_playlist_id()
        if not playlist_id:
            logger.warning("No upload playlist found for %s", self.slug)
            return []

        videos: list[dict] = []
        page_token: str | None = None

        while len(videos) < max_results:
            try:
                resp = (
                    self._service.playlistItems()
                    .list(
                        playlistId=playlist_id,
                        part="snippet,status",
                        maxResults=min(50, max_results - len(videos)),
                        pageToken=page_token,
                    )
                    .execute()
                )
            except HttpError as exc:
                logger.error("playlistItems API failed for %s: %s", self.slug, exc)
                break
            except Exception as exc:
                logger.error("playlistItems unexpected error for %s: %s", self.slug, exc)
                break

            for item in resp.get("items", []):
                snippet = item.get("snippet", {})
                resource = snippet.get("resourceId", {})
                thumbnails = snippet.get("thumbnails", {})
                thumb_url = (
                    thumbnails.get("medium", {}).get("url", "")
                    or thumbnails.get("default", {}).get("url", "")
                    or thumbnails.get("high", {}).get("url", "")
                )

                videos.append({
                    "yt_video_id": resource.get("videoId", ""),
                    "title": snippet.get("title", ""),
                    "published_at": snippet.get("publishedAt", ""),
                    "thumbnail_url": thumb_url,
                    "privacy_status": item.get("status", {}).get(
                        "privacyStatus", "public"
                    ),
                })

            page_token = resp.get("nextPageToken")
            if not page_token:
                break

        logger.info(
            "list_channel_videos: %d videos for %s (limit %d)",
            len(videos), self.slug, max_results,
        )
        return videos

    # ── Advanced Analytics ──────────────────────────────────────

    def get_video_impressions_ctr(
        self, video_ids: list[str], days: int = 30
    ) -> dict[str, dict]:
        """Get impressions and CTR for multiple videos.

        NOTE: The `impressions` metric requires YouTube Studio Analytics scope
        which is NOT available in the standard YouTube Analytics v2 API.
        This method works around the limitation by collecting
        `impressionClickThroughRate` where available.

        Returns:
            Dict mapping yt_video_id → {impressions: int, ctr_percent: float}
            Impressions will be 0 if the metric is not available for this channel.
        """
        if not self._analytics_service or not video_ids:
            return {}

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
                        metrics="impressions,impressionClickThroughRate",
                        dimensions="video",
                        filters=f"video=={','.join(batch)}",
                        maxResults=200,
                    )
                    .execute()
                )
                rows = resp.get("rows", [])
                for row in rows:
                    # row: [video_id, impressions, ctr]
                    vid = row[0]
                    result[vid] = {
                        "impressions": int(row[1]) if len(row) > 1 and row[1] else 0,
                        "ctr_percent": round(float(row[2]), 2) if len(row) > 2 and row[2] else 0.0,
                    }
                logger.debug(
                    "Impressions/CTR: %d videos returned (batch %d/%d)",
                    len(rows), i // MAX_IDS_PER_CALL + 1,
                    (len(video_ids) + MAX_IDS_PER_CALL - 1) // MAX_IDS_PER_CALL,
                )
            except Exception as exc:
                logger.warning(
                    "Impressions/CTR API failed for batch (size=%d): %s", len(batch), exc
                )

        return result

    def get_video_traffic_sources(
        self, video_ids: list[str], days: int = 30
    ) -> dict[str, list[dict]]:
        """Get traffic source breakdown for multiple videos.

        Uses dimensions=video,insightTrafficSourceType to break down
        views and watch minutes by traffic source.

        Returns:
            Dict mapping yt_video_id → [{source, views, watch_minutes}, ...]
        """
        if not self._analytics_service or not video_ids:
            return {}

        MAX_IDS_PER_CALL = 200
        result: dict[str, list[dict]] = {}

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
                        metrics="views,estimatedMinutesWatched",
                        dimensions="video,insightTrafficSourceType",
                        filters=f"video=={','.join(batch)}",
                        maxResults=2000,
                    )
                    .execute()
                )
                rows = resp.get("rows", [])
                for row in rows:
                    # row: [video_id, trafficSourceType, views, estimatedMinutesWatched]
                    vid = row[0]
                    source = row[1] if len(row) > 1 else "UNKNOWN"
                    views = int(row[2]) if len(row) > 2 and row[2] else 0
                    watch_min = float(row[3]) if len(row) > 3 and row[3] else 0.0
                    if vid not in result:
                        result[vid] = []
                    result[vid].append({
                        "dimension": source,
                        "metric_value": views,
                        "watch_minutes": watch_min,
                    })
                logger.debug(
                    "Traffic sources: %d rows returned (batch %d/%d)",
                    len(rows), i // MAX_IDS_PER_CALL + 1,
                    (len(video_ids) + MAX_IDS_PER_CALL - 1) // MAX_IDS_PER_CALL,
                )
            except Exception as exc:
                logger.warning(
                    "Traffic sources API failed for batch (size=%d): %s", len(batch), exc
                )

        return result

    def get_video_retention_pct(
        self, video_ids: list[str], days: int = 30
    ) -> dict[str, float]:
        """Get average view percentage (retention %) for multiple videos.

        Uses dimensions=video to fetch averageViewPercentage per video.
        This tells us what % of the video people watch on average.

        Returns:
            Dict mapping yt_video_id → average_view_percentage (0-100)
        """
        if not self._analytics_service or not video_ids:
            return {}

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
                        metrics="averageViewPercentage",
                        dimensions="video",
                        filters=f"video=={','.join(batch)}",
                        maxResults=200,
                    )
                    .execute()
                )
                rows = resp.get("rows", [])
                for row in rows:
                    # row: [video_id, averageViewPercentage]
                    vid = row[0]
                    result[vid] = round(float(row[1]), 2) if len(row) > 1 and row[1] else 0.0
                logger.debug(
                    "Retention pct: %d videos returned (batch %d/%d)",
                    len(rows), i // MAX_IDS_PER_CALL + 1,
                    (len(video_ids) + MAX_IDS_PER_CALL - 1) // MAX_IDS_PER_CALL,
                )
            except Exception as exc:
                logger.warning(
                    "Retention pct API failed for batch (size=%d): %s", len(batch), exc
                )

        return result

    def get_channel_demographics(self, days: int = 90) -> list[dict]:
        """Get audience demographics (age + gender) for the channel.

        Uses dimensions=ageGroup,gender to fetch viewerPercentage per segment.

        Returns:
            List of {age_group, gender, views_pct}
        """
        if not self._analytics_service:
            logger.debug("Analytics API not available for demographics")
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
                    metrics="viewerPercentage",
                    dimensions="ageGroup,gender",
                    maxResults=200,
                )
                .execute()
            )
            rows = resp.get("rows", [])
            result = []
            for row in rows:
                # row: [ageGroup, gender, viewerPercentage]
                result.append({
                    "age_group": row[0] if len(row) > 0 else "unknown",
                    "gender": row[1] if len(row) > 1 else "unknown",
                    "views_pct": round(float(row[2]), 2) if len(row) > 2 and row[2] else 0.0,
                })
            logger.info("Channel demographics: %d segments returned", len(result))
            return result
        except Exception as exc:
            logger.warning("Channel demographics API failed: %s", exc)
            return []

    # ── Batch collection ───────────────────────────────────────

    def collect_and_store(self, db, deep: bool = False) -> dict:
        """Collect stats for all uploaded videos and the channel, store in DB.

        Uses batch Data API calls (50 IDs per call) and falls back to
        YouTube Analytics API when Data API quota is exhausted.

        Returns summary dict including analytics_updated count and
        quota_exhausted flag for UI feedback.
        """
        if not self.authenticate():
            return {"error": "Authentication failed"}

        result = {
            "videos_updated": 0,
            "shorts_updated": 0,
            "channel_updated": False,
            "analytics_updated": 0,
            "quota_exhausted": False,
            "analytics_fallback_videos": 0,
            "analytics_fallback_shorts": 0,
        }

        # ── Channel stats (Data API → Analytics fallback) ──
        channel = db.get_channel_by_slug(self.slug)
        channel_stats = None
        if channel:
            channel_stats = self.get_channel_stats()
            channel_analytics = self.get_channel_analytics()

            if channel_stats:
                # Data API succeeded — merge analytics data
                if channel_analytics:
                    channel_stats.update(channel_analytics)
                db.insert_channel_stats(channel["id"], channel_stats)
                result["channel_updated"] = True
                result["channel_stats"] = channel_stats
                logger.info(
                    "Channel stats: subs=%s, views=%s, watch_mins=%s",
                    channel_stats.get("subscriberCount"),
                    channel_stats.get("viewCount"),
                    channel_stats.get("estimatedMinutesWatched"),
                )
            elif channel_analytics:
                # Data API down — Analytics API has watch time
                # Guard: analytics fallback often returns 0 for subscriberCount/viewCount,
                # which creates false valleys in dashboard charts → skip insert
                result["quota_exhausted"] = True
                sc = int(channel_analytics.get("subscriberCount", 0))
                vc = int(channel_analytics.get("viewCount", 0))
                if sc > 0 or vc > 0:
                    db.insert_channel_stats(channel["id"], channel_analytics)
                    result["channel_updated"] = True
                    result["channel_stats"] = channel_analytics
                    logger.warning(
                        "Channel stats from Analytics API only (Data API quota exhausted)"
                    )
                else:
                    logger.warning(
                        "Analytics fallback returned 0 subscribers+views for %s — skipping insert to avoid false valleys",
                        self.slug,
                    )
            else:
                logger.warning("No channel stats at all for %s", self.slug)

        # ── Gather all video/short IDs ──
        videos = db.get_videos(channel_id=channel["id"] if channel else None, limit=10000)
        video_yt_ids: list[str] = []
        video_map: dict[str, dict] = {}  # yt_id → video row
        for v in videos:
            yt_id = v.get("yt_video_id")
            if not yt_id:
                continue
            video_yt_ids.append(yt_id)
            video_map[yt_id] = v

        result["shorts_updated"] = 0
        short_yt_ids: list[str] = []
        shorts: list = []
        if channel:
            shorts = db.get_shorts(channel_id=channel["id"], status="published", limit=10000)
            for s in shorts:
                yt_id = s.get("youtube_id")
                if not yt_id:
                    continue
                short_yt_ids.append(yt_id)

        # ── Video stats — BATCH via Data API (50 IDs per call) ──
        quota_exhausted = result.get("quota_exhausted", False)
        inserted_video_ids: set[int] = set()  # track which videos got real Data API rows
        if not quota_exhausted and video_yt_ids:
            batch_result = self.get_video_stats_batch(video_yt_ids)
            if batch_result.get("_quota_exhausted"):
                quota_exhausted = True
                result["quota_exhausted"] = True
            else:
                for yt_id, stats in batch_result.items():
                    if yt_id.startswith("_"):  # skip meta keys
                        continue
                    v = video_map.get(yt_id)
                    if not v:
                        continue
                    if stats.get("is_mock"):
                        continue
                    if "error" not in stats:
                        db.insert_video_stats(v["id"], yt_id, stats)
                        result["videos_updated"] += 1
                        inserted_video_ids.add(v["id"])

        # ── Shorts stats — batch via Data API (skip if quota already gone) ──
        inserted_short_ids: set[int] = set()
        if not quota_exhausted and short_yt_ids:
            batch_result = self.get_video_stats_batch(short_yt_ids)
            if batch_result.get("_quota_exhausted"):
                quota_exhausted = True
                result["quota_exhausted"] = True
            else:
                short_map = {s["youtube_id"]: s for s in shorts if s.get("youtube_id")}
                for yt_id, stats in batch_result.items():
                    if yt_id.startswith("_"):
                        continue
                    s = short_map.get(yt_id)
                    if not s:
                        continue
                    if stats.get("is_mock"):
                        continue
                    if "error" not in stats:
                        short_type = s.get("type", "clip")
                        db.insert_short_stats(s["id"], yt_id, stats, short_type=short_type)
                        result["shorts_updated"] += 1
                        inserted_short_ids.add(s["id"])

        # ── Bulk video analytics (Analytics API — separate quota, always works) ──
        if video_yt_ids and self._analytics_service:
            try:
                bulk_analytics = self.get_all_videos_analytics(video_yt_ids)
                if bulk_analytics:
                    video_id_map = {
                        v["yt_video_id"]: v["id"]
                        for v in videos
                        if v.get("yt_video_id")
                    }

                    # ── Analytics fallback: INSERT rows when Data API was down ──
                    if quota_exhausted:
                        fallback_inserted = 0
                        for v in videos:
                            yt_id = v.get("yt_video_id")
                            if not yt_id:
                                continue
                            # Skip videos that already got real Data API rows this run
                            if v["id"] in inserted_video_ids:
                                continue
                            bdata = bulk_analytics.get(yt_id, {})
                            if not bdata:
                                continue
                            analytics_stats = {
                                "viewCount": bdata.get("analyticsViews", "0"),
                                "likeCount": "0",
                                "commentCount": "0",
                                "estimatedMinutesWatched": float(
                                    bdata.get("estimatedMinutesWatched", 0) or 0
                                ),
                                "averageViewDuration": float(
                                    bdata.get("averageViewDuration", 0) or 0
                                ),
                                "subscribersGained": int(
                                    float(bdata.get("subscribersGained", 0) or 0)
                                ),
                                "is_analytics_fallback": True,
                            }
                            db.insert_video_stats(v["id"], yt_id, analytics_stats)
                            fallback_inserted += 1
                        result["analytics_fallback_videos"] = fallback_inserted
                        if fallback_inserted:
                            logger.info(
                                "Analytics fallback: inserted %d video_stats_history rows (Data API was down)",
                                fallback_inserted,
                            )

                    # Update existing rows with precise analytics data
                    count = db.batch_update_video_analytics(video_id_map, bulk_analytics)
                    result["analytics_updated"] = count
                    logger.info("Analytics updated for %d videos via bulk query", count)

                    # Store averageViewPercentage (retention %) from expanded bulk query
                    retention_stored = 0
                    for v in videos:
                        yt_id = v.get("yt_video_id")
                        if not yt_id:
                            continue
                        bdata = bulk_analytics.get(yt_id, {})
                        avg_pct = float(bdata.get("averageViewPercentage", 0) or 0)
                        if avg_pct > 0:
                            db.insert_video_analytics_batch(
                                v["id"],
                                yt_id,
                                "retention_pct",
                                [{"dimension": None, "metric_value": avg_pct}],
                            )
                            retention_stored += 1
                    if retention_stored:
                        logger.info(
                            "Retention pct stored for %d videos via bulk query",
                            retention_stored,
                        )
                        result["retention_stored"] = retention_stored

                    # Also update shorts with analytics data
                    if short_yt_ids:
                        short_id_map = {
                            s["youtube_id"]: s["id"]
                            for s in shorts
                            if s.get("youtube_id")
                        }
                        short_count = db.batch_update_short_analytics(
                            short_id_map, bulk_analytics
                        )
                        result["analytics_updated"] += short_count
                        logger.info(
                            "Analytics updated for %d shorts via bulk query",
                            short_count,
                        )

                        # Analytics fallback for shorts too
                        if quota_exhausted:
                            fallback_short_inserted = 0
                            for s in shorts:
                                yt_id = s.get("youtube_id")
                                if not yt_id:
                                    continue
                                # Skip shorts that already got real Data API rows this run
                                if s["id"] in inserted_short_ids:
                                    continue
                                bdata = bulk_analytics.get(yt_id, {})
                                if not bdata:
                                    continue
                                analytics_short_stats = {
                                    "viewCount": bdata.get("analyticsViews", "0"),
                                    "likeCount": "0",
                                    "commentCount": "0",
                                    "estimatedMinutesWatched": float(
                                        bdata.get("estimatedMinutesWatched", 0) or 0
                                    ),
                                    "averageViewDuration": float(
                                        bdata.get("averageViewDuration", 0) or 0
                                    ),
                                    "subscribersGained": int(
                                        float(bdata.get("subscribersGained", 0) or 0)
                                    ),
                                }
                                short_type = s.get("type", "clip")
                                db.insert_short_stats(
                                    s["id"], yt_id, analytics_short_stats,
                                    short_type=short_type,
                                )
                                fallback_short_inserted += 1
                            result["analytics_fallback_shorts"] = fallback_short_inserted
                            if fallback_short_inserted:
                                logger.info(
                                    "Analytics fallback: inserted %d short_stats rows (Data API was down)",
                                    fallback_short_inserted,
                                )
            except Exception as exc:
                logger.error("Bulk analytics update failed for %s: %s", self.slug, exc)

        # ── Deep analytics (CTR, traffic, demographics) ──
        if deep and channel and video_yt_ids and self._analytics_service:
            logger.info("Deep analytics collection starting for %s (%d videos)", self.slug, len(video_yt_ids))

            # Collect impressions + CTR
            try:
                impressions_data = self.get_video_impressions_ctr(video_yt_ids, days=30)
                stored_imp = 0
                stored_ctr = 0
                for v in videos:
                    yt_id = v.get("yt_video_id")
                    if not yt_id:
                        continue
                    idata = impressions_data.get(yt_id, {})
                    impressions = idata.get("impressions", 0)
                    ctr = idata.get("ctr_percent", 0.0)
                    if impressions > 0 or ctr > 0:
                        db.insert_video_analytics_batch(
                            v["id"], yt_id, "impressions",
                            [{"dimension": None, "metric_value": impressions}],
                        )
                        stored_imp += 1
                        if ctr > 0:
                            db.insert_video_analytics_batch(
                                v["id"], yt_id, "ctr",
                                [{"dimension": None, "metric_value": ctr}],
                            )
                            stored_ctr += 1
                result["impressions_stored"] = stored_imp
                result["ctr_stored"] = stored_ctr
                logger.info("Deep analytics: %d impressions, %d CTR stored for %s",
                           stored_imp, stored_ctr, self.slug)
            except Exception as exc:
                logger.error("Deep analytics (CTR/impressions) failed for %s: %s", self.slug, exc)

            # Collect traffic sources
            try:
                traffic_data = self.get_video_traffic_sources(video_yt_ids, days=30)
                stored_traffic = 0
                for v in videos:
                    yt_id = v.get("yt_video_id")
                    if not yt_id:
                        continue
                    sources = traffic_data.get(yt_id, [])
                    if sources:
                        db.insert_video_analytics_batch(
                            v["id"], yt_id, "traffic_source", sources,
                        )
                        stored_traffic += 1
                result["traffic_stored"] = stored_traffic
                logger.info("Deep analytics: %d traffic source sets stored for %s",
                           stored_traffic, self.slug)
            except Exception as exc:
                logger.error("Deep analytics (traffic sources) failed for %s: %s", self.slug, exc)

            # Collect channel demographics
            try:
                demo_data = self.get_channel_demographics(days=90)
                if demo_data:
                    db.insert_channel_demographics(channel["id"], demo_data)
                    result["demographics_stored"] = len(demo_data)
                    logger.info("Deep analytics: %d demographic segments stored for %s",
                               len(demo_data), self.slug)
                else:
                    result["demographics_stored"] = 0
            except Exception as exc:
                logger.error("Deep analytics (demographics) failed for %s: %s", self.slug, exc)
                result["demographics_stored"] = 0

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
            "Stats collection: %s videos, %s shorts, %s analytics, channel=%s%s%s%s",
            result["videos_updated"],
            result["shorts_updated"],
            result["analytics_updated"],
            result["channel_updated"],
            " (deep)" if deep else "",
            " (quota_exhausted)" if result.get("quota_exhausted") else "",
            f" (+{result.get('analytics_fallback_videos', 0)} analytics-fallback)" 
            if result.get("analytics_fallback_videos", 0) else "",
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
