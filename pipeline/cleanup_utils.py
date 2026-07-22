"""Post-upload residual cleanup utilities.

Deletes all non-essential files after a video is successfully uploaded
to YouTube. Preserves thumbnails (panel display), SRT subtitles (SEO value),
and timestamps JSON (chapter regeneration).

Shared assets (images, video clips, AI scenes) are deleted unconditionally
because cross-video dedup tracks them in video_asset_history, making the
local files unnecessary.

Usage:
    from pipeline.cleanup_utils import cleanup_video_residuals

    bytes_freed = cleanup_video_residuals(db, video_id,
                                          audio_data=audio_data,
                                          logger=logger)
"""

import json
import logging
import re
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


def cleanup_video_residuals(
    db,
    video_id: int,
    audio_data: Optional[dict] = None,
    log=None,
) -> int:
    """Delete all non-essential files for a video after successful upload.

    Deletes:
    - MP3 narration audio + CTA audio
    - Timestamps JSON + subtitles SRT for CTA (not main body SRT/JSON)
    - All scene assets (images, video clips, AI scenes + .pollo.json)
    - MP4 video file (if not already deleted)

    Preserves:
    - Main narration SRT subtitles + timestamps JSON (SEO + analysis value)
    - Thumbnails (panel display)

    Args:
        db: Database connection (ExtendedDatabase instance).
        video_id: Video record ID.
        audio_data: Dict from TTS phase with audio_path, cta_audio_path, etc.
                    If None, queries DB for audio_path and checkpoint.
        log: Optional logger. Falls back to module logger.

    Returns:
        Total bytes freed.
    """
    _log = log or logger
    total_freed = 0

    try:
        video = db.get_video(video_id)
        if not video:
            _log.warning("cleanup: video %d not found in DB", video_id)
            return 0

        # ── 1. Delete main narration MP3 ────────────────────
        audio_path = ""
        if audio_data and isinstance(audio_data, dict):
            audio_path = audio_data.get("audio_path", "")
        if not audio_path:
            audio_path = video.get("audio_path", "")

        if audio_path:
            ap = Path(audio_path)
            total_freed += _safe_unlink(ap, _log, "main MP3")

        # ── 2. Delete CTA audio + derived files ─────────────
        cta_path = ""
        if audio_data and isinstance(audio_data, dict):
            cta_path = audio_data.get("cta_audio_path", "")
        if not cta_path:
            # Try checkpoint
            cp_raw = video.get("checkpoint_data", "{}")
            try:
                cp = json.loads(cp_raw) if isinstance(cp_raw, str) else cp_raw
                cta_path = cp.get("tts", {}).get("cta_audio_path", "")
            except (json.JSONDecodeError, TypeError):
                pass

        if cta_path:
            total_freed += _delete_audio_group(cta_path, _log, "CTA")

        # ── 3. Delete scene asset files ─────────────────────
        total_freed += _delete_scene_assets(db, video_id, _log)

        # ── 4. Delete local mp4 (if not already gone) ──────
        vp = video.get("video_path", "")
        if vp:
            total_freed += _safe_unlink(Path(vp), _log, "MP4")

        _log.info(
            "cleanup_video_residuals: video %d — freed %d bytes (%.1f MB)",
            video_id, total_freed, total_freed / (1024 * 1024),
        )

    except Exception as exc:
        _log.warning("cleanup_video_residuals: error for video %d: %s", video_id, exc)

    return total_freed


def _safe_unlink(path: Path, _log, label: str = "") -> int:
    """Safely delete a file. Returns bytes freed (0 if not found)."""
    if not path or not isinstance(path, Path):
        path = Path(str(path))
    try:
        if path.exists() and path.is_file():
            size = path.stat().st_size
            path.unlink()
            _log.info("cleanup: deleted %s: %s (%.1f MB)", label, path, size / (1024 * 1024))
            return size
    except Exception as exc:
        _log.warning("cleanup: could not delete %s %s: %s", label, path, exc)
    return 0


def _delete_audio_group(audio_path: str, _log, label: str = "") -> int:
    """Delete MP3, timestamps JSON, and subtitles SRT for an audio group."""
    freed = 0
    ap = Path(audio_path)
    freed += _safe_unlink(ap, _log, f"{label} MP3")

    stem_dir = ap.parent
    stem = ap.stem
    for suffix in ["_timestamps.json", "_subtitles.srt"]:
        derived = stem_dir / f"{stem}{suffix}"
        freed += _safe_unlink(derived, _log, f"{label} {suffix.lstrip('_')}")
    return freed


def _delete_scene_assets(db, video_id: int, _log) -> int:
    """Delete all scene asset files for a video.

    Returns total bytes freed.
    """
    freed = 0
    try:
        scenes = db.get_scenes(video_id)
    except Exception:
        _log.warning("cleanup: could not query scenes for video %d", video_id)
        return 0

    for scene in scenes:
        image_path = scene.get("image_path", "")
        if not image_path:
            continue

        # Extract actual file path(s) from image_path column
        paths = _extract_paths_from_image_path(image_path)
        for fp in paths:
            p = Path(fp) if Path(fp).is_absolute() else Path(
                __file__).resolve().parent.parent / fp
            freed += _safe_unlink(p, _log, "scene asset")

            # ── Also delete .pollo.json metadata sidecar ──
            if p.suffix.lower() in (".jpg", ".jpeg", ".png"):
                json_sidecar = p.with_suffix(".pollo.json")
                freed += _safe_unlink(json_sidecar, _log, "AI metadata")

    return freed


def _extract_paths_from_image_path(image_path_value) -> list[str]:
    """Extract actual file paths from image_path, handling both formats.

    Format A (newer): plain string like 'output/video_clips/pexels_abc123.mp4'
    Format B (legacy): dict repr like
        {'path': PosixPath('output/video_clips/pexels_video_27239437.mp4'), ...}
    """
    if not image_path_value:
        return []

    s = str(image_path_value).strip()

    # Plain path string — already normalized
    if not s.startswith("{"):
        return [s]

    # Legacy dict repr — try to extract path
    # Handle PosixPath('...') inside repr
    match = re.search(r"PosixPath\('([^']+)'\)", s)
    if match:
        return [match.group(1)]

    # Handle plain string path inside repr: {'path': 'output/...', ...}
    match = re.search(r"'path'\s*:\s*'([^']+)'", s)
    if match:
        return [match.group(1)]

    return []


def record_asset_in_history(db, video_id: int, asset: dict) -> None:
    """Record a downloaded asset in video_asset_history for cross-video dedup.

    Args:
        db: Database connection.
        video_id: Video record ID.
        asset: Asset dict with 'path', 'source', 'url' keys.
    """
    try:
        file_path = asset.get("path", "")
        if not file_path:
            return
        fp = Path(file_path)
        # Normalize to relative path
        if fp.is_absolute():
            try:
                fp = fp.relative_to(Path(__file__).resolve().parent.parent)
            except ValueError:
                fp = Path(file_path)
        rel = fp.as_posix()

        source = asset.get("source", "")
        asset_url = asset.get("url", "") or asset.get("download_url", "")

        db.insert_asset_history(
            video_id=video_id,
            file_path=rel,
            source=source,
            asset_url=asset_url,
        )
    except Exception as exc:
        logger.debug("record_asset_in_history: skipped for video %d: %s", video_id, exc)
