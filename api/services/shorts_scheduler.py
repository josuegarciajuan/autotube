"""
Per-channel shorts scheduling engine (v12 — dynamic clip scaling).
Computes publish slots/day/channel based on per-channel
shorts_planning_config (shorts_native_per_day + shorts_clips_per_long).

Key v12 changes:
- Native shorts: always 3 per day (configurable per channel)
- Clip shorts: shorts_clips_per_long × N_long_videos_planned_today (dynamic!)
- Native slots: 3-of-4 optimal franjas (daily rotation), spread across day
- Clip slots: anchored to their source long video's target_public_at,
  spread evenly across the remaining day from (long_publish + 45min) to 23:45
- Minimum 30 min spacing between any same-channel shorts
- ±20 min deterministic jitter per channel
- Fair daily rotation across channels
"""

import hashlib
import logging
import random
from datetime import date, datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

logger = logging.getLogger("autotube.shorts_scheduler")

# ── Auto-mark altered content helper (shorts) ─────────────────

def _auto_mark_ia_for_short(yt_id: str, channel_slug: str, account: str, short_id: int):
    """Background thread: mark short as AI-generated content.
    
    No end screens — YouTube doesn't support them on Shorts.
    """
    import time as _time
    try:
        _time.sleep(20)  # Wait for YouTube to finish processing
        from pipeline.youtube_browser import get_browser
        browser = get_browser(account)
        success = browser.mark_altered_content(yt_id)
        if success:
            import sqlite3
            from config.settings import DATABASE_PATH
            conn = sqlite3.connect(str(DATABASE_PATH), timeout=10)
            conn.execute(
                "UPDATE shorts SET manual_altered_content_done = 1 WHERE id = ?",
                (short_id,),
            )
            conn.commit()
            conn.close()
            logger.info("[%s] Altered content marked for short %s", channel_slug, yt_id)
        else:
            logger.warning("[%s] Failed to mark altered content for short %s", channel_slug, yt_id)
    except Exception as e:
        logger.warning("[%s] Auto-mark IA error for short %s: %s", channel_slug, yt_id, e)


# ── Auto-link long-form video to short helper ──────────────────

def _auto_link_longform_for_short(short_yt_id: str, channel_slug: str, account: str,
                                   short_id: int, source_video_id: int):
    """Background thread: link the source long-form video as 'Related video' on a Short.

    YouTube API has no endpoint for this — must use YouTube Studio browser automation.
    Only works for clip-type shorts (source_video_id IS NOT NULL).
    """
    import time as _time
    import sqlite3
    from config.settings import DATABASE_PATH

    try:
        # Wait longer than mark_altered_content (YouTube needs time to process the Short)
        _time.sleep(45)

        # Resolve the long-form YouTube video ID
        conn = sqlite3.connect(str(DATABASE_PATH), timeout=10)
        row = conn.execute(
            "SELECT yt_video_id FROM videos WHERE id = ? AND yt_video_id IS NOT NULL AND yt_video_id != ''",
            (source_video_id,),
        ).fetchone()
        conn.close()

        if not row or not row[0]:
            logger.warning("[%s] No YouTube ID for source video #%d — cannot link to short %s",
                           channel_slug, source_video_id, short_yt_id)
            return

        longform_yt_id = row[0]

        from pipeline.youtube_browser import get_browser
        browser = get_browser(account)
        success = browser.link_longform_video(short_yt_id, longform_yt_id)

        conn2 = sqlite3.connect(str(DATABASE_PATH), timeout=10)
        if success:
            conn2.execute(
                "UPDATE shorts SET longform_linked = 1, longform_linked_at = datetime('now','localtime') WHERE id = ?",
                (short_id,),
            )
            logger.info("[%s] ✅ Long-form video %s linked to short %s",
                        channel_slug, longform_yt_id, short_yt_id)
        else:
            logger.warning("[%s] Failed to link long-form %s to short %s",
                           channel_slug, longform_yt_id, short_yt_id)
        conn2.commit()
        conn2.close()
    except Exception as e:
        logger.warning("[%s] Auto-link longform error for short %s → source #%d: %s",
                       channel_slug, short_yt_id, source_video_id, e)


# ── Timezone defaults ─────────────────────────────────────────
DEFAULT_TIMEZONE = ZoneInfo("Europe/Madrid")
UTC = timezone.utc

# ── Spacing constants ─────────────────────────────────────────
MIN_SHORTS_GAP_MINUTES = 20    # Minimum generation gap between any same-channel shorts (was 35)
SAME_TYPE_SHORTS_PUBLISH_GAP_MINUTES = 45  # native↔native or clip↔clip min publish gap (was 60)
CROSS_TYPE_SHORTS_ALLOW_OVERLAP = True      # native↔clip CAN share same publish time
CLIP_DELAY_AFTER_LONG_MINUTES = 45  # Wait 45 min after long publish before clip
CLIP_END_OF_DAY = 23           # Latest clip target hour (local)
CLIP_END_MIN = 45              # Latest clip target minute
DAY_START_MINUTES = 0           # Allow 24h slots (LATAM overnight slots like 02:37)
DAY_END_MINUTES = 24 * 60       # End of day cap

# ── Jitter: asymmetric — more room before peak, tight after ──
JITTER_BEFORE_MIN = 25         # max minutes before target slot
JITTER_AFTER_MIN = 5           # max minutes after target slot

# ── Generation lead time: shorts take ~5 min to generate ──────
SHORT_GEN_LEAD_MIN = 5

# ── Shorts cooldown: minimum minutes between same-channel shorts ──
SHORTS_COOLDOWN_MINUTES = 20  # was 30 — reduced for 10-15/day density

# ── Native fallback windows (used when no optimal slots available) ──
NATIVE_WINDOWS = [
    (9, 30),     # morning
    (13, 0),     # midday
    (18, 30),    # evening
    (21, 0),     # prime time
]

# ── Filler windows: low-audience hours for extra slots ──────
# Used when (native + clip) < MIN_DAILY_SHORTS to pad the schedule
# without cannibalizing prime-time slots.
FILLER_WINDOWS = [
    (2, 0),     # madrugada
    (4, 0),     # madrugada
    (14, 0),    # mediodía (post-almuerzo)
    (15, 0),    # mediodía
]


def _local_to_utc(date_str: str, hour: int, minute: int, tz: ZoneInfo) -> str:
    """Convert a naive local datetime (YYYY-MM-DD HH:MM:SS) to UTC string."""
    dt_local = datetime.strptime(
        f"{date_str} {hour:02d}:{minute:02d}:00", "%Y-%m-%d %H:%M:%S"
    ).replace(tzinfo=tz)
    dt_utc = dt_local.astimezone(UTC)
    return dt_utc.strftime("%Y-%m-%d %H:%M:%S")


def _day_seed(date_str: str, channel_slug: str, slot_idx: int) -> int:
    """Deterministic seed for a date+channel+slot combination."""
    h = hashlib.md5(f"{date_str}::{channel_slug}::{slot_idx}".encode()).hexdigest()
    return int(h[:8], 16)


def _jitter_minutes(date_str: str, channel_slug: str, slot_idx: int,
                    before_min: int = None, after_min: int = None) -> int:
    """Return deterministic asymmetric jitter (-before_min .. +after_min)."""
    if before_min is None:
        before_min = JITTER_BEFORE_MIN
    if after_min is None:
        after_min = JITTER_AFTER_MIN
    seed = _day_seed(date_str, channel_slug, slot_idx)
    return (seed % (before_min + after_min + 1)) - before_min


def _minutes_to_utc_slot(date_str: str, total_min: int,
                          channel_id: int, channel_slug: str,
                          short_type: str, tz: ZoneInfo,
                          long_slot_position: int = None,
                          slot_position: int = 0) -> dict:
    """Convert a minute-of-day (local tz) to a slot dict with UTC timestamps.

    Handles overflow past midnight: target_upload_at / scheduled_at spill
    into the next day, but date_key stays as the original planning day.
    """
    # Handle overflow past midnight: adjust effective date for datetime
    # conversion, but keep original date_key (slot was planned for this day)
    overflow_days = total_min // (24 * 60)
    if overflow_days > 0:
        dt_date = date.fromisoformat(date_str)
        dt_date = dt_date + timedelta(days=overflow_days)
        effective_date_str = dt_date.isoformat()
        total_min = total_min % (24 * 60)
    else:
        effective_date_str = date_str
        total_min = max(0, total_min)

    h, m = total_min // 60, total_min % 60
    target_utc = _local_to_utc(effective_date_str, h, m, tz)
    sched_total = max(0, total_min - SHORT_GEN_LEAD_MIN)
    sh, sm = sched_total // 60, sched_total % 60
    sched_utc = _local_to_utc(effective_date_str, sh, sm, tz)
    return {
        "channel_id": channel_id,
        "date_key": date_str,  # keep original planning date
        "scheduled_at": sched_utc,
        "target_upload_at": target_utc,
        "short_type": short_type,
        "long_slot_position": long_slot_position,
        "slot_position": slot_position,
        "channel_slug": channel_slug,
    }


