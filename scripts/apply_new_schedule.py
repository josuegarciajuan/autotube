#!/usr/bin/env python3
"""
Apply new publishing schedule (replan daily videos + shorts).
Clears old pending slots and regenerates for today + next 7 days.

New schedule:
  ┌──────────┬───────────────┬───────────────────┬────────┬──────┐
  │ Canal    │ videos/dia    │ alternate_pattern │ native │ clip │
  ├──────────┼───────────────┼───────────────────┼────────┼──────┤
  │ canal2   │ 2 (fixed)     │ —                 │ 3      │ 1    │
  │ canal3   │ 2/3 (altern)  │ [2,3] offset=0    │ 3      │ 2    │
  │ canal4   │ 3 (fixed)     │ —                 │ 2      │ 2    │
  │ canal5   │ 2 (fixed)     │ —                 │ 2      │ 1    │
  └──────────┴───────────────┴───────────────────┴────────┴──────┘
"""

import logging
import sys
from datetime import date, timedelta

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")
logger = logging.getLogger("apply_schedule")


def main():
    from database.db_extended import ExtendedDatabase
    db = ExtendedDatabase()

    # ── 1. Channel map (slug → DB id) ────────────────────────────
    channels = {ch["slug"]: ch for ch in db.get_channels()}
    slugs = ["canal2", "canal3", "canal4", "canal5"]
    for s in slugs:
        if s not in channels:
            logger.error("Channel %s not found in DB!", s)
            return 1

    ch2 = channels["canal2"]
    ch3 = channels["canal3"]
    ch4 = channels["canal4"]
    ch5 = channels["canal5"]
    logger.info(
        "Channels: canal2=%d canal3=%d canal4=%d canal5=%d",
        ch2["id"], ch3["id"], ch4["id"], ch5["id"],
    )

    # ── 2. Sync Python configs → DB (PUBLISH_MODE=scheduled etc.) ─
    logger.info("Syncing Python configs → DB…")
    from config.config_bridge import sync_all_configs_to_db
    sync_all_configs_to_db()

    # ── 3. Force PUBLISH_MODE into DB config_json ────────────────
    # sync_all_configs_to_db may not propagate all vars.
    # Ensure DB reflects the new Python config values.
    import json as _json
    for slug, mode in [("canal3", "scheduled"), ("canal4", "scheduled"), ("canal5", "scheduled")]:
        ch = channels[slug]
        try:
            cj = _json.loads(ch.get("config_json", "{}"))
        except (_json.JSONDecodeError, TypeError):
            cj = {}
        cj["PUBLISH_MODE"] = mode
        cj["PUBLISH_TIMEZONE"] = cj.get("PUBLISH_TIMEZONE", "Europe/Madrid")
        cj["PUBLISH_JITTER_MIN"] = cj.get("PUBLISH_JITTER_MIN", 20)
        cj["PUBLISH_WARMUP_MIN"] = cj.get("PUBLISH_WARMUP_MIN", 120)
        db.update_channel(ch["id"], config=cj)
        logger.info("  Forced PUBLISH_MODE=%s in DB for %s (id=%d)", mode, slug, ch["id"])

    # ── 4. Update long-form planning configs ─────────────────────
    planning_updates = [
        # canal2: 2 videos/day fixed, all original
        (ch2["id"], dict(videos_per_day=2, alternate_pattern=None, alternate_offset=0, viral_per_day=0)),
        # canal3: alternating 2/3, desfasado even=2 odd=3, all original
        (ch3["id"], dict(videos_per_day=2, alternate_pattern=[2, 3], alternate_offset=0, viral_per_day=0)),
        # canal4: 3 videos/day fixed, ALL VIRAL (mirror content)
        (ch4["id"], dict(videos_per_day=3, alternate_pattern=None, alternate_offset=0, viral_per_day=3)),
        # canal5: 2 videos/day fixed, all original
        (ch5["id"], dict(videos_per_day=2, alternate_pattern=None, alternate_offset=0, viral_per_day=0)),
    ]

    for ch_id, upd in planning_updates:
        db.update_channel_planning_config(
            ch_id,
            videos_per_day=upd["videos_per_day"],
            alternate_pattern=upd["alternate_pattern"],
            alternate_offset=upd["alternate_offset"],
            planning_enabled=True,
            viral_per_day=upd.get("viral_per_day", 0),
        )
        logger.info(
            "  Updated planning config for ch=%d: vpd=%s pattern=%s offset=%s viral=%s",
            ch_id, upd["videos_per_day"], upd["alternate_pattern"], upd["alternate_offset"],
            upd.get("viral_per_day", 0),
        )

    # ── 5. Update shorts planning configs ────────────────────────
    shorts_updates = [
        (ch2["id"], dict(shorts_native_per_day=3, shorts_clip_per_day=1)),
        (ch3["id"], dict(shorts_native_per_day=3, shorts_clip_per_day=2)),
        (ch4["id"], dict(shorts_native_per_day=2, shorts_clip_per_day=2)),
        (ch5["id"], dict(shorts_native_per_day=2, shorts_clip_per_day=1)),
    ]

    for ch_id, upd in shorts_updates:
        db.update_shorts_planning_config(ch_id, upd)
        logger.info(
            "  Updated shorts config for ch=%d: native=%d clip=%d",
            ch_id, upd["shorts_native_per_day"], upd["shorts_clip_per_day"],
        )

    # ── 6. Delete pending planned_slots and shorts_planned_slots ─
    import sqlite3
    from config.settings import DATABASE_PATH
    conn = sqlite3.connect(str(DATABASE_PATH), timeout=30)

    for tbl in ("planned_slots", "shorts_planned_slots"):
        cursor = conn.execute(
            f"DELETE FROM {tbl} WHERE status = 'pending'"
        )
        logger.info("  Deleted %d pending rows from %s", cursor.rowcount, tbl)

    conn.commit()
    conn.close()

    # ── 7. Regenerate slots for today + next 7 days ──────────────
    today = date.today()

    # ── Long-form slots ─────────────────────────────────────────
    from api.services.planning_service import compute_and_store_slots
    for i in range(8):
        d = (today + timedelta(days=i)).isoformat()
        n = compute_and_store_slots(d, db)
        total = n.get("total_slots", 0) if isinstance(n, dict) else n
        logger.info("  [%s] Stored %d long-form planned slots", d, total)

    # ── Shorts slots ────────────────────────────────────────────
    from api.services.shorts_scheduler import generate_upcoming_shorts
    result = generate_upcoming_shorts(days=8, db=db)
    logger.info(
        "  Shorts: %d slots across %d days",
        result.get("total_slots", 0),
        result.get("days_processed", 0),
    )

    # ── 8. Verify ────────────────────────────────────────────────
    logger.info("")
    logger.info("═══ VERIFICATION ═══")
    logger.info("")

    for slug in slugs:
        ch = channels[slug]
        pc = db.get_channel_planning_config(ch["id"])
        sc_list = db.get_shorts_planning_config(ch["id"])
        sc = sc_list[0] if sc_list else {}
        today_slots = db.get_planned_slots(date_key=today.isoformat())
        ch_today = [s for s in today_slots if s["channel_id"] == ch["id"]]

        pat = pc.get("alternate_pattern")
        pattern_str = f"[{','.join(map(str, pat))}]" if pat else "fixed"
        logger.info(
            "  %s (%s): mode=%s long=%s/%s shorts=native=%d/clip=%d today_slots=%d",
            slug, ch.get("name", ""), pc.get("publish_mode"),
            pc.get("videos_per_day"), pattern_str,
            sc.get("shorts_native_per_day", "?"), sc.get("shorts_clip_per_day", "?"),
            len(ch_today),
        )

        # Print today's slot times for this channel
        for s in ch_today[:5]:
            logger.info(
                "    slot: sched=%s target=%s",
                s.get("scheduled_at", "?")[:16],
                s.get("target_upload_at", "?")[:16],
            )

    logger.info("")
    logger.info("✓ Schedule migration complete.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
