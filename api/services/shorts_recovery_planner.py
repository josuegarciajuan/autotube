"""Shorts auto-recovery planner — dynamic rebalancing for shorts schedules.

Detects channels that are behind or ahead of their daily shorts target
and creates/cancels planned slots accordingly.

Algorithm:
  1. For each active channel with shorts enabled, read combined daily target
     (shorts_native_per_day + shorts_clip_per_day = total target).
  2. Count shorts successfully published today (youtube_id IS NOT NULL).
  3. Count shorts_planned_slots still pending/running for today.
  4. If published + active < target → create recovery slots.
  5. If published + active > target → cancel excess pending slots.
  6. Recovery slots use low-audience hours to avoid prime time.

Called every 60 minutes by the background checker loop in api/main.py,
but only between 10:00-23:00 CEST.
"""

import logging
from datetime import date, datetime
from typing import Optional

import pytz

logger = logging.getLogger("autotube.shorts_recovery")

# ── Constants ────────────────────────────────────────────────────
MIN_GAP_MINUTES = 30                # Minimum gap between same-channel slot starts
SHORTS_GEN_MINUTES = 30             # Shorts generate much faster than long-form (~30 min)
MIN_HOUR_AHEAD = 1                  # Minimum hours from now to schedule a recovery slot
# Window during which recovery is active (local hours in CEST)
RECOVERY_START_HOUR = 10            # 10:00 AM
RECOVERY_END_HOUR = 23              # 11:00 PM


def _now_madrid() -> datetime:
    """Return current datetime in Europe/Madrid timezone."""
    return datetime.now(pytz.timezone("Europe/Madrid"))


def _parse_minute_of_day(val: str | None) -> Optional[int]:
    """Extract the minute-of-day (0-1439) from a slot's timestamp field."""
    if not val:
        return None
    try:
        s = str(val).replace("T", " ")
        h, m = map(int, s[11:16].split(":"))
        return h * 60 + m
    except (ValueError, IndexError):
        return None


def _collides(minute_of_day: int, existing: list[int],
              gap_min: int = MIN_GAP_MINUTES) -> bool:
    """Check if minute_of_day collides with any existing slot time."""
    for e in existing:
        if abs(minute_of_day - e) < gap_min:
            return True
    return False


def _find_available_minute(now_minute: int, existing: list[int],
                           min_ahead: int,
                           gap_min: int = MIN_GAP_MINUTES) -> Optional[int]:
    """Find first available minute-of-day that is >= now + min_ahead and doesn't collide.

    Scans in 15-min increments for efficiency.
    """
    start = now_minute + min_ahead
    for m in range(start, 24 * 60, 15):
        if not _collides(m, existing, gap_min):
            return m
    return None


