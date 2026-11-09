#!/usr/bin/env python3
"""Read-only audit of narration integrity (shorts + long-form).

Compares the real TTS audio with the rendered video and the subtitle
timestamps.  It never writes to the DB or touches media files.

Usage:
    python3 scripts/audit_narration_integrity.py --days 30
    python3 scripts/audit_narration_integrity.py --days 7 --strict
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from config.settings import DATABASE_PATH  # noqa: E402
from pipeline.audio_utils import media_duration  # noqa: E402

SHORTS_SLACK = 1.5
LONGFORM_SLACK = 1.5


def _looks_like_video(name: str) -> bool:
    return name.startswith(("sched_short_", "native_short_", "standalone_"))


def find_short_audio(file_path: str) -> tuple[Path | None, Path | None]:
    """Return (mp3, srt) for a scheduler/manual short; (None, None) if gone."""
    if not file_path:
        return None, None
    p = Path(file_path)
    name = p.name
    if name.startswith("standalone_"):
        return None, None  # audio lived in a temp dir and was deleted
    if name.startswith("sched_short_"):
        audio_name = "sched_audio_" + name[len("sched_short_"):].rsplit(".", 1)[0] + ".mp3"
    elif name.startswith("native_short_"):
        audio_name = "native_audio_" + name[len("native_short_"):].rsplit(".", 1)[0] + ".mp3"
    else:
        return None, None
    audio = p.with_name(audio_name)
    srt = audio.with_suffix(".srt")
    return (audio if audio.exists() else None, srt if srt.exists() else None)


def srt_last_end(path: Path) -> float | None:
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return None
    matches = re.findall(r"(\d+):(\d+):(\d+),(\d+)", text)
    if not matches:
        return None
    ms = [
        int(h) * 3_600_000 + int(m) * 60_000 + int(s) * 1000 + int(ms)
        for h, m, s, ms in matches
    ]
    return max(ms) / 1000.0


def ts_last_end(path: Path) -> float | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, list) or not data:
        return None
    try:
        return max(float(item.get("end_ms", 0)) for item in data) / 1000.0
    except (TypeError, ValueError):
        return None


def audit_shorts(conn: sqlite3.Connection, cutoff: str) -> list[dict]:
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """SELECT s.id, s.channel_id, c.slug, s.title, s.status, s.file_path,
                  s.duration, s.created_at
             FROM shorts s LEFT JOIN channels c ON c.id = s.channel_id
            WHERE s.file_path IS NOT NULL AND s.file_path <> ''
              AND s.created_at >= ?
            ORDER BY s.id DESC""",
        (cutoff,),
    ).fetchall()
    issues: list[dict] = []
    for r in rows:
        if not _looks_like_video(Path(r["file_path"]).name):
            continue
        mp3, srt = find_short_audio(r["file_path"])
        if not mp3:
            continue
        audio_dur = media_duration(mp3)
        video_dur = media_duration(r["file_path"])
        if not audio_dur or not video_dur:
            continue
        audio_lost = audio_dur - video_dur
        srt_end = srt_last_end(srt) if srt else None
        srt_drift = (audio_dur - srt_end) if srt_end else None
        problems = []
        if audio_lost > SHORTS_SLACK:
            problems.append(f"narration cut by {audio_lost:.1f}s")
        if srt_drift is not None and srt_drift > 2.0:
            problems.append(f"SRT drift {srt_drift:.1f}s")
        if problems:
            issues.append({
                "kind": "short",
                "id": r["id"],
                "channel": r["slug"],
                "title": r["title"],
                "status": r["status"],
                "mp3": str(mp3),
                "mp3_duration": round(audio_dur, 2),
                "mp4_duration": round(video_dur, 2),
                "srt_end": round(srt_end, 2) if srt_end else None,
                "problems": problems,
            })
    return issues


def audit_longform(conn: sqlite3.Connection, cutoff: str) -> list[dict]:
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """SELECT v.id, v.canal, v.titulo_final, v.audio_path, v.video_path,
                  v.created_at, v.status
             FROM videos v
            WHERE v.audio_path IS NOT NULL AND v.audio_path <> ''
              AND v.video_path IS NOT NULL AND v.video_path <> ''
              AND v.created_at >= ?
            ORDER BY v.id DESC""",
        (cutoff,),
    ).fetchall()
    issues: list[dict] = []
    for r in rows:
        audio_dur = media_duration(r["audio_path"])
        video_dur = media_duration(r["video_path"])
        if not audio_dur or not video_dur:
            continue
        ts_path = Path(r["audio_path"]).with_name(
            Path(r["audio_path"]).stem + "_timestamps.json"
        )
        ts_end = ts_last_end(ts_path) if ts_path.exists() else None
        problems = []
        # Long-form MP4 includes intro + CTA + outro, so it must be >= narration.
        if video_dur + LONGFORM_SLACK < audio_dur:
            problems.append(f"MP4 shorter than narration by {audio_dur - video_dur:.1f}s")
        if ts_end and audio_dur - ts_end > 2.0:
            problems.append(f"timestamp drift {audio_dur - ts_end:.1f}s")
        if problems:
            issues.append({
                "kind": "longform",
                "id": r["id"],
                "channel": r["canal"],
                "title": r["titulo_final"],
                "status": r["status"],
                "mp3": r["audio_path"],
                "mp3_duration": round(audio_dur, 2),
                "mp4_duration": round(video_dur, 2),
                "ts_end": round(ts_end, 2) if ts_end else None,
                "problems": problems,
            })
    return issues


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--strict", action="store_true", help="exit 1 if issues found")
    ap.add_argument("--json-out", default="")
    args = ap.parse_args()

    cutoff = time.strftime(
        "%Y-%m-%d %H:%M:%S", time.localtime(time.time() - args.days * 86400)
    )
    conn = sqlite3.connect(f"file:{DATABASE_PATH}?mode=ro", uri=True)
    try:
        issues = audit_shorts(conn, cutoff) + audit_longform(conn, cutoff)
    finally:
        conn.close()

    print(f"Ventana: últimos {args.days} días (desde {cutoff})")
    print(f"Incidencias: {len(issues)}")
    for item in issues:
        print(
            f"  [{item['kind']}] {item['channel']} #{item['id']} "
            f"{str(item.get('title') or '')[:55]!r}\n"
            f"      {', '.join(item['problems'])} "
            f"(mp3={item['mp3_duration']}s mp4={item['mp4_duration']}s)"
        )

    if args.json_out:
        out = Path(args.json_out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(issues, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"JSON: {out}")

    return 1 if (args.strict and issues) else 0


if __name__ == "__main__":
    raise SystemExit(main())
