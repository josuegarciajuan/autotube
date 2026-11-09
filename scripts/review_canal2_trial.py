#!/usr/bin/env python3
"""Review the canal2 long-form cadence trial (read-only).

Compares long-form reach before/after 2026-11-09 and applies the pre-registered
decision rule:
  * median impressions/video < 100  -> keep 1/day or pause
  * 100-300                         -> hold 14 more days
  * > 300                           -> restore 2/day

Usage:
    python3 scripts/review_canal2_trial.py
    python3 scripts/review_canal2_trial.py --slug canal2 --since 2026-11-09
"""
from __future__ import annotations

import argparse
import os
import sqlite3
from datetime import date
from pathlib import Path
from statistics import median

ROOT = Path(__file__).resolve().parent.parent


def db_path() -> str:
    return os.getenv("DATABASE_PATH", str(ROOT / "autotube.db"))


def cohort(conn, channel_id: int, start: str, end: str) -> dict:
    rows = conn.execute(
        """SELECT r.yt_video_id,
                  SUM(r.impressions) imp, SUM(r.views) views,
                  SUM(r.watch_minutes) watch,
                  SUM(CASE WHEN r.impressions > 0
                           THEN r.impressions * r.impressions_ctr ELSE 0 END) ctr_num
           FROM video_reach_daily r
           JOIN videos v ON v.channel_id = r.channel_id
                        AND v.yt_video_id = r.yt_video_id
           WHERE r.channel_id = ? AND r.date BETWEEN ? AND ?
           GROUP BY r.yt_video_id""",
        (channel_id, start, end),
    ).fetchall()
    if not rows:
        return {"videos": 0}
    imps = [int(r["imp"] or 0) for r in rows]
    total_imp = sum(imps)
    return {
        "videos": len(rows),
        "views": sum(int(r["views"] or 0) for r in rows),
        "impressions": total_imp,
        "median_impressions": median(imps),
        "watch_minutes": round(sum(float(r["watch"] or 0) for r in rows), 1),
        "weighted_ctr_pct": (
            round(sum(float(r["ctr_num"] or 0) for r in rows) / total_imp, 2)
            if total_imp else None
        ),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--slug", default="canal2")
    ap.add_argument("--since", default="2026-11-09")
    ap.add_argument("--end", default=date.today().isoformat())
    args = ap.parse_args()

    conn = sqlite3.connect(f"file:{db_path()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    ch = conn.execute("SELECT id, name FROM channels WHERE slug=?", (args.slug,)).fetchone()
    if not ch:
        raise SystemExit(f"channel {args.slug} not found")

    before = cohort(conn, ch["id"], "2026-10-12", "2026-11-08")
    after = cohort(conn, ch["id"], args.since, args.end)
    conn.close()

    print(f"# Revisión de cadencia — {ch['name']} ({args.slug})")
    print(f"- Antes: 2026-10-12 → 2026-11-08")
    print(f"- Después: {args.since} → {args.end}\n")
    for label, data in (("Antes", before), ("Después", after)):
        print(f"## {label}")
        if not data.get("videos"):
            print("Sin datos de alcance.\n")
            continue
        for key in ("videos", "views", "impressions", "median_impressions",
                    "watch_minutes", "weighted_ctr_pct"):
            print(f"- {key}: {data[key]}")
        print()

    med = after.get("median_impressions")
    print("## Decisión pre-registrada")
    if med is None:
        print("- Sin datos posteriores todavía; repetir tras 14 días.")
    elif med < 100:
        print(f"- Mediana {med:.0f} < 100 impresiones/vídeo → mantener 1/día o pausar largos.")
    elif med <= 300:
        print(f"- Mediana {med:.0f} (100–300) → mantener 1/día otros 14 días.")
    else:
        print(f"- Mediana {med:.0f} > 300 → restaurar 2/día.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
