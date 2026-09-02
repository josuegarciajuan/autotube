"""Shorts auto-recovery planner — dynamic rebalancing for shorts schedules (v14).

Detects channels that are behind or ahead of their daily shorts target
and creates/cancels planned slots accordingly.

v14: Capped recovery + use pct_covered (not pct_published).
  - Native target: shorts_native_per_day (fixed, e.g. 4)
  - Clip target: shorts_clips_per_long × long_videos_completed_today (dynamic!)
  - Tiered recovery uses pct_covered (published + pending) to avoid false gaps
  - MAX 2 recovery slots per channel per run (RECOVERY_MAX_PER_RUN)
  - Removed MIN_DAILY_SHORTS floor — uses native_target + clip_target directly
  - Native/clip deficit recovery also capped at 2 slots per run

v27 (Aug 2026): v26-aware clip recovery.
  - Clip coverage counts pre-rendered 'ready' shorts + pending/running slots
    (any date) so recovery stops fabricating doomed duplicate clip slots.
  - Clip recovery slots are only created when the source MP4 is still on disk
    (otherwise the slot is doomed: scheduled-publish sources are private and
    yt-dlp cannot download them once the local file is gone).
  - Slots are stored with UTC timestamps (the dispatcher treats scheduled_at
    as UTC; Madrid wall-clock fired recovery slots ~2h late).
  - Channels with shorts spam-blocked are skipped entirely.

Called every 120 minutes by the background checker loop in api/main.py,
but only between 10:00-23:00 CEST.
"""

import logging
import time
from datetime import date, datetime
from typing import Optional

import pytz

logger = logging.getLogger("autotube.shorts_recovery")

# ── Constants ────────────────────────────────────────────────────
MIN_GAP_MINUTES = 20                # Minimum gap between same-channel slot starts (was 30)
SHORTS_GEN_MINUTES = 30             # Shorts generate much faster than long-form (~30 min)
MIN_HOUR_AHEAD = 1                  # Minimum hours from now to schedule a recovery slot
# Window during which recovery is active (local hours in CEST)
RECOVERY_START_HOUR = 10            # 10:00 AM
RECOVERY_END_HOUR = 23              # 11:00 PM

# ── v13 tiered recovery thresholds ────────────────────────────
# At 14:00: if < 40% of total daily target published → emergency slots
# At 18:00: if < 70% of total daily target published → aggressive forced slots
# Total = native_target + clip_target (or MIN_DAILY_SHORTS as fallback).
RECOVERY_TIER_1_HOUR = 14
RECOVERY_TIER_1_PCT = 0.40
RECOVERY_TIER_2_HOUR = 18
RECOVERY_TIER_2_PCT = 0.70


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


# ── v12: Per-type recovery helpers ────────────────────────────────

def _count_published_by_type(db, channel_id: int) -> tuple[int, int]:
    """Count shorts published today by type (native, clip)."""
    native = 0
    clip = 0
    try:
        with db._connect() as conn:
            rows = conn.execute(
                """SELECT type FROM shorts
                   WHERE channel_id = ?
                     AND youtube_id IS NOT NULL
                     AND status = 'published'
                     AND DATE(published_at) = DATE('now', 'localtime')""",
                (channel_id,),
            ).fetchall()
            for r in rows:
                if r["type"] == "native":
                    native += 1
                elif r["type"] == "clip":
                    clip += 1
    except Exception:
        pass
    return native, clip


# ── v27: Helpers (timezone, spam-block, v26-aware clip coverage) ──

def _local_hm_to_utc(date_str: str, hour: int, minute: int,
                     tz_str: str = "Europe/Madrid") -> str:
    """Convert a local wall-clock (date_str + hour:minute) to a UTC timestamp string.

    The shorts dispatcher interprets scheduled_at / target_upload_at as UTC
    (datetime('now') in get_next_pending_shorts_slot). Recovery slots were
    stored with Madrid wall-clock times, which made them fire ~2h late.
    """
    local = pytz.timezone(tz_str).localize(
        datetime.strptime(
            f"{date_str} {int(hour):02d}:{int(minute):02d}:00",
            "%Y-%m-%d %H:%M:%S",
        )
    )
    return local.astimezone(pytz.utc).strftime("%Y-%m-%d %H:%M:%S")


