"""Shorts renderer: renders vertical 9:16 clips with synced subtitles.

Takes a source video + clip spec → renders a 1080x1920 Short with:
- Center/smart crop from 16:9 to 9:16
- Synced word-level subtitles (from TTS timestamps) burned via FFmpeg
"""

import logging
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

import numpy as np

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:
    Image = None

from config.settings import OUTPUT_DIR

logger = logging.getLogger(__name__)

SHORTS_RESOLUTION = (1080, 1920)
SHORTS_FPS = 30
SHORTS_BITRATE = "6000k"

# Subtitle style for clip shorts (white text, black outline, no box, bottom center)
SUBTITLE_FORCE_STYLE = (
    "FontSize=24,Alignment=2,"
    "PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,"
    "BorderStyle=1,Outline=2.5,MarginV=60"
)


# ── SRT helpers (same pattern as shorts_tts.py) ────────────────────────────


def _ms_to_srt_time(ms: float) -> str:
    """Convert milliseconds to SRT timestamp: HH:MM:SS,mmm."""
    total = int(ms)
    hours = int(total // 3_600_000)
    minutes = int((total % 3_600_000) // 60_000)
    seconds = int((total % 60_000) // 1000)
    millis = int(total % 1000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{millis:03d}"


def _timestamps_to_srt(timestamps: list[dict]) -> str:
    """Convert word-level timestamps into SRT subtitle format.

    Args:
        timestamps: list of dicts with {word, start_ms, end_ms}.

    Returns:
        SRT-formatted string, or empty string if no timestamps.
    """
    if not timestamps:
        return ""

    blocks: list[dict] = []
    current_words = [timestamps[0]["word"]]
    current_start = timestamps[0]["start_ms"]
    current_end = timestamps[0]["end_ms"]

    for ts in timestamps[1:]:
        gap = ts["start_ms"] - current_end
        if gap < 200 and len(" ".join(current_words)) < 42:
            current_words.append(ts["word"])
            current_end = ts["end_ms"]
        else:
            blocks.append({
                "start_ms": current_start,
                "end_ms": current_end,
                "text": " ".join(current_words).strip(),
            })
            current_words = [ts["word"]]
            current_start = ts["start_ms"]
            current_end = ts["end_ms"]

    if current_words:
        blocks.append({
            "start_ms": current_start,
            "end_ms": current_end,
            "text": " ".join(current_words).strip(),
        })

    lines: list[str] = []
    for i, b in enumerate(blocks, 1):
        lines.append(str(i))
        lines.append(
            f"{_ms_to_srt_time(b['start_ms'])} --> "
            f"{_ms_to_srt_time(b['end_ms'])}"
        )
        lines.append(b["text"])
        lines.append("")

    return "\n".join(lines)


def _esc_ffmpeg(t: str) -> str:
    """Escape single-quotes / colons / percent for FFmpeg filter args."""
    return (
        t.replace("'", "'\\\\\\''")
        .replace(":", "\\\\:")
        .replace("%", "\\\\%")
    )


class ShortsRenderer:
    """Renders a clip from a source video into a vertical Short."""

    def __init__(self, channel_config=None):
        self.config = channel_config
        self._font_cache = {}

    def render(
        self,
        source_video: Path,
        clip_spec: dict,
        output_dir: Optional[Path] = None,
        word_timestamps: Optional[list[dict]] = None,
    ) -> Optional[Path]:
        """Render a single Short from a source video clip.

        Args:
            source_video: Path to the full 16:9 video file.
            clip_spec: Dict with {start_time, end_time, hook_title, ...}.
            output_dir: Directory for output file (defaults to output/videos/shorts/).
            word_timestamps: Optional list of word-level timestamps for the
                entire source video.  If provided, only those falling inside
                [start_time, end_time] are used to generate synced subtitles.
                Each entry needs {word, start_ms, end_ms}.

        Returns:
            Path to rendered Short .mp4 file, or None on failure.
        """
        source_video = Path(source_video)
        if not source_video.exists():
            logger.error("Source video not found: %s", source_video)
            return None

        start = clip_spec.get("start_time", 0)
        end = clip_spec.get("end_time", start + 60)
        duration = end - start

        if output_dir is None:
            output_dir = OUTPUT_DIR / "videos" / "shorts"
        output_dir.mkdir(parents=True, exist_ok=True)

        import time
        ts = int(time.time())
        base_name = source_video.stem
        output_path = output_dir / f"short_{base_name}_{start:.0f}s_{end:.0f}s_{ts}.mp4"

        logger.info(
            "Rendering Short: %s -> %s (%.1fs-%.1fs, %.1fs duration)",
            source_video.name, output_path.name, start, end, duration,
        )

        try:
            self._render_with_ffmpeg(
                source_video, output_path, start, duration,
                word_timestamps=word_timestamps,
            )
            return output_path
        except Exception as e:
            logger.error("FFmpeg render failed: %s", e)
            return None

    def _render_with_ffmpeg(
        self,
        source_video: Path,
        output_path: Path,
        start_sec: float,
        duration_sec: float,
        word_timestamps: Optional[list[dict]] = None,
    ):
        """Render using FFmpeg with efficient crop, scale, and subtitle burn.

        Subtitles are generated from word_timestamps filtered to the clip
        segment, converted to SRT, and burned via the ``subtitles`` filter
        (same styling as native shorts).
        """
        # Probe source video dimensions
        try:
            probe = subprocess.run(
                ["ffprobe", "-v", "error", "-select_streams", "v:0",
                 "-show_entries", "stream=width,height", "-of", "csv=p=0",
                 str(source_video)],
                capture_output=True, text=True, timeout=10,
            )
            src_w, src_h = map(int, probe.stdout.strip().split(","))
        except Exception:
            logger.warning("ffprobe failed, assuming 1920x1080 source")
            src_w, src_h = 1920, 1080

        # Center crop 16:9 -> 9:16
        crop_width = int(src_h * 9 / 16)
        crop_x = (src_w - crop_width) // 2

        # Build base filter: crop + scale
        filters = [
            f"crop={crop_width}:{src_h}:{crop_x}:0",
            f"scale=1080:1920:flags=lanczos",
        ]
        vf = ",".join(filters)

        # ── Generate SRT from segment word-timestamps ─────────────────
        srt_path: Optional[Path] = None
        srt_file: Optional[str] = None  # tempfile name we created

        if word_timestamps:
            end_sec = start_sec + duration_sec
            start_ms = start_sec * 1000
            end_ms = end_sec * 1000

            # Filter timestamps that fall inside the clip segment
            segment_ts: list[dict] = []
            for ts in word_timestamps:
                ts_start = float(ts.get("start_ms", 0))
                ts_end = float(ts.get("end_ms", 0))
                if ts_start >= start_ms and ts_end <= end_ms:
                    segment_ts.append({
                        "word": ts.get("word", ""),
                        "start_ms": ts_start - start_ms,
                        "end_ms": ts_end - start_ms,
                    })
                elif ts_start >= start_ms and ts_start < end_ms:
                    # Word starts inside but ends after — clamp end
                    segment_ts.append({
                        "word": ts.get("word", ""),
                        "start_ms": ts_start - start_ms,
                        "end_ms": min(ts_end, end_ms) - start_ms,
                    })

            if segment_ts:
                srt_content = _timestamps_to_srt(segment_ts)
                if srt_content.strip():
                    # Write to temp file for FFmpeg subtitles filter
                    fd, srt_file = tempfile.mkstemp(suffix=".srt", prefix="short_sub_")
                    os.write(fd, srt_content.encode("utf-8"))
                    os.close(fd)
                    srt_path = Path(srt_file)
                    logger.info(
                        "Generated SRT: %d phrase blocks for clip segment",
                        srt_content.count("\n\n"),
                    )

            if not srt_path:
                logger.debug("No word timestamps found in clip segment — no subtitles")

        # ── Append subtitles filter if SRT available ──────────────────
        if srt_path and srt_path.exists():
            escaped_path = _esc_ffmpeg(str(srt_path))
            subtitles_filter = (
                f"subtitles='{escaped_path}':"
                f"force_style='{SUBTITLE_FORCE_STYLE}'"
            )
            vf += "," + subtitles_filter

        cmd = [
            "ffmpeg",
            "-ss", str(start_sec),
            "-i", str(source_video),
            "-t", str(duration_sec),
            "-vf", vf,
            "-c:v", "libx264",
            "-preset", "medium",
            "-crf", "20",
            "-b:v", SHORTS_BITRATE,
            "-pix_fmt", "yuv420p",
            "-r", str(SHORTS_FPS),
            "-c:a", "aac",
            "-b:a", "128k",
            "-movflags", "+faststart",
            "-y",
            str(output_path),
        ]

        logger.debug("FFmpeg command: %s", " ".join(cmd))

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=600,
            )

            if result.returncode != 0:
                logger.error("FFmpeg stderr:\n%s", result.stderr[-1000:])
                raise RuntimeError(f"FFmpeg exited with code {result.returncode}")

            logger.info("Short rendered successfully: %s", output_path)

        finally:
            # Clean up temp SRT file
            if srt_file and os.path.exists(srt_file):
                try:
                    os.unlink(srt_file)
                except OSError:
                    pass

    def render_with_moviepy(
        self,
        source_video: Path,
        clip_spec: dict,
        output_dir: Optional[Path] = None,
    ) -> Optional[Path]:
        """Alternative render using MoviePy for more complex effects.

        Use this when you need word-by-word animation or advanced compositing.
        """
        try:
            from moviepy import VideoFileClip, CompositeVideoClip, TextClip, vfx
        except ImportError:
            logger.error("MoviePy not available for advanced Shorts rendering")
            return None

        source_video = Path(source_video)
        if not source_video.exists():
            return None

        start = clip_spec.get("start_time", 0)
        end = clip_spec.get("end_time", start + 60)
        hook_text = clip_spec.get("hook_text", "")

        if output_dir is None:
            output_dir = OUTPUT_DIR / "videos" / "shorts"
        output_dir.mkdir(parents=True, exist_ok=True)

        import time
        ts = int(time.time())
        output_path = output_dir / f"short_mv_{source_video.stem}_{ts}.mp4"

        try:
            clip = VideoFileClip(str(source_video)).subclipped(start, end)

            # Center crop to 9:16
            crop_w = clip.w * 9 / 16
            crop_x = (clip.w - crop_w) / 2
            clip = clip.cropped(x1=crop_x, y1=0, x2=crop_x + crop_w, y2=clip.h)
            clip = clip.resized((1080, 1920))

            if hook_text:
                txt = TextClip(
                    text=hook_text,
                    font_size=60,
                    color="white",
                    stroke_color="black",
                    stroke_width=3,
                    font="Arial",
                    method="caption",
                    size=(900, None),
                )
                txt = txt.with_position(("center", 1600)).with_duration(clip.duration)
                clip = CompositeVideoClip([clip, txt])

            clip.write_videofile(
                str(output_path),
                fps=SHORTS_FPS,
                codec="libx264",
                bitrate=SHORTS_BITRATE,
                audio_codec="aac",
                preset="medium",
            )

            clip.close()
            return output_path

        except Exception as e:
            logger.error("MoviePy render failed: %s", e)
            return None
