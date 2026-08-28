#!/usr/bin/env python3
"""Migrate persisted spam blocks to the balanced 12h/24h policy.

Usage: ``--dry-run`` previews all changes; ``--apply`` writes state, audit
events, pending-video schedules, and best-effort YouTube publishAt updates.
The operation is idempotent: expired blocks are untouched and an already
shortened active block is not extended on a later run.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from api.services.spam_mitigation import resolve_spam_block_duration_hours

logger = logging.getLogger("autotube.migrate_spam_block_policy")
BLOCK_PREFIX = "shorts_spam_blocked_until_"
STRIKE_PREFIX = "shorts_spam_strikes_"
GAP_HOURS = 24
MARGIN_HOURS = 1


def _pending(db, channel_id: int, new_until: float, old_until: float) -> list[dict]:
    """Select affected pending videos, including the previous retention chain.

    Old deployments moved blocked videos to ``old_until + 1h`` and then in
    24-hour steps.  Those rows are after ``old_until`` and therefore cannot be
    found by a simple ``target < new_until`` query.  We only accept a chain
    anchored at that known retention boundary (±5 minutes), avoiding unrelated
    future videos from being moved.
    """
    import sqlite3
    try:
        with db._connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """SELECT id, yt_video_id, target_public_at FROM videos
                   WHERE channel_id=? AND published_at IS NULL
                     AND status IN ('uploaded_private','uploaded','warming','scheduled')
                     AND target_public_at IS NOT NULL ORDER BY target_public_at""",
                (channel_id,),
            ).fetchall()
    except Exception:
        return []
    result = []
    parsed = []
    for row in rows:
        try:
            dt = datetime.fromisoformat(str(row["target_public_at"]).replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            parsed.append((dt.timestamp(), dict(row)))
        except (TypeError, ValueError):
            continue
    parsed.sort(key=lambda item: item[0])
    selected = {row["id"]: row for ts, row in parsed if ts < new_until}

    # The old hold schedule starts one hour after old_until and repeats daily.
    anchor = old_until + MARGIN_HOURS * 3600
    previous = None
    for ts, row in parsed:
        if previous is None:
            if abs(ts - anchor) <= 5 * 60:
                selected[row["id"]] = row
                previous = ts
        elif abs(ts - (previous + GAP_HOURS * 3600)) <= 5 * 60:
            selected[row["id"]] = row
            previous = ts
    return [row for _, row in parsed if row["id"] in selected]


def _audit(db, channel_id: int, message: str, metadata: dict) -> None:
    try:
        from api.services.lifecycle_monitor import log_event
        log_event(db, entity_type="system", entity_id=0, channel_id=channel_id,
                  event="spam_block_policy_migrated", status="info",
                  message=message, metadata=metadata)
    except Exception as exc:
        logger.warning("audit failed for channel %s: %s", channel_id, exc)


def _apply_schedule(db, video: dict, new_dt: datetime, slug: str) -> None:
    iso = new_dt.isoformat()
    with db._connect() as conn:
        conn.execute("UPDATE videos SET target_public_at=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                     (iso, video["id"]))
        try:
            conn.execute("UPDATE planned_slots SET target_public_at=? WHERE video_id=?",
                         (iso, video["id"]))
        except Exception:
            pass
        try:
            conn.execute(
                """UPDATE video_lifecycle_actions SET scheduled_for=?
                   WHERE video_id=? AND action_type='go_public' AND status='pending'""",
                (iso, video["id"]),
            )
        except Exception:
            pass
        conn.commit()
    if video.get("yt_video_id"):
        try:
            from pipeline.youtube_uploader import YouTubeUploader
            # OAuth tokens are stored per channel slug (tokens/{slug}.pickle);
            # google_account is only the shared account/project identity.
            YouTubeUploader(account_name=slug, channel_slug=slug, db=db).set_publish_at(
                video["yt_video_id"], iso
            )
        except Exception as exc:
            logger.warning("best-effort YouTube reschedule failed for %s: %s",
                           video["yt_video_id"], exc)


def migrate_spam_state(db, *, apply: bool = False, now: float | None = None) -> dict:
    """Discover and migrate all runtime spam-block keys without hardcoding IDs."""
    now = time.time() if now is None else float(now)
    channels = {int(c["id"]): c for c in (db.get_channels(active_only=False) or [])
                if c.get("id")}
    keys = []
    try:
        with db._connect() as conn:
            keys = [r[0] for r in conn.execute(
                "SELECT key FROM system_state WHERE key LIKE ?", (BLOCK_PREFIX + "%",)
            ).fetchall()]
    except Exception:
        return {"changed_blocks": 0, "expired": 0, "replanned": 0, "audited": 0}

    result = {"changed_blocks": 0, "expired": 0, "replanned": 0, "audited": 0}
    for key in keys:
        try:
            cid = int(key[len(BLOCK_PREFIX):])
            old_until = float(db.get_system_state(key))
        except (TypeError, ValueError):
            continue
        if old_until <= now:
            result["expired"] += 1
            continue
        try:
            events = int(db.get_system_state(f"{STRIKE_PREFIX}{cid}") or 1)
        except (TypeError, ValueError):
            events = 1
        duration = resolve_spam_block_duration_hours(events)
        new_until = now + duration * 3600
        if old_until <= new_until:
            continue
        ch = channels.get(cid, {"slug": str(cid)})
        pending = _pending(db, cid, new_until, old_until)
        plan = [new_until + MARGIN_HOURS * 3600 + i * GAP_HOURS * 3600
                for i in range(len(pending))]
        if apply:
            db.set_system_state(key, str(new_until))
            for video, ts in zip(pending, plan):
                _apply_schedule(
                    db, video, datetime.fromtimestamp(ts, timezone.utc),
                    ch.get("slug", str(cid)),
                )
            _audit(db, cid, "Spam block migrated to balanced policy", {
                "old_until": old_until, "new_until": new_until,
                "events": events, "duration_hours": duration,
                "replanned_video_ids": [v["id"] for v in pending],
            })
            result["audited"] += 1
        result["changed_blocks"] += 1
        result["replanned"] += len(pending)
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    from config.settings import DATABASE_PATH
    from database.db_extended import ExtendedDatabase
    result = migrate_spam_state(ExtendedDatabase(str(DATABASE_PATH)), apply=args.apply)
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
