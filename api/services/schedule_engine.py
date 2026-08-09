"""
Per-channel adaptive scheduling engine.
Computes 2 publish slots/day/channel with:
- Per-channel avg creation time (last 3 uploaded videos)
- 6-day fair rotation
- ±15 min deterministic jitter
- 15% buffer on creation time
- Sequential, non-overlapping execution
"""

import json
import hashlib
import logging
from datetime import date, datetime, timedelta
from typing import Optional

logger = logging.getLogger("autotube.schedule_engine")

# ── Dispatch lock (serializes all generation dispatches) ────────
from api.services.generation_service import _DISPATCH_LOCK

# ── Target windows (Europe/Madrid local time) ──────────────
TARGET_WINDOWS = [
    (16, 0),    # afternoon slot (target ~16:00)
    (21, 30),   # prime time slot (target ~21:30)
]

BUFFER_PCT = 0.15       # 15% safety margin on avg creation time
JITTER_MINUTES = 15     # ±15 min deterministic jitter on target publish time


def _build_rotation(day_index: int, active_slugs: list[str]) -> tuple[list[str], list[str]]:
    """Dynamically generate a fair rotation of channels for the given day.
    
    Generates all permutations of active slugs, sorted deterministically,
    and cycles through them. Slot B is the reverse of Slot A to ensure
    no channel always gets the same position.
    
    Args:
        day_index: Day ordinal or index used to select the permutation.
        active_slugs: List of active channel slugs sorted alphabetically.
    
    Returns:
        (slot_a_order, slot_b_order) where slot_b is the reverse of slot_a.
    """
    import itertools
    
    slugs = sorted(active_slugs)
    n = len(slugs)
    
    if n == 0:
        return ([], [])
    if n == 1:
        return (slugs, slugs)
    
    # Generate all permutations of N slugs (N! total)
    all_perms = list(itertools.permutations(slugs))
    # Deterministic ordering
    all_perms.sort()
    
    # Pick permutation for this day
    perm_idx = day_index % len(all_perms)
    slot_a = list(all_perms[perm_idx])
    slot_b = list(reversed(slot_a))
    
    return (slot_a, slot_b)


def get_avg_creation_minutes(channel_id: int, n: int = 3) -> float:
    """Return average total_duration_ms (in minutes) of last N uploaded videos.

    Falls back to 120 min if no timing data available.
    """
    import sqlite3
    from config.settings import DATABASE_PATH

    conn = sqlite3.connect(str(DATABASE_PATH), timeout=60)
    conn.row_factory = sqlite3.Row

    rows = conn.execute(
        """SELECT timing_data FROM videos 
           WHERE channel_id = ? AND status = 'uploaded' 
           AND timing_data IS NOT NULL AND timing_data != '{}' AND timing_data != ''
           ORDER BY id DESC LIMIT ?""",
        (channel_id, n),
    ).fetchall()
    conn.close()

    if not rows:
        logger.warning("No timing data for channel_id=%d, using default 180 min", channel_id)
        return 180.0

    total_ms = 0
    count = 0
    for row in rows:
        try:
            td = json.loads(row["timing_data"])
            ms = td.get("total_duration_ms", 0)
            if ms > 0:
                total_ms += ms
                count += 1
        except (json.JSONDecodeError, TypeError):
            pass

    if count == 0:
        logger.warning("No valid timing_data for channel_id=%d, using default 180 min", channel_id)
        return 180.0

    avg_min = (total_ms / count) / 60000.0
    logger.info("channel_id=%d: avg creation = %.1f min (from %d videos)", channel_id, avg_min, count)
    return avg_min


def _day_seed(date_str: str, channel_slug: str, slot_idx: int) -> int:
    """Deterministic seed for a date+channel+slot combination."""
    h = hashlib.md5(f"{date_str}::{channel_slug}::{slot_idx}".encode()).hexdigest()
    return int(h[:8], 16)


def _jitter_minutes(date_str: str, channel_slug: str, slot_idx: int) -> int:
    """Return deterministic jitter in minutes (-JITTER_MINUTES .. +JITTER_MINUTES)."""
    seed = _day_seed(date_str, channel_slug, slot_idx)
    return (seed % (2 * JITTER_MINUTES + 1)) - JITTER_MINUTES


