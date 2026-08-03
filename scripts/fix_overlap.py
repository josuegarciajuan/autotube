#!/usr/bin/env python3
"""Fix overlapping and too-close scheduled publish times for any Autotube channel.

Detects videos sharing the exact same target_public_at AND videos within 3h of each
other on the same day, redistributing with minimum gap and max publications per day.

Usage:
  python3 scripts/fix_overlap.py                           # Auto-detect and fix ALL channels
  python3 scripts/fix_overlap.py --channel canal4          # Fix specific channel
  python3 scripts/fix_overlap.py --dry-run                 # Preview changes only  
  python3 scripts/fix_overlap.py --max-per-day 2           # Max 2 publications/day (default 3)
  python3 scripts/fix_overlap.py --min-gap 4               # Min 4h gap (default 3)
"""

import argparse
import logging
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

MIN_GAP_HOURS = 3
MAX_PER_DAY = 3
DEFAULT_TIMEZONE = "Europe/Madrid"

NICHO_PEAK_HOURS = {
    "misterio_paranormal": 21,
    "historia_documental": 20,
    "educacion_ciencia": 18,
    "entretenimiento_general": 20,
}


def _get_peak_hour(channel: dict) -> int:
    try:
        from config.config_bridge import get_channel_config
        cfg = get_channel_config(channel["slug"])
        if cfg and cfg.get("SEO_PRIMARY_KEYWORD"):
            kw = cfg["SEO_PRIMARY_KEYWORD"].lower()
            if any(w in kw for w in ["paranormal","fantasma","misterio","inexplicable","milagro","sincronia"]):
                return 21
            if any(w in kw for w in ["historia","documental","expedicion","civilizacion","antigu","enfermedad","medic","raro"]):
                return 20
            if any(w in kw for w in ["ciencia","educacion","cientifico","experimento"]):
                return 18
    except Exception:
        pass
    return 20


def _parse_tpa(tpa_str: str) -> datetime:
    if not tpa_str:
        return None
    s = str(tpa_str).strip()
    for fmt in ["%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"]:
        try:
            return datetime.strptime(s[:19], fmt).replace(tzinfo=timezone.utc)
        except Exception:
            continue
    try:
        if s.endswith("Z"):
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
    except Exception:
        pass
    try:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is not None:
            return dt.astimezone(timezone.utc)
    except Exception:
        pass
    return None


def _local_date(dt: datetime, tz_str: str = DEFAULT_TIMEZONE) -> str:
    import pytz
    return dt.astimezone(pytz.timezone(tz_str)).strftime("%Y-%m-%d")


def _build_utc(local_date: str, hour: int, minute: int,
               tz_str: str = DEFAULT_TIMEZONE) -> datetime:
    import pytz
    tz = pytz.timezone(tz_str)
    local = datetime.strptime(local_date, "%Y-%m-%d").replace(
        hour=hour, minute=minute, second=0, microsecond=0)
    return tz.localize(local).astimezone(timezone.utc)


def _canonical(tpa_str: str) -> str:
    return str(tpa_str)[:19] if tpa_str else ""


