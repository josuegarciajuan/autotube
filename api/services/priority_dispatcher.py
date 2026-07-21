"""
Unified priority-aware dispatch for shorts and long-form video generation.

When a worker completes (monitor detects terminal status), this module
decides what to generate next using a deterministic interleaving strategy:

    1. Past-due long-form slots: ordered by target_public_at ASC
       → the video that should have been published earliest is generated first.
    2. Past-due shorts slots: ordered by target_upload_at ASC
       → the short with the nearest upload date is generated first.
    3. Interleaving: shorts are ALWAYS tried before long-form.
       Since shorts generate fast (~10 min) and long-form takes ~45 min,
       this naturally intersperses shorts between long-form videos without
       starving either queue.

The actual sort order is enforced at the DB query level
(get_next_available_slot and get_next_pending_shorts_slot).
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("autotube.priority_dispatch")


# ── Interleaving dispatch ───────────────────────────────────────

def dispatch_next_priority_slot(db=None) -> dict[str, Any] | None:
    """Dispatch the most urgent pending slot (short preferred over long-form).

    Collects the next pending short and long-form candidates. Shorts are
    always tried first because they are fast to generate and have tight
    publish windows. Falls back to long-form if no short is ready.

    Both candidates are already sorted correctly by the DB queries:
      - Shorts: target_upload_at ASC (nearest upload date first)
      - Long-form: past-due first, then target_public_at ASC

    Returns:
        Dispatch result dict (slot_id, job_id, channel_slug, …),
        or None if nothing is ready to dispatch.
    """
    if db is None:
        from database.db_extended import ExtendedDatabase  # noqa: F811
        db = ExtendedDatabase()

    # ── Collect candidates ──────────────────────────────────────
    short_candidate = db.get_next_pending_shorts_slot()
    long_candidate = db.get_next_available_slot(max_future_hours=36)

    # ── Shorts first (interleaving strategy) ────────────────────
    if short_candidate:
        slot_id = short_candidate["id"]
        slug = short_candidate.get("channel_slug", "?")
        scheduled = (str(short_candidate.get("scheduled_at")) or "?")[:16]
        target_upload = (str(short_candidate.get("target_upload_at")) or "?")[:16]

        logger.info(
            "Priority dispatch: trying short slot #%d (channel=%s, upload=%s, scheduled=%s)",
            slot_id, slug, target_upload, scheduled,
        )

        from api.services.shorts_scheduler import dispatch_next_due_shorts_slot  # noqa: F811
        result = dispatch_next_due_shorts_slot(db=db)
        if result:
            logger.info(
                "Priority dispatch: short slot #%d dispatched (channel=%s, type=%s)",
                slot_id, slug, result.get("short_type", "?"),
            )
            return result

    # ── Fallback to long-form ───────────────────────────────────
    if long_candidate:
        slot_id = long_candidate["id"]
        slug = long_candidate.get("channel_slug", "?")
        pub_at = (str(long_candidate.get("target_public_at")) or "?")[:16]
        scheduled = (str(long_candidate.get("scheduled_at")) or "?")[:16]

        logger.info(
            "Priority dispatch: trying long-form slot #%d (channel=%s, pub=%s, scheduled=%s)",
            slot_id, slug, pub_at, scheduled,
        )

        from api.services.planning_service import process_planned_slots  # noqa: F811
        result = process_planned_slots(db=db)
        if result:
            logger.info(
                "Priority dispatch: long-form slot #%d dispatched (channel=%s)",
                slot_id, slug,
            )
            return result

    logger.debug("Priority dispatch: no dispatchable slots (all candidates blocked or absent)")
    return None
