"""Dynamic daily video planning engine.

Computes upload slots for all channels for a given day, distributing
them across Spain-optimized time windows with human-like variation
and anti-collision enforcement (only one generation at a time).

Architecture:
  compute_daily_slots(date_key, channel_configs) → list of slot dicts
  compute_and_store_slots(date_key) → persists slots to planned_slots table
  sync_midday() → reconcile config changes mid-day
  preview_week(overrides) → dry-run 7 days without persistence
"""

import hashlib
import json
import logging
from datetime import date, datetime, timedelta
from typing import Optional

logger = logging.getLogger("autotube.planning")

# ── Constants ────────────────────────────────────────────────

# Spain-optimal UPLOAD windows (CEST = UTC+2).  These are the target publish
# hours — the generation START is calculated backwards from these.
SPAIN_UPLOAD_WINDOWS = [
    # (start_hour, end_hour, weight, name)
    (10, 13, 1, "mañana"),      # Publish target: 10:00-12:59
    (14, 17, 2, "mediodía"),    # Publish target: 14:00-16:59 (lunch/post-lunch)
    (18, 22, 3, "noche"),       # Publish target: 18:00-21:59 (prime time)
]
SPAIN_FALLBACK_WINDOW = (8, 10, 0.5, "madrugada-overflow")

ESTIMATED_PIPELINE_MINUTES = 75   # typical gen duration; used to calc scheduled_at
MIN_GAP_MINUTES = 90               # minimum gap between generation START times


# ── Alternate pattern resolution ─────────────────────────────────

def _resolve_videos_per_day(ch: dict, date_str: str) -> int:
    """Resolve effective videos_per_day, supporting alternate patterns.

    If ch has 'alternate_pattern' (a list like [2, 3]), alternates based on
    the day ordinal + channel-specific offset. Otherwise uses 'videos_per_day'.
    """
    pattern = ch.get("alternate_pattern")
    if pattern and isinstance(pattern, list) and len(pattern) >= 2:
        from datetime import datetime
        day_ordinal = datetime.strptime(date_str, "%Y-%m-%d").toordinal()
        offset = ch.get("alternate_offset", 0)
        idx = (day_ordinal + offset) % len(pattern)
        return pattern[idx]
    return ch.get("videos_per_day", 1)

# ── Seed helpers ──────────────────────────────────────────────

def _day_seed(date_str: str) -> int:
    """Deterministic seed for a date. Same date always → same seed."""
    h = hashlib.md5(date_str.encode()).hexdigest()
    return int(h[:8], 16)


def _channel_seed(day_seed: int, channel_id: int) -> int:
    """Combine day seed with channel id for per-channel variation."""
    return (day_seed ^ (channel_id * 0x9E3779B9)) & 0xFFFFFFFF


# ── Core algorithm ────────────────────────────────────────────

def _pick_time_in_window(
    window: tuple,        # (start_h, end_h, weight, name)
    channel_seed: int,
    avoid_hh00_hh30: bool = True,
) -> tuple:
    """Pick a random minute within a time window using deterministic seed.
    
    Returns (hour, minute) in CEST — this is the TARGET UPLOAD time.
    """
    start_h, end_h, _, _ = window
    range_minutes = (end_h - start_h) * 60
    
    # Base: deterministically pick a minute offset within the range
    offset = channel_seed % range_minutes
    
    # Jitter: ±random(0, 20) based on seed
    jitter = ((channel_seed >> 16) % 41) - 20  # -20..+20
    
    total_offset = offset + jitter
    total_offset = max(0, min(range_minutes - 1, total_offset))
    
    total_minutes = start_h * 60 + total_offset
    hour = total_minutes // 60
    minute = total_minutes % 60
    
    # Avoid :00 and :30 — push to :07 / :37 or :22 / :52
    if avoid_hh00_hh30:
        if minute == 0:
            minute = 7 + (channel_seed % 15)   # 7-21
        elif minute == 30:
            minute = 30 + 7 + (channel_seed % 15)  # 37-51
            if minute >= 60:
                minute -= 20
    
    return hour, minute


