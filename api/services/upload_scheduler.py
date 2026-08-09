"""Upload Scheduler — Phase 2 of the 3-phase pipeline.

Dispatches upload jobs for videos that have been generated locally (F1)
and are awaiting upload (F2). Uploads happen within each channel's
configured upload windows (UPLOAD_WINDOWS list, v11+).

Upload windows are multi-window: morning (10-13h) and evening (20-22h) by default.
Videos are distributed across windows via round-robin at random times
per day to avoid bot-like patterns. Backward compatible with single-window
UPLOAD_WINDOW_START/END config.

Architecture:
  dispatch_due_uploads(db) → checks for awaiting_upload videos,
  computes random upload times within windows (round-robin),
  dispatches upload_only jobs when scheduled_upload_at arrives.
  Respects per-channel concurrency and global upload limit.

Called every 5 min by the checker loop in api/main.py.
"""

import json
import logging
import random
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

logger = logging.getLogger("autotube.upload_scheduler")

MAX_CONCURRENT_UPLOADS = 1  # One upload at a time (avoids YouTube rate limits)
MAX_UPLOAD_RETRY_PER_VIDEO = 10  # Max upload attempts before marking video as error
# ^ Increased from 3 → 10 (2026-08-09): Long backoff allows surviving multi-hour
#   outages (quota exhaustion, server restarts, YouTube processing delays).
#   Glass Box auto-recovery provides a final safety net if all 10 fail.

# Round-robin state: {(channel_id, date_str): last_window_index}
# Resets naturally when the date changes.
_windows_rr: dict[tuple[int, str], int] = {}


def _parse_upload_windows(ch_cfg: dict) -> list[dict]:
    """Parse upload windows from channel config with backward compat.

    v11+: expects UPLOAD_WINDOWS = [{"start": 10, "end": 13}, ...]
    Legacy: falls back to UPLOAD_WINDOW_START / UPLOAD_WINDOW_END ints.
    """
    windows = ch_cfg.get("UPLOAD_WINDOWS")
    if windows and isinstance(windows, list) and len(windows) > 0:
        valid = []
        for w in windows:
            if isinstance(w, dict) and "start" in w and "end" in w:
                valid.append({"start": int(w["start"]), "end": int(w["end"])})
        if valid:
            return valid
    # Backward compat: old single-window format
    ws = int(ch_cfg.get("UPLOAD_WINDOW_START", 9))
    we = int(ch_cfg.get("UPLOAD_WINDOW_END", 11))
    return [{"start": ws, "end": we}]


def _is_in_any_window(now_hour: int, windows: list[dict]) -> bool:
    """Check if current hour falls within any upload window."""
    for w in windows:
        if w["start"] <= now_hour < w["end"]:
            return True
    return False


def _compute_random_upload_time(
    windows: list[dict],
    now: datetime,
    channel_id: int,
) -> datetime | None:
    """Pick next window via round-robin and compute random upload time within it.

    Returns the scheduled upload datetime, or None if no window is available
    at all today (shouldn't happen with valid windows config).
    """
    today_str = now.date().isoformat()
    rr_key = (channel_id, today_str)

    # Get next window index via round-robin
    last_idx = _windows_rr.get(rr_key, -1)
    next_idx = (last_idx + 1) % len(windows)
    _windows_rr[rr_key] = next_idx
    chosen = windows[next_idx]

    window_start_dt = now.replace(
        hour=chosen["start"], minute=0, second=0, microsecond=0
    )
    window_end_dt = now.replace(
        hour=chosen["end"], minute=0, second=0, microsecond=0
    )

    # If the chosen window has already passed today, try the next available one
    if now >= window_end_dt:
        found = False
        for offset in range(1, len(windows) + 1):
            alt_idx = (next_idx + offset) % len(windows)
            alt = windows[alt_idx]
            alt_start = now.replace(
                hour=alt["start"], minute=0, second=0, microsecond=0
            )
            alt_end = now.replace(
                hour=alt["end"], minute=0, second=0, microsecond=0
            )
            if now < alt_end:
                chosen = alt
                next_idx = alt_idx
                _windows_rr[rr_key] = next_idx
                window_start_dt = alt_start
                window_end_dt = alt_end
                found = True
                break
        if not found:
            # All windows passed today — use tomorrow's first window
            tomorrow = now + timedelta(days=1)
            first_win = windows[0]
            window_start_dt = tomorrow.replace(
                hour=first_win["start"], minute=0, second=0, microsecond=0
            )
            window_end_dt = tomorrow.replace(
                hour=first_win["end"], minute=0, second=0, microsecond=0
            )
            _windows_rr[rr_key] = -1  # Reset for tomorrow

    # Determine the earliest possible time within the window
    if now < window_start_dt:
        earliest = window_start_dt
    else:
        earliest = now

    remaining_seconds = int((window_end_dt - earliest).total_seconds())
    if remaining_seconds <= 0:
        return None

    delay = random.randint(0, remaining_seconds)
    scheduled_time = earliest + timedelta(seconds=delay)

    logger.info(
        "Upload scheduled: channel=%d window=%02d:00-%02d:00, "
        "random=%s (delay=%ds of %ds)",
        channel_id,
        chosen["start"], chosen["end"],
        scheduled_time.strftime("%H:%M:%S"),
        delay, remaining_seconds,
    )
    return scheduled_time


