"""Auto-recovery planner for missing daily publications.

Detects channels that are behind their daily video target, and creates
recovery slots in low-audience hours (with anti-collision against
existing pending/running slots).

Algorithm:
  1. For each active channel, read videos_per_day (target).
  2. Count videos successfully published today (yt_video_id + uploaded_at = today).
  3. Count planned slots still pending/running for today.
  4. If published + pending < target, compute missing = target - (published + pending).
  5. For each missing slot, pick a time window:
     a. Priority 1: low-audience hour (NOT peak, NOT secondary peaks).
     b. Priority 2: secondary peak hour (if all low-audience collide).
     c. Fallback: any available hour with 90-min gap from now.
  6. Create the slot in planned_slots.

Called every 60 minutes by the background checker loop in api/main.py,
but only between 10:00-23:00 CEST (local time for Spain/Europe).
"""

import logging
from datetime import date, datetime, timedelta
from typing import Optional

import pytz

logger = logging.getLogger("autotube.recovery_planner")

# ── Constants ────────────────────────────────────────────────────
MIN_GAP_MINUTES = 90               # Minimum gap between same-channel slot starts
ESTIMATED_PIPELINE_MINUTES = 75    # Typical generation duration
MIN_HOUR_AHEAD = 2                 # Minimum hours from now to schedule a recovery slot
# Window during which recovery is active (local hours in CEST)
RECOVERY_START_HOUR = 10           # 10:00 AM
RECOVERY_END_HOUR = 23             # 11:00 PM
RECOVERY_INTERVAL_MINUTES = 60     # How often to run (used for logging only)


def _now_madrid() -> datetime:
    """Return current datetime in Europe/Madrid timezone."""
    return datetime.now(pytz.timezone("Europe/Madrid"))


def _parse_hour_from_slot(slot: dict, field: str = "scheduled_at") -> Optional[int]:
    """Extract the hour (0-23, local time) from a slot's timestamp field."""
    val = slot.get(field)
    if not val:
        return None
    try:
        # Handles formats: "2026-07-12 15:30:00" or ISO with T
        s = str(val).replace("T", " ")
        h, m = map(int, s[11:16].split(":"))
        return h * 60 + m  # minute of day
    except (ValueError, IndexError):
        return None


def _collides(minute_of_day: int, existing: list[int],
              gap_min: int = MIN_GAP_MINUTES) -> bool:
    """Check if minute_of_day collides with any existing slot time."""
    for e in existing:
        if abs(minute_of_day - e) < gap_min:
            return True
    return False


def _low_audience_hours(peak_hour: int, secondary_peaks: list[int],
                         now_minute_of_day: int,
                         min_ahead_minutes: int = 0) -> list[int]:
    """Return list of hour:00 times (as minute-of-day) that are:
    - NOT peak
    - NOT in secondary_peaks
    - >= now_minute_of_day + min_ahead_minutes
    - within today (0-23h)

    Returns minute-of-day values (e.g., 900 = 15:00).
    """
    peak_set = {peak_hour % 24} | {h % 24 for h in secondary_peaks}
    result = []
    for h in range(24):
        if h in peak_set:
            continue
        minute = h * 60
        if minute >= now_minute_of_day + min_ahead_minutes:
            result.append(minute)
    return result


def _secondary_peak_hours(secondary_peaks: list[int],
                           now_minute_of_day: int,
                           min_ahead_minutes: int = 0) -> list[int]:
    """Return secondary peak hours (as minute-of-day), filtered to future."""
    result = []
    for h in sorted(secondary_peaks):
        minute = (h % 24) * 60
        if minute >= now_minute_of_day + min_ahead_minutes:
            result.append(minute)
    return result


def _find_first_available(now_minute_of_day: int,
                           existing: list[int],
                           gap_min: int = MIN_GAP_MINUTES) -> int:
    """Brute-force find first available minute-of-day that doesn't collide."""
    # Scan in 30-min increments for efficiency
    for m in range(now_minute_of_day + gap_min, 24 * 60, 30):
        if not _collides(m, existing, gap_min):
            return m
    # Absolute fallback: clamp to 23:29
    fallback = 23 * 60 + 29
    for m in range(fallback, now_minute_of_day + gap_min, -30):
        if not _collides(m, existing, 0):
            return m
    return fallback


