#!/usr/bin/env python3
"""Normalise CONTENT_PILLARS to the canonical schema in every channel.

Accepts both historical schemas:
  * ``{pillar, pct, description}``
  * ``{name, ratio, desc}``

Dry-run by default; ``--apply`` writes a backup first.

Usage:
    python3 scripts/normalize_content_pillars.py --dry-run
    python3 scripts/normalize_content_pillars.py --apply
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

from config.content_pillars import normalize_pillars  # noqa: E402


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
        before = cfg.get("CONTENT_PILLARS")
        if not before:
            continue
        canonical = normalize_pillars(cfg)
        if canonical == before:
            print(f"[{row['slug']}] ya canónico")
            continue
        print(f"[{row['slug']}] {len(canonical)} pilar(es) normalizados")
        for p in canonical:
            print(f"  - {p['name']}: {p['ratio']}%")
        if apply:
            backup_dir = ROOT / "backups"
            backup_dir.mkdir(exist_ok=True)
            ts = datetime.now().strftime("%Y%m%d-%H%M%S")
            backup = backup_dir / f"config-{row['slug']}-{ts}.json"
            backup.write_text(json.dumps(cfg, ensure_ascii=False, indent=2))
            cfg["CONTENT_PILLARS"] = canonical
            conn.execute(
                "UPDATE channels SET config_json = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (json.dumps(cfg, ensure_ascii=False), row["id"]),
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
