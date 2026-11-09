#!/usr/bin/env python3
"""Repair Insight AI config writes that were lost or shadowed.

Two independent operations (both dry-run by default):

1. **Deduplicate** lower-case keys that shadow their canonical UPPER form.
   Equal values are dropped; list values are unioned; other conflicts are
   reported and resolved with ``--prefer``.

2. **Replay accepted changes** (``--replay-applied``) from the latest
   completed insight of each channel. This restores changes the user accepted
   that were later deleted by the old startup cleanup (the canal3 title case).
   Superseded and discarded recommendations are skipped; list values from
   multiple accepted recommendations are unioned.

A backup of each channel's config_json is written before any change.

Usage:
    python3 scripts/repair_insight_applications.py --dry-run
    python3 scripts/repair_insight_applications.py --apply --replay-applied
    python3 scripts/repair_insight_applications.py --apply --prefer lowercase
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

from config.config_bridge import (  # noqa: E402
    canonical_config_key,
    normalize_config_patch,
)


def db_path() -> str:
    return os.getenv("DATABASE_PATH", str(ROOT / "autotube.db"))


def _equal(a, b) -> bool:
    if a == b:
        return True
    try:
        return json.dumps(a, sort_keys=True, ensure_ascii=False) == json.dumps(
            b, sort_keys=True, ensure_ascii=False
        )
    except (TypeError, ValueError):
        return False


def _union_lists(base: list, extra: list) -> list:
    out = list(base)
    for item in extra:
        if item not in out:
            out.append(item)
    return out


def repair_config(slug: str, cfg: dict, prefer: str) -> tuple[dict, list[str]]:
    changes: list[str] = []
    groups: dict[str, list[str]] = {}
    for key in cfg:
        groups.setdefault(key.upper(), []).append(key)

    for upper, keys in groups.items():
        if len(keys) <= 1:
            continue
        canonical = canonical_config_key(keys[0], slug)
        if canonical not in keys:
            canonical = next((k for k in keys if k == upper), keys[0])
        others = [k for k in keys if k != canonical]
        base = cfg[canonical]
        for other in others:
            value = cfg[other]
            if _equal(base, value):
                cfg.pop(other, None)
                changes.append(f"{slug}: drop duplicate {other} (same value)")
                continue
            if isinstance(base, list) and isinstance(value, list):
                cfg[canonical] = _union_lists(base, value)
                cfg.pop(other, None)
                changes.append(
                    f"{slug}: union {other} -> {canonical} "
                    f"({len(base)} + {len(value)} items)"
                )
                continue
            if prefer == "lowercase":
                cfg[canonical] = value
                changes.append(f"{slug}: {other} wins over {canonical} (lowercase preferred)")
            else:
                changes.append(f"{slug}: keep {canonical}, drop conflicting {other}")
            cfg.pop(other, None)
    return cfg, changes


def _latest_insight(conn: sqlite3.Connection, channel_id: int) -> dict | None:
    row = conn.execute(
        """SELECT id, insights_json FROM channel_insights
           WHERE channel_id = ? AND status = 'completed'
           ORDER BY id DESC LIMIT 1""",
        (channel_id,),
    ).fetchone()
    if not row:
        return None
    try:
        return {"id": row["id"], "data": json.loads(row["insights_json"] or "{}")}
    except json.JSONDecodeError:
        return None


def replayable_changes(conn: sqlite3.Connection, channel_id: int, slug: str) -> tuple[dict, list[str]]:
    """Merge config_changes from accepted (non-superseded) recommendations."""
    insight = _latest_insight(conn, channel_id)
    if not insight:
        return {}, []
    merged: dict = {}
    notes: list[str] = []
    # Reverse order: the most recently listed accepted rec is the base, older
    # accepted items are appended. Keeps the user's latest intent first when
    # unioning lists (canal3: questions + Göbekli).
    for rec in reversed(insight["data"].get("recommendations", [])):
        if not rec.get("applied") or rec.get("discarded"):
            continue
        if rec.get("applied_status") == "superseded":
            continue
        # Only recommendations produced by THIS analysis. Carried-over recs
        # from older analyses keep their original analysis_id and may contain
        # obsolete/contradictory accepted flags; replaying them would rewrite
        # the channel with months-old decisions.
        analysis_id = rec.get("analysis_id")
        if rec.get("from_previous") or (analysis_id and analysis_id != insight["id"]):
            continue
        changes = normalize_config_patch(rec.get("config_changes") or {}, slug)
        if not changes:
            continue
        for key, value in changes.items():
            if key in merged and isinstance(merged[key], list) and isinstance(value, list):
                merged[key] = _union_lists(merged[key], value)
            else:
                merged[key] = value
        notes.append(f"{slug}: replay insight {insight['id']} rec {rec.get('id')}")
    return merged, notes


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--prefer", choices=["canonical", "lowercase"], default="canonical")
    ap.add_argument("--replay-applied", action="store_true")
    ap.add_argument("--slug")
    args = ap.parse_args()
    apply = args.apply and not args.dry_run

    conn = sqlite3.connect(db_path())
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT id, slug, config_json FROM channels WHERE active = 1 ORDER BY id"
    ).fetchall()

    all_changes: list[str] = []
    for row in rows:
        if args.slug and row["slug"] != args.slug:
            continue
        try:
            cfg = json.loads(row["config_json"] or "{}")
        except json.JSONDecodeError:
            print(f"SKIP {row['slug']}: invalid config_json")
            continue
        before = json.dumps(cfg, sort_keys=True, ensure_ascii=False)

        channel_changes: list[str] = []
        cfg, changes = repair_config(row["slug"], cfg, args.prefer)
        channel_changes.extend(changes)

        if args.replay_applied:
            replay, notes = replayable_changes(conn, row["id"], row["slug"])
            applied_now = 0
            for key, value in replay.items():
                if not _equal(cfg.get(key), value):
                    cfg[key] = value
                    applied_now += 1
                    channel_changes.append(
                        f"{row['slug']}: restore {key} from accepted insight"
                    )
            if applied_now:
                channel_changes.extend(notes)

        after = json.dumps(cfg, sort_keys=True, ensure_ascii=False)
        if before == after:
            continue
        all_changes.extend(channel_changes)
        print(f"\n[{row['slug']}] {len(channel_changes)} cambio(s)")
        for c in channel_changes:
            print(f"  - {c}")
        if apply:
            backup_dir = ROOT / "backups"
            backup_dir.mkdir(exist_ok=True)
            ts = datetime.now().strftime("%Y%m%d-%H%M%S")
            backup = backup_dir / f"config-{row['slug']}-{ts}.json"
            backup.write_text(json.dumps(json.loads(before), ensure_ascii=False, indent=2))
            conn.execute(
                "UPDATE channels SET config_json = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (json.dumps(cfg, ensure_ascii=False), row["id"]),
            )
            print(f"  ✅ aplicado (backup: {backup})")

    if apply:
        conn.commit()
    conn.close()

    print(f"\n{'APLICADO' if apply else 'DRY-RUN'}: {len(all_changes)} cambio(s)")
    if not apply and all_changes:
        print("Repite con --apply para escribir (se crea backup automático).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
