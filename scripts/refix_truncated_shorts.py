#!/usr/bin/env python3
"""Re-render queued (NOT published) shorts whose MP4 lost narration.

The oct/nov 2026 render bug used a short stock clip for a long scene; the
xfade chain collapsed and ``-shortest`` cut the tail of the narration.  This
script rebuilds those MP4s from the existing MP3 + the assets recorded in
``short_asset_history``.  It never re-uploads and never touches published
shorts.

Usage:
    python3 scripts/refix_truncated_shorts.py --dry-run
    python3 scripts/refix_truncated_shorts.py --apply
"""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from config.settings import DATABASE_PATH  # noqa: E402
from pipeline.audio_utils import media_duration  # noqa: E402
from pipeline.shorts_media import render_short_hybrid, validate_short_render  # noqa: E402

SLACK = 1.5
BACKUP_DIR = ROOT / "output" / "videos" / "shorts" / "_truncated_backup"
VIDEO_SUFFIXES = {".mp4", ".mov", ".mkv", ".webm"}


def _abs(path: str) -> Path:
    p = Path(path)
    return p if p.is_absolute() else (ROOT / p)


def find_short_audio(file_path: str) -> tuple[Path | None, Path | None]:
    p = Path(file_path)
    name = p.name
    if name.startswith("standalone_"):
        return None, None  # temp audio was deleted
    if name.startswith("sched_short_"):
        audio_name = "sched_audio_" + name[len("sched_short_"):].rsplit(".", 1)[0] + ".mp3"
    elif name.startswith("native_short_"):
        audio_name = "native_audio_" + name[len("native_short_"):].rsplit(".", 1)[0] + ".mp3"
    else:
        return None, None
    audio = p.with_name(audio_name)
    srt = audio.with_suffix(".srt")
    return (audio if audio.exists() else None, srt if srt.exists() else None)


def short_assets(conn: sqlite3.Connection, short_id: int) -> list[dict]:
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT file_path FROM short_asset_history "
        "WHERE short_id=? AND purged_at IS NULL ORDER BY id ASC",
        (short_id,),
    ).fetchall()
    assets: list[dict] = []
    for r in rows:
        p = _abs(r["file_path"] or "")
        if not p.exists():
            continue
        typ = "video" if p.suffix.lower() in VIDEO_SUFFIXES else "image"
        assets.append({"path": str(p), "type": typ})
    return assets


def _srt_matches_audio(srt: Path | None, audio_duration: float) -> bool:
    if not srt:
        return False
    try:
        text = srt.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return False
    import re
    matches = re.findall(r"(\d+):(\d+):(\d+),(\d+)", text)
    if not matches:
        return False
    last = max(
        int(h) * 3_600_000 + int(m) * 60_000 + int(s) * 1000 + int(ms)
        for h, m, s, ms in matches
    ) / 1000.0
    return abs(audio_duration - last) <= 2.0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true", help="write changes (default: dry-run)")
    ap.add_argument("--status", default="generated")
    ap.add_argument("--channel", default="")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--json-out", default="")
    args = ap.parse_args()

    statuses = [s.strip() for s in args.status.split(",") if s.strip()]
    placeholders = ",".join("?" for _ in statuses)
    conn = sqlite3.connect(str(DATABASE_PATH))
    conn.row_factory = sqlite3.Row
    sql = (
        "SELECT s.id, s.channel_id, c.slug, s.title, s.status, s.file_path "
        "FROM shorts s LEFT JOIN channels c ON c.id=s.channel_id "
        f"WHERE s.status IN ({placeholders}) AND s.file_path LIKE 'output/videos/shorts/%'"
    )
    params: list = list(statuses)
    if args.channel:
        sql += " AND c.slug=?"
        params.append(args.channel)
    sql += " ORDER BY s.id DESC"
    rows = conn.execute(sql, params).fetchall()

    report: list[dict] = []
    changed = 0
    for r in rows:
        if args.limit and len(report) >= args.limit:
            break
        name = Path(r["file_path"]).name
        entry = {
            "id": r["id"], "channel": r["slug"], "title": r["title"],
            "status": r["status"], "action": "", "reason": "",
        }
        if name.startswith("standalone_"):
            entry.update(action="skip", reason="standalone: audio temporal eliminado")
            report.append(entry)
            continue
        mp3, srt = find_short_audio(r["file_path"])
        if not mp3:
            entry.update(action="skip", reason="MP3 no encontrado")
            report.append(entry)
            continue
        mp4 = _abs(r["file_path"])
        if not mp4.exists():
            entry.update(action="skip", reason="MP4 no encontrado")
            report.append(entry)
            continue
        audio_dur = media_duration(mp3)
        video_dur = media_duration(mp4)
        if not audio_dur or not video_dur:
            entry.update(action="skip", reason="duración no medible")
            report.append(entry)
            continue
        entry.update(mp3_duration=round(audio_dur, 2), mp4_duration=round(video_dur, 2))
        if video_dur >= audio_dur - SLACK:
            entry.update(action="skip", reason="no truncado")
            report.append(entry)
            continue

        assets = short_assets(conn, r["id"])
        if not assets:
            entry.update(action="skip", reason="sin assets recuperables")
            report.append(entry)
            continue

        entry["assets"] = len(assets)
        if not args.apply:
            entry.update(action="would-render", reason=f"pierde {audio_dur - video_dur:.1f}s")
            report.append(entry)
            continue

        from config.config_bridge import get_channel_config
        cfg = get_channel_config(r["slug"])
        max_dur = getattr(cfg, "SHORTS_MAX_DURATION_SEC", 58.0)
        tmp_out = mp4.with_name(mp4.stem + ".refix.mp4")
        use_srt = srt if _srt_matches_audio(srt, audio_dur) else None
        try:
            render_short_hybrid(
                asset_items=assets,
                audio_path=mp3,
                output_path=tmp_out,
                audio_duration=audio_dur + 1.5,
                srt_path=use_srt,
                scene_ranges=None,
                max_duration_sec=max_dur,
            )
            meta = validate_short_render(
                tmp_out, max_duration_sec=max_dur, expected_duration_sec=audio_dur,
            )
        except Exception as exc:  # noqa: BLE001
            entry.update(action="error", reason=str(exc)[:200])
            report.append(entry)
            try:
                tmp_out.unlink(missing_ok=True)
            except OSError:
                pass
            continue

        try:
            BACKUP_DIR.mkdir(parents=True, exist_ok=True)
            backup = BACKUP_DIR / mp4.name
            if not backup.exists():
                shutil.copy2(mp4, backup)
            tmp_out.replace(mp4)
            conn.execute(
                "UPDATE shorts SET duration=?, updated_at=datetime('now') WHERE id=?",
                (meta["duration_sec"], r["id"]),
            )
            conn.commit()
        except Exception as exc:  # noqa: BLE001
            entry.update(action="error", reason=f"no se pudo reemplazar: {exc}"[:200])
            report.append(entry)
            continue

        entry.update(
            action="rendered",
            new_duration=meta["duration_sec"],
            reason=f"recuperados {audio_dur - video_dur:.1f}s de narración",
        )
        changed += 1
        report.append(entry)

    conn.close()

    print(f"Shorts analizados: {len(report)} | re-renderizados: {changed} | apply={args.apply}")
    for e in report:
        print(
            f"  #{e['id']} {e.get('channel')} [{e['action']}] "
            f"{str(e.get('title') or '')[:55]!r} — {e.get('reason','')}"
        )
    if args.json_out:
        out = Path(args.json_out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"JSON: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