def main(dry_run=False, channel_slug=None, min_gap_hours=MIN_GAP_HOURS,
         max_per_day=MAX_PER_DAY):
    from database.db_extended import ExtendedDatabase
    from pipeline.youtube_uploader import YouTubeUploader
    import pytz

    tz = pytz.timezone(DEFAULT_TIMEZONE)
    mode = "DRY RUN" if dry_run else "LIVE"

    logger.info("=" * 60)
    logger.info("fix_overlap — %s | gap=%dh | max=%d/day", mode, min_gap_hours, max_per_day)
    logger.info("=" * 60)

    db = ExtendedDatabase()

    if channel_slug:
        channel = db.get_channel_by_slug(channel_slug)
        if not channel:
            logger.error("Channel '%s' not found", channel_slug)
            return 1
        channels = [channel]
    else:
        channels = db.get_channels()

    total_updated = 0
    total_errors = 0

    for channel in channels:
        slug = channel["slug"]
        channel_id = channel["id"]
        peak_hour = _get_peak_hour(channel)
        logger.info("\n[%s] ── Processing (peak=%02d:00 Madrid) ──", slug, peak_hour)

        with db._connect() as conn:
            rows = conn.execute("""
                SELECT * FROM videos
                WHERE channel_id = ?
                  AND target_public_at IS NOT NULL
                  AND status IN ('uploaded_private','uploading','warming','scheduled')
                ORDER BY target_public_at, id
            """, (channel_id,)).fetchall()

        videos = [dict(r) for r in rows]
        if not videos:
            logger.info("[%s] No scheduled videos.", slug)
            continue

        logger.info("[%s] %d videos with target_public_at", slug, len(videos))

        # ── Step 1: Group by EXACT target_public_at and fix overlaps ──
        groups = defaultdict(list)
        for v in videos:
            v["_parsed_dt"] = _parse_tpa(v.get("target_public_at"))
            if v["_parsed_dt"] is None:
                logger.warning("[%s] Video #%d: cannot parse '%s'",
                               slug, v["id"], v.get("target_public_at"))
                continue
            key = _canonical(v.get("target_public_at"))
            groups[key].append(v)

        overlap_groups = {k: vlist for k, vlist in groups.items() if len(vlist) > 1}
        new_targets = {}

        if overlap_groups:
            logger.info("[%s] %d overlap group(s) found:", slug, len(overlap_groups))
            for tpa_key, vlist in overlap_groups.items():
                ids = [v["id"] for v in vlist]
                logger.info("  %s: %d videos [%s]", tpa_key, len(vlist),
                            ", ".join(f"#{i}" for i in ids))

        for tpa_key, vlist in overlap_groups.items():
            vlist_sorted = sorted(vlist, key=lambda v: v["id"])
            base_dt = vlist_sorted[0]["_parsed_dt"]
            for i, v in enumerate(vlist_sorted):
                new_targets[v["id"]] = base_dt + timedelta(hours=i * min_gap_hours)

        # Build all_parsed with effective target times
        all_parsed = []
        for v in videos:
            vid = v["id"]
            if vid in new_targets:
                eff_dt = new_targets[vid]
            else:
                eff_dt = v["_parsed_dt"]
                new_targets[vid] = eff_dt

            all_parsed.append({
                "video": v,
                "vid": vid,
                "current_dt": v["_parsed_dt"],
                "new_dt": eff_dt,
                "is_modified": (vid in new_targets
                                and abs((eff_dt - v["_parsed_dt"]).total_seconds()) > 60),
            })

        # ── Step 2: Enforce MIN_GAP between ALL same-day videos ──
        # This catches the gap violations that survive the overlap fix
        # (e.g., an overlap-fixed time of 14:44 clashing with an existing 15:00 video)
        gap_delta = timedelta(hours=min_gap_hours)
        gap_fixes = 0

        # Group by local date
        by_date = defaultdict(list)
        for p in all_parsed:
            dk = _local_date(p["new_dt"])
            by_date[dk].append(p)

        for dk in list(by_date.keys()):
            day_vids = sorted(by_date[dk], key=lambda p: p["new_dt"])
            
            for i in range(1, len(day_vids)):
                prev = day_vids[i - 1]
                curr = day_vids[i]
                min_allowed = prev["new_dt"] + gap_delta
                
                if curr["new_dt"] < min_allowed:
                    old_dt = curr["new_dt"]
                    curr["new_dt"] = min_allowed
                    curr["is_modified"] = True
                    gap_fixes += 1
                    logger.debug(
                        "[%s] Gap fix: #%d %s → %s (was %s)",
                        slug, curr["vid"],
                        curr["new_dt"].isoformat()[:19],
                        old_dt.isoformat()[:19],
                        prev["vid"],
                    )

        if gap_fixes:
            logger.info("[%s] %d gap violation(s) fixed (videos too close on same day).", slug, gap_fixes)

        # ── Step 3: Enforce max_per_day ──
        # Re-group after gap fixes
        by_date = defaultdict(list)
        for p in all_parsed:
            dk = _local_date(p["new_dt"])
            by_date[dk].append(p)

        all_dates = sorted(by_date.keys())
        overflow = []

        for dk in all_dates:
            day_vids = sorted(by_date[dk], key=lambda p: p["new_dt"])
            over = len(day_vids) - max_per_day
            if over > 0:
                keep = day_vids[:max_per_day]
                push = day_vids[max_per_day:]
                logger.info("[%s] %s: %d → keep %d, push %d",
                            slug, dk, len(day_vids), len(keep), len(push))
                by_date[dk] = keep
                overflow.extend(push)

        # ── Step 4: Assign overflow to future dates at peak hour ──
        if overflow:
            if all_dates:
                last_date = datetime.strptime(all_dates[-1], "%Y-%m-%d")
            else:
                last_date = datetime.now(tz).replace(hour=0, minute=0, second=0, microsecond=0)

            next_d = last_date + timedelta(days=1)

            while overflow:
                chunk = overflow[:max_per_day]
                overflow = overflow[max_per_day:]

                for j, p in enumerate(chunk):
                    local_h = peak_hour + j * min_gap_hours
                    extra_d = local_h // 24
                    local_h = local_h % 24
                    adj_date = next_d + timedelta(days=extra_d)
                    adj_str = adj_date.strftime("%Y-%m-%d")
                    p["new_dt"] = _build_utc(adj_str, local_h, 0)
                    p["is_modified"] = True
                    logger.info("[%s] Video #%d → overflow %s (%02d:00 Madrid)",
                                slug, p["vid"],
                                p["new_dt"].isoformat()[:19], local_h)

                next_d += timedelta(days=1)

        # ── Step 5: Final gap enforcement after overflow placement ──
        # Overflow videos placed on the same day at peak_hour + j*3h are fine
        # (they're already 3h apart), but check for edge cases.
        by_date_final = defaultdict(list)
        for p in all_parsed:
            dk = _local_date(p["new_dt"])
            by_date_final[dk].append(p)

        for dk in list(by_date_final.keys()):
            day_vids = sorted(by_date_final[dk], key=lambda p: p["new_dt"])
            for i in range(1, len(day_vids)):
                prev = day_vids[i - 1]
                curr = day_vids[i]
                if curr["new_dt"] < prev["new_dt"] + gap_delta:
                    curr["new_dt"] = prev["new_dt"] + gap_delta
                    curr["is_modified"] = True

        # ── Step 6: Collect final changes ──
        changes = [p for p in all_parsed if p["is_modified"]]

        if not changes:
            logger.info("[%s] ✅ All good.", slug)
            continue

        logger.info("[%s] 🔧 %d video(s) need adjustment:", slug, len(changes))
        for p in changes:
            v = p["video"]
            logger.info("  #%d: %s → %s (%s Madrid)",
                        p["vid"],
                        _canonical(v.get("target_public_at", "?")),
                        p["new_dt"].isoformat()[:19],
                        p["new_dt"].astimezone(tz).strftime("%H:%M"))

        # ── Step 7: Apply ──
        uploader = None
        try:
            uploader = YouTubeUploader(account_name=slug, channel_slug=slug, db=db)
        except Exception as e:
            logger.warning("[%s] Uploader init: %s — DB-only", slug, e)

        channel_errors = 0

        for p in changes:
            v = p["video"]
            new_iso = p["new_dt"].strftime("%Y-%m-%dT%H:%M:%S+00:00")
            yt_id = v.get("yt_video_id")
            vid = p["vid"]
            old_tpa = v.get("target_public_at")

            if yt_id and uploader:
                if dry_run:
                    logger.info("  [DRY RUN] #%d YT publishAt=%s", vid, new_iso)
                else:
                    try:
                        svc = uploader._get_service()
                        svc.videos().update(part="status", body={
                            "id": yt_id,
                            "status": {"privacyStatus": "private", "publishAt": new_iso},
                        }).execute()
                        logger.info("  ✅ #%d YT updated", vid)
                    except Exception as exc:
                        logger.error("  ❌ #%d YT: %s", vid, exc)
                        channel_errors += 1
            elif not yt_id:
                logger.info("  ⚠️ #%d: no yt_video_id — skip YT API", vid)

            if dry_run:
                logger.info("  [DRY RUN] #%d DB: target=%s", vid, new_iso)
                total_updated += 1
            else:
                try:
                    db.update_video(vid, target_public_at=new_iso)
                    with db._connect() as conn:
                        conn.execute(
                            "UPDATE planned_slots SET target_public_at=? "
                            "WHERE video_id=? AND target_public_at=?",
                            (new_iso, vid, old_tpa))
                        conn.execute(
                            "UPDATE video_lifecycle_actions SET scheduled_for=? "
                            "WHERE video_id=? AND scheduled_for=?",
                            (new_iso, vid, old_tpa))
                        conn.commit()
                    logger.info("  ✅ #%d DB updated", vid)
                    total_updated += 1
                except Exception as exc:
                    logger.error("  ❌ #%d DB: %s", vid, exc)
                    channel_errors += 1

        total_errors += channel_errors

    logger.info("\n" + "=" * 60)
    logger.info("SUMMARY: %d updated, %d errors", total_updated, total_errors)
    logger.info("=" * 60)
    if dry_run:
        logger.info("DRY RUN — no changes. Rerun without --dry-run to apply.")

    return 0 if total_errors == 0 else 1


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="Fix overlapping publish times")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--channel", type=str, default=None)
    p.add_argument("--min-gap", type=int, default=MIN_GAP_HOURS)
    p.add_argument("--max-per-day", type=int, default=MAX_PER_DAY)
    args = p.parse_args()
    sys.exit(main(args.dry_run, args.channel, args.min_gap, args.max_per_day))
