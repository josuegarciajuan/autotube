#!/usr/bin/env python3
"""Repair inconsistent pending-upload videos (publish-before-upload / upload-too-far).

Scans `awaiting_upload` videos and reports/repairs:
  1. stale `target_public_at` (past or within warmup) → recalculated to next peak.
  2. far-future `scheduled_upload_at` (>12h) → reset to NULL (upload scheduler
     recomputes the next available window).

Usage:
    python3 scripts/fix_pending_upload_times.py            # dry-run report
    python3 scripts/fix_pending_upload_times.py --apply    # actually fix

This mirrors the automatic self-heal `_recover_inconsistent_upload_times` in
api/services/upload_scheduler.py, which runs every scheduler tick going forward.
"""

import argparse
import json
import sys
from datetime import datetime, timedelta

_PROJECT_ROOT = None
for _p in [__file__.rsplit("/scripts/", 1)[0], "."]:
    pass
import os
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from database.db_extended import ExtendedDatabase
from pipeline.publish_scheduler import (
    _target_is_stale, ensure_future_target_public_at, _parse_target_public_at,
)

FAR_FUTURE_HOURS = 12


def scan(db):
    """Return list of broken video dicts (id, slug, sched, tpa, problems)."""
    with db._connect() as conn:
        rows = conn.execute(
            """SELECT v.id, v.channel_id, v.scheduled_upload_at, v.target_public_at,
                      v.titulo_final, c.slug, c.config_json
               FROM videos v
               JOIN channels c ON c.id = v.channel_id
               WHERE v.status = 'awaiting_upload'
                 AND v.video_path IS NOT NULL AND v.video_path != ''
                 AND (v.yt_video_id IS NULL OR v.yt_video_id = '')
               ORDER BY v.scheduled_upload_at ASC
            """
        ).fetchall()

    now = datetime.now()
    broken = []
    for r in rows:
        cfg = {}
        try:
            cfg = json.loads(r["config_json"] or "{}")
        except (json.JSONDecodeError, TypeError):
            pass
        tz = cfg.get("PUBLISH_TIMEZONE", "Europe/Madrid")
        warmup = int(cfg.get("PUBLISH_WARMUP_MIN", 120)) or 120

        problems = []
        tpa = r["target_public_at"]
        sched = r["scheduled_upload_at"]

        if _target_is_stale(tpa, timezone_str=tz, warmup_min=warmup):
            problems.append("target_public_at stale")
        if sched:
            try:
                sdt = datetime.strptime(str(sched)[:19], "%Y-%m-%d %H:%M:%S")
                if sdt > now + timedelta(hours=FAR_FUTURE_HOURS):
                    problems.append(f"scheduled_upload_at >{FAR_FUTURE_HOURS}h ahead")
                elif sdt < now - timedelta(hours=24):
                    problems.append("scheduled_upload_at >24h past")
            except (ValueError, TypeError):
                problems.append("scheduled_upload_at unparseable")

        if problems:
            broken.append({
                "id": r["id"], "slug": r["slug"], "channel_id": r["channel_id"],
                "titulo": (r["titulo_final"] or "")[:50],
                "sched": r["scheduled_upload_at"], "tpa": r["target_public_at"],
                "tz": tz, "warmup": warmup,
                "problems": problems,
            })
    return broken


def fix(db, broken):
    fixed = 0
    for v in broken:
        vid = v["id"]
        slug = v["slug"]
        # Recalc target_public_at whenever either condition holds — keeps both
        # fields consistent when scheduled_upload_at is reset to NULL (upload ASAP).
        new_tpa = ensure_future_target_public_at(
            v["tpa"], slug=slug, timezone_str=v["tz"],
            db=db, channel_id=v["channel_id"], warmup_min=v["warmup"], jitter_min=0,
        )
        db.update_video(vid, target_public_at=new_tpa)
        with db._connect() as conn:
            conn.execute(
                "UPDATE planned_slots SET target_public_at = ? WHERE video_id = ?",
                (new_tpa, vid),
            )
            conn.execute(
                "UPDATE video_lifecycle_actions SET scheduled_for = ? "
                "WHERE video_id = ? AND action_type = 'go_public'",
                (new_tpa, vid),
            )
            conn.commit()
        print(f"  ✓ #{vid} ({slug}): target_public_at → {new_tpa[:19]}")
        if any("scheduled_upload_at" in p for p in v["problems"]):
            # update_video() ignores None, so write NULL via raw SQL
            with db._connect() as conn:
                conn.execute(
                    "UPDATE videos SET scheduled_upload_at = NULL WHERE id = ?",
                    (vid,),
                )
                conn.commit()
            print(f"  ✓ #{vid} ({slug}): scheduled_upload_at → NULL (recompute)")
        fixed += 1
    return fixed


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="actually apply fixes (default: dry-run)")
    args = ap.parse_args()

    db = ExtendedDatabase()
    broken = scan(db)

    if not broken:
        print("No inconsistent pending-upload videos found.")
        return

    print(f"Found {len(broken)} inconsistent pending-upload video(s):\n")
    for v in broken:
        print(f"  #{v['id']} [{v['slug']}] {v['titulo']}")
        print(f"      sched_up = {v['sched']}")
        print(f"      target_pub = {v['tpa']}")
        print(f"      problems = {', '.join(v['problems'])}")
    print()

    if not args.apply:
        print("Dry-run: no changes made. Run with --apply to fix.")
        return

    fixed = fix(db, broken)
    print(f"\nFixed {fixed} video(s).")


if __name__ == "__main__":
    main()
