"""
Unified priority-aware dispatch for shorts and long-form video generation.

When a worker completes (monitor detects terminal status), this module
decides what to generate next based on urgency and estimated duration.

Priority formula:
    urgency = minutes_overdue × 2.0 + 720 / max(1, minutes_until_target)
    priority = urgency / estimated_duration_minutes

    • minutes_overdue = how long the slot has been past its scheduled_at
    • deadline_proximity = 720 / minutes_until_target — spikes near deadline
      (e.g. 30 min before target = 24 pts, 5 min before = 144 pts)
    • estimated_duration = 10 min for shorts, 45 min for long-form

This gives shorts a natural ~4.5× boost because they generate faster,
but a critically overdue long-form video can still out-prioritize
a non-urgent short when its deadline is imminent.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

logger = logging.getLogger("autotube.priority_dispatch")

# ── Constants ──────────────────────────────────────────────────
SHORT_ESTIMATED_MINUTES = 10
LONG_ESTIMATED_MINUTES = 45
DEADLINE_BASE = 720.0  # 12h baseline for deadline proximity


def _calc_priority(slot: dict[str, Any], is_short: bool, now: datetime) -> float:
    """Calculate a numeric priority score for a slot.

    Higher score == more urgent. The score is dimensionless; only
    relative ordering matters.
    """
    scheduled_at = slot.get("scheduled_at")
    # shorts table uses target_upload_at, long-form uses target_public_at
    target_at = slot.get("target_public_at") or slot.get("target_upload_at")

    minutes_overdue: float = 0.0
    if scheduled_at:
        try:
            s_dt = datetime.fromisoformat(str(scheduled_at))
            minutes_overdue = max(0.0, (now - s_dt).total_seconds() / 60)
        except (ValueError, TypeError, OSError):
            pass

    deadline_proximity: float = 0.0
    if target_at:
        try:
            t_dt = datetime.fromisoformat(str(target_at))
            minutes_until = max(1.0, (t_dt - now).total_seconds() / 60)
            deadline_proximity = DEADLINE_BASE / minutes_until
        except (ValueError, TypeError, OSError):
            pass

    urgency = minutes_overdue * 2.0 + deadline_proximity
    estimated_minutes = SHORT_ESTIMATED_MINUTES if is_short else LONG_ESTIMATED_MINUTES
    priority = urgency / estimated_minutes

    logger.debug(
        "Priority calc: slot=%d type=%s overdue=%.0f deadline=%.1f → %.2f",
        slot["id"],
        "short" if is_short else "long",
        minutes_overdue,
        deadline_proximity,
        priority,
    )
    return priority


def dispatch_next_priority_slot(db=None) -> dict[str, Any] | None:
    """Dispatch the most urgent pending slot (short or long-form).

    Finds the next pending short and long-form candidate, scores each
    by priority, then dispatches the highest-scoring one. Falls back
    to the next candidate if the top one is blocked by concurrency
    or memory guards.

    Returns:
        Dispatch result dict (slot_id, job_id, channel_slug, …),
        or None if nothing is ready to dispatch.
    """
    if db is None:
        from database.db_extended import ExtendedDatabase  # noqa: F811
        db = ExtendedDatabase()

    now = datetime.now()

    # ── Collect candidates ──────────────────────────────────────
    candidates: list[dict[str, Any]] = []

    short_candidate = db.get_next_pending_shorts_slot()
    if short_candidate:
        candidates.append(
            {
                "type": "short",
                "candidate": short_candidate,
                "priority": _calc_priority(short_candidate, is_short=True, now=now),
            }
        )

    long_candidate = db.get_next_available_slot(max_future_hours=36)
    if long_candidate:
        candidates.append(
            {
                "type": "long",
                "candidate": long_candidate,
                "priority": _calc_priority(long_candidate, is_short=False, now=now),
            }
        )

    if not candidates:
        logger.debug("No pending slots (shorts or long-form) due for dispatch")
        return None

    # ── Sort by priority descending ─────────────────────────────
    candidates.sort(key=lambda c: c["priority"], reverse=True)

    for entry in candidates:
        slot_type: str = entry["type"]
        slot: dict[str, Any] = entry["candidate"]
        pri: float = entry["priority"]

        logger.info(
            "Priority dispatch: trying %s slot #%d "
            "(channel=%s, priority=%.2f, scheduled=%s)",
            slot_type,
            slot["id"],
            slot.get("channel_slug", "?"),
            pri,
            (str(slot.get("scheduled_at")) or "?")[:16],
        )

        if slot_type == "short":
            from api.services.shorts_scheduler import dispatch_next_due_shorts_slot  # noqa: F811
            result = dispatch_next_due_shorts_slot(db=db)
        else:
            from api.services.planning_service import process_planned_slots  # noqa: F811
            result = process_planned_slots(db=db)

        if result:
            logger.info(
                "Priority dispatch: %s slot #%d dispatched (priority=%.2f)",
                slot_type,
                slot["id"],
                pri,
            )
            return result

    logger.debug("Priority dispatch: all candidates blocked by concurrency/memory guards")
    return None