def _distribute_slots(
    videos_per_day: int,
    day_seed: int,
    channel_id: int,
    is_scheduled: bool = False,
    scheduled_cfg: dict = None,
) -> list[tuple]:
    """Distribute N video slots across time windows for one channel.

    For scheduled channels, uses niche-specific peak windows (publish_scheduler).
    For immediate channels, uses generic SPAIN_UPLOAD_WINDOWS.

    Returns list of (hour, minute) tuples — TARGET UPLOAD times (public for scheduled).

    Logic:
      1/dia → best window
      2/dia → best window + second best (spread)
      3/dia → three best windows
      4+/day → distribute across all 3 windows evenly, overflow to fallback
    """
    windows = list(SPAIN_UPLOAD_WINDOWS)
    fallback = SPAIN_FALLBACK_WINDOW
    ch_seed = _channel_seed(day_seed, channel_id)

    # ── Scheduled mode: use niche-specific peak windows ──
    if is_scheduled and scheduled_cfg:
        try:
            from pipeline.publish_scheduler import get_peak_windows
            peak_wins = get_peak_windows(scheduled_cfg, n=max(videos_per_day, 3))
            if peak_wins:
                windows = peak_wins
                fallback = windows[-1] if len(windows) > 2 else windows[0]
        except Exception as exc:
            logger.debug("Failed to load peak windows, falling back to generic: %s", exc)
    
    if videos_per_day <= 0:
        return []
    
    # Sort windows by weight (descending) as default preference
    # But rotate which window is "first" based on the day
    day_window_offset = day_seed % len(windows)
    rotated = windows[day_window_offset:] + windows[:day_window_offset]
    
    slots = []
    
    if videos_per_day == 1:
        # Single slot: use the first rotated window
        h, m = _pick_time_in_window(rotated[0], ch_seed)
        slots.append((h, m))
    
    elif videos_per_day == 2:
        # Two slots: best window + second best (spread out)
        h1, m1 = _pick_time_in_window(rotated[0], ch_seed)
        h2, m2 = _pick_time_in_window(rotated[1], ch_seed ^ 0xABCD)
        slots = [(h1, m1), (h2, m2)]
    
    elif videos_per_day == 3:
        # All three windows, one per window
        for i, w in enumerate(rotated[:3]):
            h, m = _pick_time_in_window(w, ch_seed ^ (i * 0x1111))
            slots.append((h, m))
    
    else:
        # 4+: distribute across windows, overflow goes to fallback
        per_window, remainder = divmod(videos_per_day, len(rotated[:3]))
        for i, w in enumerate(rotated[:3]):
            count = per_window + (1 if i < remainder else 0)
            for j in range(count):
                sub_seed = ch_seed ^ (i * 0x1111 + j * 0x2222)
                h, m = _pick_time_in_window(w, sub_seed)
                slots.append((h, m))
        
        # If remainder didn't fit, add fallback slots
        planned = len(slots)
        remaining = videos_per_day - planned
        for j in range(remaining):
            sub_seed = ch_seed ^ (0xFFFF + j * 0x3333)
            h, m = _pick_time_in_window(fallback, sub_seed)
            slots.append((h, m))
    
    # Sort slots by time within this channel
    slots.sort(key=lambda x: x[0] * 60 + x[1])
    return slots


