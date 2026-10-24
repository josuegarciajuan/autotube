"""P02-G — semantic temporal frame sampling.

Reuses the ffmpeg infrastructure in :mod:`pipeline.visual_verifier` (which is
also used for low-level QA) but samples several inset timestamps instead of
only 1–3 frames.  It degrades cleanly for images, very short clips, unknown
duration, corrupt frames and ffmpeg failures.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Sequence

from pipeline.visual_verifier import IMAGE_EXTS, VIDEO_EXTS, extract_video_frame

logger = logging.getLogger(__name__)


@dataclass
class FrameSamplingResult:
    frames: list[str] = field(default_factory=list)
    timestamps: list[float] = field(default_factory=list)
    duration: float | None = None
    failed: int = 0
    error: str = ""
    tmp_dir: str = ""
    is_static_image: bool = False

    @property
    def ok(self) -> bool:
        return bool(self.frames)

    def to_dict(self) -> dict[str, Any]:
        return {
            "frames": list(self.frames),
            "timestamps": [round(float(t), 3) for t in self.timestamps],
            "duration": self.duration,
            "failed": self.failed,
            "error": self.error,
            "is_static_image": self.is_static_image,
            "frame_count": len(self.frames),
        }

    def cleanup(self) -> None:
        if self.tmp_dir:
            shutil.rmtree(self.tmp_dir, ignore_errors=True)


def probe_media_duration(path: str | Path) -> float | None:
    """Probe duration with ffprobe; ``None`` when unknown/unavailable."""
    if shutil.which("ffprobe") is None:
        return None
    try:
        proc = subprocess.run(
            [
                "ffprobe", "-v", "error", "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1", str(path),
            ],
            capture_output=True, text=True, timeout=15,
        )
        if proc.returncode == 0:
            value = float((proc.stdout or "").strip())
            return value if value > 0 else None
    except Exception as exc:  # noqa: BLE001
        logger.debug("ffprobe duration failed for %s: %s", path, exc)
    return None


def sample_timestamps(duration: float, max_frames: int) -> list[float]:
    """Inset, evenly-distributed timestamps (avoid exact 0/end)."""
    n = max(1, min(24, int(max_frames)))
    duration = max(0.0, float(duration or 0.0))
    if duration <= 0:
        return []
    if n == 1:
        return [duration / 2.0]
    if duration < 0.6:
        # Too short for many distinct offsets; middle frame is the safest.
        return [duration / 2.0]
    usable = max(0.05, duration - max(0.12, duration * 0.06))
    start = min(0.10, duration * 0.04)
    points = [start + usable * i / (n - 1) for i in range(n)]
    return [min(duration - 0.02, max(0.0, p)) for p in points]


def _frame_time(ts: float) -> str:
    total = max(0.0, float(ts))
    return f"{int(total // 3600):02d}:{int((total % 3600) // 60):02d}:{total % 60:05.2f}"


def sample_semantic_frames(
    asset: dict[str, Any] | str | Path,
    *,
    max_frames: int = 6,
    duration: float | None = None,
    tmp_dir: str | Path | None = None,
    extractor: Callable[..., Path | None] | None = None,
    probe: Callable[[str | Path], float | None] | None = None,
) -> FrameSamplingResult:
    """Sample frames for one downloaded candidate asset.

    ``asset`` may be an asset dict (with ``path``/``type``/``duration``) or a
    path.  For images the file itself is the single frame (no ffmpeg).
    """
    if isinstance(asset, dict):
        raw_path = asset.get("path")
        atype = str(asset.get("type") or "").lower()
        declared = asset.get("duration", duration)
    else:
        raw_path = asset
        atype = ""
        declared = duration
    if not raw_path:
        return FrameSamplingResult(error="missing_asset_path")
    path = Path(str(raw_path))
    if not path.is_file():
        return FrameSamplingResult(error="asset_not_found")

    suffix = path.suffix.lower()
    if atype == "image" or suffix in IMAGE_EXTS:
        return FrameSamplingResult(
            frames=[str(path)], timestamps=[0.0], duration=None,
            is_static_image=True,
        )
    if atype != "video" and suffix not in VIDEO_EXTS:
        return FrameSamplingResult(error="unsupported_media_type")

    resolved_duration = None
    try:
        if declared is not None and float(declared) > 0:
            resolved_duration = float(declared)
    except (TypeError, ValueError):
        resolved_duration = None
    if resolved_duration is None:
        resolved_duration = (probe or probe_media_duration)(path)
    if resolved_duration is None or resolved_duration <= 0:
        resolved_duration = 5.0
        unknown = True
    else:
        unknown = False

    work = Path(tmp_dir) if tmp_dir else Path(tempfile.mkdtemp(prefix="p02_frames_"))
    work.mkdir(parents=True, exist_ok=True)
    result = FrameSamplingResult(duration=resolved_duration, tmp_dir=str(work))
    if unknown:
        result.error = "duration_unknown_default"
    points = sample_timestamps(resolved_duration, max_frames)
    extract = extractor or extract_video_frame
    for index, ts in enumerate(points):
        frame: Path | None = None
        try:
            try:
                frame = extract(path, work, ss=_frame_time(ts), tag=f"sem{index}")
            except TypeError:
                frame = extract(path, work, ss=_frame_time(ts))
        except Exception as exc:  # noqa: BLE001
            logger.debug("P02 frame extraction failed at %.2fs: %s", ts, exc)
        if frame and Path(frame).is_file():
            result.frames.append(str(frame))
            result.timestamps.append(ts)
        else:
            result.failed += 1
    if not result.frames:
        result.error = "frame_extraction_failed"
    return result
