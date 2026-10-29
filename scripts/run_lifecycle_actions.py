#!/usr/bin/env python3
"""Run one channel's lifecycle actions in an isolated process.

The scheduler launches this script with a hard timeout so a hung Playwright
driver can be killed without blocking ``schedule_checker``. Prints a single
JSON line with the manager result on stdout; logs go to stderr.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--slug", required=True)
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stderr,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    try:
        from database.db_extended import ExtendedDatabase
        from pipeline.video_lifecycle import VideoLifecycleManager

        # Touch the DB once so migrations/bootstrap run in this process too.
        db = ExtendedDatabase()
        ch = db.get_channel_by_slug(args.slug)
        if not ch:
            print(json.dumps({"processed": 0, "succeeded": 0, "failed": 0,
                              "error": "channel_not_found"}))
            return 0
        mgr = VideoLifecycleManager(args.slug)
        result = mgr.process_due_actions() or {}
        print(json.dumps({
            "processed": int(result.get("processed", 0) or 0),
            "succeeded": int(result.get("succeeded", 0) or 0),
            "failed": int(result.get("failed", 0) or 0),
        }))
        return 0
    except Exception as exc:  # noqa: BLE001
        logging.getLogger("run_lifecycle_actions").exception(
            "lifecycle subprocess failed for %s", args.slug
        )
        print(json.dumps({"processed": 0, "succeeded": 0, "failed": 0,
                          "error": str(exc)[:500]}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