def auto_recover_shorts(db=None) -> dict:
    """Main shorts recovery function. Checks all channels and rebalances.

    Called periodically from the background scheduler loop (every 60 min).

    Returns:
        dict with {recovered_count, cancelled_count, channels_affected, details}.
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
            "cancelled_count": 0,
            "skipped": True,
            "reason": (
                f"Outside recovery window "
                f"({RECOVERY_START_HOUR:02d}-{RECOVERY_END_HOUR:02d}h)"
            ),
            "now_hour": now_hour,
        }

    today = date.today().isoformat()
    min_ahead = MIN_HOUR_AHEAD * 60  # 60 minutes

    result = {
        "date": today,
        "recovered_count": 0,
        "cancelled_count": 0,
        "channels_affected": [],
        "details": [],
    }

    # ── Get all active channels with shorts config ──
    configs = db.get_shorts_planning_config()
    if not configs:
        return {**result, "reason": "no_channel_configs"}

    for cfg in configs:
        ch_id = cfg["channel_id"]
        slug = cfg.get("slug", f"channel_{ch_id}")

        # ── Per-channel guard: shorts_enabled ──
        if not cfg.get("shorts_enabled", True):
            continue

        native = cfg.get("shorts_native_per_day", 0)
        clip = cfg.get("shorts_clip_per_day", 0)
        target = native + clip
        if target <= 0:
            continue

        # ── 1. Count shorts published today ──
        published_today = db.get_shorts_published_today(ch_id)

        # ── 2. Count pending/running shorts planned slots for today ──
        today_slots = db.get_channel_shorts_slots_today(ch_id, today)
        active_slots = [
            s for s in today_slots
            if s.get("status") in ("pending", "running")
        ]
        active_count = len(active_slots)

        total_covered = published_today + active_count

        logger.info(
            "[shorts:%s] Recovery check: target=%d (n=%d+c=%d) "
            "published=%d active_planned=%d total=%d",
            slug, target, native, clip,
            published_today, active_count, total_covered,
        )

        # ── EXCESS: cancel excess pending slots ──
        if total_covered > target:
            excess = total_covered - target
            pending = [s for s in active_slots if s["status"] == "pending"]
            cancelled = 0

            if pending and excess > 0:
                # Cancel latest (farthest) pending slots first
                pending_sorted = sorted(
                    pending,
                    key=lambda s: s.get("scheduled_at", ""),
                    reverse=True,
                )
                to_cancel = [s["id"] for s in pending_sorted[:excess]]

                if to_cancel:
                    db.cancel_shorts_slots(to_cancel)
                    cancelled = len(to_cancel)
                    logger.info(
                        "[shorts:%s] Excess: cancelled %d pending slot(s) "
                        "(target=%d): %s",
                        slug, cancelled, target,
                        ", ".join(f"#{sid}" for sid in to_cancel),
                    )

            result["cancelled_count"] += cancelled
            result["details"].append({
                "channel_id": ch_id,
                "slug": slug,
                "action": "cancelled_excess",
                "target": target,
                "published": published_today,
                "active_planned": active_count,
                "excess": excess,
                "cancelled": cancelled,
            })
            if cancelled and slug not in result["channels_affected"]:
                result["channels_affected"].append(slug)
            continue

        # ── ON TRACK ──
        if total_covered == target:
            logger.debug("[shorts:%s] On track — no recovery needed", slug)
            continue

        # ── DEFICIT: create recovery slots ──
        missing = target - total_covered
        logger.info(
            "[shorts:%s] Behind by %d short(s) — creating recovery slots",
            slug, missing,
        )

        # ── 3. Collect existing slot times for anti-collision ──
        existing_times: list[int] = []
        for s in active_slots:
            t = _parse_minute_of_day(s.get("scheduled_at"))
            if t is not None:
                existing_times.append(t)

        # ── 4. Create missing slots ──
        created_slots = []
        for i in range(missing):
            chosen = _find_available_minute(
                now_minute_of_day,
                existing_times,
                min_ahead,
            )
            if chosen is None:
                logger.warning(
                    "[shorts:%s] No available time window for "
                    "recovery slot #%d today",
                    slug, i + 1,
                )
                continue

            scheduled_h = chosen // 60
            scheduled_m = chosen % 60

            up_min = chosen + SHORTS_GEN_MINUTES
            up_h = min(up_min // 60, 23)
            up_m = min(up_min % 60, 59)

            scheduled_str = f"{today} {scheduled_h:02d}:{scheduled_m:02d}:00"
            upload_str = f"{today} {up_h:02d}:{up_m:02d}:00"

            try:
                slot_id = db.create_shorts_slot(
                    channel_id=ch_id,
                    date_key=today,
                    scheduled_at=scheduled_str,
                    target_upload_at=upload_str,
                    short_type="native",
                    slot_position=active_count + created_slots.__len__() + 1,
                )

                existing_times.append(chosen)
                created_slots.append({
                    "slot_id": slot_id,
                    "scheduled_at": scheduled_str,
                    "target_upload_at": upload_str,
                    "type": "native",
                })

                logger.info(
                    "[shorts:%s] Recovery slot #%d created: "
                    "gen=%02d:%02d upload=%02d:%02d type=native",
                    slug, slot_id,
                    scheduled_h, scheduled_m,
                    up_h, up_m,
                )

            except Exception as exc:
                logger.error(
                    "[shorts:%s] Failed to create recovery slot: %s",
                    slug, exc,
                )

        if created_slots:
            result["recovered_count"] += len(created_slots)
            if slug not in result["channels_affected"]:
                result["channels_affected"].append(slug)
            result["details"].append({
                "channel_id": ch_id,
                "slug": slug,
                "action": "recovered",
                "target": target,
                "published": published_today,
                "active_planned": active_count,
                "missing": missing,
                "recovered": len(created_slots),
                "slots": created_slots,
            })

    if result["recovered_count"] > 0 or result["cancelled_count"] > 0:
        logger.info(
            "Shorts recovery complete: +%d created, -%d cancelled "
            "across %d channels: %s",
            result["recovered_count"],
            result["cancelled_count"],
            len(result["channels_affected"]),
            ", ".join(result["channels_affected"]),
        )
    else:
        logger.debug("Shorts recovery: all channels on track")

    return result
