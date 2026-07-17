#!/usr/bin/env python3
"""Enhanced cleanup of duplicate video records with lifecycle action remapping.

v2 improvements over v1:
  - BUGFIX: Remaps video_lifecycle_actions from duplicate records to the
    kept (original) record BEFORE deleting. Without this, pending actions
    (go_public, playlist_add, comments, etc.) would be lost.
  - BUGFIX: Merges published status from duplicate to kept record.
    If a duplicate was already published (go_public executed on it),
    the kept record gets updated with published status and timestamp.
  - BUGFIX: NULLs FK references in planned_slots and generation_jobs
    that point to duplicate records.

Usage:
    python3 scripts/cleanup_duplicate_videos_v2.py --dry-run
    python3 scripts/cleanup_duplicate_videos_v2.py --execute
"""

import argparse
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "autotube.db"

GREEN = "\033[92m"
YELLOW = "\033[93m"
RED = "\033[91m"
CYAN = "\033[96m"
RESET = "\033[0m"


def get_duplicates(conn: sqlite3.Connection) -> dict:
    cur = conn.execute("""
        SELECT v.id, v.canal, v.status, v.publish_mode, v.yt_video_id,
               v.created_at, v.published_at, v.target_public_at,
               v.yt_url, v.uploaded_at
        FROM videos v
        WHERE v.yt_video_id IS NOT NULL
          AND v.yt_video_id IN (
              SELECT v2.yt_video_id FROM videos v2
              WHERE v2.yt_video_id IS NOT NULL
              GROUP BY v2.yt_video_id HAVING COUNT(*) > 1
          )
        ORDER BY v.yt_video_id, v.id
    """)
    groups = defaultdict(list)
    for row in cur:
        groups[row["yt_video_id"]].append(dict(row))
    return dict(groups)


def identify_keep_delete(group: list[dict]) -> tuple[list[int], list[int]]:
    """Lowest id is kept (created first = original record)."""
    keep_id = min(r["id"] for r in group)
    delete_ids = [r["id"] for r in group if r["id"] != keep_id]
    return [keep_id], delete_ids


def remap_and_merge(conn: sqlite3.Connection, keep_id: int, delete_ids: list[int], group: list[dict]) -> dict:
    """Remap lifecycle actions from DEL → KEEP and merge status info.
    Returns summary dict for reporting.
    """
    result = {
        "lifecycle_remapped": 0,
        "lifecycle_merged": 0,
        "status_merged": False,
        "published_at_set": None,
        "keep_updated": False,
    }

    # ── 1. Remap video_lifecycle_actions: DEL → KEEP ──
    for del_id in delete_ids:
        cur = conn.execute(
            "UPDATE video_lifecycle_actions SET video_id = ? WHERE video_id = ?",
            (keep_id, del_id),
        )
        result["lifecycle_remapped"] += cur.rowcount

    # ── 2. Count existing actions on KEEP after merge ──
    cur = conn.execute(
        "SELECT COUNT(*) as n FROM video_lifecycle_actions WHERE video_id = ?",
        (keep_id,),
    )
    result["lifecycle_merged"] = cur.fetchone()["n"]

    # ── 3. Merge published status from duplicates ──
    keep_row = next(r for r in group if r["id"] == keep_id)

    for r in group:
        if r["id"] == keep_id:
            continue
        if r["status"] == "published" and keep_row["status"] != "published":
            keep_row["status"] = "published"
            result["published_at_set"] = r.get("published_at")
            result["status_merged"] = True
            break  # first published duplicate wins

    # ── 4. If any duplicate was published, update the keep record ──
    if result["status_merged"]:
        update_kwargs = {"status": "published"}
        if result["published_at_set"]:
            update_kwargs["published_at"] = result["published_at_set"]
        # Build SET clause
        set_clause = ", ".join(f"{k}=?" for k in update_kwargs)
        values = list(update_kwargs.values()) + [keep_id]
        conn.execute(f"UPDATE videos SET {set_clause} WHERE id=?", values)
        result["keep_updated"] = True

    # ── 5. Ensure keep record has yt_video_id if missing (should never happen, but defensive) ──
    if not keep_row.get("yt_video_id"):
        for r in group:
            if r["id"] != keep_id and r.get("yt_video_id"):
                conn.execute(
                    "UPDATE videos SET yt_video_id=?, yt_url=? WHERE id=?",
                    (r["yt_video_id"], r.get("yt_url", ""), keep_id),
                )
                break

    return result


def preview(conn: sqlite3.Connection):
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
            pub_info = ""
            if r.get("published_at"):
                pub_info = f" published={r['published_at'][:19] if r['published_at'] else ''}"
            print(f"  {marker}  video #{r['id']:>5}  {r['canal']:<8}  {r['status']:<18}  {r['publish_mode']:<12}  {r['created_at']}{pub_info}")

        # Count lifecycle actions that will be remapped
        action_count = 0
        for del_id in delete_ids:
            cur = conn.execute(
                "SELECT COUNT(*) as n FROM video_lifecycle_actions WHERE video_id = ?",
                (del_id,),
            )
            action_count += cur.fetchone()["n"]
        if action_count:
            print(f"  {YELLOW}🔄 {action_count} lifecycle actions will be remapped from DEL → KEEP #{keep_ids[0]}{RESET}")

        total_delete += len(delete_ids)

    print(f"\n{YELLOW}── Dry-run summary ──{RESET}")
    print(f"  Groups with duplicates: {len(groups)}")
    print(f"  Records to KEEP:        {sum(len(identify_keep_delete(g)[0]) for g in groups.values())}")
    print(f"  Records to DELETE:      {total_delete}")
    print(f"\n  Run with --execute to perform the cleanup.")


