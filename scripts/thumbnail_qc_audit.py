#!/usr/bin/env python3
"""Read-only thumbnail QC audit (0 quota, 0 uploads).

Walks the most recent generated thumbnails, runs the deterministic QC in
``pipeline.thumbnail_qc`` and reports the issues that make a thumbnail fail in
the feed. It never mutates the DB and never contacts YouTube.

Usage:
    python3 scripts/thumbnail_qc_audit.py --days 60 --limit 200
    python3 scripts/thumbnail_qc_audit.py --canal canal3 --json reports/qc.json
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from collections import Counter, defaultdict
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
# Live data may live in the main tree while this script runs from a worktree.
MEDIA_ROOT = Path(os.environ.get("AUTOTUBE_MEDIA_ROOT", REPO_ROOT))
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from pipeline.packaging_brief import split_overlay_text  # noqa: E402
from pipeline.thumbnail_qc import run_qc  # noqa: E402


def _db_path() -> Path:
    raw = os.environ.get("DATABASE_PATH", "autotube.db")
    path = Path(raw)
    return path if path.is_absolute() else MEDIA_ROOT / path


def _connect(db_path: Path) -> sqlite3.Connection:
    uri = f"file:{db_path}?mode=ro"
    con = sqlite3.connect(uri, uri=True)
    con.row_factory = sqlite3.Row
    return con


def _estimate_box(layout: str, width: int, height: int) -> tuple[int, int, int, int]:
    if (layout or "") == "negative_space_top":
        return (int(width * 0.04), int(height * 0.04),
                int(width * 0.96), int(height * 0.42))
    return (int(width * 0.04), int(height * 0.55),
            int(width * 0.96), int(height * 0.95))


def audit(args) -> dict:
    db_path = _db_path()
    if not db_path.exists():
        raise SystemExit(f"DB not found: {db_path}")

    params: list = [f"-{int(args.days)} days"]
    where = ["COALESCE(v.thumbnail_path, '') != ''",
             "v.uploaded_at IS NOT NULL",
             "v.uploaded_at >= datetime('now', ?)"]
    if args.canal:
        placeholders = ",".join("?" for _ in args.canal)
        where.append(f"c.slug IN ({placeholders})")
        params.extend(args.canal)
    params.append(int(args.limit))

    query = f"""
        SELECT v.id, v.channel_id, v.titulo_final, v.thumbnail_path,
               v.thumbnail_text, v.thumbnail_layout, v.thumbnail_face_role,
               c.slug
        FROM videos v JOIN channels c ON c.id = v.channel_id
        WHERE {' AND '.join(where)}
        ORDER BY v.id DESC LIMIT ?
    """

    rows = []
    with _connect(db_path) as con:
        for row in con.execute(query, params):
            rows.append(dict(row))

    issue_counter: Counter = Counter()
    per_channel: dict[str, dict] = defaultdict(lambda: {
        "count": 0, "issues": Counter(), "scores": [], "missing_files": 0,
    })
    samples = []

    for row in rows:
        slug = row["slug"] or "?"
        path = Path(row["thumbnail_path"] or "")
        if not path.is_absolute():
            path = MEDIA_ROOT / path
        bucket = per_channel[slug]
        bucket["count"] += 1
        if not path.exists():
            bucket["missing_files"] += 1
            continue

        l1, l2 = split_overlay_text(row["thumbnail_text"] or "")
        try:
            from PIL import Image
            with Image.open(path) as img:
                width, height = img.size
            box = _estimate_box(row["thumbnail_layout"] or "", width, height)
            result = run_qc(
                path,
                l1=l1,
                l2=l2,
                title=row["titulo_final"] or "",
                text_boxes=[box],
                require_text=True,
            )
        except Exception as exc:  # noqa: BLE001
            bucket["issues"]["read_error"] += 1
            continue

        bucket["scores"].append(result.score)
        for issue in result.issues:
            bucket["issues"][issue] += 1
            issue_counter[issue] += 1
        if result.issues:
            samples.append({
                "video_id": row["id"],
                "slug": slug,
                "title": (row["titulo_final"] or "")[:90],
                "overlay": (row["thumbnail_text"] or "")[:60],
                "score": result.score,
                "issues": list(result.issues),
                "path": str(path),
            })

    summary = {
        "db": str(db_path),
        "days": int(args.days),
        "videos": len(rows),
        "issue_totals": dict(issue_counter.most_common()),
        "channels": {},
        "worst": sorted(samples, key=lambda s: s["score"])[:20],
    }
    for slug, data in sorted(per_channel.items()):
        scores = data["scores"]
        summary["channels"][slug] = {
            "videos": data["count"],
            "missing_files": data["missing_files"],
            "mean_score": round(sum(scores) / len(scores), 1) if scores else None,
            "issues": dict(data["issues"].most_common()),
        }
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--canal", action="append", default=[],
                        help="Channel slug (repeatable). Default: all.")
    parser.add_argument("--days", type=int, default=60,
                        help="Only videos uploaded in the last N days (default 60).")
    parser.add_argument("--limit", type=int, default=300,
                        help="Max videos to inspect (default 300).")
    parser.add_argument("--json", default="",
                        help="Write the full report to this JSON path.")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()

    report = audit(args)

    if args.json:
        out = Path(args.json)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, ensure_ascii=False, indent=2),
                       encoding="utf-8")

    if not args.quiet:
        print(f"Videos auditados: {report['videos']}")
        print(f"Issues: {report['issue_totals'] or 'ninguno'}")
        for slug, data in report["channels"].items():
            print(f"  {slug}: n={data['videos']} "
                  f"media={data['mean_score']} issues={data['issues'] or '{}'}")
        if report["worst"]:
            print("Peores:")
            for item in report["worst"][:10]:
                print(f"  #{item['video_id']} [{item['slug']}] "
                      f"{item['score']} {item['issues']} :: {item['title']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
