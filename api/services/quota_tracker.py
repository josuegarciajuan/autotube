"""Quota Tracker — pasive YouTube API quota accounting.

v1 (Aug 2026): Lightweight module that logs every YouTube Data API call
to the `yt_quota_log` table. Does NOT modify behavior — purely diagnostic.

Usage:
    from api.services.quota_tracker import track_quota

    track_quota("canal2", "videos.insert", 1600, yt_id="abc123")

    # Or as a decorator:
    @tracked("videos.insert", 1600)
    def upload_video(self, ...): ...

Cost: 0 YouTube API units (SQLite-only).
Throughput: ~0.1ms per insert (in-memory DB lock, no fsync).
"""

from __future__ import annotations

import logging
import threading
import time as _time
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path
from typing import Optional

logger = logging.getLogger("autotube.quota_tracker")

# ── Throttling: max 1 flush per THROTTLE_SEC ──
_THROTTLE_SEC = 2.0  # batch writes at most every 2 seconds
_batch_buffer: list[tuple] = []
_batch_lock = threading.Lock()
_last_flush = 0.0

# Default daily quota per channel (YouTube Data API v3 free tier)
DEFAULT_DAILY_QUOTA = 10_000


def track_quota(
    channel_slug: str,
    operation: str,
    units: int,
    *,
    yt_id: Optional[str] = None,
    success: bool = True,
    error: Optional[str] = None,
    caller: Optional[str] = None,
) -> None:
    """Record a YouTube API call against the quota budget.

    Thread-safe. Writes are buffered and flushed every ~2 seconds.

    Args:
        channel_slug: Channel identifier (canal2, canal3, etc.) or "shared"
        operation: API operation name (e.g., "videos.insert", "thumbnails.set")
        units: Quota cost in YouTube Data API units (1-1600)
        yt_id: YouTube video/channel/playlist ID involved
        success: Whether the call succeeded (False = error, but quota still consumed)
        error: Error message if call failed
        caller: Name of the calling function for traceability
    """
    if units <= 0:
        return

    _batch_buffer.append((
        channel_slug,
        operation,
        units,
        yt_id or "",
        1 if success else 0,
        (error or "")[:500],
        (caller or "")[:200],
    ))

    # Flush every THROTTLE_SEC or when buffer grows beyond 200 entries
    if len(_batch_buffer) >= 200:
        _flush()
    else:
        _maybe_flush()


def _maybe_flush() -> None:
    """Flush if enough time has passed since last write."""
    global _last_flush
    now = _time.monotonic()
    if now - _last_flush >= _THROTTLE_SEC:
        _flush()


def _flush() -> None:
    """Write buffered entries to the database."""
    global _last_flush, _batch_buffer
    with _batch_lock:
        if not _batch_buffer:
            return
        batch = _batch_buffer
        _batch_buffer = []
        _last_flush = _time.monotonic()

    try:
        from database.db_extended import ExtendedDatabase
        db = ExtendedDatabase()
        with db._connect() as conn:
            conn.executemany(
                """INSERT INTO yt_quota_log
                   (timestamp, channel_slug, operation, units, yt_id, success, error, caller)
                   VALUES (datetime('now','localtime'), ?, ?, ?, ?, ?, ?, ?)""",
                batch,
            )
            conn.commit()
    except Exception as exc:
        logger.debug("quota_tracker flush failed: %s (dropped %d entries)",
                     exc, len(batch))
        # Re-queue silently lost entries (best effort) — but don't infinite loop
        # Just log them at WARNING level for manual analysis
        logger.warning("quota_tracker: %d entries could not be persisted", len(batch))


def flush_quota_log() -> None:
    """Force immediate flush of pending entries. Call before shutdown."""
    _flush()