def _channel_avg_for_scheduling(db, channels: list[dict]) -> dict:
    """Build slug -> {id, name, avg_min, buffered_min} map."""
    ch_info = {}
    for ch in channels:
        avg = get_avg_creation_minutes(ch["id"])
        ch_info[ch["slug"]] = {
            "id": ch["id"],
            "name": ch.get("name", ch["slug"]),
            "avg_min": avg,
            "buffered_min": avg * (1 + BUFFER_PCT),
        }
    return ch_info


def compute_daily_schedule(date_str: str, db=None) -> list[dict]:
    """Compute 6 planned slots for a given date (YYYY-MM-DD).

    Returns list of slot dicts with keys:
      channel_id, date_key, scheduled_at, target_upload_at, slot_position,
      channel_name, channel_slug.
    """
    if db is None:
        from database.db_extended import ExtendedDatabase
        db = ExtendedDatabase()

    # Get active channels (exclude test)
    channels = db.get_channels(active_only=True)
    channels = [ch for ch in channels if ch["slug"] != "test"]

    if len(channels) < 1:
        logger.warning("No active channels found — cannot compute schedule")
        return []

    ch_info = _channel_avg_for_scheduling(db, channels)

    # Build active slugs list (deterministically sorted)
    active_slugs = sorted(ch["slug"] for ch in channels)

    # Determine rotation index for this date
    d = datetime.strptime(date_str, "%Y-%m-%d").toordinal()
    slot_a_order, slot_b_order = _build_rotation(d, active_slugs)
    if not slot_a_order or not slot_b_order:
        logger.warning("No active channels match rotation slugs")
        return []

    logger.info(
        "Day %s: A=%s  B=%s",
        date_str,
        "→".join(slot_a_order), "→".join(slot_b_order),
    )

    all_slots = []
    global_slot_pos = 0

    # ── Compute each window independently ──────────────────────
    window_schedules = []  # will hold (scheduled_list, target_datetimes) per window

    for window_idx, (target_h, target_m) in enumerate(TARGET_WINDOWS):
        order = slot_a_order if window_idx == 0 else slot_b_order

        # Compute jittered target times per channel
        target_datetimes = []
        for slug in order:
            jitter = _jitter_minutes(date_str, slug, window_idx)
            total_min = target_h * 60 + target_m + jitter
            # Clamp to valid range
            if total_min < 0:
                total_min = 0
            if total_min >= 24 * 60:
                total_min = 24 * 60 - 1
            h = total_min // 60
            m = total_min % 60
            target_datetimes.append(
                datetime.strptime(f"{date_str} {h:02d}:{m:02d}:00", "%Y-%m-%d %H:%M:%S")
            )

        # Work backwards: last channel finishes at its jittered target,
        # each previous channel finishes before the next starts
        scheduled = []

        next_start_time = None
        for i in range(len(order) - 1, -1, -1):
            slug = order[i]
            buffered = ch_info[slug]["buffered_min"]

            if i == len(order) - 1:
                # Anchor: finish at target time
                finish = target_datetimes[i]
            else:
                # Finish when the next channel starts
                finish = next_start_time

            start = finish - timedelta(minutes=buffered)
            next_start_time = start
            scheduled.insert(0, (slug, start, finish))

        window_schedules.append(scheduled)

    # ── Cross-slot collision resolution ────────────────────────
    # Slot B's first job must start AFTER Slot A's last job finishes.
    # Since only one generation runs at a time, push Slot B forward
    # if there's an overlap.
    slot_a_last = window_schedules[0][-1]   # (slug, start, finish)
    slot_b_first = window_schedules[1][0]   # (slug, start, finish)

    slot_a_finish = slot_a_last[2]  # finish time of Slot A's last job
    slot_b_start = slot_b_first[1]  # start time of Slot B's first job

    if slot_b_start <= slot_a_finish:
        # Gap needed: at least 1 minute between jobs
        gap = timedelta(minutes=1)
        shift = (slot_a_finish + gap) - slot_b_start
        logger.info(
            "Cross-slot collision: Slot A finishes %s, Slot B wants to start %s → shifting by %.0f min",
            slot_a_finish.strftime("%H:%M"), slot_b_start.strftime("%H:%M"),
            shift.total_seconds() / 60,
        )

        # Shift all Slot B jobs forward by the collision amount
        shifted_b = []
        for slug, start, finish in window_schedules[1]:
            shifted_b.append((slug, start + shift, finish + shift))
        window_schedules[1] = shifted_b

    # ── Build slot dicts ───────────────────────────────────────
    for window_idx, scheduled in enumerate(window_schedules):
        for slug, start, finish in scheduled:
            global_slot_pos += 1
            all_slots.append({
                "channel_id": ch_info[slug]["id"],
                "date_key": date_str,
                "scheduled_at": start.strftime("%Y-%m-%d %H:%M:%S"),
                "target_upload_at": finish.strftime("%Y-%m-%d %H:%M:%S"),
                "target_public_at": finish.strftime("%Y-%m-%d %H:%M:%S"),
                "slot_position": global_slot_pos,
                "channel_name": ch_info[slug]["name"],
                "channel_slug": slug,
                "upload_window_start": 9,
                "upload_window_end": 11,
            })

    # Sort all slots by scheduled_at
    all_slots.sort(key=lambda s: s["scheduled_at"])

    # Re-number slot positions after final sort
    for pos, s in enumerate(all_slots, 1):
        s["slot_position"] = pos

    return all_slots


