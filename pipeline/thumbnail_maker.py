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

from config.canal1_config import (
    CANAL_DISPLAY_NAME,
    COLOR_PALETTE,
    THUMBNAIL_BORDER_WIDTH,
    THUMBNAIL_HEIGHT,
    THUMBNAIL_WIDTH,
    THUMBNAIL_FONT_SIZE,
)
from config.settings import THUMBNAILS_DIR

logger = logging.getLogger(__name__)


def _find_font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    """Find an available TrueType font, falling back to default."""
    candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
        "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
        "/usr/share/fonts/truetype/freefont/FreeSans.ttf",
        "/usr/share/fonts/truetype/ubuntu/Ubuntu-Bold.ttf",
        "/usr/share/fonts/truetype/ubuntu/Ubuntu-Regular.ttf",
    ]
    kwargs = {}
    if not bold and "Bold" in candidates[0]:
        for p in candidates:
            if "Bold" not in p:
                kwargs["candidates"] = candidates[1:]  # will still try bold first as fallback
                break

    for path in candidates:
        if Path(path).exists():
            return ImageFont.truetype(path, size)
    logger.warning("No TrueType font found; using default bitmap font.")
    return ImageFont.load_default()


class ThumbnailMaker:
    """Generates YouTube thumbnails with image, gradient overlay, and title text."""

    def __init__(self, config=None):
        if config is None:
            from config import canal1_config

            config = canal1_config
        self.width = getattr(config, "THUMBNAIL_WIDTH", THUMBNAIL_WIDTH)
        self.height = getattr(config, "THUMBNAIL_HEIGHT", THUMBNAIL_HEIGHT)
        self.font_size = getattr(config, "THUMBNAIL_FONT_SIZE", THUMBNAIL_FONT_SIZE)
        self.border_width = getattr(config, "THUMBNAIL_BORDER_WIDTH", THUMBNAIL_BORDER_WIDTH)
        self.palette = getattr(config, "COLOR_PALETTE", COLOR_PALETTE)
        self.channel_name = getattr(config, "CANAL_DISPLAY_NAME", CANAL_DISPLAY_NAME)
        self.output_dir = Path(getattr(config, "THUMBNAILS_DIR", THUMBNAILS_DIR))
        self._channel_cfg = config

        self.font = _find_font(self.font_size, bold=True)
        self.font_small = _find_font(int(self.font_size * 0.65), bold=True)

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
            canal_slug: Channel slug for cache key (e.g. "canal1").
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
        slug = canal_slug or "canal1"

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
        """Generate thumbnail image via Pollo AI with LLM quality review.

        Generates 2 images, has the vision LLM score them. If score < threshold,
        refines the prompt and retries (max THUMBNAIL_MAX_QC_ATTEMPTS times).
        """
        from config.settings import THUMBNAIL_QUALITY_THRESHOLD, THUMBNAIL_MAX_QC_ATTEMPTS
        from pipeline.thumbnail_style_engine import build_pollo_prompt
        from pipeline.ai_image_generator import AIImageGenerator

        threshold = THUMBNAIL_QUALITY_THRESHOLD
        max_attempts = THUMBNAIL_MAX_QC_ATTEMPTS

        prompt = build_pollo_prompt(brief.image_concept, style)
        ai_gen = AIImageGenerator()

        for attempt in range(1, max_attempts + 1):
            logger.info(
                "QC attempt %d/%d (prompt=%r...)",
                attempt, max_attempts, prompt[:80],
            )

            # Generate 2 variant images
            prompts = [
                prompt,
                prompt + " alternative composition, different angle",
            ]
            paths = ai_gen.generate_batch(
                prompts=prompts,
                output_dir=self.output_dir,
                prefix=f"qc_{slug}_a{attempt}_",
            )

            if len(paths) < 1:
                logger.error("QC attempt %d: no images generated", attempt)
                if attempt < max_attempts:
                    prompt = self._refine_prompt(prompt, "no images generated — try simpler composition")
                    time.sleep(5)
                    continue
                # Last attempt failed — use black fallback
                return self._black_fallback()

            if len(paths) == 1:
                # Only got 1 image — use it directly (skip QC review)
                logger.info("QC: only 1 image generated — using directly")
                return paths[0]

            # Review both images with vision LLM
            try:
                best_idx, score, feedback = self._review_with_vision_llm(
                    image_paths=paths,
                    expected_content=brief.image_concept,
                )
                logger.info("QC review: best=%d score=%.1f/10", best_idx + 1, score)

                if score >= threshold:
                    logger.info("✅ QC passed (%.1f ≥ %d)", score, threshold)
                    return paths[best_idx]

                logger.warning(
                    "QC score %.1f < threshold %d — refining prompt", score, threshold,
                )
                prompt = self._refine_prompt(prompt, feedback)

            except Exception as exc:
                logger.warning("QC vision review failed: %s — using first image", exc)
                return paths[0]

        # All attempts exhausted — return best available
        logger.warning("All QC attempts exhausted — returning first image")
        return paths[0] if paths else self._black_fallback()

    def _review_with_vision_llm(
        self,
        image_paths: list[Path],
        expected_content: str,
    ) -> tuple[int, float, str]:
        """Use vision-capable LLM to review and score generated images.

        Returns (best_index, score 0-10, feedback_string).
        """
        from openai import OpenAI
        from config.settings import LLM_API_KEY, LLM_BASE_URL, LLM_MODEL

        client = OpenAI(api_key=LLM_API_KEY, base_url=LLM_BASE_URL)

        # Read images as base64
        import base64

        image_contents = []
        for i, p in enumerate(image_paths):
            b64 = base64.b64encode(p.read_bytes()).decode("utf-8")
            image_contents.append({
                "type": "image_url",
                "image_url": {"url": f"data:image/jpeg;base64,{b64}", "detail": "low"},
            })

        # Build message with images
        user_content = [
            {
                "type": "text",
                "text": (
                    "Evalúa estas 2 imágenes generadas para una miniatura de YouTube.\n\n"
                    f"CONTENIDO ESPERADO: {expected_content}\n\n"
                    "Evalúa cada imagen del 0 al 10 en estos criterios:\n"
                    "1. Relevancia al contenido esperado\n"
                    "2. Calidad estética y composición\n"
                    "3. Impacto visual (¿haría click una persona?)\n"
                    "4. Iluminación y atmósfera\n"
                    "5. Adecuación para miniatura de YouTube (1280x720)\n\n"
                    "Responde SOLO con JSON:\n"
                    '{"best_index": 0, "scores": [8.5, 6.0], '
                    '"feedback": "breve explicación de por qué la mejor es mejor '
                    'y qué mejorar en la peor"}'
                ),
            },
            *image_contents,
        ]

        response = client.chat.completions.create(
            model=LLM_MODEL,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Eres un crítico de diseño visual especializado en miniaturas "
                        "de YouTube. Evalúas imágenes objetivamente y respondes JSON."
                    ),
                },
                {"role": "user", "content": user_content},
            ],
            temperature=0.3,
            max_tokens=300,
        )

        content = response.choices[0].message.content.strip()
        if content.startswith("```"):
            lines = content.split("\n")
            content = "\n".join(lines[1:-1])
            content = content.replace("```json", "").replace("```", "").strip()

        result = json.loads(content)
        best_idx = int(result.get("best_index", 0))
        scores = result.get("scores", [5.0, 5.0])
        avg_score = float(scores[best_idx]) if best_idx < len(scores) else 5.0
        feedback = str(result.get("feedback", ""))

        return best_idx, avg_score, feedback

    def _refine_prompt(self, original_prompt: str, feedback: str) -> str:
        """Use LLM to refine the image prompt based on QC feedback."""
        from openai import OpenAI
        from config.settings import LLM_API_KEY, LLM_BASE_URL, LLM_MODEL

        client = OpenAI(api_key=LLM_API_KEY, base_url=LLM_BASE_URL)

        refine_prompt = f"""Refina este prompt de generación de imagen para mejorar la calidad.

PROMPT ORIGINAL: {original_prompt}

FEEDBACK DE CALIDAD: {feedback}

INSTRUCCIONES:
- Corrige los problemas mencionados en el feedback
- Mantén el estilo cinematográfico oscuro
- Asegura que la imagen funcione como miniatura de YouTube (1280x720)
- El resultado debe ser un prompt de 1-3 frases en inglés

Responde SOLO con el prompt refinado, sin comillas ni JSON."""

        response = client.chat.completions.create(
            model=LLM_MODEL,
            messages=[
                {"role": "system", "content": "Eres un experto en prompts para generación de imágenes con IA."},
                {"role": "user", "content": refine_prompt},
            ],
            temperature=0.7,
            max_tokens=200,
        )

        refined = response.choices[0].message.content.strip()
        logger.info("Refined prompt: %r...", refined[:80])
        return refined

    def _black_fallback(self) -> Path:
        """Create a plain black fallback image."""
        fallback = self.output_dir / "_thumb_fallback.jpg"
        img = Image.new("RGB", (self.width, self.height), (10, 10, 15))
        img.save(fallback, "JPEG", quality=90)
        return fallback

    # ── F4: Final Composition ─────────────────────────────────

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

        # Color grading per style
        effects = style.get("effects", {})
        contrast_boost = float(effects.get("contrast_boost", 1.3))
        saturation = float(effects.get("saturation", 0.85))
        enhancer = ImageEnhance.Contrast(img)
        img = enhancer.enhance(contrast_boost)
        enhancer = ImageEnhance.Color(img)
        img = enhancer.enhance(saturation)

        # ── Focus vignette (radial darkening → draws eye to centre) ──
        img = self._apply_focus_vignette(img)

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
        viral_font = _find_font(viral_font_size, bold=True)

        if text_for_visual:
            lines = self._wrap_text_viral(
                draw, text_for_visual,
                max_width=int(self.width * 0.88),
                font=viral_font,
            )
            self._draw_viral_text_with_palette(
                draw, lines, viral_font, text_color, shadow_color,
            )

        # ── 4K Badge ─────────────────────────────────────────
        self._draw_4k_badge(draw)

        # ── Red brand border (fixed, all channels) ─────────────
        border_w = max(5, self.border_width + 1)
        red_border = (204, 0, 0)  # #CC0000 — brand red, high contrast
        for i in range(border_w):
            draw.rectangle(
                [i, i, self.width - 1 - i, self.height - 1 - i],
                outline=red_border,
            )

        # ── Classified stamp (if style matches) ──────────────
        layout = brief.layout or style.get("base_composition", "dark_reveal")
        if layout == "classified_document":
            self._add_classified_overlay(draw, {
                "accent": accent,
                "text_primary": text_color,
                "text_shadow": shadow_color,
            })

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
        badge_w, badge_h = 64, 30
        padding = 15
        x = self.width - badge_w - padding
        y = padding

        # Semi-transparent dark background
        bg_color = (10, 10, 10)
        draw.rounded_rectangle(
            [x, y, x + badge_w, y + badge_h],
            radius=6,
            fill=bg_color,
            outline=(255, 215, 0),   # yellow border
            width=2,
        )

        # "4K" text
        badge_font = _find_font(16, bold=True)
        text = "4K"
        bbox = draw.textbbox((0, 0), text, font=badge_font)
        tw = bbox[2] - bbox[0]
        th = bbox[3] - bbox[1]
        tx = x + (badge_w - tw) // 2
        ty = y + (badge_h - th) // 2 - 2
        draw.text((tx, ty), text, font=badge_font, fill=(255, 255, 255))

    # ── Focus vignette (radial darkening → eye drawn to centre) ──

    def _apply_focus_vignette(self, img: Image.Image) -> Image.Image:
        """Apply a soft radial vignette that darkens edges, brightening the centre.

        Creates a circular gradient mask: centre is transparent, corners are dark.
        This naturally guides the viewer's eye to the focal point.
        """
        w, h = self.width, self.height
        cx, cy = w / 2, h / 2
        max_r = max(cx, cy)

        # Radial gradient: 0 (centre) → 1 (corner)
        yv, xv = np.ogrid[:h, :w]
        dist = np.sqrt((xv - cx) ** 2 + (yv - cy) ** 2) / max_r
        # Soft curve: start darkening at ~40 % radius, fully dark at edges
        alpha = np.clip((dist - 0.35) / 0.65, 0, 1) * 140
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
    ) -> None:
        """Draw text with aggressive shadow for maximum contrast."""
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
                draw.text((sx, sy), line, font=font, fill=shadow_color)

            # Main text
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
