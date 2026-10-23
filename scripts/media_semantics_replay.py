#!/usr/bin/env python3
"""Offline replay of P01 media-selection fixtures (no network/downloads).

Usage:
    python3 scripts/media_semantics_replay.py --fixture tests/fixtures/media_semantics/example.json
    python3 scripts/media_semantics_replay.py --fixture ... --threshold 0.26
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.media_semantics.config import SemanticConfig  # noqa: E402
from pipeline.media_semantics.replay import replay_file  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", required=True)
    parser.add_argument("--threshold", type=float, default=None)
    parser.add_argument("--dump-detail", action="store_true")
    args = parser.parse_args()

    result = replay_file(
        args.fixture,
        config=SemanticConfig(mode="semantic"),
        threshold=args.threshold,
    )
    if not args.dump_detail:
        result = {
            "fixture": result["fixture"],
            "summary": result["summary"],
            "results": [
                {
                    "scene_id": row["scene_id"],
                    "legacy": row["legacy"]["status"],
                    "semantic": row["semantic"]["status"],
                    "agreement": row["agreement"],
                }
                for row in result["results"]
            ],
        }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
