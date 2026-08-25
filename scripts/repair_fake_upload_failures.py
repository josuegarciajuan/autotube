#!/usr/bin/env python3
"""Repair upload jobs failed only by the worker's unbound-local bug.

The repair is deliberately narrow: it only closes failed ``upload_only`` jobs
whose video already has a YouTube id and an uploaded status.  It never retries
an upload and never changes the video row, so a rerun is idempotent.
"""

from __future__ import annotations

import argparse
import os
import sqlite3
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATABASE = Path(os.getenv("DATABASE_PATH", str(PROJECT_ROOT / "autotube.db")))
EXCLUDED_JOB_IDS = (7436, 7583)


def find_candidates(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Return only rows safe to close as fake worker failures."""
    placeholders = ",".join("?" for _ in EXCLUDED_JOB_IDS)
    return conn.execute(
        f"""
        SELECT gj.id, gj.video_id, gj.error_msg, v.yt_video_id, v.status AS video_status
          FROM generation_jobs AS gj
          JOIN videos AS v ON v.id = gj.video_id
         WHERE gj.action = 'upload_only'
           AND gj.status = 'failed'
           AND instr(COALESCE(gj.error_msg, ''), '_upload_retryable_fail') > 0
           AND COALESCE(v.yt_video_id, '') <> ''
           AND v.status IN ('uploaded', 'uploaded_private')
           AND gj.id NOT IN ({placeholders})
         ORDER BY gj.id
        """,
        EXCLUDED_JOB_IDS,
    ).fetchall()


def repair_database(database: Path, *, dry_run: bool = False) -> list[int]:
    """Close matching jobs and return their ids; dry-run performs no writes."""
    conn = sqlite3.connect(database)
    conn.row_factory = sqlite3.Row
    try:
        candidates = find_candidates(conn)
        ids = [int(row["id"]) for row in candidates]
        for row in candidates:
            print(
                f"job={row['id']} video={row['video_id']} "
                f"yt_video_id={row['yt_video_id']} video_status={row['video_status']}"
            )
        if not dry_run and ids:
            conn.executemany(
                """
                UPDATE generation_jobs
                   SET status = 'completed', error_msg = NULL, finished_at = datetime('now')
                 WHERE id = ? AND status = 'failed'
                """,
                [(job_id,) for job_id in ids],
            )
            conn.commit()
        return ids
    finally:
        conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--database",
        type=Path,
        default=DEFAULT_DATABASE,
        help=f"SQLite database path (default: {DEFAULT_DATABASE})",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="List matching jobs without modifying the database",
    )
    args = parser.parse_args()
    ids = repair_database(args.database, dry_run=args.dry_run)
    action = "would repair" if args.dry_run else "repaired"
    print(f"{action} {len(ids)} fake upload failure(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
