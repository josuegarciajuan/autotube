"""Gradual backfill: add long-form links to existing short descriptions.

Processes shorts in small batches (10-15 at a time) every 30 minutes
to stay well under YouTube API quota (50 units per video update).

Survives server restarts via system_state persistence.
Runs until all published shorts have been backfilled.
"""

import logging
import sqlite3
import time
from pathlib import Path

logger = logging.getLogger("autotube.backfill")

# ── Tuning ────────────────────────────────────────────────
BATCH_SIZE = 15               # shorts per batch (15 × 50 = 750 quota units)
BATCH_COOLDOWN_SECONDS = 1800  # 30 min between batches (~48 batches/day = 720 shorts)
STATE_KEY_CHANNEL = "shorts_backfill_channel_id"
STATE_KEY_LAST_ID = "shorts_backfill_last_short_id"
STATE_KEY_UPDATED = "shorts_backfill_updated"
STATE_KEY_COMPLETED = "shorts_backfill_completed"

# Ensure project root in path for imports that happen inside functions
import sys
PROJECT_ROOT = str(Path(__file__).resolve().parent.parent.parent)
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)


def _get_db_path() -> str:
    from config.settings import DATABASE_PATH
    return str(DATABASE_PATH)


def _get_pending_count() -> int:
    """How many published shorts still need backfill? """
    conn = sqlite3.connect(_get_db_path(), timeout=10)
    try:
        row = conn.execute(
            """SELECT COUNT(*) FROM shorts s
               WHERE s.status = 'published'
                 AND s.youtube_id IS NOT NULL AND s.youtube_id != ''
                 AND (s.longform_linked = 0 OR s.longform_linked IS NULL)
            """
        ).fetchone()
        return row[0] if row else 0
    finally:
        conn.close()


def is_backfill_complete() -> bool:
    """Quick check: are all shorts already linked?"""
    from database.db_extended import ExtendedDatabase
    db = ExtendedDatabase()
    # If the state says completed, double-check with a real count
    if db.get_system_state(STATE_KEY_COMPLETED) == "true":
        pending = _get_pending_count()
        if pending == 0:
            return True
        # Reset flag if there are actually still pending shorts
        db.set_system_state(STATE_KEY_COMPLETED, "false")
        return False
    return _get_pending_count() == 0


