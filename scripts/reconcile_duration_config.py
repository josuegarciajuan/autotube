#!/usr/bin/env python3
"""Reconcile the duration block of every active channel.

Single source of truth: ``VIDEO_AVERAGE_DURATION_MIN ±
VIDEO_DURATION_DISCREPANCY_MIN``. Legacy ``PROD_VIDEO_DURATION_*`` fields are
aligned to the derived range so no consumer sees a contradiction.

Dry-run by default; ``--apply`` writes a per-channel backup first.

Usage:
    python3 scripts/reconcile_duration_config.py --dry-run
    python3 scripts/reconcile_duration_config.py --apply
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from config.config_bridge import normalize_config_patch  # noqa: E402
from config.duration_policy import (  # noqa: E402
    apply_range_to_config,
    resolve_duration_range,
    validate_duration_config,
)


def db_path() -> str:
    return os.getenv("DATABASE_PATH", str(ROOT / "autotube.db"))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--slug")
    args = ap.parse_args()
    apply = args.apply and not args.dry_run

    conn = sqlite3.connect(db_path())
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT id, slug, config_json FROM channels WHERE active = 1 ORDER BY id"
    ).fetchall()

    touched = 0
    for row in rows:
        if args.slug and row["slug"] != args.slug:
            continue
        try:
            cfg = json.loads(row["config_json"] or "{}")
        except json.JSONDecodeError:
            print(f"SKIP {row['slug']}: invalid config_json")
            continue
        cfg = normalize_config_patch(cfg, row["slug"])
        policy = resolve_duration_range(cfg, row["slug"])
        issues = validate_duration_config(cfg)
        new_cfg = apply_range_to_config(cfg, policy.min_min, policy.max_min)
        changed = json.dumps(cfg, sort_keys=True) != json.dumps(new_cfg, sort_keys=True)

        print(
            f"\n[{row['slug']}] objetivo {policy.mean_min:g} ± {policy.discrepancy_min:g} min "
            f"→ {policy.min_min:g}-{policy.max_min:g} min (fuente: {policy.source})"
        )
        for issue in issues:
            print(f"  ⚠️  {issue}")
        if changed:
            print(
                "  → normalizar a: "
                f"mean={new_cfg['VIDEO_AVERAGE_DURATION_MIN']}, "
                f"disc={new_cfg['VIDEO_DURATION_DISCREPANCY_MIN']}, "
                f"legacy={new_cfg['PROD_VIDEO_DURATION_MIN']}-{new_cfg['PROD_VIDEO_DURATION_MAX']}"
            )
        else:
            print("  ✓ ya coherente")

        if apply and changed:
            backup_dir = ROOT / "backups"
            backup_dir.mkdir(exist_ok=True)
            ts = datetime.now().strftime("%Y%m%d-%H%M%S")
            backup = backup_dir / f"config-{row['slug']}-{ts}.json"
            backup.write_text(json.dumps(cfg, ensure_ascii=False, indent=2))
            conn.execute(
                "UPDATE channels SET config_json = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (json.dumps(new_cfg, ensure_ascii=False), row["id"]),
            )
            touched += 1
            print(f"  ✅ aplicado (backup: {backup})")

    if apply:
        conn.commit()
    conn.close()
    print(f"\n{'APLICADO' if apply else 'DRY-RUN'}: {touched if apply else 'ver arriba'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
