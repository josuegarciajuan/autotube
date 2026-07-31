"""View Gap Monitor — daily YT vs DB view comparison.

Compares the total view count reported by YouTube for each channel
against the sum of views tracked in the local database. When the gap
grows beyond a threshold within 24 hours, an alert is raised in the
existing pipeline_alerts table. Optionally, unregistered video IDs
are auto-scanned and registered.

Architecture:
  check_all_channels()  →  itera canales activos
    check_channel()     →  lógica por canal
      _scan_and_register()  →  descubre videos no trackeados
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from config.settings import (
    VIEW_GAP_THRESHOLD,
    VIEW_GAP_SCAN_UNREGISTERED,
)
from config.config_bridge import get_channel_config
from pipeline.youtube_stats import YouTubeStatsFetcher

logger = logging.getLogger("autotube.view_gap")

# Dedicated log file so gap events are always easy to find
_LOG_DIR = Path(__file__).parent.parent.parent / "logs"
_LOG_DIR.mkdir(exist_ok=True)
_gap_log_handler: logging.FileHandler | None = None


def _ensure_log_handler() -> None:
    """Set up the dedicated view-gap log file once."""
    global _gap_log_handler
    if _gap_log_handler is not None:
        return
    _gap_log_handler = logging.FileHandler(
        _LOG_DIR / "view_gap_monitor.log", encoding="utf-8"
    )
    _gap_log_handler.setFormatter(
        logging.Formatter("%(asctime)s [%(levelname)s] %(message)s",
                          datefmt="%Y-%m-%d %H:%M:%S")
    )
    _gap_log_handler.setLevel(logging.INFO)
    logger.addHandler(_gap_log_handler)
    logger.propagate = True  # also go to root logger


# ── Public API ────────────────────────────────────────────────────

class ViewGapMonitor:
    """Daily view-gap detection and optional unregistered-video scan."""

    def check_all_channels(self, db) -> dict:
        """Run the gap check for every active, authenticated channel.

        Returns:
            {"channels_checked": int, "gaps_detected": int,
             "videos_registered": int, "errors": int}
        """
        _ensure_log_handler()

        from database.db_extended import ExtendedDatabase
        channels = db.get_channels(active_only=True)
        if not channels:
            logger.info("No active channels — skipping view gap check")
            return {"channels_checked": 0, "gaps_detected": 0,
                    "videos_registered": 0, "errors": 0}

        result = {"channels_checked": 0, "gaps_detected": 0,
                  "videos_registered": 0, "errors": 0}

        for ch in channels:
            slug = ch.get("slug", "")
            channel_id = ch["id"]
            channel_name = ch.get("name", slug)

            # Per-channel opt-out
            try:
                cfg = get_channel_config(slug)
                if getattr(cfg, "ENABLE_DAILY_VIEW_GAP_CHECK", False) is False:
                    logger.debug("View gap check disabled for %s via config", slug)
                    continue
            except Exception:
                pass  # default = check

            try:
                r = self.check_channel(db, ch)
                result["channels_checked"] += 1
                if r.get("alert_created"):
                    result["gaps_detected"] += 1
                result["videos_registered"] += r.get("videos_registered", 0)
            except Exception as exc:
                logger.error("View gap check failed for %s: %s", slug, exc)
                result["errors"] += 1

        logger.info(
            "View gap check complete: %d channels, %d gaps, %d registered, %d errors",
            result["channels_checked"], result["gaps_detected"],
            result["videos_registered"], result["errors"],
        )
        return result

    def check_channel(self, db, channel: dict) -> dict:
        """Check a single channel for view-gap anomalies.

        Args:
            db: ExtendedDatabase instance.
            channel: Row dict from channels table (must include id, slug, name).

        Returns dict with keys:
          gap, delta, yt_total, db_total, coverage_pct, alert_created,
          videos_registered, error (if any).
        """
        slug = channel["slug"]
        channel_id = channel["id"]
        channel_name = channel.get("name", slug)

        # 1. Authenticate and fetch YouTube channel total views
        fetcher = YouTubeStatsFetcher(slug)
        if not fetcher.authenticate():
            logger.warning("View gap: auth failed for %s", slug)
            return {"error": "auth_failed", "slug": slug}

        yt_stats = fetcher.get_channel_stats()
        if not yt_stats:
            logger.warning("View gap: no YT stats for %s", slug)
            return {"error": "no_yt_stats", "slug": slug}

        try:
            yt_total = int(yt_stats.get("viewCount", 0))
            yt_video_count = int(yt_stats.get("videoCount", 0))
        except (ValueError, TypeError):
            logger.warning("View gap: invalid viewCount for %s: %r",
                          slug, yt_stats.get("viewCount"))
            return {"error": "bad_yt_data", "slug": slug}

        # 2. Sum known views from DB
        known = db.get_db_known_views_sum(channel_id)
        db_total = known["total"]
        db_longform = known["longform_views"]
        db_shorts = known["shorts_views"]
        db_video_count = known["video_count"]

        # 3. Calculate gap and coverage
        gap = max(0, yt_total - db_total)
        coverage = (db_total / yt_total * 100) if yt_total > 0 else 100.0
        coverage = round(coverage, 1)

        now = datetime.now(timezone.utc)
        now_iso = now.isoformat()
        state_key = f"view_gap_{slug}"

        # 4. Load previous gap state
        prev_data: dict[str, Any] = {}
        try:
            raw = db.get_system_state(state_key)
            if raw:
                prev_data = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            prev_data = {}

        prev_gap = prev_data.get("gap", gap)
        prev_at = prev_data.get("last_checked", "")
        delta = max(0, gap - prev_gap)

        # 5. Persist current state
        current_state = {
            "gap": gap,
            "delta": delta,
            "yt_total_views": yt_total,
            "yt_video_count": yt_video_count,
            "db_total_views": db_total,
            "db_longform_views": db_longform,
            "db_shorts_views": db_shorts,
            "db_video_count": db_video_count,
            "coverage_pct": coverage,
            "last_checked": now_iso,
            "last_gap": prev_gap,
            "last_checked_prev": prev_at,
        }
        try:
            db.set_system_state(state_key, json.dumps(current_state))
        except Exception as exc:
            logger.error("View gap: failed to persist state for %s: %s", slug, exc)

        # 6. Check threshold → alert
        alert_created = False
        if delta > VIEW_GAP_THRESHOLD and prev_at:
            hours_since = "unknown"
            try:
                prev_dt = datetime.fromisoformat(prev_at)
                hours_since = f"{round((now - prev_dt).total_seconds() / 3600, 1)}h"
            except Exception:
                pass

            alert_created = self._create_gap_alert(
                db, channel_id, channel_name, slug,
                gap=gap, delta=delta, yt_total=yt_total, db_total=db_total,
                coverage=coverage, yt_video_count=yt_video_count,
                db_video_count=db_video_count, hours_since=hours_since,
                prev_gap=prev_gap,
            )

            logger.info(
                "GAP ALERT [%s] yt=%d db=%d gap=%d delta=+%d coverage=%.1f%%",
                slug, yt_total, db_total, gap, delta, coverage,
            )

        # 7. Optional: scan for unregistered videos
        videos_registered = 0
        if VIEW_GAP_SCAN_UNREGISTERED and gap > 0:
            try:
                videos_registered = self._scan_and_register(
                    db, fetcher, channel_id, slug
                )
            except Exception as exc:
                logger.error(
                    "View gap: unregistered scan failed for %s: %s", slug, exc
                )

        return {
            "gap": gap,
            "delta": delta,
            "yt_total": yt_total,
            "db_total": db_total,
            "db_longform": db_longform,
            "db_shorts": db_shorts,
            "coverage_pct": coverage,
            "alert_created": alert_created,
            "videos_registered": videos_registered,
        }

    # ── helpers ───────────────────────────────────────────────────

    @staticmethod
    def _create_gap_alert(
        db, channel_id: int, channel_name: str, slug: str,
        gap: int, delta: int, yt_total: int, db_total: int,
        coverage: float, yt_video_count: int, db_video_count: int,
        hours_since: str, prev_gap: int,
    ) -> bool:
        """Create a pipeline_alerts row for a detected view gap."""
        try:
            from api.services.lifecycle_monitor import create_alert

            title = (
                f"View gap detected: {channel_name} "
                f"(+{delta:,} untracked views)"
            )
            message = (
                f"YouTube reports {yt_total:,} total channel views but "
                f"Autotube only tracks {db_total:,}. "
                f"Gap: {gap:,} views (Δ+{delta:,} over ~{hours_since}). "
                f"Coverage: {coverage:.1f}%. "
                f"YouTube video count: {yt_video_count} | DB tracked: {db_video_count}. "
                f"Suggest reviewing YouTube Analytics for this channel "
                f"to identify viral videos not tracked by Autotube."
            )

            alert_id = create_alert(
                db,
                entity_type="system",
                entity_id=None,
                channel_id=channel_id,
                alert_type="view_gap_detected",
                severity="warning",
                title=title,
                message=message,
                metadata={
                    "gap": gap,
                    "delta": delta,
                    "previous_gap": prev_gap,
                    "yt_total_views": yt_total,
                    "db_total_views": db_total,
                    "coverage_pct": coverage,
                    "yt_video_count": yt_video_count,
                    "db_video_count": db_video_count,
                    "slug": slug,
                    "channel_name": channel_name,
                    "checked_at": datetime.now(timezone.utc).isoformat(),
                    "threshold": VIEW_GAP_THRESHOLD,
                },
            )
            if alert_id:
                logger.info(
                    "Gap alert #%d created for %s (gap=%d delta=+%d)",
                    alert_id, slug, gap, delta,
                )
                return True
        except Exception as exc:
            logger.error("Failed to create gap alert for %s: %s", slug, exc)
        return False

    @staticmethod
    def _scan_and_register(
        db, fetcher: YouTubeStatsFetcher, channel_id: int, slug: str,
    ) -> int:
        """Scan YouTube for video IDs not in our DB and register them.

        Downloads thumbnails for newly discovered videos so they appear
        with a preview in the panel.
        """
        # Get IDs known to our system
        known_ids = db.get_known_yt_ids(channel_id)

        # Fetch videos from YouTube (most recent 200)
        try:
            yt_videos = fetcher.list_channel_videos(max_results=200)
        except Exception as exc:
            logger.warning("list_channel_videos failed for %s: %s", slug, exc)
            return 0

        if not yt_videos:
            return 0

        # Find unregistered ones
        registered = 0
        thumb_dir = Path(__file__).parent.parent.parent / "output" / "thumbnails" / slug
        thumb_dir.mkdir(parents=True, exist_ok=True)

        for v in yt_videos:
            yt_id = v.get("yt_video_id", "")
            if not yt_id or yt_id in known_ids:
                continue

            # Download thumbnail if available
            thumb_path = ""
            thumb_url = v.get("thumbnail_url", "")
            if thumb_url:
                try:
                    import urllib.request
                    thumb_file = thumb_dir / f"unreg_{yt_id}.jpg"
                    urllib.request.urlretrieve(thumb_url, thumb_file)
                    thumb_path = str(thumb_file)
                except Exception:
                    pass

            v["thumbnail_path"] = thumb_path
            new_id = db.register_unregistered_video(channel_id, v, slug=slug)
            if new_id:
                registered += 1
                known_ids.add(yt_id)
                logger.info(
                    "Registered untracked video: %s (%s) — %s",
                    yt_id, slug, v.get("title", "")[:80],
                )

        if registered:
            logger.info(
                "Auto-registered %d new videos for %s (out of %d scanned)",
                registered, slug, len(yt_videos),
            )

        return registered


# ── Convenience function ──────────────────────────────────────────

def run_view_gap_check(db=None) -> dict:
    """Run the daily view gap check for all channels.

    Can be called standalone for testing:
        python3 -c "from api.services.view_gap_monitor import run_view_gap_check; run_view_gap_check()"
    """
    if db is None:
        from database.db_extended import ExtendedDatabase
        db = ExtendedDatabase()

    _ensure_log_handler()

    monitor = ViewGapMonitor()
    result = monitor.check_all_channels(db)

    print(json.dumps(result, indent=2))
    return result
