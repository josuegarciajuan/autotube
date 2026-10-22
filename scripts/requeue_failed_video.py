#!/usr/bin/env python3
"""Requeue a failed long-form video through the subprocess worker.

The worker resumes from ``videos.checkpoint_data`` and applies the transient
retry policy.  Use ``--reset-from media`` to force a media re-fetch / re-render
(e.g. video #2723, whose render failed the black-frame gate after most assets
were purged from disk).

Examples:
    python3 scripts/requeue_failed_video.py --video-id 2738
    python3 scripts/requeue_failed_video.py --video-id 2729 --dry-run
    python3 scripts/requeue_failed_video.py --video-id 2723 --reset-from media
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

PHASE_ORDER = ["scrape", "script", "pre_validate", "tts", "media", "video", "metadata", "post_validate", "upload"]


def _db_path() -> Path:
    from config.settings import DATABASE_PATH
    return Path(str(DATABASE_PATH))


def _conn(path: Path):
    conn = sqlite3.connect(str(path), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--video-id", type=int, required=True)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument(
        "--reset-from", choices=("tts", "media"), default=None,
        help="Drop this phase and every later checkpoint phase (forces re-fetch/re-render)",
    )
    args = ap.parse_args()

    from database.db import init_db
    from database.db_extended import migrate_v2, ExtendedDatabase

    if not args.dry_run:
        init_db()
        migrate_v2()

    path = _db_path()
    with _conn(path) as conn:
        video = conn.execute("SELECT * FROM videos WHERE id=?", (args.video_id,)).fetchone()
        if not video:
            print(f"ERROR: video #{args.video_id} no existe", file=sys.stderr)
            return 2
        active = conn.execute(
            "SELECT id,status FROM generation_jobs WHERE video_id=? AND status IN ('queued','running')",
            (args.video_id,),
        ).fetchone()
        if active:
            print(
                f"ERROR: video #{args.video_id} ya tiene job activo #{active['id']} "
                f"({active['status']}) — no se duplica",
                file=sys.stderr,
            )
            return 3
        if not video["channel_id"]:
            print(f"ERROR: video #{args.video_id} sin channel_id", file=sys.stderr)
            return 2

    checkpoint = {}
    try:
        checkpoint = json.loads(video["checkpoint_data"] or "{}")
    except Exception:
        checkpoint = {}
    if not checkpoint.get("script"):
        print(
            f"ERROR: video #{args.video_id} no tiene checkpoint de script; "
            "requiere regeneración completa (no reencolable por esta vía)",
            file=sys.stderr,
        )
        return 4

    if args.reset_from:
        start = PHASE_ORDER.index(args.reset_from)
        dropped = [p for p in PHASE_ORDER[start:] if p in checkpoint]
        for phase in dropped:
            checkpoint.pop(phase, None)
        print(f"Checkpoint: se eliminan fases {dropped or 'ninguna'}")

    if args.dry_run:
        print(
            f"DRY-RUN video #{args.video_id} canal #{video['channel_id']} "
            f"estado={video['status']} fases={list(checkpoint)} "
            f"reset_from={args.reset_from or '-'}"
        )
        return 0

    db = ExtendedDatabase()
    job_id = db.create_job(int(video["channel_id"]), "generate_only", int(args.video_id))
    db.update_job(
        job_id,
        status="queued",
        error_msg=f"Requeue manual desde script (reset_from={args.reset_from or '-'})",
    )
    db.update_video(
        int(args.video_id),
        status="error",
        progress_phase="queued_retry",
        error_message=f"Reencolado manual (job #{job_id})",
        checkpoint_data=json.dumps(checkpoint, ensure_ascii=False),
    )
    print(
        f"OK: video #{args.video_id} reencolado como job #{job_id} "
        f"(canal #{video['channel_id']}, reset_from={args.reset_from or '-'})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
