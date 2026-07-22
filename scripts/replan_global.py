#!/usr/bin/env python3
"""Global replan: cancel impossible slots, rescore, re-chain with realistic gaps.

Macro-fase A — Smart Scheduling v2 Foundation.

Usage:
    python3 scripts/replan_global.py           # dry-run (preview changes)
    python3 scripts/replan_global.py --apply   # apply changes to DB
"""

import json
import logging
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

# Ensure project root is on sys.path
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger("replan_global")

# ── Constants ────────────────────────────────────────────────────
GLOBAL_GAP_FLOOR_MINUTES = 30     # minimum gap between generation starts
BUFFER_PCT = 0.15                 # safety margin on per-channel avg creation time
STALE_SCHEDULED_HOURS = 6         # cancel if scheduled_at > this many hours in the past
STALE_UPLOAD_BUFFER_MINUTES = 90  # cancel if target_upload_at + buffer is in the past


def get_db():
    from database.db_extended import ExtendedDatabase, migrate_v2
    migrate_v2()
    return ExtendedDatabase()


def get_channel_configs(db):
    """Load active channel configs with resolved videos_per_day."""
    from config.config_bridge import get_channel_config
    channels = db.get_channels(active_only=True)
    configs = []
    for ch in channels:
        slug = ch.slug if hasattr(ch, 'slug') else ch.get("slug", "")
        channel_id = ch.id if hasattr(ch, 'id') else ch.get("id", 0)
        channel_name = ch.name if hasattr(ch, 'name') else ch.get("name", slug)
        cfg_ns = get_channel_config(slug)
        # Convert SimpleNamespace → dict and inject channel_id
        cfg = vars(cfg_ns).copy() if hasattr(cfg_ns, '__dict__') else dict(cfg_ns)
        cfg["channel_id"] = channel_id
        cfg["slug"] = slug
        cfg["name"] = channel_name
        # Normalize config_json from channels table (DB overrides)
        config_json_raw = ch.config_json if hasattr(ch, 'config_json') else ch.get("config_json", "{}")
        if isinstance(config_json_raw, str):
            try:
                db_cfg = json.loads(config_json_raw)
                for k, v in db_cfg.items():
                    if v is not None:
                        cfg[k] = v
            except (json.JSONDecodeError, TypeError):
                pass
        configs.append(cfg)
    return configs


def get_avg_creation_minutes(channel_id: int, db, n: int = 5) -> float:
    """Return average generation minutes from last N uploaded videos."""
    import sqlite3
    from config.settings import DATABASE_PATH

    conn = sqlite3.connect(str(DATABASE_PATH), timeout=60)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """SELECT timing_data FROM videos 
               WHERE channel_id = ? AND status IN ('uploaded','published','uploaded_private')
               AND timing_data IS NOT NULL AND timing_data != '' AND timing_data != '{}'
               ORDER BY id DESC LIMIT ?""",
            (channel_id, n),
        ).fetchall()
    finally:
        conn.close()

    if not rows:
        return 180.0
    total_ms, count = 0, 0
    for row in rows:
        try:
            td = json.loads(row["timing_data"])
            ms = td.get("total_duration_ms", 0)
            if ms > 0:
                total_ms += ms
                count += 1
        except (json.JSONDecodeError, TypeError):
            pass
    return (total_ms / count) / 60000.0 if count else 180.0


def count_videos_today(channel_id: int, db) -> int:
    """Count videos generated, uploaded, or published today for this channel."""
    import sqlite3
    from config.settings import DATABASE_PATH

    today = date.today().isoformat()
    conn = sqlite3.connect(str(DATABASE_PATH), timeout=60)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            """SELECT COUNT(*) as cnt FROM videos 
               WHERE channel_id = ? AND date(created_at) = ?
               AND status IN ('generating','awaiting_upload','uploading','uploaded',
                              'uploaded_private','published')""",
            (channel_id, today),
        ).fetchone()
    finally:
        conn.close()
    return row["cnt"] if row else 0


