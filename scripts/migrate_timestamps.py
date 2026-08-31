#!/usr/bin/env python3
"""Normalize legacy scheduling timestamps to the UTC-naive SQLite contract.

Usage: python3 scripts/migrate_timestamps.py [--db PATH] [--dry-run]
The default is intentionally a write operation; use --dry-run first.
"""
import argparse
import json
from datetime import datetime
from pathlib import Path
import sqlite3
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from api.time_utils import local_to_utc, sqlite_utc, parse_utc

MARKER = "timestamps_utc_madrid_v1"


def migrate_timestamps(db_path: str, dry_run: bool = False) -> dict:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    report = {"migration": MARKER, "dry_run": dry_run, "changes": [], "skipped": 0}
    try:
        state = conn.execute("SELECT value FROM system_state WHERE key = ?", (MARKER,)).fetchone()
    except sqlite3.OperationalError:
        state = None
    if state and not dry_run:
        conn.close()
        report["already_applied"] = True
        return report

    def convert(table, column, timezone_name):
        try:
            rows = conn.execute(
                f"SELECT id, {column} FROM {table} WHERE {column} IS NOT NULL AND {column} != ''"
            ).fetchall()
        except sqlite3.OperationalError:
            return
        for row in rows:
            old = row[column]
            try:
                # Explicit offsets are already instants. Legacy naive planning
                # values are Madrid local; this is deliberately not guessed for
                # CURRENT_TIMESTAMP fields, which are handled separately.
                if any(x in str(old)[10:] for x in ("+", "-", "Z", "z")):
                    new = sqlite_utc(parse_utc(old))
                else:
                    new = sqlite_utc(local_to_utc(old, timezone_name))
            except (ValueError, TypeError, KeyError):
                report["skipped"] += 1
                continue
            if new != old:
                report["changes"].append({"table": table, "id": row["id"], "column": column, "old": old, "new": new})
                if not dry_run:
                    conn.execute(f"UPDATE {table} SET {column} = ? WHERE id = ?", (new, row["id"]))

    # Historical planning values were Madrid local. Published/generation
    # timestamps produced by SQLite CURRENT_TIMESTAMP are already UTC and are
    # intentionally excluded to avoid changing correct publication history.
    for column in ("scheduled_at", "target_upload_at", "target_public_at"):
        convert("planned_slots", column, "Europe/Madrid")
    convert("videos", "target_public_at", "Europe/Madrid")
    convert("videos", "scheduled_upload_at", "Europe/Madrid")

    # Shorts planning was always emitted in UTC-naive form; normalize only its
    # representation, never reinterpret it as Madrid local time.
    for column in ("scheduled_at", "target_upload_at"):
        try:
            rows = conn.execute(f"SELECT id, {column} FROM shorts_planned_slots WHERE {column} IS NOT NULL").fetchall()
        except sqlite3.OperationalError:
            rows = []
        for row in rows:
            try:
                new = sqlite_utc(parse_utc(row[column]))
            except (ValueError, TypeError):
                report["skipped"] += 1
                continue
            if new != row[column]:
                report["changes"].append({"table": "shorts_planned_slots", "id": row["id"], "column": column, "old": row[column], "new": new})
                if not dry_run:
                    conn.execute(f"UPDATE shorts_planned_slots SET {column} = ? WHERE id = ?", (new, row["id"]))

    report["changed_count"] = len(report["changes"])
    if not dry_run:
        conn.execute("CREATE TABLE IF NOT EXISTS system_state (key TEXT PRIMARY KEY, value TEXT, updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
        conn.execute("INSERT OR REPLACE INTO system_state(key, value, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP)", (MARKER, json.dumps(report, ensure_ascii=False)))
        conn.commit()
    else:
        conn.rollback()
    conn.close()
    return report


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=None)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if args.db is None:
        from config.settings import DATABASE_PATH
        args.db = str(DATABASE_PATH)
    print(json.dumps(migrate_timestamps(args.db, args.dry_run), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