def _channel_shorts_spam_blocked(channel_id: int, db) -> bool:
    """Return True if the channel's shorts are blocked by a spam strike."""
    from api.services.channel_policy import get_channel_strike_state
    return get_channel_strike_state(channel_id, db)["strike_active"]


def _count_clip_coverage(db, channel_id: int, long_ids: list[int],
                         clips_per_long: int) -> int:
    """Count how many clip shorts are already handled for today's longs.

    v26-aware: a long's clips are covered by pre-rendered 'ready' shorts,
    published shorts, OR pending/running clip slots (any date — pre-render
    schedules same-day, recovery may look across days). Per long, the covered
    count is capped at clips_per_long; MAX (not SUM) avoids double-counting a
    ready short and its own linked slot.
    """
    if not long_ids:
        return 0
    total = 0
    try:
        with db._connect() as conn:
            for vid in long_ids:
                row = conn.execute(
                    """SELECT
                         (SELECT COUNT(*) FROM shorts
                           WHERE channel_id = ? AND type = 'clip'
                             AND source_video_id = ?
                             AND status IN ('ready', 'published')) AS shorts_cnt,
                         (SELECT COUNT(*) FROM shorts_planned_slots
                           WHERE channel_id = ? AND short_type = 'clip'
                             AND source_video_id = ?
                             AND status IN ('pending', 'running')) AS slots_cnt""",
                    (channel_id, vid, channel_id, vid),
                ).fetchone()
                if row:
                    total += min(clips_per_long, max(row["shorts_cnt"] or 0,
                                                     row["slots_cnt"] or 0))
    except Exception:
        return 0
    return total


def _cancel_excess_slots(db, pending_slots: list[dict], excess: int) -> int:
    """Cancel the farthest excess pending slots. Returns count cancelled."""
    if not pending_slots or excess <= 0:
        return 0
    pending_sorted = sorted(
        pending_slots,
        key=lambda s: s.get("scheduled_at", ""),
        reverse=True,
    )
    to_cancel = [s["id"] for s in pending_sorted[:excess]]
    if to_cancel:
        db.cancel_shorts_slots(to_cancel)
        logger.info("Recovery: cancelled %d excess slot(s): %s",
                     len(to_cancel), ", ".join(f"#{sid}" for sid in to_cancel))
    return len(to_cancel)


def _cancel_disabled_clip_work(db) -> int:
    """Cancel unuploaded clip work so recovery can never resurrect clips."""
    cancelled = 0
    with db._connect() as conn:
        cur = conn.execute(
            """UPDATE shorts_planned_slots
                  SET status='cancelled', updated_at=CURRENT_TIMESTAMP
                WHERE short_type='clip' AND status IN ('pending', 'generated')"""
        )
        cancelled += cur.rowcount
        cur = conn.execute(
            """UPDATE shorts
                  SET status='cancelled', error_message='clip shorts disabled'
                WHERE type='clip' AND status IN ('generated', 'ready')
                  AND (youtube_id IS NULL OR youtube_id='')"""
        )
        cancelled += cur.rowcount
        conn.commit()
    if cancelled:
        logger.info("Recovery: cancelled %d disabled clip item(s)", cancelled)
    return cancelled


