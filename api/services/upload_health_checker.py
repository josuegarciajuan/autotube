"""Upload Health Checker — Post-upload YouTube processing monitoring.

Monitors YouTube's processingStatus for recently uploaded videos
at scheduled intervals (5min, 30min, 2h) to detect encoding failures
that happen AFTER the initial upload verification succeeds.

If processing fails, auto-retries the upload (max 2 attempts).
Requires the video file to still exist locally and pass checksum verification.

Architecture:
    schedule_checks(video_id, yt_video_id, channel_slug)
        → Called by orchestrator.phase_upload() after successful upload
        → Inserts 3 rows into upload_health_checks table (5min, 30min, 2h)

    process_due_checks()
        → Called every 5 min by the checker loop in api/main.py
        → Queries pending checks (check_at <= now, status='pending')
        → Verifies each via YouTube API
        → If processing failed → auto_retry_upload()

    auto_retry_upload(video_id, yt_video_id, channel_slug)
        → Verifies local file integrity (checksum)
        → Re-uploads via YouTubeUploader
        → Updates DB with new yt_video_id
        → Schedules new health checks for the re-upload
"""

import hashlib
import logging
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

logger = logging.getLogger("autotube.health_checker")

# Check intervals (minutes after upload)
CHECK_INTERVALS = [5, 30, 120]  # 5min, 30min, 2 hours

# Maximum auto-retry attempts per video
MAX_RETRY_ATTEMPTS = 3  # initial + 2 retries


def schedule_checks(video_id: int, yt_video_id: str, channel_slug: str,
                    db: Optional["ExtendedDatabase"] = None):
    """Schedule post-upload health checks for a newly uploaded video.

    Inserts rows into upload_health_checks for each check interval.
    Called from orchestrator.phase_upload() after successful upload.

    Args:
        video_id: DB videos.id
        yt_video_id: YouTube video ID
        channel_slug: Channel slug for authentication
        db: Optional DB reference (creates one if None)
    """
    if db is None:
        from database.db_extended import ExtendedDatabase
        db = ExtendedDatabase()

    now_utc = datetime.now(timezone.utc)

    try:
        # Count existing attempts for this video
        attempt = count_attempts(video_id, db) + 1
        if attempt > MAX_RETRY_ATTEMPTS:
            logger.warning(
                "[health_check] Video #%d exceeded max retry attempts (%d/%d) — skipping",
                video_id, attempt - 1, MAX_RETRY_ATTEMPTS,
            )
            return

        with db._connect() as conn:
            for interval_min in CHECK_INTERVALS:
                check_at = now_utc + timedelta(minutes=interval_min)
                conn.execute(
                    """INSERT INTO upload_health_checks 
                       (video_id, yt_video_id, channel_slug, check_at, 
                        attempt, status)
                       VALUES (?, ?, ?, ?, ?, 'pending')""",
                    (video_id, yt_video_id, channel_slug,
                     check_at.isoformat(), attempt),
                )
            conn.commit()

        logger.info(
            "[health_check] Scheduled %d checks for video #%d (yt=%s, attempt=%d/%d)",
            len(CHECK_INTERVALS), video_id, yt_video_id,
            attempt, MAX_RETRY_ATTEMPTS,
        )
    except Exception as exc:
        logger.error("[health_check] Failed to schedule checks for video #%d: %s",
                     video_id, exc)


def count_attempts(video_id: int, db) -> int:
    """Count how many health check attempts exist for this video."""
    try:
        with db._connect() as conn:
            row = conn.execute(
                "SELECT COALESCE(MAX(attempt), 0) FROM upload_health_checks WHERE video_id = ?",
                (video_id,),
            ).fetchone()
            return int(row[0]) if row else 0
    except Exception:
        return 0


def process_due_checks(loop=None):
    """Process all due health checks (check_at <= now, status='pending').

    Called every 5 min by the checker loop in api/main.py.

    Args:
        loop: asyncio event loop (for running coroutines in thread)

    Returns:
        dict with {processed, failed_detected, retried, errors}
    """
    from database.db_extended import ExtendedDatabase

    db = ExtendedDatabase()
    stats = {"processed": 0, "failed_detected": 0, "retried": 0, "errors": 0}

    try:
        if db.all_channels_quota_exhausted():
            logger.info("[health_check] all channel projects quota-exhausted — due checks paused")
            return stats
    except Exception:
        pass

    try:
        now_iso = datetime.now(timezone.utc).isoformat()
        with db._connect() as conn:
            rows = conn.execute(
                """SELECT id, video_id, yt_video_id, channel_slug, attempt
                   FROM upload_health_checks
                   WHERE status = 'pending' AND check_at <= ?
                   ORDER BY check_at ASC
                   LIMIT 20""",
                (now_iso,),
            ).fetchall()
    except Exception as exc:
        logger.error("[health_check] Failed to query due checks: %s", exc)
        return stats

    if not rows:
        return stats  # nothing due

    logger.info("[health_check] Processing %d due health check(s)", len(rows))

    for row in rows:
        check_id, video_id, yt_video_id, channel_slug, attempt = row
        try:
            result = _verify_processing(yt_video_id, channel_slug)
            stats["processed"] += 1

            if result.get("processing_failed"):
                stats["failed_detected"] += 1
                _mark_check_result(db, check_id, "failed",
                                   result.get("reason", ""))
                # Try auto-retry
                if attempt <= MAX_RETRY_ATTEMPTS:
                    success = _auto_retry_upload(
                        video_id, yt_video_id, channel_slug, db,
                        result.get("reason", ""),
                    )
                    if success:
                        stats["retried"] += 1
            elif result.get("ok"):
                _mark_check_result(db, check_id, "ok",
                                   f"processingStatus={result.get('processing_status', '?')}")
            else:
                # Still processing or unknown — let next check interval handle it
                _mark_check_result(db, check_id, "processing",
                                   f"status={result.get('processing_status', '?')}")

        except Exception as exc:
            stats["errors"] += 1
            logger.error("[health_check] Error processing check #%d: %s", check_id, exc)
            _mark_check_result(db, check_id, "error", str(exc)[:200])

    return stats