def score_slot(slot: dict, channel_id: int, today: str, vpd: int,
               videos_done_today: int, awaiting_count: int) -> int:
    """Score a pending slot for prioritization. Higher = more urgent.

    Factors:
      - date_key proximity (today=100, tomorrow=70, +2=40, +3+=10)
      - deadline urgency (passed but still viable → catch-up bonus)
      - channel fairness (no videos today → bonus; quota met → penalty)
      - buffer pressure (too many awaiting → depress priority)
    """
    score = 0
    slot_date = slot["date_key"]

    # ── 1. Date proximity ──
    days_ahead = (datetime.strptime(slot_date, "%Y-%m-%d").date() - date.today()).days
    if days_ahead == 0:
        score += 100
    elif days_ahead == 1:
        score += 70
    elif days_ahead == 2:
        score += 40
    else:
        score += max(5, 30 - days_ahead * 5)

    # ── 2. Deadline urgency ──
    upload_at = slot.get("target_upload_at")
    if upload_at:
        try:
            upload_dt = datetime.strptime(str(upload_at)[:19], "%Y-%m-%d %H:%M:%S")
            if upload_dt < datetime.now():
                # Deadline passed — catch-up bonus (still viable, try to publish late)
                score += 50
        except (ValueError, TypeError):
            pass

    # ── 3. Channel fairness ──
    # Fairness bonus/penalty: only applies to TODAY's slots
    if slot_date == today:
        if videos_done_today == 0 and vpd > 0:
            score += 30  # channel hasn't produced anything today — urgent
        if vpd > 0 and videos_done_today >= vpd:
            score -= 50  # quota already met — deprioritize today's slots

    # ── 4. Buffer pressure ──
    if awaiting_count >= 3:
        score -= 30  # don't pile up too many awaiting_upload for this channel

    return score


def is_slot_viable(slot: dict, avg_gen_min: float, videos_done_today: int,
                   vpd: int) -> tuple[bool, str]:
    """Determine if a slot can realistically be fulfilled.

    Returns:
        (True, "") if viable, (False, reason) if should be cancelled.
    """
    slot_date = slot["date_key"]

    # ── Quota already met for today ──
    if slot_date == date.today().isoformat() and vpd > 0 and videos_done_today >= vpd:
        return False, f"quota met ({videos_done_today}/{vpd})"

    # ── Upload deadline hopelessly passed ──
    upload_at = slot.get("target_upload_at")
    if upload_at:
        try:
            upload_dt = datetime.strptime(str(upload_at)[:19], "%Y-%m-%d %H:%M:%S")
            deadline = upload_dt + timedelta(minutes=STALE_UPLOAD_BUFFER_MINUTES)
            if deadline < datetime.now():
                return False, "upload deadline passed"
        except (ValueError, TypeError):
            pass

    # ── Scheduled too far in the past (crash/abandoned) ──
    sched_at = slot.get("scheduled_at")
    if sched_at:
        try:
            sched_dt = datetime.strptime(str(sched_at)[:19], "%Y-%m-%d %H:%M:%S")
            if sched_dt < datetime.now() - timedelta(hours=STALE_SCHEDULED_HOURS):
                return False, f"scheduled >{STALE_SCHEDULED_HOURS}h ago"
        except (ValueError, TypeError):
            pass

    return True, ""




