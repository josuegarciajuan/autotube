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
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

logger = logging.getLogger("autotube.shorts_scheduler")

# ── Auto-mark altered content helper (shorts) ─────────────────

def _auto_mark_ia_for_short(yt_id: str, channel_slug: str, account: str, short_id: int):
    """Background thread: mark short as AI-generated content.
    
    No end screens — YouTube doesn't support them on Shorts.
    """
    import time as _time
    from pipeline.youtube_browser import cleanup_browser_thread
    
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
    finally:
        cleanup_browser_thread()


# ── Auto-link long-form video to short helper ──────────────────

def _wait_until_processed(channel_slug: str, yt_id: str, timeout: int = 300) -> bool:
    """Poll YouTube Data API until the video finishes processing.

    Checks processingDetails.processingStatus via YouTube Data API v3.
    Waits until status transitions from "processing" to "succeeded" (or fails).

    If the API is unavailable, falls back to exponential backoff sleep
    as a best-effort substitute.

    Args:
        channel_slug: Channel slug for auth.
        yt_id: YouTube video ID to check.
        timeout: Maximum total wait time in seconds (default 5 min).

    Returns:
        True when the video is ready (processingStatus != "processing").
        False if timeout is reached or the API consistently fails.
    """
    import time as _time

    start = _time.monotonic()
    api_attempts = 0
    api_failures = 0

    while True:
        elapsed = _time.monotonic() - start
        if elapsed > timeout:
            logger.warning("[%s] Timeout waiting for video %s to finish processing (%.0fs)",
                           channel_slug, yt_id, elapsed)
            return False

        # ── Try YouTube Data API ────────────────────────────────
        try:
            from pipeline.youtube_uploader import YouTubeUploader
            uploader = YouTubeUploader(account_name=channel_slug, channel_slug=channel_slug)
            if not uploader.authenticate():
                raise RuntimeError("Authentication failed for " + channel_slug)

            service = uploader._get_service()
            resp = service.videos().list(
                part="processingDetails", id=yt_id
            ).execute()

            items = resp.get("items", [])
            if not items:
                # Video not yet visible in API — still processing
                logger.debug("[%s] Video %s not yet visible in API (attempt %d, %.0fs elapsed)",
                             channel_slug, yt_id, api_attempts + 1, elapsed)
                _time.sleep(15)
                api_attempts += 1
                continue

            processing = items[0].get("processingDetails", {})
            status = processing.get("processingStatus", "unknown")
            progress = processing.get("processingProgress", {})

            if status == "succeeded":
                logger.info("[%s] Video %s processing complete (API confirmed in %.0fs, %d attempts)",
                            channel_slug, yt_id, elapsed, api_attempts + 1)
                return True

            if status == "failed":
                reason = processing.get("processingFailureReason", "unknown")
                logger.error("[%s] Video %s processing FAILED: %s", channel_slug, yt_id, reason)
                return False

            if status == "terminated":
                logger.warning("[%s] Video %s processing terminated — may have been taken down",
                               channel_slug, yt_id)
                return False

            # Still "processing"
            parts_processed = progress.get("partsProcessed", "?")
            parts_total = progress.get("partsTotal", "?")
            logger.debug("[%s] Video %s still processing (%s/%s parts, attempt %d, %.0fs elapsed)",
                         channel_slug, yt_id, parts_processed, parts_total,
                         api_attempts + 1, elapsed)
            _time.sleep(15)
            api_attempts += 1

        except Exception as api_err:
            api_failures += 1
            logger.debug("[%s] API polling failed for %s (failure %d): %s",
                         channel_slug, yt_id, api_failures, api_err)

            if api_failures >= 3:
                # Too many API failures — fall back to time-based wait
                logger.warning("[%s] API polling failed %d times for %s — "
                               "falling back to exponential backoff sleep",
                               channel_slug, api_failures, yt_id)
                remaining = timeout - elapsed
                if remaining <= 0:
                    return False
                # Conservative sleep: wait the remaining estimated processing time
                # capped at 120s (typical short processing takes 30-90s)
                wait = min(remaining, 120)
                logger.info("[%s] Waiting %.0fs (backoff fallback) for video %s",
                            channel_slug, wait, yt_id)
                _time.sleep(wait)
                return True  # Assume ready after backoff
            else:
                _time.sleep(15)


def _auto_link_longform_for_short(short_yt_id: str, channel_slug: str, account: str,
                                   short_id: int, source_video_id: int):
    """Background thread: link the source long-form video as 'Related video' on a Short.

    YouTube API has no endpoint for this — must use YouTube Studio browser automation.
    Only works for clip-type shorts (source_video_id IS NOT NULL).
    """
    import sqlite3
    from config.settings import DATABASE_PATH
    from pipeline.youtube_browser import cleanup_browser_thread

    try:
        # ── Poll API until the Short finishes processing ──────
        # YouTube needs time to transcode and process the Short before
        # the edit page becomes available. Instead of a hardcoded sleep,
        # we query processingDetails via YouTube Data API.
        if not _wait_until_processed(channel_slug, short_yt_id, timeout=300):
            logger.warning("[%s] Short %s did not finish processing in time — skipping longform link",
                           channel_slug, short_yt_id)
            return

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
    finally:
        cleanup_browser_thread()


# ── Timezone defaults ─────────────────────────────────────────
DEFAULT_TIMEZONE = ZoneInfo("Europe/Madrid")
UTC = timezone.utc

# ── Spacing constants ─────────────────────────────────────────
MIN_SHORTS_GAP_MINUTES = 20    # Minimum generation gap between any same-channel shorts (was 35)
SAME_TYPE_SHORTS_PUBLISH_GAP_MINUTES = 45  # native↔native or clip↔clip min publish gap (was 60)
CROSS_TYPE_SHORTS_PUBLISH_GAP_MINUTES = 20  # v10.3: native↔clip min publish gap (was 0 — allowed overlap)
CROSS_TYPE_SHORTS_ALLOW_OVERLAP = False     # v10.3: enforce cross-type publish gap
CATCH_UP_BYPASS_HOURS = 6      # v10.3: only skip gap if slot is >6h past-due (was: any past-due)
CLIP_DELAY_AFTER_LONG_MINUTES = 45  # Wait 45 min after long publish before clip
CLIP_END_OF_DAY = 23           # Latest clip target hour (local)
CLIP_END_MIN = 45              # Latest clip target minute
DAY_START_MINUTES = 0           # Allow 24h slots (LATAM overnight slots like 02:37)
DAY_END_MINUTES = 24 * 60       # End of day cap

# ── Jitter: asymmetric — more room before peak, tight after ──
JITTER_BEFORE_MIN = 25         # max minutes before target slot
JITTER_AFTER_MIN = 5           # max minutes after target slot

# ── Generation lead time: shorts need realistic buffer ──────
# v10.3 (Aug 2026): Differentiated by type:
#   - native: full pipeline (LLM + TTS + render + upload) → ~3-7 min real
#   - clip pre-render: upload only (~30s)
#   - clip on-the-fly: extract + render + upload (~3-5 min)
SHORT_GEN_LEAD_MIN = 15           # native shorts (was 5)
SHORT_GEN_LEAD_MIN_CLIP = 5       # pre-rendered clip: just upload
SHORT_GEN_LEAD_MIN_CLIP_ONTHEFLY = 15  # on-the-fly clip: extract + render + upload

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
                          slot_position: int = 0,
                          gen_lead_min: int = None) -> dict:
    """Convert a minute-of-day (local tz) to a slot dict with UTC timestamps.

    Handles overflow past midnight: target_upload_at / scheduled_at spill
    into the next day, but date_key stays as the original planning day.

    Args:
        gen_lead_min: generation lead time in minutes. Defaults to
            SHORT_GEN_LEAD_MIN (15) for native, pass SHORT_GEN_LEAD_MIN_CLIP (5)
            for pre-rendered clips.
    """
    if gen_lead_min is None:
        gen_lead_min = SHORT_GEN_LEAD_MIN
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
    sched_total = max(0, total_min - gen_lead_min)
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


def _load_shorts_optimal_windows(db=None, channel_id=None) -> list[tuple]:
    """Load optimal publish windows for shorts (native or clip).

    Returns list of (hour, minute) tuples, sorted by hour.
    Falls back to NATIVE_WINDOWS if no DB or no optimal slots found.
    """
    windows = list(NATIVE_WINDOWS)  # default fallback
    if db is not None and channel_id is not None:
        try:
            optimal_slots = db.get_optimal_publish_slots(
                channel_id, content_type="short",
            )
            if optimal_slots:
                windows = []
                seen = set()
                for s in optimal_slots:
                    h = s.get("target_hour")
                    m = s.get("target_minute", 0)
                    key = (h, m)
                    if h is not None and key not in seen:
                        windows.append((h, m))
                        seen.add(key)
                windows.sort(key=lambda x: (x[0], x[1]))
                if windows:
                    logger.debug(
                        "_load_shorts_optimal_windows: using %d optimal slots for ch=%d",
                        len(windows), channel_id,
                    )
        except Exception:
            pass
    return windows