def execute_cleanup(conn: sqlite3.Connection):
    groups = get_duplicates(conn)
    if not groups:
        print(f"{GREEN}No duplicate records found.{RESET}")
        return

    all_delete_video_ids = []
    all_delete_job_ids = []
    total_merged = 0
    total_published_fixed = 0

    for yt_id, group in sorted(groups.items()):
        keep_ids, delete_ids = identify_keep_delete(group)
        if not delete_ids:
            continue

        # ── Phase 1: Remap lifecycle actions and merge status ──
        keep_id = keep_ids[0]
        merge_result = remap_and_merge(conn, keep_id, delete_ids, group)

        if merge_result["lifecycle_remapped"]:
            print(f"{CYAN}[{yt_id}]{RESET} Remapped {merge_result['lifecycle_remapped']} lifecycle actions → video #{keep_id}")
            total_merged += merge_result["lifecycle_remapped"]
        if merge_result["status_merged"]:
            print(f"{CYAN}[{yt_id}]{RESET} Merged 'published' status → video #{keep_id}")
            total_published_fixed += 1

        all_delete_video_ids.extend(delete_ids)

        # Collect generation_jobs referencing these duplicates
        placeholders = ",".join("?" for _ in delete_ids)
        job_rows = conn.execute(
            f"SELECT id FROM generation_jobs WHERE video_id IN ({placeholders})",
            delete_ids,
        ).fetchall()
        all_delete_job_ids.extend(r["id"] for r in job_rows)

    if not all_delete_video_ids:
        print(f"{GREEN}No duplicates to delete after analysis.{RESET}")
        return

    # ── Phase 2: Clean up FK references ──────────────────────

    placeholders = ",".join("?" for _ in all_delete_video_ids)

    # NULL out planned_slots pointing to duplicates
    try:
        cur = conn.execute(
            f"UPDATE planned_slots SET video_id = NULL WHERE video_id IN ({placeholders})",
            all_delete_video_ids,
        )
        if cur.rowcount:
            print(f"{YELLOW}NULLed {cur.rowcount} planned_slots.video_id refs{RESET}")
    except sqlite3.OperationalError:
        pass

    # NULL out shorts.source_video_id
    try:
        cur = conn.execute(
            f"UPDATE shorts SET source_video_id = NULL WHERE source_video_id IN ({placeholders})",
            all_delete_video_ids,
        )
        if cur.rowcount:
            print(f"{YELLOW}NULLed {cur.rowcount} shorts.source_video_id refs{RESET}")
    except sqlite3.OperationalError:
        pass

    # Delete generation_jobs
    if all_delete_job_ids:
        j_ph = ",".join("?" for _ in all_delete_job_ids)
        conn.execute(f"DELETE FROM generation_jobs WHERE id IN ({j_ph})", all_delete_job_ids)
        print(f"{YELLOW}Deleted {len(all_delete_job_ids)} generation_jobs{RESET}")

    # Delete video_scenes
    try:
        cur = conn.execute(
            f"DELETE FROM video_scenes WHERE video_id IN ({placeholders})",
            all_delete_video_ids,
        )
        if cur.rowcount:
            print(f"{YELLOW}Deleted {cur.rowcount} video_scenes rows{RESET}")
    except sqlite3.OperationalError:
        pass

    # ── Phase 3: Delete the duplicate videos ─────────────────
    conn.execute(f"DELETE FROM videos WHERE id IN ({placeholders})", all_delete_video_ids)

    # Commit everything
    conn.commit()

    print(f"\n{GREEN}══ Cleanup done ══{RESET}")
    print(f"  Lifecycle actions remapped:  {total_merged}")
    print(f"  Status merged (published):   {total_published_fixed}")
    print(f"  Duplicate videos deleted:    {len(all_delete_video_ids)}")

    # Verify
    remaining = get_duplicates(conn)
    if remaining:
        print(f"\n{RED}WARNING: {len(remaining)} groups still have duplicates!{RESET}")
    else:
        print(f"\n{GREEN}Verification: no duplicate yt_video_ids remain.{RESET}")


def main():
    parser = argparse.ArgumentParser(description="Clean up duplicate video records (v2 — preserves lifecycle actions)")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--dry-run", action="store_true", help="Preview without executing")
    group.add_argument("--execute", action="store_true", help="Execute cleanup")
    args = parser.parse_args()

    if not DB_PATH.exists():
        print(f"{RED}Error: database not found at {DB_PATH}{RESET}")
        sys.exit(1)

    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = OFF")  # we handle FK manually
    conn.execute("PRAGMA journal_mode = WAL")

    try:
        if args.dry_run:
            preview(conn)
        else:
            execute_cleanup(conn)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