def replan_global(db, apply: bool = False) -> dict:
    """Execute global replan: cancel impossible slots, rescore, re-chain.

    Returns:
        dict with counts: {cancelled, kept, reordered, channels_affected}
    """
    today = date.today().isoformat()
    configs = get_channel_configs(db)

    # ── Per-channel metrics ──────────────────────────────────────
    ch_metrics = {}
    for cfg in configs:
        ch_id = cfg["channel_id"]
        avg_gen = get_avg_creation_minutes(ch_id, db)
        vpd = cfg.get("videos_per_day", 1)
        done = count_videos_today(ch_id, db)

        # Count awaiting_upload for this channel
        import sqlite3 as _sql
        from config.settings import DATABASE_PATH as _DB_PATH
        _conn = _sql.connect(str(_DB_PATH), timeout=60)
        _conn.row_factory = _sql.Row
        try:
            _row = _conn.execute(
                "SELECT COUNT(*) as cnt FROM videos WHERE channel_id=? AND status='awaiting_upload'",
                (ch_id,)
            ).fetchone()
            awaiting = _row["cnt"] if _row else 0
        finally:
            _conn.close()

        ch_metrics[ch_id] = {
            "avg_gen_min": avg_gen,
            "vpd": vpd,
            "done_today": done,
            "awaiting": awaiting,
            "slug": cfg.get("slug", "?"),
        }
        logger.info(
            "Channel %s: vpd=%d done_today=%d avg_gen=%.0fmin awaiting=%d",
            cfg["slug"], vpd, done, avg_gen, awaiting,
        )

    # ── Load all pending slots ───────────────────────────────────
    horizon_end = (date.today() + timedelta(days=7)).isoformat()
    all_slots = db.get_planned_slots_week(today, horizon_end)
    pending = [s for s in all_slots if s["status"] == "pending"]
    logger.info("Loaded %d total slots, %d pending", len(all_slots), len(pending))

    if not pending:
        logger.info("No pending slots to replan")
        return {"cancelled": 0, "kept": 0, "reordered": 0, "channels_affected": []}

    # ── Phase 1: Score and filter ────────────────────────────────
    viable = []
    cancelled_ids = []

    for slot in pending:
        ch_id = slot["channel_id"]
        m = ch_metrics.get(ch_id, {})
        avg_gen = m.get("avg_gen_min", 180.0)
        vpd = m.get("vpd", 1)
        done = m.get("done_today", 0)

        viable_flag, cancel_reason = is_slot_viable(slot, avg_gen, done, vpd)
        if viable_flag:
            slot["_score"] = score_slot(
                slot, ch_id, today, vpd, done, m.get("awaiting", 0)
            )
            slot["_avg_gen_min"] = avg_gen
            viable.append(slot)
        else:
            cancelled_ids.append(slot["id"])
            slug = m.get("slug", "?")
            logger.info(
                "CANCEL slot #%d (%s %s): %s",
                slot["id"], slug, slot["date_key"], cancel_reason,
            )

    # ── Phase 2: Sort by score descending ────────────────────────
    viable.sort(key=lambda s: (-s["_score"], s["date_key"], s.get("target_upload_at", "9999")))

    # ── Phase 3: Re-chain with realistic gaps ────────────────────
    now = datetime.now()
    anchor = now + timedelta(minutes=GLOBAL_GAP_FLOOR_MINUTES)
    next_start = anchor

    for i, slot in enumerate(viable):
        ch_dur = slot["_avg_gen_min"]
        effective_gap = max(GLOBAL_GAP_FLOOR_MINUTES, int(ch_dur * (1 + BUFFER_PCT)))

        old_sched = (str(slot.get("scheduled_at", "?")) or "?")[:16]
        new_sched = next_start.strftime("%Y-%m-%d %H:%M:%S")

        slot["_new_scheduled_at"] = new_sched
        slot["_gap_min"] = effective_gap

        ch_name = ch_metrics.get(slot["channel_id"], {}).get("slug", "?")
        score = slot.get("_score", 0)
        logger.info(
            "  %2d. #%-5d %-7s %s  score=%3d  %s → %s  (gap=%dmin)",
            i + 1, slot["id"], ch_name, slot["date_key"],
            score, old_sched, new_sched[11:16], effective_gap,
        )

        next_start = next_start + timedelta(minutes=ch_dur + effective_gap)

    # ── Compute estimated completion ─────────────────────────────
    if viable:
        total_gen_min = sum(s["_avg_gen_min"] + s["_gap_min"] for s in viable)
        est_completion = anchor + timedelta(minutes=total_gen_min)
        logger.info(
            "Estimated completion: %s (%d slots, ~%.1fh of generation)",
            est_completion.strftime("%Y-%m-%d %H:%M"),
            len(viable),
            total_gen_min / 60,
        )

    # ── Phase 4: Apply to DB ─────────────────────────────────────
    affected_channels = set()

    if apply:
        # Cancel non-viable slots
        if cancelled_ids:
            db.cancel_slots(cancelled_ids)
            logger.info("Cancelled %d slots: %s", len(cancelled_ids), cancelled_ids[:10])

        # Update scheduled_at for viable slots
        updated = 0
        for slot in viable:
            new_sched = slot["_new_scheduled_at"]
            with db._connect() as conn:
                conn.execute(
                    "UPDATE planned_slots SET scheduled_at = ? WHERE id = ?",
                    (new_sched, slot["id"]),
                )
                conn.commit()
            updated += 1
            affected_channels.add(slot["channel_id"])

        logger.info("Updated scheduled_at for %d slots across %d channels",
                     updated, len(affected_channels))
    else:
        logger.info("DRY RUN — no changes applied. Use --apply to commit.")

    return {
        "cancelled": len(cancelled_ids),
        "kept": len(viable),
        "reordered": len(viable),
        "channels_affected": [ch_metrics[c]["slug"] for c in affected_channels],
    }


def main():
    apply = "--apply" in sys.argv
    db = get_db()
    result = replan_global(db, apply=apply)

    print()
    print("=" * 60)
    print(f"  Global Replan — {'APPLIED' if apply else 'DRY RUN'}")
    print(f"  Cancelled: {result['cancelled']}  |  Kept/Reordered: {result['kept']}")
    if result["channels_affected"]:
        print(f"  Channels affected: {', '.join(result['channels_affected'])}")
    print("=" * 60)


if __name__ == "__main__":
    main()
