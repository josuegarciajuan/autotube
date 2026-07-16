#!/usr/bin/env python3
"""Clean up duplicate video records in the local DB created by the
missing db_video_id bug in generation_service.py.

Bug: PipelineOrchestrator was created without db_video_id, causing
phase_upload to INSERT a new video record instead of updating the
existing one. Each upload_only job created 2 extra DB records per
original video.

Duplicate identification rules:
  1. Keep the video linked to planned_slots (via video_id) — always original
  2. If no planned_slot link for any record, keep the lowest id
  3. Delete all others sharing the same yt_video_id

Affected tables: videos, generation_jobs (jobs referencing duplicate video_ids)

Usage:
    python3 scripts/cleanup_duplicate_videos.py --dry-run     # preview only
    python3 scripts/cleanup_duplicate_videos.py --execute     # actually delete
"""

import argparse
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

DB_PATH = Path("autotube.db")

# ── Color helpers ──────────────────────────────────────────
GREEN = "\033[92m"
YELLOW = "\033[93m"
RED = "\033[91m"
CYAN = "\033[96m"
RESET = "\033[0m"


def get_duplicates(conn: sqlite3.Connection) -> dict:
    """Find all duplicate yt_video_id groups. Returns {yt_video_id: [video_rows]}"""
    cursor = conn.execute("""
        SELECT v.id, v.canal, v.status, v.publish_mode, v.yt_video_id,
               v.created_at, v.yt_url, ps.id as slot_id
        FROM videos v
        LEFT JOIN planned_slots ps ON ps.video_id = v.id
        WHERE v.yt_video_id IS NOT NULL
          AND v.yt_video_id IN (
              SELECT v2.yt_video_id FROM videos v2
              WHERE v2.yt_video_id IS NOT NULL
              GROUP BY v2.yt_video_id HAVING COUNT(*) > 1
          )
        ORDER BY v.yt_video_id, v.id
    """)

    groups = defaultdict(list)
    for row in cursor:
        groups[row["yt_video_id"]].append(dict(row))
    return dict(groups)


def identify_keep_delete(group: list[dict]) -> tuple[list[int], list[int]]:
    """For a group sharing the same yt_video_id, decide which to keep and which to delete.

    Rule:
      - Keep the record linked to planned_slots (has slot_id)
      - If none linked, keep the lowest id
      - Delete all others
    """
    with_slot = [r for r in group if r["slot_id"] is not None]
    if with_slot:
        keep_id = with_slot[0]["id"]
    else:
        keep_id = min(r["id"] for r in group)

    delete_ids = [r["id"] for r in group if r["id"] != keep_id]
    return [keep_id], delete_ids


def preview(conn: sqlite3.Connection):
    """Show what would be deleted."""
    groups = get_duplicates(conn)
    if not groups:
        print(f"{GREEN}No duplicate records found.{RESET}")
        return

    total_delete = 0
    for yt_id, group in sorted(groups.items()):
        keep_ids, delete_ids = identify_keep_delete(group)
        print(f"\n{CYAN}YouTube ID: {yt_id}{RESET}")
        for r in group:
            marker = f"{GREEN}KEEP{RESET}" if r["id"] in keep_ids else f"{RED}DEL{RESET}"
            slot_info = f" slot=#{r['slot_id']}" if r["slot_id"] else ""
            print(f"  {marker}  video #{r['id']:>5}  {r['canal']:<8}  {r['status']:<18}  "
                  f"{r['publish_mode']:<12}  {r['created_at']}{slot_info}")
        total_delete += len(delete_ids)

    print(f"\n{YELLOW}── Dry-run summary ──{RESET}")
    print(f"  Groups with duplicates: {len(groups)}")
    print(f"  Records to KEEP:        {sum(1 for g in groups.values() for _ in identify_keep_delete(g)[0])}")
    print(f"  Records to DELETE:      {total_delete}")
    print(f"\n  Run with --execute to perform the cleanup.")


def execute_cleanup(conn: sqlite3.Connection):
    """Delete duplicate records and their associated generation_jobs."""
    groups = get_duplicates(conn)
    if not groups:
        print(f"{GREEN}No duplicate records found.{RESET}")
        return

    all_delete_video_ids = []
    all_delete_job_ids = []

    for yt_id, group in sorted(groups.items()):
        keep_ids, delete_ids = identify_keep_delete(group)
        if not delete_ids:
            continue
        all_delete_video_ids.extend(delete_ids)

        # Find generation_jobs referencing the duplicate video_ids
        placeholders = ",".join("?" for _ in delete_ids)
        job_rows = conn.execute(
            f"SELECT id FROM generation_jobs WHERE video_id IN ({placeholders})",
            delete_ids,
        ).fetchall()
        all_delete_job_ids.extend(r["id"] for r in job_rows)

    if not all_delete_video_ids:
        print(f"{GREEN}No duplicates to delete after analysis.{RESET}")
        return

    placeholders = ",".join("?" for _ in all_delete_video_ids)

    # ── Delete dependent rows (FK constraints without CASCADE) ──
    dependent_tables = [
        "video_lifecycle_actions",
        "video_playlists",
        "comment_log",
        "shorts_planned_slots",
    ]
    for tbl in dependent_tables:
        try:
            cur = conn.execute(
                f"DELETE FROM {tbl} WHERE video_id IN ({placeholders})",
                all_delete_video_ids,
            )
            if cur.rowcount:
                print(f"{YELLOW}Deleted {cur.rowcount} rows from {tbl}{RESET}")
        except sqlite3.OperationalError:
            pass  # table may not exist

    # ── NULL out FK references (SET NULL constraints) ──
    for tbl, col in [("shorts", "source_video_id"), ("content_schedules", "video_id")]:
        try:
            cur = conn.execute(
                f"UPDATE {tbl} SET {col} = NULL WHERE {col} IN ({placeholders})",
                all_delete_video_ids,
            )
            if cur.rowcount:
                print(f"{YELLOW}NULLed {cur.rowcount} rows in {tbl}.{col}{RESET}")
        except sqlite3.OperationalError:
            pass

    # ── Delete generation_jobs (SET NULL cascaded, but clean up anyway) ──
    if all_delete_job_ids:
        j_ph = ",".join("?" for _ in all_delete_job_ids)
        conn.execute(
            f"DELETE FROM generation_jobs WHERE id IN ({j_ph})",
            all_delete_job_ids,
        )
        print(f"{YELLOW}Deleted {len(all_delete_job_ids)} generation_jobs{RESET}")

    # ── Delete duplicate videos ──
    conn.execute(f"DELETE FROM videos WHERE id IN ({placeholders})", all_delete_video_ids)
    conn.commit()
    print(f"{GREEN}Deleted {len(all_delete_video_ids)} duplicate video records{RESET}")

    # ── Verify ──
    remaining_groups = get_duplicates(conn)
    if remaining_groups:
        print(f"\n{RED}WARNING: {len(remaining_groups)} groups still have duplicates!{RESET}")
    else:
        print(f"\n{GREEN}Cleanup successful — no duplicate yt_video_ids remain.{RESET}")


def main():
    parser = argparse.ArgumentParser(description="Clean up duplicate video records in local DB")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--dry-run", action="store_true", help="Preview duplicates without deleting")
    group.add_argument("--execute", action="store_true", help="Actually delete duplicates")
    args = parser.parse_args()

    if not DB_PATH.exists():
        print(f"{RED}Error: database not found at {DB_PATH}{RESET}")
        sys.exit(1)

    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")

    try:
        if args.dry_run:
            preview(conn)
        else:
            execute_cleanup(conn)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