def _build_shorts_slots_for_channel(
    ch: dict,
    date_str: str,
    native_count: int,
    clips_per_long: int,
    long_video_count: int,
    long_target_hours: list[int],
    global_start_pos: int,
) -> tuple[list[dict], int]:
    """Generate shorts slots for ONE channel for a given date.

    v14: Uses optimal publish slots (epsilon-greedy) for native shorts,
    per-channel timezone (PUBLISH_TIMEZONE), asymmetric jitter,
    and records slot usage for performance feedback.
    Clips also prefer optimal short slots within their anchor windows.

    Args:
        ch: channel dict with at least id, slug, name
        date_str: YYYY-MM-DD
        native_count: how many native shorts today
        clips_per_long: multiplier (default 3)
        long_video_count: how many long videos published yesterday
        long_target_hours: target publish hours for those long videos (local tz)
        global_start_pos: starting slot_position for this channel

    Returns (slots_list, next_global_pos).
    """
    slug = ch["slug"]
    channel_id = ch["id"]
    all_slots = []
    pos = global_start_pos

    clip_count = clips_per_long * long_video_count

    # ── Load channel timezone from config ──
    tz = DEFAULT_TIMEZONE
    try:
        from config.config_bridge import get_channel_config
        ch_config = get_channel_config(slug)
        tz_str = getattr(ch_config, "PUBLISH_TIMEZONE", None)
        if tz_str:
            tz = ZoneInfo(tz_str)
    except Exception:
        pass

    # ── Load optimal publish slots for shorts from DB ──
    optimal_franjas = []  # list of (hour, minute, slot_rank)
    try:
        from database.db_extended import ExtendedDatabase
        _db = ExtendedDatabase()
        optimal_slots = _db.get_optimal_slots(channel_id, "short")
        if optimal_slots and len(optimal_slots) >= 1:
            for s in optimal_slots:
                optimal_franjas.append((
                    s["target_hour"],
                    s.get("target_minute", 0),
                    s["slot_rank"],
                ))
            optimal_franjas.sort(key=lambda x: (x[0], x[1]))
            logger.debug("Using %d optimal short slots for %s: %s",
                         len(optimal_franjas), slug,
                         [f"{h:02d}:{m:02d}" for h, m, _ in optimal_franjas])
    except Exception as exc:
        logger.debug("Optimal shorts slots lookup skipped for %s: %s", slug, exc)

    # ── Fallback franjas if no optimal slots ──
    if not optimal_franjas:
        optimal_franjas = [(int(w[0]), int(w[1]), 0)
                           for w in NATIVE_WINDOWS[:3]]
        logger.debug("[%s] Using fallback native windows: %s", slug,
                     [f"{h:02d}:{m:02d}" for h, m, _ in optimal_franjas])

    # ── 1. Native slots: one per optimal franja (epsilon-greedy picks the slot) ──
    for i in range(native_count):
        # Pick a franja: round-robin through available ones
        franja_h, franja_m, slot_rank = optimal_franjas[i % len(optimal_franjas)]
        base_min = franja_h * 60 + franja_m
        jitter = _jitter_minutes(date_str, slug, i)
        total_min = base_min + jitter
        total_min = max(DAY_START_MINUTES, min(total_min, 24 * 60 - 1))
        total_min = int(total_min)
        all_slots.append((total_min, "native", None, slot_rank))

        # Record slot usage for epsilon-greedy feedback
        try:
            _db.record_slot_usage(channel_id, "short", slot_rank)
        except Exception:
            pass

    # ── 2. Clip slots: anchored to source long videos, prefer optimal short hours ──
    if clip_count > 0:
        # Default long publish anchors if no real data
        effective_long_hours = long_target_hours if long_target_hours else [16, 19, 22]

        # Build a set of optimal short hours for clip preference
        optimal_short_hours = set(h for h, _, _ in optimal_franjas)

        for long_idx in range(long_video_count):
            # Get the expected publish hour for this long video
            long_h = (
                effective_long_hours[long_idx]
                if long_idx < len(effective_long_hours)
                else effective_long_hours[long_idx % len(effective_long_hours)]
            )

            # Clips start 45 min after long video publishes
            clip_window_start = max(
                (long_h + 1) * 60,
                long_h * 60 + CLIP_DELAY_AFTER_LONG_MINUTES,
            )
            # Latest clip time: 23:45
            clip_window_end = CLIP_END_OF_DAY * 60 + CLIP_END_MIN

            if clip_window_start >= clip_window_end:
                clip_window_start = max(DAY_START_MINUTES, clip_window_end - 90)

            window_minutes = clip_window_end - clip_window_start
            if window_minutes <= 0:
                window_minutes = 60  # safety: at least 1h

            # Spread the clips for this long video evenly
            for c in range(clips_per_long):
                if clips_per_long > 1:
                    offset = c * window_minutes // (clips_per_long - 1)
                else:
                    offset = window_minutes // 2

                total_min_center = clip_window_start + offset

                # Prefer optimal short hours within the clip window
                clip_hour = total_min_center // 60
                if clip_hour in optimal_short_hours:
                    # Snap to the optimal short hour's exact minute
                    matching = [m for h, m, r in optimal_franjas if h == clip_hour]
                    if matching:
                        total_min_center = clip_hour * 60 + matching[0]

                # Jitter: asymmetric around the spread point
                seed = _day_seed(date_str, f"{slug}_clip_{long_idx}_{c}",
                                long_idx * clips_per_long + c)
                jitter = (seed % 31) - 25  # -25..+5 asymmetric
                total_min = total_min_center + jitter
                total_min = max(clip_window_start + 5, min(total_min, clip_window_end - 5))

                # long_slot_position: 1-indexed position of source long video
                long_slot_pos = long_idx + 1

                # Clip's slot_rank = 0 (not tied to an optimal short slot)
                all_slots.append((int(total_min), "clip", long_slot_pos, 0))

    # ── 3. Sort all slots by time and resolve collisions ──
    #    Same-type (native↔native, clip↔clip) → 60min publish gap enforced
    #    Cross-type (native↔clip) → overlap allowed, only 35min gen gap
    #    Iterates forward through resolved slots; skips later slots to avoid
    #    false-positive negative gaps from reversed-order checking.
    all_slots.sort(key=lambda x: x[0])

    resolved = []
    for total_min, slot_type, long_pos, slot_rank in all_slots:
        pushed_min = total_min
        for prev_min, prev_type, _, _ in resolved:
            if prev_min >= pushed_min:
                continue  # This resolved slot is after us — not a collision
            if slot_type == prev_type:
                # Same type: enforce publish-level gap
                if pushed_min - prev_min < SAME_TYPE_SHORTS_PUBLISH_GAP_MINUTES:
                    pushed_min = prev_min + SAME_TYPE_SHORTS_PUBLISH_GAP_MINUTES
            else:
                # Cross-type (native↔clip): only enforce gen-level gap
                if pushed_min - prev_min < MIN_SHORTS_GAP_MINUTES:
                    pushed_min = prev_min + MIN_SHORTS_GAP_MINUTES
        pushed_min = pushed_min  # no end-of-day clamp — _minutes_to_utc_slot handles overflow
        resolved.append((pushed_min, slot_type, long_pos, slot_rank))

    # ── 3b. Dedup same-type slots at the exact same minute ──
    #    When multiple same-type slots collide at the exact same minute
    #    (e.g. end-of-day clamping from earlier collision rounds before
    #    the overflow fix), push later ones forward by the gap.
    #    Overflow past midnight is handled by _minutes_to_utc_slot.
    deduped: list = []
    for minutes_val, stype, long_pos, rank in resolved:
        pushed = minutes_val
        for prev_min, prev_type, _, _ in deduped:
            if prev_type == stype and prev_min == pushed:
                pushed = prev_min + SAME_TYPE_SHORTS_PUBLISH_GAP_MINUTES
        deduped.append((pushed, stype, long_pos, rank))
    resolved = deduped

    # ── 4. Build slot dicts ──
    slots = []
    for total_min, slot_type, long_pos, slot_rank in resolved:
        pos += 1
        slot = _minutes_to_utc_slot(
            date_str, total_min, channel_id, slug,
            short_type=slot_type,
            tz=tz,
            long_slot_position=long_pos,
            slot_position=pos,
        )
        slot["channel_name"] = ch.get("name", slug)
        slot["slot_rank"] = slot_rank  # track which optimal slot was used
        slots.append(slot)

    logger.debug(
        "[%s] Built %d shorts slots: %d native + %d clip "
        "(longs=%d, clips_per_long=%d)",
        slug, len(slots), native_count, clip_count,
        long_video_count, clips_per_long,
    )

    return slots, pos


