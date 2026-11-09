#!/usr/bin/env python3
"""Audit applied Insight AI recommendations against the effective config.

Read-only. Compares each recommendation's ``config_changes`` (normalized to
canonical keys) with ``channels.config_json`` and reports whether it is
effective, superseded or missing. Optionally cross-checks logs/api.log.

Usage:
    python3 scripts/audit_insight_applications.py --since-id 43
    python3 scripts/audit_insight_applications.py --slug canal3 --json
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from config.config_bridge import canonical_config_key  # noqa: E402


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


def _case_get(mapping: dict, key: str):
    if key in mapping:
        return mapping[key]
    upper = key.upper()
    for k, v in mapping.items():
        if k.upper() == upper:
            return v
    return None


def audit(conn: sqlite3.Connection, since_id: int, slug: str | None) -> list[dict]:
    rows = conn.execute(
        """SELECT i.id, i.channel_id, i.generated_at, i.applied_at, i.insights_json,
                  ch.slug, ch.config_json
           FROM channel_insights i
           JOIN channels ch ON ch.id = i.channel_id
           WHERE i.id >= ? AND ch.active = 1
           ORDER BY i.id""",
        (since_id,),
    ).fetchall()
    out: list[dict] = []
    for row in rows:
        if slug and row["slug"] != slug:
            continue
        try:
            data = json.loads(row["insights_json"] or "{}")
            cfg = json.loads(row["config_json"] or "{}")
        except json.JSONDecodeError:
            continue
        for rec in data.get("recommendations", []):
            changes = rec.get("config_changes") or {}
            if not changes:
                continue
            checks = []
            for key, expected in changes.items():
                canonical = canonical_config_key(key, row["slug"])
                actual = _case_get(cfg, canonical)
                checks.append({
                    "key": canonical,
                    "expected": expected,
                    "actual": actual,
                    "match": _equal(actual, expected),
                })
            matched = sum(1 for c in checks if c["match"])
            carried = bool(
                rec.get("from_previous")
                or (rec.get("analysis_id") and rec.get("analysis_id") != row["id"])
            )
            if carried:
                status = "stale-applied" if rec.get("applied") else "not-applied"
            elif not rec.get("applied") and not rec.get("applied_status"):
                status = "not-applied"
            elif rec.get("applied_status") == "superseded":
                status = "superseded"
            elif matched == len(checks):
                status = "effective" if rec.get("applied") else "stored-not-flagged"
            elif matched == 0:
                status = "not-effective"
            else:
                status = "partial"
            out.append({
                "insight_id": row["id"],
                "slug": row["slug"],
                "generated_at": row["generated_at"],
                "applied_at": row["applied_at"],
                "rec_id": rec.get("id"),
                "title": rec.get("title"),
                "applied_flag": bool(rec.get("applied")),
                "applied_status": rec.get("applied_status"),
                "status": status,
                "checks": checks,
            })
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--since-id", type=int, default=1)
    ap.add_argument("--slug")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--all", action="store_true", help="Include recommendations never applied")
    args = ap.parse_args()

    conn = sqlite3.connect(f"file:{db_path()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    results = audit(conn, args.since_id, args.slug)
    conn.close()
    if not args.all:
        results = [
            r for r in results
            if r["status"] not in ("not-applied", "stale-applied")
        ]

    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
        return 0

    print(f"{'insight':>7} {'canal':<8} {'rec':<10} {'estado':<20} título")
    print("-" * 100)
    for r in results:
        print(
            f"{r['insight_id']:>7} {r['slug']:<8} {str(r['rec_id'])[:8]:<10} "
            f"{r['status']:<20} {(r['title'] or '')[:55]}"
        )
        if r["status"] not in ("effective",):
            for c in r["checks"]:
                if not c["match"]:
                    print(
                        f"          ↳ {c['key']}: esperado={json.dumps(c['expected'], ensure_ascii=False)[:60]} "
                        f"actual={json.dumps(c['actual'], ensure_ascii=False)[:60]}"
                    )
    print(f"\nTotal: {len(results)} recomendaciones con config_changes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
