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

# ── Dispatch lock (serializes all generation dispatches) ────────
from api.services.generation_service import _DISPATCH_LOCK

# ── Cooldown guard: prevent rapid successive replans ─────
_last_horizon_replan_ts: Optional[datetime] = None
_HORIZON_REPLAN_COOLDOWN_MIN = 5  # minimum minutes between replans

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

ESTIMATED_PIPELINE_MINUTES = 120  # fallback gen duration when per-channel data unavailable
MIN_GAP_MINUTES = 90               # minimum gap between generation START times (same-channel collision)
# v2 smart scheduling: realistic gap between completion of one job and start of next.
# Previously 5 min (unrealistic). Now 30 min buffer, chained as: spacing = ch_dur + GAP.
GLOBAL_GAP_MINUTES = 30            # buffer minutes between consecutive generation jobs
MIN_SAME_CHANNEL_PUBLISH_GAP_HOURS = 3  # minimum hours between publish times for same channel (v10.1 collision fix)
BUFFER_PCT = 0.15                  # safety buffer on per-channel avg creation time
DEFAULT_HORIZON_DAYS = 7           # days to plan ahead (today + 6)


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

# ── Source mode alternation ─────────────────────────────────

def _build_source_mode_sequence(total: int, viral_per_day: int) -> list[str]:
    """Build alternating source_mode sequence for one channel's daily slots.

    Always starts with 'viral' when viral_per_day > 0 so viral slots get
    earlier scheduled_at and are dispatched first. Then alternates to
    distribute them evenly throughout the day.

    Examples:
        total=3, viral=1 → ['viral', 'original', 'original']
        total=3, viral=2 → ['viral', 'original', 'viral']
        total=4, viral=2 → ['viral', 'original', 'viral', 'original']
        total=4, viral=1 → ['viral', 'original', 'original', 'original']
        total=2, viral=0 → ['original', 'original']
        total=2, viral=2 → ['viral', 'viral']
    """
    if viral_per_day <= 0:
        return ["original"] * total
    if viral_per_day >= total:
        return ["viral"] * total

    original_count = total - viral_per_day
    # Always start with viral first so they get earlier scheduled_at
    # and are dispatched before original slots. The alternation still
    # ensures even distribution throughout the day.
    first, second = "viral", "original"
    first_avail, second_avail = viral_per_day, original_count

    result = []
    for i in range(total):
        if i % 2 == 0:
            # Even slots → use the primary (more abundant) type
            if first_avail > 0:
                result.append(first)
                first_avail -= 1
            else:
                result.append(second)
                second_avail -= 1
        else:
            # Odd slots → use the secondary type
            if second_avail > 0:
                result.append(second)
                second_avail -= 1
            else:
                result.append(first)
                first_avail -= 1
    return result


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
    
    # For scheduled channels: the primary peak (highest-weight window) MUST
    # always be one of the assigned slots. Only secondary peaks rotate by day.
    if is_scheduled and len(windows) > 1:
        primary = windows[0]
        secondary_wins = windows[1:]
        day_window_offset = day_seed % len(secondary_wins)
        rotated = [primary] + (secondary_wins[day_window_offset:] + secondary_wins[:day_window_offset])
    else:
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
    
    # ── v10.1: Enforce minimum spread between same-channel publish times ──
    # After sorting, ensure no two adjacent slots are closer than 
    # MIN_SAME_CHANNEL_PUBLISH_GAP_HOURS. Push later slots forward if needed.
    if len(slots) >= 2:
        min_gap_minutes = MIN_SAME_CHANNEL_PUBLISH_GAP_HOURS * 60
        spread_slots = [slots[0]]
        for i in range(1, len(slots)):
            prev_minutes = spread_slots[-1][0] * 60 + spread_slots[-1][1]
            curr_minutes = slots[i][0] * 60 + slots[i][1]
            if curr_minutes - prev_minutes < min_gap_minutes:
                # Push this slot forward to maintain minimum gap
                new_minutes = prev_minutes + min_gap_minutes
                new_h = min(new_minutes // 60, 23)
                new_m = new_minutes % 60
                if new_h >= 23 and new_m > 50:
                    # Overflow: wrap to next day (caller handles this)
                    new_h = min(new_h, 23)
                    new_m = min(new_m, 59)
                spread_slots.append((new_h, new_m))
                logger.debug("_distribute_slots: pushed slot %d from %02d:%02d → %02d:%02d "
                             "(gap was %d min < %d min)",
                             i + 1,
                             slots[i][0], slots[i][1],
                             new_h, new_m,
                             curr_minutes - prev_minutes,
                             min_gap_minutes)
            else:
                spread_slots.append(slots[i])
        slots = spread_slots
    
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
        
        # ── Build source_mode sequence for this channel's daily slots ──
        viral_n = ch.get("viral_per_day", 0)
        mode_sequence = _build_source_mode_sequence(n, viral_n) if n > 0 else []
        
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
            
            # Assign source_mode from the pre-built alternating sequence (0-indexed)
            slot_mode = mode_sequence[pos - 1] if (pos - 1) < len(mode_sequence) else "original"
            
            all_slots.append({
                "channel_id": ch["channel_id"],
                "date_key": date_str,
                "scheduled_at": sched_str,
                # For scheduled: target_upload_at is the PUBLIC peak time
                # For immediate: target_upload_at is the upload time (unchanged)
                "target_upload_at": target_str,
                "target_public_at": target_str if is_scheduled else None,
                "slot_position": pos,
                "channel_name": ch.get("name", ""),
                "channel_slug": ch.get("slug", ""),
                "source_mode": slot_mode,
                "publish_mode": ch.get("publish_mode", "immediate"),
                "upload_window_start": ch.get("upload_window_start", 9),
                "upload_window_end": ch.get("upload_window_end", 11),
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
    # ── v10.1: Also enforce same-channel publish spread for scheduled channels ──
    # Group slots by channel to check intra-channel target_upload_at collisions
    channel_slots = {}
    for s in resolved:
        ch_id = s["channel_id"]
        if ch_id not in channel_slots:
            channel_slots[ch_id] = []
        channel_slots[ch_id].append(s)
    
    for ch_id, ch_slots in channel_slots.items():
        # Sort by target_upload_at for this channel
        ch_slots.sort(key=lambda s: s["target_upload_at"])
        min_gap = MIN_SAME_CHANNEL_PUBLISH_GAP_HOURS * 60
        
        for i in range(1, len(ch_slots)):
            prev = ch_slots[i - 1]
            curr = ch_slots[i]
            
            prev_h, prev_m = map(int, prev["target_upload_at"][11:16].split(":"))
            curr_h, curr_m = map(int, curr["target_upload_at"][11:16].split(":"))
            gap = (curr_h * 60 + curr_m) - (prev_h * 60 + prev_m)
            
            if gap < min_gap and curr.get("publish_mode") == "scheduled":
                new_total = prev_h * 60 + prev_m + min_gap
                nh = min(new_total // 60, 23)
                nm = new_total % 60
                curr["target_upload_at"] = f"{curr['date_key']} {nh:02d}:{nm:02d}:00"
                logger.info(
                    "compute_daily_slots: [%s] pushed slot #%d target from %02d:%02d → %02d:%02d "
                    "(gap was %d min < %d min)",
                    curr.get("channel_slug", "?"), curr["slot_position"],
                    curr_h, curr_m, nh, nm, gap, min_gap,
                )
    
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


# ── 3-Phase Horizon Planning (v9) ──────────────────────────────

def _pick_upload_minute(day_seed: int, channel_id: int, slot_pos: int,
                         window_start: int = None, window_end: int = None,
                         videos_per_day: int = 1,
                         windows: list = None) -> tuple:
    """Pick a deterministic minute within the upload window(s) for one slot.

    v11+: Accepts `windows` list of dicts [{"start":10,"end":13},...] with
    round-robin distribution across windows. Falls back to single window_start/end
    for backward compatibility.

    Spreads multiple videos from the same channel across the window
    (min 15 min apart). Returns (hour, minute, window_start, window_end).
    """
    # ── v11: multi-window round-robin ──
    if windows and isinstance(windows, list) and len(windows) > 0:
        # Validate and pick window via round-robin
        valid_windows = []
        for w in windows:
            if isinstance(w, dict) and "start" in w and "end" in w:
                valid_windows.append(w)
        if valid_windows:
            rr_idx = (day_seed + slot_pos + channel_id) % len(valid_windows)
            chosen = valid_windows[rr_idx]
            window_start = chosen["start"]
            window_end = chosen["end"]
        # else: fall through to window_start/window_end
    elif window_start is None or window_end is None:
        window_start, window_end = 9, 11  # ultimate fallback

    ch_seed = _channel_seed(day_seed, channel_id) ^ (slot_pos * 0xCAFE)
    window_minutes = (window_end - window_start) * 60

    if videos_per_day <= 1:
        # Single video: random within window
        offset = ch_seed % max(window_minutes - 1, 1)
    else:
        # Multiple videos: divide the window into N segments, then jitter
        segment = window_minutes // videos_per_day
        base = slot_pos * segment - segment // 2
        jitter = (ch_seed % max(segment - 15, 1))
        offset = max(0, min(window_minutes - 1, base + jitter))

    total_min = window_start * 60 + offset
    return (total_min // 60, total_min % 60, window_start, window_end)


def compute_horizon_slots(
    channel_configs: list[dict],
    horizon_days: int = DEFAULT_HORIZON_DAYS,
    db=None,
) -> list[dict]:
    """Core 3-phase planning: generate slots across N-day horizon with cross-day gen chaining.
    
    Algorithm:
      1. For each day D in [today .. today+horizon_days):
         - Compute target_public_at (peak publish) per channel using existing windows
         - Compute target_upload_at (upload window) — deterministic minute within 
           the channel's UPLOAD_WINDOW_START..END on day D
      2. Sort ALL slots by target_upload_at (chronologically).
      3. Chain generation starts BACKWARDS:
         - Each generation must FINISH before its target_upload_at
         - Each generation must not START more than lead_hours before target_public_at
         - Generations are chained with GLOBAL_GAP_MINUTES gap
         - Uses per-channel avg creation time (from timing_data) + BUFFER_PCT
    
    Returns:
        Sorted list of slot dicts with: channel_id, date_key, scheduled_at,
        target_upload_at, target_public_at, slot_position, channel_name, channel_slug,
        source_mode, publish_mode.
    """
    from datetime import date as _date, datetime as _dt, timedelta as _td
    
    today = _date.today()
    all_raw_slots = []
    
    # ── 1. Collect all raw slots across the horizon ──────────────
    for day_offset in range(horizon_days):
        d = today + _td(days=day_offset)
        date_str = d.isoformat()
        
        for ch in channel_configs:
            if not ch.get("planning_enabled", True):
                continue
            n = _resolve_videos_per_day(ch, date_str)
            if n <= 0:
                continue
            
            is_scheduled = ch.get("publish_mode") == "scheduled"
            ch_id = ch["channel_id"]
            
            # ── A. Compute target_public_at (peak publish time) ──
            raw_peaks = _distribute_slots(
                n, _day_seed(date_str), ch_id,
                is_scheduled=is_scheduled,
                scheduled_cfg=ch if is_scheduled else None,
            )
            
            # ── B. Build source_mode sequence ──
            viral_n = ch.get("viral_per_day", 0)
            mode_sequence = _build_source_mode_sequence(n, viral_n) if n > 0 else ["original"] * n
            
            for pos, (peak_h, peak_m) in enumerate(raw_peaks, 1):
                target_public_at = f"{date_str} {peak_h:02d}:{peak_m:02d}:00"
                
                # ── C. Compute target_upload_at (within upload window on pub day) ──
                upload_windows = ch.get("upload_windows")
                win_start = ch.get("upload_window_start", 9)
                win_end = ch.get("upload_window_end", 11)
                if not is_scheduled:
                    # Immediate mode: upload = right after gen (use target_public_at as deadline)
                    up_h, up_m = peak_h, peak_m
                else:
                    up_h, up_m, ws_start, ws_end = _pick_upload_minute(
                        _day_seed(date_str), ch_id, pos,
                        window_start=win_start, window_end=win_end,
                        videos_per_day=n, windows=upload_windows,
                    )
                target_upload_at = f"{date_str} {up_h:02d}:{up_m:02d}:00"
                
                all_raw_slots.append({
                    "channel_id": ch_id,
                    "date_key": date_str,           # Publication day
                    "target_public_at": target_public_at,
                    "target_upload_at": target_upload_at,
                    "slot_position": pos,
                    "channel_name": ch.get("name", ""),
                    "channel_slug": ch.get("slug", ""),
                    "source_mode": mode_sequence[pos - 1] if (pos - 1) < len(mode_sequence) else "original",
                    "publish_mode": ch.get("publish_mode", "immediate"),
                    "lead_hours": ch.get("generation_lead_hours", 36),
                    "avg_duration_min": ch.get("avg_duration_min", ESTIMATED_PIPELINE_MINUTES),
                    "upload_window_start": ws_start,
                    "upload_window_end": ws_end,
                })
    
    if not all_raw_slots:
        return []
    
    # ── 2. Sort all slots by target_upload_at ───────────────────
    all_raw_slots.sort(key=lambda s: s["target_upload_at"])
    
    # ── 3. Chain generation starts (walk BACKWARDS) ─────────────
    # Walk from the latest slot to the earliest, chaining generation starts
    # so each gen finishes before the NEXT slot's upload window.
    
    next_scheduled_at = None  # The next (later) slot's scheduled_at
    
    for i in range(len(all_raw_slots) - 1, -1, -1):
        s = all_raw_slots[i]
        ch_dur = s["avg_duration_min"]
        lead_h = s["lead_hours"]
        is_scheduled = s["publish_mode"] == "scheduled"
        
        # ── Adaptive lead time: increase buffer when behind schedule ──
        # If past-due slots exist for this channel, boost lead_hours so
        # future slots are planned with much more margin, building a
        # pre-generated buffer that prevents future delays.
        if is_scheduled:
            try:
                from database.db_extended import ExtendedDatabase
                _adap_db = ExtendedDatabase()
                past_due = _adap_db.count_past_due_slots()
                if past_due > 0:
                    # Boost: base lead + extra per past-due slot (max 72h)
                    lead_h = min(lead_h + past_due * 2, 72)
            except Exception:
                pass
        
        # Parse target times
        upload_dt = _dt.strptime(s["target_upload_at"], "%Y-%m-%d %H:%M:%S")
        public_dt = _dt.strptime(s["target_public_at"], "%Y-%m-%d %H:%M:%S")
        
        # ── Latest allowed start: must finish before upload window ──
        latest_start = upload_dt - _td(minutes=ch_dur)
        
        # ── Earliest allowed start: no more than lead_hours before public ──
        earliest_start = public_dt - _td(hours=lead_h)
        
        if not is_scheduled:
            # Immediate mode: same logic as before (back from public)
            latest_start = public_dt - _td(minutes=ESTIMATED_PIPELINE_MINUTES)
            earliest_start = latest_start  # No lead time for immediate
        
        # ── Chain constraint: must finish before next upload ──
        if next_scheduled_at is not None:
            chained_latest = next_scheduled_at - _td(minutes=GLOBAL_GAP_MINUTES) - _td(minutes=ch_dur)
            latest_start = min(latest_start, chained_latest)
        
        # ── Pick the start time ──
        if latest_start < earliest_start:
            # Overcapacity: can't fit in lead window. Respect chain constraint
            # anyway to maintain realistic gaps between consecutive generations.
            # Violates lead_hours but prevents impossible 5-minute gaps.
            if next_scheduled_at is not None:
                scheduled_dt = next_scheduled_at - _td(minutes=GLOBAL_GAP_MINUTES) - _td(minutes=ch_dur)
            else:
                scheduled_dt = latest_start  # no chain constraint to respect
            logger.warning(
                "Overcapacity: %s slot #%d (pub=%s) — "
                "latest_start=%s < earliest_start=%s. Chaining with gap=%dmin.",
                s["channel_slug"], s["slot_position"],
                s["target_public_at"],
                latest_start.strftime("%m-%d %H:%M"),
                earliest_start.strftime("%m-%d %H:%M"),
                GLOBAL_GAP_MINUTES,
            )
        else:
            # Start as EARLY as possible within the window (clear the queue quickly)
            scheduled_dt = earliest_start
        
        s["scheduled_at"] = scheduled_dt.strftime("%Y-%m-%d %H:%M:%S")
        next_scheduled_at = scheduled_dt
    
    # ── 4. Sort final output by scheduled_at ────────────────────
    all_raw_slots.sort(key=lambda s: s["scheduled_at"])
    
    for pos, s in enumerate(all_raw_slots, 1):
        s["slot_position"] = pos
    
    return all_raw_slots


# ── Persistence layer ─────────────────────────────────────────

def compute_and_store_slots(
    date_str: Optional[str] = None,
    db=None,
) -> dict:
    """Compute slots for a single date and store them in planned_slots.
    
    Legacy single-day planning — for backward compatibility.
    Prefer compute_and_store_horizon() for scheduled channels.
    
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
        slots_by_channel[ch_id].append(s.get("target_upload_at", "")[11:16])  # show upload times in log
    
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


def _augment_channel_configs(db, channel_configs: list[dict]) -> list[dict]:
    """Add per-channel avg_duration_min from historical timing data."""
    from api.services.schedule_engine import get_avg_creation_minutes
    
    for cfg in channel_configs:
        ch_id = cfg["channel_id"]
        try:
            avg = get_avg_creation_minutes(ch_id)
            cfg["avg_duration_min"] = avg * (1 + BUFFER_PCT)  # buffered
        except Exception:
            cfg["avg_duration_min"] = ESTIMATED_PIPELINE_MINUTES
    return channel_configs


def compute_and_store_horizon(
    horizon_days: int = DEFAULT_HORIZON_DAYS,
    db=None,
    force_replan: bool = False,
) -> dict:
    """Plan and store all slots across the N-day horizon using 3-phase cross-day chaining.
    
    Replaces the old per-day planning loop. Drops and recreates ALL pending
    slots across the horizon to ensure a fresh, coherent plan.
    
    Args:
        horizon_days: days to plan (including today).
        db: ExtendedDatabase instance.
        force_replan: if True, also drop running slots (use with care).
    
    Returns:
        dict with {total_slots, days_planned, slots_by_channel}.
    """
    from datetime import date as _date
    
    # ── Cooldown guard ──────────────────────────────────
    global _last_horizon_replan_ts
    now = datetime.now()
    if _last_horizon_replan_ts is not None and not force_replan:
        elapsed = (now - _last_horizon_replan_ts).total_seconds() / 60
        if elapsed < _HORIZON_REPLAN_COOLDOWN_MIN:
            logger.info(
                "compute_and_store_horizon: skipped — last replan %.0f min ago (cooldown=%d min)",
                elapsed, _HORIZON_REPLAN_COOLDOWN_MIN,
            )
            return {
                "total_slots": 0,
                "days_planned": 0,
                "slots_by_channel": {},
                "skipped": True,
                "reason": f"cooldown ({_HORIZON_REPLAN_COOLDOWN_MIN} min)",
            }
    _last_horizon_replan_ts = now
    # ────────────────────────────────────────────────────
    
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
        logger.info("compute_and_store_horizon: no active planning channels")
        return {"total_slots": 0, "days_planned": 0, "slots_by_channel": {}}
    
    # Augment with real durations
    channel_configs = _augment_channel_configs(db, channel_configs)
    
    today = _date.today()
    
    # Delete existing PENDING slots across the horizon
    with db._connect() as conn:
        start_date = today.isoformat()
        end_date = (today + timedelta(days=horizon_days)).isoformat()
        deleted = conn.execute(
            "DELETE FROM planned_slots WHERE date_key >= ? AND date_key < ? AND status = 'pending'",
            (start_date, end_date),
        ).rowcount
        if force_replan:
            # Also cancel running slots that are stale (no job)
            conn.execute(
                "UPDATE planned_slots SET status = 'cancelled' "
                "WHERE date_key >= ? AND date_key < ? AND status = 'running' "
                "AND job_id IS NULL",
                (start_date, end_date),
            )
        conn.commit()
        if deleted:
            logger.info("Horizon replan: cleared %d pending slots", deleted)
    
    # Compute horizon slots
    slots = compute_horizon_slots(channel_configs, horizon_days=horizon_days, db=db)
    
    if not slots:
        logger.info("compute_and_store_horizon: no slots to plan")
        return {"total_slots": 0, "days_planned": 0, "slots_by_channel": {}}
    
    # Store them
    stored = db.create_planned_slots_batch(slots)
    
    # Collect stats
    slots_by_channel = {}
    days_used = set()
    for s in slots:
        ch_id = s["channel_id"]
        if ch_id not in slots_by_channel:
            slots_by_channel[ch_id] = []
        slots_by_channel[ch_id].append({
            "date": s["date_key"],
            "gen": s["scheduled_at"][11:16],
            "upload": s["target_upload_at"][11:16],
            "public": s["target_public_at"][11:16],
        })
        days_used.add(s["date_key"])
    
    # Log summary
    for ch_id, items in slots_by_channel.items():
        slug = items[0] if items else "?"
        logger.info(
            "  %s: %d slots, gen starts: %s",
            next(s["channel_slug"] for s in slots if s["channel_id"] == ch_id) if slots else "?",
            len(items),
            ", ".join(f"{i['date']}@{i['gen']}" for i in items),
        )
    
    logger.info(
        "Horizon planned: %d days, %d slots across %d channels",
        len(days_used), stored, len(slots_by_channel),
    )
    
    # ── Overcapacity check ──
    total_gen_min = sum(s["avg_duration_min"] + GLOBAL_GAP_MINUTES for s in slots)
    available_hours = horizon_days * 24
    if total_gen_min > available_hours * 60 * 0.9:  # >90% capacity
        logger.warning(
            "⚠️  OVERCAPACITY RISK: %.0f h of generation needed for %d days "
            "(%.1f%% of available time). Consider reducing videos_per_day or increasing lead_hours.",
            total_gen_min / 60, horizon_days,
            (total_gen_min / (available_hours * 60)) * 100,
        )
    
    return {
        "total_slots": stored,
        "days_planned": len(days_used),
        "slots_by_channel": {ch_id: len(items) for ch_id, items in slots_by_channel.items()},
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
        
        # Count real publications (includes manual videos) + active slots only.
        # NOT counting "completed" slots — they may be error videos (pre-fix)
        # or already accounted for via published_today (avoiding double count).
        published_today = db.get_videos_published_today(ch_id)
        running = sum(1 for s in ch_slots if s["status"] == "running")
        pending = [s for s in ch_slots if s["status"] == "pending"]
        
        current_planned = published_today + running + len(pending)
        target_planned = target
        
        if current_planned == target_planned:
            continue  # No change needed
        
        if current_planned > target_planned:
            # Too many: cancel the last N pending slots.
            # Sort so 'original' slots are cancelled before 'viral' slots,
            # preserving viral quota whenever possible.
            excess = current_planned - target_planned
            pending.sort(key=lambda s: 0 if s.get("source_mode") == "viral" else 1)
            to_cancel = pending[:excess] if excess <= len(pending) else pending
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
                "viral_per_day": cfg.get("viral_per_day", 0),
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


# ── Smart replanning (v2) ─────────────────────────────────────

# Never drop below this many pending slots across all channels.
# If below threshold, auto-trigger a full horizon replan.
MIN_PENDING_SLOTS = 3

# Days ahead to check for empty coverage (no slots at all for that day)
EMPTY_DAY_CHECK_DAYS = 3


def smart_replan(db=None) -> dict:
    """Periodic intelligent replan: cancel fulfilled quotas, detect config changes,
    cancel stale slots, fill empty days, warn on overcapacity.
    
    Called every ~30 min during active hours (10:00-23:00) by the checker loop.
    Automatically triggers full horizon replan if:
      - Pending slots drop below MIN_PENDING_SLOTS
      - Any of the next EMPTY_DAY_CHECK_DAYS has zero slots
      - Channel config (videos_per_day) changed
    
    Returns dict with: {cancelled_count, horiz_replan, channels_adjusted, overcapacity_warn}
    """
    import json as _json
    from datetime import date as _date, datetime as _dt, timedelta as _td
    
    if db is None:
        from database.db_extended import ExtendedDatabase
        db = ExtendedDatabase()
    
    today = _date.today().isoformat()
    now = _dt.now()
    cancelled_total = 0
    channels_adjusted = []
    overcapacity_warn = False
    horizon_replanned = False
    
    # ── 0. Count total pending slots ──
    with db._connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) as cnt FROM planned_slots WHERE status='pending'"
        ).fetchone()
        total_pending = row["cnt"] if row else 0
    
    # ── 0a. Auto-replan if pipeline is running dry ──
    if total_pending < MIN_PENDING_SLOTS:
        logger.warning(
            "Pipeline running DRY: %d pending slots (min=%d). "
            "Triggering full horizon replan.",
            total_pending, MIN_PENDING_SLOTS,
        )
        result = compute_and_store_horizon(horizon_days=7, db=db, force_replan=True)
        horizon_replanned = True
        logger.info(
            "Auto-replan complete: %d slots across %d days",
            result.get("total_slots", 0), result.get("days_planned", 0),
        )
        # Refresh pending count
        with db._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) as cnt FROM planned_slots WHERE status='pending'"
            ).fetchone()
            total_pending = row["cnt"] if row else 0
    
    # ── 0b. Detect empty days in the horizon ──
    need_replan = False
    for day_offset in range(EMPTY_DAY_CHECK_DAYS):
        check_day = (_date.today() + _td(days=day_offset)).isoformat()
        with db._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) as cnt FROM planned_slots WHERE date_key=? AND status='pending'",
                (check_day,),
            ).fetchone()
            day_count = row["cnt"] if row else 0
        if day_count == 0:
            logger.warning(
                "Empty day detected: %s has 0 pending slots. Triggering horizon replan.",
                check_day,
            )
            need_replan = True
            break
    
    if need_replan and not horizon_replanned:
        result = compute_and_store_horizon(horizon_days=7, db=db, force_replan=True)
        horizon_replanned = True
        logger.info(
            "Empty-day replan complete: %d slots across %d days",
            result.get("total_slots", 0), result.get("days_planned", 0),
        )
        with db._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) as cnt FROM planned_slots WHERE status='pending'"
            ).fetchone()
            total_pending = row["cnt"] if row else 0
    
    # ── 1. Load all active channels ──
    channels = db.get_channels(active_only=True)
    if not channels:
        return {"cancelled_count": 0, "horiz_replan": horizon_replanned,
                "channels_adjusted": [], "overcapacity_warn": False}
    
    for ch in channels:
        ch_id = ch.id if hasattr(ch, 'id') else ch.get("id", 0)
        slug = ch.slug if hasattr(ch, 'slug') else ch.get("slug", "?")
        cfg_raw = ch.config_json if hasattr(ch, 'config_json') else ch.get("config_json", "{}")
        try:
            cfg = _json.loads(cfg_raw) if isinstance(cfg_raw, str) else (cfg_raw or {})
        except (_json.JSONDecodeError, TypeError):
            cfg = {}
        
        vpd = int(cfg.get("videos_per_day", 1) or 1)
        planning_enabled = cfg.get("planning_enabled", True)
        
        if not planning_enabled:
            with db._connect() as conn:
                cnt = conn.execute(
                    "UPDATE planned_slots SET status='cancelled' WHERE channel_id=? AND status='pending'",
                    (ch_id,),
                ).rowcount
                conn.commit()
            if cnt > 0:
                logger.info("Smart replan: cancelled %d slots for %s (planning disabled)", cnt, slug)
                cancelled_total += cnt
                channels_adjusted.append(slug)
            continue
        
        # ── 2a. Count videos done today ──
        generated_today = db.count_videos_generated_today(ch_id)
        
        # ── 2b. Cancel today's slots if quota met ──
        with db._connect() as conn:
            today_cnt = conn.execute(
                "SELECT COUNT(*) as cnt FROM planned_slots WHERE channel_id=? AND date_key=? AND status='pending'",
                (ch_id, today),
            ).fetchone()
            today_pending = today_cnt["cnt"] if today_cnt else 0
        
        if generated_today >= vpd and today_pending > 0:
            with db._connect() as conn:
                cnt = conn.execute(
                    "UPDATE planned_slots SET status='cancelled' WHERE channel_id=? AND date_key=? AND status='pending'",
                    (ch_id, today),
                ).rowcount
                conn.commit()
            logger.info(
                "Smart replan: cancelled %d today-slots for %s (quota met: %d/%d)",
                cnt, slug, generated_today, vpd,
            )
            cancelled_total += cnt
            channels_adjusted.append(slug)
        
        # ── 2c. Detect config change vs tomorrow's slots ──
        if not horizon_replanned:
            tomorrow_str = (_date.today() + _td(days=1)).isoformat()
            with db._connect() as conn:
                tomorrow_cnt = conn.execute(
                    "SELECT COUNT(*) as cnt FROM planned_slots WHERE channel_id=? AND date_key=? AND status='pending'",
                    (ch_id, tomorrow_str),
                ).fetchone()
                tcnt = tomorrow_cnt["cnt"] if tomorrow_cnt else 0
            if tcnt > 0 and tcnt != vpd:
                logger.warning(
                    "Config mismatch for %s: tomorrow has %d slots but videos_per_day=%d. "
                    "Triggering horizon replan.",
                    slug, tcnt, vpd,
                )
                if not horizon_replanned:
                    result = compute_and_store_horizon(horizon_days=7, db=db, force_replan=True)
                    horizon_replanned = True
                    logger.info(
                        "Config-change replan complete: %d slots",
                        result.get("total_slots", 0),
                    )
        
        # ── 2d. Cancel stale slots — only for today/past dates.
        #     Future-date slots with old scheduled_at are pre-generated
        #     buffer slots and should NOT be cancelled.
        with db._connect() as conn:
            stale_cnt = conn.execute(
                """UPDATE planned_slots SET status='cancelled'
                   WHERE channel_id=? AND status='pending'
                     AND date_key <= date('now', 'localtime')
                     AND scheduled_at <= datetime('now', 'localtime', '-6 hours')""",
                (ch_id,),
            ).rowcount
            conn.commit()
        if stale_cnt > 0:
            logger.info("Smart replan: cancelled %d stale slots for %s (today/past only)", stale_cnt, slug)
            cancelled_total += stale_cnt
    
    # ── 3. Overcapacity check ──
    daily_capacity = 8  # with pipelining
    max_realistic = daily_capacity * 2  # 2 days worth
    
    # Refresh count if replan happened
    if not horizon_replanned:
        with db._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) as cnt FROM planned_slots WHERE status='pending'"
            ).fetchone()
            total_pending = row["cnt"] if row else 0
    
    if total_pending > max_realistic:
        logger.warning(
            "Smart replan: overcapacity — %d pending slots vs ~%d realistic (2 days).",
            total_pending, max_realistic,
        )
        overcapacity_warn = True
    
    return {
        "cancelled_count": cancelled_total,
        "horiz_replan": horizon_replanned,
        "channels_adjusted": list(set(channels_adjusted)),
        "overcapacity_warn": overcapacity_warn,
        "pending_total": total_pending,
    }


# ── Scheduler integration ─────────────────────────────────────

def _score_priority_slot(slot: dict, db, today_str: str) -> int:
    """Score a slot for priority dispatch. Higher = dispatch first.
    
    Factors:
      - date_key proximity: today=100, tomorrow=70, +2=40, +3+=10
      - deadline urgency: past-due but still viable → +50
      - channel fairness: no video generated today → +30
      - buffer pressure: exceeding max_awaiting_upload → -30
      - priority_weight: per-channel multiplier (from config_json, default 1.0)
    """
    from datetime import date, datetime
    
    score = 0
    slot_date = slot["date_key"]
    ch_id = slot["channel_id"]
    
    # ── Load channel config overrides ──
    cfg_json = slot.get("config_json", "{}")
    if isinstance(cfg_json, str):
        import json as _j
        try:
            cfg_json = _j.loads(cfg_json)
        except Exception:
            cfg_json = {}
    elif cfg_json is None:
        cfg_json = {}
    
    max_awaiting = int(cfg_json.get("max_awaiting_upload", 3) or 3)
    priority_weight = float(cfg_json.get("priority_weight", 1.0) or 1.0)
    
    # ── 1. Date proximity ──
    days_ahead = (datetime.strptime(slot_date, "%Y-%m-%d").date() - date.today()).days
    if days_ahead == 0:
        score += 100
    elif days_ahead == 1:
        score += 70
    elif days_ahead == 2:
        score += 40
    else:
        score += max(5, 30 - days_ahead * 5)
    
    # ── 2. Deadline urgency ──
    upload_at = slot.get("target_upload_at")
    if upload_at:
        try:
            upload_dt = datetime.strptime(str(upload_at)[:19], "%Y-%m-%d %H:%M:%S")
            if upload_dt < datetime.now():
                score += 50  # catch-up bonus: this is late but still viable
        except (ValueError, TypeError):
            pass
    
    # ── 3. Channel fairness: channel with nothing generated today gets priority ──
    try:
        generated_today = db.count_videos_generated_today(ch_id)
        if generated_today == 0:
            vpd = int(cfg_json.get("videos_per_day", 1) or 1)
            if vpd > 0:
                score += 30  # channel needs content today
    except Exception:
        pass
    
    # ── 4. Buffer pressure: too many awaiting → lower priority ──
    try:
        awaiting = db.count_awaiting_upload(ch_id)
        if awaiting >= max_awaiting:
            score -= 30
    except Exception:
        pass
    
    # ── 5. Apply channel priority weight ──
    if priority_weight != 1.0:
        score = int(score * priority_weight)
    
    return score


def process_planned_slots(db=None) -> dict | None:
    """Check for due planned slots and dispatch generation if possible.
    
    Called every 5 min by the API checker loop.
    
    v9 3-phase model:
      - Scheduled channels: dispatch with generate_only (F1), upload later (F2)
      - Pull-forward: dispatch early if worker is idle and target_public_at 
        is within the channel's lead window.
    
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
    
    # 1b. Cancel stale pending slots (upload window already passed)
    _cancel_stale_slots(db)
    
    # 2. Find candidate slots and pick the best one by priority score
    from datetime import date as _date
    today_str = _date.today().isoformat()
    candidates = db.get_priority_slot_candidates(max_future_hours=36, limit=20)
    if not candidates:
        return None
    
    # Score each candidate and sort by priority (highest first)
    for c in candidates:
        c["_priority_score"] = _score_priority_slot(c, db, today_str)
    candidates.sort(key=lambda c: c["_priority_score"], reverse=True)
    
    # Pick the highest-scoring candidate
    next_slot = candidates[0]
    best_score = next_slot.get("_priority_score", 0)
    slug_preview = next_slot.get("channel_slug", "?")
    if len(candidates) > 1:
        runner_up = candidates[1]
        logger.info(
            "Priority dispatch: %d candidates → selected %s (score=%d, pub=%s) "
            "over %s (score=%d)",
            len(candidates), slug_preview, best_score,
            (next_slot.get("target_public_at") or "?")[:16],
            runner_up.get("channel_slug", "?"), runner_up.get("_priority_score", 0),
        )
    else:
        logger.info(
            "Priority dispatch: 1 candidate → %s (score=%d)",
            slug_preview, best_score,
        )
    
    # 2b. Is there already an active job for this channel?
    active = db.get_active_job_for_channel(next_slot["channel_id"])
    if active:
        logger.debug("Planned slot skipped: channel %d already has active job #%d",
                     next_slot["channel_id"], active["id"])
        return None
    
    # 2c. Phase-pipelining guard: allow up to 1 render + 1 prep concurrently.
    #     - If no render is active → dispatch any job (it will claim render slot).
    #     - If 1 render is active → allow dispatching 1 PREP worker (2 total).
    #     - If 2 jobs already active → defer dispatch.
    active_count = db.count_active_jobs()
    render_count = db.count_render_phase_jobs()
    MAX_TOTAL_JOBS = 2
    MAX_RENDER_JOBS = 1
    
    if active_count >= MAX_TOTAL_JOBS:
        logger.info(
            "Planned slot deferred: %d/%d active jobs (render=%d) — at capacity",
            active_count, MAX_TOTAL_JOBS, render_count,
        )
        return None
    
    if render_count >= MAX_RENDER_JOBS:
        # A render is active. Check if this channel allows pipelining.
        ch_cfg = {}
        try:
            cfg_raw = next_slot.get("config_json", "{}")
            if isinstance(cfg_raw, str):
                import json as _j2
                ch_cfg = _j2.loads(cfg_raw) if cfg_raw else {}
            elif cfg_raw:
                ch_cfg = cfg_raw
        except Exception:
            pass
        if ch_cfg.get("pipeline_enabled", True) is False:
            logger.info(
                "Planned slot deferred: render active + pipeline disabled for %s",
                slug_preview,
            )
            return None
        
        # A render is active. Only allow dispatch if there's room for a prep worker.
        if active_count >= MAX_TOTAL_JOBS:
            logger.info(
                "Planned slot deferred: render active + %d total jobs = at capacity",
                active_count,
            )
            return None
        logger.info(
            "Phase pipelining: render active (%d), dispatching prep worker "
            "(total active: %d → %d)",
            render_count, active_count, active_count + 1,
        )
    
    # 3b. Memory guard: different threshold for prep vs render dispatch.
    #     Prep phases (scrape→media) need ~1.5 GB. Render needs ~4 GB.
    if render_count > 0:
        # Dispatching a prep worker while a render is active
        if not _memory_ok(min_free_gb=1.5):
            logger.warning("Low memory (prep) — delaying planned slot dispatch")
            return None
    else:
        if not _memory_ok(min_free_gb=4.0):
            logger.warning("Low memory (render) — delaying planned slot dispatch")
            return None
    
    # ── Enter dispatch critical section ──────────────────────────
    # Acquire global lock before atomically creating the job.
    # The lock + guards together prevent TOCTOU races with other
    # dispatch entry points (manual click, due schedules, priority).
    with _DISPATCH_LOCK:
        # Re-check guard under lock (belts-and-suspenders for TOCTOU)
        active_under = db.count_active_jobs()
        render_under = db.count_render_phase_jobs()
        if active_under >= MAX_TOTAL_JOBS:
            logger.info("Planned slot deferred (under lock): %d/%d jobs active",
                        active_under, MAX_TOTAL_JOBS)
            return None
        if render_under >= MAX_RENDER_JOBS and active_under >= MAX_TOTAL_JOBS:
            logger.info("Planned slot deferred (under lock): render active + at capacity")
            return None
        
        slot_id = next_slot["id"]
        channel_id = next_slot["channel_id"]
        slug = next_slot.get("channel_slug", "")
        source_mode = next_slot.get("source_mode", "original")
        
        logger.info(
            "Dispatching slot #%d: %s (scheduled=%s, pub=%s)",
            slot_id, slug,
            (next_slot.get("scheduled_at") or "?")[11:16],
            (next_slot.get("target_public_at") or "?")[11:16] if next_slot.get("target_public_at") else "?",
        )
        
        # 4. Mark slot as running
        db.update_slot_status(slot_id, "running")
        
        # 5. Create the video record
        from database.db_extended import ExtendedDatabase
        
        # Get channel config to check publish mode
        ch_cfg = db.get_channel_planning_config(channel_id)
        publish_mode = ch_cfg.get("publish_mode", "immediate") if ch_cfg else "immediate"
        is_scheduled = publish_mode == "scheduled"
        
        # ── Use the correct target_public_at (peak publish time) ──
        target_public_at = next_slot.get("target_public_at")
        
        with db._connect() as conn:
            cursor = conn.execute(
                "INSERT INTO videos (canal, channel_id, video_path, status, progress, "
                "publish_mode, target_public_at, created_at) "
                "VALUES (?, ?, '', 'generating', 0, ?, ?, CURRENT_TIMESTAMP)",
                (slug, channel_id, publish_mode, target_public_at),
            )
            conn.commit()
            video_id = cursor.lastrowid
        
        # 6. Determine action — scheduled channels use generate_only (F1 only)
        if is_scheduled:
            action = "generate_only"
            # Don't pass upload=True — the worker will skip upload and keep mp4
        else:
            action = "generate_and_upload"
        
        # 7. Create job and mark it running IMMEDIATELY
        job_id = db.create_job(channel_id, action, video_id)
        db.update_job(job_id, status="running")
        
        # 8. Link job to slot
        db.update_slot_status(slot_id, "running", job_id=job_id, video_id=video_id)
    # ── End dispatch critical section ────────────────────────────
    
    # 9. Fire and forget the generation
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
                action=action,
                source_mode=source_mode,
            )
        )
    else:
        asyncio.create_task(
            start_generation_job(
                job_id=job_id,
                channel_id=channel_id,
                video_id=video_id,
                action=action,
                source_mode=source_mode,
            )
        )
    
    return {
        "slot_id": slot_id,
        "job_id": job_id,
        "video_id": video_id,
        "channel_slug": slug,
        "action": action,
    }


def _ensure_never_dry(db) -> bool:
    """Fallback: if no slot could be dispatched, check if the pipeline is empty
    and auto-replan the horizon to keep generation flowing.
    
    Returns True if a replan was triggered.
    """
    # Check if there's at least one pending slot in the next 72h
    candidates = db.get_priority_slot_candidates(max_future_hours=72, limit=1)
    if candidates:
        return False  # slots exist, just not dispatchable right now
    
    # No slots in the next 3 days — pipeline is empty!
    logger.warning(
        "Pipeline EMPTY: no dispatchable slots in next 72h. "
        "Triggering emergency horizon replan."
    )
    try:
        result = compute_and_store_horizon(horizon_days=7, db=db, force_replan=True)
        logger.info(
            "Emergency replan complete: %d slots across %d days",
            result.get("total_slots", 0), result.get("days_planned", 0),
        )
        return True
    except Exception as exc:
        logger.error("Emergency replan failed: %s", exc)
        return False


def _sync_running_slots(db):
    """Check ALL running slots: if their job is done, mark the slot accordingly.
    
    Scans every slot with status='running' across ALL dates, not just today.
    This prevents orphaned running slots from accumulating when jobs are
    dispatched from future date_keys and subsequently fail.
    
    Also triggers _readjust_pending_slots() when a job completes, to
    realign the remaining slots and avoid cascading time drift.
    """
    running_slots = db.get_planned_slots(status="running")
    any_completed = False
    
    for s in running_slots:
        job_id = s.get("job_id")
        if not job_id:
            # No job linked at all → stale, mark cancelled (never consumed)
            db.update_slot_status(s["id"], "cancelled")
            logger.info("Slot #%d cancelled (no job linked — stale)", s["id"])
            continue
        job = db.get_job(job_id)
        if not job:
            # Job row missing → stale, mark cancelled (never consumed)
            db.update_slot_status(s["id"], "cancelled")
            logger.info("Slot #%d cancelled (job #%d not found)", s["id"], job_id)
            continue
        if job["status"] in ("completed", "success"):
            db.update_slot_status(s["id"], "completed")
            logger.info("Slot #%d marked completed (job #%d done)", s["id"], job_id)
            any_completed = True
        elif job["status"] in ("failed", "cancelled"):
            # Job failed or was cancelled → slot was never consumed
            db.update_slot_status(s["id"], "cancelled")
            logger.info("Slot #%d cancelled (job #%d %s)", s["id"], job_id, job["status"])
            # NOT any_completed — failed slots should NOT trigger readjustment
    
    if any_completed:
        _readjust_pending_slots(db)


def _readjust_pending_slots(db):
    """Realign remaining pending slots after a job finishes — cross-day aware.
    
    When a generation job completes, pulls forward the next pending slot
    regardless of its date_key. All pending slots across the horizon are
    re-chained with per-channel durations and GLOBAL_GAP_MINUTES.
    
    target_upload_at and target_public_at are preserved — only scheduled_at
    (generation start) is adjusted for the real timeline.
    """
    GAP = GLOBAL_GAP_MINUTES
    
    # Get ALL pending slots across the horizon (not just today)
    now = datetime.now()
    today = date.today().isoformat()
    horizon_end = (date.today() + timedelta(days=DEFAULT_HORIZON_DAYS)).isoformat()
    
    all_pending = db.get_planned_slots_week(today, horizon_end)
    pending = [s for s in all_pending if s["status"] == "pending"]
    if not pending:
        return
    
    # Sort by current scheduled_at
    pending_sorted = sorted(pending, key=lambda s: s.get("scheduled_at", "9999"))
    
    # Anchor: now + gap (real time)
    anchor = now + timedelta(minutes=GAP)
    
    logger.info(
        "Readjusting %d pending slots cross-day from anchor=%s",
        len(pending_sorted), anchor.strftime("%m-%d %H:%M"),
    )
    
    next_start = anchor
    for slot in pending_sorted:
        # Get channel's avg duration
        ch_dur = ESTIMATED_PIPELINE_MINUTES  # fallback
        try:
            from api.services.schedule_engine import get_avg_creation_minutes
            ch_dur = get_avg_creation_minutes(slot["channel_id"]) * (1 + BUFFER_PCT)
        except Exception:
            pass
        
        old_sched = slot.get("scheduled_at", "?")[11:16] if slot.get("scheduled_at") else "?"
        
        new_sched = next_start.strftime("%Y-%m-%d %H:%M:%S")
        new_sched_short = next_start.strftime("%H:%M")
        
        if old_sched != new_sched_short:
            logger.info(
                "  Slot #%d (%s): gen %s → %s (pub=%s, upload=%s)",
                slot["id"], slot.get("channel_slug", "?"),
                old_sched, new_sched_short,
                (slot.get("target_public_at") or "?")[11:16] if slot.get("target_public_at") else "?",
                (slot.get("target_upload_at") or "?")[11:16] if slot.get("target_upload_at") else "?",
            )
        
        with db._connect() as conn:
            conn.execute(
                "UPDATE planned_slots SET scheduled_at = ? WHERE id = ?",
                (new_sched, slot["id"]),
            )
            conn.commit()
        
        next_start = next_start + timedelta(minutes=ch_dur + GAP)


def _cancel_stale_slots(db):
    """Cancel pending slots whose upload window has already passed.

    In the 3-phase model, a slot is "stale" if its target_upload_at is in the past
    (the upload window for that day has come and gone). These slots can never be
    generated+uploaded in time.

    Also cancels slots whose scheduled_at is >6h in the past (server crash/restart
    scenarios where the dispatcher never picked them up).

    Viral protection: stale viral slots are preserved if the channel has NOT yet
    generated any viral video today — this ensures the viral quota is met even when
    scheduling delays push viral slots past their window.

    Skips cancellation when an active generation is in progress — slots
    blocked by the global concurrency guard are held intentionally.
    """
    # Don't cancel slots held back by an active generation
    if db.count_active_jobs() > 0:
        return

    # Subquery: channels that already generated a viral video today.
    # Viral slots for these channels can be cancelled (quota met).
    # Viral slots for other channels are preserved (quota not met yet).
    channels_with_viral_today = """
        SELECT DISTINCT v.channel_id
        FROM videos v
        JOIN scripts sc ON v.script_id = sc.id
        JOIN raw_content rc ON sc.raw_content_id = rc.id
        WHERE rc.source_mode = 'viral'
          AND DATE(v.uploaded_at) = DATE('now', 'localtime')
          AND v.yt_video_id IS NOT NULL
    """

    cancelled = 0
    with db._connect() as conn:
        # Cancel slots whose upload window has passed.
        # Viral slots are only cancelled if the channel already has a
        # viral video today; otherwise they are preserved.
        c1 = conn.execute(
            f"""UPDATE planned_slots SET status = 'cancelled'
               WHERE status = 'pending'
                 AND (
                     source_mode != 'viral'
                     OR channel_id IN ({channels_with_viral_today})
                 )
                 AND target_upload_at IS NOT NULL
                 AND target_upload_at <= datetime('now', 'localtime', '-30 minutes')"""
        ).rowcount

        # Also cancel very old pending slots (>6h past scheduled_at).
        # Only for today/past dates — future-date slots with old scheduled_at
        # are pre-generated buffer slots and should NOT be cancelled.
        c2 = conn.execute(
            f"""UPDATE planned_slots SET status = 'cancelled'
               WHERE status = 'pending'
                 AND date_key <= date('now', 'localtime')
                 AND (
                     source_mode != 'viral'
                     OR channel_id IN ({channels_with_viral_today})
                 )
                 AND scheduled_at <= datetime('now', 'localtime', '-6 hours')"""
        ).rowcount

        conn.commit()
        cancelled = c1 + c2

    if cancelled:
        logger.info("Cancelled %d stale pending slots (upload window passed: %d, very old: %d)",
                     cancelled, c1, c2)


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
    """Ensure today has planned_slots. Called on API startup.
    
    Now uses horizon planning to rebuild the full week if empty.
    """
    if db is None:
        from database.db_extended import ExtendedDatabase
        db = ExtendedDatabase()
    
    today = date.today().isoformat()
    existing = db.get_planned_slots(date_key=today)
    
    if not existing:
        logger.info("No slots found for today (%s) — computing horizon...", today)
        compute_and_store_horizon(horizon_days=7, db=db)
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

    # Fetch recent topics to avoid repetition
    from database.db_extended import ExtendedDatabase
    dbx = ExtendedDatabase(str(DATABASE_PATH))
    recent_topics = dbx.get_recent_short_topics(channel_id, limit=15)
    topic_warning = ""
    if recent_topics:
        topic_list = "\n".join(f'  - "{t}"' for t in recent_topics)
        topic_warning = (
            f"\n\n⚠️ IMPORTANTE: NO repitas NINGUNO de estos temas ya publicados "
            f"recientemente en este canal:\n{topic_list}\n\n"
            f"Elige un tema COMPLETAMENTE DIFERENTE y fresco. "
            f"Incluye en el JSON un campo \"tema\" con una frase corta (max 80 chars) "
            f"que identifique claramente de qué trata este Short.\n"
        )
    else:
        topic_warning = (
            f"\n\nIncluye en el JSON un campo \"tema\" con una frase corta (max 80 chars) "
            f"que identifique claramente de qué trata este Short.\n"
        )

    # 1. Script via LLM
    from openai import OpenAI
    client = OpenAI(api_key=LLM_API_KEY, base_url=LLM_BASE_URL)
    niche = getattr(ch_config, "CANAL_NARRATIVE_STYLE", "documental")
    display_name = getattr(ch_config, "CANAL_DISPLAY_NAME", channel_slug)
    tagline = getattr(ch_config, "CANAL_TAGLINE", "")

    response = client.chat.completions.create(
        model=LLM_MODEL,
        messages=[{"role": "user", "content": f"Genera un Short viral en español de ~45-50 segundos (~65-80 palabras totales, minimo 50). Canal: {display_name} — {niche}. Tagline: {tagline}.{topic_warning}Usa 5 bloques (hook, desarrollo1, desarrollo2, climax, cierre). IMPORTANTE: desarrollo1, desarrollo2 y climax deben tener 2-3 frases cada uno. Hook y cierre: 1-2 frases. Minimo 10 palabras por bloque. El total debe superar 50 palabras. PARA CADA BLOQUE genera 'search_query_en': 5-8 keywords EN INGLÉS para buscar imágenes de stock que coincidan con lo narrado. Sé muy concreto: incluye tema + detalles visuales (iluminación, tipo de plano, atmósfera). NO uses español. Además genera 'theme_keywords_en': 5-8 keywords EN INGLÉS del tema visual GLOBAL del short. Devuelve SOLO JSON: {{\"tema\": \"frase corta que identifica el tema (max 80 chars)\", \"titulo\": \"...\", \"hook_text\": \"frase de gancho 8-12 palabras\", \"theme_keywords_en\": [\"global\", \"theme\"], \"bloques\": [{{\"tipo\": \"hook\", \"texto\": \"1-2 frases\", \"search_query_en\": \"english keywords\"}}, {{\"tipo\": \"desarrollo1\", \"texto\": \"2-3 frases con contexto y detalle\", \"search_query_en\": \"english keywords\"}}, {{\"tipo\": \"desarrollo2\", \"texto\": \"2-3 frases con dato impactante especifico\", \"search_query_en\": \"english keywords\"}}, {{\"tipo\": \"climax\", \"texto\": \"2-3 frases con la consecuencia o revelacion\", \"search_query_en\": \"english keywords\"}}, {{\"tipo\": \"cierre\", \"texto\": \"1-2 frases cierre + suscribete\", \"search_query_en\": \"english keywords\"}}]}}. NADA MAS fuera del JSON."}],
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
    topic = (script.get("tema") or "")[:200]  # store topic for dedup

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

    # 3. Fetch portrait images — build theme-aware English queries
    from pipeline.shorts_media import fetch_portrait_images, render_slideshow_with_images, _build_portrait_query
    theme_kw = script.get("theme_keywords_en", [])
    style_mod = getattr(ch_config, "IMAGE_STYLE_MODIFIERS", "")

    portrait_queries = []
    for b in bloques:
        search_en = b.get("search_query_en", "")
        if search_en and search_en.strip():
            portrait_queries.append(_build_portrait_query(search_en, theme_kw, style_mod))
        else:
            texto = b.get("texto", "")
            if texto.strip():
                portrait_queries.append(texto[:80])

    if not portrait_queries:
        portrait_queries = [hook_text[:80]]
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
        cursor = conn.execute("INSERT INTO shorts (channel_id, type, title, hook_title, hook_text, topic, status, file_path, youtube_id, youtube_url, published_at) VALUES (?, 'native', ?, ?, ?, ?, 'published', ?, ?, ?, datetime('now','localtime'))", (channel_id, title, title[:60], hook_text, topic, str(video_path), yt_id, result.get("url", "")))
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
