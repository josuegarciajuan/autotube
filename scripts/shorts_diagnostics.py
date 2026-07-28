#!/usr/bin/env python3
"""Shorts production diagnostics — compare targets vs actual output.

Usage: python3 scripts/shorts_diagnostics.py [--days 30]

Queries the shorts table and shorts_planning_config to report:
  - Per-channel daily averages over last 7/14/30 days (native + clip)
  - Yesterday detail: planned vs published by type
  - Current configs vs observed production
"""

import argparse
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path
from collections import defaultdict

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATABASE_PATH = PROJECT_ROOT / "data" / "autotube.db"

if not DATABASE_PATH.exists():
    DATABASE_PATH = PROJECT_ROOT / "autotube.db"
if not DATABASE_PATH.exists():
    # Try alternate locations
    for candidate in [
        PROJECT_ROOT / "var" / "autotube.db",
        Path("/root/autotube/data/autotube.db"),
        Path("/root/autotube/autotube.db"),
    ]:
        if candidate.exists():
            DATABASE_PATH = candidate
            break


def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DATABASE_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def get_channels(db: sqlite3.Connection) -> list[dict]:
    rows = db.execute(
        "SELECT id, name, slug FROM channels WHERE active = 1 AND slug != 'test'"
    ).fetchall()
    return [dict(r) for r in rows]


def get_shorts_configs(db: sqlite3.Connection) -> dict[int, dict]:
    rows = db.execute(
        "SELECT channel_id, shorts_native_per_day, shorts_clip_per_day, "
        "shorts_clips_per_long, shorts_enabled FROM shorts_planning_config"
    ).fetchall()
    return {r["channel_id"]: dict(r) for r in rows}


def daily_published_stats(db: sqlite3.Connection, channel_id: int,
                          days: int) -> dict:
    """Count shorts published per day for the last `days` days."""
    since = (date.today() - timedelta(days=days)).isoformat()
    rows = db.execute(
        """SELECT DATE(published_at) as pub_date, type, COUNT(*) as cnt
           FROM shorts
           WHERE channel_id = ?
             AND youtube_id IS NOT NULL
             AND status = 'published'
             AND published_at >= ?
           GROUP BY pub_date, type
           ORDER BY pub_date""",
        (channel_id, since),
    ).fetchall()

    daily = defaultdict(lambda: {"native": 0, "clip": 0, "total": 0})
    for r in rows:
        d = r["pub_date"]
        daily[d][r["type"]] = r["cnt"]
        daily[d]["total"] += r["cnt"]

    # Compute averages
    active_days = len(daily)
    total_native = sum(v["native"] for v in daily.values())
    total_clip = sum(v["clip"] for v in daily.values())
    total_all = total_native + total_clip

    return {
        "window_days": days,
        "active_days": active_days,
        "total_native": total_native,
        "total_clip": total_clip,
        "total": total_all,
        "avg_native_per_day": round(total_native / max(active_days, 1), 1),
        "avg_clip_per_day": round(total_clip / max(active_days, 1), 1),
        "avg_total_per_day": round(total_all / max(active_days, 1), 1),
        "daily_breakdown": dict(daily) if days <= 14 else None,
    }


def yesterday_detail(db: sqlite3.Connection, channel_id: int) -> dict:
    """Detailed yesterday breakdown: planned slots vs actually published."""
    yesterday = (date.today() - timedelta(days=1)).isoformat()

    # Planned slots for yesterday
    planned = db.execute(
        """SELECT short_type, status, COUNT(*) as cnt
           FROM shorts_planned_slots
           WHERE channel_id = ? AND date_key = ?
           GROUP BY short_type, status""",
        (channel_id, yesterday),
    ).fetchall()

    # Published shorts yesterday
    published = db.execute(
        """SELECT type, COUNT(*) as cnt
           FROM shorts
           WHERE channel_id = ?
             AND youtube_id IS NOT NULL
             AND status = 'published'
             AND DATE(published_at) = ?""",
        (channel_id, yesterday),
    ).fetchall()

    planned_by_type = defaultdict(lambda: defaultdict(int))
    for r in planned:
        planned_by_type[r["short_type"]][r["status"]] = r["cnt"]

    published_by_type = {r["type"]: r["cnt"] for r in published}

    return {
        "date": yesterday,
        "planned": {t: dict(st) for t, st in planned_by_type.items()},
        "published": published_by_type,
    }


def print_separator(char="─", width=80):
    print(char * width)


