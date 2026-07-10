"""YouTube thumbnail generator using Pillow + Pollo AI.

Supports two modes:
1. Classic: extract frame from video + overlay text (Pillow only)
2. Viral v2: 4-phase pipeline — Style Engine → Brainstorming →
   Pollo AI generation + QC → Composition with 4K badge
"""

import hashlib
import json
import logging
import random
import time
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont, ImageFilter, ImageEnhance

from config.canal2_config import (
    CANAL_DISPLAY_NAME,
    COLOR_PALETTE,
    THUMBNAIL_BORDER_WIDTH,
    THUMBNAIL_HEIGHT,
    THUMBNAIL_WIDTH,
    THUMBNAIL_FONT_SIZE,
)
from config.settings import THUMBNAILS_DIR

logger = logging.getLogger(__name__)


# ── Font registry ──────────────────────────────────────────────
# Maps short font-family names to a priority-ordered list of
# absolute TTF paths.  The first file found on disk wins.
_FONT_REGISTRY: dict[str, list[str]] = {
    "DejaVuSans-Bold": [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    ],
    "DejaVuSerif-Bold": [
        "/usr/share/fonts/truetype/dejavu/DejaVuSerif-Bold.ttf",
    ],
    "LiberationSerif-Bold": [
        "/usr/share/fonts/truetype/liberation/LiberationSerif-Bold.ttf",
    ],
}

# Default sans-serif candidates (used when font_name is not in registry)
_FONT_FALLBACK_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
    "/usr/share/fonts/truetype/freefont/FreeSans.ttf",
    "/usr/share/fonts/truetype/ubuntu/Ubuntu-Bold.ttf",
    "/usr/share/fonts/truetype/ubuntu/Ubuntu-Regular.ttf",
]


def _find_font(size: int, bold: bool = False,
               font_name: str | None = None) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    """Find an available TrueType font, falling back to default.

    Args:
        size: Font size in points.
        bold: When True AND font_name is None, prefer a Bold weight.
        font_name: Optional registry key (e.g. ``\"DejaVuSerif-Bold\"``).
            If the key exists in ``_FONT_REGISTRY`` those exact paths
            are tried first.  Otherwise the standard sans-serif
            candidates are used.
    """
    # ── 1. Try named registry entry ─────────────────────────────
    if font_name and font_name in _FONT_REGISTRY:
        for path in _FONT_REGISTRY[font_name]:
            if Path(path).exists():
                return ImageFont.truetype(path, size)
        logger.warning("Registered font %r not found on disk; falling back.", font_name)

    # ── 2. Standard sans-serif fallback ─────────────────────────
    candidates = list(_FONT_FALLBACK_CANDIDATES)
    if not bold and "Bold" in candidates[0]:
        # Push the first (bold) candidate to the end so regular is tried first
        candidates = candidates[1:] + [candidates[0]]

    for path in candidates:
        if Path(path).exists():
            return ImageFont.truetype(path, size)

    logger.warning("No TrueType font found; using default bitmap font.")
    return ImageFont.load_default()


