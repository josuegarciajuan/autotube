"""Thumbnail verification service — post-upload scheduler.

v24 (Aug 2026): Periodically checks recently uploaded videos to ensure
their custom thumbnails were successfully applied on YouTube. Videos
without thumbnails are retried using the local thumbnail file.

Integrated into the main checker loop (every 5 minutes).
Batch size: 5 videos per cycle to keep quota usage low (~480 units/day).
"""

import logging
from pathlib import Path
from datetime import datetime, timezone

from api.services.quota_tracker import track_quota

logger = logging.getLogger("autotube.thumbnail_verify")

THUMBNAIL_VERIFY_BATCH = 5  # max videos per cycle
THUMBNAIL_VERIFY_MAX_AGE_DAYS = 3  # only check recent uploads

# ── Quota guard cache: avoid re-querying DB for every video ──
_skipped_channels: dict[str, float] = {}  # channel_slug → last_skip_timestamp
_SKIP_CACHE_TTL = 300  # re-check quota every 5 min per channel


def track_quota_exceeded_for_cycle(channel_slug: str) -> bool:
    """Check if quota is too low for thumbnail verification on this channel.

    Caches result for _SKIP_CACHE_TTL seconds to avoid hammering the DB
    for every video in the batch.
    """
    import time as _time
    now = _time.monotonic()
    if channel_slug in _skipped_channels:
        if now - _skipped_channels[channel_slug] < _SKIP_CACHE_TTL:
            return True  # still skipping this channel
        del _skipped_channels[channel_slug]  # cache expired, re-check

    from api.services.quota_tracker import should_skip_thumbnail_verify
    if should_skip_thumbnail_verify(channel_slug):
        _skipped_channels[channel_slug] = now
        return True
    return False


async def run_thumbnail_verification_cycle(db=None):
    """Check recent uploaded videos for missing YT thumbnails and retry.

    Called from the checker loop every 5 minutes.
    Processes up to THUMBNAIL_VERIFY_BATCH videos per cycle.

    Quota: 2 units per video (thumbnails().list() + set()).
    Max daily: 5 × 12 × 24 = 1,440 units (safe with 10,000 daily quota).
    """
    from config.settings import YT_REMEDIATION_MODE
    if YT_REMEDIATION_MODE:
        return

    if db is None:
        from database.db_extended import ExtendedDatabase
        db = ExtendedDatabase()

    try:
        with db._connect() as conn:
            conn.row_factory = None  # tuples
            rows = conn.execute(
                """SELECT v.id, v.channel_id, v.canal, v.yt_video_id, v.thumbnail_path,
                          v.publish_mode
                   FROM videos v
                   WHERE v.yt_video_id IS NOT NULL AND v.yt_video_id != ''
                     AND v.thumbnail_path IS NOT NULL AND v.thumbnail_path != ''
                     AND (v.thumbnail_verified IS NULL OR v.thumbnail_verified = 0)
                     AND v.status IN ('uploaded', 'uploaded_private', 'published')
                     AND v.created_at >= datetime('now', ?)
                   ORDER BY v.uploaded_at ASC
                   LIMIT ?""",
                (f"-{THUMBNAIL_VERIFY_MAX_AGE_DAYS} days", THUMBNAIL_VERIFY_BATCH),
            ).fetchall()  # noqa: E501
    except Exception as e:
        logger.debug("Thumbnail verify: DB query failed: %s", e)
        return

    if not rows:
        return

    recovered = 0
    for row in rows:
        video_id, channel_id, canal, yt_video_id, thumbnail_path, publish_mode = row

        # ── Quota guard: skip if channel >50% consumed ──────────────
        if track_quota_exceeded_for_cycle(canal):
            logger.debug(
                "Thumbnail verify: skipping video #%d (%s) — quota >50%% for %s",
                video_id, yt_video_id, canal,
            )
            continue

        # Verify local file exists
        if not thumbnail_path or not Path(thumbnail_path).exists():
            logger.debug(
                "Thumbnail verify: video #%d has no local file (%s) — skipping",
                video_id, thumbnail_path,
            )
            try:
                db.update_video(video_id, thumbnail_verified=0)
            except Exception:
                pass
            continue

        # Authenticate YouTube for this channel
        try:
            from pipeline.youtube_uploader import YouTubeUploader
            uploader = YouTubeUploader(canal)
            if not uploader.authenticate():
                logger.debug("Thumbnail verify: auth failed for %s — retry next cycle", canal)
                continue

            service = uploader._get_service()

            # Check if a custom thumbnail exists on YT (1 quota unit).
            # NOTE: YouTube Data API v3 has NO `thumbnails().list()` endpoint —
            # only `thumbnails().set()`. A previous revision called the non-existent
            # `list()`, raising `AttributeError: 'Resource' object has no attribute
            # 'list'` every cycle and always falling through to a 50-unit re-upload.
            # Verify via videos().list(part="snippet") and detect a custom
            # thumbnail through the maxres key instead.
            try:
                resp = service.videos().list(
                    part="snippet",
                    id=yt_video_id,
                    fields="items/snippet/thumbnails",
                ).execute()

                # ── Track quota (diagnostic) ──────────────────────────
                track_quota(canal, "videos.list", 1,
                            yt_id=yt_video_id, caller="thumbnail_verify.check")

                items = resp.get("items", [])
                thumbnails = (
                    items[0].get("snippet", {}).get("thumbnails", {})
                    if items else {}
                )
                if thumbnails.get("maxres"):
                    # Custom thumbnail already present — mark as verified
                    db.update_video(video_id, thumbnail_verified=1)
                    logger.info(
                        "[%s] ✅ Thumbnail verified for video #%d (%s)",
                        canal, video_id, yt_video_id,
                    )
                    continue
            except Exception:
                # Can't verify — possibly YT API issue. Try upload anyway.
                pass

            # No custom thumbnail — re-upload local file (50 quota units)
            logger.info(
                "[%s] 🔄 Re-uploading thumbnail for video #%d (%s)",
                canal, video_id, yt_video_id,
            )
            upload_success = uploader._set_thumbnail(
                service, yt_video_id, Path(thumbnail_path),
            )
            if upload_success:
                db.update_video(video_id, thumbnail_verified=1)
                recovered += 1
                logger.info(
                    "[%s] ✅ Thumbnail re-uploaded for video #%d (%s)",
                    canal, video_id, yt_video_id,
                )
            else:
                logger.warning(
                    "[%s] ❌ Thumbnail re-upload FAILED for video #%d — "
                    "retry next cycle",
                    canal, video_id,
                )

        except Exception as e:
            logger.warning(
                "[%s] Thumbnail verify error for video #%d: %s",
                canal, video_id, e,
            )
            continue

    if recovered:
        logger.info("Thumbnail verify: %d thumbnail(s) re-uploaded this cycle", recovered)
