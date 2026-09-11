#!/usr/bin/env python3
"""Requeue videos stuck in ``validation_failed`` after a packaging fix.

Runs the same validator as the final upload gate and returns recoverable videos
to ``awaiting_upload``. Idempotent: videos that still fail are left untouched.

Usage:
    # Dry run for one channel
    python3 scripts/requeue_validation_failed.py --canal canal2 --dry-run
    # Apply for all channels
    python3 scripts/requeue_validation_failed.py --all --apply

When run from a git worktree, point DATABASE_PATH to the live DB, e.g.:
    DATABASE_PATH=/root/autotube/autotube.db python3 scripts/requeue_validation_failed.py ...
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from api.services.packaging_recovery import recover_packaging_held_videos  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--canal", default=None, help="Only this channel slug")
    parser.add_argument("--all", action="store_true", help="All channels")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--dry-run", action="store_true", help="Report only")
    group.add_argument("--apply", action="store_true", help="Requeue recoverable videos")
    args = parser.parse_args()

    if not args.dry_run and not args.apply:
        parser.error("pass --dry-run or --apply")
    if not args.canal and not args.all:
        parser.error("pass --canal <slug> or --all")

    from database.db_extended import ExtendedDatabase
    db = ExtendedDatabase()

    result = recover_packaging_held_videos(
        db, dry_run=args.dry_run, channel_slug=args.canal,
    )
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