def _recover_stuck_uploading_videos(db) -> int:
    """Safety-net recovery for videos stuck in 'uploading' or 'ready' whose upload job died.

    Scenarios this catches:
    1. Server restart between `db.update_video(status='uploading')` and
       `asyncio.create_task(...)` — the video gets stuck with no worker.
    2. Worker crashed immediately after dispatch before marking job as `running`.
    3. Startup recovery missed the video due to race condition or timing.
    4. Upload job failed during dispatch and video reverted to 'ready'
       instead of 'awaiting_upload' (ghost worker, manual cleanup, etc).

    Strategy: find videos in 'uploading' or 'ready' status whose latest
    upload_only job is 'failed' (dead worker), and revert them to
    'awaiting_upload' so the dispatcher retries them on the next cycle.

    Returns count of recovered videos.
    """
    recovered = 0
    try:
        with db._connect() as conn:
            # ── Scenario A: videos stuck in 'uploading' with failed upload job ──
            stuck = conn.execute(
                """SELECT v.id, v.channel_id
                   FROM videos v
                   JOIN (
                       SELECT video_id, MAX(id) as max_job_id
                       FROM generation_jobs
                       WHERE action = 'upload_only'
                       GROUP BY video_id
                   ) j_latest ON j_latest.video_id = v.id
                   JOIN generation_jobs j ON j.id = j_latest.max_job_id
                   WHERE v.status = 'uploading'
                     AND j.status = 'failed'
                """
            ).fetchall()

            for row in stuck:
                video_id = row["id"]
                conn.execute(
                    "UPDATE videos SET status='awaiting_upload', "
                    "progress_phase='upload', scheduled_upload_at=NULL "
                    "WHERE id=? AND status='uploading'",
                    (video_id,)
                )
                if conn.total_changes > 0:
                    recovered += 1
                    logger.warning(
                        "🔁 Recovery: video #%d (ch=%d) stuck in 'uploading' with dead "
                        "upload job → reverted to 'awaiting_upload' for retry",
                        video_id, row["channel_id"],
                    )

            # ── Scenario B: videos stuck in 'ready' with a failed upload job ──
            # These were generated successfully but the upload dispatch failed
            # (ghost worker, manual cleanup, concurrency guard, etc). Without
            # recovery they sit in 'ready' forever with no planned_slot.
            stuck_ready = conn.execute(
                """SELECT v.id, v.channel_id
                   FROM videos v
                   JOIN (
                       SELECT video_id, MAX(id) as max_job_id
                       FROM generation_jobs
                       WHERE action = 'upload_only'
                       GROUP BY video_id
                   ) j_latest ON j_latest.video_id = v.id
                   JOIN generation_jobs j ON j.id = j_latest.max_job_id
                   WHERE v.status = 'ready'
                     AND j.status = 'failed'
                """
            ).fetchall()

            for row in stuck_ready:
                video_id = row["id"]
                conn.execute(
                    "UPDATE videos SET status='awaiting_upload', "
                    "progress=100, progress_phase='upload', "
                    "scheduled_upload_at=NULL, error_message=NULL "
                    "WHERE id=? AND status='ready'",
                    (video_id,)
                )
                if conn.total_changes > 0:
                    recovered += 1
                    logger.warning(
                        "🔁 Recovery: video #%d (ch=%d) stuck in 'ready' with failed "
                        "upload job → reverted to 'awaiting_upload' for retry",
                        video_id, row["channel_id"],
                    )
            conn.commit()
    except Exception as e:
        logger.debug("Recovery scan for stuck uploading videos skipped: %s", e)
    return recovered


