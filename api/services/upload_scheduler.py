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
from datetime import date, datetime, timedelta
from pathlib import Path

logger = logging.getLogger("autotube.upload_scheduler")

MAX_CONCURRENT_UPLOADS = 1  # One upload at a time (avoids YouTube rate limits)

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


def dispatch_due_uploads(db=None) -> dict | None:
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
               ORDER BY v.scheduled_upload_at ASC, v.created_at ASC
               LIMIT 20"""
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
                pub_dt = datetime.strptime(str(target_public), "%Y-%m-%d %H:%M:%S")
                if pub_dt < now:
                    past_due = True
                    logger.info(
                        "Video %d: public time already passed — uploading ASAP", video_id
                    )
            except (ValueError, TypeError):
                try:
                    pub_dt = datetime.fromisoformat(
                        str(target_public).replace("Z", "+00:00")
                    )
                    if pub_dt < now:
                        past_due = True
                        logger.info(
                            "Video %d: public time already passed — uploading ASAP",
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

    # ── 4. Dispatch the first eligible video ──
    entry = eligible[0]
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

    logger.info(
        "📤 Despachando subida: video #%d (%s), archivo=%s | público programado: %s",
        video_id, slug, vp.name,
        (
            (str(video.get("target_public_at") or "?")[:19]
             if video.get("target_public_at")
             else "INMEDIATA")
        ),
    )

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

    asyncio.create_task(
        start_upload_job_from_scheduler(
            job_id=job_id,
            video_id=video_id,
            channel_id=channel_id,
        )
    )

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