def main():
    parser = argparse.ArgumentParser(description="Shorts production diagnostics")
    parser.add_argument("--days", type=int, default=30,
                       help="Days to look back (default: 30)")
    parser.add_argument("--yesterday", action="store_true",
                       help="Show detailed yesterday breakdown")
    args = parser.parse_args()

    if not DATABASE_PATH.exists():
        print(f"ERROR: Database not found at {DATABASE_PATH}")
        return

    db = get_db()
    channels = get_channels(db)
    configs = get_shorts_configs(db)

    windows = [7, 14, 30]

    print_separator("=")
    print("  SHORTS PRODUCTION DIAGNOSTICS")
    print(f"  Database: {DATABASE_PATH}")
    print(f"  Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print_separator("=")

    for ch in channels:
        ch_id = ch["id"]
        slug = ch["slug"]
        name = ch.get("name", slug)
        cfg = configs.get(ch_id, {})

        print(f"\n▶ {name} ({slug})  [channel_id={ch_id}]")
        print(f"  Config: native={cfg.get('shorts_native_per_day', '?')}/day, "
              f"clips_per_long={cfg.get('shorts_clips_per_long', '?')}, "
              f"enabled={cfg.get('shorts_enabled', '?')}")

        # Averages per window
        print(f"  {'Window':<10} {'Days':<8} {'Native':<8} {'Clips':<8} {'Total':<8} {'Avg/day':<10}")
        print(f"  {'-'*10} {'-'*8} {'-'*8} {'-'*8} {'-'*8} {'-'*10}")
        for w in windows:
            stats = daily_published_stats(db, ch_id, w)
            print(f"  {f'{w}d':<10} {stats['active_days']:<8} "
                  f"{stats['total_native']:<8} {stats['total_clip']:<8} "
                  f"{stats['total']:<8} {stats['avg_total_per_day']:<10}")

        # Target comparison (30d)
        stats_30 = daily_published_stats(db, ch_id, 30)
        native_target = cfg.get("shorts_native_per_day", 0) or 0
        clips_target = cfg.get("shorts_clips_per_long", 0) or 0
        print(f"  Target: native={native_target}/day + clips={clips_target}×longs")
        gap = (native_target + clips_target) - stats_30["avg_total_per_day"]
        if gap > 0:
            print(f"  ⚠ GAP: {gap:.1f} shorts/day below target")
        elif gap < 0:
            print(f"  ✓ Above target by {-gap:.1f} shorts/day")
        else:
            print(f"  ✓ On target")

    # Yesterday detail
    if args.yesterday:
        print_separator()
        print("  YESTERDAY DETAIL (per channel)")
        print_separator()
        for ch in channels:
            ch_id = ch["id"]
            slug = ch["slug"]
            yd = yesterday_detail(db, ch_id)
            print(f"\n▶ {slug} — {yd['date']}")
            print(f"  Planned slots: {yd['planned']}")
            print(f"  Published:      {yd['published']}")

    # Global summary
    print_separator("=")
    print("  GLOBAL SUMMARY (30-day)")
    print_separator("=")
    total_native = 0
    total_clip = 0
    for ch in channels:
        s = daily_published_stats(db, ch["id"], 30)
        total_native += s["total_native"]
        total_clip += s["total_clip"]
    total = total_native + total_clip
    print(f"  All channels: {total} shorts in 30 days")
    print(f"    Native: {total_native} ({total_native / max(total, 1) * 100:.0f}%)")
    print(f"    Clips:  {total_clip} ({total_clip / max(total, 1) * 100:.0f}%)")
    print(f"    Avg/day: {total / 30:.1f} across {len(channels)} channels")
    print(f"    Per channel/day: {total / 30 / max(len(channels), 1):.1f}")
    print()

    # Quota estimate
    print_separator("=")
    print("  QUOTA ESTIMATE (at 15 shorts/day/channel)")
    print_separator("=")
    shorts_per_ch = 15
    quota_per_upload = 1600
    quota_per_thumbnail = 50
    daily_per_channel = shorts_per_ch * (quota_per_upload + quota_per_thumbnail)
    total_daily = daily_per_channel * len(channels)
    print(f"  {shorts_per_ch} shorts/day × (1600 upload + 50 thumbnail)")
    print(f"  = {daily_per_channel:,} units/day/channel")
    print(f"  × {len(channels)} active channels = {total_daily:,} units/day total")
    print(f"  Default YouTube quota: 10,000/day")
    print(f"  ⚠ Over quota by {total_daily / 10000:.1f}× — quota increase required")
    print()

    db.close()


if __name__ == "__main__":
    main()