def run_backfill_batch() -> dict:
    """Process one batch of unlinked shorts.

    Returns:
        {"updated": N, "errors": N, "done": bool}
    """
    from database.db_extended import ExtendedDatabase
    from config.config_bridge import get_channel_config
    from pipeline.shorts_cross_promote import (
        get_best_longform_link,
        build_short_description,
    )

    db = ExtendedDatabase()

    # ── Resume from last saved position ────────────────────
    last_channel_id = None
    last_short_id = None
    saved_ch = db.get_system_state(STATE_KEY_CHANNEL)
    saved_id = db.get_system_state(STATE_KEY_LAST_ID)
    if saved_ch:
        try:
            last_channel_id = int(saved_ch)
        except (ValueError, TypeError):
            last_channel_id = None
    if saved_id:
        try:
            last_short_id = int(saved_id)
        except (ValueError, TypeError):
            last_short_id = None

    # ── Get channels ordered by ID ─────────────────────────
    conn = sqlite3.connect(_get_db_path(), timeout=10)
    channels = conn.execute(
        "SELECT id, slug FROM channels WHERE active = 1 ORDER BY id"
    ).fetchall()
    conn.close()

    # Find starting channel (resume or first)
    start_idx = 0
    if last_channel_id:
        for i, ch in enumerate(channels):
            if ch[0] == last_channel_id:
                start_idx = i
                break

    updated = 0
    errors = 0
    batch = 0

    for ch_idx in range(start_idx, len(channels)):
        channel_id, slug = channels[ch_idx]
        ch_config = get_channel_config(slug)

        if not getattr(ch_config, "SHORTS_LONGFORM_LINK_ENABLED", True):
            continue

        conn = sqlite3.connect(_get_db_path(), timeout=10)

        # Fetch next batch of unlinked shorts for this channel
        query = """SELECT s.id, s.youtube_id, s.title, s.hook_text,
                          s.source_video_id
                   FROM shorts s
                   WHERE s.channel_id = ?
                     AND s.status = 'published'
                     AND s.youtube_id IS NOT NULL AND s.youtube_id != ''
                     AND (s.longform_linked = 0 OR s.longform_linked IS NULL)
                   ORDER BY s.id"""
        params = [channel_id]

        # If resuming from last_short_id within the same channel, pick up after it
        if last_short_id and channel_id == last_channel_id:
            query += " AND s.id > ?"
            params.append(last_short_id)
            last_short_id = None  # Reset for subsequent channels

        shorts = conn.execute(query + " LIMIT ?", params + [BATCH_SIZE]).fetchall()
        conn.close()

        for short in shorts:
            short_id, yt_id, title, hook_text, source_video_id = short

            if batch >= BATCH_SIZE:
                # Save progress and return
                db.set_system_state(STATE_KEY_CHANNEL, str(channel_id))
                db.set_system_state(STATE_KEY_LAST_ID, str(short_id - 1))
                _inc_updated(db, updated)
                return {"updated": updated, "errors": errors, "done": False}

            # Find the best long-form link for this short
            longform_url = get_best_longform_link(
                channel_id,
                source_video_id=source_video_id,
            )
            if not longform_url:
                continue

            hashtags = getattr(ch_config, "SHORTS_HASHTAGS", ["#Shorts"])
            channel_url = getattr(ch_config, "YOUTUBE_CHANNEL_URL", "")

            new_desc = build_short_description(
                hook_text=hook_text or "",
                hashtags=hashtags,
                longform_url=longform_url,
                channel_url=channel_url,
            )

            # ── Update YouTube description ──────────────────
            try:
                from pipeline.youtube_uploader import YouTubeUploader
                uploader = YouTubeUploader(
                    account_name=slug,
                    channel_slug=slug,
                )
                if not uploader.authenticate():
                    logger.error("[backfill] Auth failed for %s short %d", slug, short_id)
                    errors += 1
                    batch += 1
                    continue

                uploader.update_description(yt_id, new_desc)
                logger.info("[backfill] %s short #%d: description updated → %s",
                            slug, short_id, yt_id)

                # Mark as processed in DB
                conn2 = sqlite3.connect(_get_db_path(), timeout=10)
                conn2.execute(
                    "UPDATE shorts SET longform_linked = 1, longform_linked_at = datetime('now','localtime') WHERE id = ?",
                    (short_id,),
                )
                conn2.commit()
                conn2.close()

                updated += 1
                batch += 1

                # Small sleep to avoid rate-limiting
                time.sleep(0.5)

            except Exception as exc:
                error_str = str(exc)[:200]
                if "quotaExceeded" in error_str:
                    logger.warning("[backfill] Quota exceeded at channel=%s short=%d — will retry later",
                                   slug, short_id)
                    # Save position so we resume here on next batch
                    db.set_system_state(STATE_KEY_CHANNEL, str(channel_id))
                    db.set_system_state(STATE_KEY_LAST_ID, str(short_id - 1))
                    _inc_updated(db, updated)
                    return {"updated": updated, "errors": errors, "done": False}
                logger.error("[backfill] %s short #%d error: %s", slug, short_id, error_str)
                errors += 1
                batch += 1

    # ── All channels processed ─────────────────────────────
    _inc_updated(db, updated)
    remaining = _get_pending_count()
    if remaining == 0:
        db.set_system_state(STATE_KEY_COMPLETED, "true")
        db.set_system_state(STATE_KEY_CHANNEL, "")
        db.set_system_state(STATE_KEY_LAST_ID, "")
        logger.info("[backfill] ✅ Complete! All published shorts now have long-form links.")
        return {"updated": updated, "errors": errors, "done": True}
    else:
        # Reset for next full pass
        db.set_system_state(STATE_KEY_CHANNEL, "")
        db.set_system_state(STATE_KEY_LAST_ID, "")
        logger.info("[backfill] Full pass done: %d updated, %d errors, %d remaining",
                    updated, errors, remaining)
        return {"updated": updated, "errors": errors, "done": False}


def _inc_updated(db, count: int):
    current = db.get_system_state(STATE_KEY_UPDATED) or "0"
    try:
        db.set_system_state(STATE_KEY_UPDATED, str(int(current) + count))
    except (ValueError, TypeError):
        db.set_system_state(STATE_KEY_UPDATED, str(count))


def get_backfill_status() -> dict:
    """Return current backfill progress for the dashboard."""
    from database.db_extended import ExtendedDatabase
    db = ExtendedDatabase()
    return {
        "completed": db.get_system_state(STATE_KEY_COMPLETED) == "true",
        "updated": int(db.get_system_state(STATE_KEY_UPDATED) or "0"),
        "channel_id": db.get_system_state(STATE_KEY_CHANNEL) or None,
        "last_short_id": db.get_system_state(STATE_KEY_LAST_ID) or None,
        "pending": _get_pending_count(),
    }