def _snap_to_optimal_shorts_window(
    anchor_utc: datetime,
    timezone_str: str = "Europe/Madrid",
    db=None,
    channel_id=None,
    min_delay_min: int = 60,
) -> datetime:
    """Snap a UTC datetime to the nearest upcoming optimal shorts publish window.

    v10.3 (Aug 2026): Used by clip pre-render to ensure clips land in
    optimal publishing windows instead of blind 60-min increments.

    Args:
        anchor_utc: UTC datetime to anchor from (e.g., long video target_public_at).
        timezone_str: Channel timezone for local hour calculations.
        db: Database instance for optimal slots lookup.
        channel_id: Channel ID for optimal slots lookup.
        min_delay_min: Minimum delay from anchor before the first clip can publish.
            Default 60 min (to allow pre-render time).

    Returns:
        UTC datetime snapped to the nearest upcoming optimal window.
        If no window is available within 24h, returns anchor + min_delay_min.
    """
    windows = _load_shorts_optimal_windows(db, channel_id)
    if not windows:
        return anchor_utc + timedelta(minutes=min_delay_min)

    tz = ZoneInfo(timezone_str) if timezone_str else UTC
    anchor_local = anchor_utc.astimezone(tz)
    earliest_local = anchor_local + timedelta(minutes=min_delay_min)

    # Try today's windows first, then tomorrow's
    for day_offset in (0, 1):
        for (h, m) in windows:
            candidate = earliest_local.replace(
                hour=h, minute=m, second=0, microsecond=0,
            )
            if day_offset > 0:
                candidate += timedelta(days=day_offset)
            if candidate > earliest_local:
                result_utc = candidate.astimezone(UTC)
                logger.debug(
                    "_snap_to_optimal_shorts_window: snapped %s → %s "
                    "(window %02d:%02d, delay=%d min)",
                    anchor_utc.isoformat()[:16],
                    result_utc.isoformat()[:16],
                    h, m,
                    int((result_utc - anchor_utc).total_seconds() / 60),
                )
                return result_utc

    # Fallback: no window available (shouldn't happen with 4 windows × 2 days)
    return anchor_utc + timedelta(minutes=min_delay_min)


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

    # ── 2. Clip slots: NO LONGER PRE-PLANNED ─────────────────────────
    # v26: Clip shorts are generated ad-hoc right after long video generation
    # (before cleanup), and go directly to "Pendiente subida" column with
    # scheduled upload times. They do NOT appear in "Planificado".
    # The old pre-planning logic (lines below) is removed.
    # clip_count is kept at 0 — only native slots are pre-planned.
    _clip_count = 0  # explicitly zero

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
                # Cross-type (native↔clip): enforce publish gap (v10.3)
                if pushed_min - prev_min < CROSS_TYPE_SHORTS_PUBLISH_GAP_MINUTES:
                    pushed_min = prev_min + CROSS_TYPE_SHORTS_PUBLISH_GAP_MINUTES
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
        "[%s] Built %d shorts slots: %d native + 0 clip "
        "(clips generated ad-hoc after long video)",
        slug, len(slots), native_count,
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
        # v26: Clip shorts are generated ad-hoc (not pre-planned), so only
        # native shorts count toward the daily floor.
        total_planned = native_count

        if total_planned < MIN_DAILY_SHORTS:
            fillers_needed = min(
                MIN_DAILY_SHORTS - total_planned,
                MAX_DAILY_SHORTS - (total_planned + 0),
            )
            if fillers_needed > 0:
                logger.info(
                    "[%s] Adding %d filler shorts to reach floor (%d < %d): "
                    "planned=%d native",
                    slug, fillers_needed, total_planned, MIN_DAILY_SHORTS,
                    native_count,
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


def _inject_compensation_native_slots(date_str: str, db) -> int:
    """Inject 2 extra native short slots per channel for today only.
    
    Called from generate_upcoming_shorts() when today's slots already exist
    and would normally be skipped by the early-return guard.
    
    v25: One-day compensation for cancelled clip shorts (2026-07-31).
    Distributes slots across remaining future time windows.
    """
    import sqlite3
    from datetime import datetime, timezone
    from config.settings import DATABASE_PATH
    
    if date_str != '2026-07-31':
        return 0
    
    now_utc = datetime.now(timezone.utc)
    # Future windows from now until 04:00 CEST (02:00 UTC next day)
    # Hour:minute tuples in UTC (CEST = UTC+2)
    future_windows = []
    current_hour = now_utc.hour
    for h in range(current_hour + 1, 24):
        future_windows.append((h, 15))  # hh:15
        future_windows.append((h, 45))  # hh:45
    for h in range(0, 3):  # 00:00–02:00 UTC = 02:00–04:00 CEST
        future_windows.append((h, 15))
        future_windows.append((h, 45))
    
    # Get enabled channels (only active)
    channels = db.get_channels(active_only=True)
    
    conn = sqlite3.connect(str(DATABASE_PATH), timeout=30)
    inserted = 0
    slot_idx = 0
    
    for ch in channels:
        ch_id = ch['id']
        slug = ch.get('slug', 'unknown')
        
        # Check if channel already has native slots pending for today
        existing_pending = conn.execute(
            """SELECT COUNT(*) FROM shorts_planned_slots
               WHERE channel_id = ? AND date_key = ? AND short_type = 'native' AND status = 'pending'""",
            (ch_id, date_str),
        ).fetchone()[0]
        
        if existing_pending >= 2:
            continue  # already has enough
        
        needed = 2 - existing_pending
        for n in range(needed):
            if slot_idx >= len(future_windows):
                break
            h, m = future_windows[slot_idx]
            slot_idx += 1
            
            scheduled = f'{date_str} {h:02d}:{m:02d}:00'
            target = f'{date_str} {h:02d}:{m + 5:02d}:00'
            
            # Get max slot_position for this channel+date
            max_pos = conn.execute(
                """SELECT COALESCE(MAX(slot_position), 0) FROM shorts_planned_slots
                   WHERE channel_id = ? AND date_key = ?""",
                (ch_id, date_str),
            ).fetchone()[0]
            
            conn.execute(
                """INSERT INTO shorts_planned_slots
                   (channel_id, date_key, scheduled_at, target_upload_at, short_type,
                    status, slot_position, slot_rank, source_mode)
                   VALUES (?, ?, ?, ?, 'native', 'pending', ?, ?, 'original')""",
                (ch_id, date_str, scheduled, target, max_pos + 1, 9999),
            )
            inserted += 1
    
    conn.commit()
    conn.close()
    logger.info(
        "One-day native compensation: injected %d extra native slots for %s across %d channels",
        inserted, date_str, len(channels),
    )
    return inserted


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
                # ── v25: One-day native compensation (2026-07-31 only) ──
                # All clip shorts for today were cancelled (pre-render switch).
                # Inject 2 extra native short slots per channel for today,
                # distributed across remaining future windows.
                if day_str == '2026-07-31':
                    _inject_compensation_native_slots(day_str, db)
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

def dispatch_next_due_shorts_slot(db=None, loop=None) -> dict | None:
    """Check for due shorts planned slots and dispatch ONE.

    Called every 5 min by the API checker loop.
    Shorts can coexist with long-form generation (AGENTS.md excludes
    shorts from sequential-only limit). Guarded by: one-short-at-a-time,
    per-channel cooldown, and minimum RAM threshold.

    - For native slots: dispatch immediately
    - For clip slots: check if source long video exists (today, completed)
      If not, cancel the slot and retry with the next candidate (up to
      _MAX_CLIP_RETRIES times to avoid wasting scheduler ticks).

    Args:
        db: ExtendedDatabase instance (created if None).
        loop: asyncio event loop for scheduling the async worker. Required
              when called from a thread pool (e.g. via asyncio.to_thread).
              If None, falls back to asyncio.create_task (must be called
              from an active event loop thread).

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

    # 2. Cancel stale pending slots (>24h past scheduled_at)
    _cancel_stale_shorts_slots(db)

    # 3. Allowed concurrency: shorts coexist with long-form generation and uploads.
    #    Per AGENTS.md, shorts are excluded from the sequential-only limit.
    #    Guards #4 (per-channel short job check), #5 (min 1GB RAM), and #6 (per-channel
    #    cooldown) provide sufficient resource protection for low-footprint shorts.
    #
    #    NOTE: Guard #4 moved INSIDE the retry loop (per-channel check) instead of
    #    globally, so different channels can generate shorts in parallel.

    # 4. Memory gate (shorts use minimal RAM — 1 GB is enough)
    if not _memory_ok(min_free_gb=1.0):
        logger.warning("Low memory — delaying shorts slot dispatch")
        return None

    # 5. Global concurrency guard: only ONE short at a time globally.
    #    Shorts rendering runs in thread pool threads (via asyncio.to_thread),
    #    each consuming significant CPU/RAM (ffmpeg, Kokoro TTS, LLM).
    #    Multiple concurrent shorts exhaust the thread pool and cause system
    #    contention. Limit to 1 globally, regardless of channel.
    active_global = db.get_active_shorts_job()
    if active_global:
        logger.debug(
            "Shorts dispatch deferred: short job #%d already running (channel=%s)",
            active_global.get("id"), active_global.get("channel_slug", "?"),
        )
        return None

    # ── Core dispatch loop (retries on clip cancellations + cooldown skips) ──
    # v25: Changed from fixed for-loop (3 retries) to while-loop where predictable
    # skips (no source channel / no resolved source video) do NOT consume retries.
    # This prevents a handful of unwinnable clip slots at the front of the queue
    # from consuming all retry iterations and blocking the entire dispatch pipeline.
    _MAX_CLIP_RETRIES = 3  # max effective retries (only counts hard failures)
    _skipped_slot_ids: set[int] = set()  # slots skipped due to cooldown/conflict
    _failed_force_ids: set[int] = set()  # force-dispatch: clip slots without source
    _retry_count = 0  # effective retry counter (only incremented on actual dispatch)

    # ── Pre-filter: cache which channels have completed long videos today ──
    # Clip shorts need a source long video. Pre-computing this avoids wasting
    # retry iterations on clip slots that will never succeed.
    _channels_with_source: set[int] = set()
    _channels_without_source: set[int] = set()
    try:
        today_long = db.get_completed_videos_today_all_channels()
        if today_long:
            for row in today_long:
                _channels_with_source.add(row.get("channel_id", 0))
        # Get all channel IDs that have ANY pending clip slot today
        # to mark channels that are known to lack a source video.
        pending_clip_channels = db.get_channels_with_pending_clip_slots_today()
        for ch_id in pending_clip_channels or []:
            if ch_id not in _channels_with_source:
                _channels_without_source.add(int(ch_id))
    except Exception:
        pass  # non-critical optimization

    while _retry_count < _MAX_CLIP_RETRIES:
        # 6. Get next pending short slot that is due (skip cooldown-blocked slots)
        exclude_list = list(_skipped_slot_ids) if _skipped_slot_ids else None
        next_slot = db.get_next_pending_shorts_slot(exclude_slot_ids=exclude_list)
        if not next_slot:
            if _skipped_slot_ids:
                logger.warning(
                    "No dispatchable shorts: %d slot(s) skipped (cooldown/conflict) "
                    "— all exhausted. Forcing dispatch of most urgent slot.",
                    len(_skipped_slot_ids),
                )
                # ── Fallback: all eligible slots are blocked by guards. ──────────
                # Force-dispatch the most urgent pending slot regardless of
                # cooldown, same-type conflict, or per-channel job guard.
                # This prevents the queue from stalling when every slot is
                # mutually blocked (common with 24h lookahead where many
                # future slots have nearby target_upload_at).
                #
                # Iterate: skip clip slots without source and retry up to
                # _MAX_CLIP_RETRIES times to find a viable slot.
                # v25: while-loop — predictable source-less skips don't consume retries.
                import time as _time
                _force_retry = 0
                while _force_retry < _MAX_CLIP_RETRIES:
                    if _force_retry > 0:
                        _time.sleep(2)  # back off between retries to reduce log spam
                    force_slot = db.get_next_pending_shorts_slot(
                        exclude_slot_ids=list(_failed_force_ids) if _failed_force_ids else None
                    )
                    if not force_slot:
                        logger.warning("Force dispatch: no viable slots after %d attempts", _force_retry)
                        return None

                    slot_id = force_slot["id"]
                    channel_id = force_slot["channel_id"]
                    slug = force_slot.get("channel_slug", "")
                    short_type_f = force_slot.get("short_type", "native")
                    scheduled_f = force_slot.get("scheduled_at", "?")
                    slot_rank_f = force_slot.get("slot_rank", 0)
                    source_video_id_f = None

                    # Safety guard: skip inactive channels in force dispatch too.
                    if not force_slot.get("channel_active", 1):
                        logger.warning(
                            "Force dispatch: slot #%d (%s) — channel inactive, skipping",
                            slot_id, slug,
                        )
                        _failed_force_ids.add(slot_id)
                        continue

                    if short_type_f == "clip":
                        # Skip clips for channels known to lack completed long videos today.
                        if channel_id in _channels_without_source:
                            logger.info(
                                "Force dispatch: clip slot #%d (%s) — channel has "
                                "no completed long videos today, skipping",
                                slot_id, slug,
                            )
                            _failed_force_ids.add(slot_id)
                            continue  # don't consume retry
                        source_video_id_f = _resolve_clip_source(channel_id,
                            force_slot.get("long_slot_position"))
                        if source_video_id_f is None:
                            logger.info(
                                "Force dispatch: clip slot #%d (%s) has no source — skipping",
                                slot_id, slug,
                            )
                            _failed_force_ids.add(slot_id)
                            continue  # don't consume retry — may resolve later

                    _force_retry += 1  # only consume retry when we actually dispatch

                    logger.warning(
                        "FORCE DISPATCH: slot #%d %s type=%s (scheduled %s) — "
                        "all other slots blocked by guards",
                        slot_id, slug, short_type_f, scheduled_f,
                    )

                    db.update_shorts_slot_status(slot_id, "running",
                                                  source_video_id=source_video_id_f)
                    conn = sqlite3.connect(str(DATABASE_PATH), timeout=30)
                    job_action_f = "generate_native_short" if short_type_f == "native" else "generate_clip_short"
                    job_id_f = db.create_job(channel_id, job_action_f)
                    db.update_job(job_id_f, status="running")
                    db.update_shorts_slot_status(slot_id, "running", job_id=job_id_f,
                                                  source_video_id=source_video_id_f)
                    conn.close()

                    # ── Schedule async worker safely ──
                    # asyncio.create_task fails with RuntimeError when called
                    # from a thread-pool thread (no running event loop). Use
                    # run_coroutine_threadsafe when a loop is provided by the
                    # caller (main.py passes it via asyncio.get_running_loop).
                    _short_async_coro = _dispatch_short_async(
                        slot_id=slot_id, job_id=job_id_f,
                        channel_id=channel_id, channel_slug=slug,
                        short_type=short_type_f,
                        source_video_id=source_video_id_f,
                        slot_rank=slot_rank_f,
                        target_upload_at=force_slot.get("target_upload_at"),
                    )
                    if loop is not None:
                        import asyncio as _asyncio_f
                        _asyncio_f.run_coroutine_threadsafe(_short_async_coro, loop)
                    else:
                        import asyncio as _asyncio_f
                        _asyncio_f.create_task(_short_async_coro)
                    return {
                        "slot_id": slot_id, "job_id": job_id_f,
                        "channel_slug": slug, "short_type": short_type_f,
                    }

                logger.warning("Force dispatch: exhausted %d attempts", _MAX_CLIP_RETRIES)
                return None
            else:
                # Check if there are any pending slots at all (even outside window)
                try:
                    total_pending = db.count_shorts_by_status("pending")
                    if total_pending > 0:
                        next_due = db.get_earliest_pending_short()
                        if next_due:
                            logger.info(
                                "No shorts due yet — next slot: #%d at %s (%d total pending)",
                                next_due["id"], next_due.get("scheduled_at", "?")[:16],
                                total_pending,
                            )
                        else:
                            logger.info("No shorts due — %d pending but none in lookahead", total_pending)
                    else:
                        logger.info("No pending shorts slots at all")
                except Exception:
                    logger.info("No pending shorts slots due")
            return None

        slot_id = next_slot["id"]
        channel_id = next_slot["channel_id"]
        slug = next_slot.get("channel_slug", "")
        short_type = next_slot.get("short_type", "native")
        scheduled = next_slot.get("scheduled_at", "?")
        slot_rank = next_slot.get("slot_rank", 0)
        channel_active = next_slot.get("channel_active", 1)   # channels.active column from JOIN

        # Safety guard: skip inactive channels (should be filtered by
        # get_next_pending_shorts_slot SQL but belt-and-suspenders).
        if not channel_active:
            logger.warning(
                "Shorts slot #%d (%s): channel is inactive — skipping",
                slot_id, slug,
            )
            _skipped_slot_ids.add(slot_id)
            continue

        # 4. Per-channel short job guard — skip to next slot instead of failing.
        #    Different channels can generate shorts in parallel since each has
        #    its own API keys, TTS resources, and render pipeline.
        active_channel_short = db.get_active_shorts_job_for_channel(channel_id)
        if active_channel_short:
            logger.info(
                "Shorts slot #%d (%s) skipped: channel already has active "
                "short job #%d — trying next channel",
                slot_id, slug, active_channel_short["id"],
            )
            _skipped_slot_ids.add(slot_id)
            continue

        # 5. Per-channel cooldown guard — skip to next slot instead of failing
        if not _channel_shorts_cooldown_ok(channel_id, db):
            logger.info(
                "Shorts slot #%d (%s) skipped: cooldown active "
                "(last short < %d min ago) — trying next channel",
                slot_id, slug, SHORTS_COOLDOWN_MINUTES,
            )
            _skipped_slot_ids.add(slot_id)
            continue

        # 8. Publish collision guard — same-type (45 min) + cross-type (20 min)
        target_upload = next_slot.get("target_upload_at")
        if _same_type_shorts_slot_conflict(channel_id, short_type, target_upload, db,
                                            exclude_slot_id=slot_id, cross_type=True):
            logger.info(
                "Shorts slot #%d (%s) skipped: publish conflict "
                "(%s within %d min same-type / %d min cross-type) — trying next channel",
                slot_id, slug, short_type,
                SAME_TYPE_SHORTS_PUBLISH_GAP_MINUTES,
                CROSS_TYPE_SHORTS_PUBLISH_GAP_MINUTES,
            )
            _skipped_slot_ids.add(slot_id)
            continue

        logger.info(
            "Dispatching shorts slot #%d: %s type=%s (scheduled %s)",
            slot_id, slug, short_type, scheduled,
        )

        # 9. For clip slots: check source video dependency (with pre-filter)
        source_video_id = None
        if short_type == "clip":
            # ── v25: Pre-rendered clip check ──
            # If this slot already has a pre-rendered short linked (status='ready',
            # file on disk), skip source resolution entirely. The dispatch path
            # will detect it and upload directly.
            slot_short_id = next_slot.get("short_id")
            if slot_short_id:
                # Verify the short exists and file is on disk
                conn_check = sqlite3.connect(str(DATABASE_PATH), timeout=10)
                conn_check.row_factory = sqlite3.Row
                pre_row = conn_check.execute(
                    "SELECT id, status, file_path FROM shorts WHERE id = ? AND status = 'ready'",
                    (slot_short_id,),
                ).fetchone()
                conn_check.close()
                if pre_row and pre_row["file_path"] and Path(pre_row["file_path"]).exists():
                    source_video_id = next_slot.get("source_video_id")
                    if source_video_id:
                        logger.info(
                            "Shorts slot #%d (%s): pre-rendered clip short #%d ready — "
                            "skipping source resolution, will upload directly",
                            slot_id, slug, slot_short_id,
                        )
                        # Skip the source resolve block below, go directly to dispatch
                    else:
                        logger.warning(
                            "Shorts slot #%d (%s): pre-rendered short #%d but "
                            "no source_video_id on slot — will try normal resolve",
                            slot_id, slug, slot_short_id,
                        )
                        source_video_id = None  # reset, fall through to normal resolve

            # If source_video_id is still None (no pre-render or pre-render check
            # failed), do the normal source resolution.
            if source_video_id is None:
                # Pre-filter: skip clip slots for channels with no completed long videos today.
                if channel_id in _channels_without_source:
                    logger.info(
                        "Shorts slot #%d (%s) skipped: clip type but channel has "
                        "no completed long videos today — trying next channel",
                        slot_id, slug,
                    )
                    _skipped_slot_ids.add(slot_id)
                    continue

                long_pos = next_slot.get("long_slot_position")
                source_video_id = _resolve_clip_source(channel_id, long_pos)
                if source_video_id is None:
                    logger.info(
                        "Shorts slot #%d (%s) skipped: clip type but no completed source "
                        "long video available yet (channel=%s, long_slot=%s) — "
                        "keeping pending, retrying later",
                        slot_id, slug, channel_id, long_pos,
                    )
                    _skipped_slot_ids.add(slot_id)
                    continue  # ← retry with next candidate

        # All guards passed — this is a dispatchable slot. Only NOW do we
        # consume a retry count (predictable skips like no-source don't count).
        _retry_count += 1

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
        # ── Schedule async worker safely ──
        # asyncio.create_task fails with RuntimeError when called
        # from a thread-pool thread (no running event loop). Use
        # run_coroutine_threadsafe when a loop is provided.
        _short_async_coro = _dispatch_short_async(
            slot_id=slot_id,
            job_id=job_id,
            channel_id=channel_id,
            channel_slug=slug,
            short_type=short_type,
            source_video_id=source_video_id,
            slot_rank=slot_rank,
            target_upload_at=next_slot.get("target_upload_at"),
        )
        if loop is not None:
            import asyncio as _asyncio2
            _asyncio2.run_coroutine_threadsafe(_short_async_coro, loop)
        else:
            import asyncio
            asyncio.create_task(_short_async_coro)

        return {
            "slot_id": slot_id,
            "job_id": job_id,
            "channel_slug": slug,
            "short_type": short_type,
        }

    # Exhausted all retries (e.g. all pending clip slots have no source)
    logger.warning("Shorts dispatch: exhausted %d clip retries — no dispatchable slot", _MAX_CLIP_RETRIES)
    return None


def _resolve_clip_source(channel_id: int, long_slot_position) -> int | None:
    """Find the completed long video for a clip short.
    
    long_slot_position 1 = first completed video today
    long_slot_position 2 = second completed video today
    
    Returns video_id or None if not available.
    
    v21: Excludes source videos that already have clip shorts published/pending
    today to prevent duplicate clips from the same long-form video.
    """
    import sqlite3
    from database.db_extended import ExtendedDatabase
    from config.settings import DATABASE_PATH
    db = ExtendedDatabase()

    videos = db.get_completed_videos_today(channel_id)
    if not videos:
        return None

    # ── v21: Exclude source videos that already have clip shorts today ──
    conn = sqlite3.connect(str(DATABASE_PATH), timeout=30)
    already_used_ids = set()
    try:
        used_rows = conn.execute(
            """SELECT DISTINCT source_video_id FROM shorts
               WHERE channel_id = ?
                 AND type = 'clip'
                 AND source_video_id IS NOT NULL
                 AND status IN ('published', 'uploading', 'ready', 'rendering', 'extracted')
                 AND date(created_at) = date('now', 'localtime')""",
            (channel_id,),
        ).fetchall()
        already_used_ids = {row[0] for row in used_rows}
    finally:
        conn.close()

    # Find the first available video that hasn't been used for clips today
    pos = long_slot_position or 1
    found = None
    for video in videos:
        if video["id"] not in already_used_ids:
            found = video
            break

    if found:
        logger.info(
            "_resolve_clip_source: selected video #%d (skipped %d already-used today)",
            found["id"], len(already_used_ids),
        )
        return found["id"]

    return None


async def _dispatch_short_async(slot_id: int, job_id: int, channel_id: int,
                                 channel_slug: str, short_type: str,
                                 source_video_id: int = None,
                                 slot_rank: int = 0,
                                 target_upload_at: str = None):
    """Async wrapper that dispatches the actual short generation and updates DB.

    IMPORTANT: _dispatch_native_short() and _dispatch_clip_short() are
    synchronous functions that block for 5+ minutes (LLM, TTS, ffmpeg).
    They MUST be called via asyncio.to_thread() to avoid blocking the
    uvicorn event loop thread. DO NOT call them synchronously here.

    v10.3: target_upload_at enables scheduled publishing (private + publishAt)
    instead of immediate public.
    """
    import asyncio
    import sqlite3
    from pathlib import Path
    from config.settings import DATABASE_PATH

    try:
        if short_type == "native":
            short_id = await asyncio.to_thread(
                _dispatch_native_short,
                channel_id, channel_slug,
                slot_rank=slot_rank, job_id=job_id,
                target_upload_at=target_upload_at,
            )
        else:
            # ── v25: Check for pre-rendered clip ──
            pre_rendered_short_id = None
            pre_rendered_status = None
            try:
                conn_check = sqlite3.connect(str(DATABASE_PATH), timeout=10)
                conn_check.row_factory = sqlite3.Row
                row = conn_check.execute(
                    """SELECT s.id, s.status, s.file_path
                       FROM shorts s
                       JOIN shorts_planned_slots sps ON sps.short_id = s.id
                       WHERE sps.id = ?
                         AND s.type = 'clip'
                         AND s.status = 'ready'""",
                    (slot_id,),
                ).fetchone()
                conn_check.close()
                if row and row["file_path"] and Path(row["file_path"]).exists():
                    pre_rendered_short_id = row["id"]
                    pre_rendered_status = row["status"]
                    logger.info(
                        "Shorts slot #%d: found pre-rendered clip short #%d (status=%s) — "
                        "skipping render, uploading directly",
                        slot_id, pre_rendered_short_id, pre_rendered_status,
                    )
            except Exception as _pr_check_err:
                logger.debug("Pre-render check for slot #%d failed (non-fatal): %s",
                             slot_id, _pr_check_err)

            short_id = await asyncio.to_thread(
                _dispatch_clip_short,
                channel_id, channel_slug, source_video_id,
                slot_rank=slot_rank, job_id=job_id,
                pre_rendered_short_id=pre_rendered_short_id,
                target_upload_at=target_upload_at,
            )

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
            # ── Retry logic: don't permanently cancel on first failure ──
            # TTS or script failures are often transient (LLM word count,
            # voice speed). Instead of cancelling, set back to 'pending'
            # for up to 2 automatic retries. After 2 failures, cancel permanently.
            conn = sqlite3.connect(str(DATABASE_PATH), timeout=30)
            retries = 0
            try:
                row = conn.execute(
                    "SELECT retry_count FROM shorts_planned_slots WHERE id = ?",
                    (slot_id,),
                ).fetchone()
                retries = (int(row[0]) if row and row[0] else 0)
            except Exception:
                pass

            if retries < 2:
                conn.execute(
                    "UPDATE shorts_planned_slots SET status = 'pending', retry_count = ?, "
                    "error_message = 'Auto-retry after failure (attempt ' || ? || '/2)', "
                    "job_id = NULL, "
                    "scheduled_at = datetime('now', '+10 minutes'), "
                    "updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                    (retries + 1, retries + 1, slot_id),
                )
                conn.execute(
                    "UPDATE generation_jobs SET status = 'failed', "
                    "error_msg = 'No short_id returned (retry ' || ? || '/2)' WHERE id = ?",
                    (retries + 1, job_id),
                )
                conn.commit()
                conn.close()
                logger.warning(
                    "Shorts slot #%d failed (retry %d/2) — rescheduled as pending",
                    slot_id, retries + 1,
                )
            else:
                conn.execute(
                    "UPDATE shorts_planned_slots SET status = 'cancelled', "
                    "error_message = 'Exhausted retries (2/2)', "
                    "updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                    (slot_id,),
                )
                conn.execute(
                    "UPDATE generation_jobs SET status = 'failed', "
                    "error_msg = 'No short_id returned (exhausted retries)' WHERE id = ?",
                    (job_id,),
                )
                conn.commit()
                conn.close()
                logger.warning("Shorts slot #%d exhausted retries — cancelled", slot_id)
    except Exception as e:
        logger.error("Shorts dispatch error for slot #%d: %s", slot_id, e)
        try:
            conn = sqlite3.connect(str(DATABASE_PATH), timeout=30)
            retries = 0
            try:
                row = conn.execute(
                    "SELECT retry_count FROM shorts_planned_slots WHERE id = ?",
                    (slot_id,),
                ).fetchone()
                retries = (int(row[0]) if row and row[0] else 0)
            except Exception:
                pass

            if retries < 2:
                conn.execute(
                    "UPDATE shorts_planned_slots SET status = 'pending', retry_count = ?, "
                    "error_message = ?, job_id = NULL, "
                    "scheduled_at = datetime('now', '+10 minutes'), "
                    "updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                    (retries + 1, f"Auto-retry after error (attempt {retries+1}/2): {str(e)[:200]}", slot_id),
                )
                conn.execute(
                    "UPDATE generation_jobs SET status = 'failed', error_msg = ? WHERE id = ?",
                    (f"Exception (retry {retries+1}/2): {str(e)[:300]}", job_id),
                )
                conn.commit()
                logger.warning(
                    "Shorts slot #%d crashed (retry %d/2) — rescheduled as pending: %s",
                    slot_id, retries + 1, str(e)[:100],
                )
            else:
                conn.execute(
                    "UPDATE shorts_planned_slots SET status = 'cancelled', "
                    "error_message = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                    (f"Exhausted retries after error: {str(e)[:300]}", slot_id),
                )
                conn.execute(
                    "UPDATE generation_jobs SET status = 'failed', error_msg = ? WHERE id = ?",
                    (f"Exception (exhausted retries): {str(e)[:300]}", job_id),
                )
                conn.commit()
                logger.warning("Shorts slot #%d exhausted retries after crash — cancelled: %s", slot_id, str(e)[:100])
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

        # ── Dispatch-on-completion: immediately try next due short ──
        # Without this, the next short waits for the schedule checker
        # tick (1-5 min). By triggering now, we chain shorts back-to-back
        # and clear the backlog faster.
        #
        # ── Cleanup: sync running slots before chaining, to prevent ──
        #     orphaned 'running' slots from blocking the dispatch loop.
        try:
            _sync_running_shorts_slots(None)  # creates its own db connection
        except Exception:
            pass

        try:
            import asyncio as _asyncio
            _chain_loop = _asyncio.get_running_loop()
            async def _chain_next():
                await _asyncio.sleep(2)  # let DB changes settle
                try:
                    from api.services.shorts_scheduler import dispatch_next_due_shorts_slot
                    # Run in thread pool to avoid blocking the event loop.
                    # Pass _chain_loop so internal async scheduling uses
                    # run_coroutine_threadsafe instead of create_task.
                    next_result = await _chain_loop.run_in_executor(
                        None,
                        dispatch_next_due_shorts_slot,
                        None,       # db
                        _chain_loop,  # loop
                    )
                    if next_result:
                        logger.info(
                            "Chained next short: slot=%d channel=%s type=%s",
                            next_result["slot_id"], next_result["channel_slug"],
                            next_result["short_type"],
                        )
                    else:
                        logger.info("Chain dispatch: no due slots within 24h lookahead")
                except Exception as _chain_err:
                    logger.warning("Chain dispatch error: %s", _chain_err)
            _asyncio.create_task(_chain_next())
        except Exception:
            pass  # best-effort, never crash the finally block


# ── Short job progress helper ───────────────────────────────────

def _update_short_job_progress(job_id: int | None, progress: int, phase: str):
    """Update progress and phase for a shorts generation_jobs record.
    
    Called at key milestones during short generation so the pipeline
    view shows real-time progress instead of 0%. No-op when job_id is None
    (manual generation endpoints don't create job records).
    """
    if not job_id:
        return
    try:
        import sqlite3
        from config.settings import DATABASE_PATH
        conn = sqlite3.connect(str(DATABASE_PATH), timeout=15)
        conn.execute(
            "UPDATE generation_jobs SET progress = ?, phase = ? WHERE id = ?",
            (progress, phase, job_id),
        )
        conn.commit()
        conn.close()
        logger.info("Short job #%d progress: %d%% (%s)", job_id, progress, phase)
    except Exception as e:
        logger.warning("Short job progress update failed for #%d: %s", job_id, e)


# ── Native short generation ────────────────────────────────────

def _dispatch_native_short(channel_id: int, channel_slug: str,
                            slot_rank: int = 0, job_id: int = None,
                            target_upload_at: str = None) -> int | None:
    """Generate and publish a native Short.

    Uses the existing native short generation pipeline (LLM script → TTS → render → upload).

    v10.3: If target_upload_at is provided, uploads as private + publishAt
    (scheduled publishing) instead of immediate public.

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

    # 0b. Extract visual theme context (v8) — run BEFORE script generation
    #      so the LLM can generate theme-aware search queries
    theme_context = None
    try:
        from pipeline.theme_extractor import ThemeExtractor
        theme_extractor = ThemeExtractor(config=ch_config)
        # Build a synthetic content text from the channel context and topic
        theme_content = (
            f"Short sobre: {niche}. Tagline: {tagline}."
        )
        theme_context = theme_extractor.extract(
            content_text=theme_content[:2000],
            channel_name=display_name,
            channel_theme=tagline,
            niche_keywords=getattr(ch_config, "NICHE_KEYWORDS_ENG", None),
        )
        if theme_context and theme_context.theme_keywords_en:
            logger.info(
                "Shorts theme extracted for %s: genre=%s keywords=%s",
                channel_slug, theme_context.genre, theme_context.theme_keywords_en,
            )
        else:
            theme_context = None
    except Exception as te:
        logger.debug("Shorts theme extraction skipped (non-fatal): %s", te)
        theme_context = None

    # ── Build theme context block for the LLM prompt (v8) ──────
    theme_block = ""
    if theme_context:
        tl = ["\nCONTEXTO TEMÁTICO DEL SHORT (ancla visual para las escenas):"]
        if theme_context.genre and theme_context.genre != "documental":
            tl.append(f"- Género: {theme_context.genre}")
        if theme_context.era and theme_context.era != "atemporal":
            tl.append(f"- Época: {theme_context.era}")
        if theme_context.primary_subject:
            tl.append(f"- Sujeto visual: {theme_context.primary_subject}")
        if theme_context.key_motifs:
            tl.append(f"- Motivos visuales: {', '.join(theme_context.key_motifs[:4])}")
        if theme_context.mood:
            tl.append(f"- Mood: {theme_context.mood}")
        if theme_context.forbidden_elements:
            tl.append(f"- ⛔ NUNCA incluir en search_query_en: {', '.join(theme_context.forbidden_elements)}")
        tl.append("\nUsa este contexto para generar search_query_en ancladas al MISMO mundo visual.")
        theme_block = "\n".join(tl) + "\n"

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
                f"Genera un Short viral en español de ~50-58 segundos (~70-85 palabras totales, minimo 70). "
                f"Canal: {display_name} — {niche}. Tagline: {tagline}."
                f"{topic_warning}"
                f"{theme_block}"  # v8: visual theme context for query anchoring
                f"Usa entre 6 y 8 bloques: hook, [desarrollo1, desarrollo2, (desarrollo3 opcional)], climax, cierre. "
                f"IMPORTANTE: los bloques de desarrollo y climax deben tener 3-4 frases cada uno. "
                f"Hook y cierre: 2-3 frases. Minimo 12 palabras por bloque, maximo 18. "
                f"El total debe superar 70 palabras y no exceder 90. "
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

    # 1b. Validate script completeness (with smart truncation for over-long scripts)
    from pipeline.shorts_tts import validate_short_script, MAX_WORD_COUNT as _MAX_WORDS, MIN_WORD_COUNT as _MIN_WORDS
    errors = validate_short_script(script)
    if errors:
        # Separate structural errors from word-count issues
        structural_errors = [e for e in errors if "too long" not in e and "too short" not in e
                             and "words" not in e and "Blocks" not in e and "blocks" not in e]
        if structural_errors:
            logger.error("Short script validation failed for %s: %s", channel_slug, structural_errors)
            return None

        bloques_for_trim = script.get("bloques", [])
        total_words = sum(len(b.get("texto", "").split()) for b in bloques_for_trim)

        if total_words > _MAX_WORDS:
            # Trim words from blocks: desarrollo → climax → cierre → hook (last resort)
            trim_order = ["desarrollo3", "desarrollo2", "desarrollo1", "climax", "cierre", "hook"]
            words_to_remove = total_words - _MAX_WORDS

            for block_type in trim_order:
                if words_to_remove <= 0:
                    break
                for b in bloques_for_trim:
                    if b.get("tipo") == block_type:
                        words = b.get("texto", "").split()
                        min_words = 5 if block_type not in ("hook", "cierre") else 7
                        if len(words) > min_words:
                            remove_from_block = min(words_to_remove, len(words) - min_words)
                            b["texto"] = " ".join(words[:len(words) - remove_from_block])
                            words_to_remove -= remove_from_block

            logger.warning(
                "[%s] Script trimmed from %d to ~%d words (LLM exceeded limit of %d)",
                channel_slug, total_words, _MAX_WORDS, _MAX_WORDS,
            )

            # Re-validate after trimming
            errors2 = validate_short_script(script)
            if errors2:
                logger.error(
                    "Short script still invalid after trimming for %s: %s",
                    channel_slug, errors2,
                )
                return None
        elif total_words < _MIN_WORDS:
            logger.warning(
                "[%s] Script has only %d words (< %d min) — proceeding anyway",
                channel_slug, total_words, _MIN_WORDS,
            )

    _update_short_job_progress(job_id, 10, "script")

    title = (script.get("titulo") or script.get("title") or "Short")[:100]
    hook_text = (script.get("hook_text") or "")[:100]
    bloques = script.get("bloques", [])
    topic = (script.get("tema") or "")[:200]  # store topic for dedup

    # 1c. Subscribe CTA (~40% of native shorts) — programmatic append
    has_subscribe_cta = False
    cta_variants = getattr(ch_config, "SHORTS_SUBSCRIBE_CTA_VARIANTS", [])
    if cta_variants and random.random() < 0.4:
        cta_text = random.choice(cta_variants)
        # ── Word budget guard: skip CTA if script is already long ──
        current_words = sum(len(b.get("texto", "").split()) for b in bloques)
        cta_words = len(cta_text.split())
        if current_words + cta_words > 85:  # 85 words ≈ 42s safe under 58s max (aligned with MAX_WORD_COUNT=105)
            logger.info(
                "[%s] Skipping subscribe CTA — script already %d words (+%d CTA would overflow)",
                channel_slug, current_words, cta_words,
            )
        else:
            bloques.append({
                "tipo": "subscribe_cta",
                "texto": cta_text,
                "search_query_en": "subscribe button youtube channel notification bell",
            })
            has_subscribe_cta = True
            logger.info("[%s] Added subscribe CTA to native short: '%s'", channel_slug, cta_text)

    # ── Pre-TTS duration estimation ──────────────────────────
    # Avoid wasting TTS time on scripts that will definitely fail
    # the audio length check. Worst-case voice speed: 0.50 s/word.
    pre_tts_words = sum(len(b.get("texto", "").split()) for b in bloques)
    pre_tts_est = pre_tts_words * 0.50  # worst case for slow voices
    if pre_tts_est > 53.0:  # 5s margin under 58s SHORTS_MAX_DURATION_SEC
        # Re-trim aggressively to ~90 words (safe under 45s)
        target_words = 90
        words_to_remove = pre_tts_words - target_words
        logger.warning(
            "[%s] Pre-TTS: estimated %.1fs from %d words (> 53s) — "
            "re-trimming to ~%d words",
            channel_slug, pre_tts_est, pre_tts_words, target_words,
        )
        trim_order = ["desarrollo3", "desarrollo2", "desarrollo1", "climax", "cierre", "hook"]
        for block_type in trim_order:
            if words_to_remove <= 0:
                break
            for b in bloques:
                if b.get("tipo") == block_type:
                    words = b.get("texto", "").split()
                    if len(words) > 5:
                        remove = min(words_to_remove, len(words) - 5)
                        b["texto"] = " ".join(words[:len(words) - remove])
                        words_to_remove -= remove

        new_total = sum(len(b.get("texto", "").split()) for b in bloques)
        new_est = new_total * 0.50
        logger.info(
            "[%s] Re-trimmed: %d → %d words (est %.1fs → %.1fs)",
            channel_slug, pre_tts_words, new_total, pre_tts_est, new_est,
        )
        if new_est > 55.0:
            logger.error(
                "[%s] Still too long after re-trim (est %.1fs > 55s) — aborting",
                channel_slug, new_est,
            )
            return None  # will be caught by retry mechanism

    # 2. Segmented TTS
    output_dir = OUTPUT_DIR / "videos" / "shorts"
    output_dir.mkdir(parents=True, exist_ok=True)
    ts = int(time.time())
    audio_path = output_dir / f"sched_audio_{channel_slug}_{ts}.mp3"
    srt_path = output_dir / f"sched_audio_{channel_slug}_{ts}.srt"

    from pipeline.shorts_tts import synthesize_shorts_blocks
    try:
        tts_total = len(bloques)
        def _shorts_tts_progress(i_block: int, total: int):
            if total > 0:
                pct = 15 + int((i_block / total) * 8)
                _update_short_job_progress(job_id, pct, "tts")
        tts_result = synthesize_shorts_blocks(
            bloques=bloques,
            ch_config=ch_config,
            output_audio_path=audio_path,
            output_srt_path=srt_path,
            progress_cb=_shorts_tts_progress,
        )
        audio_duration = tts_result["duration_sec"] + 1.5
    except RuntimeError as e:
        logger.error("Short TTS failed for %s: %s", channel_slug, e)
        return None

    _update_short_job_progress(job_id, 25, "tts")

    # 2b. Compute scene_ranges from TTS timestamps (v9 — sub-scene splitting)
    #     Splits blocks longer than SCENE_DURATION_MAX (10s) into sub-scenes
    #     so each sub-scene gets its own distinct visual asset.  Mirrors the
    #     long-form pipeline logic in orchestrator.phase_media().
    from pipeline.video_editor import VideoEditor
    try:
        editor = VideoEditor(ch_config)
        scene_ranges = editor._compute_block_ranges(
            bloques, tts_result.get("timestamps", [])
        )
        logger.info(
            "[%s] Computed %d scene ranges from %d blocks (TTS=%.1fs)",
            channel_slug, len(scene_ranges), len(bloques),
            tts_result.get("duration_sec", 0),
        )
        # Add 'duracion_sec' for fetch_short_assets_exhaustive compatibility
        for sr in scene_ranges:
            sr["duracion_sec"] = sr.get("duration", 5.0)
    except Exception as e:
        logger.warning(
            "[%s] Scene range computation failed — falling back to raw blocks: %s",
            channel_slug, e,
        )
        scene_ranges = None

    # 3. Fetch assets exhaustively (v2) — one distinct asset per scene
    #    (uses scene_ranges when available so sub-scenes each get their own asset).
    #    50-60% video mix, cross-short dedup, query pool with variations
    from pipeline.shorts_media import (
        fetch_short_assets_exhaustive, render_short_hybrid,
        flush_short_asset_history,
    )
    theme_kw = script.get("theme_keywords_en", [])

    # Use scene_ranges as fetch list if available (one asset per sub-scene),
    # otherwise fall back to raw bloques.
    fetch_list = scene_ranges if scene_ranges else bloques

    asset_items = []
    try:
        media_total = len(fetch_list)
        def _shorts_media_progress(i_fetch: int, total: int):
            if total > 0:
                pct = 26 + int((i_fetch / total) * 22)
                _update_short_job_progress(job_id, pct, "media")
        asset_items = fetch_short_assets_exhaustive(
            fetch_list, ch_config, theme_kw,
            theme_ctx=theme_context,  # v8: pass full ThemeContext
            channel_id=channel_id, channel_slug=channel_slug,
            progress_cb=_shorts_media_progress,
        )
        logger.info("Fetched %d assets for Short (fetch_list=%d)",
                    len(asset_items), len(fetch_list))
    except Exception as e:
        logger.warning("Exhaustive asset fetch failed (will use solid bg): %s", e)

    _update_short_job_progress(job_id, 50, "media")

    # 3b. Align assets with scene_ranges for the renderer.
    #     Pass the FULL lists (including None entries) so the renderer can
    #     insert solid-bg filler segments where assets failed — this keeps
    #     the xfade timeline contiguous and offsets cumulative.
    if scene_ranges and len(scene_ranges) == len(asset_items):
        render_assets = asset_items
        render_ranges = scene_ranges
        valid_count = sum(1 for a in asset_items if a is not None)
        logger.info(
            "[%s] %d valid assets + %d filler (from %d scene_ranges)",
            channel_slug, valid_count, len(scene_ranges) - valid_count,
            len(scene_ranges),
        )
    else:
        render_assets = asset_items
        render_ranges = None

    # 4. Render hybrid (video + Ken Burns images + xfade)
    video_path = output_dir / f"sched_short_{channel_slug}_{ts}.mp4"

    color_palette = getattr(ch_config, "COLOR_PALETTE", {})
    def _to_hex(c):
        if isinstance(c, (tuple, list)) and len(c) == 3:
            return f"{int(c[0]):02x}{int(c[1]):02x}{int(c[2]):02x}"
        return str(c).lstrip("#").replace("#", "")
    bg_color = _to_hex(color_palette.get("text_shadow", (10, 10, 26)))

    try:
        def _shorts_render_progress(pct: int, phase: str, msg: str):
            _update_short_job_progress(job_id, pct, phase)
        render_short_hybrid(
            asset_items=render_assets,
            audio_path=audio_path,
            output_path=video_path,
            audio_duration=audio_duration,
            bg_color_hex=bg_color,
            srt_path=srt_path if srt_path.exists() else None,
            scene_ranges=render_ranges,
            progress_cb=_shorts_render_progress,
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

    _update_short_job_progress(job_id, 75, "render")

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
    _update_short_job_progress(job_id, 90, "upload")
    # ── v10.3: Scheduled publishing ──
    if target_upload_at:
        privacy_mode = "private"
        publish_at = target_upload_at
        logger.info("Native short: scheduled publish at %s", publish_at[:19])
    else:
        privacy_mode = "public"
        publish_at = None
    result = uploader.upload(
        video_path=video_path,
        title=title[:100],
        description=description[:5000],
        tags=hashtags[:60],
        category_id=getattr(ch_config, "YT_CATEGORY_ID", "24"),
        privacy=privacy_mode,
        publish_at=publish_at,
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
                          source_video_id: int, slot_rank: int = 0,
                          job_id: int = None,
                          pre_rendered_short_id: int = None,
                          target_upload_at: str = None) -> int | None:
    """Extract a clip from a long video, render, and publish.

    Uses the ShortsExtractor pipeline pattern from api/routers/shorts.py
    (extract-and-publish endpoint).

    v25: If pre_rendered_short_id is provided, skips the LLM extraction and
    rendering phases (the clip was pre-rendered by pre_render_clip_shorts_for_video()
    right after the long-form upload). Only uploads the existing file.

    v10.3: If target_upload_at is provided, uploads as private + publishAt
    (scheduled publishing) instead of immediate public.

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

    # ── v25: Fast path for pre-rendered clips ──
    if pre_rendered_short_id:
        conn_pre = sqlite3.connect(str(DATABASE_PATH), timeout=30)
        conn_pre.row_factory = sqlite3.Row
        short_row = conn_pre.execute(
            "SELECT * FROM shorts WHERE id = ? AND type = 'clip' AND status = 'ready'",
            (pre_rendered_short_id,),
        ).fetchone()
        conn_pre.close()

        if short_row and short_row["file_path"] and Path(short_row["file_path"]).exists():
            short_data = dict(short_row)
            output_path = Path(short_data["file_path"])
            logger.info(
                "Clip short #%d: pre-rendered file ready — uploading directly (%s)",
                pre_rendered_short_id, output_path.name,
            )

            # Upload phase (same as original _dispatch_clip_short upload section)
            _update_short_job_progress(job_id, 75, "upload")
            ch_config = get_channel_config(channel_slug)
            hashtags = getattr(ch_config, "SHORTS_HASHTAGS", ["#Shorts"])

            from pipeline.youtube_uploader import YouTubeUploader
            uploader = YouTubeUploader(account_name=channel_slug, channel_slug=channel_slug)
            if not uploader.authenticate():
                logger.error("YouTube auth failed for %s (pre-rendered clip)", channel_slug)
                return None

            title = (short_data.get("hook_title") or short_data.get("title") or "Short")[:100]
            hook_text = short_data.get("hook_text", "")

            from pipeline.shorts_cross_promote import (
                get_best_longform_link, build_short_description, run_post_publish_promotion,
                should_cross_promote,
            )
            longform_url = None
            if should_cross_promote(ch_config):
                longform_url = get_best_longform_link(channel_id, source_video_id=short_data.get("source_video_id"))

            channel_url_value = getattr(ch_config, "YOUTUBE_CHANNEL_URL", "")
            description = build_short_description(
                hook_text=hook_text, hashtags=hashtags,
                longform_url=longform_url, channel_url=channel_url_value,
            )
            _update_short_job_progress(job_id, 90, "upload")
            # ── v10.3: Scheduled publishing ──
            if target_upload_at:
                clip_privacy = "private"
                clip_publish_at = target_upload_at
            else:
                clip_privacy = "public"
                clip_publish_at = None
            result = uploader.upload(
                video_path=output_path,
                title=title,
                description=description[:5000],
                tags=hashtags[:60],
                category_id=getattr(ch_config, "YT_CATEGORY_ID", "24"),
                privacy=clip_privacy,
                publish_at=clip_publish_at,
            )

            yt_id = result.get("video_id")
            if not yt_id:
                logger.error("Upload failed for pre-rendered clip short #%d", pre_rendered_short_id)
                return None

            # Update shorts table: mark as published
            conn_upd = sqlite3.connect(str(DATABASE_PATH), timeout=30)
            conn_upd.execute(
                """UPDATE shorts SET status = 'published', youtube_id = ?,
                   youtube_url = ?, published_at = datetime('now','localtime'),
                   updated_at = datetime('now','localtime')
                   WHERE id = ?""",
                (yt_id, result.get("url", ""), pre_rendered_short_id),
            )
            conn_upd.commit()
            conn_upd.close()

            # Auto-mark altered content (IA) via browser
            try:
                if getattr(ch_config, "AUTO_MARK_ALTERED_CONTENT", False):
                    from pipeline.youtube_browser import get_account_for_channel
                    account = get_account_for_channel(channel_slug)
                    if account:
                        import threading
                        threading.Thread(
                            target=_auto_mark_ia_for_short,
                            args=(yt_id, channel_slug, account, pre_rendered_short_id),
                            daemon=True
                        ).start()
            except Exception as e_mark:
                logger.warning("[%s] Failed to trigger auto-mark IA: %s", channel_slug, e_mark)

            # Auto-link long-form video
            try:
                from pipeline.youtube_browser import get_account_for_channel
                account = get_account_for_channel(channel_slug)
                if account and short_data.get("source_video_id"):
                    import threading
                    threading.Thread(
                        target=_auto_link_longform_for_short,
                        args=(yt_id, channel_slug, account, pre_rendered_short_id,
                              short_data["source_video_id"]),
                        daemon=True
                    ).start()
            except Exception as e_link:
                logger.warning("[%s] Failed to trigger longform link: %s", channel_slug, e_link)

            run_post_publish_promotion(
                channel_slug=channel_slug, short_yt_id=yt_id,
                channel_id=channel_id,
                source_yt_id=longform_url.split("v=")[-1] if longform_url else None,
            )

            # v25: Delete pre-rendered clip MP4 after upload (no longer needed)
            try:
                output_path.unlink(missing_ok=True)
                logger.info("Deleted pre-rendered clip MP4 after upload: %s", output_path)
            except Exception:
                pass

            logger.info("Pre-rendered clip Short published: %s → %s",
                        title[:40], result.get("url", ""))
            return pre_rendered_short_id

        else:
            logger.warning(
                "Pre-rendered short #%d not found or file missing — "
                "falling back to full clip generation",
                pre_rendered_short_id,
            )
            # Fall through to normal flow

    # ── Normal flow (no pre-render, or pre-render failed) ──

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

    _update_short_job_progress(job_id, 10, "script")

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

    # ── v21: Query existing clip shorts for this source_video to exclude ──
    exclude_ranges = []
    try:
        existing_clips = conn.execute(
            """SELECT start_time, end_time FROM shorts
               WHERE source_video_id = ?
                 AND type = 'clip'
                 AND status IN ('published', 'uploading', 'ready', 'rendering', 'extracted')
               ORDER BY start_time""",
            (source_video_id,),
        ).fetchall()
        exclude_ranges = [(float(r["start_time"]), float(r["end_time"])) for r in existing_clips]
        if exclude_ranges:
            logger.info(
                "Dedup: excluding %d already-published clip ranges for source_video #%d: %s",
                len(exclude_ranges), source_video_id,
                ", ".join(f"{s:.0f}-{e:.0f}s" for s, e in exclude_ranges),
            )
    except Exception as e:
        logger.warning("Dedup query failed (non-fatal): %s", e)

    clips = extractor.extract(script_text=script_text, timestamps=timestamps,
                              max_clips=3, min_clips=1,
                              exclude_ranges=exclude_ranges)

    conn.close()

    if not clips:
        logger.error("No suitable clip found in video #%d", source_video_id)
        return None

    best_clip = clips[0]
    _update_short_job_progress(job_id, 20, "script")

    # ── Phase 3: Find or download source video ──
    source_path, clip_offset = _resolve_source_video(video, best_clip["start_time"],
                                                      best_clip["end_time"])
    if source_path is None:
        logger.error("Cannot access source video file for #%d", source_video_id)
        return None

    # ── v21: Save original clip times BEFORE clip_offset adjustment for DB ──
    # The DB stores the original time range from the long-form video (used for dedup).
    # best_clip may be modified in-place by clip_offset adjustment for rendering.
    db_start_time = best_clip["start_time"]
    db_end_time = best_clip["end_time"]

    if clip_offset > 0:
        clip_duration = best_clip["end_time"] - best_clip["start_time"]
        best_clip["start_time"] = clip_offset
        best_clip["end_time"] = clip_offset + clip_duration

    _update_short_job_progress(job_id, 30, "media")

    # ── Phase 4: Render → Upload → Promote ──
    _downloaded_temp = source_path
    from pipeline.shorts_renderer import ShortsRenderer
    renderer = ShortsRenderer()
    output_path = None

    try:
        # ── v21: Subtitle fallback ──────────────────────────────────────
        # Build proper word-level timestamps for SRT subtitle rendering.
        # Strategy:
        #   1. Use TTS word timestamps from timing_data if available (best quality)
        #   2. When clip_offset > 0 (YouTube download), adjust coordinates
        #   3. Fallback: use block-based approximate timestamps (interpolated)
        render_word_ts = None

        if tts_word_ts:
            # TTS timestamps available — normalize to start_ms/end_ms format
            normalized = []
            for ts in tts_word_ts:
                start_val = float(ts.get("start_ms", ts.get("start", 0)))
                end_val = float(ts.get("end_ms", ts.get("end", 0)))
                normalized.append({
                    "word": ts.get("word", ""),
                    "start_ms": start_val,
                    "end_ms": end_val,
                })

            if clip_offset == 0:
                render_word_ts = normalized
                logger.debug("Subtitle: using TTS timestamps (local file, offset=0)")
            else:
                # Adjust timestamps for downloaded file with padding
                shift_s = clip_offset - db_start_time
                shift_ms = shift_s * 1000
                adjusted = []
                for ts in normalized:
                    new_start = ts["start_ms"] + shift_ms
                    new_end = ts["end_ms"] + shift_ms
                    adjusted.append({
                        "word": ts["word"],
                        "start_ms": new_start,
                        "end_ms": new_end,
                    })
                render_word_ts = adjusted
                logger.info(
                    "Subtitle: adjusted %d TTS timestamps by +%.0fms (offset=%.1f, original=%.1f)",
                    len(adjusted), shift_ms, clip_offset, db_start_time,
                )
        else:
            # ── v21 Fallback: use block-based approximate timestamps ──
            # Convert from {"start": seconds} to {"start_ms": millis} format
            if timestamps:
                block_ts = []
                for ts in timestamps:
                    block_ts.append({
                        "word": ts.get("word", ""),
                        "start_ms": ts["start"] * 1000,
                        "end_ms": ts["end"] * 1000,
                    })
                # If using downloaded file with offset, adjust coordinates
                if clip_offset > 0:
                    shift_s = clip_offset - db_start_time
                    shift_ms = shift_s * 1000
                    adjusted = []
                    for ts in block_ts:
                        new_start = ts["start_ms"] + shift_ms
                        new_end = ts["end_ms"] + shift_ms
                        adjusted.append({
                            "word": ts["word"],
                            "start_ms": new_start,
                            "end_ms": new_end,
                        })
                    block_ts = adjusted
                render_word_ts = block_ts
                logger.info(
                    "Subtitle: using block-based approximate timestamps (%d words, adjusted=%s)",
                    len(block_ts), clip_offset > 0,
                )
            else:
                logger.warning("Subtitle: no timestamps available — clip short will have NO subtitles")

        _update_short_job_progress(job_id, 45, "render")

        output_path = renderer.render(
            source_path, best_clip, word_timestamps=render_word_ts,
        )
        if not output_path or not output_path.exists():
            logger.error("Render produced no output for clip from video #%d", source_video_id)
            return None

        _update_short_job_progress(job_id, 75, "render")

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
        _update_short_job_progress(job_id, 90, "upload")
        # ── v10.3: Scheduled publishing ──
        if target_upload_at:
            clip_privacy2 = "private"
            clip_publish_at2 = target_upload_at
        else:
            clip_privacy2 = "public"
            clip_publish_at2 = None
        result = uploader.upload(
            video_path=output_path,
            title=title,
            description=description[:5000],
            tags=hashtags[:60],
            category_id=getattr(ch_config, "YT_CATEGORY_ID", "24"),
            privacy=clip_privacy2,
            publish_at=clip_publish_at2,
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
             db_start_time, db_end_time,
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


def pre_render_clip_shorts_for_video(
    video_id: int, channel_id: int, channel_slug: str,
    video_path: str, script_id: int = None,
) -> list[int]:
    """Pre-render all clip shorts for a long-form video right after generation.

    v26: No longer depends on pre-existing shorts_planned_slots entries.
    Instead reads shorts_clips_per_long from channel config, extracts clips
    via LLM (1 call for all), renders them, and CREATES new shorts_planned_slots
    entries with calculated target_upload_at times.

    The pre-rendered clips wait in the 'Pendiente subida' pipeline column until
    their scheduled upload time, at which point _dispatch_clip_short() detects
    the ready file and skips directly to uploading.

    Returns list of short_ids created, or empty list on failure.
    """
    import sqlite3
    import json as _json
    import time
    from datetime import datetime, timezone as _timezone, timedelta as _timedelta
    from pathlib import Path
    from config.settings import DATABASE_PATH, OUTPUT_DIR

    conn = sqlite3.connect(str(DATABASE_PATH), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")

    try:
        # 0. Guard: skip if this video already has ready clips on disk
        existing_ready = conn.execute(
            """SELECT COUNT(*) as cnt FROM shorts
               WHERE source_video_id = ?
                 AND type = 'clip'
                 AND status = 'ready'""",
            (video_id,),
        ).fetchone()
        if existing_ready and existing_ready["cnt"] > 0:
            logger.info(
                "pre_render: video #%d already has %d ready clips — skipping (already pre-rendered)",
                video_id, existing_ready["cnt"],
            )
            return []

        # 1. Determine how many clips to generate from channel config
        max_clips = 3  # default
        try:
            sc_row = conn.execute(
                "SELECT shorts_clips_per_long FROM shorts_planning_config WHERE channel_id = ?",
                (channel_id,),
            ).fetchone()
            if sc_row and sc_row["shorts_clips_per_long"]:
                max_clips = int(sc_row["shorts_clips_per_long"])
        except Exception:
            pass

        logger.info(
            "pre_render: video #%d → up to %d clip(s) (channel=%s)",
            video_id, max_clips, channel_slug,
        )

        # Get channel timezone for date calculations
        channel_tz = _timezone.utc
        try:
            from config.config_bridge import get_channel_config
            ch_config = get_channel_config(channel_slug)
            tz_str = getattr(ch_config, "PUBLISH_TIMEZONE", None)
            if tz_str:
                from zoneinfo import ZoneInfo
                channel_tz = ZoneInfo(tz_str)
        except Exception:
            pass

        # 2. Load source video metadata (script + blocks + timing)
        source_video = Path(video_path)
        if not source_video.exists():
            logger.error(
                "pre_render: source video file not found: %s — cannot pre-render",
                video_path,
            )
            return []

        # Fetch video metadata from DB
        video_row = conn.execute(
            "SELECT * FROM videos WHERE id = ?", (video_id,),
        ).fetchone()
        if not video_row:
            logger.error("pre_render: video #%d not found in DB", video_id)
            return []
        video = dict(video_row)

        # ── v26: Calculate clip upload schedule ──
        # If the long video has target_public_at, clips are scheduled after it.
        # Otherwise (immediate mode), schedule from now.
        target_public_at = video.get("target_public_at")
        now_utc = datetime.now(_timezone.utc)
        if target_public_at:
            try:
                if isinstance(target_public_at, str):
                    anchor = datetime.fromisoformat(target_public_at.replace("Z", "+00:00"))
                    if anchor.tzinfo is None:
                        anchor = anchor.replace(tzinfo=_timezone.utc)
                else:
                    anchor = target_public_at
                    if hasattr(anchor, 'tzinfo') and anchor.tzinfo is None:
                        anchor = anchor.replace(tzinfo=_timezone.utc)
                logger.info(
                    "pre_render: anchoring clip uploads to target_public_at=%s",
                    anchor.isoformat(),
                )
            except Exception:
                anchor = now_utc
                logger.debug("pre_render: failed to parse target_public_at, using now")
        else:
            anchor = now_utc
            logger.info("pre_render: no target_public_at — using now as anchor")

        # ── v10.3: Calculate clip upload schedule using optimal shorts windows ──
        # Instead of blind 60-min increments, snap each clip to the nearest
        # upcoming optimal shorts publish window (≥60 min after anchor).
        # windows: (9,30), (13,0), (18,30), (21,0) or DB optimal slots.
        # Subsequent clips spread across consecutive windows.
        upload_times = []
        channel_timezone = "Europe/Madrid"
        try:
            from config.config_bridge import get_channel_config
            ch_cfg = get_channel_config(channel_slug)
            channel_timezone = getattr(ch_cfg, "PUBLISH_TIMEZONE", "Europe/Madrid")
        except Exception:
            pass

        # Load optimal windows (or fallback to NATIVE_WINDOWS)
        windows = _load_shorts_optimal_windows(db=None, channel_id=channel_id)
        windows_used = []  # track which windows we've assigned this batch

        for i in range(max_clips):
            if i == 0:
                # First clip: snap to the nearest upcoming optimal window ≥60 min
                target_upload = _snap_to_optimal_shorts_window(
                    anchor, timezone_str=channel_timezone,
                    db=None, channel_id=channel_id,
                    min_delay_min=60,
                )
                # Find which window was used
                tz_obj = ZoneInfo(channel_timezone)
                target_local = target_upload.astimezone(tz_obj)
                used_hour = target_local.hour
                windows_used.append(used_hour)
            else:
                # Subsequent clips: use the next consecutive optimal window
                # Find the window after the previous clip's window
                prev_local = upload_times[-1].astimezone(tz_obj)
                prev_hour = prev_local.hour
                
                # Find the next window after prev_hour
                found = False
                for (wh, wm) in windows:
                    if wh > prev_hour:
                        candidate = prev_local.replace(
                            hour=wh, minute=wm, second=0, microsecond=0,
                        )
                        # Ensure at least some spacing (avoid same-hour issues)
                        if (candidate - prev_local).total_seconds() > 30 * 60:
                            target_upload = candidate.astimezone(UTC)
                            windows_used.append(wh)
                            found = True
                            break
                
                if not found:
                    # Fallback: anchor + 60*(i+1) with a small offset
                    target_upload = anchor + _timedelta(minutes=75 + 75 * (i - 1))

            # Ensure target_upload is not in the past (for immediate mode)
            if target_upload < now_utc:
                target_upload = now_utc + _timedelta(minutes=30 + 60 * i)
            upload_times.append(target_upload)

        if upload_times:
            logger.info(
                "pre_render: clip upload windows: %s → %s",
                anchor.isoformat()[:16],
                ", ".join(t.isoformat()[:16] for t in upload_times),
            )

        # Script + blocks
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
            logger.error("pre_render: video #%d has no script text", video_id)
            return []

        # 3. Build approximate word-level timestamps from blocks
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

        # TTS word timestamps for subtitle rendering
        tts_word_ts = []
        try:
            td_raw = video.get("timing_data") or "{}"
            td = _json.loads(td_raw) if isinstance(td_raw, str) else td_raw
            tts_word_ts = td.get("phases", {}).get("tts_timestamps", [])
            if not isinstance(tts_word_ts, list):
                tts_word_ts = []
        except Exception:
            pass

        # 4. Run LLM extraction ONCE — get up to max_clips clips
        extractor = ShortsExtractor()

        # Query existing clips for this video (in case of retry)
        exclude_ranges = []
        existing_clips = conn.execute(
            """SELECT start_time, end_time FROM shorts
               WHERE source_video_id = ?
                 AND type = 'clip'
                 AND status IN ('published', 'uploading', 'ready', 'rendering', 'extracted')
               ORDER BY start_time""",
            (video_id,),
        ).fetchall()
        exclude_ranges = [(float(r["start_time"]), float(r["end_time"])) for r in existing_clips]

        clips = extractor.extract(
            script_text=script_text, timestamps=timestamps,
            max_clips=max_clips, min_clips=1,
            exclude_ranges=exclude_ranges,
        )

        if not clips:
            logger.error("pre_render: LLM returned no clips for video #%d", video_id)
            return []

        logger.info(
            "pre_render: LLM returned %d clip(s) for video #%d — rendering now",
            len(clips), video_id,
        )

        # 5. Render each clip and save as 'ready'
        from pipeline.shorts_renderer import ShortsRenderer
        renderer = ShortsRenderer()
        short_ids = []

        for clip_idx, clip in enumerate(clips):
            if clip_idx >= max_clips:
                break

            # Build subtitle timestamps for this clip
            render_word_ts = None
            if tts_word_ts:
                normalized = []
                for ts in tts_word_ts:
                    start_val = float(ts.get("start_ms", ts.get("start", 0)))
                    end_val = float(ts.get("end_ms", ts.get("end", 0)))
                    normalized.append({
                        "word": ts.get("word", ""),
                        "start_ms": start_val,
                        "end_ms": end_val,
                    })
                render_word_ts = normalized
            elif timestamps:
                block_ts = []
                for ts in timestamps:
                    block_ts.append({
                        "word": ts.get("word", ""),
                        "start_ms": ts["start"] * 1000,
                        "end_ms": ts["end"] * 1000,
                    })
                render_word_ts = block_ts

            try:
                output_path = renderer.render(
                    source_video, clip, word_timestamps=render_word_ts,
                )
            except Exception as render_err:
                logger.error(
                    "pre_render: render failed for clip %d of video #%d: %s",
                    clip_idx + 1, video_id, render_err,
                )
                continue

            if not output_path or not output_path.exists():
                logger.error(
                    "pre_render: no output for clip %d of video #%d",
                    clip_idx + 1, video_id,
                )
                continue

            # Calculate schedule for this clip
            target_upload = upload_times[clip_idx]
            scheduled_at = target_upload - _timedelta(minutes=SHORT_GEN_LEAD_MIN_CLIP)  # upload lead time
            date_key = scheduled_at.astimezone(channel_tz).strftime("%Y-%m-%d")

            # Save to shorts table as 'ready'
            cursor = conn.execute(
                """INSERT INTO shorts
                   (channel_id, source_video_id, type, title, hook_title, hook_text,
                    start_time, end_time, status, file_path, scheduled_date)
                   VALUES (?, ?, 'clip', ?, ?, ?, ?, ?, 'ready', ?, ?)""",
                (
                    channel_id, video_id,
                    clip.get("hook_title", "Short")[:100],
                    clip.get("hook_title", "Short")[:60],
                    clip.get("hook_text", ""),
                    clip.get("start_time", 0),
                    clip.get("end_time", 0),
                    str(output_path),
                    date_key,
                ),
            )
            short_id = cursor.lastrowid

            # ── v26: Create new shorts_planned_slots entry for scheduling ──
            # This slot acts as the upload trigger — the dispatcher picks it up
            # when target_upload_at is due and uploads the pre-rendered clip.
            conn.execute(
                """INSERT INTO shorts_planned_slots
                   (channel_id, date_key, scheduled_at, target_upload_at,
                    short_type, status, slot_position, long_slot_position,
                    source_video_id, short_id)
                   VALUES (?, ?, ?, ?, 'clip', 'pending', ?, ?, ?, ?)""",
                (
                    channel_id, date_key,
                    scheduled_at.strftime("%Y-%m-%d %H:%M:%S"),
                    target_upload.strftime("%Y-%m-%d %H:%M:%S"),
                    clip_idx + 1,    # slot_position
                    clip_idx + 1,    # long_slot_position
                    video_id,        # source_video_id
                    short_id,        # short_id (links ready short for dispatch)
                ),
            )
            slot_id = cursor.lastrowid

            short_ids.append(short_id)
            logger.info(
                "pre_render: clip %d/%d rendered → short #%d (slot #%d) for %s "
                "| upload at %s",
                clip_idx + 1, len(clips), short_id, slot_id, channel_slug,
                target_upload.strftime("%Y-%m-%d %H:%M:%S"),
            )

        conn.commit()
        logger.info(
            "pre_render: video #%d → %d clip shorts pre-rendered (status=ready)",
            video_id, len(short_ids),
        )
        return short_ids

    except Exception as e:
        logger.error("pre_render: unexpected error for video #%d: %s", video_id, e,
                     exc_info=True)
        return []
    finally:
        conn.close()


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
             "--retries", "5",
             "--fragment-retries", "5",
             "--extractor-retries", "3",
             "--file-access-retries", "3",
             "--force-overwrites",
             "--no-warnings",
             "-o", tmp_path,
             yt_url],
            capture_output=True, text=True, timeout=300,
        )
        if result.returncode != 0 or not Path(tmp_path).exists():
            logger.error("yt-dlp failed (code %d): %s",
                         result.returncode,
                         result.stderr[-500:] if result.stderr else "(no output)")
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
        last_utc = last_time.replace(tzinfo=DEFAULT_TIMEZONE).astimezone(UTC)
        elapsed = (datetime.now(UTC) - last_utc).total_seconds()
        return elapsed >= SHORTS_COOLDOWN_MINUTES * 60
    except (ValueError, TypeError):
        return True  # Can't parse — let it proceed


def _same_type_shorts_slot_conflict(
    channel_id: int, short_type: str,
    target_upload_at: str, db,
    exclude_slot_id: int | None = None,
    cross_type: bool = False,
) -> bool:
    """Check for same-channel shorts publish collisions.

    v10.3 (Aug 2026): Extended to check:
      - shorts_planned_slots (pending/running/completed same-type, ±45 min)
      - shorts_planned_slots (pending/running/completed cross-type, ±20 min)
      - shorts table (already published, ±gap)
      - Catch-up bypass limited to >6h past-due (was: any past-due)

    Returns True if a collision is detected, False if the slot is clear
    or exempted via catch-up bypass.

    Args:
        channel_id: channel to check
        short_type: 'native' or 'clip'
        target_upload_at: ISO8601 timestamp of the candidate slot
        db: database instance
        exclude_slot_id: optional slot ID to exclude from conflict check
        cross_type: if True, also check cross-type collisions (native↔clip)
    """
    if not target_upload_at:
        return False

    try:
        target_dt = datetime.fromisoformat(
            target_upload_at.replace("Z", "+00:00").replace(" ", "T"))
        if target_dt.tzinfo is None:
            target_dt = target_dt.replace(tzinfo=DEFAULT_TIMEZONE).astimezone(UTC)
    except (ValueError, TypeError):
        return False

    # ── v10.3: Limited catch-up bypass ──
    # Only skip gap enforcement if the slot is >6h past-due.
    # Previously any past-due slot was dispatched immediately, causing
    # avalanche clustering. Now we require a meaningful delay.
    now_utc = datetime.now(UTC)
    if target_dt < now_utc:
        hours_late = (now_utc - target_dt).total_seconds() / 3600.0
        if hours_late >= CATCH_UP_BYPASS_HOURS:
            logger.info(
                "Bypassing conflict for severely past-due shorts slot "
                "(target=%s, now=%s, %.1fh late — catch-up mode)",
                target_dt.isoformat()[:16], now_utc.isoformat()[:16], hours_late,
            )
            return False
        else:
            logger.debug(
                "Past-due shorts slot (%sh late) — NOT bypassing, "
                "will enforce spacing (threshold: %sh)",
                f"{hours_late:.1f}", CATCH_UP_BYPASS_HOURS,
            )

    # ── Same-type gap (45 min) ──
    same_gap = timedelta(minutes=SAME_TYPE_SHORTS_PUBLISH_GAP_MINUTES)
    # ── Cross-type gap (20 min) ──
    cross_gap = timedelta(minutes=CROSS_TYPE_SHORTS_PUBLISH_GAP_MINUTES)

    # ── Check 1: shorts_planned_slots — same-type ──
    window_same_start = (target_dt - same_gap).strftime("%Y-%m-%d %H:%M:%S")
    window_same_end = (target_dt + same_gap).strftime("%Y-%m-%d %H:%M:%S")

    try:
        with db._connect() as conn:
            if exclude_slot_id is not None:
                existing = conn.execute(
                    """SELECT sps.id, sps.short_type, sps.target_upload_at, sps.status
                       FROM shorts_planned_slots sps
                       WHERE sps.channel_id = ?
                         AND sps.short_type = ?
                         AND sps.status IN ('pending', 'running', 'completed')
                         AND sps.target_upload_at IS NOT NULL
                         AND sps.target_upload_at >= ?
                         AND sps.target_upload_at <= ?
                         AND sps.id != ?
                       ORDER BY sps.target_upload_at
                       LIMIT 3""",
                    (channel_id, short_type, window_same_start, window_same_end,
                     exclude_slot_id),
                ).fetchall()
            else:
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
                    (channel_id, short_type, window_same_start, window_same_end),
                ).fetchall()

            if existing:
                logger.debug(
                    "Same-type conflict: %s slot ch=%d has %d nearby same-type "
                    "slots in [%s .. %s]",
                    short_type, channel_id, len(existing),
                    window_same_start, window_same_end,
                )
                return True

            # ── Check 2: shorts_planned_slots — cross-type (if requested) ──
            if cross_type:
                other_type = "clip" if short_type == "native" else "native"
                window_cross_start = (target_dt - cross_gap).strftime("%Y-%m-%d %H:%M:%S")
                window_cross_end = (target_dt + cross_gap).strftime("%Y-%m-%d %H:%M:%S")

                if exclude_slot_id is not None:
                    cross_existing = conn.execute(
                        """SELECT sps.id, sps.short_type, sps.target_upload_at, sps.status
                           FROM shorts_planned_slots sps
                           WHERE sps.channel_id = ?
                             AND sps.short_type = ?
                             AND sps.status IN ('pending', 'running', 'completed')
                             AND sps.target_upload_at IS NOT NULL
                             AND sps.target_upload_at >= ?
                             AND sps.target_upload_at <= ?
                             AND sps.id != ?
                           LIMIT 3""",
                        (channel_id, other_type, window_cross_start, window_cross_end,
                         exclude_slot_id),
                    ).fetchall()
                else:
                    cross_existing = conn.execute(
                        """SELECT sps.id, sps.short_type, sps.target_upload_at, sps.status
                           FROM shorts_planned_slots sps
                           WHERE sps.channel_id = ?
                             AND sps.short_type = ?
                             AND sps.status IN ('pending', 'running', 'completed')
                             AND sps.target_upload_at IS NOT NULL
                             AND sps.target_upload_at >= ?
                             AND sps.target_upload_at <= ?
                           LIMIT 3""",
                        (channel_id, other_type, window_cross_start, window_cross_end),
                    ).fetchall()

                if cross_existing:
                    logger.debug(
                        "Cross-type conflict: %s slot ch=%d has %d nearby %s "
                        "slots in [%s .. %s]",
                        short_type, channel_id, len(cross_existing), other_type,
                        window_cross_start, window_cross_end,
                    )
                    return True

            # ── Check 3: shorts table — already published shorts ──
            # v10.3: Also check recently published shorts. A short that was
            # published 10 min ago with no planned_slot counterpart still
            # occupies the time window and should prevent clustering.
            window_pub_start = (target_dt - same_gap).strftime("%Y-%m-%d %H:%M:%S")
            window_pub_end = (target_dt + same_gap).strftime("%Y-%m-%d %H:%M:%S")

            published = conn.execute(
                """SELECT s.id, s.type, s.published_at
                   FROM shorts s
                   WHERE s.channel_id = ?
                     AND s.status = 'published'
                     AND s.published_at IS NOT NULL
                     AND s.published_at >= ?
                     AND s.published_at <= ?
                   ORDER BY s.published_at
                   LIMIT 5""",
                (channel_id, window_pub_start, window_pub_end),
            ).fetchall()

            for row in published:
                pub_type = row["type"]
                # Same-type check: 45 min gap
                if pub_type == short_type:
                    logger.debug(
                        "Published short conflict: %s slot ch=%d conflicts with "
                        "published %s short #%d at %s",
                        short_type, channel_id, pub_type, row["id"], row["published_at"],
                    )
                    return True
                # Cross-type check: 20 min gap (if cross_type requested)
                elif cross_type:
                    # Parse published_at to compute exact gap
                    try:
                        pub_dt = datetime.fromisoformat(
                            str(row["published_at"]).replace("Z", "+00:00").replace(" ", "T"))
                        if pub_dt.tzinfo is None:
                            pub_dt = pub_dt.replace(tzinfo=DEFAULT_TIMEZONE).astimezone(UTC)
                        gap_min = abs((target_dt - pub_dt).total_seconds()) / 60.0
                        if gap_min < CROSS_TYPE_SHORTS_PUBLISH_GAP_MINUTES:
                            logger.debug(
                                "Published cross-type conflict: %s slot ch=%d too close "
                                "to published %s short #%d (%.0f min gap < %d min)",
                                short_type, channel_id, pub_type, row["id"],
                                gap_min, CROSS_TYPE_SHORTS_PUBLISH_GAP_MINUTES,
                            )
                            return True
                    except (ValueError, TypeError):
                        pass

    except Exception as exc:
        logger.debug("Shorts collision check failed: %s", exc)

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

        # Case 2: linked job failed (server restart, error, etc.) → reset to pending
        # Previously these were marked as 'failed' and lost until the recovery
        # planner ran (up to 60 min later, only during active hours). Now they
        # reset to 'pending' with job_id=NULL so the dispatcher picks them up
        # automatically in the next tick.
        if job_id:
            job = db.get_job(job_id)
            if job is None or job.get("status") == "failed":
                # Reset to pending: clear job link + error so the dispatcher
                # sees it as a fresh pending slot. Uses direct SQL because
                # update_shorts_slot_status() skips fields when None is passed.
                import sqlite3 as _sql_sync
                from config.settings import DATABASE_PATH as _DBP
                with _sql_sync.connect(str(_DBP), timeout=30) as _conn:
                    _conn.execute(
                        "UPDATE shorts_planned_slots SET status='pending', "
                        "job_id=NULL, error_message=NULL, updated_at=CURRENT_TIMESTAMP "
                        "WHERE id=?",
                        (slot_id,),
                    )
                    _conn.commit()
                reason = "orphaned after restart" if job is None else "job failed"
                logger.info("Shorts slot #%d reset to pending (%s)", slot_id, reason)


def _cancel_stale_shorts_slots(db):
    """Cancel pending shorts slots that are >24h past their scheduled_at (UTC).
    
    Scans ALL dates (not just today) to catch stuck slots from previous days.
    Threshold is 24h to match the 24h visibility window in
    get_next_pending_shorts_slot(), ensuring slots aren't cancelled before
    they get a fair chance to be dispatched (e.g. after a long-form video
    that took several hours to generate).
    """
    STALE_HOURS = 24
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
            sched = datetime.strptime(s["scheduled_at"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=DEFAULT_TIMEZONE).astimezone(UTC)
        except (ValueError, TypeError):
            continue
        if (now_utc - sched).total_seconds() > STALE_HOURS * 3600:
            db.update_shorts_slot_status(s["id"], "cancelled")
            cancelled += 1

    if cancelled:
        logger.info("Cancelled %d stale pending shorts slots (>%dh past scheduled)", cancelled, STALE_HOURS)


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
