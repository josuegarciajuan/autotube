"""Shorts renderer: renders vertical 9:16 clips with animated burned-in subtitles.

Takes a source video + clip spec → renders a 1080x1920 Short with:
- Center/smart crop from 16:9 to 9:16
- Large animated subtitles (word-by-word highlight)
- Channel branding (small logo overlay)
"""

import logging
import os
import subprocess
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

# Subtitle style defaults
DEFAULT_FONT_SIZE = 68
DEFAULT_HIGHLIGHT_COLOR = "#FFD700"
DEFAULT_BG_ALPHA = 0.65
DEFAULT_WORDS_PER_LINE = 4


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
    ) -> Optional[Path]:
        """Render a single Short from a source video clip.

        Args:
            source_video: Path to the full 16:9 video file.
            clip_spec: Dict with {start_time, end_time, hook_title, hook_text, ...}.
            output_dir: Directory for output file (defaults to output/videos/shorts/).

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
        hook_text = clip_spec.get("hook_text", "")

        if output_dir is None:
            output_dir = OUTPUT_DIR / "videos" / "shorts"
        output_dir.mkdir(parents=True, exist_ok=True)

        # Generate output filename
        import time
        ts = int(time.time())
        base_name = source_video.stem
        output_path = output_dir / f"short_{base_name}_{start:.0f}s_{end:.0f}s_{ts}.mp4"

        logger.info(
            "Rendering Short: %s → %s (%.1fs-%.1fs, %.1fs duration)",
            source_video.name, output_path.name, start, end, duration,
        )

        # Strategy: Use FFmpeg for efficient crop + extract + subtitle burn
        try:
            self._render_with_ffmpeg(
                source_video, output_path, start, duration, hook_text
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
        hook_text: str,
    ):
        """Render using FFmpeg with efficient crop, scale, and subtitle burn."""
        # Crop filter: center crop 16:9 → 9:16
        # 16:9 source at 1920x1080 → we want 1080x1920
        # Source width / 9 * 16 = 1920/9*16 = 3413... no, 1920/9*16 is wrong
        # Actually: we want a 9:16 crop from 1920x1080.
        # Target width = 1080, target height = 1920.
        # But source is only 1080 high! So we need to:
        # 1. Take a crop of 607x1080 from the source (607 = 1080 * 9/16)
        # 2. Scale up to 1080x1920
        
        # Crop width from 16:9 source to get 9:16 aspect ratio
        # 1080 * 9/16 = 607.5 → crop 608x1080 then scale to 1080x1920
        crop_width = int(1080 * 9 / 16)  # = 607.5 → 607
        crop_x = (1920 - crop_width) // 2  # Center horizontally = 656

        # Build the FFmpeg filter chain
        # crop=w:h:x:y,scale=w:h
        filters = [
            f"crop={crop_width}:1080:{crop_x}:0",
            f"scale=1080:1920:flags=lanczos",
        ]

        # Add subtitle burn if we have text
        vf = ",".join(filters)
        
        if hook_text:
            # Add DrawText filter for burned subtitles at bottom
            # Split text into lines of ~4 words each
            words = hook_text.split()
            lines = []
            for i in range(0, len(words), DEFAULT_WORDS_PER_LINE):
                lines.append(" ".join(words[i:i + DEFAULT_WORDS_PER_LINE]))

            # For now, use a simple centered text at bottom
            # More sophisticated animation would require a complex filter chain
            text_filter = self._build_text_filter(lines, len(lines))
            vf += "," + text_filter

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
        
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=600,  # 10 minute timeout
        )

        if result.returncode != 0:
            # Log stderr for debugging
            logger.error("FFmpeg stderr:\n%s", result.stderr[-1000:])
            raise RuntimeError(f"FFmpeg exited with code {result.returncode}")

        logger.info("Short rendered successfully: %s", output_path)

    def _build_text_filter(self, lines: list, num_lines: int) -> str:
        """Build an FFmpeg drawtext filter for burned-in subtitles.
        
        Creates text at the bottom of the 1080x1920 frame with semi-transparent
        background and white text with highlight color.
        """
        if not lines:
            return "null"

        # Escape single quotes and special chars for FFmpeg
        def _escape(text: str) -> str:
            return text.replace("'", "'\\\\\\''").replace(":", "\\\\:").replace("%", "\\\\%")

        # Calculate vertical positions
        # Frame height: 1920. Text at bottom 40%.
        line_height = 80
        base_y = 1920 - 80 - (num_lines - 1) * line_height

        # For simplicity, we draw a single text block with a background box
        joined_text = "\\n".join(lines)
        escaped = _escape(joined_text)

        # Semi-transparent black box behind text
        filter_str = (
            f"drawtext=text='{escaped}':"
            f"fontsize={DEFAULT_FONT_SIZE}:"
            f"fontcolor=white:"
            f"x=(w-text_w)/2:"
            f"y=h-{40 + num_lines * line_height}:"
            f"box=1:"
            f"boxcolor=black@{DEFAULT_BG_ALPHA}:"
            f"boxborderw=20:"
            f"font='Sans':"
            f"line_spacing=10"
        )

        return filter_str

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
            # Load clip
            clip = VideoFileClip(str(source_video)).subclipped(start, end)

            # Center crop to 9:16
            crop_w = clip.w * 9 / 16
            crop_x = (clip.w - crop_w) / 2
            clip = clip.cropped(x1=crop_x, y1=0, x2=crop_x + crop_w, y2=clip.h)
            clip = clip.resized((1080, 1920))

            if hook_text:
                # Burn simple subtitle
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