def auto_recover_missing_publications(db=None) -> dict:
    """Main recovery function. Checks all channels and replans missing slots.

    Called periodically from the background scheduler loop.

    Returns:
        dict with {recovered_count, channels_affected, details}.
    """
    if db is None:
        from database.db_extended import ExtendedDatabase
        db = ExtendedDatabase()

    now_local = _now_madrid()
    now_hour = now_local.hour
    now_minute_of_day = now_local.hour * 60 + now_local.minute

    # ── Window guard: only run during active hours ──
    if now_hour < RECOVERY_START_HOUR or now_hour >= RECOVERY_END_HOUR:
        return {
            "recovered_count": 0,
            "skipped": True,
            "reason": f"Outside recovery window ({RECOVERY_START_HOUR:02d}-{RECOVERY_END_HOUR:02d}h)",
            "now_hour": now_hour,
        }

    today = date.today().isoformat()
    min_ahead = MIN_HOUR_AHEAD * 60  # 120 minutes

    result = {
        "date": today,
        "recovered_count": 0,
        "channels_affected": [],
        "details": [],
    }

    # ── Get all active channels ──
    channels = db.get_channels(active_only=True)
    if not channels:
        return {**result, "reason": "no_active_channels"}

    for ch in channels:
        channel_id = ch["id"]
        slug = ch.get("slug", f"channel_{channel_id}")

        # ── Read planning config ──
        cfg = db.get_channel_planning_config(channel_id)
        if not cfg.get("planning_enabled", True):
            continue
        target = cfg.get("videos_per_day", 0)
        if target <= 0:
            continue

        # ── 1. Count published today ──
        published_today = db.get_videos_published_today(channel_id)

        # ── 2. Count pending/running planned slots for today ──
        today_slots = db.get_channel_slots_today(channel_id, today)
        active_slots = [s for s in today_slots
                        if s.get("status") in ("pending", "running")]
        active_count = len(active_slots)

        total_covered = published_today + active_count

        logger.info(
            "[%s] Recovery check: target=%d published=%d active_planned=%d total=%d",
            slug, target, published_today, active_count, total_covered,
        )

        if total_covered >= target:
            logger.debug("[%s] On track — no recovery needed", slug)
            continue

        missing = target - total_covered
        logger.info("[%s] Behind by %d video(s) — attempting recovery", slug, missing)

        # ── 3. Get peak info for low-audience calc ──
        from pipeline.publish_scheduler import get_channel_peak_info
        peak_info = get_channel_peak_info(cfg)
        peak_hour = peak_info["peak_hour"]
        secondary_peaks = list(peak_info.get("secondary_peaks", []))

        # ── 4. Compute available time buckets ──
        low_aud = _low_audience_hours(peak_hour, secondary_peaks,
                                       now_minute_of_day, min_ahead)
        sec_peaks = _secondary_peak_hours(secondary_peaks,
                                           now_minute_of_day, min_ahead)

        # ── 5. Collect existing slot times for anti-collision ──
        existing_times = []
        for s in active_slots:
            t = _parse_hour_from_slot(s, "scheduled_at")
            if t is not None:
                existing_times.append(t)

        # ── 6. Create missing slots ──
        created_slots = []
        for i in range(missing):
            assigned = False
            chosen_time = None
            source = None

            # Priority 1: low-audience hour
            for t in low_aud:
                if not _collides(t, existing_times):
                    chosen_time = t
                    source = "low_audience"
                    assigned = True
                    break

            # Priority 2: secondary peak hour
            if not assigned:
                for t in sec_peaks:
                    if not _collides(t, existing_times):
                        chosen_time = t
                        source = "secondary_peak"
                        assigned = True
                        break

            # Fallback: any available time
            if not assigned:
                chosen_time = _find_first_available(now_minute_of_day, existing_times)
                source = "fallback"
                assigned = True

            if chosen_time is None:
                logger.warning(
                    "[%s] Could not find a recovery slot #%d — no available hours left today",
                    slug, i + 1,
                )
                continue

            # ── Build slot ──
            scheduled_h = chosen_time // 60
            scheduled_m = chosen_time % 60

            # target_upload_at = scheduled_at + pipeline + warmup (if scheduled)
            is_scheduled = cfg.get("publish_mode") == "scheduled"
            warmup = cfg.get("publish_warmup_min", 120) if is_scheduled else 0
            up_minutes = chosen_time + ESTIMATED_PIPELINE_MINUTES + warmup
            up_h = min(up_minutes // 60, 23)
            up_m = min(up_minutes % 60, 59)

            scheduled_str = f"{today} {scheduled_h:02d}:{scheduled_m:02d}:00"
            upload_str = f"{today} {up_h:02d}:{up_m:02d}:00"

            try:
                slot_id = db.create_planned_slot(
                    channel_id=channel_id,
                    date_key=today,
                    scheduled_at=scheduled_str,
                    target_upload_at=upload_str,
                    slot_position=active_count + i + 1,
                )

                existing_times.append(chosen_time)
                created_slots.append({
                    "slot_id": slot_id,
                    "scheduled_at": scheduled_str,
                    "target_upload_at": upload_str,
                    "source": source,
                })

                logger.info(
                    "[%s] Recovery slot #%d created: gen=%02d:%02d upload=%02d:%02d source=%s",
                    slug, slot_id,
                    scheduled_h, scheduled_m, up_h, up_m, source,
                )

            except Exception as exc:
                logger.error(
                    "[%s] Failed to create recovery slot: %s", slug, exc,
                )

        if created_slots:
            result["recovered_count"] += len(created_slots)
            result["channels_affected"].append(slug)
            result["details"].append({
                "channel_id": channel_id,
                "slug": slug,
                "target": target,
                "published": published_today,
                "active_planned": active_count,
                "missing": missing,
                "recovered": len(created_slots),
                "slots": created_slots,
            })

    if result["recovered_count"] > 0:
        logger.info(
            "Recovery complete: %d slots across %d channels: %s",
            result["recovered_count"],
            len(result["channels_affected"]),
            ", ".join(result["channels_affected"]),
        )
    else:
        logger.debug("Recovery check: all channels on track")

    return result


# ── Convenience alias ────────────────────────────────────────────
run_recovery = auto_recover_missing_publications