def _build_filler_slots_for_channel(
    ch: dict,
    date_str: str,
    fillers_needed: int,
    existing_slots: list[dict],
    global_start_pos: int,
) -> tuple[list[dict], int]:
    """Create filler shorts slots to meet MIN_DAILY_SHORTS floor.

    Fillers are scheduled in low-audience windows (FILLER_WINDOWS)
    and do not cannibalize optimal franja slots. They use the same
    collision resolution as regular native slots to avoid overcrowding.

    Args:
        ch: channel dict
        date_str: YYYY-MM-DD
        fillers_needed: how many extra filler slots to create
        existing_slots: already-built slot dicts for this channel (from _build_shorts_slots_for_channel)
        global_start_pos: current slot position counter

    Returns (filler_slots, next_global_pos).
    """
    slug = ch["slug"]
    channel_id = ch["id"]
    pos = global_start_pos

    # Load channel timezone from config
    tz = DEFAULT_TIMEZONE
    try:
        from config.config_bridge import get_channel_config
        ch_config = get_channel_config(slug)
        tz_str = getattr(ch_config, "PUBLISH_TIMEZONE", None)
        if tz_str:
            tz = ZoneInfo(tz_str)
    except Exception:
        pass

    # Extract existing slot minute-of-day values to avoid collisions
    existing_minutes = []
    for s in existing_slots:
        # Parse slot's target_upload_at minute
        tu = s.get("target_upload_at", "")
        try:
            parts = str(tu).replace("T", " ").split(" ")
            time_part = parts[1].split(":")
            h, m = int(time_part[0]), int(time_part[1])
            existing_minutes.append(h * 60 + m)
        except (ValueError, IndexError):
            pass

    filler_slots = []
    for i in range(fillers_needed):
        # Round-robin through filler windows
        franja_h, franja_m = FILLER_WINDOWS[i % len(FILLER_WINDOWS)]
        base_min = franja_h * 60 + franja_m
        jitter = _jitter_minutes(date_str, f"{slug}_filler", i,
                                 before_min=15, after_min=15)
        total_min = base_min + jitter
        total_min = max(0, min(total_min, 24 * 60 - 1))

        # Resolve collisions with existing slots (same type = native)
        pushed_min = total_min
        for prev_min in sorted(existing_minutes):
            if pushed_min - prev_min < SAME_TYPE_SHORTS_PUBLISH_GAP_MINUTES:
                pushed_min = prev_min + SAME_TYPE_SHORTS_PUBLISH_GAP_MINUTES

        existing_minutes.append(pushed_min)
        existing_minutes.sort()

        slot = _minutes_to_utc_slot(
            date_str, pushed_min, channel_id, slug,
            short_type="native",
            tz=tz,
            long_slot_position=None,
            slot_position=pos + 1,
        )
        pos += 1
        slot["channel_name"] = ch.get("name", slug)
        slot["slot_rank"] = -1  # filler — no optimal slot rank
        slot["is_filler"] = True  # mark so UI/analytics can distinguish
        filler_slots.append(slot)

    logger.debug(
        "[%s] Added %d filler slots (total now %d)",
        slug, len(filler_slots), len(existing_slots) + len(filler_slots),
    )

    return filler_slots, pos