def persist_daily_schedule(date_str: str, slots: list[dict], db=None) -> int:
    """Store computed slots in planned_slots table.
    Deletes existing pending slots for this date first.
    Returns count of stored slots.
    """
    if db is None:
        from database.db_extended import ExtendedDatabase
        db = ExtendedDatabase()

    # Delete existing PENDING slots for this date (keep completed/running)
    with db._connect() as conn:
        conn.execute(
            "DELETE FROM planned_slots WHERE date_key = ? AND status = 'pending'",
            (date_str,),
        )
        conn.commit()

    if not slots:
        return 0

    count = db.create_planned_slots_batch(slots)
    logger.info("Persisted %d slots for %s", count, date_str)
    return count


def generate_upcoming(days: int = 7, db=None) -> dict:
    """Generate and persist slots for the next N days (including today).

    Returns summary dict: {date_str: "N slots" | "ERROR: ..."}.
    """
    if db is None:
        from database.db_extended import ExtendedDatabase
        db = ExtendedDatabase()

    today = date.today()
    results = {}

    for day_offset in range(days):
        day_str = (today + timedelta(days=day_offset)).isoformat()
        try:
            slots = compute_daily_schedule(day_str, db)
            count = persist_daily_schedule(day_str, slots, db)
            results[day_str] = f"{count} slots"
        except Exception as e:
            logger.error("Failed to generate schedule for %s: %s", day_str, e)
            results[day_str] = f"ERROR: {e}"

    total = sum(
        int(v.split()[0]) for v in results.values() if v[0].isdigit()
    )
    logger.info(
        "Generated slots for %d days: %d total — %s",
        days, total,
        {k: v for k, v in list(results.items())[:3]},
    )
    return results


def ensure_today_scheduled(db=None) -> bool:
    """Check if today has planned slots. If not, generate them.
    Returns True if slots exist (existing or newly generated).
    """
    if db is None:
        from database.db_extended import ExtendedDatabase
        db = ExtendedDatabase()

    today = date.today().isoformat()
    existing = db.get_planned_slots(date_key=today)

    # Count non-cancelled slots
    active_slots = [s for s in existing if s["status"] != "cancelled"]
    if len(active_slots) >= 6:
        logger.debug("Today's schedule OK: %d active slots", len(active_slots))
        return True

    # Need to regenerate
    logger.info(
        "Regenerating today's schedule (%d existing, %d active)",
        len(existing), len(active_slots),
    )
    slots = compute_daily_schedule(today, db)
    count = persist_daily_schedule(today, slots, db)
    return count > 0


