#!/usr/bin/env python3
"""Cohort report for growth experiments (read-only).

Compares two date windows for one channel and format. Useful for E1-E5 when a
proper A/B is not available.

Usage:
    python3 scripts/growth_experiment_report.py --slug canal5 --format long \
        --a 2026-10-10:2026-11-06 --b 2026-11-07:2026-12-04
"""
from __future__ import annotations

import argparse
import os
import sqlite3
from pathlib import Path
from statistics import median

ROOT = Path(__file__).resolve().parent.parent


def db_path() -> str:
    return os.getenv("DATABASE_PATH", str(ROOT / "autotube.db"))


def cohort(conn, cid: int, start: str, end: str, fmt: str) -> dict:
    if fmt == "long":
        join = "JOIN videos v ON v.channel_id = r.channel_id AND v.yt_video_id = r.yt_video_id"
    else:
        join = "JOIN shorts s ON s.channel_id = r.channel_id AND s.youtube_id = r.yt_video_id"
    rows = conn.execute(
        f"""SELECT r.yt_video_id,
                   SUM(r.impressions) imp, SUM(r.views) views,
                   SUM(r.watch_minutes) watch, SUM(r.subs_gained) subs,
                   SUM(CASE WHEN r.impressions > 0
                            THEN r.impressions * r.impressions_ctr ELSE 0 END) ctr_num
            FROM video_reach_daily r {join}
            WHERE r.channel_id = ? AND r.date BETWEEN ? AND ?
            GROUP BY r.yt_video_id""",
        (cid, start, end),
    ).fetchall()
    if not rows:
        return {"videos": 0}
    imps = [int(r["imp"] or 0) for r in rows]
    total = sum(imps)
    return {
        "videos": len(rows),
        "views": sum(int(r["views"] or 0) for r in rows),
        "impressions": total,
        "median_impressions": median(imps),
        "watch_minutes": round(sum(float(r["watch"] or 0) for r in rows), 1),
        "subs": sum(int(r["subs"] or 0) for r in rows),
        "weighted_ctr_pct": round(sum(float(r["ctr_num"] or 0) for r in rows) / total, 2) if total else None,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--slug", required=True)
    ap.add_argument("--format", choices=["long", "short"], default="long")
    ap.add_argument("--a", required=True, help="START:END")
    ap.add_argument("--b", required=True, help="START:END")
    args = ap.parse_args()
    a_start, a_end = args.a.split(":", 1)
    b_start, b_end = args.b.split(":", 1)

    conn = sqlite3.connect(f"file:{db_path()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    ch = conn.execute("SELECT id, name FROM channels WHERE slug=?", (args.slug,)).fetchone()
    if not ch:
        raise SystemExit(f"channel {args.slug} not found")
    print(f"# {ch['name']} — {args.format}\n")
    for label, (start, end) in (("A", (a_start, a_end)), ("B", (b_start, b_end))):
        data = cohort(conn, ch["id"], start, end, args.format)
        print(f"## {label}: {start} → {end}")
        for k, v in data.items():
            print(f"- {k}: {v}")
        print()
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