def _check_for_overlapping_targets(db, channel_id: int, slug: str,
                                   effective_target: str, current_video_id: int) -> str:
    """v11+v23: Detect and FIX overlapping target_public_at collisions.

    If multiple uploaded_private videos share the same target_public_at,
    push the current video forward by 3h to avoid simultaneous publications.
    Updates DB (videos, planned_slots, lifecycle_actions) to reflect the fix.

    Returns:
        The (possibly corrected) effective_target string.
    """
    if not effective_target:
        return effective_target

    try:
        from datetime import timedelta as _td

        with db._connect() as conn:
            rows = conn.execute(
                """SELECT v.id, v.titulo_final, v.target_public_at
                   FROM videos v
                   WHERE v.channel_id = ?
                     AND v.status IN ('uploaded_private', 'uploading', 'warming', 'scheduled')
                     AND v.id != ?
                     AND v.target_public_at = ?
                   ORDER BY v.id
                   LIMIT 20""",
                (channel_id, current_video_id, effective_target),
            ).fetchall()

        if not rows:
            return effective_target

        overlap_ids = [r["id"] for r in rows]
        logger.warning(
            "⚠️ [%s] Video #%d target_public_at COLLISION with %s at %s — auto-fixing...",
            slug, current_video_id, overlap_ids, str(effective_target)[:19],
        )

        # ── Auto-fix: push current video forward by 3h ──
        # Parse current effective_target
        for fmt in ["%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"]:
            try:
                current_dt = datetime.strptime(str(effective_target)[:19], fmt)
                break
            except ValueError:
                continue
        else:
            logger.warning("[%s] Cannot parse effective_target '%s' — keeping as-is",
                           slug, effective_target)
            return effective_target

        # Push forward by 3h and persist
        GAP_HOURS = 3
        new_dt = current_dt + _td(hours=GAP_HOURS)
        new_target = new_dt.strftime("%Y-%m-%dT%H:%M:%S+00:00")

        # Persist to DB
        old_tpa = effective_target
        db.update_video(current_video_id, target_public_at=new_target)

        with db._connect() as conn:
            conn.execute(
                """UPDATE planned_slots SET target_public_at = ?
                   WHERE video_id = ? AND target_public_at = ?""",
                (new_target, current_video_id, old_tpa),
            )
            conn.execute(
                """UPDATE video_lifecycle_actions SET scheduled_for = ?
                   WHERE video_id = ? AND scheduled_for = ?""",
                (new_target, current_video_id, old_tpa),
            )
            conn.commit()

        logger.info(
            "✅ [%s] Video #%d: overlap auto-fixed: %s → %s",
            slug, current_video_id,
            str(old_tpa)[:19], str(new_target)[:19],
        )

        return new_target

    except Exception:
        pass

    return effective_target