def compute_daily_slots(
    date_str: str,
    channel_configs: list[dict],
) -> list[dict]:
    """Core planning algorithm: compute upload slots for all channels.

    The times picked from the optimal windows are TARGET UPLOAD times
    (when the video should appear on YouTube).  Generation START time
    is calculated backwards: scheduled_at = target_upload_at − pipeline_minutes.

    Collision resolution operates on scheduled_at (only one gen at a time).
    After resolving, target_upload_at is recalculated forward.

    Args:
        date_str: ISO date string (YYYY-MM-DD).
        channel_configs: list of dicts with {channel_id, slug, videos_per_day,
                          planning_enabled, name}.

    Returns:
        Sorted list of slot dicts with keys: channel_id, date_key, scheduled_at,
        target_upload_at, slot_position.
    """
    day_seed = _day_seed(date_str)
    all_slots = []
    
    for ch in channel_configs:
        if not ch.get("planning_enabled", True):
            continue
        n = _resolve_videos_per_day(ch, date_str)
        if n <= 0:
            continue

        is_scheduled = ch.get("publish_mode") == "scheduled"
        warmup_min = ch.get("publish_warmup_min", 120) if is_scheduled else 0
        jitter_min = ch.get("publish_jitter_min", 20) if is_scheduled else 0
        
        # _distribute_slots returns (h, m) = TARGET_UPLOAD (public for scheduled)
        raw_slots = _distribute_slots(n, day_seed, ch["channel_id"],
                                       is_scheduled=is_scheduled,
                                       scheduled_cfg=ch if is_scheduled else None)
        
        for pos, (target_h, target_m) in enumerate(raw_slots, 1):
            # target_upload_at: for scheduled = public peak time; for immediate = upload time
            target_str = f"{date_str} {target_h:02d}:{target_m:02d}:00"
            
            # ── Generation START: work backwards from target ──
            if is_scheduled:
                # Back out: pipeline + warmup (no extra buffer — collision resolution handles gaps)
                reverse_min = ESTIMATED_PIPELINE_MINUTES + warmup_min
            else:
                reverse_min = ESTIMATED_PIPELINE_MINUTES
            
            total_min = target_h * 60 + target_m - reverse_min
            if total_min < 0:
                total_min = 0  # clamp to midnight
            sched_h = total_min // 60
            sched_m = total_min % 60
            sched_str = f"{date_str} {sched_h:02d}:{sched_m:02d}:00"
            
            all_slots.append({
                "channel_id": ch["channel_id"],
                "date_key": date_str,
                "scheduled_at": sched_str,
                # For scheduled: target_upload_at is the PUBLIC peak time
                # For immediate: target_upload_at is the upload time (unchanged)
                "target_upload_at": target_str,
                "slot_position": pos,
                "channel_name": ch.get("name", ""),
                "channel_slug": ch.get("slug", ""),
                "source_mode": ch.get("default_source_mode", "original"),
                "publish_mode": ch.get("publish_mode", "immediate"),
            })
    
    # Sort all slots chronologically by generation START time
    all_slots.sort(key=lambda s: s["scheduled_at"])
    
    # ── Collision resolution on scheduled_at ──────────────────
    resolved = []
    for slot in all_slots:
        if resolved:
            last = resolved[-1]
            last_h, last_m = map(int, last["scheduled_at"][11:16].split(":"))
            this_h, this_m = map(int, slot["scheduled_at"][11:16].split(":"))
            
            last_total = last_h * 60 + last_m
            this_total = this_h * 60 + this_m
            
            diff = this_total - last_total
            if diff < MIN_GAP_MINUTES:
                # Push this slot's generation START forward
                new_total = last_total + MIN_GAP_MINUTES
                nh = new_total // 60
                nm = new_total % 60
                nh = min(nh, 23)
                nm = min(nm, 59)
                slot["scheduled_at"] = f"{date_str} {nh:02d}:{nm:02d}:00"
        
        resolved.append(slot)
    
    # Recalculate target_upload_at from scheduled_at after collision resolution
    for s in resolved:
        sched_h, sched_m = map(int, s["scheduled_at"][11:16].split(":"))
        up_total = sched_h * 60 + sched_m + ESTIMATED_PIPELINE_MINUTES
        uh = min(up_total // 60, 23)
        um = min(up_total % 60, 59)
        # For scheduled channels, target_upload_at stays as original peak time
        # (don't recalculate — the generation start was already backed out from peak)
        if s.get("publish_mode") != "scheduled":
            s["target_upload_at"] = f"{date_str} {uh:02d}:{um:02d}:00"
    
    # Update slot positions after collision resolution
    for pos, s in enumerate(resolved, 1):
        s["slot_position"] = pos
    
    return resolved


# ── Persistence layer ─────────────────────────────────────────

def compute_and_store_slots(
    date_str: Optional[str] = None,
    db=None,
) -> dict:
    """Compute slots for a date and store them in planned_slots.
    
    Args:
        date_str: date to plan (defaults to today).
        db: ExtendedDatabase instance (auto-created if None).
    
    Returns:
        dict with {date, total_slots, slots_by_channel}.
    """
    if date_str is None:
        date_str = date.today().isoformat()
    
    if db is None:
        from database.db_extended import ExtendedDatabase
        db = ExtendedDatabase()
    
    # Get all active channels and their planning config
    channels = db.get_channels(active_only=True)
    channel_configs = []
    for ch in channels:
        cfg = db.get_channel_planning_config(ch["id"])
        if cfg.get("planning_enabled", True) and cfg.get("videos_per_day", 0) > 0:
            channel_configs.append(cfg)
    
    if not channel_configs:
        logger.info("compute_and_store_slots(%s): no active planning channels", date_str)
        return {"date": date_str, "total_slots": 0, "slots_by_channel": {}}
    
    # Delete existing PENDING slots for this date (to allow full replan)
    with db._connect() as conn:
        conn.execute(
            "DELETE FROM planned_slots WHERE date_key = ? AND status = 'pending'",
            (date_str,),
        )
        conn.commit()
    
    # Compute slots
    slots = compute_daily_slots(date_str, channel_configs)
    
    # Store them
    stored = db.create_planned_slots_batch(slots)
    
    slots_by_channel = {}
    for s in slots:
        ch_id = s["channel_id"]
        if ch_id not in slots_by_channel:
            slots_by_channel[ch_id] = []
        slots_by_channel[ch_id].append(s["target_upload_at"][11:16])  # show upload times in log
    
    logger.info(
        "Planned %s: %d slots across %d channels → %s",
        date_str, stored, len(slots_by_channel),
        {ch_id: times for ch_id, times in slots_by_channel.items()},
    )
    
    return {
        "date": date_str,
        "total_slots": stored,
        "slots_by_channel": slots_by_channel,
    }


def sync_midday(db=None) -> dict:
    """Mid-day reconciliation: adapt planning if channel configs changed.
    
    Called every 5 min by the checker loop.  Checks:
    1. If today has no planned_slots yet → compute all
    2. If a channel's videos_per_day increased → add new pending slots
       in remaining windows
    3. If decreased → cancel excess pending slots
    4. If planning_enabled toggled off → cancel all pending
    
    Returns the updated state.
    """
    if db is None:
        from database.db_extended import ExtendedDatabase
        db = ExtendedDatabase()
    
    today = date.today().isoformat()
    existing = db.get_planned_slots(date_key=today)
    
    # Case 1: No slots at all for today → full plan
    if not existing:
        return compute_and_store_slots(today, db)
    
    # Get current channel configs
    channels = db.get_channels(active_only=True)
    result = {"date": today, "added": 0, "cancelled": 0, "replanned_channels": []}
    
    for ch in channels:
        cfg = db.get_channel_planning_config(ch["id"])
        ch_id = ch["id"]
        target = cfg.get("videos_per_day", 0) if cfg.get("planning_enabled", True) else 0
        
        # Current slots for this channel today
        ch_slots = [s for s in existing if s["channel_id"] == ch_id]
        
        completed_running = sum(
            1 for s in ch_slots if s["status"] in ("completed", "running")
        )
        pending = [s for s in ch_slots if s["status"] == "pending"]
        
        current_planned = completed_running + len(pending)
        target_planned = target
        
        if current_planned == target_planned:
            continue  # No change needed
        
        if current_planned > target_planned:
            # Too many: cancel the last N pending slots
            excess = current_planned - target_planned
            to_cancel = pending[-excess:] if excess <= len(pending) else pending
            if to_cancel:
                ids = [s["id"] for s in to_cancel]
                cancelled = db.cancel_slots(ids)
                result["cancelled"] += cancelled
                result["replanned_channels"].append({
                    "channel_id": ch_id,
                    "slug": ch["slug"],
                    "action": "cancelled",
                    "count": cancelled,
                })
        
        elif current_planned < target_planned:
            # Not enough: add new slots in remaining windows
            needed = target_planned - current_planned
            if needed <= 0:
                continue
            
            # Find the last scheduled_at for all non-cancelled slots today
            all_sorted = sorted(existing, key=lambda s: s["scheduled_at"])
            last_sched = None
            for s in reversed(all_sorted):
                if s["status"] != "cancelled":
                    last_sched = s["scheduled_at"]
                    break
            
            # Compute full day for this channel alone (already includes collisions internally)
            day_slots = compute_daily_slots(today, [{
                "channel_id": ch_id,
                "slug": ch["slug"],
                "name": ch["name"],
                "videos_per_day": target,
                "planning_enabled": True,
                "publish_mode": cfg.get("publish_mode", "immediate"),
                "publish_target_hour": cfg.get("publish_target_hour"),
                "publish_jitter_min": cfg.get("publish_jitter_min", 20),
                "publish_warmup_min": cfg.get("publish_warmup_min", 120),
                "publish_timezone": cfg.get("publish_timezone", "Europe/Madrid"),
                "seo_primary_keyword": cfg.get("seo_primary_keyword", ""),
                "seo_secondary_keywords": cfg.get("seo_secondary_keywords", []),
            }])
            
            # Filter: only take slots NOT already covered by existing non-cancelled slots
            new_slots = []
            existing_positions = {
                es["slot_position"]
                for es in ch_slots
                if es["status"] != "cancelled"
            }
            for s in day_slots:
                if s["slot_position"] not in existing_positions:
                    new_slots.append(s)
            new_slots = new_slots[:needed]
            
            # Ensure new slots don't collide with today's existing non-cancelled slots
            for ns in new_slots:
                if last_sched:
                    lh, lm = map(int, last_sched[11:16].split(":"))
                    sh, sm = map(int, ns["scheduled_at"][11:16].split(":"))
                    diff = (sh * 60 + sm) - (lh * 60 + lm)
                    if diff < MIN_GAP_MINUTES:
                        # Push generation start forward
                        new_gen = lh * 60 + lm + MIN_GAP_MINUTES
                        nh = min(new_gen // 60, 23)
                        nm = min(new_gen % 60, 59)
                        ns["scheduled_at"] = f"{today} {nh:02d}:{nm:02d}:00"
                        up_t = nh * 60 + nm + ESTIMATED_PIPELINE_MINUTES
                        uh = min(up_t // 60, 23)
                        um = min(up_t % 60, 59)
                        ns["target_upload_at"] = f"{today} {uh:02d}:{um:02d}:00"
                last_sched = ns["scheduled_at"]
            
            db.create_planned_slots_batch(new_slots)
            result["added"] += len(new_slots)
            result["replanned_channels"].append({
                "channel_id": ch_id,
                "slug": ch["slug"],
                "action": "added",
                "count": len(new_slots),
            })
    
    if result["added"] > 0 or result["cancelled"] > 0:
        logger.info(
            "Mid-day sync(%s): +%d added, -%d cancelled → %s",
            today, result["added"], result["cancelled"],
            [r["slug"] for r in result["replanned_channels"]],
        )
    
    return result


# ── Dry-run preview ───────────────────────────────────────────

def preview_week(overrides: dict = None, db=None) -> dict:
    """Simulate 7 days of planning WITHOUT persisting.
    
    Args:
        overrides: dict mapping slug → {videos_per_day, planning_enabled} to
                   simulate config changes.
    
    Returns:
        dict with {days: [{date, slots: [{time, channel_name, channel_slug}]}]}
    """
    if db is None:
        from database.db_extended import ExtendedDatabase
        db = ExtendedDatabase()
    
    channels = db.get_channels(active_only=True)
    channel_configs = []
    
    for ch in channels:
        cfg = db.get_channel_planning_config(ch["id"])
        
        # Apply overrides if any
        slug = ch["slug"]
        if overrides and slug in overrides:
            ov = overrides[slug]
            if "videos_per_day" in ov:
                cfg["videos_per_day"] = ov["videos_per_day"]
            if "planning_enabled" in ov:
                cfg["planning_enabled"] = ov["planning_enabled"]
        
        if cfg.get("planning_enabled") and cfg.get("videos_per_day", 0) > 0:
            channel_configs.append(cfg)
    
    today = date.today()
    days = []
    
    for offset in range(7):
        d = today + timedelta(days=offset)
        date_str = d.isoformat()
        slots = compute_daily_slots(date_str, channel_configs)
        
        days.append({
            "date": date_str,
            "weekday": d.strftime("%a"),
            "slots": [
                {
                    "gen_start": s["scheduled_at"][11:16],
                    "upload": s["target_upload_at"][11:16] if s.get("target_upload_at") else None,
                    "channel_name": s["channel_name"],
                    "channel_slug": s["channel_slug"],
                    "channel_id": s["channel_id"],
                }
                for s in slots
            ],
        })
    
    return {"days": days, "overrides_applied": bool(overrides)}


# ── Scheduler integration ─────────────────────────────────────

def process_planned_slots(db=None) -> dict | None:
    """Check for due planned slots and dispatch generation if possible.
    
    Called every 5 min by the API checker loop.
    
    Returns:
        dict with dispatched slot info, or None if nothing to do.
    """
    if db is None:
        from database.db_extended import ExtendedDatabase
        db = ExtendedDatabase()
    
    # 0. Mark completed/failed: any running slot whose job is done
    _sync_running_slots(db)
    
    # 0b. Detect manual jobs that completed recently (no planned_slot row)
    #     and trigger reajuste so pending slots align to the real timeline.
    if _detect_manual_completions(db):
        _readjust_pending_slots(db)
    
    # 1. Reconcile mid-day changes
    sync_midday(db)
    
    # 1b. Cancel stale pending slots (>3h past their scheduled time)
    _cancel_stale_slots(db)
    
    # 2. Find the next pending slot whose time is due
    next_slot = db.get_next_pending_slot()
    if not next_slot:
        return None
    
    # 2b. Is there already an active job for this channel?
    active = db.get_active_job_for_channel(next_slot["channel_id"])
    if active:
        logger.debug("Planned slot skipped: channel %d already has active job #%d",
                     next_slot["channel_id"], active["id"])
        return None
    
    # 3b. Memory guard: skip dispatch if RAM is critically low
    if not _memory_ok():
        logger.warning("Low memory — delaying planned slot dispatch")
        return None
    
    slot_id = next_slot["id"]
    channel_id = next_slot["channel_id"]
    slug = next_slot.get("channel_slug", "")
    source_mode = next_slot.get("source_mode", "original")
    
    logger.info(
        "Dispatching slot #%d: %s at %s",
        slot_id, slug, next_slot["scheduled_at"],
    )
    
    # 4. Mark slot as running
    db.update_slot_status(slot_id, "running")
    
    # 5. Create the video record
    from database.db_extended import ExtendedDatabase
    
    # Get channel config to check publish mode
    ch_cfg = db.get_channel_planning_config(channel_id)
    publish_mode = ch_cfg.get("publish_mode", "immediate") if ch_cfg else "immediate"
    
    with db._connect() as conn:
        cursor = conn.execute(
            "INSERT INTO videos (canal, channel_id, video_path, status, progress, "
            "publish_mode, target_public_at, created_at) "
            "VALUES (?, ?, '', 'generating', 0, ?, ?, CURRENT_TIMESTAMP)",
            (slug, channel_id, publish_mode,
             next_slot.get("target_upload_at") if publish_mode == "scheduled" else None),
        )
        conn.commit()
        video_id = cursor.lastrowid
    
    # 6. Create job
    job_id = db.create_job(channel_id, "generate_and_upload", video_id)
    
    # 7. Link job to slot
    db.update_slot_status(slot_id, "running", job_id=job_id, video_id=video_id)
    
    # 8. Fire and forget the generation
    import asyncio
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
                source_mode=source_mode,
            )
        )
    else:
        asyncio.create_task(
            start_generation_job(
                job_id=job_id,
                channel_id=channel_id,
                video_id=video_id,
                action="generate_and_upload",
                source_mode=source_mode,
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
    
    Also triggers _readjust_pending_slots() when a job completes, to
    realign the remaining slots and avoid cascading time drift.
    """
    today = date.today().isoformat()
    running_slots = db.get_planned_slots(date_key=today, status="running")
    any_completed = False
    
    for s in running_slots:
        job_id = s.get("job_id")
        if not job_id:
            # No job linked at all → stale, mark completed (slot was consumed)
            db.update_slot_status(s["id"], "completed")
            logger.info("Slot #%d marked completed (no job linked — stale)", s["id"])
            any_completed = True
            continue
        job = db.get_job(job_id)
        if not job:
            # Job row missing → stale, mark completed (slot was consumed)
            db.update_slot_status(s["id"], "completed")
            logger.info("Slot #%d marked completed (job #%d not found)", s["id"], job_id)
            any_completed = True
            continue
        if job["status"] in ("completed", "success"):
            db.update_slot_status(s["id"], "completed")
            logger.info("Slot #%d marked completed (job #%d done)", s["id"], job_id)
            any_completed = True
        elif job["status"] in ("failed", "cancelled"):
            # failed or cancelled = slot was consumed / is dead
            db.update_slot_status(s["id"], "completed")
            logger.info("Slot #%d marked completed (job #%d %s)", s["id"], job_id, job["status"])
            any_completed = True
    
    if any_completed:
        _readjust_pending_slots(db)


def _readjust_pending_slots(db):
    """Realign remaining pending slots after a job finishes.

    When a generation job completes, the real finish time may differ from
    the planned time.  This function recalculates the scheduled_at of all
    pending slots for today starting from now + MIN_GAP_MINUTES, so that
    subsequent upload targets stay as close as possible to the planned
    windows instead of drifting backwards in a cascade.

    Only affects slots whose scheduled_at is in the future (or has passed
    but wasn't dispatched yet).  Already-running slots are untouched.
    """
    today = date.today().isoformat()
    pending = db.get_planned_slots(date_key=today, status="pending")
    if not pending:
        return
    
    # Find the real finish time: now (the moment the last job ended).
    # Use max(now, last_completed_slot's scheduled_at + buffer) as anchor.
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    now_h, now_m = map(int, now[11:16].split(":"))
    now_total = now_h * 60 + now_m
    
    # Also check the last completed slot's scheduled_at + MIN_GAP_MINUTES
    # as a lower bound
    completed = db.get_planned_slots(date_key=today, status="completed")
    if completed:
        last_comp = sorted(completed, key=lambda s: s["scheduled_at"])[-1]
        lh, lm = map(int, last_comp["scheduled_at"][11:16].split(":"))
        anchor = max(now_total, lh * 60 + lm + MIN_GAP_MINUTES)
    else:
        anchor = now_total + MIN_GAP_MINUTES
    
    # Sort pending slots by current scheduled_at
    pending_sorted = sorted(pending, key=lambda s: s["scheduled_at"])
    
    logger.info(
        "Reajustando %d slots pendientes desde anchor=%02d:%02d",
        len(pending_sorted), anchor // 60, anchor % 60,
    )
    
    next_start = anchor
    for slot in pending_sorted:
        nh = next_start // 60
        nm = next_start % 60
        if nh >= 24:
            nh = 23
            nm = 59
        
        new_sched = f"{today} {nh:02d}:{nm:02d}:00"
        up_total = nh * 60 + nm + ESTIMATED_PIPELINE_MINUTES
        uh = min(up_total // 60, 23)
        um = min(up_total % 60, 59)
        new_upload = f"{today} {uh:02d}:{um:02d}:00"
        
        old_time = slot.get("scheduled_at", "?")[11:16] if slot.get("scheduled_at") else "?"
        new_time = new_sched[11:16]
        
        if old_time != new_time:
            logger.info(
                "  Slot #%d (%s): %s → %s (gen), 📺 %s → 📺 %s",
                slot["id"], slot.get("channel_slug", "?"),
                old_time, new_time, 
                (slot.get("target_upload_at") or "?")[11:16] if slot.get("target_upload_at") else "?",
                new_upload[11:16],
            )
        
        with db._connect() as conn:
            conn.execute(
                "UPDATE planned_slots SET scheduled_at = ?, target_upload_at = ? WHERE id = ?",
                (new_sched, new_upload, slot["id"]),
            )
            conn.commit()
        
        next_start = nh * 60 + nm + MIN_GAP_MINUTES


def _cancel_stale_slots(db):
    """Cancel pending slots whose scheduled time passed more than 3 hours ago.
    
    These are zombie slots — the dispatcher should have picked them up but
    couldn't (e.g., server restart, checker loop crash).  Marking them as
    cancelled prevents the dispatcher from acting on stale times.
    """
    today = date.today().isoformat()
    with db._connect() as conn:
        cancelled = conn.execute(
            """UPDATE planned_slots SET status = 'cancelled'
               WHERE status = 'pending' AND date_key = ?
                 AND scheduled_at <= datetime('now', 'localtime', '-3 hours')""",
            (today,),
        ).rowcount
        conn.commit()
    if cancelled:
        logger.info("Cancelled %d stale pending slots (>3h overdue)", cancelled)


def _detect_manual_completions(db) -> bool:
    """Detect recently completed jobs that have NO planned_slots row.
    
    These are manual generations (triggered from the panel).  When one
    finishes, we should reajust today's pending planned slots so they
    don't fall behind the real timeline.
    
    Returns True if any manual completions were found in the last 15 min.
    """
    with db._connect() as conn:
        row = conn.execute(
            """SELECT COUNT(*) as cnt FROM generation_jobs j
               WHERE j.status IN ('completed', 'failed')
                 AND j.finished_at >= datetime('now', 'localtime', '-15 minutes')
                 AND NOT EXISTS (
                     SELECT 1 FROM planned_slots ps WHERE ps.job_id = j.id
                 )"""
        ).fetchone()
        count = row["cnt"] if row else 0
    
    if count > 0:
        logger.info(
            "Detected %d manual job completion(s) — triggering reajuste", count
        )
        return True
    return False


def ensure_today_planned(db=None):
    """Ensure today has planned_slots. Called on API startup."""
    if db is None:
        from database.db_extended import ExtendedDatabase
        db = ExtendedDatabase()
    
    today = date.today().isoformat()
    existing = db.get_planned_slots(date_key=today)
    
    if not existing:
        logger.info("No slots found for today (%s) — computing...", today)
        compute_and_store_slots(today, db)
    else:
        # Quick sync in case config changed while API was down
        sync_midday(db)


def _memory_ok(min_free_gb: float = 4.0) -> bool:
    """Check if the server has enough free RAM to launch a generation job.
    
    Returns False if available memory is below the threshold, to prevent
    OOM killer from taking down the process.
    
    Uses /proc/meminfo::MemAvailable (container-aware) instead of
    os.sysconf() which reports host memory in containerized environments.
    
    Threshold raised to 4.0 GB (from 2.0) to account for MoviePy rendering
    peaks that can exhaust ~25 MB/frame in raw RGB during write_videofile.
    """
    try:
        # MemAvailable is container-aware (cgroup limits) on Linux 3.14+
        with open('/proc/meminfo') as f:
            for line in f:
                if line.startswith('MemAvailable:'):
                    avail_kb = int(line.split()[1])
                    avail_gb = avail_kb / (1024 * 1024)
                    if avail_gb < min_free_gb:
                        logger.warning(
                            "Memory guard: only %.1f GB free (need %.1f GB) — skipping dispatch",
                            avail_gb, min_free_gb,
                        )
                        return False
                    return True
        # Fallback: no MemAvailable field (old kernel) — let it proceed
        logger.warning("Memory guard: /proc/meminfo has no MemAvailable — skipping guard")
        return True
    except Exception:
        return True  # can't determine — let it proceed


# ── Shorts Planning (DEPRECATED — use api.services.shorts_scheduler instead) ──
# These functions have been moved to:
#   - api/services/shorts_scheduler.py (compute, persist, dispatch)
#   - database/db_extended.py (get_shorts_planning_config, update_shorts_planning_config)
#
# Kept for backward compatibility during transition. Will be removed in v3.

def get_shorts_planning_config(db=None) -> list[dict]:
    """[DEPRECATED] Get shorts planning config for all active channels.
    
    Use db.get_shorts_planning_config() instead.
    """
    if db is None:
        from database.db_extended import ExtendedDatabase
        db = ExtendedDatabase()
    return db.get_shorts_planning_config()


def update_shorts_planning_config(channel_id: int, data: dict, db=None) -> dict:
    """[DEPRECATED] Update shorts planning config for one channel.
    
    Use db.update_shorts_planning_config(channel_id, data) instead.
    """
    if db is None:
        from database.db_extended import ExtendedDatabase
        db = ExtendedDatabase()
    ok = db.update_shorts_planning_config(channel_id, data)
    return {"ok": ok, "message": "Shorts planning config updated" if ok else "No valid fields"}


def ensure_shorts_planned(db=None):
    """[DEPRECATED] Ensure today has shorts_schedule entries.
    
    Use shorts_scheduler.generate_upcoming_shorts() instead.
    """
    from api.services.shorts_scheduler import generate_upcoming_shorts
    generate_upcoming_shorts(days=7, db=db)


def process_shorts_slots(db=None):
    """[DEPRECATED] Process due shorts_schedule slots.
    
    Use shorts_scheduler.dispatch_next_due_shorts_slot() instead.
    Called by the background checker loop in api/main.py.
    """
    from api.services.shorts_scheduler import dispatch_next_due_shorts_slot
    dispatch_next_due_shorts_slot(db=db)


def _generate_and_publish_native_short(channel_id: int, channel_slug: str, db=None):
    """Generate and publish one native Short for a channel (internal, used by scheduler).

    This function is kept as the canonical native short generator and is imported
    by shorts_scheduler._dispatch_native_short. The shorts_scheduler has its own
    copy for independence, but external callers can use this too.

    Args:
        channel_id: Channel ID in the database.
        channel_slug: Channel slug (e.g. 'canal2').
        db: Optional ExtendedDatabase instance.

    Returns:
        short_id if successful, None on failure.
    """
    import json, re, subprocess, time, sqlite3
    from pathlib import Path
    from config.settings import DATABASE_PATH, LLM_API_KEY, LLM_BASE_URL, LLM_MODEL, OUTPUT_DIR
    from config.config_bridge import get_channel_config

    ch_config = get_channel_config(channel_slug)
    hashtags = getattr(ch_config, "SHORTS_HASHTAGS", ["#Shorts"])

    # 1. Script via LLM
    from openai import OpenAI
    client = OpenAI(api_key=LLM_API_KEY, base_url=LLM_BASE_URL)
    niche = getattr(ch_config, "CANAL_NARRATIVE_STYLE", "documental")
    display_name = getattr(ch_config, "CANAL_DISPLAY_NAME", channel_slug)
    tagline = getattr(ch_config, "CANAL_TAGLINE", "")

    response = client.chat.completions.create(
        model=LLM_MODEL,
        messages=[{"role": "user", "content": f"Genera un Short viral en español de ~45-50 segundos (~65-80 palabras totales, minimo 50). Canal: {display_name} — {niche}. Tagline: {tagline}. Usa 5 bloques (hook, desarrollo1, desarrollo2, climax, cierre). IMPORTANTE: desarrollo1, desarrollo2 y climax deben tener 2-3 frases cada uno. Hook y cierre: 1-2 frases. Minimo 10 palabras por bloque. El total debe superar 50 palabras. Devuelve SOLO JSON: {{\"titulo\": \"...\", \"hook_text\": \"frase de gancho 8-12 palabras\", \"bloques\": [{{\"tipo\": \"hook\", \"texto\": \"1-2 frases\"}}, {{\"tipo\": \"desarrollo1\", \"texto\": \"2-3 frases con contexto y detalle\"}}, {{\"tipo\": \"desarrollo2\", \"texto\": \"2-3 frases con dato impactante especifico\"}}, {{\"tipo\": \"climax\", \"texto\": \"2-3 frases con la consecuencia o revelacion\"}}, {{\"tipo\": \"cierre\", \"texto\": \"1-2 frases cierre + suscribete\"}}]}}. NADA MAS fuera del JSON."}],
        temperature=0.9, max_tokens=1200,
    )
    content = response.choices[0].message.content
    content = re.sub(r"^```(?:json)?\s*\n", "", content)
    content = re.sub(r"\n```\s*$", "", content).strip()
    match = re.search(r"\{.*\}", content, re.DOTALL)
    script = json.loads(match.group(0))

    # 1b. Validate script completeness
    from pipeline.shorts_tts import validate_short_script
    errors = validate_short_script(script)
    if errors:
        logger.error("Short script validation failed for %s: %s", channel_slug, errors)
        return

    title = (script.get("titulo") or script.get("title") or "Short")[:100]
    hook_text = (script.get("hook_text") or "")[:100]
    bloques = script.get("bloques", [])

    # 2. Segmented TTS (block-by-block, no mid-phrase truncation)
    output_dir = OUTPUT_DIR / "videos" / "shorts"
    output_dir.mkdir(parents=True, exist_ok=True)
    ts = int(time.time())
    audio_path = output_dir / f"sched_audio_{channel_slug}_{ts}.mp3"
    srt_path = output_dir / f"sched_audio_{channel_slug}_{ts}.srt"

    from pipeline.shorts_tts import synthesize_shorts_blocks
    try:
        tts_result = synthesize_shorts_blocks(
            bloques=bloques,
            ch_config=ch_config,
            output_audio_path=audio_path,
            output_srt_path=srt_path,
        )
        audio_duration = tts_result["duration_sec"] + 1.5
    except RuntimeError as e:
        logger.error("Short TTS failed for %s: %s", channel_slug, e)
        return

    # 3. Fetch portrait images for the Short
    portrait_queries = [b.get("texto", "")[:80] for b in bloques]
    portrait_queries = [q for q in portrait_queries if q.strip()]
    if not portrait_queries:
        portrait_queries = [hook_text[:80]]

    from pipeline.shorts_media import fetch_portrait_images, render_slideshow_with_images
    image_paths = fetch_portrait_images(portrait_queries, ch_config, count=4)

    # 4. Render (slideshow of images + text + audio)
    video_path = output_dir / f"sched_short_{channel_slug}_{ts}.mp4"

    # Background color as fallback
    color_palette = getattr(ch_config, "COLOR_PALETTE", {})
    def _to_hex(c):
        if isinstance(c, (tuple, list)) and len(c) == 3:
            return f"{int(c[0]):02x}{int(c[1]):02x}{int(c[2]):02x}"
        return str(c).lstrip("#").replace("#", "")
    bg_color = _to_hex(color_palette.get("text_shadow", (10, 10, 26)))

    try:
        render_slideshow_with_images(
            image_paths=image_paths,
            audio_path=audio_path,
            hook_text=hook_text,
            output_path=video_path,
            audio_duration=audio_duration,
            bg_color_hex=bg_color,
            srt_path=srt_path if srt_path.exists() else None,
        )
    except Exception as e:
        logger.warning("Slideshow render failed, falling back to solid bg: %s", e)
        # Solid-bg fallback with subtitles
        from pipeline.shorts_media import _build_solid_bg_filter
        filter_str = _build_solid_bg_filter(
            bg_color,
            srt_path=srt_path if srt_path.exists() else None,
        )
        render_cmd = ["ffmpeg", "-y", "-f", "lavfi", "-i", f"color=c=0x{bg_color}:s=1080x1920:d={audio_duration}:r=30",
            "-i", str(audio_path), "-filter_complex", filter_str,
            "-map", "[v]", "-map", "1:a", "-c:v", "libx264", "-preset", "fast", "-crf", "22",
            "-pix_fmt", "yuv420p", "-r", "30", "-c:a", "aac", "-b:a", "128k",
            "-shortest", "-movflags", "+faststart", str(video_path)]
        subprocess.run(render_cmd, capture_output=True, timeout=120)

    # 5. Upload
    from pipeline.youtube_uploader import YouTubeUploader
    uploader = YouTubeUploader(account_name=channel_slug, channel_slug=channel_slug)
    if not uploader.authenticate():
        return

    # ── Cross-promotion: link to long-form video ──────────
    from pipeline.shorts_cross_promote import (
        get_best_longform_link, build_short_description, run_post_publish_promotion,
        should_cross_promote,
    )
    longform_url = None
    if should_cross_promote(ch_config):
        longform_url = get_best_longform_link(channel_id)

    channel_url = getattr(ch_config, "YOUTUBE_CHANNEL_URL", "")
    description = build_short_description(
        hook_text=hook_text,
        hashtags=hashtags,
        longform_url=longform_url,
        channel_url=channel_url,
    )
    result = uploader.upload(video_path=video_path, title=title[:100], description=description[:5000], tags=hashtags[:60], category_id=getattr(ch_config, "YT_CATEGORY_ID", "24"), privacy="public")

    yt_id = result.get("video_id")
    if yt_id:
        conn = sqlite3.connect(str(DATABASE_PATH))
        cursor = conn.execute("INSERT INTO shorts (channel_id, type, title, hook_title, hook_text, status, file_path, youtube_id, youtube_url, published_at) VALUES (?, 'native', ?, ?, ?, 'published', ?, ?, ?, datetime('now'))", (channel_id, title, title[:60], hook_text, str(video_path), yt_id, result.get("url", "")))
        short_id = cursor.lastrowid
        conn.commit()
        conn.close()

        # ── Post-publish cross-promotion ──────────────
        run_post_publish_promotion(
            channel_slug=channel_slug,
            short_yt_id=yt_id,
            channel_id=channel_id,
            source_yt_id=longform_url.split("v=")[-1] if longform_url else None,
            channel_config=ch_config,
        )

        logger.info("Scheduled native Short published: %s → %s", title[:40], result.get("url", ""))
        return short_id

    return None