def get_daily_usage(db=None, channel_slug: Optional[str] = None,
                    date: Optional[str] = None) -> dict:
    """Get YouTube API quota usage for today (or a specific date).

    Returns:
        {
            "date": "2026-08-10",
            "total_units": 7234,
            "by_channel": {"canal2": 2100, "canal3": 3900, ...},
            "by_operation": {"videos.insert": 4800, "shorts.insert": 1600, ...},
            "by_hour": {"00": 0, "01": 200, ...},
            "quota_limit": 10000,
            "remaining": 2766,
            "exhausted_estimated_at": "2026-08-10T15:30:00",
        }
    """
    if db is None:
        from database.db_extended import ExtendedDatabase
        db = ExtendedDatabase()

    if date is None:
        date = datetime.now().strftime("%Y-%m-%d")

    with db._connect() as conn:
        conn.row_factory = None

        # ── Per-channel total ──
        by_channel = {}
        rows = conn.execute(
            "SELECT channel_slug, SUM(units) FROM yt_quota_log "
            "WHERE date(timestamp) = ? AND success = 1 "
            "GROUP BY channel_slug",
            (date,),
        ).fetchall()
        for ch, total in rows:
            by_channel[ch] = total or 0

        # ── Per-operation total ──
        by_operation = {}
        rows = conn.execute(
            "SELECT operation, SUM(units) FROM yt_quota_log "
            "WHERE date(timestamp) = ? AND success = 1 "
            "GROUP BY operation",
            (date,),
        ).fetchall()
        for op, total in rows:
            by_operation[op] = total or 0

        # ── Per-hour breakdown ──
        by_hour = {}
        rows = conn.execute(
            "SELECT strftime('%H', timestamp) AS h, SUM(units) FROM yt_quota_log "
            "WHERE date(timestamp) = ? AND success = 1 "
            "GROUP BY h ORDER BY h",
            (date,),
        ).fetchall()
        for h, total in rows:
            by_hour[h] = total or 0

        total = sum(by_channel.values())

        # ── Estimate exhaustion time (simple linear projection) ──
        hourly_rate = 0
        if by_hour:
            filled_hours = len(by_hour)
            hourly_rate = total / max(filled_hours, 1)

        exhausted_estimated_at = None
        if hourly_rate > 0 and total < DEFAULT_DAILY_QUOTA:
            remaining = DEFAULT_DAILY_QUOTA - total
            hours_left = remaining / hourly_rate
            # Pacific midnight = 07:00 UTC
            from datetime import timedelta
            estimated = datetime.now(timezone.utc) + timedelta(hours=hours_left)
            # Cap at next PT midnight
            pt_midnight = datetime.now(timezone.utc).replace(
                hour=7, minute=0, second=0, microsecond=0
            )
            if datetime.now(timezone.utc) > pt_midnight:
                pt_midnight += timedelta(days=1)
            if estimated > pt_midnight:
                estimated = pt_midnight
            exhausted_estimated_at = estimated.isoformat()

    return {
        "date": date,
        "total_units": total,
        "by_channel": by_channel,
        "by_operation": by_operation,
        "by_hour": by_hour,
        "quota_limit": DEFAULT_DAILY_QUOTA,
        "remaining": max(DEFAULT_DAILY_QUOTA - total, 0),
        "exhausted_estimated_at": exhausted_estimated_at,
    }


def get_recent_quota_log(limit: int = 50, channel_slug: Optional[str] = None) -> list[dict]:
    """Get recent quota log entries for debugging."""
    db = None
    try:
        from database.db_extended import ExtendedDatabase
        db = ExtendedDatabase()
        with db._connect() as conn:
            conn.row_factory = __import__("sqlite3").Row
            if channel_slug:
                rows = conn.execute(
                    "SELECT * FROM yt_quota_log WHERE channel_slug = ? "
                    "ORDER BY timestamp DESC LIMIT ?",
                    (channel_slug, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM yt_quota_log "
                    "ORDER BY timestamp DESC LIMIT ?",
                    (limit,),
                ).fetchall()
            return [dict(r) for r in rows]
    except Exception:
        return []


def is_quota_exhausted_for_channel(channel_slug: str) -> bool:
    """Check if a channel has exhausted its daily quota."""
    try:
        usage = get_daily_usage(channel_slug=channel_slug)
        return usage["remaining"] <= 0
    except Exception:
        return False


def should_throttle(channel_slug: str, threshold_pct: float = 0.85) -> bool:
    """Check if quota usage is above threshold and should throttle non-essential calls."""
    try:
        usage = get_daily_usage(channel_slug=channel_slug)
        used_pct = usage["total_units"] / max(usage["quota_limit"], 1)
        return used_pct >= threshold_pct
    except Exception:
        return False


# ── Decorator ────────────────────────────────────────────────────

def tracked(operation: str, units: int):
    """Decorator to track quota for a function that makes a YouTube API call.

    Usage:
        @tracked("videos.insert", 1600)
        def upload_video(self, video_file, ...): ...

    Auto-extracts channel_slug from first arg (self) or kwargs.
    """
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            # Try to extract channel_slug
            channel_slug = "unknown"
            try:
                # Try self.channel_slug (common pattern in this codebase)
                if args and hasattr(args[0], "channel_slug"):
                    channel_slug = args[0].channel_slug
                elif "channel_slug" in kwargs:
                    channel_slug = kwargs["channel_slug"]
                elif "canal" in kwargs:
                    channel_slug = kwargs["canal"]
            except Exception:
                pass

            success = True
            error = None
            yt_id = None

            try:
                result = func(*args, **kwargs)
                # Try to extract yt_video_id from result
                if isinstance(result, dict):
                    yt_id = result.get("yt_video_id") or result.get("id")
                elif isinstance(result, str) and len(result) == 11:
                    yt_id = result
                return result
            except Exception as exc:
                success = False
                error = str(exc)[:500]
                raise
            finally:
                track_quota(
                    channel_slug,
                    operation,
                    units,
                    yt_id=yt_id,
                    success=success,
                    error=error,
                    caller=func.__name__,
                )

        return wrapper
    return decorator
