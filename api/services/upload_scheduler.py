"""Upload Scheduler — Phase 2 of the 3-phase pipeline.

Dispatches upload jobs for videos that have been generated locally (F1)
and are awaiting upload (F2). Uploads happen within each channel's
configured upload window (UPLOAD_WINDOW_START..END).

Uploads are network-bound and can run in parallel with a generation job
(they don't contend for ffmpeg/CPU). Uses a separate concurrency guard
to allow 1 upload + 1 generation simultaneously.

Architecture:
  dispatch_due_uploads(db) → checks for awaiting_upload videos,
  dispatches upload_only jobs within window, respects per-channel 
  concurrency and global upload limit.

Called every 5 min by the checker loop in api/main.py.
"""

import logging
from datetime import date, datetime, timedelta
from pathlib import Path

logger = logging.getLogger("autotube.upload_scheduler")

MAX_CONCURRENT_UPLOADS = 1  # One upload at a time (avoids YouTube rate limits)


def dispatch_due_uploads(db=None) -> dict | None:
    """Check for awaiting_upload videos and dispatch upload jobs.

    A video is ready for upload when:
    1. Status is 'awaiting_upload'
    2. Current time is within its channel's upload window (configurable)
    3. Its file still exists locally
    4. No other upload is in progress (MAX_CONCURRENT_UPLOADS)

    Returns:
        dict with dispatched info, or None if nothing to upload.
    """
    if db is None:
        from database.db_extended import ExtendedDatabase
        db = ExtendedDatabase()

    # ── 1. Count active upload jobs ──
    active_uploads = db.count_active_upload_jobs()
    if active_uploads >= MAX_CONCURRENT_UPLOADS:
        logger.info("📤 Upload scheduler: %d upload(s) activos (max=%d) — no se despachan más",
                    active_uploads, MAX_CONCURRENT_UPLOADS)
        return None

    # ── 2. Find videos awaiting upload ──
    now = datetime.now()
    current_hour = now.hour

    with db._connect() as conn:
        rows = conn.execute(
            """SELECT v.id, v.channel_id, v.canal, v.video_path, v.thumbnail_path,
                      v.titulo_final, v.description, v.tags_json, v.target_public_at,
                      c.slug as channel_slug, c.config_json
               FROM videos v
               JOIN channels c ON v.channel_id = c.id
               WHERE v.status = 'awaiting_upload'
                 AND v.video_path IS NOT NULL
                 AND v.video_path != ''
               ORDER BY v.created_at ASC
               LIMIT 20"""
        ).fetchall()

    if not rows:
        return None

    # ── 3. Filter by channel upload window ──
    import json
    eligible = []
    for row in rows:
        ch_cfg = {}
        try:
            ch_cfg = json.loads(row["config_json"] or "{}")
        except (json.JSONDecodeError, TypeError):
            pass

        win_start = ch_cfg.get("UPLOAD_WINDOW_START", 9)
        win_end = ch_cfg.get("UPLOAD_WINDOW_END", 11)

        # Determine if this video is past-due (target_public_at already passed)
        past_due = False
        target_public = row["target_public_at"]
        if target_public:
            try:
                pub_dt = datetime.strptime(str(target_public), "%Y-%m-%d %H:%M:%S")
                if pub_dt < now:
                    past_due = True
                    logger.info("Video %d: public time already passed — uploading ASAP", row["id"])
            except (ValueError, TypeError):
                # Try ISO format too
                try:
                    pub_dt = datetime.fromisoformat(str(target_public).replace("Z", "+00:00"))
                    if pub_dt < now:
                        past_due = True
                        logger.info("Video %d: public time already passed — uploading ASAP", row["id"])
                except (ValueError, TypeError):
                    pass

        # Past-due videos: upload regardless of window (catch-up)
        # Normal videos: only upload within the configured window
        if past_due or (win_start <= current_hour < win_end):
            eligible.append(dict(row))

    if not eligible:
        # ── Periodic heartbeat: log only once every ~15 min when idle ──
        import time as _t
        if not hasattr(dispatch_due_uploads, "_last_noop_log") or \
           _t.time() - dispatch_due_uploads._last_noop_log > 900:
            logger.info("📤 Upload scheduler: 0 vídeos en ventana (%d-%dh, %d esperando total)",
                       win_start, win_end, len(rows))
            dispatch_due_uploads._last_noop_log = _t.time()
        return None

    # ── Sort: past-due videos first, then normal order ──
    now_ts = now
    def _is_past_due(vid: dict) -> bool:
        tp = vid.get("target_public_at")
        if not tp:
            return False
        try:
            return datetime.strptime(str(tp), "%Y-%m-%d %H:%M:%S") < now_ts
        except (ValueError, TypeError):
            try:
                return datetime.fromisoformat(str(tp).replace("Z", "+00:00")) < now_ts
            except (ValueError, TypeError):
                return False
    eligible.sort(key=lambda v: (not _is_past_due(v), v.get("created_at", "")))

    # ── 4. Dispatch the first eligible video ──
    video = eligible[0]
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
        logger.warning("Video %d: file missing (%s) — marking as error", video_id, video.get("video_path"))
        db.update_video(video_id, status="error", progress_phase="upload")
        return None

    logger.info("📤 Despachando subida: video #%d (%s), archivo=%s | público programado: %s",
                video_id, slug, vp.name,
                (str(video.get("target_public_at") or "?")[:19] if video.get("target_public_at") else "INMEDIATA"))
    
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

    # ── 5. Create upload job and dispatch ──
    import asyncio
    from api.services.generation_service import start_upload_job_from_scheduler

    job_id = db.create_job(channel_id, "upload_only", video_id)
    db.update_job(job_id, status="running")

    # Update video status to show it's being uploaded
    db.update_video(video_id, status="uploading", progress=5, progress_phase="upload")

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