def dispatch_due_uploads(loop=None, db=None) -> dict | None:
    """Check for awaiting_upload videos and dispatch upload jobs.

    A video is ready for upload when:
    1. Status is 'awaiting_upload'
    2. Its scheduled_upload_at has arrived (or is NULL → compute now)
    3. Current time is within its channel's upload windows (or video is past-due)
    4. Its file still exists locally
    5. No other upload is in progress (MAX_CONCURRENT_UPLOADS)

    Returns:
        dict with dispatched info, or None if nothing to upload.
    """
    if db is None:
        from database.db_extended import ExtendedDatabase

        db = ExtendedDatabase()

    # ── 0. Recovery scan: revert stuck 'uploading' videos whose job died ──
    # This catches videos that got stuck due to server restart / worker crash
    # where the startup recovery didn't revert them (e.g. race condition, missed tick).
    _recover_stuck_uploading_videos(db)

    # ── 1. Count active upload jobs ──
    active_uploads = db.count_active_upload_jobs()
    if active_uploads >= MAX_CONCURRENT_UPLOADS:
        logger.info(
            "📤 Upload scheduler: %d upload(s) activos (max=%d) — no se despachan más",
            active_uploads, MAX_CONCURRENT_UPLOADS,
        )
        return None

    # ── 2. Find videos awaiting upload (due now or needing scheduling) ──
    now = datetime.now()
    now_utc = datetime.now(timezone.utc)  # UTC-aware for past-due comparisons
    now_utc_naive = now_utc.replace(tzinfo=None)  # naive UTC for legacy fallback comparisons

    with db._connect() as conn:
        rows = conn.execute(
            """SELECT v.id, v.channel_id, v.canal, v.video_path, v.thumbnail_path,
                       v.titulo_final, v.description, v.tags_json, v.target_public_at,
                       v.scheduled_upload_at, c.slug as channel_slug, c.config_json
               FROM videos v
               JOIN channels c ON v.channel_id = c.id
                WHERE v.status = 'awaiting_upload'
                  AND v.video_path IS NOT NULL
                  AND v.video_path != ''
                  AND (v.scheduled_upload_at IS NULL
                       OR REPLACE(v.scheduled_upload_at, 'T', ' ') <= datetime('now', 'localtime'))
                  -- v24: exclude videos that exceeded upload retry limit
                   AND (SELECT COUNT(*) FROM generation_jobs gj2
                        WHERE gj2.video_id = v.id AND gj2.action = 'upload_only'
                          AND gj2.status = 'failed') < ?
                ORDER BY v.scheduled_upload_at ASC, v.created_at ASC
               LIMIT 20""",
            (MAX_UPLOAD_RETRY_PER_VIDEO,),
        ).fetchall()

    if not rows:
        return None

    # ── 3. Filter candidates: compute scheduled_upload_at if NULL, check windows ──
    eligible = []
    for row in rows:
        ch_cfg = {}
        try:
            ch_cfg = json.loads(row["config_json"] or "{}")
        except (json.JSONDecodeError, TypeError):
            pass

        windows = _parse_upload_windows(ch_cfg)
        channel_id = row["channel_id"]
        video_id = row["id"]

        # Check if scheduled_upload_at needs to be set (first time seeing this video)
        # sqlite3.Row doesn't have .get() — use dict-style access with fallback
        sched_at_val = row["scheduled_upload_at"] if "scheduled_upload_at" in row.keys() else None
        if sched_at_val is None:
            # ── v24: exponential backoff for retries ──
            retry_count = 0
            try:
                retry_count = conn.execute(
                    "SELECT COUNT(*) FROM generation_jobs "
                    "WHERE video_id = ? AND action = 'upload_only' AND status = 'failed'",
                    (video_id,),
                ).fetchone()[0]
            except Exception:
                pass
            if retry_count > 0:
                # Exponential backoff: 10min * 2^(retry-1), capped at 12h (720 min)
                backoff_min = min(10 * (2 ** (retry_count - 1)), 720)
                sched_time = now + timedelta(minutes=backoff_min)
                logger.info(
                    "Video %d: retry #%d — applying %dmin backoff (next: %s)",
                    video_id, retry_count, backoff_min,
                    sched_time.strftime("%H:%M"),
                )
            else:
                sched_time = _compute_random_upload_time(windows, now, channel_id)
                if sched_time is None:
                    logger.debug("Video %d: no upload window available, skipping", video_id)
                    continue
            # Store in DB for future cycles
            try:
                db.update_video(video_id, scheduled_upload_at=sched_time.strftime('%Y-%m-%d %H:%M:%S'))
            except Exception:
                pass
            if sched_time > now:
                logger.debug(
                    "Video %d: scheduled at %s, waiting...",
                    video_id, sched_time.strftime("%H:%M"),
                )
                continue
            # sched_time <= now — ready to dispatch now

        # Past-due check (target_public_at already passed → catch-up)
        past_due = False
        target_public = row["target_public_at"]
        if target_public:
            try:
                from pipeline.publish_scheduler import _parse_target_public_at
                # Parse with timezone awareness (handles both naive local and ISO8601 UTC)
                pub_dt = _parse_target_public_at(str(target_public))
                # Compare aware-vs-aware: pub_dt is UTC-aware, now_utc is also UTC-aware
                if pub_dt is not None and pub_dt < now_utc:
                    past_due = True
                    logger.info(
                        "Video %d: public time already passed — uploading ASAP", video_id
                    )
            except (ValueError, TypeError):
                # Fallback: try legacy naive parsing
                # target_public format: "2026-07-24T23:00:00+00:00" — strip tz offset for naive parse
                try:
                    pub_dt = datetime.strptime(str(target_public)[:19], "%Y-%m-%dT%H:%M:%S")
                    if pub_dt < now_utc_naive:
                        past_due = True
                        logger.info(
                            "Video %d: public time already passed — uploading ASAP",
                            video_id,
                        )
                except (ValueError, TypeError):
                    try:
                        pub_dt = datetime.fromisoformat(
                            str(target_public).replace("Z", "+00:00")
                        )
                        if pub_dt < now_utc:
                            past_due = True
                            logger.info(
                                "Video %d: public time already passed — uploading ASAP",
                                video_id,
                            )
                    except (ValueError, TypeError):
                        pass

        # v12.1: — scheduled_upload_at past-due check ─
        # A video whose scheduled_upload_at has passed is overdue for upload
        # regardless of whether target_public_at has arrived yet.
        if not past_due and sched_at_val is not None:
            try:
                sched_dt = datetime.strptime(
                    str(sched_at_val)[:19], "%Y-%m-%d %H:%M:%S"
                )
                if sched_dt < now:
                    past_due = True
                    logger.info(
                        "Video %d: scheduled_upload_at already passed — uploading ASAP",
                        video_id,
                    )
            except (ValueError, TypeError):
                pass

        # Window gate (unless past-due)
        current_hour = now.hour
        if past_due or _is_in_any_window(current_hour, windows):
            eligible.append({"row": dict(row), "past_due": past_due})

    if not eligible:
        # ── Heartbeat: log only once every ~15 min when idle ──
        import time as _t
        if not hasattr(dispatch_due_uploads, "_last_noop_log") or \
           _t.time() - dispatch_due_uploads._last_noop_log > 900:
            total_awaiting = len(rows)
            logger.info(
                "📤 Upload scheduler: 0 vídeos en ventana (%d esperando total)",
                total_awaiting,
            )
            dispatch_due_uploads._last_noop_log = _t.time()
        return None

    # ── Sort: past-due videos first, then by created_at ──
    eligible.sort(key=lambda v: (not v["past_due"], v["row"].get("created_at", "")))

    # ── 3b. Daily upload limit: skip channels that already hit their quota today ──
    today_uploads = {}  # channel_id → count of uploads today
    for entry in eligible:
        ch_id = entry["row"]["channel_id"]
        if ch_id not in today_uploads:
            with db._connect() as conn:
                row = conn.execute(
                    """SELECT COUNT(*) as cnt FROM videos
                       WHERE channel_id = ?
                         AND date(uploaded_at) = date('now', 'localtime')
                         AND status = 'published'
                         AND yt_video_id IS NOT NULL""",
                    (ch_id,),
                ).fetchone()
            today_uploads[ch_id] = row["cnt"] if row else 0

    # Resolve videos_per_day per channel from config_json
    ch_vpd = {}  # channel_id → videos_per_day
    for entry in eligible:
        ch_id = entry["row"]["channel_id"]
        if ch_id not in ch_vpd:
            try:
                cfg = json.loads(entry["row"].get("config_json") or "{}")
                vpd = cfg.get("videos_per_day", 1)
                ch_vpd[ch_id] = max(vpd, 1)  # at least 1
            except Exception:
                ch_vpd[ch_id] = 1

    # Pick first eligible video whose channel hasn't hit daily quota
    selected = None
    for entry in eligible:
        ch_id = entry["row"]["channel_id"]
        uploaded_today = today_uploads.get(ch_id, 0)
        max_allowed = ch_vpd.get(ch_id, 1)
        if uploaded_today >= max_allowed:
            slug = entry["row"].get("channel_slug", "?")
            logger.info(
                "📤 Upload skipped for %s: daily quota met (%d/%d)",
                slug, uploaded_today, max_allowed,
            )
            continue
        selected = entry
        break

    if not selected:
        logger.debug("📤 Upload scheduler: all eligible channels at daily quota")
        return None

    entry = selected
    video = entry["row"]
    video_id = video["id"]
    channel_id = video["channel_id"]
    slug = video.get("channel_slug", video.get("canal", "unknown"))

    # Per-channel guard: skip if this channel already has an upload running
    active_for_channel = db.get_active_upload_job_for_channel(channel_id)
    if active_for_channel:
        logger.debug("Upload for %s deferred: channel already has active upload", slug)
        return None

    # Verify file exists
    vp = Path(video["video_path"]) if video.get("video_path") else None
    if not vp or not vp.exists():
        logger.warning(
            "Video %d: file missing (%s) — marking as error",
            video_id, video.get("video_path"),
        )
        db.update_video(video_id, status="error", progress_phase="upload")
        return None

    # ── v12: Recalculate target_public_at if stale before dispatch ──
    # The video's target_public_at may be in the past (planned before gen delay)
    # or before the upload time. Recalculate to ensure it's after upload+warmup.
    effective_target = video.get("target_public_at")
    try:
        from pipeline.publish_scheduler import _target_is_stale, ensure_future_target_public_at
        ch_cfg_raw = ch_cfg.get("PUBLISH_TIMEZONE", "Europe/Madrid")
        ch_cfg_spread = ch_cfg.get("PUBLISH_WINDOW_SPREAD_MIN", 90)
        if _target_is_stale(effective_target, timezone_str=ch_cfg_raw, warmup_min=60):
            logger.warning(
                "[%s] Video #%d: target_public_at is stale (%s). Recalculating...",
                slug, video_id, str(effective_target)[:19] if effective_target else "None"
            )
            effective_target = ensure_future_target_public_at(
                effective_target,
                slug=slug,
                timezone_str=ch_cfg_raw,
                db=db,
                channel_id=channel_id,
                warmup_min=60,
                publish_window_spread_min=ch_cfg_spread,
            )
            # Persist to both tables
            db.update_video(video_id, target_public_at=effective_target)
            with db._connect() as conn:
                conn.execute(
                    "UPDATE planned_slots SET target_public_at = ? WHERE video_id = ?",
                    (effective_target, video_id),
                )
                conn.commit()
            logger.info(
                "[%s] Video #%d: target_public_at recalculated → %s",
                slug, video_id, effective_target[:19],
            )
    except Exception as e:
        logger.debug("[%s] Target recalculation skipped: %s", slug, e)

    # ── v23: Overlap guard — detect and auto-fix if multiple videos share the same target_public_at ──
    effective_target = _check_for_overlapping_targets(db, channel_id, slug, effective_target, video_id)

    logger.info(
        "📤 Despachando subida: video #%d (%s), archivo=%s | público programado: %s",
        video_id, slug, vp.name,
        (
            (str(video.get("target_public_at") or "?")[:19]
             if video.get("target_public_at")
             else "INMEDIATA")
        ),
    )

    # ── v24: Secondary retry-limit guard (belt-and-suspenders with SQL exclusion) ──
    retry_count = 0
    try:
        with db._connect() as _conn:
            retry_count = _conn.execute(
                "SELECT COUNT(*) FROM generation_jobs "
                "WHERE video_id = ? AND action = 'upload_only' AND status = 'failed'",
                (video_id,),
            ).fetchone()[0]
    except Exception:
        pass
    if retry_count >= MAX_UPLOAD_RETRY_PER_VIDEO:
        logger.warning(
            "Video %d: exceeded max upload retries (%d/%d) — marking as error",
            video_id, retry_count, MAX_UPLOAD_RETRY_PER_VIDEO,
        )
        db.update_video(video_id, status="error",
                         progress_phase="upload",
                         progress=0)
        return None

    # ── 5. Create upload job and dispatch ──
    import asyncio
    from api.services.generation_service import start_upload_job_from_scheduler

    job_id = db.create_job(channel_id, "upload_only", video_id)
    db.update_job(job_id, status="running")

    # Update video status and clear scheduled_upload_at after dispatch
    db.update_video(
        video_id,
        status="uploading",
        progress=5,
        progress_phase="upload",
        scheduled_upload_at=None,
    )

    # ── Log to dedicated scheduled_publish log ──
    try:
        from api.services.scheduled_publish_logger import log_publish_event
        log_publish_event(
            event="upload_dispatched",
            slug=slug,
            video_id=video_id,
            target_public_at=str(video.get("target_public_at", "") or "INMEDIATA")[:19],
            job_id=job_id,
        )
    except Exception:
        pass

    try:
        if loop is None:
            loop = asyncio.get_running_loop()
        asyncio.run_coroutine_threadsafe(
            start_upload_job_from_scheduler(
                job_id=job_id,
                video_id=video_id,
                channel_id=channel_id,
            ),
            loop,
        )
    except Exception as e:
        logger.error(
            "Failed to schedule upload task for job %d (video %d): %s — cleaning up",
            job_id, video_id, e,
        )
        db.update_job(job_id, status="failed",
                       error_msg=f"Upload scheduling failed: {e}"[:500])
        db.update_video(video_id, status="awaiting_upload",
                         progress_phase="upload",
                         scheduled_upload_at=None)

    return {
        "video_id": video_id,
        "job_id": job_id,
        "channel_slug": slug,
        "target_public_at": video.get("target_public_at"),
    }


