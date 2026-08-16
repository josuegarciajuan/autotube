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
import os
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

# Default daily quota per project (YouTube Data API v3).
# Fase 2 (ago 2026): era 10_000 (free tier), pero los canales consumen 30-55k ud/día
# sin bloqueos → tienen ampliación. Configurable vía YT_DAILY_QUOTA_LIMIT.
DEFAULT_DAILY_QUOTA = int(os.getenv("YT_DAILY_QUOTA_LIMIT", "10000"))


_project_cache: dict[str, str] = {}


def get_channel_project(channel_slug: str) -> str:
    """Resolve the GCP project for a channel (authoritative from client_secret)."""
    if channel_slug in _project_cache:
        return _project_cache[channel_slug]
    proj = "unknown"
    try:
        import json as _json
        from config.settings import PROJECT_ROOT
        for cand in (f"client_secret_{channel_slug}.json", "client_secret.json"):
            p = PROJECT_ROOT / "config" / cand
            if p.exists():
                d = _json.loads(p.read_text())
                found = (d.get("installed") or d.get("web") or {}).get("project_id")
                if found:
                    proj = found
                    break
    except Exception:
        pass
    _project_cache[channel_slug] = proj
    return proj


def quota_day_pacific(now: datetime | None = None) -> str:
    """Return the YouTube quota day, which resets at midnight Pacific Time."""
    from zoneinfo import ZoneInfo
    current = now or datetime.now(timezone.utc)
    return current.astimezone(ZoneInfo("America/Los_Angeles")).date().isoformat()


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
                    project_id: Optional[str] = None,
                    date: Optional[str] = None) -> dict:
    """Get YouTube API quota usage for today (or a specific date).

    Fase cuota (ago 2026): channel_slug / project_id AHORA filtran de verdad
    (antes se ignoraban y TODOS los throttles operaban con el total global,
    lo que hacía que los guards por canal nunca se aplicaran como se esperaba).

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
        # Quota day is Pacific (resets at midnight PT = 07:00 UTC). Grouping by
        # the server-local date (Europe/Madrid) miscounts usage between local
        # midnight and the PT reset, tripping the throttle governor on the wrong
        # day. Timestamps are stored in server-local time (Europe/Madrid, UTC+2),
        # which is 9h ahead of Pacific (UTC-7), so shift back 9h before grouping.
        date = quota_day_pacific()

    # ── Build filter (channel or project) ──
    if channel_slug:
        slug_filter = "AND channel_slug = ?"
        slug_params: list = [channel_slug]
    elif project_id:
        # usage por proyecto: filtrar por los canales de ese proyecto
        slugs: list[str] = []
        try:
            from database.db_extended import ExtendedDatabase as _EDB
            _pdb = _EDB()
            for ch in (_pdb.get_channels(active_only=False) or []):
                s = ch.get("slug")
                if s and get_channel_project(s) == project_id:
                    slugs.append(s)
        except Exception:
            pass
        if not slugs:
            slugs = ["__none__"]
        placeholders = ",".join("?" for _ in slugs)
        slug_filter = f"AND channel_slug IN ({placeholders})"
        slug_params = slugs
    else:
        slug_filter = ""
        slug_params = []

    quota_limit = DEFAULT_DAILY_QUOTA
    if project_id:
        try:
            from config.settings import get_project_budget_units
            quota_limit = get_project_budget_units(project_id)
        except Exception:
            pass

    with db._connect() as conn:
        conn.row_factory = None

        # ── Per-channel total ──
        by_channel = {}
        rows = conn.execute(
            "SELECT channel_slug, SUM(units) FROM yt_quota_log "
            f"WHERE date(timestamp, '-9 hours') = ? {slug_filter} AND success = 1 "
            "GROUP BY channel_slug",
            (date, *slug_params),
        ).fetchall()
        for ch, total in rows:
            by_channel[ch] = total or 0

        # ── Per-operation total ──
        by_operation = {}
        rows = conn.execute(
            "SELECT operation, SUM(units) FROM yt_quota_log "
            f"WHERE date(timestamp, '-9 hours') = ? {slug_filter} AND success = 1 "
            "GROUP BY operation",
            (date, *slug_params),
        ).fetchall()
        for op, total in rows:
            by_operation[op] = total or 0

        # ── Per-hour breakdown ──
        by_hour = {}
        rows = conn.execute(
            "SELECT strftime('%H', timestamp) AS h, SUM(units) FROM yt_quota_log "
            f"WHERE date(timestamp, '-9 hours') = ? {slug_filter} AND success = 1 "
            "GROUP BY h ORDER BY h",
            (date, *slug_params),
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
        if hourly_rate > 0 and total < quota_limit:
            remaining = quota_limit - total
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
        "quota_limit": quota_limit,
        "remaining": max(quota_limit - total, 0),
        "exhausted_estimated_at": exhausted_estimated_at,
    }


def get_project_usage(db=None, project_id: Optional[str] = None,
                      date: Optional[str] = None) -> dict:
    """Consumo del día-PT de un proyecto GCP concreto (o todos si None)."""
    return get_daily_usage(db=db, project_id=project_id, date=date)


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
    """Check if a channel's GCP project has exhausted its daily quota."""
    try:
        usage = get_daily_usage(project_id=get_channel_project(channel_slug))
        return usage["remaining"] <= 0
    except Exception:
        return False


