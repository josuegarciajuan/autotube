"""
Unified priority-aware dispatch for shorts and long-form video generation.

When a worker completes (monitor detects terminal status), this module
decides what to generate next using a deterministic interleaving strategy:

    1. Batch ALL overdue shorts first: when a long-form video finishes,
       catch up on every pending short that has passed its scheduled time.
       Shorts are fast (~10 min) so batching them before the next long-form
       (~45 min) prevents the shorts queue from starving.
    
    2. Fallback to long-form: only when no shorts are dispatchable.

    3. Past-due long-form slots: ordered by target_public_at ASC
       → the video that should have been published earliest is generated first.

The actual sort order is enforced at the DB query level
(get_next_available_slot and get_next_pending_shorts_slot).
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("autotube.priority_dispatch")


# ── Interleaving dispatch ───────────────────────────────────────
_BATCH_MAX_SHORTS = 8  # safety cap: max overdue shorts dispatched per batch


def dispatch_next_priority_slot(db=None) -> dict[str, Any] | None:
    """Dispatch all overdue shorts after a long-form completes, then fall
    back to the next long-form slot.

    Batch strategy: when a long-form video finishes, loop-dispatch every
    due pending short until no more are dispatchable (guards, cooldown,
    or no pending slots). Each dispatched short runs as its own fire-and-
    forget async task. Once the batch is exhausted, start the next
    long-form.

    Returns:
        Dispatch result dict (slot_id, job_id, channel_slug, …),
        or None if nothing is ready to dispatch.
    """
    if db is None:
        from database.db_extended import ExtendedDatabase  # noqa: F811
        db = ExtendedDatabase()

    # ── Phase 1: batch ALL overdue shorts ───────────────────────
    dispatched_shorts: list[dict[str, Any]] = []

    for _ in range(_BATCH_MAX_SHORTS):
        from api.services.shorts_scheduler import dispatch_next_due_shorts_slot  # noqa: F811
        result = dispatch_next_due_shorts_slot(db=db)
        if result is None:
            break
        dispatched_shorts.append(result)

    if dispatched_shorts:
        count = len(dispatched_shorts)
        last = dispatched_shorts[-1]
        logger.info(
            "Priority dispatch: batched %d overdue short%s. "
            "Last: slot#%d ch=%s type=%s",
            count, "" if count == 1 else "s",
            last["slot_id"],
            last.get("channel_slug", "?"),
            last.get("short_type", "?"),
        )
        return dispatched_shorts[0]

    # ── Phase 2: fallback to long-form ──────────────────────────
    long_candidate = db.get_next_available_slot(max_future_hours=36)
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
            # v26 fix: log the slot that was ACTUALLY dispatched.
            # process_planned_slots performs its own round-robin priority
            # selection, so the dispatched slot may differ from the
            # candidate we probed above (get_next_available_slot).
            logger.info(
                "Priority dispatch: long-form slot #%s dispatched (channel=%s, pub=%s)",
                result.get("slot_id"),
                result.get("channel_slug", "?"),
                (str(result.get("target_public_at")) or "?")[:16],
            )
            return result

    logger.debug("Priority dispatch: no dispatchable slots (all candidates blocked or absent)")
    return None