def get_day_schedule_summary(date_str: str = None, db=None) -> str:
    """Human-readable summary of the day's schedule."""
    if date_str is None:
        date_str = date.today().isoformat()
    if db is None:
        from database.db_extended import ExtendedDatabase
        db = ExtendedDatabase()

    slots = db.get_planned_slots(date_key=date_str)
    if not slots:
        return f"No hay horarios programados para {date_str}."

    lines = [f"Horario {date_str} (Europe/Madrid)"]
    status_emoji = {
        "pending": "-", "running": "*", "completed": "+", "cancelled": "x",
    }

    for s in slots:
        em = status_emoji.get(s["status"], "?")
        sched = s["scheduled_at"][11:16] if s["scheduled_at"] else "??:??"
        target = s.get("target_upload_at", "")
        target = target[11:16] if target else "??:??"
        lines.append(
            f"  {em} gen={sched}  pub={target}  [{s['channel_name']}]"
        )

    return "\n".join(lines)


# ── Smart slot dispatcher ──────────────────────────────────────

def dispatch_next_due_slot(db=None) -> dict | None:
    """Check for due planned slots and dispatch ONE generation job.

    Called every 5 min by the API checker loop.
    Only dispatches if no job is currently running.

    Returns:
        dict with dispatched slot info, or None if nothing to do.
    """
    import asyncio
    import sqlite3
    import time as _time
    from config.settings import DATABASE_PATH

    if db is None:
        from database.db_extended import ExtendedDatabase
        db = ExtendedDatabase()

    # 1. Sync running slots: mark completed/failed based on job status
    _sync_running_slots(db)

    # 2. Cancel stale pending slots (>3h past scheduled_at)
    _cancel_stale_slots(db)

    # 3. Memory gate
    if not _memory_ok():
        logger.warning("Low memory — delaying planned slot dispatch")
        return None

    # 5. Get next pending slot that is due
    next_slot = db.get_next_pending_slot()
    if not next_slot:
        logger.debug("No pending slots due")
        return None

    # 5b. Per-channel guard: skip if this channel already has an active job
    active = db.get_active_job_for_channel(next_slot["channel_id"])
    if active:
        logger.debug("Smart dispatch skipped: channel %d already has active job #%d",
                     next_slot["channel_id"], active["id"])
        return None

    # 5c. Global guard: defer if ANY generation is running across all channels
    active_count = db.count_active_longform_jobs()
    if active_count > 0:
        logger.info("Smart dispatch deferred: %d active job(s) running — retrying next tick",
                    active_count)
        return None

    # ── Enter dispatch critical section ──────────────────────────
    with _DISPATCH_LOCK:
        # Re-check global guard under lock (belts-and-suspenders)
        if db.count_active_longform_jobs() > 0:
            logger.info("Smart dispatch deferred (under lock): active job detected")
            return None

        slot_id = next_slot["id"]
        channel_id = next_slot["channel_id"]
        slug = next_slot.get("channel_slug", "")
        scheduled = next_slot.get("scheduled_at", "?")

        logger.info(
            "Dispatching slot #%d: %s (scheduled %s, now %s late)",
            slot_id, slug, scheduled,
            f"{_time_since(scheduled):.0f} min" if _time_since(scheduled) else "on time",
        )

        # 6. Mark slot as running
        db.update_slot_status(slot_id, "running")

        # 7. Create video record with correct publish_mode from channel config
        from config.config_bridge import get_channel_config
        ch_cfg = get_channel_config(slug)
        publish_mode = getattr(ch_cfg, "PUBLISH_MODE", "scheduled")
        conn = sqlite3.connect(str(DATABASE_PATH), timeout=30)
        cursor = conn.execute(
            "INSERT INTO videos (canal, channel_id, video_path, status, progress, publish_mode, created_at) "
            "VALUES (?, ?, '', 'generating', 0, ?, CURRENT_TIMESTAMP)",
            (slug, channel_id, publish_mode),
        )
        conn.commit()
        video_id = cursor.lastrowid
        conn.close()

        # 8. Create job
        job_id = db.create_job(channel_id, "generate_and_upload", video_id)
        
        # 9. Mark job as running IMMEDIATELY to prevent _queue_consumer from 
        #    picking it up (race condition in the checker loop).
        db.update_job(job_id, status="running")

        # 10. Link job to slot
        db.update_slot_status(slot_id, "running", job_id=job_id, video_id=video_id)
    # ── End dispatch critical section ────────────────────────────

    # 11. Fire and forget the generation
    from api.services.generation_service import (
        start_generation_job,
        start_generation_job_subprocess,
        USE_SUBPROCESS_WORKER,
    )

    if USE_SUBPROCESS_WORKER:
        asyncio.create_task(
            start_generation_job_subprocess(
                job_id=job_id,
                channel_id=channel_id,
                video_id=video_id,
                action="generate_and_upload",
            )
        )
    else:
        asyncio.create_task(
            start_generation_job(
                job_id=job_id,
                channel_id=channel_id,
                video_id=video_id,
                action="generate_and_upload",
            )
        )

    return {
        "slot_id": slot_id,
        "job_id": job_id,
        "video_id": video_id,
        "channel_slug": slug,
    }