def _get_yesterday_published_count(channel_id: int, date_str: str, db=None) -> int:
    """Count long-form videos published/uploaded on the day before date_str.
    
    Clip shorts are now based on yesterday's published videos (not today's planned).
    Returns the count of videos with status IN ('uploaded','published','uploaded_private')
    for the date_key = (date_str - 1 day).
    """
    from datetime import datetime as _dt, timedelta
    yesterday = (_dt.strptime(date_str, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")
    try:
        if db is None:
            from database.db_extended import ExtendedDatabase
            db = ExtendedDatabase()
        count = db.count_completed_videos_for_date(channel_id, yesterday)
        return count
    except Exception as exc:
        logger.debug("Yesterday published count lookup failed for ch%d date=%s: %s",
                     channel_id, yesterday, exc)
        return 0


def _get_planned_long_video_count(channel_id: int, date_str: str) -> tuple[int, list[int]]:
    """Get how many long-form videos are planned today for a channel.

    Returns (count, target_hours_cest_list).
    Falls back to deterministic calculation if no planned_slots exist yet.
    """
    try:
        from database.db_extended import ExtendedDatabase
        db = ExtendedDatabase()
        # Try planned_slots first (most accurate)
        slots = db.get_planned_slots(date_key=date_str, channel_id=channel_id)
        if slots:
            # Count non-cancelled slots
            active = [s for s in slots if s.get("status") != "cancelled"]
            count = len(active)
            # Extract target upload hours (local tz, parse from DB string)
            hours = []
            for s in active:
                tu = s.get("target_upload_at") or s.get("target_public_at") or s.get("scheduled_at") or ""
                try:
                    parts = tu.replace("T", " ").strip().split(" ")[1].split(":")[:2]
                    h = int(parts[0])
                    hours.append(h)
                except (ValueError, IndexError):
                    pass
            if count > 0:
                logger.debug("Channel %d: %d planned longs today → %s", channel_id, count, hours)
                return count, hours

        # Fallback: compute deterministic videos_per_day from channel config
        ch = db.get_channel(channel_id)
        if not ch:
            return 0, []
        # Use planning_service._resolve_videos_per_day for alternate patterns
        try:
            import json as _json
            config = _json.loads(ch.get("config_json", "{}"))
            pattern = config.get("alternate_pattern")
            if pattern and isinstance(pattern, list) and len(pattern) >= 2:
                day_ordinal = datetime.strptime(date_str, "%Y-%m-%d").toordinal()
                offset = config.get("alternate_offset", 0)
                idx = (day_ordinal + offset) % len(pattern)
                count = pattern[idx]
            else:
                count = config.get("videos_per_day", 0)
        except (ValueError, TypeError, KeyError):
            count = 0

        return max(0, count), []
    except Exception as exc:
        logger.debug("Long video count lookup failed for ch%d: %s", channel_id, exc)
        return 0, []


def compute_daily_shorts_slots(date_str: str, db=None) -> list[dict]:
    """Compute shorts slots per active channel for a given date (YYYY-MM-DD).

    v14: clip count is dynamic — clips_per_long × long_videos_published_yesterday.
    Native count is fixed at shorts_native_per_day.
    Filler slots are added when (native + clip) < MIN_DAILY_SHORTS to
    guarantee the daily floor. Fillers go to low-audience windows.
    Timestamps are converted to UTC for storage.

    Returns list of dicts sorted by scheduled_at.
    """
    if db is None:
        from database.db_extended import ExtendedDatabase
        db = ExtendedDatabase()

    from config.settings import MIN_DAILY_SHORTS, MAX_DAILY_SHORTS

    # Get active channels
    channels = db.get_channels(active_only=True)
    channels = [ch for ch in channels if ch["slug"] != "test"]

    if len(channels) < 1:
        logger.warning("No active channels found — cannot compute shorts schedule")
        return []

    # Get per-channel shorts configs
    shorts_configs = db.get_shorts_planning_config()
    config_by_chid = {sc["channel_id"]: sc for sc in shorts_configs}

    all_slots = []
    global_pos = 0

    for ch in channels:
        ch_id = ch["id"]
        slug = ch["slug"]
        sc = config_by_chid.get(ch_id, {})
        if not sc.get("shorts_enabled", True):
            continue

        native_count = sc.get("shorts_native_per_day", 3)
        clips_per_long = sc.get("shorts_clips_per_long", 3)

        if native_count == 0 and clips_per_long == 0:
            continue

        # Dynamic: how many long-form videos were published yesterday?
        # Clip slots are now based on YESTERDAY's published videos, not today's planned.
        yesterday_count = _get_yesterday_published_count(ch_id, date_str, db)
        # No real target hours for yesterday's videos → use empty list (defaults apply)
        long_target_hours: list[int] = []

        channel_slots, global_pos = _build_shorts_slots_for_channel(
            ch, date_str, native_count, clips_per_long,
            yesterday_count, long_target_hours, global_pos,
        )

        # ── Filler: guarantee MIN_DAILY_SHORTS floor ──────────────
        clip_count = clips_per_long * yesterday_count
        total_planned = native_count + clip_count

        if total_planned < MIN_DAILY_SHORTS:
            fillers_needed = min(
                MIN_DAILY_SHORTS - total_planned,
                MAX_DAILY_SHORTS - (total_planned + 0),
            )
            if fillers_needed > 0:
                logger.info(
                    "[%s] Adding %d filler shorts to reach floor (%d < %d): "
                    "planned=%d native + %d clips",
                    slug, fillers_needed, total_planned, MIN_DAILY_SHORTS,
                    native_count, clip_count,
                )
                filler_slots, global_pos = _build_filler_slots_for_channel(
                    ch, date_str, fillers_needed, channel_slots, global_pos,
                )
                channel_slots.extend(filler_slots)

        all_slots.extend(channel_slots)

    # Sort by scheduled_at
    all_slots.sort(key=lambda s: s["scheduled_at"])

    # Re-number slot positions after final sort
    for pos, s in enumerate(all_slots, 1):
        s["slot_position"] = pos

    return all_slots


def persist_daily_shorts_slots(date_str: str, slots: list[dict], db=None) -> int:
    """Store computed shorts slots in shorts_planned_slots table.
    Deletes existing pending slots for this date first.
    Returns count of stored slots.
    """
    if db is None:
        from database.db_extended import ExtendedDatabase
        db = ExtendedDatabase()

    # Delete existing PENDING slots for this date (keep completed/running)
    with db._connect() as conn:
        conn.execute(
            "DELETE FROM shorts_planned_slots WHERE date_key = ? AND status = 'pending'",
            (date_str,),
        )
        conn.commit()

    if not slots:
        return 0

    count = db.create_shorts_planned_slots_batch(slots)
    logger.info("Persisted %d shorts slots for %s", count, date_str)
    return count


def generate_upcoming_shorts(days: int = 7, db=None) -> dict:
    """Generate and persist shorts slots for the next N days (including today).

    Returns summary dict: {date_str: "N slots" | "ERROR: ..."}.
    """
    if db is None:
        from database.db_extended import ExtendedDatabase
        db = ExtendedDatabase()

    today = datetime.now(DEFAULT_TIMEZONE).date()
    results = {}

    for day_offset in range(days):
        day_str = (today + timedelta(days=day_offset)).isoformat()
        try:
            # Skip if date already has any slots (pending, running, completed, etc.)
            # This prevents server restarts from wiping and recreating today's slots,
            # which would cause all past-due slots to fire back-to-back.
            existing = db.get_shorts_planned_slots(date_key=day_str)
            if existing and len(existing) > 0:
                results[day_str] = f"{len(existing)} slots (existing, skipped)"
                logger.debug("Shorts slots for %s: %d existing — skipping regeneration", day_str, len(existing))
                continue

            slots = compute_daily_shorts_slots(day_str, db)
            count = persist_daily_shorts_slots(day_str, slots, db)
            results[day_str] = f"{count} slots"
        except Exception as e:
            logger.error("Failed to generate shorts schedule for %s: %s", day_str, e)
            results[day_str] = f"ERROR: {e}"

    total = sum(
        int(v.split()[0]) for v in results.values() if v and v[0].isdigit()
    )
    logger.info(
        "Generated shorts slots for %d days: %d total",
        days, total,
    )
    return results


def ensure_today_shorts_scheduled(db=None) -> bool:
    """Check if today has pending/running shorts slots. If not, generate them.
    
    Only considers 'pending' and 'running' slots as needing to exist.
    Completed, cancelled, and failed slots are ignored - they don't prevent
    regeneration of new pending slots.
    
    BUT: if there are already completed slots for today, the system has been
    active and the recovery planner is managing deficits. Do NOT blindly
    regenerate — that would create a cancel-regenerate loop with the recovery
    planner. Let recovery handle any gaps.
    
    Returns True if slots exist (existing or newly generated).
    """
    if db is None:
        from database.db_extended import ExtendedDatabase
        db = ExtendedDatabase()

    today = datetime.now(DEFAULT_TIMEZONE).date().isoformat()
    existing = db.get_shorts_planned_slots(date_key=today)

    # Only count pending and running slots as "needs no regeneration"
    active_slots = [s for s in existing if s["status"] in ("pending", "running")]
    if len(active_slots) > 0:
        logger.debug("Today's shorts schedule OK: %d pending/running slots", len(active_slots))
        return True

    # Guard: if there are completed slots for today, the system has been
    # working. Don't regenerate — the recovery planner manages gaps.
    completed_slots = [s for s in existing if s["status"] == "completed"]
    if len(completed_slots) > 0:
        logger.info(
            "Today has %d completed shorts slots — no pending/running. "
            "Deferring to recovery planner (avoids cancel-regenerate loop).",
            len(completed_slots),
        )
        return True

    logger.info("No pending/running shorts slots for today — regenerating schedule")
    slots = compute_daily_shorts_slots(today, db)
    count = persist_daily_shorts_slots(today, slots, db)
    return count > 0


# ── Smart shorts slot dispatcher ───────────────────────────────

def dispatch_next_due_shorts_slot(db=None) -> dict | None:
    """Check for due shorts planned slots and dispatch ONE.

    Called every 5 min by the API checker loop.
    Shorts can coexist with long-form generation (AGENTS.md excludes
    shorts from sequential-only limit). Guarded by: one-short-at-a-time,
    per-channel cooldown, and minimum RAM threshold.

    - For native slots: dispatch immediately
    - For clip slots: check if source long video exists (today, completed)
      If not, skip. If yes, set source_video_id and dispatch.

    Returns:
        dict with dispatched slot info, or None if nothing to do.
    """
    import sqlite3
    from config.settings import DATABASE_PATH

    if db is None:
        from database.db_extended import ExtendedDatabase
        db = ExtendedDatabase()

    # 1. Sync running slots: mark completed/failed based on short status
    _sync_running_shorts_slots(db)

    # 2. Cancel stale pending slots (>4h past scheduled_at)
    _cancel_stale_shorts_slots(db)

    # 3. Allowed concurrency: shorts coexist with long-form generation and uploads.
    #    Per AGENTS.md, shorts are excluded from the sequential-only limit.
    #    Guards #4 (one short at a time), #5 (min 1GB RAM), and #6 (per-channel
    #    cooldown) provide sufficient resource protection for low-footprint shorts.

    # 4. Guard: only one SHORT at a time
    active = db.get_active_shorts_job()
    if active:
        logger.debug("Shorts dispatch skipped: short job #%d is %s", active["id"], active["status"])
        return None

    # 5. Memory gate (shorts use minimal RAM — 1 GB is enough)
    if not _memory_ok(min_free_gb=1.0):
        logger.warning("Low memory — delaying shorts slot dispatch")
        return None

    # 6. Get next pending short slot that is due
    next_slot = db.get_next_pending_shorts_slot()
    if not next_slot:
        logger.debug("No pending shorts slots due")
        return None

    slot_id = next_slot["id"]
    channel_id = next_slot["channel_id"]
    slug = next_slot.get("channel_slug", "")
    short_type = next_slot.get("short_type", "native")
    scheduled = next_slot.get("scheduled_at", "?")
    slot_rank = next_slot.get("slot_rank", 0)

    # 7. Per-channel cooldown guard: enforce minimum spacing between
    # same-channel shorts. Prevents rapid-fire dispatch when many
    # slots are past-due (e.g. after server restart generates catch-up slots).
    if not _channel_shorts_cooldown_ok(channel_id, db):
        logger.debug(
            "Shorts dispatch skipped for %s: cooldown active "
            "(last short < %d min ago)",
            slug, SHORTS_COOLDOWN_MINUTES,
        )
        return None

    # 8. Same-type publish guard: enforces SAME_TYPE_SHORTS_PUBLISH_GAP_MINUTES
    #    between same-type shorts (native↔native, clip↔clip).
    #    Cross-type (native↔clip) overlap is intentionally allowed.
    target_upload = next_slot.get("target_upload_at")
    if _same_type_shorts_slot_conflict(channel_id, short_type, target_upload, db):
        logger.debug(
            "Shorts dispatch skipped for %s: same-type (%s) conflict "
            "(another %s publishing within %d min)",
            slug, short_type, short_type, SAME_TYPE_SHORTS_PUBLISH_GAP_MINUTES,
        )
        return None

    logger.info(
        "Dispatching shorts slot #%d: %s type=%s (scheduled %s)",
        slot_id, slug, short_type, scheduled,
    )

    # 9. For clip slots: check source video dependency
    source_video_id = None
    if short_type == "clip":
        long_pos = next_slot.get("long_slot_position")
        source_video_id = _resolve_clip_source(channel_id, long_pos)
        if source_video_id is None:
            # No source video available — cancel this slot so it doesn't
            # block the queue. Next cycle picks the next due slot.
            db.update_shorts_slot_status(
                slot_id, "cancelled",
                error_message="No completed source long video available",
            )
            logger.info(
                "Shorts slot #%d cancelled: clip type but no completed source "
                "long video (channel=%s, long_slot=%s)",
                slot_id, slug, long_pos,
            )
            return None

    # 10. Mark slot as running with source_video_id
    db.update_shorts_slot_status(slot_id, "running", source_video_id=source_video_id)

    # 11. Create job record for tracking
    conn = sqlite3.connect(str(DATABASE_PATH), timeout=30)
    job_action = "generate_native_short" if short_type == "native" else "generate_clip_short"
    job_id = db.create_job(channel_id, job_action)

    # Mark job as running immediately
    db.update_job(job_id, status="running")

    # Link job to slot
    db.update_shorts_slot_status(slot_id, "running", job_id=job_id,
                                  source_video_id=source_video_id)
    conn.close()

    # 12. Dispatch the actual generation (fire and forget)
    import asyncio
    asyncio.create_task(
        _dispatch_short_async(
            slot_id=slot_id,
            job_id=job_id,
            channel_id=channel_id,
            channel_slug=slug,
            short_type=short_type,
            source_video_id=source_video_id,
            slot_rank=slot_rank,
        )
    )

    return {
        "slot_id": slot_id,
        "job_id": job_id,
        "channel_slug": slug,
        "short_type": short_type,
    }


def _resolve_clip_source(channel_id: int, long_slot_position) -> int | None:
    """Find the completed long video for a clip short.
    
    long_slot_position 1 = first completed video today
    long_slot_position 2 = second completed video today
    
    Returns video_id or None if not available.
    """
    from database.db_extended import ExtendedDatabase
    db = ExtendedDatabase()

    videos = db.get_completed_videos_today(channel_id)
    if not videos:
        return None

    pos = long_slot_position or 1
    # Map to 0-indexed: position 1 -> index 0, position 2 -> index 1
    idx = pos - 1
    if idx < len(videos):
        return videos[idx]["id"]

    return None


async def _dispatch_short_async(slot_id: int, job_id: int, channel_id: int,
                                 channel_slug: str, short_type: str,
                                 source_video_id: int = None,
                                 slot_rank: int = 0):
    """Async wrapper that dispatches the actual short generation and updates DB."""
    import sqlite3
    from config.settings import DATABASE_PATH

    try:
        if short_type == "native":
            short_id = _dispatch_native_short(channel_id, channel_slug, slot_rank=slot_rank)
        else:
            short_id = _dispatch_clip_short(channel_id, channel_slug, source_video_id,
                                            slot_rank=slot_rank)

        if short_id:
            # Mark slot as completed
            conn = sqlite3.connect(str(DATABASE_PATH), timeout=30)
            conn.execute(
                "UPDATE shorts_planned_slots SET status = 'completed', short_id = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (short_id, slot_id),
            )
            conn.execute(
                "UPDATE generation_jobs SET status = 'completed' WHERE id = ?",
                (job_id,),
            )
            conn.commit()
            conn.close()
            logger.info("Shorts slot #%d completed: short_id=%d", slot_id, short_id)
        else:
            # Mark slot as cancelled
            conn = sqlite3.connect(str(DATABASE_PATH), timeout=30)
            conn.execute(
                "UPDATE shorts_planned_slots SET status = 'cancelled', updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (slot_id,),
            )
            conn.execute(
                "UPDATE generation_jobs SET status = 'failed', error_msg = 'No short_id returned' WHERE id = ?",
                (job_id,),
            )
            conn.commit()
            conn.close()
            logger.warning("Shorts slot #%d returned no short_id", slot_id)
    except Exception as e:
        logger.error("Shorts dispatch error for slot #%d: %s", slot_id, e)
        try:
            conn = sqlite3.connect(str(DATABASE_PATH), timeout=30)
            conn.execute(
                "UPDATE shorts_planned_slots SET status = 'cancelled', updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (slot_id,),
            )
            conn.execute(
                "UPDATE generation_jobs SET status = 'failed', error_msg = ? WHERE id = ?",
                (str(e)[:500], job_id),
            )
            conn.commit()
            conn.close()
        except Exception:
            pass
    finally:
        # ── Release memory after EVERY short generation ──────────
        # Without this, Python's heap fragments and the OS never gets
        # pages back. gc.collect() finds unreachable objects;
        # malloc_trim(0) returns freed pages to the OS immediately.
        try:
            import gc
            gc.collect()
            import ctypes
            ctypes.CDLL("libc.so.6").malloc_trim(0)
        except Exception:
            pass


# ── Native short generation ────────────────────────────────────

def _dispatch_native_short(channel_id: int, channel_slug: str,
                           slot_rank: int = 0) -> int | None:
    """Generate and publish a native Short.

    Uses the existing native short generation pipeline (LLM script → TTS → render → upload).

    Returns short_id or None on failure.
    """
    import json
    import random
    import re
    import subprocess
    import time
    import sqlite3
    from pathlib import Path
    from config.settings import DATABASE_PATH, LLM_MODEL, OUTPUT_DIR
    from config.config_bridge import get_channel_config

    ch_config = get_channel_config(channel_slug)
    hashtags = getattr(ch_config, "SHORTS_HASHTAGS", ["#Shorts"])
    niche = getattr(ch_config, "CANAL_NARRATIVE_STYLE", "documental")
    display_name = getattr(ch_config, "CANAL_DISPLAY_NAME", channel_slug)
    tagline = getattr(ch_config, "CANAL_TAGLINE", "")

    # 0. Fetch recent topics to avoid repetition
    from database.db_extended import ExtendedDatabase
    dbx = ExtendedDatabase(str(DATABASE_PATH))
    recent_topics = dbx.get_recent_short_topics(channel_id, limit=15)
    topic_warning = ""
    if recent_topics:
        topic_list = "\n".join(f'  - "{t}"' for t in recent_topics)
        topic_warning = (
            f"\n\n⚠️ IMPORTANTE: NO repitas NINGUNO de estos temas ya publicados recientemente "
            f"en este canal:\n{topic_list}\n\n"
            f"Elige un tema COMPLETAMENTE DIFERENTE y fresco. "
            f"Incluye en el JSON un campo \"tema\" con una frase corta (max 80 chars) que "
            f"identifique claramente de qué trata este Short.\n"
        )
    else:
        topic_warning = (
            f"\n\nIncluye en el JSON un campo \"tema\" con una frase corta (max 80 chars) "
            f"que identifique claramente de qué trata este Short.\n"
        )

    # 1. Script via LLM
    from config.llm_client import create_llm_client
    from config.llm_helpers import llm_json_call
    client = create_llm_client(enable_thinking=False)

    try:
        script = llm_json_call(
            client,
            max_retries=3,
            retry_delay=2.0,
            model=LLM_MODEL,
            messages=[{"role": "user", "content": (
                f"Genera un Short viral en español de ~40-50 segundos (~55-70 palabras totales, minimo 45). "
                f"Canal: {display_name} — {niche}. Tagline: {tagline}."
                f"{topic_warning}"
                f"Usa entre 5 y 7 bloques: hook, [desarrollo1, desarrollo2, (desarrollo3 opcional)], climax, cierre. "
                f"IMPORTANTE: los bloques de desarrollo y climax deben tener 2-3 frases cada uno. "
                f"Hook y cierre: 1-2 frases. Minimo 8 palabras por bloque, maximo 18. "
                f"El total debe superar 45 palabras y no exceder 160. "
                f"Añade desarrollo3 SOLO si el tema lo justifica (mas variedad visual). "
                f"PARA CADA BLOQUE genera 'search_query_en': 5-8 keywords EN INGLÉS para buscar "
                f"imagenes y videos de stock que coincidan EXACTAMENTE con lo narrado en ese momento. "
                f"Incluye tema + detalles visuales (iluminacion, tipo de plano, atmosfera, accion). "
                f"NO uses espanol (las APIs de stock no lo entienden). "
                f"Ademas genera 'theme_keywords_en': 5-8 keywords EN INGLES del tema visual GLOBAL "
                f"del short para mantener coherencia entre escenas. "
                f"Devuelve SOLO JSON: "
                f'{{"tema": "frase corta que identifica el tema (max 80 chars)", '
                f'"titulo": "...", "hook_text": "frase de gancho 8-12 palabras", '
                f'"theme_keywords_en": ["global", "theme", "keywords"], '
                f'"bloques": [{{"tipo": "hook", "texto": "1-2 frases", '
                f'"search_query_en": "english keywords for stock search"}}, '
                f'{{"tipo": "desarrollo1", "texto": "2-3 frases con contexto y detalle", '
                f'"search_query_en": "english keywords"}}, '
                f'{{"tipo": "desarrollo2", "texto": "2-3 frases con dato impactante especifico", '
                f'"search_query_en": "english keywords"}}, '
                f'{{"tipo": "desarrollo3", "texto": "2-3 frases con detalle adicional (opcional)", '
                f'"search_query_en": "english keywords"}}, '
                f'{{"tipo": "climax", "texto": "2-3 frases con la consecuencia o revelacion", '
                f'"search_query_en": "english keywords"}}, '
                f'{{"tipo": "cierre", "texto": "1-2 frases cierre + suscribete", '
                f'"search_query_en": "english keywords"}}]}}. '
                f"NADA MAS fuera del JSON. El array bloques debe tener entre 5 y 7 elementos."
            )}],
            temperature=0.9, max_tokens=1800,
        )
    except Exception as e:
        logger.error("Short script generation failed after retries for %s: %s", channel_slug, e)
        return None

    # 1b. Validate script completeness
    from pipeline.shorts_tts import validate_short_script
    errors = validate_short_script(script)
    if errors:
        logger.error("Short script validation failed for %s: %s", channel_slug, errors)
        return None

    title = (script.get("titulo") or script.get("title") or "Short")[:100]
    hook_text = (script.get("hook_text") or "")[:100]
    bloques = script.get("bloques", [])
    topic = (script.get("tema") or "")[:200]  # store topic for dedup

    # 1c. Subscribe CTA (~40% of native shorts) — programmatic append
    has_subscribe_cta = False
    cta_variants = getattr(ch_config, "SHORTS_SUBSCRIBE_CTA_VARIANTS", [])
    if cta_variants and random.random() < 0.4:
        cta_text = random.choice(cta_variants)
        bloques.append({
            "tipo": "subscribe_cta",
            "texto": cta_text,
            "search_query_en": "subscribe button youtube channel notification bell",
        })
        has_subscribe_cta = True
        logger.info("[%s] Added subscribe CTA to native short: '%s'", channel_slug, cta_text)

    # 2. Segmented TTS
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
        return None

    # 3. Fetch assets exhaustively (v2) — one distinct asset per block,
    #    50-60% video mix, cross-short dedup, query pool with variations
    from pipeline.shorts_media import (
        fetch_short_assets_exhaustive, render_short_hybrid,
        flush_short_asset_history,
    )
    theme_kw = script.get("theme_keywords_en", [])
    asset_items = []
    try:
        asset_items = fetch_short_assets_exhaustive(bloques, ch_config, theme_kw, channel_id, channel_slug=channel_slug)
        logger.info("Fetched %d assets for Short (blocks=%d)", len(asset_items), len(bloques))
    except Exception as e:
        logger.warning("Exhaustive asset fetch failed (will use solid bg): %s", e)

    # 4. Render hybrid (video + Ken Burns images + xfade)
    video_path = output_dir / f"sched_short_{channel_slug}_{ts}.mp4"

    color_palette = getattr(ch_config, "COLOR_PALETTE", {})
    def _to_hex(c):
        if isinstance(c, (tuple, list)) and len(c) == 3:
            return f"{int(c[0]):02x}{int(c[1]):02x}{int(c[2]):02x}"
        return str(c).lstrip("#").replace("#", "")
    bg_color = _to_hex(color_palette.get("text_shadow", (10, 10, 26)))

    try:
        render_short_hybrid(
            asset_items=asset_items,
            audio_path=audio_path,
            output_path=video_path,
            audio_duration=audio_duration,
            bg_color_hex=bg_color,
            srt_path=srt_path if srt_path.exists() else None,
        )
    except Exception as e:
        logger.warning("Hybrid render failed, falling back to solid bg: %s", e)
        # render_short_hybrid internally uses solid-color bg when asset_items is empty,
        # but if it raised an exception (e.g. complex FFmpeg filter error),
        # re-render with empty assets to force the solid-bg path
        try:
            render_short_hybrid(
                asset_items=[],
                audio_path=audio_path,
                output_path=video_path,
                audio_duration=audio_duration,
                bg_color_hex=bg_color,
                srt_path=srt_path if srt_path.exists() else None,
            )
        except Exception as e2:
            logger.error("Solid-bg fallback render also failed for %s: %s", channel_slug, e2)
            return None

    if not video_path.exists():
        logger.error("Render produced no output file for %s", channel_slug)
        return None

    # 5. Upload
    from pipeline.youtube_uploader import YouTubeUploader
    uploader = YouTubeUploader(account_name=channel_slug, channel_slug=channel_slug)
    if not uploader.authenticate():
        logger.error("YouTube auth failed for %s", channel_slug)
        return None

    # Cross-promotion
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
    result = uploader.upload(
        video_path=video_path,
        title=title[:100],
        description=description[:5000],
        tags=hashtags[:60],
        category_id=getattr(ch_config, "YT_CATEGORY_ID", "24"),
        privacy="public",
    )

    yt_id = result.get("video_id")
    if not yt_id:
        logger.error("Upload failed for %s: no video ID", channel_slug)
        return None

    # 6. Register in DB
    conn = sqlite3.connect(str(DATABASE_PATH), timeout=30)
    cursor = conn.execute(
        """INSERT INTO shorts
           (channel_id, type, title, hook_title, hook_text, topic,
            status, file_path, youtube_id, youtube_url, published_at, has_subscribe_cta)
           VALUES (?, 'native', ?, ?, ?, ?, 'published', ?, ?, ?, datetime('now','localtime'), ?)""",
        (channel_id, title, title[:60], hook_text, topic,
         str(video_path), yt_id, result.get("url", ""),
         int(has_subscribe_cta)),
    )
    short_id = cursor.lastrowid
    conn.commit()
    conn.close()

    # 6b. Record assets in short_asset_history for cross-short dedup
    if asset_items:
        try:
            flush_short_asset_history(short_id, channel_id, asset_items)
        except Exception as e:
            logger.warning("[%s] Failed to flush short asset history: %s", channel_slug, e)

    # Auto-mark altered content (IA) via browser
    try:
        if getattr(ch_config, "AUTO_MARK_ALTERED_CONTENT", False):
            from pipeline.youtube_browser import get_account_for_channel
            account = get_account_for_channel(channel_slug)
            if account:
                import threading
                threading.Thread(
                    target=_auto_mark_ia_for_short,
                    args=(yt_id, channel_slug, account, short_id),
                    daemon=True
                ).start()
    except Exception as e:
        logger.warning("[%s] Failed to trigger auto-mark IA for short: %s", channel_slug, e)

    # Post-publish cross-promotion
    run_post_publish_promotion(
        channel_slug=channel_slug,
        short_yt_id=yt_id,
        channel_id=channel_id,
        source_yt_id=longform_url.split("v=")[-1] if longform_url else None,
        channel_config=ch_config,
    )

    logger.info("Scheduled native Short published: %s → %s", title[:40], result.get("url", ""))
    return short_id


# ── Clip short generation ──────────────────────────────────────

def _dispatch_clip_short(channel_id: int, channel_slug: str,
                          source_video_id: int, slot_rank: int = 0) -> int | None:
    """Extract a clip from a long video, render, and publish.

    Uses the ShortsExtractor pipeline pattern from api/routers/shorts.py
    (extract-and-publish endpoint).

    Returns short_id or None on failure.
    """
    import sqlite3
    import json as _json
    import subprocess
    import tempfile
    import time
    from pathlib import Path
    from config.settings import DATABASE_PATH, OUTPUT_DIR
    from config.config_bridge import get_channel_config

    logger.info("Clip extraction: channel=%s source_video=%d", channel_slug, source_video_id)

    conn = sqlite3.connect(str(DATABASE_PATH), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")

    video = conn.execute(
        """SELECT v.*, c.slug as channel_slug
           FROM videos v
           JOIN channels c ON v.channel_id = c.id
           WHERE v.id = ?""",
        (source_video_id,),
    ).fetchone()

    if not video:
        conn.close()
        logger.error("Source video #%d not found for clip short", source_video_id)
        return None

    video = dict(video)

    # ── Phase 1: Script + blocks ──
    script_text = ""
    bloques = []
    if video.get("script_id"):
        script_row = conn.execute(
            "SELECT guion, bloques_json FROM scripts WHERE id = ?",
            (video["script_id"],),
        ).fetchone()
        if script_row:
            script_text = script_row["guion"] or ""
            try:
                bloques = _json.loads(script_row["bloques_json"] or "[]") if script_row["bloques_json"] else []
            except Exception:
                bloques = []

    if not script_text and bloques:
        script_text = " ".join(b.get("texto", "") for b in bloques if b.get("texto"))

    if not script_text:
        bloques_raw = video.get("title_options") or "{}"
        try:
            fallback = _json.loads(bloques_raw) if isinstance(bloques_raw, str) else {}
        except Exception:
            fallback = {}
        script_text = str(fallback.get("script", "")) or script_text

    if not script_text:
        conn.close()
        logger.error("Source video #%d has no script text", source_video_id)
        return None

    # ── Phase 2: LLM extracts best clip timecodes ──
    # Build approximate word-level timestamps from blocks
    from pipeline.shorts_extractor import ShortsExtractor
    total_duration = video.get("duracion_seg") or 0
    n_blocks = len(bloques) if bloques else 1
    timestamps = []
    for idx, block in enumerate(bloques if bloques else [{"texto": script_text}]):
        texto = block.get("texto", "")
        if not texto:
            continue
        words = texto.split()
        block_start = (idx / n_blocks) * total_duration
        block_end = ((idx + 1) / n_blocks) * total_duration
        word_dur = (block_end - block_start) / max(len(words), 1)
        for wi, word in enumerate(words):
            ts_start = round(block_start + wi * word_dur, 1)
            ts_end = round(ts_start + word_dur, 1)
            timestamps.append({"word": word, "start": ts_start, "end": ts_end})

    # Extract word-level TTS timestamps for subtitle rendering
    tts_word_ts = []
    try:
        td_raw = video.get("timing_data") or "{}"
        td = _json.loads(td_raw) if isinstance(td_raw, str) else td_raw
        tts_word_ts = td.get("phases", {}).get("tts_timestamps", [])
        if not isinstance(tts_word_ts, list):
            tts_word_ts = []
    except Exception:
        pass

    extractor = ShortsExtractor()
    clips = extractor.extract(script_text=script_text, timestamps=timestamps,
                              max_clips=3, min_clips=1)

    conn.close()

    if not clips:
        logger.error("No suitable clip found in video #%d", source_video_id)
        return None

    best_clip = clips[0]

    # ── Phase 3: Find or download source video ──
    source_path, clip_offset = _resolve_source_video(video, best_clip["start_time"],
                                                      best_clip["end_time"])
    if source_path is None:
        logger.error("Cannot access source video file for #%d", source_video_id)
        return None

    if clip_offset > 0:
        original_start = best_clip["start_time"]
        original_end = best_clip["end_time"]
        clip_duration = original_end - original_start
        best_clip["start_time"] = clip_offset
        best_clip["end_time"] = clip_offset + clip_duration

    # ── Phase 4: Render → Upload → Promote ──
    _downloaded_temp = source_path
    from pipeline.shorts_renderer import ShortsRenderer
    renderer = ShortsRenderer()
    output_path = None

    try:
        render_word_ts = tts_word_ts if clip_offset == 0 else None
        output_path = renderer.render(
            source_path, best_clip, word_timestamps=render_word_ts,
        )
        if not output_path or not output_path.exists():
            logger.error("Render produced no output for clip from video #%d", source_video_id)
            return None

        ch_config = get_channel_config(channel_slug)
        hashtags = getattr(ch_config, "SHORTS_HASHTAGS", ["#Shorts"])

        from pipeline.youtube_uploader import YouTubeUploader
        uploader = YouTubeUploader(account_name=channel_slug, channel_slug=channel_slug)
        if not uploader.authenticate():
            logger.error("YouTube auth failed for %s", channel_slug)
            return None

        title = best_clip.get("hook_title", "Short")[:100]
        hook_text = best_clip.get("hook_text", "")

        from pipeline.shorts_cross_promote import (
            get_best_longform_link, build_short_description, run_post_publish_promotion,
            should_cross_promote,
        )
        longform_url = None
        if should_cross_promote(ch_config):
            longform_url = get_best_longform_link(channel_id, source_video_id=source_video_id)

        channel_url_value = getattr(ch_config, "YOUTUBE_CHANNEL_URL", "")
        description = build_short_description(
            hook_text=hook_text,
            hashtags=hashtags,
            longform_url=longform_url,
            channel_url=channel_url_value,
        )
        result = uploader.upload(
            video_path=output_path,
            title=title,
            description=description[:5000],
            tags=hashtags[:60],
            category_id=getattr(ch_config, "YT_CATEGORY_ID", "24"),
            privacy="public",
        )

        yt_id = result.get("video_id")
        if not yt_id:
            logger.error("Upload failed for clip: no video ID returned")
            return None

        # Register in DB
        conn2 = sqlite3.connect(str(DATABASE_PATH), timeout=30)
        cursor = conn2.execute(
            """INSERT INTO shorts
               (channel_id, source_video_id, type, title, hook_title, hook_text,
                start_time, end_time, status, file_path, youtube_id, youtube_url, published_at)
               VALUES (?, ?, 'clip', ?, ?, ?, ?, ?, 'published', ?, ?, ?, datetime('now','localtime'))""",
            (channel_id, source_video_id, title, title[:60], hook_text,
             best_clip.get("start_time"), best_clip.get("end_time"),
             str(output_path), yt_id, result.get("url", "")),
        )
        short_id = cursor.lastrowid
        conn2.commit()
        conn2.close()

        # Auto-mark altered content (IA) via browser
        try:
            if getattr(ch_config, "AUTO_MARK_ALTERED_CONTENT", False):
                from pipeline.youtube_browser import get_account_for_channel
                account = get_account_for_channel(channel_slug)
                if account:
                    import threading
                    threading.Thread(
                        target=_auto_mark_ia_for_short,
                        args=(yt_id, channel_slug, account, short_id),
                        daemon=True
                    ).start()
        except Exception as e:
            logger.warning("[%s] Failed to trigger auto-mark IA for short: %s", channel_slug, e)

        # Auto-link long-form video as "Related video" (only for clip shorts)
        try:
            from pipeline.youtube_browser import get_account_for_channel
            account = get_account_for_channel(channel_slug)
            if account and source_video_id:
                import threading
                threading.Thread(
                    target=_auto_link_longform_for_short,
                    args=(yt_id, channel_slug, account, short_id, source_video_id),
                    daemon=True
                ).start()
                logger.info("[%s] Triggered longform link for short %s → source_video #%d",
                            channel_slug, yt_id, source_video_id)
        except Exception as e:
            logger.warning("[%s] Failed to trigger longform link for short: %s", channel_slug, e)

        run_post_publish_promotion(
            channel_slug=channel_slug,
            short_yt_id=yt_id,
            channel_id=channel_id,
            source_yt_id=longform_url.split("v=")[-1] if longform_url else None,
        )

        logger.info("Scheduled clip Short published: %s → %s", title[:40], result.get("url", ""))
        return short_id

    finally:
        if _downloaded_temp and str(_downloaded_temp).startswith("/tmp/"):
            try:
                _downloaded_temp.unlink(missing_ok=True)
            except Exception:
                pass


def _resolve_source_video(video: dict, clip_start: float, clip_end: float):
    """Find or download the source video for clip extraction.
    Returns (Path, offset_seconds) or (None, None)."""
    import subprocess
    import tempfile
    from pathlib import Path

    # 1. Try local file
    if video.get("video_path"):
        for p in [Path(video["video_path"]),
                  Path("/root/autotube") / str(video["video_path"])]:
            if p.exists():
                return p, 0.0

    # 2. Download clip segment from YouTube
    yt_id = video.get("yt_video_id")
    if not yt_id:
        return None, None

    yt_url = video.get("yt_url") or f"https://www.youtube.com/watch?v={yt_id}"
    padding = 3.0
    section_start = max(0, clip_start - padding)
    section_end = clip_end + padding
    section_spec = f"*{section_start:.1f}-{section_end:.1f}"

    with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as tmp:
        tmp_path = tmp.name

    try:
        result = subprocess.run(
            ["yt-dlp",
             "--download-sections", section_spec,
             "-f", "best[height<=720]/best",
             "--no-playlist",
             "--socket-timeout", "30",
             "--force-overwrites",
             "--no-warnings",
             "-o", tmp_path,
             yt_url],
            capture_output=True, text=True, timeout=180,
        )
        if result.returncode != 0 or not Path(tmp_path).exists():
            Path(tmp_path).unlink(missing_ok=True)
            return None, None
        return Path(tmp_path), padding
    except Exception:
        Path(tmp_path).unlink(missing_ok=True)
        return None, None


# ── Internal helpers ───────────────────────────────────────────

def _channel_shorts_cooldown_ok(channel_id: int, db) -> bool:
    """Check if enough time has passed since the channel's last completed short.

    Returns True if the channel is clear to dispatch another short.
    Returns False if the channel's last completed short was less than
    SHORTS_COOLDOWN_MINUTES ago.
    """
    last_completed = db.get_channel_last_short_completed_at(channel_id)
    if last_completed is None:
        return True  # No completed shorts yet — always ok

    try:
        last_time = datetime.strptime(last_completed, "%Y-%m-%d %H:%M:%S")
        elapsed = (datetime.now(UTC) - last_time.replace(tzinfo=UTC)).total_seconds()
        return elapsed >= SHORTS_COOLDOWN_MINUTES * 60
    except (ValueError, TypeError):
        return True  # Can't parse — let it proceed


def _same_type_shorts_slot_conflict(
    channel_id: int, short_type: str,
    target_upload_at: str, db,
) -> bool:
    """Check for same-channel, same-type shorts slot collisions.

    Returns True if another same-type short from the same channel is
    scheduled or recently published within SAME_TYPE_SHORTS_PUBLISH_GAP_MINUTES.
    Cross-type (native↔clip) collisions are intentionally allowed.

    Args:
        channel_id: channel to check
        short_type: 'native' or 'clip'
        target_upload_at: ISO8601 timestamp of the candidate slot
        db: database instance
    """
    if not target_upload_at:
        return False

    try:
        target_dt = datetime.fromisoformat(
            target_upload_at.replace("Z", "+00:00").replace(" ", "T"))
        if target_dt.tzinfo is None:
            target_dt = target_dt.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return False

    min_gap = timedelta(minutes=SAME_TYPE_SHORTS_PUBLISH_GAP_MINUTES)
    window_start = (target_dt - min_gap).strftime("%Y-%m-%d %H:%M:%S")
    window_end = (target_dt + min_gap).strftime("%Y-%m-%d %H:%M:%S")

    try:
        with db._connect() as conn:
            existing = conn.execute(
                """SELECT sps.id, sps.short_type, sps.target_upload_at, sps.status
                   FROM shorts_planned_slots sps
                   WHERE sps.channel_id = ?
                     AND sps.short_type = ?
                     AND sps.status IN ('pending', 'running', 'completed')
                     AND sps.target_upload_at IS NOT NULL
                     AND sps.target_upload_at >= ?
                     AND sps.target_upload_at <= ?
                   ORDER BY sps.target_upload_at
                   LIMIT 3""",
                (channel_id, short_type, window_start, window_end),
            ).fetchall()

        if existing:
            logger.debug(
                "Same-type conflict: %s slot ch=%d has %d nearby same-type "
                "slots in [%s .. %s]",
                short_type, channel_id, len(existing),
                window_start, window_end,
            )
            return True
    except Exception as exc:
        logger.debug("Same-type conflict check failed: %s", exc)

    return False


def _sync_running_shorts_slots(db):
    """Check running shorts slots across ALL recent dates: mark completed if their
    short exists, mark failed if their generation job died (e.g. server restart).

    Previously only scanned today's slots, which left server-restart orphans
    stuck in 'running' state indefinitely for previous days.
    """
    today = datetime.now(DEFAULT_TIMEZONE).date()
    all_running = []
    for offset in range(-7, 1):  # scan last 7 days including today
        date_key = (today + timedelta(days=offset)).isoformat()
        slots = db.get_shorts_planned_slots(date_key=date_key, status="running")
        if slots:
            all_running.extend(slots)

    if not all_running:
        return

    for s in all_running:
        slot_id = s["id"]
        short_id = s.get("short_id")
        job_id = s.get("job_id")

        # Case 1: short exists and is published → mark completed
        if short_id:
            short = db.get_short(short_id)
            if short and short.get("status") == "published":
                db.update_shorts_slot_status(slot_id, "completed")
                logger.info("Shorts slot #%d marked completed", slot_id)
                continue

        # Case 2: linked job failed (server restart, error, etc.) → mark failed
        if job_id:
            job = db.get_job(job_id)
            if job is None:
                # Job record deleted — slot is orphaned
                db.update_shorts_slot_status(
                    slot_id, "failed",
                    error_message="Orphaned: job record missing",
                )
                logger.info("Shorts slot #%d marked failed (job #%d missing)", slot_id, job_id)
            elif job.get("status") == "failed":
                db.update_shorts_slot_status(
                    slot_id, "failed",
                    error_message=f"Job #{job_id} failed: {(job.get('error_msg') or '')[:200]}",
                )
                logger.info("Shorts slot #%d marked failed (job #%d failed)", slot_id, job_id)


def _cancel_stale_shorts_slots(db):
    """Cancel pending shorts slots that are >4h past their scheduled_at (UTC).
    
    Scans ALL dates (not just today) to catch stuck slots from previous days.
    """
    # Fetch pending slots across all recent dates (past 7 days)
    today = datetime.now(DEFAULT_TIMEZONE).date()
    all_pending = []
    for offset in range(-7, 1):  # from 7 days ago to today
        date_key = (today + timedelta(days=offset)).isoformat()
        pending = db.get_shorts_planned_slots(date_key=date_key, status="pending")
        if pending:
            all_pending.extend(pending)

    if not all_pending:
        return

    now_utc = datetime.now(UTC)
    cancelled = 0
    for s in all_pending:
        try:
            sched = datetime.strptime(s["scheduled_at"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
        except (ValueError, TypeError):
            continue
        if (now_utc - sched).total_seconds() > 4 * 3600:
            db.update_shorts_slot_status(s["id"], "cancelled")
            cancelled += 1

    if cancelled:
        logger.info("Cancelled %d stale pending shorts slots (>4h past scheduled)", cancelled)


def _memory_ok(min_free_gb: float = 4.0) -> bool:
    """Check if enough RAM is available for dispatch.
    
    Shorts use minimal RAM (1.0 GB), long-form videos need 4.0 GB.
    """
    try:
        from pipeline.ram_governor import available_mb
        avail_mb = available_mb()
        if avail_mb < 0:
            return True  # Can't determine — let it proceed
        min_free_mb = min_free_gb * 1024
        return avail_mb >= min_free_mb
    except ImportError:
        return True
