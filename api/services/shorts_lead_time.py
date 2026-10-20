"""Per-channel render+upload lead time for planned shorts.

Planned shorts are atomic jobs: a slot stores the target PUBLIC publish time
and the dispatcher starts it ``lead_time`` earlier. The lead must cover the
whole job (script + TTS + media + render + YouTube upload), so this module
estimates it from real successful jobs instead of a hardcoded constant.

Estimator rules
---------------
* Window: successful ``generate_native_short`` jobs from the last 45 days.
* Duration: ``upload_finished_at - started_at`` when the upload phase is
  instrumented, else ``finished_at - started_at`` (the direct render+upload
  path finishes after the upload).
* Statistic: **p90**, not the mean. A mean would make roughly half of the
  publishes late; p90 keeps public times inside their franja.
* Sample floor: 20 clean samples per channel. Below that, pool all channels;
  below that, use a safe constant.
* A small configurable margin covers scheduler tick latency, auth and jitter.
"""

from __future__ import annotations

import logging
import math
import os
import time
from typing import Iterable

logger = logging.getLogger("autotube.shorts_lead_time")

MIN_SAMPLES = 20
LOOKBACK_DAYS = 45
LEAD_MARGIN_MIN = int(os.getenv("SHORTS_LEAD_MARGIN_MIN", "10") or 10)
FALLBACK_LEAD_MIN = int(os.getenv("SHORTS_LEAD_FALLBACK_MIN", "60") or 60)
MIN_LEAD_MIN = int(os.getenv("SHORTS_LEAD_MIN_MIN", "15") or 15)
MAX_LEAD_MIN = int(os.getenv("SHORTS_LEAD_MAX_MIN", "180") or 180)
CACHE_TTL_SEC = 1800

# A job shorter than 30 s cannot include a real render; longer than 3 h is an
# outlier (worker stalled / API restart) and would inflate the lead for days.
_MIN_VALID_SEC = 30
_MAX_VALID_SEC = 3 * 3600

_cache: dict[tuple[str, int], tuple[float, int, int]] = {}


def _p90(values: list[float]) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    idx = max(0, math.ceil(0.9 * len(ordered)) - 1)
    return ordered[min(idx, len(ordered) - 1)]


def _db_key(db) -> str:
    path = getattr(db, "db_path", None) or getattr(db, "path", None)
    return str(path or "default")


def _query_durations(db, channel_id: int | None) -> list[float]:
    """Return clean end-to-end durations (seconds) for successful native jobs."""
    where = [
        "action = 'generate_native_short'",
        "status = 'completed'",
        "started_at IS NOT NULL",
        f"created_at >= datetime('now', '-{LOOKBACK_DAYS} days')",
    ]
    params: list = []
    if channel_id is not None:
        where.append("channel_id = ?")
        params.append(int(channel_id))
    sql = f"""
        SELECT started_at, finished_at, render_finished_at,
               upload_started_at, upload_finished_at
          FROM generation_jobs
         WHERE {' AND '.join(where)}
    """
    try:
        with db._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
    except Exception as exc:
        logger.debug("lead-time query failed: %s", exc)
        return []

    durations: list[float] = []
    for row in rows:
        def _ts(key):
            value = row[key] if not isinstance(row, dict) else row.get(key)
            return str(value)[:19] if value else None

        start = _ts("started_at")
        end = _ts("upload_finished_at") or _ts("finished_at")
        if not start or not end:
            continue
        try:
            from datetime import datetime
            fmt = "%Y-%m-%d %H:%M:%S"
            delta = (
                datetime.strptime(end.replace("T", " "), fmt)
                - datetime.strptime(start.replace("T", " "), fmt)
            ).total_seconds()
        except (TypeError, ValueError):
            continue
        if _MIN_VALID_SEC <= delta <= _MAX_VALID_SEC:
            durations.append(float(delta))
    return durations


def _to_lead_minutes(durations: Iterable[float]) -> int:
    p90_sec = _p90(list(durations))
    if p90_sec <= 0:
        return 0
    return int(math.ceil(p90_sec / 60.0)) + LEAD_MARGIN_MIN


def estimate_channel_lead_minutes(channel_id: int, db=None) -> int:
    """Lead time (minutes) to start a planned native short before publishing."""
    if db is None:
        from database.db_extended import ExtendedDatabase
        db = ExtendedDatabase()

    key = (_db_key(db), int(channel_id))
    now = time.time()
    cached = _cache.get(key)
    if cached and now - cached[0] < CACHE_TTL_SEC:
        return cached[1]

    samples = _query_durations(db, int(channel_id))
    source = "channel"
    if len(samples) < MIN_SAMPLES:
        samples = _query_durations(db, None)
        source = "global" if len(samples) >= MIN_SAMPLES else "fallback"

    lead = _to_lead_minutes(samples)
    if lead <= 0:
        lead = FALLBACK_LEAD_MIN
        source = "fallback"
    lead = max(MIN_LEAD_MIN, min(MAX_LEAD_MIN, lead))

    _cache[key] = (now, lead, len(samples))
    logger.info(
        "[shorts lead] channel=%d source=%s samples=%d lead=%dmin",
        channel_id, source, len(samples), lead,
    )
    return lead


def lead_time_diagnostics(channel_id: int, db=None) -> dict:
    """Non-cached breakdown for the panel/API."""
    if db is None:
        from database.db_extended import ExtendedDatabase
        db = ExtendedDatabase()
    samples = _query_durations(db, int(channel_id))
    return {
        "channel_id": int(channel_id),
        "samples": len(samples),
        "p90_minutes": round(_p90(samples) / 60.0, 1) if samples else None,
        "lead_minutes": estimate_channel_lead_minutes(channel_id, db),
        "margin_minutes": LEAD_MARGIN_MIN,
        "fallback_minutes": FALLBACK_LEAD_MIN,
    }


def invalidate_cache() -> None:
    _cache.clear()