def _sync_running_slots(db):
    """Check running slots: if their job is done, mark the slot accordingly.
    
    For transient failures (RAM, timeout, etc.), applies exponential backoff
    via dispatch cooldown instead of immediately cancelling the slot.
    """
    from datetime import date as _date
    today = _date.today().isoformat()

    running_slots = db.get_planned_slots(date_key=today, status="running")
    if not running_slots:
        return

    # Transient error patterns (same as generation_service._auto_retry_if_transient)
    TRANSIENT_PATTERNS = [
        "timeout", "memory guard", "broken pipe", "brokenpipe",
        "orphaned: process lost", "memory", "abortado: memoria",
        "ram too low", "ram insuficiente",
    ]

    for s in running_slots:
        if not s.get("job_id"):
            # No job linked at all → stale running slot, mark cancelled
            db.update_slot_status(s["id"], "cancelled")
            logger.info("Slot #%d cancelled (no job linked)", s["id"])
            continue
        job = db.get_job(s["job_id"])
        if not job:
            # Job row missing → stale running slot, mark cancelled
            db.update_slot_status(s["id"], "cancelled")
            logger.info("Slot #%d cancelled (job #%d not found)", s["id"], s["job_id"])
            continue
        if job["status"] in ("completed", "success"):
            db.update_slot_status(s["id"], "completed")
            logger.info("Slot #%d marked completed (job #%d done)", s["id"], s["job_id"])
        elif job["status"] in ("failed", "cancelled"):
            error_msg = (job.get("error_msg") or "").lower()
            is_transient = any(p in error_msg for p in TRANSIENT_PATTERNS)
            if is_transient:
                # Apply backoff instead of cancelling — v12
                result = db.record_slot_dispatch_failure(s["job_id"])
                logger.info(
                    "Slot #%d (%s) transient failure — backoff=%s (error: %.100s)",
                    s["id"], s["job_id"], result, error_msg,
                )
            else:
                db.update_slot_status(s["id"], "cancelled")
                logger.info("Slot #%d cancelled (job #%d %s)", s["id"], s["job_id"], job["status"])


def _cancel_stale_slots(db):
    """Cancel pending slots that are >3h past their scheduled_at.
    
    Skips when an active generation is in progress — slots blocked
    by the global concurrency guard are held intentionally.
    """
    # Don't cancel slots held back by an active generation
    if db.count_active_jobs() > 0:
        return
    
    today = date.today().isoformat()
    pending = db.get_planned_slots(date_key=today, status="pending")
    if not pending:
        return

    now = datetime.now()
    cancelled = 0
    for s in pending:
        try:
            sched = datetime.strptime(s["scheduled_at"], "%Y-%m-%d %H:%M:%S")
        except (ValueError, TypeError):
            continue
        if (now - sched).total_seconds() > 3 * 3600:  # >3h late
            db.update_slot_status(s["id"], "cancelled")
            cancelled += 1

    if cancelled:
        logger.info("Cancelled %d stale pending slots (>3h past scheduled)", cancelled)


def _memory_ok() -> bool:
    """Check if enough RAM is available."""
    try:
        from pipeline.ram_governor import is_ram_ok_for_dispatch
        return is_ram_ok_for_dispatch()
    except ImportError:
        return True  # ram_governor not available — proceed


def _time_since(ts_str: str) -> float:
    """Minutes since a timestamp string. Returns 0 if parse fails."""
    try:
        ts = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S")
        return (datetime.now() - ts).total_seconds() / 60.0
    except (ValueError, TypeError):
        return 0
