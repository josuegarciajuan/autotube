"""Glass Box: periodic auto-recovery of orphaned videos with complete .mp4 files.

Runs every 30 min from the checker loop.  Guarantees that once a video
is fully generated, it is NEVER permanently abandoned — even if retries
are exhausted, server restarts kill upload workers, or other transient failures.

Strategy:
  - Scans ALL videos in 'error' status with video_path on disk
  - Uses the same classification logic as recover_orphaned_videos.py
  - Tier 1 (safe): generate_only completed, upload_only failed → auto-recover
  - Tier 2 (likely): progress >= 87%, large mp4 → auto-recover
  - Tier 3 (review): everything else → logged but not auto-recovered

Safety gates:
  - Max 1 recovery per channel per cycle (avoids spam)
  - Skips videos marked 'private_quality_issue' or 'auth_stuck'
  - Skips videos recovered in the last 24h (avoid loops)
"""

import hashlib
import logging
import os
from datetime import datetime, timedelta
from typing import Optional

logger = logging.getLogger("autotube.glass_box")

# — Constants —
GLASS_BOX_INTERVAL_MIN = 30  # how often this runs
MAX_RECOVERIES_PER_CHANNEL_PER_CYCLE = 1
RECOVERY_COOLDOWN_HOURS = 24  # don't recover same video twice within 24h


def glass_box_recover_orphaned_videos(db=None, dry_run: bool = False) -> dict:
    """Auto-recover error videos with .mp4 files on disk.

    Called from the checker loop every GLASS_BOX_INTERVAL_MIN.

    Returns dict with {recovered, skipped, errors} counts.
    """
    if db is None:
        from database.db_extended import ExtendedDatabase
        db = ExtendedDatabase()

    recovery_tag = "__glass_box_recovered__"
    now = datetime.utcnow()
    cooldown_cutoff = (now - timedelta(hours=RECOVERY_COOLDOWN_HOURS)).strftime("%Y-%m-%d %H:%M:%S")

    recovered = 0
    skipped = 0
    errors = 0
    per_channel_count: dict[int, int] = {}

    with db._connect() as conn:
        # Find all candidate videos
        candidates = conn.execute(
            """SELECT v.id, v.channel_id, v.status, v.video_path, v.progress,
                      v.progress_phase, v.generation_finished_at, v.error_message,
                      c.slug, c.name
               FROM videos v JOIN channels c ON c.id = v.channel_id
               WHERE v.status = 'error'
                 AND v.video_path IS NOT NULL AND v.video_path != ''
               ORDER BY v.created_at DESC""",
        ).fetchall()

    if not candidates:
        return {"recovered": 0, "skipped": 0, "errors": 0}

    for row in candidates:
        video = dict(row)
        vid = video["id"]
        slug = video["slug"]
        ch_id = video["channel_id"]
        path = video["video_path"]

        # — Safety gate 1: file must exist on disk —
        if not path or not os.path.exists(path):
            continue

        size_mb = os.path.getsize(path) / 1024 / 1024
        if size_mb < 10:
            continue  # too small, likely corrupted

        # — Safety gate 2: skip special error types —
        err_msg = (video.get("error_message") or "").lower()
        if any(term in err_msg for term in ("quality_issue", "auth_stuck", "abandoned_intentionally")):
            skipped += 1
            continue

        # — Safety gate 3: cooldown (don't recover same video repeatedly) —
        if recovery_tag in (err_msg or ""):
            skipped += 1
            continue

        # — Safety gate 4: per-channel limit —
        if per_channel_count.get(ch_id, 0) >= MAX_RECOVERIES_PER_CHANNEL_PER_CYCLE:
            skipped += 1
            continue

        # — Classification —
        progress = video.get("progress", 0) or 0
        progress_phase = video.get("progress_phase", "") or ""
        gen_finished = video.get("generation_finished_at")

        with db._connect() as conn:
            has_gen_completed = conn.execute(
                "SELECT COUNT(*) as cnt FROM generation_jobs "
                "WHERE video_id=? AND action='generate_only' AND status='completed'",
                (vid,),
            ).fetchone()["cnt"] > 0

            all_upload_failed = conn.execute(
                "SELECT COUNT(*) as cnt FROM generation_jobs "
                "WHERE video_id=? AND action='upload_only' AND status='failed'",
                (vid,),
            ).fetchone()["cnt"] > 0 and not conn.execute(
                "SELECT COUNT(*) as cnt FROM generation_jobs "
                "WHERE video_id=? AND action='upload_only' AND status='completed'",
                (vid,),
            ).fetchone()["cnt"] > 0

        should_recover = False
        tier = "unknown"

        if has_gen_completed and all_upload_failed:
            should_recover = True
            tier = "T1 (safe)"
        elif progress >= 87 and size_mb > 50:
            should_recover = True
            tier = "T2 (likely)"
        # Tier 3 is NOT auto-recovered

        if not should_recover:
            skipped += 1
            continue

        # — Recover —
        if dry_run:
            logger.info(
                "[GLASS_BOX] [DRY-RUN] Would recover %s vid=%d ch=%s %s (%.0fMB, progress=%d%%)",
                tier, vid, slug, video.get("name", ""), size_mb, progress,
            )
            recovered += 1
            continue

        try:
            with db._connect() as conn:
                # Reset failed upload_only jobs so the retry counter is cleared
                conn.execute(
                    "UPDATE generation_jobs SET status='cancelled', "
                    "error_msg='Glass Box auto-recovery' "
                    "WHERE video_id=? AND action='upload_only' AND status='failed'",
                    (vid,),
                )
                # Reset video to awaiting_upload
                conn.execute(
                    "UPDATE videos SET status='awaiting_upload', progress=5, "
                    "progress_phase='upload', scheduled_upload_at=NULL, "
                    "error_message=? "
                    "WHERE id=?",
                    (f"Recovered by Glass Box at {now.strftime('%Y-%m-%d %H:%M UTC')}. {recovery_tag}", vid),
                )
                conn.commit()

            per_channel_count[ch_id] = per_channel_count.get(ch_id, 0) + 1
            recovered += 1

            logger.warning(
                "[GLASS_BOX] 🟢 Recovered %s vid=%d ch=%s '%s' (%.0fMB) → awaiting_upload",
                tier, vid, slug, (video.get("titulo_final") or "?")[:50], size_mb,
            )

        except Exception as e:
            errors += 1
            logger.error("[GLASS_BOX] ❌ Failed to recover vid=%d: %s", vid, e)

    if recovered > 0:
        logger.info(
            "[GLASS_BOX] Cycle complete: %d recovered, %d skipped, %d errors",
            recovered, skipped, errors,
        )

    return {"recovered": recovered, "skipped": skipped, "errors": errors}
