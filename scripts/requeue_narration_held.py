#!/usr/bin/env python3
"""Requeue narration-truncated / render-gate-video failures for reassembly.

Videos blocked at the narration-integrity gate (``validation_failed`` +
``progress_phase='upload'``) or at the render gate (``post_validate``) keep
their checkpoint (tts + media), so they can be re-assembled without
re-scraping or re-generating the script. This script walks them and calls
``POST /api/videos/{id}/reassemble`` when their channel is idle.

It is intentionally conservative:
* one active job per channel (the API enforces it; this script just skips),
* the global long-form limit is enforced by the API queue (409/blocked jobs
  are retried by the existing queue consumer),
* never deletes or edits media/checkpoints.

Usage:
    python3 scripts/requeue_narration_held.py --dry-run
    python3 scripts/requeue_narration_held.py --watch --interval 300
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.settings import DATABASE_PATH

API = "http://127.0.0.1:8000"
RETRYABLE_PHASES = ("video", "error", "upload", "post_validate", "render")


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DATABASE_PATH), timeout=15)
    conn.row_factory = sqlite3.Row
    return conn


def _eligible(conn: sqlite3.Connection, days: int = 14) -> list[dict]:
    placeholders = ",".join("?" for _ in RETRYABLE_PHASES)
    rows = conn.execute(
        f"""SELECT id, canal, channel_id, status, progress_phase, checkpoint_data
              FROM videos
             WHERE status IN ('error','validation_failed')
               AND progress_phase IN ({placeholders})
               AND COALESCE(created_at, '') >= datetime('now', ?)
               AND id NOT IN (
                   SELECT video_id FROM generation_jobs
                    WHERE video_id IS NOT NULL
                      AND status IN ('running','queued')
               )
             ORDER BY id""",
        (*RETRYABLE_PHASES, f"-{int(days)} days"),
    ).fetchall()
    out = []
    for row in rows:
        raw = row["checkpoint_data"] or "{}"
        try:
            cp = json.loads(raw) if isinstance(raw, str) else (raw or {})
        except (json.JSONDecodeError, TypeError):
            cp = {}
        if not cp.get("tts") or not cp.get("media"):
            continue
        out.append(dict(row))
    return out


def _channel_busy(conn: sqlite3.Connection, channel_id: int) -> bool:
    if not channel_id:
        return True
    row = conn.execute(
        "SELECT 1 FROM generation_jobs "
        "WHERE channel_id = ? AND status IN ('running','queued') LIMIT 1",
        (channel_id,),
    ).fetchone()
    return row is not None


def _post_reassemble(video_id: int) -> tuple[bool, str]:
    req = urllib.request.Request(
        f"{API}/api/videos/{video_id}/reassemble", method="POST",
        data=b"{}", headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = json.loads(resp.read().decode() or "{}")
        return True, f"job {body.get('job_id')}"
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode()[:160]
        return False, f"HTTP {exc.code}: {detail}"
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)


def run_once(dry_run: bool = False, days: int = 14) -> dict:
    conn = _connect()
    queued = skipped = failed = 0
    try:
        for video in _eligible(conn, days=days):
            vid = int(video["id"])
            channel_id = int(video["channel_id"] or 0)
            if _channel_busy(conn, channel_id):
                skipped += 1
                print(f"  #{vid} ({video['canal']}): canal ocupado — se reintentará")
                continue
            if dry_run:
                print(f"  #{vid} ({video['canal']}): listo para reassemble "
                      f"[{video['status']}/{video['progress_phase']}]")
                queued += 1
                continue
            ok, detail = _post_reassemble(vid)
            if ok:
                queued += 1
                print(f"  #{vid} ({video['canal']}): reassemble encolado — {detail}")
            else:
                failed += 1
                print(f"  #{vid} ({video['canal']}): no encolado — {detail}")
    finally:
        conn.close()
    return {"queued": queued, "skipped": skipped, "failed": failed}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--watch", action="store_true",
                    help="repeat until no eligible video can be queued")
    ap.add_argument("--interval", type=int, default=300,
                    help="seconds between passes in --watch mode")
    ap.add_argument("--days", type=int, default=14,
                    help="only videos created in the last N days (default 14)")
    ap.add_argument("--max-hours", type=float, default=24.0,
                    help="stop the watch loop after N hours (default 24)")
    args = ap.parse_args()

    total = {"queued": 0, "skipped": 0, "failed": 0}
    started = time.monotonic()
    while True:
        print(f"[{time.strftime('%H:%M:%S')}] pasada de reencolado")
        result = run_once(dry_run=args.dry_run, days=args.days)
        for key in total:
            total[key] += result[key]
        print(f"  => encolados={result['queued']} "
              f"canal_ocupado={result['skipped']} errores={result['failed']}")
        if not args.watch:
            break
        if result["queued"] == 0 and result["failed"] == 0 and result["skipped"] == 0:
            print("  => nada pendiente — fin")
            break
        if (time.monotonic() - started) >= args.max_hours * 3600:
            print(f"  => límite de {args.max_hours}h alcanzado — fin")
            break
        time.sleep(max(30, args.interval))
    print(f"Total: encolados={total['queued']} "
          f"canal_ocupado={total['skipped']} errores={total['failed']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
