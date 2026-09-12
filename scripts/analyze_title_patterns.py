#!/usr/bin/env python3
"""Read-only title pattern analysis.

Cross-references ``scripts.titulo_options`` + ``videos.script_id`` with
``video_stats_history`` (views / likes / retention proxies) to report, per
channel, how published titles are built and which ones perform.

This script NEVER writes to the database. It only reads it and emits a JSON
report to ``output/title_analysis_YYYYMMDD.json`` plus a stdout summary.

Usage:
    python3 scripts/analyze_title_patterns.py
    python3 scripts/analyze_title_patterns.py --db /path/to/autotube.db
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from pipeline.title_tokens import normalize, uppercase_words  # noqa: E402


def _parse_titles(raw) -> list[str]:
    if not raw:
        return []
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return []
    else:
        parsed = raw
    if not isinstance(parsed, list):
        return []
    return [str(t).strip() for t in parsed if str(t or "").strip()]


def _title_features(title: str) -> dict:
    text = (title or "").strip()
    caps = uppercase_words(text)
    return {
        "length": len(text),
        "has_number": bool(re.search(r"\d", text)),
        "is_question": text.endswith("?"),
        "caps_words": caps,
        "has_caps": len(caps) > 0,
        "has_broken_marker": bool("|" in text or "[" in text or "]" in text),
    }


def _load_rows(conn: sqlite3.Connection) -> list[dict]:
    tables = {
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    if "videos" not in tables:
        return []

    video_cols = {row[1] for row in conn.execute("PRAGMA table_info(videos)")}
    select_titles = "v.title_options" if "title_options" in video_cols else "NULL AS title_options"
    script_join = ""
    script_col = "NULL AS script_titles"
    if "scripts" in tables and "script_id" in video_cols:
        script_join = "LEFT JOIN scripts s ON s.id = v.script_id"
        script_col = "s.titulo_options AS script_titles"

    query = f"""
        SELECT v.id AS video_id,
               v.canal AS canal,
               v.script_id AS script_id,
               v.titulo_final AS title,
               {script_col},
               {select_titles}
        FROM videos v
        {script_join}
        WHERE v.titulo_final IS NOT NULL AND TRIM(v.titulo_final) <> ''
    """
    rows = [dict(r) for r in conn.execute(query)]

    stats: dict[int, dict] = {}
    if "video_stats_history" in tables:
        stats_query = """
            SELECT video_id, views, likes, comments,
                   estimated_minutes_watched, average_view_duration, fetched_at
            FROM video_stats_history
            ORDER BY fetched_at ASC, id ASC
        """
        for r in conn.execute(stats_query):
            stats[r["video_id"]] = dict(r)

    for row in rows:
        row["stats"] = stats.get(row["video_id"], {})
    return rows


def _aggregate(title_rows: list[dict]) -> dict:
    features = [_title_features(r["title"]) for r in title_rows]
    n = len(features)
    if not n:
        return {
            "count": 0, "avg_length": 0.0, "pct_number": 0.0,
            "pct_question": 0.0, "pct_caps": 0.0, "pct_broken_marker": 0.0,
            "titles": [], "top_titles": [], "bottom_titles": [],
        }

    def _pct(pred) -> float:
        return round(100.0 * sum(1 for f in features if pred(f)) / n, 1)

    ranked = sorted(
        title_rows,
        key=lambda r: (r.get("stats", {}).get("views") or 0),
        reverse=True,
    )

    return {
        "count": n,
        "avg_length": round(sum(f["length"] for f in features) / n, 1),
        "pct_number": _pct(lambda f: f["has_number"]),
        "pct_question": _pct(lambda f: f["is_question"]),
        "pct_caps": _pct(lambda f: f["has_caps"]),
        "pct_broken_marker": _pct(lambda f: f["has_broken_marker"]),
        "titles": [
            {
                "video_id": r["video_id"],
                "title": r["title"],
                "length": f["length"],
                "views": r.get("stats", {}).get("views") or 0,
                "likes": r.get("stats", {}).get("likes") or 0,
                "avg_view_duration": r.get("stats", {}).get("average_view_duration") or 0,
                "min_watched": r.get("stats", {}).get("estimated_minutes_watched") or 0,
                "script_options": _parse_titles(r.get("script_titles")),
                "video_options": _parse_titles(r.get("title_options")),
            }
            for r, f in zip(title_rows, features)
        ],
        "top_titles": [
            {"video_id": r["video_id"], "title": r["title"],
             "views": r.get("stats", {}).get("views") or 0}
            for r in ranked[:10]
        ],
        "bottom_titles": [
            {"video_id": r["video_id"], "title": r["title"],
             "views": r.get("stats", {}).get("views") or 0}
            for r in ranked[-10:]
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only title pattern analysis")
    parser.add_argument("--db", default=None, help="Path to autotube.db")
    parser.add_argument("--out", default=None, help="Output JSON path")
    args = parser.parse_args()

    if args.db:
        db_path = Path(args.db)
    else:
        from config.settings import DATABASE_PATH
        db_path = Path(str(DATABASE_PATH))

    if not db_path.exists():
        print(f"ERROR: database not found: {db_path}", file=sys.stderr)
        return 2

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = _load_rows(conn)
    finally:
        conn.close()

    by_channel: dict[str, list[dict]] = {}
    for row in rows:
        canal = row.get("canal") or "?"
        by_channel.setdefault(canal, []).append(row)

    report = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "database": str(db_path),
        "channels": {canal: _aggregate(items) for canal, items in sorted(by_channel.items())},
    }

    out_path = Path(args.out) if args.out else (
        REPO_ROOT / "output" / f"title_analysis_{datetime.now():%Y%m%d}.json"
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"Title analysis written to {out_path}")
    print(f"Videos analysed: {len(rows)} across {len(by_channel)} channel(s)")
    for canal, agg in report["channels"].items():
        if not agg["count"]:
            print(f"  [{canal}] no published titles")
            continue
        top = agg["top_titles"][0] if agg["top_titles"] else {}
        print(
            f"  [{canal}] n={agg['count']} avg_len={agg['avg_length']} "
            f"num={agg['pct_number']}% q={agg['pct_question']}% "
            f"caps={agg['pct_caps']}% broken={agg['pct_broken_marker']}% "
            f"| top={top.get('title', '')[:50]!r} ({top.get('views', 0)} views)"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
