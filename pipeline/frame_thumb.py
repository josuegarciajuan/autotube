"""Extract a usable (non-black) thumbnail frame from a rendered video.

The reassembly / recovery paths used to grab a frame at a fixed ``00:00:15``
offset, which frequently lands in the channel's dark intro and produced an
effectively black thumbnail. This helper probes several timestamps and keeps
the brightest one, so recovered videos always get a visible thumbnail.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)

# Fractions of total duration to probe, ordered to prefer mid-video content
# (the intro/outro tend to be dark). Probing stops early once a good frame
# is found.
_PROBE_FRACTIONS = (0.30, 0.50, 0.70, 0.40, 0.60, 0.20, 0.80)

# Absolute offsets (seconds) used when duration can't be determined.
_FALLBACK_OFFSETS = (15.0, 30.0, 60.0, 90.0, 5.0)

_BLACK_LUMINANCE = 16      # pixel luminance below this = "near-black"
_GOOD_BLACKNESS_PCT = 8.0  # stop probing once a frame is this clean


def _probe_duration(video_path: Path) -> float:
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(video_path)],
            capture_output=True, text=True, timeout=15,
        )
        return float(out.stdout.strip()) if out.stdout.strip() else 0.0
    except Exception:
        return 0.0


def _frame_blackness_pct(path: Path) -> float | None:
    """Return the % of near-black pixels in an image, or None if unreadable."""
    try:
        from PIL import Image
        import numpy as np
        arr = np.asarray(Image.open(str(path)).convert("L"), dtype=np.float32)
        total = arr.size
        if total == 0:
            return None
        black = int(np.count_nonzero(arr < _BLACK_LUMINANCE))
        return (black / total) * 100.0
    except Exception:
        return None


def extract_thumbnail_frame(
    video_path, out_path, *, label: str = "", timeout: int = 30,
) -> Path | None:
    """Extract the brightest (least-black) frame from ``video_path`` into ``out_path``.

    Returns the output ``Path`` on success, or ``None`` if no usable frame
    could be extracted.
    """
    video_path = Path(video_path)
    out_path = Path(out_path)
    if not video_path.exists():
        logger.warning("frame_thumb: video not found: %s", video_path)
        return None

    out_path.parent.mkdir(parents=True, exist_ok=True)

    duration = _probe_duration(video_path)
    if duration > 0:
        candidates = [duration * f for f in _PROBE_FRACTIONS]
    else:
        candidates = list(_FALLBACK_OFFSETS)

    best_path: Path | None = None
    best_blackness = 100.0
    tmp_dir = Path(tempfile.mkdtemp(prefix="thumbprobe_"))
    try:
        for idx, ts in enumerate(candidates):
            tmp = tmp_dir / f"probe_{idx}.jpg"
            try:
                r = subprocess.run(
                    ["ffmpeg", "-y", "-v", "error", "-ss", f"{ts:.2f}",
                     "-i", str(video_path), "-vframes", "1", "-q:v", "2", str(tmp)],
                    capture_output=True, text=True, timeout=timeout,
                )
            except Exception:
                continue
            if r.returncode != 0 or not tmp.exists() or tmp.stat().st_size < 100:
                continue
            blackness = _frame_blackness_pct(tmp)
            if blackness is None:
                continue
            if blackness < best_blackness:
                best_blackness = blackness
                best_path = tmp
            if best_blackness <= _GOOD_BLACKNESS_PCT:
                break
    finally:
        if best_path is None:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    if best_path is None:
        logger.warning("frame_thumb: no usable frame extracted from %s", video_path)
        return None

    try:
        shutil.copyfile(best_path, out_path)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    if label:
        logger.info("frame_thumb [%s]: thumbnail saved %s (blackness=%.0f%%)",
                    label, out_path, best_blackness)
    else:
        logger.info("frame_thumb: thumbnail saved %s (blackness=%.0f%%)",
                    out_path, best_blackness)
    return out_path
