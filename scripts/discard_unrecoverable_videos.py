#!/usr/bin/env python3
"""Discard never-uploaded videos whose media was purged and cannot be rebuilt.

`purge_dead_entity(delete_row=True)` already deletes the final MP4, narration
MP3/CTA audio, scene images/clips (and `.pollo.json` sidecars), asset-history
files, render segments and the DB rows. This script adds the two artifacts the
general retention policy preserves on purpose (main SRT/timestamps and the
per-video thumbnails) because they are junk for a discarded video.

Safety gates (all mandatory):
  * never uploaded (no ``yt_video_id``),
  * no queued/running job for that video,
  * status in ``error`` / ``validation_failed``,
  * dry-run by default; requires ``--apply`` to delete anything.

Usage:
    python3 scripts/discard_unrecoverable_videos.py --video-ids 2806,2813 --dry-run
    python3 scripts/discard_unrecoverable_videos.py --video-ids 2806,2813 --apply
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import settings
from database.db_extended import ExtendedDatabase
from pipeline.media_retention import purge_dead_entity


def _main_audio(video: dict) -> str:
    if video.get("audio_path"):
        return str(video["audio_path"])
    try:
        cp = json.loads(video.get("checkpoint_data") or "{}")
        return str((cp.get("tts") or {}).get("audio_path") or "")
    except (json.JSONDecodeError, TypeError):
        return ""


def _delete_derived(audio_path: str, report: dict) -> None:
    if not audio_path:
        return
    base = Path(audio_path)
    for suffix in ("_subtitles.srt", "_timestamps.json"):
        p = base.with_name(f"{base.stem}{suffix}")
        try:
            if p.is_file():
                size = p.stat().st_size
                p.unlink()
                report["freed_bytes"] += size
                report["deleted"].append(str(p))
        except Exception as exc:  # noqa: BLE001
            report["errors"].append({"path": str(p), "error": str(exc)})


def _delete_thumbnails(video_id: int, canal: str, report: dict) -> None:
    if not canal:
        return
    thumb_dir = Path(settings.OUTPUT_DIR) / "thumbnails" / canal
    for name in (f"thumb_{video_id}.jpg", f"recover_{video_id}.jpg"):
        p = thumb_dir / name
        try:
            if p.is_file():
                size = p.stat().st_size
                p.unlink()
                report["freed_bytes"] += size
                report["deleted"].append(str(p))
        except Exception as exc:  # noqa: BLE001
            report["errors"].append({"path": str(p), "error": str(exc)})


def _active_job(db: ExtendedDatabase, video_id: int) -> bool:
    try:
        with db._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM generation_jobs "
                "WHERE video_id = ? AND status IN ('queued','running') LIMIT 1",
                (video_id,),
            ).fetchone()
        return row is not None
    except Exception:
        return True


def discard(db: ExtendedDatabase, video_ids: list[int], apply: bool) -> dict:
    summary = {"scanned": 0, "discarded": 0, "skipped": 0, "errors": 0,
               "freed_bytes": 0, "details": []}
    for vid in video_ids:
        summary["scanned"] += 1
        video = db.get_video(vid)
        detail = {"video_id": vid}
        if not video:
            detail.update(action="skipped", reason="not_found")
            summary["skipped"] += 1
            summary["details"].append(detail)
            continue
        detail["channel"] = video.get("canal")
        if (video.get("yt_video_id") or "").strip():
            detail.update(action="skipped", reason="uploaded")
            summary["skipped"] += 1
            summary["details"].append(detail)
            continue
        if video.get("status") not in ("error", "validation_failed"):
            detail.update(action="skipped",
                          reason=f"status_{video.get('status')}")
            summary["skipped"] += 1
            summary["details"].append(detail)
            continue
        if _active_job(db, vid):
            detail.update(action="skipped", reason="active_job")
            summary["skipped"] += 1
            summary["details"].append(detail)
            continue

        try:
            report = purge_dead_entity(
                db, "video", vid,
                reason="unrecoverable_purged_media",
                dry_run=not apply,
                delete_row=apply,
            )
        except Exception as exc:  # noqa: BLE001
            detail.update(action="error", reason=str(exc)[:200])
            summary["errors"] += 1
            summary["details"].append(detail)
            continue

        if report.get("skipped"):
            detail.update(action="skipped", reason=report["skipped"])
            summary["skipped"] += 1
            summary["details"].append(detail)
            continue

        if apply and report.get("db_deleted", {}).get("deleted"):
            _delete_derived(_main_audio(video), report)
            _delete_thumbnails(vid, str(video.get("canal") or ""), report)

        freed = int(report.get("freed_bytes") or 0)
        summary["freed_bytes"] += freed
        detail.update(
            action="would_discard" if not apply else "discarded",
            freed_mb=round(freed / (1024 * 1024), 1),
            files=len(report.get("deleted") or report.get("would_delete") or []),
            db_deleted=bool(report.get("db_deleted", {}).get("deleted")),
            incomplete=bool(report.get("incomplete")),
            errors=len(report.get("errors") or []),
        )
        summary["discarded"] += 1
        summary["details"].append(detail)
    return summary


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--video-ids", required=True,
                    help="comma-separated video ids")
    ap.add_argument("--apply", action="store_true",
                    help="actually purge media and delete DB rows")
    ap.add_argument("--dry-run", action="store_true",
                    help="explicit dry-run (default)")
    args = ap.parse_args()

    ids = [int(v) for v in str(args.video_ids).replace(" ", "").split(",") if v]
    apply = bool(args.apply and not args.dry_run)
    db = ExtendedDatabase()
    summary = discard(db, ids, apply=apply)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(
        f"\n{'APPLIED' if apply else 'DRY-RUN'}: "
        f"discarded={summary['discarded']} skipped={summary['skipped']} "
        f"errors={summary['errors']} "
        f"freed={summary['freed_bytes'] / (1024 * 1024):.1f} MB"
    )
    return 0 if summary["errors"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
