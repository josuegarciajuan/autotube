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

logger = logging.getLogger("autotube.thumbnail_verify")

THUMBNAIL_VERIFY_BATCH = 5  # max videos per cycle
THUMBNAIL_VERIFY_MAX_AGE_DAYS = 7  # only check recent uploads

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


def _persist_verified(db, video_id: int) -> bool:
    """Persist a confirmed verification and report whether it was written.

    ``ExtendedDatabase.update_video`` filters unknown columns, so a missing
    allow-list entry used to make this a silent no-op. Never announce success
    unless the row was actually updated.
    """
    try:
        ok = db.update_video(video_id, thumbnail_verified=1)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Thumbnail verify: DB update failed for #%d: %s", video_id, exc)
        return False
    if ok is False:
        logger.error(
            "Thumbnail verify: could not persist thumbnail_verified for video #%d "
            "— update_video rejected the column", video_id,
        )
        return False
    return True


async def run_thumbnail_verification_cycle(db=None):
    """Check recent uploaded videos for missing YT thumbnails and retry.

    Detection is by *perceptual comparison* between the image YouTube actually
    serves (``i.ytimg.com/vi/<id>/maxresdefault.jpg``, 0 API quota) and the
    local thumbnail — NOT by the presence of ``maxres`` in ``videos.list``
    (which also exists for auto-generated thumbnails, giving false positives).

    Called from the checker loop every 5 minutes. Processes up to
    ``THUMBNAIL_VERIFY_BATCH`` videos per cycle. A re-upload costs 50 units
    (``thumbnails.set``) and only happens when the served image really differs.
    """
    import json as _json

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

    from pipeline.thumbnail_compare import thumbnails_match, fetch_served_thumbnail

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

        base_path = Path(thumbnail_path) if thumbnail_path else None
        if not base_path or not base_path.exists():
            logger.debug(
                "Thumbnail verify: video #%d has no local file (%s) — skipping",
                video_id, thumbnail_path,
            )
            continue

        # ── Candidate local thumbnails: base + A/B variants ─────────
        # A served image matching ANY candidate counts as OK (the A/B worker
        # may have swapped to a variant, so the base file can legitimately
        # differ from what YouTube currently shows).
        candidates: list[Path] = [base_path]
        active_path = base_path
        try:
            with db._connect() as conn:
                conn.row_factory = None
                ab = conn.execute(
                    "SELECT thumbnail_variant_paths, thumbnail_variant_active "
                    "FROM video_ab_tests WHERE video_id=? ORDER BY id DESC LIMIT 1",
                    (video_id,),
                ).fetchone()
            if ab and ab[0]:
                vpaths = [Path(p) for p in (_json.loads(ab[0]) or []) if p]
                for p in vpaths:
                    if p.exists() and p not in candidates:
                        candidates.append(p)
                active_idx = int(ab[1] or 1)
                if 1 <= active_idx <= len(vpaths) and vpaths[active_idx - 1].exists():
                    active_path = vpaths[active_idx - 1]
        except Exception as exc:
            logger.debug("Thumbnail verify: variant lookup failed for #%d: %s", video_id, exc)

        # ── 0-quota check: does YouTube already serve one of ours? ──
        served = fetch_served_thumbnail(yt_video_id)
        if served is None:
            # Private / not yet indexed — retry next cycle, never re-set blindly.
            logger.debug(
                "Thumbnail verify: cannot fetch served image for %s — retry later",
                yt_video_id,
            )
            continue

        if any(thumbnails_match(cand, yt_video_id, served_image=served) is True
               for cand in candidates):
            if _persist_verified(db, video_id):
                logger.info("[%s] ✅ Thumbnail verified for video #%d (%s)", canal, video_id, yt_video_id)
            continue

        # ── Mismatch: re-upload the active thumbnail and confirm ────
        try:
            from pipeline.youtube_uploader import YouTubeUploader
            uploader = YouTubeUploader(canal)
            if not uploader.authenticate():
                logger.debug("Thumbnail verify: auth failed for %s — retry next cycle", canal)
                continue

            service = uploader._get_service()
            logger.info(
                "[%s] 🔄 Re-uploading thumbnail for video #%d (%s)",
                canal, video_id, yt_video_id,
            )
            if not uploader._set_thumbnail(service, yt_video_id, active_path):
                logger.warning(
                    "[%s] ❌ Thumbnail re-upload FAILED for video #%d — retry next cycle",
                    canal, video_id,
                )
                continue

            if uploader._verify_thumbnail_applied(yt_video_id, active_path, attempts=2, delay=20) is True:
                if _persist_verified(db, video_id):
                    recovered += 1
                    logger.info("[%s] ✅ Thumbnail repaired for video #%d (%s)", canal, video_id, yt_video_id)
            else:
                logger.warning(
                    "[%s] ❌ Thumbnail accepted but not confirmed for video #%d — retry next cycle",
                    canal, video_id,
                )
        except Exception as e:
            logger.warning(
                "[%s] Thumbnail verify error for video #%d: %s",
                canal, video_id, e,
            )
            continue

    if recovered:
        logger.info("Thumbnail verify: %d thumbnail(s) repaired this cycle", recovered)
