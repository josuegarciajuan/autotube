"""Render recovery: requeue long-form renders held by narration/timeline gates.

Videos that fail ``_assert_body_timeline_sync`` (body shorter than the
narration) or another render-timeline gate keep their checkpoint (script + TTS
+ media). The root causes are fixed in code, so a **reassembly with the current
code** is enough — no re-scrape, no re-scripting.

Why a supervised loop
---------------------
A fix deployed while a render is in flight does NOT affect that worker: the
project restarts the API but intentionally keeps generation workers alive
(``KillMode=process``). When the old worker finally fails, the video stays in
``error`` and nothing retries it. This loop gives those videos a bounded
retry once the new code is running.

Safety gates (all mandatory)
----------------------------
* never uploaded (``yt_video_id`` empty),
* no queued/running job for the video or its channel,
* checkpoint has TTS + media + ``scene_ranges`` 1:1 with the assets,
* ALL media assets still exist on disk. Reassembly cannot restore purged video
  clips; a degraded fallback render is worse than leaving the row for a manual
  decision,
* bounded attempts + cooldown so a deterministic failure never loops.

Kill-switch: ``RENDER_RECOVERY_ENABLED=false``.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

logger = logging.getLogger("autotube.render_recovery")

INTERVAL_MIN = int(os.getenv("RENDER_RECOVERY_INTERVAL_MIN", "30") or 30)
MAX_PER_CYCLE = int(os.getenv("RENDER_RECOVERY_MAX_PER_CYCLE", "2") or 2)
MAX_ATTEMPTS = int(os.getenv("RENDER_RECOVERY_MAX_ATTEMPTS", "2") or 2)
COOLDOWN_MIN = float(os.getenv("RENDER_RECOVERY_COOLDOWN_MIN", "120") or 120)
LOOKBACK_DAYS = int(os.getenv("RENDER_RECOVERY_LOOKBACK_DAYS", "30") or 30)
MIN_ASSET_RATIO = float(os.getenv("RENDER_RECOVERY_MIN_ASSET_RATIO", "1.0") or 1.0)

# Errors that a plain reassembly (same checkpoint, current code) can resolve.
# Keep this list explicit: a random ``error`` must never be retried blindly.
RECOVERABLE_ERROR_MARKERS = (
    "Body narration is longer than the video body",
    "Body narration is shorter than the video body",
    "Rendered body is shorter than the planned timeline",
    "Narration is shorter than the planned timeline",
    "Scene timeline does not cover the narration",
    "Scene timeline exceeds the narration",
    "One or more planned scene segments failed",
    "render_gate:",
)

# Statuses we are allowed to look at. ``validation_failed`` covers the final
# render gate (e.g. excessive black frames) which is also fixed by reassembly.
CANDIDATE_STATUSES = ("error", "validation_failed")


def _enabled() -> bool:
    raw = os.getenv("RENDER_RECOVERY_ENABLED", "true").strip().lower()
    return raw not in ("0", "false", "no", "off")


def _attempts_key(video_id: int) -> str:
    return f"render_recovery_attempts_{int(video_id)}"


def _last_key(video_id: int) -> str:
    return f"render_recovery_last_{int(video_id)}"


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _checkpoint_ready(video: dict) -> tuple[bool, str]:
    """Validate that a reassembly can reproduce this video faithfully.

    Returns ``(ok, reason)``. A missing asset is fatal: the editor would drop
    it and substitute fallback media, producing a degraded video. Those rows
    need a manual decision (regenerate from scratch / discard), not a retry.
    """
    raw = video.get("checkpoint_data") or "{}"
    try:
        cp = json.loads(raw) if isinstance(raw, str) else (raw or {})
    except (json.JSONDecodeError, TypeError):
        return False, "checkpoint_invalid"

    tts = cp.get("tts") or {}
    audio_path = tts.get("audio_path") or video.get("audio_path") or ""
    if not audio_path or not Path(str(audio_path)).is_file():
        return False, "missing_tts_audio"

    media = cp.get("media") or {}
    assets = media.get("assets") or []
    ranges = media.get("scene_ranges") or []
    if not assets:
        return False, "missing_media_assets"
    if not ranges or len(ranges) != len(assets):
        return False, "scene_ranges_not_1to1"

    present = sum(
        1 for a in assets
        if a.get("path") and Path(str(a["path"])).is_file()
    )
    ratio = present / len(assets)
    if ratio < MIN_ASSET_RATIO:
        return False, f"purged_assets_{present}_{len(assets)}"
    return True, "ok"


def _active_job(db, video_id: int, channel_id: int) -> bool:
    try:
        with db._connect() as conn:
            row = conn.execute(
                """SELECT 1 FROM generation_jobs
                    WHERE status IN ('queued','running')
                      AND (video_id = ? OR channel_id = ?)
                    LIMIT 1""",
                (video_id, channel_id),
            ).fetchone()
        return row is not None
    except Exception as exc:  # noqa: BLE001 — fail closed (skip this video)
        logger.debug("render_recovery: active-job check failed: %s", exc)
        return True


def _candidates(db, lookback_days: int) -> list[dict]:
    placeholders = ",".join("?" for _ in CANDIDATE_STATUSES)
    like = " OR ".join("error_message LIKE ?" for _ in RECOVERABLE_ERROR_MARKERS)
    params = (
        *CANDIDATE_STATUSES,
        f"-{int(lookback_days)} days",
        *(f"%{marker}%" for marker in RECOVERABLE_ERROR_MARKERS),
    )
    sql = f"""SELECT id, canal, channel_id, status, progress_phase,
                     error_message, checkpoint_data, audio_path, created_at
                FROM videos
               WHERE status IN ({placeholders})
                 AND (yt_video_id IS NULL OR yt_video_id = '')
                 AND COALESCE(created_at, '') >= datetime('now', ?)
                 AND ({like})
               ORDER BY id"""
    with db._connect() as conn:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


def recover_render_held_videos(
    db=None,
    dry_run: bool = False,
    now: datetime | None = None,
) -> dict:
    """Requeue eligible videos as ``queued`` reassemble jobs.

    The queue consumer serializes them; this function never spawns a render
    directly. Idempotent: a video with an active job or an exhausted attempt
    budget is skipped.
    """
    if not _enabled():
        return {"scanned": 0, "queued": 0, "skipped": 0, "errors": 0,
                "details": [], "disabled": True}

    if db is None:
        from database.db_extended import ExtendedDatabase
        db = ExtendedDatabase()

    now = now or datetime.now(timezone.utc)
    summary = {"scanned": 0, "queued": 0, "skipped": 0, "errors": 0,
               "details": []}
    if MAX_PER_CYCLE <= 0:
        return summary

    try:
        rows = _candidates(db, LOOKBACK_DAYS)
    except Exception as exc:  # noqa: BLE001 — a bad query must not kill the loop
        logger.warning("render_recovery: candidate query failed: %s", exc)
        summary["errors"] += 1
        return summary

    for video in rows:
        if summary["queued"] >= MAX_PER_CYCLE:
            break
        summary["scanned"] += 1
        vid = int(video["id"])
        channel_id = int(video.get("channel_id") or 0)
        detail = {"video_id": vid, "channel": video.get("canal")}

        try:
            attempts = int(db.get_system_state(_attempts_key(vid)) or 0)
        except Exception:
            attempts = 0
        if attempts >= MAX_ATTEMPTS:
            detail.update(action="skipped", reason="attempts_exhausted")
            summary["skipped"] += 1
            summary["details"].append(detail)
            continue

        last = _parse_dt(db.get_system_state(_last_key(vid)))
        if last is not None and (now - last) < timedelta(minutes=COOLDOWN_MIN):
            detail.update(action="skipped", reason="cooldown")
            summary["skipped"] += 1
            summary["details"].append(detail)
            continue

        if not channel_id:
            detail.update(action="skipped", reason="no_channel")
            summary["skipped"] += 1
            summary["details"].append(detail)
            continue

        if _active_job(db, vid, channel_id):
            detail.update(action="skipped", reason="active_job")
            summary["skipped"] += 1
            summary["details"].append(detail)
            continue

        ok, reason = _checkpoint_ready(video)
        if not ok:
            detail.update(action="skipped", reason=reason)
            summary["skipped"] += 1
            summary["details"].append(detail)
            continue

        if dry_run:
            detail.update(action="would_requeue", reason="ok")
            summary["queued"] += 1
            summary["details"].append(detail)
            continue

        try:
            job_id = db.create_job(channel_id, "reassemble", vid)
            db.set_system_state(_attempts_key(vid), str(attempts + 1))
            db.set_system_state(_last_key(vid), now.isoformat())
            detail.update(action="queued", job_id=job_id,
                          attempt=attempts + 1)
            summary["queued"] += 1
            logger.info(
                "[%s] render recovery: video #%d queued as reassemble job #%d "
                "(attempt %d/%d, status=%s)",
                video.get("canal"), vid, job_id, attempts + 1, MAX_ATTEMPTS,
                video.get("status"),
            )
        except Exception as exc:  # noqa: BLE001
            detail.update(action="error", reason=str(exc)[:200])
            summary["errors"] += 1
            logger.warning("render recovery: failed to queue #%d: %s", vid, exc)
        summary["details"].append(detail)

    return summary
