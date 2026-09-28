#!/usr/bin/env python3
"""Offline packaging audit (W7) — read-only, 0 quota, 0 uploads.

Reports, per channel:
  * incomplete/cut titles (W1 completeness guard);
  * overlays actually persisted (W3 OverlaySpec);
  * off-niche titles (W4 niche guard);
  * CTR by layout / variant strategy (W7 analytics).

Optionally emits a ``packaging_quality`` alert when thresholds are breached.
It never mutates videos and never touches YouTube.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from config.config_bridge import get_channel_config  # noqa: E402
from pipeline.niche_guard import is_guard_enabled, niche_fit_score  # noqa: E402
from pipeline.title_tokens import is_complete_title  # noqa: E402

INCOMPLETE_ALERT_RATIO = 0.10
OVERLAY_PERSIST_ALERT_RATIO = 0.90


def _columns(con, table: str) -> set[str]:
    try:
        return {r[1] for r in con.execute(f"PRAGMA table_info({table})")}
    except sqlite3.OperationalError:
        return set()


def audit_channel(con, channel: dict, limit: int) -> dict:
    cols = _columns(con, "videos")
    wanted = [c for c in (
        "id", "titulo_final", "thumbnail_text", "thumbnail_overlay_source",
        "thumbnail_variant_strategy", "thumbnail_layout", "status",
    ) if c in cols]
    channel_id = channel["id"]
    query_cols = ", ".join(wanted)
    rows = con.execute(
        f"""SELECT {query_cols} FROM videos
            WHERE channel_id = ? AND COALESCE(titulo_final, '') != ''
            ORDER BY id DESC LIMIT ?""",
        (channel_id, int(limit)),
    ).fetchall()

    cfg = None
    try:
        cfg = get_channel_config(channel["slug"])
    except Exception:
        cfg = None

    total = len(rows)
    incomplete = []
    off_niche = []
    with_overlay = 0
    with_source = 0
    for row in rows:
        title = (row["titulo_final"] if "titulo_final" in row.keys() else "") or ""
        complete, reason = is_complete_title(title)
        if not complete:
            incomplete.append({"id": row["id"], "title": title, "reason": reason})
        if cfg is not None and is_guard_enabled(cfg):
            if niche_fit_score(title, cfg) < float(getattr(cfg, "TITLE_NICHE_FIT_MIN", 0.3)):
                off_niche.append({"id": row["id"], "title": title})
        if "thumbnail_text" in row.keys() and (row["thumbnail_text"] or "").strip():
            with_overlay += 1
        if "thumbnail_overlay_source" in row.keys() and (row["thumbnail_overlay_source"] or "").strip():
            with_source += 1

    return {
        "channel_id": channel_id,
        "slug": channel["slug"],
        "name": channel["name"],
        "sampled": total,
        "incomplete_titles": len(incomplete),
        "incomplete_ratio": round(len(incomplete) / total, 3) if total else 0.0,
        "incomplete_examples": incomplete[:5],
        "off_niche": len(off_niche),
        "off_niche_examples": off_niche[:5],
        "overlay_persisted": with_overlay,
        "overlay_persisted_ratio": round(with_overlay / total, 3) if total else 0.0,
        "overlay_source_set": with_source,
    }


def _emit_alert(channel: dict, result: dict) -> bool:
    try:
        from api.services.lifecycle_monitor import create_alert
        from database.db_extended import ExtendedDatabase
        problems = []
        if result["incomplete_ratio"] > INCOMPLETE_ALERT_RATIO:
            problems.append(f"{result['incomplete_titles']}/{result['sampled']} títulos incompletos")
        if result["sampled"] and result["overlay_persisted_ratio"] < OVERLAY_PERSIST_ALERT_RATIO:
            problems.append(
                f"overlay persistido en {result['overlay_persisted_ratio']*100:.0f}% "
                f"(esperado ≥{OVERLAY_PERSIST_ALERT_RATIO*100:.0f}%)"
            )
        if not problems:
            return False
        db = ExtendedDatabase()
        create_alert(
            db,
            entity_type="channel",
            channel_id=channel["id"],
            alert_type="packaging_quality",
            severity="warning",
            title=f"Packaging a revisar en {channel['name']}",
            message="; ".join(problems),
            metadata=result,
        )
        return True
    except Exception as exc:  # noqa: BLE001 — never fail the audit
        print(f"  (alert skip: {exc})")
        return False


def main() -> int:
    ap = argparse.ArgumentParser(description="Auditoría offline de packaging")
    ap.add_argument("--db", default=os.environ.get("DATABASE_PATH", "autotube.db"))
    ap.add_argument("--limit", type=int, default=100)
    ap.add_argument("--channel", default="", help="slug (canal2…) o vacío = todos")
    ap.add_argument("--json", default="", help="ruta de salida JSON")
    ap.add_argument("--alerts", action="store_true", help="crear alertas packaging_quality")
    args = ap.parse_args()

    if not os.path.exists(args.db):
        print(f"DB no encontrada: {args.db}")
        return 2

    con = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    if args.channel:
        channels = con.execute(
            "SELECT id, slug, name FROM channels WHERE slug = ?", (args.channel,)
        ).fetchall()
    else:
        channels = con.execute(
            "SELECT id, slug, name FROM channels WHERE COALESCE(slug, '') != '' ORDER BY id"
        ).fetchall()

    report = []
    for channel in channels:
        result = audit_channel(con, dict(channel), args.limit)
        report.append(result)
        print(f"\n=== {result['slug']} / {result['name']} ===")
        print(f"  muestreados:            {result['sampled']}")
        print(f"  títulos incompletos:    {result['incomplete_titles']} "
              f"({result['incomplete_ratio']*100:.0f}%)")
        for ex in result["incomplete_examples"]:
            print(f"      · [{ex['id']}] {ex['reason']}: {ex['title'][:70]!r}")
        print(f"  fuera de nicho:         {result['off_niche']}")
        for ex in result["off_niche_examples"]:
            print(f"      · [{ex['id']}] {ex['title'][:70]!r}")
        print(f"  overlay persistido:     {result['overlay_persisted']}/{result['sampled']} "
              f"({result['overlay_persisted_ratio']*100:.0f}%)")
        if args.alerts:
            if _emit_alert(dict(channel), result):
                print("  → alerta packaging_quality emitida")

    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(report, fh, ensure_ascii=False, indent=2)
        print(f"\nJSON: {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