def _verify_processing(yt_video_id: str, channel_slug: str) -> dict:
    """Query YouTube API for a video's processing status.

    Returns dict:
        ok: bool — processing succeeded
        processing_failed: bool — processing failed
        processing_status: str
        processing_failure_reason: str
        reason: str — human-readable
    """
    try:
        from pipeline.youtube_uploader import YouTubeUploader
        from googleapiclient.errors import HttpError

        from database.db_extended import ExtendedDatabase
        _db = ExtendedDatabase()
        if _db.is_quota_exhausted_for_channel(channel_slug):
            return {"ok": False, "processing_failed": False,
                    "processing_status": "quota_exhausted",
                    "reason": "YouTube quota exhausted (project breaker)"}

        uploader = YouTubeUploader(account_name=channel_slug, channel_slug=channel_slug)
        if not uploader.authenticate():
            return {"ok": False, "processing_failed": False,
                    "processing_status": "unknown",
                    "reason": "Auth failed"}

        service = uploader._get_service()
        try:
            resp = service.videos().list(
                part="status",
                id=yt_video_id,
            ).execute()
        except HttpError as exc:
            # readonly=True: un 403 en un videos.list de 1 ud NO debe fijar el
            # breaker de subidas del proyecto (anti ping-pong).
            uploader._raise_if_quota_exceeded(
                exc, "upload_health_checker._verify_processing", readonly=True
            )
            raise

        items = resp.get("items", [])
        if not items:
            return {"ok": False, "processing_failed": False,
                    "processing_status": "deleted",
                    "reason": "Video not found (deleted)"}

        status = items[0].get("status", {})
        processing_status = status.get("processingStatus", "")
        processing_failure = status.get("processingFailureReason", "")
        upload_status = status.get("uploadStatus", "")

        if processing_status == "failed":
            return {
                "ok": False,
                "processing_failed": True,
                "processing_status": "failed",
                "processing_failure_reason": processing_failure,
                "reason": f"Processing failed: {processing_failure or 'unknown'}",
            }
        elif processing_status == "succeeded" or upload_status == "processed":
            return {
                "ok": True,
                "processing_failed": False,
                "processing_status": processing_status or upload_status,
                "reason": "",
            }
        elif processing_status == "suspended":
            return {
                "ok": False,
                "processing_failed": True,
                "processing_status": "suspended",
                "processing_failure_reason": "suspended",
                "reason": "Processing suspended (policy violation?)",
            }
        else:
            # processing, or empty/uploaded — still in progress
            return {
                "ok": False,
                "processing_failed": False,
                "processing_status": processing_status or upload_status,
                "reason": f"Still processing: {processing_status or upload_status}",
            }

    except Exception as exc:
        logger.warning("[health_check] API error for %s: %s", yt_video_id, str(exc)[:150])
        return {"ok": False, "processing_failed": False,
                "processing_status": "api_error",
                "reason": f"API error: {str(exc)[:120]}"}


def _mark_check_result(db, check_id: int, status: str, result: str):
    """Update a health check row with its result."""
    try:
        with db._connect() as conn:
            conn.execute(
                "UPDATE upload_health_checks SET status = ?, result = ? WHERE id = ?",
                (status, result[:500], check_id),
            )
            conn.commit()
    except Exception:
        pass