def should_throttle(channel_slug: str, threshold_pct: float = 0.85) -> bool:
    """Check if the channel's PROJECT usage is above threshold.

    Fase cuota (ago 2026): la cuota es por proyecto; antes se computaba el
    total global ignorando el filtro, así que los throttles "por canal"
    saltaban por consumo de OTROS proyectos.
    """
    try:
        usage = get_daily_usage(project_id=get_channel_project(channel_slug))
        used_pct = usage["total_units"] / max(usage["quota_limit"], 1)
        return used_pct >= threshold_pct
    except Exception:
        return False


# ── Guard helpers for callers ────────────────────────────────────

# Thresholds for different operation tiers
QUOTA_CRITICAL = 0.85   # >85%: stop ALL non-essential operations
QUOTA_TIGHT = 0.70      # >70%: skip expensive extras (comments, per-video playlists)
QUOTA_CAUTION = 0.50    # >50%: skip purely cosmetic operations (thumbnail verify)


def should_skip_thumbnail_verify(channel_slug: str) -> bool:
    """True if quota is too low for thumbnail verification (cosmetic)."""
    return should_throttle(channel_slug, QUOTA_CAUTION)


def should_skip_short_comments(channel_slug: str) -> bool:
    """True if quota is too tight for first comments on shorts."""
    return should_throttle(channel_slug, QUOTA_TIGHT)


def should_skip_per_video_playlist(channel_slug: str) -> bool:
    """True if quota is too tight for per-video playlist creation."""
    return should_throttle(channel_slug, QUOTA_TIGHT)


def should_skip_all_cross_promote(channel_slug: str) -> bool:
    """True if quota is critical — skip ALL cross-promotion (playlists + comments)."""
    return should_throttle(channel_slug, QUOTA_CRITICAL)


def should_preserve_quota(channel_slug: str, threshold_pct: float = QUOTA_CAUTION) -> bool:
    """Convenience: True when quota is above threshold and non-essential ops should be paused."""
    return should_throttle(channel_slug, threshold_pct)


def should_throttle_global(threshold_pct: float = 0.85) -> bool:
    """Check if ANY GCP project exceeded its quota threshold.

    Fase cuota (ago 2026): la cuota es POR PROYECTO. Se compara el consumo
    de cada proyecto contra SU presupuesto real (YT_PROJECT_BUDGET_UNITS,
    default 10000). Antes todos los proyectos se comparaban contra 10000 fijo
    ignorando la cuota real configurada.
    """
    try:
        from config.settings import get_project_budget_units
        usage = get_daily_usage()  # no filter = all channels
        by_channel = usage.get("by_channel", {})
        project_units: dict[str, int] = {}
        for slug, units in by_channel.items():
            proj = get_channel_project(slug)
            project_units[proj] = project_units.get(proj, 0) + units
        for proj, units in project_units.items():
            budget = get_project_budget_units(proj)
            used_pct = units / max(budget, 1)
            if used_pct >= threshold_pct:
                logger.debug("Quota throttle: project '%s' at %.0f%%", proj, used_pct * 100)
                return True
        return False
    except Exception:
        return False


def project_has_free_capacity(project_id: str, min_free_pct: float = 15.0) -> bool:
    """True si el proyecto tiene al menos min_free_pct% de cuota libre hoy.

    Usado para gatear operaciones no esenciales (collab, comentarios...).
    """
    try:
        usage = get_daily_usage(project_id=project_id)
        used_pct = usage["total_units"] / max(usage["quota_limit"], 1)
        return (1.0 - used_pct) * 100 >= min_free_pct
    except Exception:
        return False


def in_pt_day_end_window(now: Optional[datetime] = None,
                         minutes_before_reset: int = 30) -> bool:
    """True si estamos en los últimos N minutos del día de cuota (PT).

    Anti ping-pong (Fase cuota ago 2026): la cuota del día anterior está casi
    con seguridad agotada en la última media hora antes del reset PT. Intentar
    subidas en esa ventana provoca un 403 → breaker → clear a las 07:15 UTC →
    reintento → 403… (el bucle que paralizó el sistema el 15-ago).
    Los dispatchers RETIENEN subidas en esta ventana sin fijar ningún breaker.
    """
    from zoneinfo import ZoneInfo
    from datetime import datetime as _dt, timezone as _tz
    now = now or _dt.now(_tz.utc)
    pt = now.astimezone(ZoneInfo("America/Los_Angeles"))
    minutes_to_midnight = (24 * 60 - (pt.hour * 60 + pt.minute)) % (24 * 60)
    return minutes_to_midnight <= minutes_before_reset


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
