#!/usr/bin/env python3
"""Dry-run report of the current per-channel policy state.

This phase intentionally has no write mode: it is safe to run against the live
database and preserves the four existing channel policies exactly as stored.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


def clear_false_positive_block(db, channel_id: int, evidence: dict) -> None:
    """Clear only the operational block; preserve strike history and evidence."""
    db.set_system_state(f"shorts_spam_blocked_until_{channel_id}", "")
    db.set_system_state(
        f"spam_block_verification_{channel_id}",
        json.dumps({
            "status": "cleared_false_positive",
            "verified_at": datetime.now(timezone.utc).isoformat(),
            "evidence": evidence,
        }, ensure_ascii=False),
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", required=True,
                        help="required; this command never writes policy state")
    parser.add_argument("--clear-false-positive-channel", type=int,
                        help="deprecated: live correction is intentionally disabled")
    args = parser.parse_args()
    if args.clear_false_positive_channel is not None:
        parser.error("la corrección de estado se ejecuta mediante migración revisada, no desde este informe")
    if not args.dry_run:  # defensive, argparse currently makes this unreachable
        parser.error("solo se admite --dry-run")

    from database.db_extended import ExtendedDatabase
    from api.services.channel_policy import collect_channel_policy_snapshot

    db = ExtendedDatabase()
    print(json.dumps(collect_channel_policy_snapshot(db), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
