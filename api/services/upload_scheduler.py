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

    **v24 (Aug 2026): Guards against duplicate re-upload.**
    - Videos with yt_video_id already set are NEVER reverted to awaiting_upload.
      The upload succeeded — only the job tracking failed. Mark them as uploaded.
    - Both Scenario A and B now filter `AND v.yt_video_id IS NULL`.

    Returns count of recovered videos.
    """
    recovered = 0
    try:
        with db._connect() as conn:
            # ── Scenario A: videos stuck in 'uploading' with failed upload job ──
            # ONLY recover videos that have NO yt_video_id (upload didn't actually complete).
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
                     AND (v.yt_video_id IS NULL OR v.yt_video_id = '')
                """
            ).fetchall()

            for row in stuck:
                video_id = row["id"]
                conn.execute(
                    "UPDATE videos SET status='awaiting_upload', "
                    "progress_phase='upload', scheduled_upload_at=NULL "
                    "WHERE id=? AND status='uploading' AND (yt_video_id IS NULL OR yt_video_id = '')",
                    (video_id,)
                )
                if conn.total_changes > 0:
                    recovered += 1
                    logger.warning(
                        "🔁 Recovery: video #%d (ch=%d) stuck in 'uploading' with dead "
                        "upload job → reverted to 'awaiting_upload' for retry",
                        video_id, row["channel_id"],
                    )

            # ── Scenario A2: videos in 'uploading' that DO have yt_video_id ──
            # The upload actually succeeded but the job tracking died.
            # Mark them as uploaded — DO NOT re-upload.
            stuck_with_yt = conn.execute(
                """SELECT v.id, v.channel_id, v.yt_video_id, v.publish_mode
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
                     AND v.yt_video_id IS NOT NULL
                     AND v.yt_video_id != ''
                """
            ).fetchall()

            for row in stuck_with_yt:
                video_id = row["id"]
                target_status = "uploaded_private" if row["publish_mode"] == "scheduled" else "uploaded"
                yt_url = f"https://youtube.com/watch?v={row['yt_video_id']}"
                conn.execute(
                    "UPDATE videos SET status=?, progress=100, "
                    "progress_phase='upload', yt_url=? "
                    "WHERE id=? AND status='uploading'",
                    (target_status, yt_url, video_id),
                )
                if conn.total_changes > 0:
                    recovered += 1
                    logger.warning(
                        "🔁 Recovery: video #%d (ch=%d) stuck in 'uploading' BUT already "
                        "uploaded (yt=%s) → marked as '%s' (NOT re-uploaded)",
                        video_id, row["channel_id"], row["yt_video_id"], target_status,
                    )

            # ── Scenario B: videos stuck in 'ready' with a failed upload job ──
            # These were generated successfully but the upload dispatch failed
            # (ghost worker, manual cleanup, concurrency guard, etc). Without
            # recovery they sit in 'ready' forever with no planned_slot.
            # v27 (Aug 2026): also match jobs with action='generate_and_upload'
            # that failed AFTER generation (video generated, upload phase died).
            stuck_ready = conn.execute(
                """SELECT v.id, v.channel_id
                   FROM videos v
                   JOIN (
                       SELECT video_id, MAX(id) as max_job_id
                       FROM generation_jobs
                       WHERE action IN ('upload_only', 'generate_and_upload')
                       GROUP BY video_id
                   ) j_latest ON j_latest.video_id = v.id
                   JOIN generation_jobs j ON j.id = j_latest.max_job_id
                   WHERE v.status = 'ready'
                     AND j.status = 'failed'
                     AND (v.yt_video_id IS NULL OR v.yt_video_id = '')
                 """
            ).fetchall()

            for row in stuck_ready:
                video_id = row["id"]
                conn.execute(
                    "UPDATE videos SET status='awaiting_upload', "
                    "progress=100, progress_phase='upload', "
                    "scheduled_upload_at=NULL, error_message=NULL "
                    "WHERE id=? AND status='ready' AND (yt_video_id IS NULL OR yt_video_id = '')",
                    (video_id,)
                )
                if conn.total_changes > 0:
                    recovered += 1
                    logger.warning(
                        "🔁 Recovery: video #%d (ch=%d) stuck in 'ready' with failed "
                        "upload job → reverted to 'awaiting_upload' for retry",
                        video_id, row["channel_id"],
                    )

            # ── Scenario C: videos stuck in 'ready' whose upload never ran ──
            # A reassembly completes the build (sets 'ready', progress_phase='upload')
            # but the subsequent upload step returns without uploading (auth fail,
            # quota, silent error) — or the reassembly job itself finished without
            # a failed upload_only/generate_and_upload job (Scenario B won't match).
            # These sit in 'ready' forever: the upload scheduler only dispatches
            # 'awaiting_upload'. Revert any 'ready' video with no yt_video_id,
            # progress_phase='upload', a present file, and NO active (running/queued)
            # job → awaiting_upload so the scheduler picks it up.
            stuck_ready_no_job = conn.execute(
                """SELECT v.id, v.channel_id
                   FROM videos v
                   WHERE v.status = 'ready'
                     AND (v.yt_video_id IS NULL OR v.yt_video_id = '')
                     AND (v.progress_phase = 'upload' OR v.progress_phase = 'upload_retry')
                     AND v.video_path IS NOT NULL AND v.video_path != ''
                     AND NOT EXISTS (
                         SELECT 1 FROM generation_jobs gj
                         WHERE gj.video_id = v.id
                           AND gj.status IN ('running', 'queued')
                     )
                """
            ).fetchall()

            for row in stuck_ready_no_job:
                video_id = row["id"]
                conn.execute(
                    "UPDATE videos SET status='awaiting_upload', "
                    "progress=100, progress_phase='upload', "
                    "scheduled_upload_at=NULL, error_message=NULL "
                    "WHERE id=? AND status='ready' AND (yt_video_id IS NULL OR yt_video_id = '')",
                    (video_id,)
                )
                if conn.total_changes > 0:
                    recovered += 1
                    logger.warning(
                        "🔁 Recovery: video #%d (ch=%d) stuck in 'ready' with no active "
                        "job → reverted to 'awaiting_upload' for upload",
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


# ── Threshold (hours) beyond which a scheduled_upload_at is considered
# far-future and reset. Upload windows are daily (e.g. 10-13, 20-22), so a
# video should never wait >12h for the next window. Values beyond this mean
# the seed came from a stale/deferred planned slot.
FAR_FUTURE_UPLOAD_HOURS = 12


def _recover_inconsistent_upload_times(db) -> int:
    """Self-heal: fix awaiting_upload videos whose upload/public times are inconsistent.

    Fixes two symptoms observed in production:
      1. target_public_at stale (in the past, or within warmup) relative to now —
         the video would upload with a publishAt already passed → "publish before upload".
      2. scheduled_upload_at far in the future (>FAR_FUTURE_UPLOAD_HOURS) — seeded
         from a deferred planned slot, making a ready video wait days → "upload too far".

    For (1): recalculates target_public_at to the next peak slot.
    For (2): resets scheduled_upload_at to NULL so dispatch_due_uploads computes a
             fresh time in the next available upload window.

    Returns the number of videos fixed.
    """
    fixed = 0
    try:
        with db._connect() as conn:
            rows = conn.execute(
                """SELECT v.id, v.channel_id, v.scheduled_upload_at, v.target_public_at,
                          c.slug, c.config_json
                   FROM videos v
                   JOIN channels c ON c.id = v.channel_id
                   WHERE v.status = 'awaiting_upload'
                     AND v.video_path IS NOT NULL AND v.video_path != ''
                     AND (v.yt_video_id IS NULL OR v.yt_video_id = '')
                """
            ).fetchall()
    except Exception as exc:
        logger.debug("Consistency scan skipped: %s", exc)
        return 0

    from pipeline.publish_scheduler import (
        _target_is_stale, ensure_future_target_public_at,
    )

    now = datetime.now()
    for row in rows:
        video_id = row["id"]
        channel_id = row["channel_id"]
        slug = row["slug"]
        sched_val = row["scheduled_upload_at"]
        tpa = row["target_public_at"]

        cfg = {}
        try:
            cfg = json.loads(row["config_json"] or "{}")
        except (json.JSONDecodeError, TypeError):
            pass
        tz_str = cfg.get("PUBLISH_TIMEZONE", "Europe/Madrid")
        warmup = int(cfg.get("PUBLISH_WARMUP_MIN", 120)) or 120

        changed = False

        # ── (1) stale target_public_at → recalc ──
        tpa_stale = _target_is_stale(tpa, timezone_str=tz_str, warmup_min=warmup)

        # ── (2) far-future scheduled_upload_at → reset ──
        # Quota-aware (ago 2026): los uploads de batch (publish_mode scheduled
        # con scheduled_upload_at planificado) son intencionados — el video se
        # genera con lead de días y se sube en el batch del día de publicación.
        # No se resetean aunque superen FAR_FUTURE_UPLOAD_HOURS.
        is_batch_scheduled = False
        try:
            pub_mode = (row["publish_mode"] if "publish_mode" in row.keys() else "") or ""
            is_batch_scheduled = bool(sched_val) and str(pub_mode).lower() == "scheduled"
        except Exception:
            pass
        sched_far_future = False
        if sched_val and not is_batch_scheduled:
            try:
                sched_dt = datetime.strptime(str(sched_val)[:19], "%Y-%m-%d %H:%M:%S")
                if sched_dt > now + timedelta(hours=FAR_FUTURE_UPLOAD_HOURS):
                    sched_far_future = True
            except (ValueError, TypeError):
                pass

        if not tpa_stale and not sched_far_future:
            continue

        # ── Recalc target_public_at whenever either condition holds ──
        # If scheduled_upload_at is being reset to NULL, the video will upload
        # ASAP in the next window, so its publish time must be recomputed to a
        # future peak (>= upload + warmup). Recalculating here keeps both fields
        # consistent in a single pass.
        new_tpa = None
        try:
            new_tpa = ensure_future_target_public_at(
                tpa, slug=slug, timezone_str=tz_str,
                db=db, channel_id=channel_id,
                warmup_min=warmup, jitter_min=0,
            )
            db.update_video(video_id, target_public_at=new_tpa)
            with db._connect() as conn:
                conn.execute(
                    "UPDATE planned_slots SET target_public_at = ? WHERE video_id = ?",
                    (new_tpa, video_id),
                )
                conn.execute(
                    "UPDATE video_lifecycle_actions SET scheduled_for = ? "
                    "WHERE video_id = ? AND action_type = 'go_public'",
                    (new_tpa, video_id),
                )
                conn.commit()
            logger.warning(
                "🔁 Consistency: video #%d (%s) target_public_at %s → %s",
                video_id, slug, str(tpa)[:19] if tpa else "None", new_tpa[:19],
            )
            changed = True
        except Exception as exc:
            logger.debug("[%s] target_public_at recalc failed: %s", slug, exc)

        # ── Reset far-future scheduled_upload_at so upload happens ASAP ──
        # NOTE: db.update_video() ignores None values, so we write NULL via raw SQL.
        if sched_far_future:
            with db._connect() as conn:
                conn.execute(
                    "UPDATE videos SET scheduled_upload_at = NULL WHERE id = ?",
                    (video_id,),
                )
                conn.commit()
            logger.warning(
                "🔁 Consistency: video #%d (%s) scheduled_upload_at %s is >%dh ahead → reset",
                video_id, slug, str(sched_val)[:19], FAR_FUTURE_UPLOAD_HOURS,
            )
            changed = True

        if changed:
            fixed += 1

    return fixed


def _spawn_upload_worker(job_id: int, channel_id: int, video_id: int) -> bool:
    """Fase 1.3: lanza la subida como subproceso independiente (sobrevive reinicios).

    Antes la subida corría in-process (ThreadPoolExecutor en start_upload_job_from_scheduler)
    y moría en cada reinicio del API → 77 fallos 'Server restarted' en 4 días.
    Ahora usa full_pipeline_worker.py --action upload_only como proceso con
    start_new_session=True, igual que las generaciones.
    """
    import subprocess
    import sys
    from pathlib import Path
    from config import settings

    worker = Path(__file__).parent / "full_pipeline_worker.py"
    log_path = settings.LOGS_DIR / f"worker_{job_id}.log"
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        cmd = [
            sys.executable, str(worker),
            "--job-id", str(job_id),
            "--channel-id", str(channel_id),
            "--video-id", str(video_id),
            "--action", "upload_only",
        ]
        subprocess.Popen(
            cmd,
            start_new_session=True,
            stdout=open(log_path, "w"),
            stderr=subprocess.STDOUT,
        )
        logger.info("📤 Upload worker spawned: job=%d video=%d (subprocess)", job_id, video_id)
        return True
    except Exception as e:
        logger.error("Failed to spawn upload worker for job %d: %s", job_id, e)
        return False


def _minutes_since_last_upload(db, channel_id: int) -> float | None:
    """Minutes since the channel's last upload, or None if never uploaded.

    Computed in SQLite (julianday, UTC) to avoid local/UTC ambiguity: both
    uploaded_at (CURRENT_TIMESTAMP) and 'now' are UTC inside SQLite.
    """
    try:
        with db._connect() as conn:
            row = conn.execute(
                "SELECT (julianday('now') - julianday(MAX(uploaded_at))) * 1440.0 AS mins_ago "
                "FROM videos WHERE channel_id = ? "
                "AND yt_video_id IS NOT NULL AND yt_video_id != '' "
                "AND uploaded_at IS NOT NULL",
                (channel_id,),
            ).fetchone()
        if row and row["mins_ago"] is not None:
            return float(row["mins_ago"])
    except Exception:
        pass
    return None


def _apply_same_channel_gap(db, ch_cfg: dict, channel_id: int,
                            candidate: datetime, now: datetime) -> datetime:
    """Push a candidate upload time forward to respect the same-channel minimum gap.

    Reads MIN_SAME_CHANNEL_UPLOAD_GAP_HOURS from channel config (default 3h).
    The gap is a duration, so it is timezone-agnostic: the earliest allowed time
    is now + (gap - minutes_since_last_upload). Returns candidate unchanged if
    no gap applies or it is already beyond the earliest allowed time.
    """
    try:
        gap_hours = int(ch_cfg.get("MIN_SAME_CHANNEL_UPLOAD_GAP_HOURS", 3) or 3)
    except (TypeError, ValueError):
        gap_hours = 3
    if gap_hours <= 0:
        return candidate
    mins_ago = _minutes_since_last_upload(db, channel_id)
    if mins_ago is None:
        return candidate
    remaining_min = gap_hours * 60 - mins_ago
    if remaining_min <= 0:
        return candidate
    earliest = now + timedelta(minutes=remaining_min)
    if candidate >= earliest:
        return candidate
    return earliest


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

    from config.settings import YT_REMEDIATION_MODE
    if YT_REMEDIATION_MODE:
        logger.warning("📤 Upload scheduler: remediation mode active — uploads require preflight approval")
        return None

    # ── 0. YouTube quota circuit breaker ─────────────────────
    # Project-level reservation admission in YouTubeUploadDispatcher is the
    # quota source of truth.  This global breaker only blocks when ALL
    # channels' projects are exhausted (a single project's 403 no longer
    # freezes uploads of the other project's channels).
    try:
        if db.all_channels_quota_exhausted():
            logger.info("📤 Upload scheduler: all channel projects quota-exhausted — uploads paused")
            return None
    except Exception as exc:
        logger.warning("Upload quota breaker check failed; blocking dispatch fail-closed: %s", exc)
        return None

    # ── 0a. Anti ping-pong: retener subidas en los últimos 30 min del día-PT ──
    # La cuota del día anterior está agotada casi con seguridad; intentar subir
    # ahí provoca 403 → breaker → clear → reintento → 403… Sin fijar breaker.
    try:
        from api.services.quota_tracker import in_pt_day_end_window
        if in_pt_day_end_window():
            logger.info("📤 Upload scheduler: end-of-PT-day window — uploads held until reset")
            return None
    except Exception:
        pass

    # ── 0. Recovery scan: revert stuck 'uploading' videos whose job died ──
    # This catches videos that got stuck due to server restart / worker crash
    # where the startup recovery didn't revert them (e.g. race condition, missed tick).
    _recover_stuck_uploading_videos(db)

    # ── 0b. Consistency scan: fix stale target_public_at / far-future scheduled_upload_at ──
    # Self-heals the "publish before upload" and "upload too far" inconsistencies.
    _recover_inconsistent_upload_times(db)

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
                       v.scheduled_upload_at, v.publish_mode, c.slug as channel_slug, c.config_json
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
                with db._connect() as _conn:
                    retry_count = _conn.execute(
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

        current_hour = now.hour
        in_window = _is_in_any_window(current_hour, windows)

        # ── Quota-aware (ago 2026): subidas de batch planificadas ──
        # Los videos scheduled con scheduled_upload_at pre-fijado (batch por
        # cuenta desde la planificación) se suben a esa hora, sin pasar por la
        # ventana UPLOAD_WINDOWS (la hora de batch es la ventana autoritativa).
        is_batch_scheduled = False
        if sched_at_val is not None:
            try:
                pub_mode = (row["publish_mode"] if "publish_mode" in row.keys() else "") or ""
                if str(pub_mode).lower() == "scheduled":
                    is_batch_scheduled = True
            except Exception:
                pass

        # ── v25 (Aug 2026): reprogram overdue scheduled_upload_at instead of ASAP ──
        # A backlog of past-due videos must drain through future upload windows,
        # NOT be dumped back-to-back. If scheduled_upload_at is overdue AND we are
        # outside a window, recompute a future window time (respecting the
        # same-channel gap) and wait. target_public_at staleness is handled
        # separately by _recover_inconsistent_upload_times + the pre-dispatch recalc.
        # Los uploads de batch NO se reprograman (la hora planificada es sagrada).
        if sched_at_val is not None and not in_window and not is_batch_scheduled:
            try:
                sched_dt = datetime.strptime(str(sched_at_val)[:19], "%Y-%m-%d %H:%M:%S")
            except (ValueError, TypeError):
                sched_dt = None
            if sched_dt is not None and sched_dt < now:
                new_time = _compute_random_upload_time(windows, now, channel_id)
                if new_time is None:
                    logger.debug("Video %d: overdue but no window available — skipping", video_id)
                    continue
                new_time = _apply_same_channel_gap(db, ch_cfg, channel_id, new_time, now)
                try:
                    db.update_video(
                        video_id,
                        scheduled_upload_at=new_time.strftime('%Y-%m-%d %H:%M:%S'),
                    )
                except Exception:
                    pass
                logger.info(
                    "🔁 Video %d: overdue (%s) outside window → reprogrammed to %s",
                    video_id, str(sched_at_val)[:19], new_time.strftime('%Y-%m-%d %H:%M'),
                )
                continue

        # Window gate: los uploads de batch (planificados) no dependen de la
        # ventana UPLOAD_WINDOWS — la hora de batch es la ventana autoritativa.
        if in_window or is_batch_scheduled:
            eligible.append({
                "row": dict(row),
                "past_due": False,
                "is_batch_scheduled": is_batch_scheduled,
            })

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
                         AND status IN ('published', 'uploaded_private', 'uploaded')
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

        # ── v25: same-channel upload gap (default 3h) ──
        # Prevents a backlog from uploading back-to-back for the same channel.
        # Quota-aware (ago 2026): los uploads de batch planificados se EXCLUYEN
        # del gap — dentro de un batch un canal puede subir varios videos con
        # jitter de minutos (sensación manual confirmada).
        if not entry.get("is_batch_scheduled"):
            sel_cfg = {}
            try:
                sel_cfg = json.loads(entry["row"].get("config_json") or "{}")
            except (json.JSONDecodeError, TypeError):
                pass
            try:
                gap_hours = int(sel_cfg.get("MIN_SAME_CHANNEL_UPLOAD_GAP_HOURS", 3) or 3)
            except (TypeError, ValueError):
                gap_hours = 3
            if gap_hours > 0:
                mins_ago = _minutes_since_last_upload(db, ch_id)
                if mins_ago is not None and mins_ago < gap_hours * 60:
                    slug = entry["row"].get("channel_slug", "?")
                    logger.info(
                        "📤 Upload skipped for %s: same-channel gap (%dh) not elapsed "
                        "(%.0f min since last upload)",
                        slug, gap_hours, mins_ago,
                    )
                    continue

        selected = entry
        break

    if not selected:
        logger.debug("📤 Upload scheduler: no eligible video (daily quota or same-channel gap)")
        return None

    entry = selected
    video = entry["row"]
    video_id = video["id"]
    channel_id = video["channel_id"]
    slug = video.get("channel_slug", video.get("canal", "unknown"))

    # Late gate: quota can be consumed between candidate selection and dispatch.
    # Per-channel: solo bloquea si el PROYECTO de este canal está agotado.
    try:
        if db.is_quota_exhausted_for_channel(slug):
            logger.info("📤 Upload scheduler: project quota exhausted for %s — skipping", slug)
            return None
    except Exception as exc:
        logger.warning("Upload quota breaker recheck failed; blocking dispatch fail-closed: %s", exc)
        return None

    # ── v24 (Aug 2026): Pre-dispatch yt_video_id guard ──
    # If the video already has a YouTube ID, the upload already succeeded.
    # Fix the status rather than re-uploading.
    existing_yt = video.get("yt_video_id") if isinstance(video, dict) else None
    if existing_yt and str(existing_yt).strip():
        logger.warning(
            "📤 Upload skip for video #%d (%s): already uploaded (yt=%s) — correcting status",
            video_id, slug, existing_yt,
        )
        pub_mode = video.get("publish_mode", "immediate")
        corrected_status = "uploaded_private" if pub_mode == "scheduled" else "uploaded"
        db.update_video(video_id, status=corrected_status, progress=100,
                        progress_phase="upload")
        return None

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
        # Use the SELECTED video's config (not the last loop iteration's config).
        sel_cfg2 = {}
        try:
            sel_cfg2 = json.loads(video.get("config_json") or "{}")
        except (json.JSONDecodeError, TypeError):
            pass
        ch_cfg_raw = sel_cfg2.get("PUBLISH_TIMEZONE", "Europe/Madrid")
        ch_cfg_spread = sel_cfg2.get("PUBLISH_WINDOW_SPREAD_MIN", 90)
        ch_cfg_warmup = int(sel_cfg2.get("PUBLISH_WARMUP_MIN", 60) or 60)
        if _target_is_stale(effective_target, timezone_str=ch_cfg_raw, warmup_min=ch_cfg_warmup):
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
                warmup_min=ch_cfg_warmup,
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

    # ── 5. Create upload job and dispatch (Fase 1.3: subproceso independiente) ──
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

    # ── Fase 1.3: spawn subproceso independiente (sobrevive reinicios) ──
    if not _spawn_upload_worker(job_id, channel_id, video_id):
        logger.error(
            "Failed to spawn upload worker for job %d (video %d) — cleaning up",
            job_id, video_id,
        )
        db.update_job(job_id, status="failed",
                       error_msg="Failed to spawn upload worker")
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
