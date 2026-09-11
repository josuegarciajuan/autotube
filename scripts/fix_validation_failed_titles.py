#!/usr/bin/env python3
"""Re-title videos stuck in ``validation_failed`` so they pass the upload gate.

Hybrid: regenerate the title from the script with the channel MetadataGenerator
(LLM), validated against the real packaging gate, falling back to a
deterministic trim. Videos without a script are left for manual review.

Usage:
    # Review proposals for one channel (no writes)
    python3 scripts/fix_validation_failed_titles.py --canal canal2 --dry-run
    # Apply for all channels
    python3 scripts/fix_validation_failed_titles.py --all --apply
    # Deterministic only (no LLM cost)
    python3 scripts/fix_validation_failed_titles.py --all --apply --no-llm

When run from a git worktree, point DATABASE_PATH to the live DB:
    DATABASE_PATH=/root/autotube/autotube.db python3 scripts/...
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from api.services.title_recovery import retitle_validation_failed  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--canal", default=None, help="Only this channel slug")
    parser.add_argument("--all", action="store_true", help="All channels")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--dry-run", action="store_true", help="Report only")
    group.add_argument("--apply", action="store_true", help="Persist and requeue")
    parser.add_argument("--no-llm", action="store_true",
                        help="Deterministic trim only (no LLM calls)")
    args = parser.parse_args()

    if not args.dry_run and not args.apply:
        parser.error("pass --dry-run or --apply")
    if not args.canal and not args.all:
        parser.error("pass --canal <slug> or --all")

    from database.db_extended import ExtendedDatabase
    db = ExtendedDatabase()

    result = retitle_validation_failed(
        db,
        dry_run=args.dry_run,
        channel_slug=args.canal,
        use_llm=not args.no_llm,
    )
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