class ThumbnailMaker:
    """Generates YouTube thumbnails with image, gradient overlay, and title text."""

    def __init__(self, config=None):
        if config is None:
            from config import canal2_config

            config = canal2_config
        self.width = getattr(config, "THUMBNAIL_WIDTH", THUMBNAIL_WIDTH)
        self.height = getattr(config, "THUMBNAIL_HEIGHT", THUMBNAIL_HEIGHT)
        self.font_size = getattr(config, "THUMBNAIL_FONT_SIZE", THUMBNAIL_FONT_SIZE)
        self.border_width = getattr(config, "THUMBNAIL_BORDER_WIDTH", THUMBNAIL_BORDER_WIDTH)
        self.palette = getattr(config, "COLOR_PALETTE", COLOR_PALETTE)
        self.channel_name = getattr(config, "CANAL_DISPLAY_NAME", CANAL_DISPLAY_NAME)
        self.output_dir = Path(getattr(config, "THUMBNAILS_DIR", THUMBNAILS_DIR))
        self._channel_cfg = config
        self._last_raw_base: Path | None = None  # raw Pollo image before F4 composition

        # ── Per-channel thumbnail customisation (v2.1) ──────────
        self.font_family = getattr(config, "THUMBNAIL_FONT_FAMILY", "DejaVuSans-Bold")
        self.border_color = self._parse_color(
            getattr(config, "THUMBNAIL_BORDER_COLOR", "#CC0000")
        )
        self.show_4k_badge = getattr(config, "THUMBNAIL_SHOW_4K_BADGE", True)
        self.text_stroke_width = getattr(config, "THUMBNAIL_TEXT_STROKE_WIDTH", 0)
        self.text_stroke_color = self._parse_color(
            getattr(config, "THUMBNAIL_TEXT_STROKE_COLOR", "#000000")
        )

        # ── Rescue-themed overlays (distress signal style) ────────
        self.rescue_mayday = getattr(config, "THUMBNAIL_RESCUE_MAYDAY", False)
        self.rescue_coordinates = getattr(config, "THUMBNAIL_RESCUE_COORDINATES", False)
        self.rescue_sin_senal = getattr(config, "THUMBNAIL_RESCUE_SIN_SENAL", False)

        self.font = _find_font(self.font_size, bold=True,
                               font_name=self.font_family)
        self.font_small = _find_font(int(self.font_size * 0.65), bold=True,
                                     font_name=self.font_family)

    def make(
        self, image_path: Path, title: str, channel_name: str | None = None,
        overlay_text: str | None = None,
    ) -> Path:
        """Create thumbnail from base image + title text.

        Steps:
        1. Load image → resize to 1280x720 center crop
        2. Dark gradient overlay on bottom 40%
        3. Text overlay (52pt, bold white with dark shadow) centered bottom third
           - If overlay_text is provided, it's used for the visual text (short, punchy, 3-5 words)
           - Otherwise, the full title is used (word-wrapped to max 2 lines)
        4. 4px border in accent color
        5. Save as JPEG to THUMBNAILS_DIR

        Returns Path to generated thumbnail.
        """
        image_path = Path(image_path)
        channel = channel_name or self.channel_name

        img = Image.open(image_path).convert("RGB")
        img = self._resize_center_crop(img)

        overlay = Image.new("RGBA", (self.width, self.height), (0, 0, 0, 0))
        draw_overlay = ImageDraw.Draw(overlay)

        overlay = self._draw_gradient_overlay(overlay, draw_overlay)
        img = Image.alpha_composite(img.convert("RGBA"), overlay).convert("RGB")

        draw = ImageDraw.Draw(img)
        
        # Use overlay_text for the visual if provided (short, punchy marketing text)
        text_for_visual = overlay_text.strip() if overlay_text else title
        lines = self._wrap_text(draw, text_for_visual, max_width=int(self.width * 0.85), max_lines=2)

        self._draw_text_with_shadow(draw, lines)

        if self.border_width > 0:
            accent = self.palette.get("accent", (200, 160, 40))
            for i in range(self.border_width):
                draw.rectangle(
                    [i, i, self.width - 1 - i, self.height - 1 - i],
                    outline=accent,
                )

        slug = self._slugify(title)[:50]
        out_path = self.output_dir / f"thumb_{slug}.jpg"
        self.output_dir.mkdir(parents=True, exist_ok=True)
        img.save(out_path, "JPEG", quality=90)
        logger.info("Thumbnail saved: %s", out_path)
        return out_path

    def make_from_video_frame(
        self, video_path: Path, title: str, overlay_text: str | None = None,
    ) -> Path:
        """Extract first frame via moviepy, then apply make().
        
        Args:
            video_path: Path to the MP4 video file
            title: YouTube title (used for filename slug)
            overlay_text: Optional short punchy text for thumbnail visual (3-5 words).
                          If not provided, the title text is used.
        """
        try:
            from moviepy import VideoFileClip
        except ImportError:
            logger.error("moviepy not installed; cannot extract frame.")
            raise

        video_path = Path(video_path)
        clip = VideoFileClip(str(video_path))
        frame_path = self.output_dir / f"_frame_{video_path.stem}.png"
        duration = clip.duration() if callable(clip.duration) else clip.duration
        clip.save_frame(frame_path, t=duration * 0.15)
        clip.close()

        thumb = self.make(frame_path, title, overlay_text=overlay_text)
        try:
            frame_path.unlink()
        except OSError:
            pass
        return thumb

    # ── Viral Thumbnail v2 (4-phase pipeline) ───────────────────

    def make_viral_thumbnail(
        self,
        title: str,
        overlay_text: str = "",
        keywords: list | None = None,
        scene_images: list | None = None,
        script_text: str = "",
        canal_slug: str = "",
        channel_display_name: str = "",
        channel_description: str = "",
        channel_theme: str = "",
        base_image_path: Path | None = None,
        video_id: int = 0,
    ) -> Path:
        """Create a CTR-optimized viral thumbnail using the 4-phase pipeline.

        Pipeline:
        F1. Style Engine → decide/load channel visual style (cached).
        F2. Brainstorming → psychology + marketing agents design the brief.
        F3. Image Gen + QC → Pollo AI generates 2 images, LLM reviews quality.
        F4. Composition → color grade + gradient + text + 4K badge + border.

        Args:
            title: Full YouTube title.
            overlay_text: Pre-computed marketing text (from metadata phase).
                If empty, the brainstorming agent will generate one.
            keywords: SEO keywords for context.
            scene_images: Unused in v2 (Pollo AI generates fresh images).
            script_text: First ~1500 chars of script for context.
            canal_slug: Channel slug for cache key (e.g. "canal2").
            channel_display_name: Display name for style engine.
            channel_description: About section for style engine.
            channel_theme: Theme summary for style engine.
            base_image_path: If provided, SKIP Pollo AI generation and
                only recompose the text overlay on this existing image.
                Used by phase_metadata() to avoid regenerating the image.
            video_id: Database video ID for per-video unique naming
                and per-channel subdirectory placement.

        Returns:
            Path to the generated thumbnail JPEG.
        """
        slug = canal_slug or "canal2"

        # ── F1: Style Engine (cached per channel) ──────────────
        logger.info("[Thumbnail v2] F1: Loading channel style for %s", slug)
        style = self._get_or_create_style(
            slug=slug,
            channel_name=channel_display_name or self.channel_name,
            description=channel_description,
            theme=channel_theme,
            keywords=keywords or [],
        )

        # ── F2: Brainstorming ──────────────────────────────────
        logger.info("[Thumbnail v2] F2: Brainstorming thumbnail concept")
        brief = self._run_brainstorm(
            script_text=script_text,
            title=title,
            keywords=keywords or [],
            style=style,
            channel_name=channel_display_name or self.channel_name,
            channel_theme=channel_theme,
        )

        # Use provided overlay_text or the one from brainstorming
        final_overlay = overlay_text.strip() if overlay_text else brief.text_overlay

        # ── F3: Image Generation + QC ──────────────────────────
        if base_image_path and Path(base_image_path).exists():
            logger.info("[Thumbnail v2] F3: Using existing base image (skip generation)")
            base_image = Path(base_image_path)
        else:
            logger.info("[Thumbnail v2] F3: Generating image via Pollo AI + QC")
            base_image = self._generate_with_quality_control(
                brief=brief,
                style=style,
                slug=slug,
            )

        # Store raw Pollo image BEFORE composition (for metadata phase recompose)
        self._last_raw_base = base_image if not (base_image_path and Path(base_image_path).exists()) else None

        # ── F4: Composition ────────────────────────────────────
        logger.info("[Thumbnail v2] F4: Composing final thumbnail")
        thumb_path = self._compose_final(
            base_image=base_image,
            brief=brief,
            style=style,
            overlay_text=final_overlay,
            title=title,
            canal_slug=slug,
            video_id=video_id,
        )

        logger.info("[Thumbnail v2] ✅ Complete: %s", thumb_path)
        return thumb_path

    # ── F1: Style Engine helpers ──────────────────────────────

    def _get_or_create_style(
        self,
        slug: str,
        channel_name: str,
        description: str,
        theme: str,
        keywords: list[str],
    ) -> dict:
        """Get cached style profile or create via LLM."""
        from pipeline.thumbnail_style_engine import get_channel_style, STYLE_DEFAULTS

        # Check for per-channel style override
        visual_style = getattr(self._channel_cfg, "THUMBNAIL_VISUAL_STYLE", "auto")

        if visual_style and visual_style != "auto":
            manual = getattr(self._channel_cfg, "THUMBNAIL_MANUAL_STYLE", None)
            if manual and manual.get("visual_style") == visual_style:
                logger.info("Using per-channel manual style: %s (%s)", slug, visual_style)
                return dict(manual)
            # Fallback to STYLE_DEFAULTS for this category
            defaults = STYLE_DEFAULTS.get(visual_style, STYLE_DEFAULTS.get("dark_cinematic", {}))
            if defaults:
                logger.info("Using STYLE_DEFAULTS for %s (channel %s)", visual_style, slug)
                style = dict(defaults)
                style["visual_style"] = visual_style
                return style

        return get_channel_style(
            channel_name=channel_name,
            description=description,
            theme=theme,
            keywords=keywords,
            channel_slug=slug,
        )

    # ── F2: Brainstorming helpers ─────────────────────────────

    def _run_brainstorm(
        self,
        script_text: str,
        title: str,
        keywords: list[str],
        style: dict,
        channel_name: str,
        channel_theme: str,
    ) -> "ThumbnailBrief":
        """Run psychology + marketing agents."""
        from pipeline.thumbnail_brainstorm import ThumbnailBrainstorm

        brainstorm = ThumbnailBrainstorm()
        return brainstorm.brainstorm(
            script_text=script_text[:1500] if script_text else "",
            title=title,
            keywords=keywords[:10] if keywords else [],
            style_profile=style,
            channel_name=channel_name,
            channel_theme=channel_theme,
        )

    # ── F3: Image Generation + Quality Control ────────────────

    def _generate_with_quality_control(
        self,
        brief: "ThumbnailBrief",
        style: dict,
        slug: str,
    ) -> Path:
        """Generate a single thumbnail image via Pollo AI.

        Generates exactly 1 image per call — no variants, no QC loop,
        no vision LLM review. Saves Pollo credits.
        """
        from pipeline.thumbnail_style_engine import build_pollo_prompt
        from pipeline.ai_image_generator import AIImageGenerator, PolloAIError

        prompt = build_pollo_prompt(brief.image_concept, style)
        logger.info("Pollo AI prompt: %r...", prompt[:80])

        ai_gen = AIImageGenerator()
        out_path = self.output_dir / f"qc_{slug}_a1_01.jpg"

        try:
            path = ai_gen.generate(prompt, out_path)
            if path and Path(path).exists():
                logger.info("Pollo image generated: %s", path)
                return Path(path)
        except PolloAIError as exc:
            logger.error("Pollo AI generation failed: %s", exc)
        except Exception as exc:
            logger.error("Unexpected error generating thumbnail: %s", exc)

        logger.warning("No image generated — using black fallback")
        return self._black_fallback()

    def _black_fallback(self) -> Path:
        """Create a plain black fallback image."""
        fallback = self.output_dir / "_thumb_fallback.jpg"
        img = Image.new("RGB", (self.width, self.height), (10, 10, 15))
        img.save(fallback, "JPEG", quality=90)
        return fallback

    # ── F4: Final Composition ─────────────────────────────────

    @staticmethod
    def _parse_color(value) -> tuple[int, int, int]:
        """Accept hex string '#RRGGBB' or a 3-tuple of ints."""
        if isinstance(value, tuple) and len(value) == 3:
            return value
        hex_str = str(value).lstrip("#")
        if len(hex_str) == 3:
            hex_str = "".join(c * 2 for c in hex_str)
        return tuple(int(hex_str[i:i + 2], 16) for i in (0, 2, 4))

    def _compose_final(
        self,
        base_image: Path,
        brief: "ThumbnailBrief",
        style: dict,
        overlay_text: str,
        title: str,
        canal_slug: str = "",
        video_id: int = 0,
    ) -> Path:
        """Apply channel-style composition to the base image.

        Layers applied:
        1. Color grading (contrast + saturation per style)
        2. Dark gradient overlay (bottom 50%)
        3. Marketing text overlay (UPPERCASE, bold, thick shadow)
        4. 4K badge (top-right corner)
        5. Accent border
        6. Classified stamp (if applicable)
        """
        # Load and resize
        img = Image.open(base_image).convert("RGB")
        img = self._resize_center_crop(img)

        # Color grading per style (boosted 1.5x for viral impact)
        effects = style.get("effects", {})
        contrast_boost = float(effects.get("contrast_boost", 1.3)) * 1.5
        saturation = float(effects.get("saturation", 0.85)) * 1.5
        enhancer = ImageEnhance.Contrast(img)
        img = enhancer.enhance(contrast_boost)
        enhancer = ImageEnhance.Color(img)
        img = enhancer.enhance(saturation)

        # Apply sharpening filter to the focal area
        enhancer = ImageEnhance.Sharpness(img)
        img = enhancer.enhance(2.0)


        # ── Focus vignette (radial darkening → draws eye to centre) ──
        # More subtle vignette to keep image bright
        img = self._apply_focus_vignette(img, strength=0.6)  # reduced from 1.0

        # Dark gradient overlay (bottom 50%)
        overlay = Image.new("RGBA", (self.width, self.height), (0, 0, 0, 0))
        draw_overlay = ImageDraw.Draw(overlay)
        start_y = int(self.height * 0.50)
        for y in range(start_y, self.height):
            alpha = int(200 * (y - start_y) / (self.height - start_y))
            draw_overlay.line([(0, y), (self.width, y)], fill=(0, 0, 0, alpha))
        # Subtle top gradient
        for y in range(0, int(self.height * 0.10)):
            alpha = int(50 * (1 - y / (self.height * 0.10)))
            draw_overlay.line([(0, y), (self.width, y)], fill=(0, 0, 0, alpha))
        img = Image.alpha_composite(img.convert("RGBA"), overlay).convert("RGB")
        draw = ImageDraw.Draw(img)

        # ── Marketing text overlay ───────────────────────────
        text_for_visual = overlay_text.strip() if overlay_text else brief.text_overlay
        text_style = style.get("text_style", {})
        use_uppercase = text_style.get("uppercase", True)
        if use_uppercase:
            text_for_visual = text_for_visual.upper()

        # Use channel color palette for text
        color_palette = style.get("color_palette", {})
        text_color = self._hex_to_rgb(color_palette.get("text", "#F5F0E8"))
        shadow_color = self._hex_to_rgb(color_palette.get("shadow", "#0A0A0A"))

        viral_font_size = int(self.font_size * 1.3)
        viral_font = _find_font(viral_font_size, bold=True,
                                font_name=self.font_family)

        if text_for_visual:
            lines = self._wrap_text_viral(
                draw, text_for_visual,
                max_width=int(self.width * 0.88),
                font=viral_font,
            )
            self._draw_viral_text_with_palette(
                draw, lines, viral_font, text_color, shadow_color,
                stroke_width=self.text_stroke_width,
                stroke_color=self.text_stroke_color,
            )

        # ── 4K Badge (per-channel configurable) ────────────────
        if self.show_4k_badge:
            self._draw_4k_badge(draw)

        # ── Border (per-channel configurable color) ─────────────
        border_w = max(6, self.border_width + 2)  # thicker for viral impact
        for i in range(border_w):
            draw.rectangle(
                [i, i, self.width - 1 - i, self.height - 1 - i],
                outline=self.border_color,
            )

        # ── Classified stamp (if style matches) ──────────────
        layout = brief.layout or style.get("base_composition", "dark_reveal")
        if layout == "classified_document":
            self._add_classified_overlay(draw, {
                "accent": accent,
                "text_primary": text_color,
                "text_shadow": shadow_color,
            })

        # ── Rescue-themed overlays (distress signal) ────────────
        if self.rescue_mayday:
            self._draw_mayday_banner(draw)
        if self.rescue_sin_senal:
            self._draw_sin_senal_stamp(draw)
        if self.rescue_coordinates:
            self._draw_coordinates_overlay(draw)

        # ── Save ─────────────────────────────────────────────
        if video_id and canal_slug:
            out_dir = self.output_dir / canal_slug
            out_path = out_dir / f"thumb_{video_id}.jpg"
        else:
            # Fallback: legacy naming for callers that don't pass video_id
            out_dir = self.output_dir
            slug = self._slugify(title)[:50]
            out_path = out_dir / f"thumb_v2_{slug}.jpg"
        out_dir.mkdir(parents=True, exist_ok=True)
        img.save(out_path, "JPEG", quality=95)
        logger.info("Viral v2 thumbnail saved: %s", out_path)
        return out_path

    def _draw_4k_badge(self, draw: ImageDraw.ImageDraw) -> None:
        """Draw a small '4K' badge in the top-right corner."""
        badge_w, badge_h = 128, 60
        padding = 15
        x = self.width - badge_w - padding
        y = padding

        # Semi-transparent dark background
        bg_color = (10, 10, 10)
        draw.rounded_rectangle(
            [x, y, x + badge_w, y + badge_h],
            radius=12,
            fill=bg_color,
            outline=(255, 215, 0),   # yellow border
            width=3,
        )

        # "4K" text
        badge_font = _find_font(32, bold=True)
        text = "4K"
        bbox = draw.textbbox((0, 0), text, font=badge_font)
        tw = bbox[2] - bbox[0]
        th = bbox[3] - bbox[1]
        tx = x + (badge_w - tw) // 2
        ty = y + (badge_h - th) // 2 - 2
        draw.text((tx, ty), text, font=badge_font, fill=(255, 255, 255))

    # ── Focus vignette (radial darkening → eye drawn to centre) ──

    def _apply_focus_vignette(self, img: Image.Image, strength: float = 1.0) -> Image.Image:
        """Apply a soft radial vignette that darkens edges, brightening the centre.

        Creates a circular gradient mask: centre is transparent, corners are dark.
        This naturally guides the viewer's eye to the focal point.

        Args:
            strength: Multiplier for vignette intensity (1.0 = normal, 0.5 = half).
        """
        w, h = self.width, self.height
        cx, cy = w / 2, h / 2
        max_r = max(cx, cy)

        # Radial gradient: 0 (centre) → 1 (corner)
        yv, xv = np.ogrid[:h, :w]
        dist = np.sqrt((xv - cx) ** 2 + (yv - cy) ** 2) / max_r
        # Soft curve: start darkening at ~40 % radius, fully dark at edges
        alpha = np.clip((dist - 0.35) / 0.65, 0, 1) * 140 * strength
        alpha = alpha.astype(np.uint8)

        vignette_overlay = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        vignette_arr = np.array(vignette_overlay)
        vignette_arr[:, :, 3] = alpha  # set alpha channel
        vignette_overlay = Image.fromarray(vignette_arr, "RGBA")

        return Image.alpha_composite(img.convert("RGBA"), vignette_overlay).convert("RGB")

    # ── Viral text rendering ────────────────────────────────

    def _draw_viral_text_with_palette(
        self,
        draw: ImageDraw.ImageDraw,
        lines: list[str],
        font: ImageFont.FreeTypeFont,
        text_color: tuple[int, int, int],
        shadow_color: tuple[int, int, int],
        stroke_width: int = 0,
        stroke_color: tuple[int, int, int] = (0, 0, 0),
    ) -> None:
        """Draw text with aggressive shadow for maximum contrast.

        When *stroke_width* > 0 a coloured outline is drawn around
        each glyph BEFORE the fill, so the text stays readable on
        bright backgrounds without relying solely on drop-shadows.
        """
        line_height = int(font.size * 1.3)
        total_height = line_height * len(lines)
        start_y = int(self.height * 0.62) + (
            self.height - int(self.height * 0.62) - total_height
        ) // 2

        shadow_offset = max(5, font.size // 12)

        for i, line in enumerate(lines):
            bbox = draw.textbbox((0, 0), line, font=font)
            text_width = bbox[2] - bbox[0]
            x = (self.width - text_width) // 2
            y = start_y + i * line_height

            # Multiple shadow layers for depth
            for sx, sy in [
                (x + shadow_offset, y + shadow_offset),
                (x + shadow_offset + 1, y + shadow_offset + 1),
                (x - 2, y - 2),
                (x + 2, y + 2),
                (x - 1, y + 1),
                (x + 1, y - 1),
            ]:
                draw.text((sx, sy), line, font=font, fill=shadow_color,
                          stroke_width=0)

            # Optional outline (stroke) - rendered before fill for clean edge
            if stroke_width > 0:
                draw.text((x, y), line, font=font,
                          fill=text_color,
                          stroke_width=stroke_width,
                          stroke_fill=stroke_color)
            else:
                # Main text (no stroke)
                draw.text((x, y), line, font=font, fill=text_color)

    # ── Helpers ──────────────────────────────────────────────

    @staticmethod
    def _hex_to_rgb(hex_color: str) -> tuple[int, int, int]:
        """Convert '#RRGGBB' or 'RRGGBB' hex string to (R, G, B) tuple."""
        hex_color = hex_color.lstrip("#")
        if len(hex_color) == 3:
            hex_color = "".join(c * 2 for c in hex_color)
        return tuple(int(hex_color[i:i + 2], 16) for i in (0, 2, 4))

    def _wrap_text_viral(
        self, draw: ImageDraw.ImageDraw, text: str, max_width: int, font: ImageFont.FreeTypeFont,
    ) -> list[str]:
        """Word-wrap for viral thumbnail (max 2 lines, aggressive truncation)."""
        words = text.upper().split()  # UPPERCASE for viral impact
        lines = []
        current = ""

        for word in words:
            test = f"{current} {word}".strip()
            bbox = draw.textbbox((0, 0), test, font=font)
            if bbox[2] - bbox[0] <= max_width:
                current = test
            else:
                if current:
                    lines.append(current)
                current = word
                if len(lines) >= 1:  # Only 2 lines max
                    break

        if current and len(lines) < 2:
            lines.append(current)

        if not lines:
            lines = [text.upper()[:40]]

        return lines[:2]

    def _draw_viral_text(
        self, draw: ImageDraw.ImageDraw, lines: list[str],
        font: ImageFont.FreeTypeFont, palette: dict,
    ) -> None:
        """Draw text with aggressive shadow for maximum contrast."""
        text_color = palette.get("text_primary", (240, 240, 245))
        shadow_color = palette.get("text_shadow", (5, 5, 10))
        line_height = int(font.size * 1.3)
        total_height = line_height * len(lines)
        start_y = int(self.height * 0.62) + (self.height - int(self.height * 0.62) - total_height) // 2

        # Thicker shadow offset for viral thumbnails
        shadow_offset = max(5, font.size // 12)

        for i, line in enumerate(lines):
            bbox = draw.textbbox((0, 0), line, font=font)
            text_width = bbox[2] - bbox[0]
            x = (self.width - text_width) // 2
            y = start_y + i * line_height

            # Multiple shadow layers for depth
            for sx, sy in [
                (x + shadow_offset, y + shadow_offset),
                (x + shadow_offset + 1, y + shadow_offset + 1),
                (x - 2, y - 2),
                (x + 2, y + 2),
                (x - 1, y + 1),
                (x + 1, y - 1),
            ]:
                draw.text((sx, sy), line, font=font, fill=shadow_color)

            # Main text
            draw.text((x, y), line, font=font, fill=text_color)

    def _add_classified_overlay(self, draw: ImageDraw.ImageDraw, palette: dict) -> None:
        """Add 'CLASSIFIED' / 'CONFIDENTIAL' aesthetic stamps."""
        accent = palette.get("accent", (220, 40, 40))
        small_font = _find_font(18, bold=True)

        # Top-right "CLASSIFIED" stamp
        stamp_text = "CLASIFICADO"
        bbox = draw.textbbox((0, 0), stamp_text, font=small_font)
        tw = bbox[2] - bbox[0]
        x = self.width - tw - 25
        y = 15

        # Red semi-transparent background for stamp
        draw.rectangle([x - 8, y - 4, x + tw + 8, y + bbox[3] - bbox[1] + 6],
                        fill=(*accent, 180), outline=accent)
        draw.text((x, y), stamp_text, font=small_font, fill=(255, 255, 255))

    # ── Rescue-themed overlays (distress signal style) ───────────

    def _draw_mayday_banner(self, draw: ImageDraw.ImageDraw) -> None:
        """Draw emergency MAYDAY hazard banner across the top of the thumbnail.

        A black strip with alternating orange diagonal hazard stripes and
        a bold white '⚠ MAYDAY' centred label, mimicking emergency rescue tape.
        """
        banner_h = 45
        rescue_orange = (255, 92, 0)
        black = (13, 13, 13)

        # ── Black background strip ──────────────────────────────
        draw.rectangle([0, 0, self.width, banner_h], fill=black)

        # ── Diagonal hazard stripes ─────────────────────────────
        stripe_spacing = 22
        stripe_width = 8
        for start_x in range(-banner_h, self.width + banner_h, stripe_spacing):
            # Each stripe is a parallelogram (slanted rectangle)
            draw.polygon([
                (start_x, 0),
                (start_x + stripe_width, 0),
                (start_x + banner_h + stripe_width, banner_h),
                (start_x + banner_h, banner_h),
            ], fill=rescue_orange)

        # ── MAYDAY text centred on top of stripes ──────────────
        banner_font = _find_font(26, bold=True)
        label = "⚠  MAYDAY"
        bbox = draw.textbbox((0, 0), label, font=banner_font)
        tw = bbox[2] - bbox[0]
        th = bbox[3] - bbox[1]
        tx = (self.width - tw) // 2
        ty = (banner_h - th) // 2 - 1

        # Black outline behind text for readability over stripes
        for ox, oy in [(-1, 0), (1, 0), (0, -1), (0, 1)]:
            draw.text((tx + ox, ty + oy), label, font=banner_font, fill=black)
        draw.text((tx, ty), label, font=banner_font, fill=(255, 255, 255))

    def _draw_coordinates_overlay(self, draw: ImageDraw.ImageDraw) -> None:
        """Draw fake GPS coordinates in bottom-left corner.

        Mimics an expedition log / distress beacon transmission.
        Coordinates are randomly chosen from a pool of real expedition
        locations to maintain authenticity.
        """
        import random as _random

        coords_pool = [
            "LAT: 64°08'N  ·  LONG: 21°56'W  ·  SIN CONTACTO",       # Iceland / Franklin area
            "LAT: 78°13'N  ·  LONG: 15°38'E  ·  ÚLTIMA POSICIÓN",    # Svalbard
            "LAT: 27°59'S  ·  LONG: 86°56'E  ·  SEÑAL PERDIDA",       # Everest region
            "LAT: 41°44'N  ·  LONG: 49°57'W  ·  SIN RESPUESTA",       # Titanic area
            "LAT: 68°50'S  ·  LONG: 90°35'W  ·  EXPEDICIÓN PERDIDA",   # Antarctic
        ]
        coord_text = _random.choice(coords_pool)

        # Use monospace font for authentic GPS/radio look
        mono_path = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"
        if Path(mono_path).exists():
            mono_font = ImageFont.truetype(mono_path, 14)
        else:
            mono_font = _find_font(14, bold=False)
        padding = 12
        y = self.height - 32

        # Semi-transparent dark background pill behind text
        bbox = draw.textbbox((0, 0), coord_text, font=mono_font)
        tw = bbox[2] - bbox[0]
        th = bbox[3] - bbox[1]
        draw.rounded_rectangle(
            [padding - 6, y - 4, padding + tw + 12 + 8, y + th + 4],
            radius=6,
            fill=(6, 12, 24),  # deep navy
            outline=(255, 92, 0),
            width=1,
        )
        # Orange accent dot before text
        draw.ellipse(
            [padding + 2, y + th // 2 - 3, padding + 8, y + th // 2 + 3],
            fill=(255, 92, 0),
        )
        draw.text((padding + 14, y), coord_text, font=mono_font,
                  fill=(235, 240, 245))

    def _draw_sin_senal_stamp(self, draw: ImageDraw.ImageDraw) -> None:
        """Draw a 'SIN SEÑAL' red stamp in the top-right corner.

        Positioned below the 4K badge (if present) or at default top-right offset.
        Uses a bold red pill with orange border for a 'stamped on' look.
        """
        # Position: below 4K badge if present, else default top-right
        badge_offset = 80 if self.show_4k_badge else 0
        stamp_x = self.width - 145
        stamp_y = 15 + badge_offset
        stamp_w = 130
        stamp_h = 36

        # Red pill background
        draw.rounded_rectangle(
            [stamp_x, stamp_y, stamp_x + stamp_w, stamp_y + stamp_h],
            radius=8,
            fill=(180, 35, 35),          # rescue red
            outline=(255, 92, 0),         # orange border
            width=2,
        )

        # "SIN SEÑAL" text centred
        stamp_font = _find_font(17, bold=True)
        label = "SIN SEÑAL"
        bbox = draw.textbbox((0, 0), label, font=stamp_font)
        tw = bbox[2] - bbox[0]
        th = bbox[3] - bbox[1]
        tx = stamp_x + (stamp_w - tw) // 2
        ty = stamp_y + (stamp_h - th) // 2 - 1
        draw.text((tx, ty), label, font=stamp_font, fill=(255, 255, 255))

    # ── helpers ──────────────────────────────────────────────────

    def _resize_center_crop(self, img: Image.Image) -> Image.Image:
        w, h = img.size
        target_ratio = self.width / self.height
        current_ratio = w / h

        if current_ratio > target_ratio:
            new_w = int(h * target_ratio)
            left = (w - new_w) // 2
            img = img.crop((left, 0, left + new_w, h))
        else:
            new_h = int(w / target_ratio)
            top = (h - new_h) // 2
            img = img.crop((0, top, w, top + new_h))

        return img.resize((self.width, self.height), Image.LANCZOS)

    def _draw_gradient_overlay(
        self, overlay: Image.Image, draw: ImageDraw.ImageDraw
    ) -> Image.Image:
        start_y = int(self.height * 0.55)
        end_y = self.height
        for y in range(start_y, end_y):
            alpha = int(180 * (y - start_y) / (end_y - start_y))
            draw.line([(0, y), (self.width, y)], fill=(0, 0, 0, alpha))
        return overlay

    def _wrap_text(
        self,
        draw: ImageDraw.ImageDraw,
        text: str,
        max_width: int,
        max_lines: int = 2,
    ) -> list[str]:
        words = text.split()
        lines: list[str] = []
        current = ""

        for word in words:
            test = f"{current} {word}".strip()
            bbox = draw.textbbox((0, 0), test, font=self.font)
            if bbox[2] - bbox[0] <= max_width:
                current = test
            else:
                if current:
                    lines.append(current)
                current = word
                if len(lines) >= max_lines - 1:
                    break

        if current:
            remaining = " ".join(words[len(" ".join(lines).split()) :])
            if remaining:
                bbox_full = draw.textbbox((0, 0), f"{current} {remaining}", font=self.font)
                if bbox_full[2] - bbox_full[0] > max_width:
                    truncated = ""
                    for ch in remaining:
                        test_line = f"{current} {truncated}{ch}"
                        bbox_test = draw.textbbox((0, 0), test_line, font=self.font)
                        if bbox_test[2] - bbox_test[0] <= max_width:
                            truncated += ch
                        else:
                            current = f"{current} {truncated.strip()}..."
                            break
                    else:
                        current = f"{current} {remaining}"
                else:
                    current = f"{current} {remaining}"
            lines.append(current)

        # If we couldn't fit anything (edge case), force single line
        if not lines:
            lines = [text[:50] + "..."]

        return lines[:max_lines]

    def _draw_text_with_shadow(
        self, draw: ImageDraw.ImageDraw, lines: list[str]
    ) -> None:
        shadow_color = self.palette.get("text_shadow", (10, 10, 10))
        text_color = self.palette.get("text", (230, 230, 230))
        line_height = int(self.font_size * 1.25)
        total_height = line_height * len(lines)
        start_y = int(self.height * 0.62) + (self.height // 2 - int(self.height * 0.62) - total_height) // 2

        shadow_offset = max(3, self.font_size // 18)

        for i, line in enumerate(lines):
            bbox = draw.textbbox((0, 0), line, font=self.font)
            text_width = bbox[2] - bbox[0]
            x = (self.width - text_width) // 2
            y = start_y + i * line_height

            for sx, sy in [
                (x + shadow_offset, y + shadow_offset),
                (x - 1, y - 1),
                (x + 1, y + 1),
            ]:
                draw.text((sx, sy), line, font=self.font, fill=shadow_color)

            draw.text((x, y), line, font=self.font, fill=text_color)

    @staticmethod
    def _slugify(text: str) -> str:
        out = ""
        for ch in text.lower():
            if ch.isalnum():
                out += ch
            elif ch in (" ", "-", "_"):
                out += "_"
        while "__" in out:
            out = out.replace("__", "_")
        return out.strip("_")
