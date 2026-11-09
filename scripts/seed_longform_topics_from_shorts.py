#!/usr/bin/env python3
"""Seed long-form topic candidates from a channel's best-performing shorts.

Used by experiment E3 (canal2): test whether long-forms built from proven
short topics recover distribution. Inserts into ``topic_demand_candidates``
(the same pool the planner already consumes), tagged ``shorts_winner``.

Dry-run by default.

Usage:
    python3 scripts/seed_longform_topics_from_shorts.py --slug canal2 --limit 10 --dry-run
    python3 scripts/seed_longform_topics_from_shorts.py --slug canal2 --limit 10 --apply
"""
from __future__ import annotations

import argparse
import os
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def db_path() -> str:
    return os.getenv("DATABASE_PATH", str(ROOT / "autotube.db"))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--slug", required=True)
    ap.add_argument("--limit", type=int, default=10)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    apply = args.apply and not args.dry_run

    conn = sqlite3.connect(db_path())
    conn.row_factory = sqlite3.Row
    ch = conn.execute("SELECT id, name FROM channels WHERE slug=?", (args.slug,)).fetchone()
    if not ch:
        raise SystemExit(f"channel {args.slug} not found")

    rows = conn.execute(
        """SELECT s.title, COALESCE(MAX(ss.views), 0) views
           FROM shorts s
           LEFT JOIN short_stats ss ON ss.short_id = s.id
           WHERE s.channel_id = ? AND s.youtube_id IS NOT NULL
             AND s.title IS NOT NULL AND TRIM(s.title) != ''
           GROUP BY s.id
           ORDER BY views DESC, s.id DESC
           LIMIT ?""",
        (ch["id"], max(1, int(args.limit))),
    ).fetchall()

    inserted = 0
    for row in rows:
        query = " ".join(str(row["title"]).split())[:200]
        exists = conn.execute(
            """SELECT 1 FROM topic_demand_candidates
               WHERE channel_id = ? AND lower(query) = lower(?)""",
            (ch["id"], query),
        ).fetchone()
        if exists:
            print(f"= ya existe: {query[:70]}")
            continue
        score = min(1.0, float(row["views"] or 0) / 1000.0)
        print(f"+ {query[:70]} (views={row['views']}, score={score:.2f})")
        if apply:
            conn.execute(
                """INSERT INTO topic_demand_candidates
                   (channel_id, query, source, demand_score, status)
                   VALUES (?, ?, 'shorts_winner', ?, 'new')""",
                (ch["id"], query, score),
            )
            inserted += 1

    if apply:
        conn.commit()
    conn.close()
    print(f"\n{'INSERTADOS' if apply else 'DRY-RUN'}: {inserted if apply else len(rows)} candidato(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