def get_active_upload_count(db=None) -> int:
    """Count currently running upload_only jobs."""
    if db is None:
        from database.db_extended import ExtendedDatabase

        db = ExtendedDatabase()
    return db.count_active_upload_jobs()


def verify_no_overlaps(db=None, auto_fix: bool = True) -> dict:
    """Post-upload safety net: scan all channels for overlapping target_public_at.

    Called periodically (e.g., every 15 min) from the scheduler loop.
    If overlaps are found and auto_fix=True, automatically redistributes
    videos with 3h gaps (updates DB only — YouTube API requires auth).

    Returns:
        {"fixed": int, "remaining": int, "errors": int}
    """
    if db is None:
        from database.db_extended import ExtendedDatabase
        db = ExtendedDatabase()

    total_fixed = 0
    total_remaining = 0
    total_errors = 0

    try:
        with db._connect() as conn:
            overlap_groups = conn.execute("""
                SELECT v.channel_id, c.slug, v.target_public_at, COUNT(*) as cnt,
                       GROUP_CONCAT(v.id) as ids
                FROM videos v
                JOIN channels c ON c.id = v.channel_id
                WHERE v.status IN ('uploaded_private', 'uploading', 'warming', 'scheduled')
                  AND v.target_public_at IS NOT NULL
                GROUP BY v.channel_id, v.target_public_at
                HAVING cnt > 1
                ORDER BY v.channel_id, v.target_public_at
            """).fetchall()

        if not overlap_groups:
            return {"fixed": 0, "remaining": 0, "errors": 0}

        for group in overlap_groups:
            slug = group["slug"]
            tpa = group["target_public_at"]
            ids_str = group["ids"]
            video_ids = [int(x) for x in ids_str.split(",")]
            count = group["cnt"]

            logger.warning(
                "⚠️ [%s] Post-upload overlap detected: %d videos at %s [%s]",
                slug, count, str(tpa)[:19] if tpa else "?", ids_str,
            )

            if not auto_fix or len(video_ids) < 2:
                total_remaining += count
                continue

            # Auto-fix: redistribute with 3h gaps
            from datetime import timedelta as _td
            base_tpa = tpa[:19] if tpa else None
            if not base_tpa:
                total_remaining += count
                continue

            try:
                base_dt = datetime.strptime(base_tpa, "%Y-%m-%dT%H:%M:%S")
            except Exception:
                try:
                    base_dt = datetime.strptime(base_tpa, "%Y-%m-%d %H:%M:%S")
                except Exception:
                    total_remaining += count
                    continue

            for i, vid in enumerate(video_ids):
                if i == 0:
                    continue  # Keep first at original time
                new_dt = base_dt + _td(hours=i * 3)
                new_target = new_dt.strftime("%Y-%m-%dT%H:%M:%S+00:00")
                try:
                    db.update_video(vid, target_public_at=new_target)
                    with db._connect() as conn:
                        conn.execute(
                            "UPDATE planned_slots SET target_public_at=? "
                            "WHERE video_id=? AND target_public_at=?",
                            (new_target, vid, tpa))
                        conn.execute(
                            "UPDATE video_lifecycle_actions SET scheduled_for=? "
                            "WHERE video_id=? AND scheduled_for=?",
                            (new_target, vid, tpa))
                        conn.commit()
                    total_fixed += 1
                    logger.info(
                        "✅ [%s] Auto-fixed video #%d: %s → %s",
                        slug, vid, str(tpa)[:19], str(new_target)[:19],
                    )
                except Exception as e:
                    logger.error("❌ [%s] Failed to fix video #%d: %s", slug, vid, e)
                    total_errors += 1

    except Exception as e:
        logger.error("verify_no_overlaps failed: %s", e)
        total_errors += 1

    if total_fixed > 0 or total_remaining > 0:
        logger.info(
            "🔍 Overlap scan complete: %d fixed, %d remaining, %d errors",
            total_fixed, total_remaining, total_errors,
        )

    return {"fixed": total_fixed, "remaining": total_remaining, "errors": total_errors}