def _auto_retry_upload(video_id: int, yt_video_id: str, channel_slug: str,
                       db, failure_reason: str) -> bool:
    """Re-upload a video whose YouTube processing failed.

    1. Verify local file exists and checksum is valid
    2. Get video metadata from DB
    3. Upload to YouTube via YouTubeUploader
    4. Update DB with new yt_video_id
    5. Schedule new health checks
    """
    try:
        try:
            if db.is_quota_exhausted_for_channel(channel_slug):
                logger.info("[health_check] Auto-retry skipped for #%d — project quota exhausted", video_id)
                return False
        except Exception:
            pass

        # ── Get video info from DB ──
        video = db.get_video(video_id)
        if not video:
            logger.error("[health_check] Video #%d not found in DB", video_id)
            return False

        video_path_str = video.get("video_path", "")
        if not video_path_str:
            logger.error("[health_check] Video #%d has no video_path", video_id)
            return False

        video_path = Path(video_path_str)
        if not video_path.exists():
            logger.error("[health_check] Video file not found: %s", video_path)
            return False

        # ── Verify file integrity (basic: file exists + has readable content) ──
        file_size = video_path.stat().st_size
        if file_size < 1024:  # less than 1KB = definitely broken
            logger.error("[health_check] Video #%d file too small: %d bytes", video_id, file_size)
            return False

        # Try to open and read first/last KB to verify it's a valid file
        try:
            with open(video_path, "rb") as f:
                f.read(1024)
                f.seek(-1024, 2)
                f.read(1024)
        except Exception as exc:
            logger.error("[health_check] Video #%d file unreadable: %s", video_id, exc)
            return False

        sha = hashlib.sha256()
        with open(video_path, "rb") as f:
            while chunk := f.read(8192):
                sha.update(chunk)
        checksum = sha.hexdigest()
        logger.info("[health_check] Video #%d checksum OK (%s, %d bytes)",
                    video_id, checksum[:12], file_size)

        # ── Get metadata ──
        title = video.get("titulo_final") or "Video sin título"
        description = video.get("description") or ""
        tags_str = video.get("tags_json") or "[]"
        import json
        tags = json.loads(tags_str) if isinstance(tags_str, str) else tags_str
        thumbnail_path_str = video.get("thumbnail_path", "")

        # ── Re-upload ──
        from pipeline.youtube_uploader import YouTubeUploader

        # Get privacy — preserve original privacy status
        privacy = video.get("privacy_status") or "public"

        logger.info(
            "[health_check] 🔄 AUTO-RETRY upload for video #%d (yt=%s, reason=%s)",
            video_id, yt_video_id, failure_reason,
        )

        uploader = YouTubeUploader(channel_slug)
        if not uploader.authenticate():
            logger.error("[health_check] Auth failed for re-upload of video #%d", video_id)
            return False

        result = uploader.upload(
            video_path=video_path,
            title=title,
            description=description,
            tags=tags,
            thumbnail_path=Path(thumbnail_path_str) if thumbnail_path_str else None,
            privacy=privacy,
        )

        new_yt_id = result.get("video_id")
        if not new_yt_id:
            logger.error("[health_check] Re-upload returned no video_id for #%d", video_id)
            return False

        new_url = result.get("url", "")
        logger.info(
            "[health_check] ✅ Re-upload successful: video #%d → %s (old: %s)",
            video_id, new_yt_id, yt_video_id,
        )

        # ── Update DB ──
        # Append old yt_video_id to track history
        old_ids_str = video.get("yt_failed_ids") or ""
        old_ids = []
        if old_ids_str:
            try:
                old_ids = json.loads(old_ids_str)
            except Exception:
                old_ids = []
        old_ids.append(yt_video_id)

        db.update_video(
            video_id,
            yt_video_id=new_yt_id,
            yt_url=new_url,
            status="uploaded_private" if privacy == "private" else "uploaded",
            yt_failed_ids=json.dumps(old_ids),
            # Reset published_verified for the new upload
            published_verified_at=None,
            published_retry_at=None,
            published_retry_count=0,
        )

        # ── Schedule new health checks for the re-upload ──
        schedule_checks(video_id, new_yt_id, channel_slug, db)

        return True

    except Exception as exc:
        logger.error("[health_check] Auto-retry failed for video #%d: %s", video_id, exc)
        return False


def get_health_check_stats(db=None) -> dict:
    """Get summary stats of health check activity.

    Returns dict with total counts by status.
    """
    if db is None:
        from database.db_extended import ExtendedDatabase
        db = ExtendedDatabase()

    try:
        with db._connect() as conn:
            stats = {"pending": 0, "ok": 0, "failed": 0, "processing": 0,
                     "error": 0, "total": 0}
            rows = conn.execute(
                "SELECT status, COUNT(*) as cnt FROM upload_health_checks GROUP BY status"
            ).fetchall()
            for status, cnt in rows:
                if status in stats:
                    stats[status] = cnt
                stats["total"] += cnt
            return stats
    except Exception:
        return {"pending": 0, "ok": 0, "failed": 0, "processing": 0,
                "error": 0, "total": 0}


def cleanup_old_checks(db=None, max_age_days: int = 7):
    """Remove health check entries older than max_age_days.

    Called periodically to keep the table from growing unbounded.
    """
    if db is None:
        from database.db_extended import ExtendedDatabase
        db = ExtendedDatabase()

    try:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=max_age_days)).isoformat()
        with db._connect() as conn:
            result = conn.execute(
                "DELETE FROM upload_health_checks WHERE created_at < ?",
                (cutoff,),
            )
            conn.commit()
            deleted = result.rowcount
            if deleted > 0:
                logger.info("[health_check] Cleaned up %d old checks (>%s days)", deleted, max_age_days)
    except Exception as exc:
        logger.warning("[health_check] Cleanup failed: %s", exc)