def _create_recovery_slots(
    ch_id: int, slug: str, today: str, short_type: str,
    missing: int, now_minute_of_day: int,
    active_slots: list[dict], db,
) -> list[dict]:
    """Create recovery slots of the given type. Returns list of created slot info."""
    existing_times = []
    for s in active_slots:
        t = _parse_minute_of_day(s.get("scheduled_at"))
        if t is not None:
            existing_times.append(t)

    min_ahead = MIN_HOUR_AHEAD * 60
    created_slots = []

    for i in range(missing):
        chosen = _find_available_minute(now_minute_of_day, existing_times, min_ahead)
        if chosen is None:
            logger.warning("[shorts:%s] No available time for recovery slot #%d", slug, i + 1)
            continue

        scheduled_h = chosen // 60
        scheduled_m = chosen % 60
        up_min = chosen + SHORTS_GEN_MINUTES
        up_h = min(up_min // 60, 23)
        up_m = min(up_min % 60, 59)

        scheduled_str = _local_hm_to_utc(today, scheduled_h, scheduled_m)
        upload_str = _local_hm_to_utc(today, up_h, up_m)

        try:
            slot_id = db.create_shorts_slot(
                channel_id=ch_id, date_key=today,
                scheduled_at=scheduled_str, target_upload_at=upload_str,
                short_type=short_type,
                slot_position=len(active_slots) + len(created_slots) + 1,
            )
            existing_times.append(chosen)
            created_slots.append({
                "slot_id": slot_id, "scheduled_at": scheduled_str,
                "target_upload_at": upload_str, "type": short_type,
            })
            logger.info(
                "[shorts:%s] Recovery slot #%d: gen=%02d:%02d type=%s",
                slug, slot_id, scheduled_h, scheduled_m, short_type,
            )
        except Exception as exc:
            logger.error("[shorts:%s] Failed to create recovery slot: %s", slug, exc)

    return created_slots


def _create_clip_recovery_slots(
    ch_id: int, slug: str, today: str, missing: int,
    clips_per_long: int, longs_today: list[dict],
    now_minute_of_day: int, active_slots: list[dict], db,
) -> list[dict]:
    """Create clip recovery slots, linked to completed long videos.

    v27 (v26-aware): a clip recovery slot is only created when it can actually
    be fulfilled:
      - If the source long already has ANY clip artifact (pre-rendered 'ready'
        short, published short, or pending/running slot on any date) it is
        covered — no duplicate slot is created.
      - If the source MP4 still exists on disk (pre-render failed but the file
        is there), an unlinked slot is created; dispatch extracts from the
        local file.
      - Otherwise the slot would be doomed (scheduled-publish source is private,
        no local file → yt-dlp cannot download it), so it is SKIPPED.

    Slots are stored with UTC timestamps: the dispatcher treats scheduled_at /
    target_upload_at as UTC, and storing Madrid wall-clock fired them ~2h late.
    """
    from pathlib import Path

    # Build map: long_slot_position → how many clips exist
    clip_counts_by_long = {}
    for s in active_slots:
        pos = s.get("long_slot_position")
        if pos and s.get("short_type") == "clip":
            clip_counts_by_long[pos] = clip_counts_by_long.get(pos, 0) + 1

    # Assign each missing clip to the long with fewest clips
    existing_times = []
    for s in active_slots:
        t = _parse_minute_of_day(s.get("scheduled_at"))
        if t is not None:
            existing_times.append(t)

    min_ahead = MIN_HOUR_AHEAD * 60
    created_slots = []
    skipped = 0

    for i in range(missing):
        # Pick long with fewest clips
        best_long_pos = None
        best_count = 999
        for long_pos in range(1, len(longs_today) + 1):
            count = clip_counts_by_long.get(long_pos, 0)
            if count < clips_per_long and count < best_count:
                best_count = count
                best_long_pos = long_pos

        if best_long_pos is None:
            break

        source = longs_today[best_long_pos - 1]
        source_vid = source.get("id")

        # ── Coverage guard: skip longs that already have their clips ──
        try:
            with db._connect() as conn:
                cov_row = conn.execute(
                    """SELECT
                         (SELECT COUNT(*) FROM shorts
                           WHERE channel_id = ? AND type = 'clip'
                             AND source_video_id = ?
                             AND status IN ('ready', 'published')) AS s_cnt,
                         (SELECT COUNT(*) FROM shorts_planned_slots
                           WHERE channel_id = ? AND short_type = 'clip'
                             AND source_video_id = ?
                             AND status IN ('pending', 'running')) AS p_cnt""",
                    (ch_id, source_vid, ch_id, source_vid),
                ).fetchone()
            covered = 0
            if cov_row:
                covered = min(clips_per_long, max(cov_row["s_cnt"] or 0,
                                                  cov_row["p_cnt"] or 0))
        except Exception:
            covered = 0
        if covered >= clips_per_long:
            clip_counts_by_long[best_long_pos] = clip_counts_by_long.get(best_long_pos, 0) + 1
            logger.debug(
                "[shorts:%s] Clip recovery: long=%d (source #%s) already covered "
                "(%d) — skipping duplicate",
                slug, best_long_pos, source_vid, covered,
            )
            continue

        # ── Doom guard: only create when the slot can actually be fulfilled ──
        local_ok = False
        try:
            local_ok = bool(source.get("video_path")) and Path(source["video_path"]).exists()
        except Exception:
            local_ok = False
        if not local_ok:
            skipped += 1
            logger.info(
                "[shorts:%s] Clip recovery skipped for long=%d (source #%s): "
                "no ready short and no local MP4 (private scheduled source) — "
                "slot would be doomed",
                slug, best_long_pos, source_vid,
            )
            clip_counts_by_long[best_long_pos] = clip_counts_by_long.get(best_long_pos, 0) + 1
            continue

        chosen = _find_available_minute(now_minute_of_day, existing_times, min_ahead)
        if chosen is None:
            logger.warning("[shorts:%s] No time for clip recovery slot #%d", slug, i + 1)
            continue

        scheduled_h = chosen // 60
        scheduled_m = chosen % 60
        up_min = chosen + SHORTS_GEN_MINUTES
        up_h = min(up_min // 60, 23)
        up_m = min(up_min % 60, 59)

        scheduled_str = _local_hm_to_utc(today, scheduled_h, scheduled_m)
        upload_str = _local_hm_to_utc(today, up_h, up_m)

        try:
            with db._connect() as conn:
                cur = conn.execute(
                    """INSERT INTO shorts_planned_slots
                       (channel_id, date_key, scheduled_at, target_upload_at,
                        short_type, long_slot_position, source_video_id,
                        slot_position, status)
                       VALUES (?, ?, ?, ?, 'clip', ?, ?, ?, 'pending')""",
                    (ch_id, today, scheduled_str, upload_str,
                     best_long_pos, source_vid,
                     len(active_slots) + len(created_slots) + 1),
                )
                slot_id = cur.lastrowid
                conn.commit()

            existing_times.append(chosen)
            clip_counts_by_long[best_long_pos] = clip_counts_by_long.get(best_long_pos, 0) + 1
            created_slots.append({
                "slot_id": slot_id, "scheduled_at": scheduled_str,
                "target_upload_at": upload_str, "type": "clip",
                "long_slot_position": best_long_pos,
                "source_video_id": source_vid,
            })
            logger.info(
                "[shorts:%s] Clip recovery slot #%d: gen=%02d:%02d long=%d "
                "(local MP4 present)",
                slug, slot_id, scheduled_h, scheduled_m, best_long_pos,
            )
        except Exception as exc:
            logger.error("[shorts:%s] Failed clip recovery slot: %s", slug, exc)

    if skipped:
        logger.info(
            "[shorts:%s] Clip recovery: %d candidate(s) skipped "
            "(no ready short / no local MP4)",
            slug, skipped,
        )

    return created_slots


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

        # Clips are permanently disabled; clean legacy pending work before
        # calculating coverage so no recovery pass can recreate it.
        _cancel_disabled_clip_work(db)

        # ── Per-channel guard: shorts_enabled ──
        if not cfg.get("shorts_enabled", True):
            continue

        # ── v27: Skip channels whose shorts are spam-blocked (their slots are
        # cancelled by the dispatcher anyway — creating them is pure churn) ──
        if _channel_shorts_spam_blocked(ch_id, db):
            logger.info(
                "[shorts:%s] channel spam-blocked — skipping recovery", slug,
            )
            continue

        native_target = cfg.get("shorts_native_per_day", 3)
        clips_per_long = 0

        if native_target <= 0 and clips_per_long <= 0:
            continue

        # ── 1. Count shorts published today BY TYPE ──
        published_native, published_clip = _count_published_by_type(db, ch_id)

        # ── 2. Count pending/running shorts planned slots today BY TYPE ──
        today_slots = db.get_channel_shorts_slots_today(ch_id, today)
        active_slots = [s for s in today_slots if s.get("status") in ("pending", "running")]
        active_native = [s for s in active_slots if s.get("short_type") == "native"]
        active_clip = [s for s in active_slots if s.get("short_type") == "clip"]

        # ── 3. Dynamic clip target: clips_per_long × completed longs ──
        longs_today = []
        try:
            longs_today = db.get_completed_videos_today(ch_id) or []
        except Exception:
            longs_today = []
        completed_longs = len(longs_today)
        long_ids = [v.get("id") for v in longs_today if v.get("id")]
        clip_target = clips_per_long * completed_longs

        # ── 4. Coverage by type ──
        # v27: clip coverage includes pre-rendered 'ready' shorts tied to
        # today's longs (v26 pre-render), so recovery stops fabricating doomed
        # duplicate clip slots for sources that already have their clips.
        native_covered = published_native + len(active_native)
        ready_clips = _count_clip_coverage(db, ch_id, long_ids, clips_per_long)
        clip_covered = published_clip + len(active_clip) + ready_clips

        logger.info(
            "[shorts:%s] Recovery check: native=%d/%d clip=%d/%d "
            "(target_clips=%d=%d×%d longs)",
            slug,
            native_covered, native_target,
            clip_covered, clip_target,
            clip_target, clips_per_long, completed_longs,
        )

        # ── NATIVE: excess → cancel, deficit → recover ──
        if native_covered > native_target:
            excess = native_covered - native_target
            pending_native = [s for s in active_native if s["status"] == "pending"]
            cancelled = _cancel_excess_slots(db, pending_native, excess)
            if cancelled:
                result["cancelled_count"] += cancelled
                if slug not in result["channels_affected"]:
                    result["channels_affected"].append(slug)
                result["details"].append({
                    "channel_id": ch_id, "slug": slug, "action": "cancelled_native",
                    "target": native_target, "excess": excess, "cancelled": cancelled,
                })
        elif native_covered < native_target and native_target > 0:
            missing = min(native_target - native_covered, 2)
            created = _create_recovery_slots(
                ch_id, slug, today, "native", missing,
                now_minute_of_day, active_slots, db,
            )
            if created:
                result["recovered_count"] += len(created)
                if slug not in result["channels_affected"]:
                    result["channels_affected"].append(slug)
                result["details"].append({
                    "channel_id": ch_id, "slug": slug, "action": "recovered_native",
                    "target": native_target, "missing": missing,
                    "recovered": len(created), "slots": created,
                })

        # ── TIERED RECOVERY (v14): time-based thresholds ───────────
        # v14: total_target = native_target + clip_target (per-channel config).
        # Removed MIN_DAILY_SHORTS floor — it inflated targets beyond per-channel config.
        # Uses pct_covered (published + pending/running) instead of pct_published
        # to avoid creating slots when coverage is already sufficient but not yet published.
        # Caps recovery to max 2 slots per channel per run (RECOVERY_MAX_PER_RUN).
        total_target = native_target + clip_target
        total_published = published_native + published_clip
        total_covered = native_covered + clip_covered
        pct_covered = total_covered / max(total_target, 1)
        pct_published = total_published / max(total_target, 1)
        RECOVERY_MAX_PER_RUN = 2  # cap recovery slots per channel per run

        # Tier 1 (14:00): < 40% of total target covered → emergency (capped)
        if RECOVERY_TIER_1_HOUR <= now_hour < RECOVERY_TIER_2_HOUR:
            if pct_covered < RECOVERY_TIER_1_PCT and total_target > 0:
                emergency_count = max(
                    1, int((RECOVERY_TIER_1_PCT - pct_covered) * total_target)
                )
                emergency_count = min(emergency_count, total_target - total_covered)
                emergency_count = min(emergency_count, RECOVERY_MAX_PER_RUN)
                if emergency_count > 0:
                    logger.warning(
                        "[shorts:%s] TIER-1 EMERGENCY @ %02d:00: "
                        "%d/%d covered (%.0f%% < %.0f%%) — "
                        "creating %d emergency slots (capped at %d)",
                        slug, RECOVERY_TIER_1_HOUR,
                        total_covered, total_target,
                        pct_covered * 100, RECOVERY_TIER_1_PCT * 100,
                        emergency_count, RECOVERY_MAX_PER_RUN,
                    )
                    created = _create_recovery_slots(
                        ch_id, slug, today, "native", emergency_count,
                        now_minute_of_day, active_slots, db,
                    )
                    if created:
                        result["recovered_count"] += len(created)
                        if slug not in result["channels_affected"]:
                            result["channels_affected"].append(slug)
                        result["details"].append({
                            "channel_id": ch_id, "slug": slug,
                            "action": "recovered_tier1_emergency",
                            "total_target": total_target,
                            "covered": total_covered,
                            "pct": round(pct_covered * 100, 1),
                            "recovered": len(created), "slots": created,
                        })

        # Tier 2 (18:00): < 70% of total target covered → aggressive forced fill (capped)
        if now_hour >= RECOVERY_TIER_2_HOUR:
            if pct_covered < RECOVERY_TIER_2_PCT and total_target > 0:
                # Fill remaining gap, but capped
                forced_count = min(total_target - total_covered, RECOVERY_MAX_PER_RUN)
                if forced_count > 0:
                    logger.warning(
                        "[shorts:%s] TIER-2 AGGRESSIVE @ %02d:00: "
                        "%d/%d covered (%.0f%% < %.0f%%) — "
                        "forcing %d remaining slots (capped at %d)",
                        slug, RECOVERY_TIER_2_HOUR,
                        total_covered, total_target,
                        pct_covered * 100, RECOVERY_TIER_2_PCT * 100,
                        forced_count, RECOVERY_MAX_PER_RUN,
                    )
                    created = _create_recovery_slots(
                        ch_id, slug, today, "native", forced_count,
                        now_minute_of_day, active_slots, db,
                    )
                    if created:
                        result["recovered_count"] += len(created)
                        if slug not in result["channels_affected"]:
                            result["channels_affected"].append(slug)
                        result["details"].append({
                            "channel_id": ch_id, "slug": slug,
                            "action": "recovered_tier2_aggressive",
                            "total_target": total_target,
                            "covered": total_covered,
                            "pct": round(pct_covered * 100, 1),
                            "recovered": len(created), "slots": created,
                        })

        # ── CLIPS: excess → cancel, deficit → ONLY recreate if more longs completed ──
        if clip_covered > clip_target:
            excess = clip_covered - clip_target
            pending_clip = [s for s in active_clip if s["status"] == "pending"]
            cancelled = _cancel_excess_slots(db, pending_clip, excess)
            if cancelled:
                result["cancelled_count"] += cancelled
                if slug not in result["channels_affected"]:
                    result["channels_affected"].append(slug)
                result["details"].append({
                    "channel_id": ch_id, "slug": slug, "action": "cancelled_clip",
                    "target": clip_target, "excess": excess, "cancelled": cancelled,
                })
        elif clip_covered < clip_target and clip_target > 0:
            missing = min(clip_target - clip_covered, 2)
            # Assign missing clips to completed longs with fewest existing clips
            created = _create_clip_recovery_slots(
                ch_id, slug, today, missing, clips_per_long,
                longs_today, now_minute_of_day, active_slots, db,
            )
            if created:
                result["recovered_count"] += len(created)
                if slug not in result["channels_affected"]:
                    result["channels_affected"].append(slug)
                result["details"].append({
                    "channel_id": ch_id, "slug": slug, "action": "recovered_clip",
                    "target": clip_target, "missing": missing,
                    "recovered": len(created), "slots": created,
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
