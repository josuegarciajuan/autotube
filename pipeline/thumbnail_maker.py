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

from config.settings import THUMBNAILS_DIR

# ── Default thumbnail dimensions (overridable per channel via config_bridge) ──
THUMBNAIL_WIDTH = 1280
THUMBNAIL_HEIGHT = 720
THUMBNAIL_FONT_SIZE = 56
THUMBNAIL_BORDER_WIDTH = 5
CANAL_DISPLAY_NAME = "Autotube"
COLOR_PALETTE: dict = {"primary": (180, 30, 30)}

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
            from config.config_bridge import get_channel_config
            from config import settings
            config = get_channel_config(settings.ACTIVE_CHANNELS[0])
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

        # ── Medical-themed overlays (clinical_mystery style) ───────
        self.medical_ecg = getattr(config, "THUMBNAIL_MEDICAL_ECG", False)
        self.medical_cross = getattr(config, "THUMBNAIL_MEDICAL_CROSS", False)
        self.medical_diagnosis = getattr(config, "THUMBNAIL_MEDICAL_DIAGNOSIS", False)

        self.font = _find_font(self.font_size, bold=True,
                               font_name=self.font_family)
        self.font_small = _find_font(int(self.font_size * 0.65), bold=True,
                                      font_name=self.font_family)

    # ── Layout-driven composition profiles ──────────────────────
    # Each layout maps to a set of visual complexity flags.
    # The F4 composition engine reads this dict to decide which
    # elements (insets, badge, border weight, gradient opacity,
    # vignette strength, number of text lines) are applied.
    LAYOUT_COMPOSITION: dict = {
        "shock_closeup": {
            "show_insets": False,
            "show_badge": False,
            "show_border": True,
            "border_width_factor": 0.5,
            "gradient_opacity": 120,
            "gradient_start_pct": 0.35,
            "text_lines": 1,
            "vignette_strength": 0.4,
        },
        "dark_reveal": {
            "show_insets": True,
            "inset_count": 1,
            "show_badge": True,
            "show_border": True,
            "border_width_factor": 0.7,
            "gradient_opacity": 120,
            "gradient_start_pct": 0.45,
            "text_lines": 2,
            "vignette_strength": 0.4,
        },
        "split_face": {
            "show_insets": False,
            "show_badge": True,
            "show_border": True,
            "border_width_factor": 1.0,
            "gradient_opacity": 140,
            "gradient_start_pct": 0.40,
            "text_lines": 2,
            "vignette_strength": 0.5,
        },
        "classified_document": {
            "show_insets": True,
            "inset_count": 2,
            "show_badge": True,
            "show_border": True,
            "border_width_factor": 1.0,
            "gradient_opacity": 200,
            "gradient_start_pct": 0.50,
            "text_lines": 2,
            "vignette_strength": 0.7,
        },
        "incomplete_puzzle": {
            "show_insets": True,
            "inset_count": 1,
            "show_badge": True,
            "show_border": True,
            "border_width_factor": 0.8,
            "gradient_opacity": 150,
            "gradient_start_pct": 0.45,
            "text_lines": 2,
            "vignette_strength": 0.55,
        },
    }

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
            scene_images: Existing video scene images (paths); one is reused
                as the inset recuadro image when available. No extra AI
                image generation is performed for the inset.
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
        slug = canal_slug or ""

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
        
        # ── v2: extract multi-text fields from brief ────────────
        text_gancho = brief.text_gancho if hasattr(brief, 'text_gancho') and brief.text_gancho else ""
        text_complemento = brief.text_complemento if hasattr(brief, 'text_complemento') and brief.text_complemento else ""
        badge_text = brief.badge_text if hasattr(brief, 'badge_text') and brief.badge_text else ""

        # ── P3 (ago 2026): el texto de la miniatura NUNCA debe repetir el
        # inicio del título (mata el curiosity gap y el CTR). Se recortan los
        # tokens duplicados del overlay/gancho antes de componer. ──
        final_overlay = self._dedupe_overlay(title, final_overlay)
        text_gancho = self._dedupe_overlay(title, text_gancho)

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
        # Reuse an existing video scene image for the inset recuadro —
        # avoids painting the secondary_scene prompt as literal text.
        inset_path = self._pick_inset_image(scene_images, base_image=base_image)
        if inset_path:
            logger.info("[Thumbnail v2] F4: inset recuadro image: %s", inset_path)
        thumb_path = self._compose_final(
            base_image=base_image,
            brief=brief,
            style=style,
            overlay_text=final_overlay,
            title=title,
            canal_slug=slug,
            video_id=video_id,
            text_gancho=text_gancho,
            text_complemento=text_complemento,
            badge_text=badge_text,
            layout=getattr(brief, 'layout', '') or '',
            inset_image_path=inset_path,
        )

        logger.info("[Thumbnail v2] ✅ Complete: %s", thumb_path)

        # ── P1 (ago 2026): registrar estilo + layout para el loop CTR→estilo ──
        if video_id:
            try:
                from database.db_extended import ExtendedDatabase
                db = ExtendedDatabase()
                db.update_video_thumbnail_style(
                    video_id,
                    str(style.get("visual_style", "") or ""),
                    str(getattr(brief, 'layout', '') or ''),
                )
            except Exception as exc:
                logger.warning("[Thumbnail v2] thumbnail_style persist failed: %s", exc)

        return thumb_path

    def make_variant_thumbnails(
        self,
        title: str,
        script_text: str = "",
        keywords: list | None = None,
        canal_slug: str = "",
        channel_display_name: str = "",
        channel_description: str = "",
        channel_theme: str = "",
        video_id: int = 0,
        variant_briefs: list | None = None,
        num_variants: int = 3,
        scene_images: list | None = None,
    ) -> list[Path]:
        """Generate multiple thumbnail variants for A/B testing.
        
        Strategy: generate ONE base image via Pollo AI (expensive),
        then recompose N variants with different text overlays (cheap).
        
        Each variant gets a different ThumbnailBrief from brainstorm_variants()
        or from the provided variant_briefs list. Only F4 (composition) runs
        for each variant — the base image is reused.

        Args:
            title: Full YouTube title.
            script_text: First ~1500 chars of script for context.
            keywords: SEO keywords for context.
            canal_slug: Channel slug for cache key.
            channel_display_name: Display name for style engine.
            channel_description: About section for style engine.
            channel_theme: Theme summary for style engine.
            video_id: Database video ID for unique naming.
            variant_briefs: Optional pre-generated list of ThumbnailBrief.
                If None, brainstorm_variants() is called to generate them.
            num_variants: Number of variants to generate (default 3).
            scene_images: Existing video scene images (paths); one is reused
                as the inset recuadro image when available.

        Returns:
            List of Path objects, one per generated thumbnail variant.
        """
        slug = canal_slug or ""
        
        # ── F1: Style Engine (shared, cached) ──────────────────
        style = self._get_or_create_style(
            slug=slug,
            channel_name=channel_display_name or self.channel_name,
            description=channel_description,
            theme=channel_theme,
            keywords=keywords or [],
        )
        
        # ── F2: Generate variant briefs if not provided ───────
        if variant_briefs is None or len(variant_briefs) == 0:
            try:
                from pipeline.thumbnail_brainstorm import ThumbnailBrainstorm
                brainstormer = ThumbnailBrainstorm()
                variant_briefs = brainstormer.brainstorm_variants(
                    script_text=script_text,
                    title=title,
                    keywords=keywords or [],
                    style_profile=style,
                    channel_name=channel_display_name or self.channel_name,
                    channel_theme=channel_theme,
                    num_variants=num_variants,
                )
            except Exception as exc:
                logger.warning("Brainstorm variants failed: %s — using single brief fallback", exc)
                brief = self._run_brainstorm(
                    script_text=script_text,
                    title=title,
                    keywords=keywords or [],
                    style=style,
                    channel_name=channel_display_name or self.channel_name,
                    channel_theme=channel_theme,
                )
                variant_briefs = [brief]  # Fallback: single variant
        
        # ── F3: Generate ONE base image (shared across variants) ──
        logger.info("[Thumbnail v2] F3: Generating shared base image for %d variants", len(variant_briefs))
        base_image = self._generate_with_quality_control(
            brief=variant_briefs[0],  # Use first brief for image gen
            style=style,
            slug=slug,
        )
        # Store for metadata recompose
        self._last_raw_base = base_image

        # Reuse an existing video scene image for the inset recuadro
        inset_path = self._pick_inset_image(scene_images, base_image=base_image)

        # ── F4: Compose each variant on the same base image ────
        variant_paths: list[Path] = []
        for i, brief in enumerate(variant_briefs):
            # Build overlay text from brief fields
            l1 = getattr(brief, 'text_gancho', '') or ''
            l2 = getattr(brief, 'text_complemento', '') or ''
            overlay = f"{l1} {l2}".strip() if l1 or l2 else getattr(brief, 'text_overlay', '')
            # P3: no repetir el inicio del título en el texto de la miniatura
            l1 = self._dedupe_overlay(title, l1)
            overlay = self._dedupe_overlay(title, overlay)
            badge = getattr(brief, 'badge_text', '') or ''
            layout = getattr(brief, 'layout', '') or ''
            
            thumb_path = self._compose_final(
                base_image=base_image,
                brief=brief,
                style=style,
                overlay_text=overlay,
                title=title,
                canal_slug=slug,
                video_id=video_id,
                text_gancho=l1,
                text_complemento=l2,
                badge_text=badge,
                layout=layout,
                inset_image_path=inset_path,
            )
            variant_paths.append(thumb_path)
            logger.info(
                "[Thumbnail v2] ✅ Variant %d/%d complete: %s", i + 1, len(variant_briefs), thumb_path
            )

        # ── P1 (ago 2026): registrar estilo usado (loop CTR→estilo) ──
        if video_id:
            try:
                from database.db_extended import ExtendedDatabase
                db = ExtendedDatabase()
                db.update_video_thumbnail_style(
                    video_id,
                    str(style.get("visual_style", "") or ""),
                    str(getattr(variant_briefs[0], 'layout', '') or ''),
                )
            except Exception as exc:
                logger.warning("[Thumbnail v2] thumbnail_style persist failed (variants): %s", exc)

        return variant_paths

    # ── F1: Style Engine helpers ──────────────────────────────

    def _dedupe_overlay(self, title: str, overlay: str) -> str:
        """Recorta del overlay las palabras que repiten el INICIO del título.

        Regla de psicología CTR: el texto de la miniatura NUNCA debe repetir
        las primeras 3 palabras del título (redundancia = menos curiosidad).
        Si el overlay empieza por 2+ palabras consecutivas iguales al inicio
        del título, se eliminan esas palabras del overlay.

        Ejemplo: título "La Atlántida: ¿pruebas reales?" + overlay
        "La Atlántida NO existió" → overlay queda "NO existió".
        """
        if not overlay or not title:
            return overlay
        try:
            title_words = [w for w in title.strip().lower().split() if w]
            overlay_words = overlay.strip().split()
            if not overlay_words or len(title_words) < 2:
                return overlay
            # Cuántas palabras del overlay coinciden en orden con el inicio del título
            overlap = 0
            for tw, ow in zip(title_words, [w.lower().strip('¿?¡!.,:;') for w in overlay_words]):
                if tw.strip('¿?¡!.,:;') == ow:
                    overlap += 1
                    if overlap >= 2:
                        break
                else:
                    break
            if overlap >= 2:
                trimmed = " ".join(overlay_words[overlap:]).strip()
                return trimmed or overlay  # nunca devolver vacío
            return overlay
        except Exception:
            return overlay

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

        # Per-channel concept directive (overrides default surprised-face pattern)
        cfg = self._channel_cfg
        allow_faces = getattr(cfg, "THUMBNAIL_ALLOW_FACES", True)
        concept_directive = getattr(cfg, "THUMBNAIL_CONCEPT_DIRECTIVE", "")

        brainstorm = ThumbnailBrainstorm()
        return brainstorm.brainstorm(
            script_text=script_text[:1500] if script_text else "",
            title=title,
            keywords=keywords[:10] if keywords else [],
            style_profile=style,
            channel_name=channel_name,
            channel_theme=channel_theme,
            allow_faces=allow_faces,
            concept_directive=concept_directive,
        )

    # ── F3: Image Generation + Quality Control ────────────────

    # Content-filter trigger words that may cause Pollo AI to reject
    _CONTENT_FILTER_TRIGGERS: list[tuple[str, str]] = [
        # Spanish age-specific terms → adult qualifiers
        ("adolescente", "persona adulta joven"),
        ("niño", "persona adulta"),
        ("niña", "persona adulta"),
        ("niños", "personas adultas"),
        ("niñas", "personas adultas"),
        ("menor de edad", "persona adulta"),
        ("menores", "adultos"),
        ("infantil", "adulto"),
        ("infancia", "edad adulta"),
        # English equivalents
        ("teenager", "young adult person"),
        ("teen", "young adult"),
        ("child", "adult person"),
        ("children", "adults"),
        ("minor", "adult"),
        ("underage", "adult"),
        # Body-part focus terms that may raise flags
        ("solo ojos", "mirada intensa"),
        ("solo boca", "expresión facial"),
        ("solo rostro", "rostro en penumbra"),
        ("parcialmente cubierta", "con expresión intensa"),
    ]

    @staticmethod
    def _prompt_safe_rewrite(prompt: str) -> str:
        """Rewrite a prompt to avoid Pollo AI content-filter rejections.

        Applies cascading transformations:
        1. Replace age-specific terms with adult qualifiers.
        2. Replace body-part-focus phrasing with broader descriptions.
        3. Inject explicit 'adult subject, age 25+' qualifier near the start.
        4. Shift focus toward atmosphere/mood rather than physical description.

        Returns a modified prompt that preserves the visual intent while
        being less likely to trigger safety filters.
        """
        import re

        safe = prompt

        # ── 1. Replace known trigger words ─────────────────────
        for trigger, replacement in ThumbnailMaker._CONTENT_FILTER_TRIGGERS:
            # Case-insensitive replacement
            safe = re.sub(trigger, replacement, safe, flags=re.IGNORECASE)

        # ── 2. Inject adult qualifier early ────────────────────
        # Find the first sentence boundary (., !, ?) and inject after it
        first_boundary = re.search(r'[.!?]\s', safe)
        if first_boundary:
            pos = first_boundary.end()
            safe = safe[:pos] + "Adult subject, age 25 or older. " + safe[pos:]
        else:
            # No sentence boundary found — prepend
            safe = "Adult subject, age 25 or older. " + safe

        # ── 3. Shift toward atmosphere ─────────────────────────
        # If prompt focuses heavily on a person, add atmospheric context
        person_focus_words = ["primer plano", "close-up", "closeup", "rostro", "cara",
                              "face", "expresión", "expression", "retrato", "portrait"]
        has_person_focus = any(w in prompt.lower() for w in person_focus_words)
        if has_person_focus:
            safe += (
                ". Cinematic wide establishing shot, atmospheric lighting, "
                "professional photography, documentary style"
            )

        # ── 4. Remove trailing negative prompts that might confuse ──
        # Keep --no sections but ensure they don't contain filterable terms
        safe = re.sub(r'--no\s+.*', '--no text, watermark, logo, signature, low quality', safe)

        # ── 5. Trim to max prompt length ───────────────────────
        MAX_CHARS = 2000
        if len(safe) > MAX_CHARS:
            safe = safe[:MAX_CHARS - 3].rsplit('.', 1)[0] + '.'

        return safe

    def _generate_with_quality_control(
        self,
        brief: "ThumbnailBrief",
        style: dict,
        slug: str,
    ) -> Path:
        """Generate a thumbnail base image with 3-stage retry logic.

        Pipeline:
        1. Attempt 1: Original Pollo AI prompt (unchanged).
        2. Attempt 2 (retry 1): Content-filter-safe rewrite of the original prompt.
        3. Attempt 3 (retry 2): Generic style-based prompt (no specific subject).
        4. Final fallback: Rich gradient canvas from channel color palette
           (never a black image — always visually presentable).

        Returns a Path to the image (Pollo-generated or gradient fallback).
        """
        from pipeline.thumbnail_style_engine import build_pollo_prompt
        from pipeline.ai_image_generator import AIImageGenerator, PolloAIError

        # ── Build base prompt and alternative prompts ──────────
        original_prompt = build_pollo_prompt(brief.image_concept, style)
        safe_prompt = self._prompt_safe_rewrite(original_prompt)

        # Generic prompt: only style suffix + neutral scene, no specific subject
        style_suffix = style.get("pollo_prompt_suffix", "")
        generic_prompt = (
            "Dramatic atmospheric cinematic scene, mysterious mood, "
            "professional documentary photography. "
            f"{style_suffix}. "
            "high contrast, cinematic lighting, photorealistic, 8K resolution. "
            "--no text, watermark, logo, signature, low quality, nudity"
        )

        ai_gen = None  # lazy init — reuse across attempts

        def _try_generate(label: str, prompt: str, suffix: str) -> Path | None:
            """Run one Pollo AI generation attempt. Returns Path or None."""
            nonlocal ai_gen
            logger.info("[Thumbnail v2] F3 attempt '%s': %r...", label, prompt[:120])

            try:
                if ai_gen is None:
                    ai_gen = AIImageGenerator()
                out_path = self.output_dir / f"qc_{slug}_{suffix}.jpg"
                path = ai_gen.generate(prompt, out_path)
                if path and Path(path).exists():
                    logger.info("[Thumbnail v2] F3 '%s' SUCCESS: %s", label, path)
                    return Path(path)
            except PolloAIError as exc:
                logger.warning("[Thumbnail v2] F3 '%s' PolloAIError: %s", label, exc)
            except Exception as exc:
                logger.error("[Thumbnail v2] F3 '%s' unexpected error: %s", label, exc)

            logger.warning("[Thumbnail v2] F3 '%s' FAILED", label)
            return None

        # ── Attempt 1: Original prompt ─────────────────────────
        result = _try_generate("original", original_prompt, "a1_01")
        if result:
            return result

        # ── Attempt 2: Content-filter-safe rewrite ─────────────
        if safe_prompt != original_prompt:
            logger.info("[Thumbnail v2] F3: Retrying with content-filter-safe prompt")
            result = _try_generate("safe_rewrite", safe_prompt, "a2_safe")
            if result:
                return result
        else:
            logger.info("[Thumbnail v2] F3: Safe rewrite identical to original — skipping retry 1")

        # ── Attempt 3: Generic style-based prompt ──────────────
        logger.info("[Thumbnail v2] F3: Retrying with generic style-based prompt")
        result = _try_generate("generic", generic_prompt, "a3_generic")
        if result:
            return result

        # ── Final fallback: gradient canvas (NEVER black) ──────
        logger.warning("[Thumbnail v2] F3: All Pollo AI attempts failed — using gradient fallback")
        return self._gradient_fallback(style)

    def _gradient_fallback(self, style: dict) -> Path:
        """Create a rich gradient background canvas from the channel's color palette.

        Generates a visually appealing radial/diagonal gradient using the primary
        and accent colors from the style profile. This ensures the final composed
        thumbnail always has a presentable background even when Pollo AI fails.

        Much better than a solid black rectangle — the text, border, and 4K badge
        all compose over a professional-looking dark gradient.
        """
        fallback = self.output_dir / "_thumb_fallback.jpg"
        w, h = self.width, self.height

        palette = style.get("color_palette", {})
        primary_hex = palette.get("primary", "#8B0000")
        accent_hex = palette.get("accent", "#DAA520")
        shadow_hex = palette.get("shadow", "#0A0A0A")
        secondary_hex = palette.get("secondary", "#111111")

        primary = self._parse_color(primary_hex)
        accent = self._parse_color(accent_hex)
        shadow = self._parse_color(shadow_hex)
        secondary = self._parse_color(secondary_hex)

        # Create a rich composite gradient:
        # 1. Radial gradient: bright accent in top-left → dark primary in center → near-black corners
        # 2. Soft noise overlay for film-grain texture
        img = Image.new("RGB", (w, h))
        pixels = img.load()

        center_x, center_y = w * 0.35, h * 0.35  # offset upper-left for visual interest
        max_dist = np.sqrt(w**2 + h**2)

        for y in range(h):
            for x in range(w):
                # Radial distance from focal point (normalized 0..1)
                dx = (x - center_x) / w
                dy = (y - center_y) / h
                dist = np.sqrt(dx**2 + dy**2)
                # Diagonal bias (darker toward bottom-right)
                diagonal = (x / w + y / h) * 0.5

                # Blend: accent at center → primary mid-distance → shadow at edges
                t_accent = max(0, 1 - dist * 2.5)        # fades quickly
                t_primary = max(0, 1 - abs(dist - 0.35) * 3)  # peak at mid-distance
                t_shadow = min(1, dist * 1.3 + diagonal * 0.3)  # stronger at edges/diag
                t_secondary = max(0, 1 - abs(dist - 0.6) * 4)

                r = int(
                    accent[0] * t_accent +
                    primary[0] * t_primary +
                    shadow[0] * t_shadow +
                    secondary[0] * t_secondary
                )
                g = int(
                    accent[1] * t_accent +
                    primary[1] * t_primary +
                    shadow[1] * t_shadow +
                    secondary[1] * t_secondary
                )
                b = int(
                    accent[2] * t_accent +
                    primary[2] * t_primary +
                    shadow[2] * t_shadow +
                    secondary[2] * t_secondary
                )

                # Clamp
                pixels[x, y] = (
                    max(0, min(255, r)),
                    max(0, min(255, g)),
                    max(0, min(255, b)),
                )

        # Apply Gaussian blur to make the gradient smooth (hides banding)
        img = img.filter(ImageFilter.GaussianBlur(radius=20))

        # ── Subtle film grain ──────────────────────────────────
        grain = np.random.randint(-8, 9, (h, w, 3), dtype=np.int16)
        base_arr = np.array(img, dtype=np.int16)
        grain_arr = np.clip(base_arr + grain, 0, 255).astype(np.uint8)
        img = Image.fromarray(grain_arr, "RGB")

        # ── Slight vignette (darkens edges further) ────────────
        vignette = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        vdraw = ImageDraw.Draw(vignette)
        for yy in range(h):
            edge_factor = max(0, (yy / h - 0.3)) * max(0, (1 - yy / h - 0.15))
            edge_factor += max(0, (yy / h - 0.6)) * 0.5
            alpha = int(min(100, edge_factor * 250))
            if alpha > 0:
                vdraw.line([(0, yy), (w, yy)], fill=(0, 0, 0, alpha))
        img = Image.alpha_composite(img.convert("RGBA"), vignette).convert("RGB")

        img.save(fallback, "JPEG", quality=92)
        logger.info("[Thumbnail v2] Gradient fallback saved: %s", fallback)
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
        text_gancho: str = "",
        text_complemento: str = "",
        badge_text: str = "",
        layout: str = "",
        inset_image_path: Path | None = None,
    ) -> Path:
        """Apply channel-style mosaic composition to the base image.

        Layers applied *depend on the layout profile*:
        1. Color grading (contrast + saturation per style, 1.5x boost)
        2. Focus vignette (strength varies by layout)
        3. Dark gradient overlay (opacity and range vary by layout)
        4. Inset recuadros — ONLY when layout profile allows them.
           IMPORTANTE: los recuadros NUNCA pintan texto libre (prompts de
           escena secundaria). Usan una escena real del video o una etiqueta
           fija corta. No reintroducir secondary_scene aquí.
        5. Marketing text overlay (1 or 2 lines depending on layout)
        6. Badge stamp / 4K — ONLY when layout profile allows it
        7. Accent border (weight varies by layout)
        """
        # ── Resolve layout profile ─────────────────────────────
        resolved_layout = layout or brief.layout or style.get("base_composition", "dark_reveal")
        comp = self.LAYOUT_COMPOSITION.get(
            resolved_layout, self.LAYOUT_COMPOSITION["dark_reveal"]
        )
        logger.info(
            "[Thumbnail v2] F4: layout=%s complexity=%s insets=%s badge=%s lines=%d",
            resolved_layout, resolved_layout,
            "yes" if comp["show_insets"] else "no",
            "yes" if comp["show_badge"] else "no",
            comp["text_lines"],
        )

        # ── Palette helpers ─────────────────────────────────────
        color_palette = style.get("color_palette", {})
        accent_rgb = self._hex_to_rgb(color_palette.get("accent", "#CC3333"))
        text_style = style.get("text_style", {})
        use_uppercase = text_style.get("uppercase", True)
        text_color = self._hex_to_rgb(color_palette.get("text", "#F5F0E8"))
        shadow_color = self._hex_to_rgb(color_palette.get("shadow", "#0A0A0A"))

        # ── Load and resize ─────────────────────────────────────
        img = Image.open(base_image).convert("RGB")
        img = self._resize_center_crop(img)

        # ── Color grading per style (1.3x viral boost) ──────────
        effects = style.get("effects", {})
        contrast_boost = float(effects.get("contrast_boost", 1.3)) * 1.3
        saturation = float(effects.get("saturation", 0.85)) * 1.3
        enhancer = ImageEnhance.Contrast(img)
        img = enhancer.enhance(contrast_boost)
        enhancer = ImageEnhance.Color(img)
        img = enhancer.enhance(saturation)
        enhancer = ImageEnhance.Sharpness(img)
        img = enhancer.enhance(2.0)

        # ── Brightness boost (per-style overrideable, gentle global default) ──
        brightness_boost = float(effects.get("brightness_boost", 1.15))
        enhancer = ImageEnhance.Brightness(img)
        img = enhancer.enhance(brightness_boost)

        # ── Focus vignette (strength from layout profile) ───────
        img = self._apply_focus_vignette(
            img, strength=comp["vignette_strength"]
        )

        # ── Dark gradient overlay ───────────────────────────────
        overlay = Image.new("RGBA", (self.width, self.height), (0, 0, 0, 0))
        draw_overlay = ImageDraw.Draw(overlay)
        gradient_opacity = comp["gradient_opacity"]
        gradient_start = comp["gradient_start_pct"]
        start_y = int(self.height * gradient_start)
        for y in range(start_y, self.height):
            alpha = int(gradient_opacity * (y - start_y) / (self.height - start_y))
            draw_overlay.line([(0, y), (self.width, y)], fill=(0, 0, 0, alpha))
        # Subtle top gradient (always applied, halved)
        for y in range(0, int(self.height * 0.08)):
            alpha = int(30 * (1 - y / (self.height * 0.08)))
            draw_overlay.line([(0, y), (self.width, y)], fill=(0, 0, 0, alpha))
        img = Image.alpha_composite(img.convert("RGBA"), overlay).convert("RGB")
        draw = ImageDraw.Draw(img)

        # ── Inset recuadros (conditional on layout) ──────────
        if comp["show_insets"]:
            self._draw_insets(
                img=img,
                draw=draw,
                accent_rgb=accent_rgb,
                title=title,
                badge_text=badge_text,
                inset_count=comp.get("inset_count", 2),
                inset_image_path=inset_image_path,
            )

        # ── Text overlay (1 or 2 lines per layout) ──────────────
        text_gancho_v = text_gancho.strip() if text_gancho else ""
        text_complemento_v = text_complemento.strip() if text_complemento else ""

        # Fallback: parse from legacy overlay_text (format: "L1 | L2")
        if not text_gancho_v and overlay_text.strip():
            parts = overlay_text.strip().split("|")
            if len(parts) >= 2:
                text_gancho_v = parts[0].strip()
                text_complemento_v = parts[1].strip()
            else:
                text_gancho_v = overlay_text.strip()

        # If still nothing, derive from brief.text_overlay
        if not text_gancho_v and brief.text_overlay:
            parts = brief.text_overlay.split("|")
            if len(parts) >= 2:
                text_gancho_v = parts[0].strip()
                text_complemento_v = parts[1].strip()
            else:
                text_gancho_v = brief.text_overlay[:12]

        if use_uppercase:
            text_gancho_v = text_gancho_v.upper()
            text_complemento_v = text_complemento_v.upper()

        # Truncate
        text_gancho_v = text_gancho_v[:14]
        text_complemento_v = text_complemento_v[:28]

        stroke_w = max(3, self.text_stroke_width + 2)

        # ── L1 (gancho) — larger for 1-line layouts ─────────
        # P6 (ago 2026): texto más grande para legibilidad en móvil (miniaturas
        # pequeñas). Regla psicología: 25-35% del alto; 17-19% es el máximo
        # legible sin recortes con _fit_text_to_box. Antes 13-17%.
        if text_gancho_v:
            # Bigger font when there is only one text line
            if comp["text_lines"] == 1:
                gancho_font_size = max(80, int(self.height * 0.19))
                l1_y_pct = 0.38   # more centred
            else:
                gancho_font_size = max(72, int(self.height * 0.16))
                l1_y_pct = 0.52  # lower third
            gancho_font = _find_font(gancho_font_size, bold=True, font_name=self.font_family)
            if gancho_font:
                bbox_l1 = draw.textbbox((0, 0), text_gancho_v, font=gancho_font)
                l1_w = bbox_l1[2] - bbox_l1[0]
                l1_h = bbox_l1[3] - bbox_l1[1]
                l1_x = (self.width - l1_w) // 2
                l1_y = int(self.height * l1_y_pct)

                # Shadow in 8 directions
                for sx, sy in [(-stroke_w, 0), (stroke_w, 0), (0, -stroke_w), (0, stroke_w),
                               (-stroke_w, -stroke_w), (stroke_w, stroke_w), (-stroke_w, stroke_w), (stroke_w, -stroke_w)]:
                    draw.text((l1_x + sx, l1_y + sy), text_gancho_v, fill=shadow_color, font=gancho_font)
                draw.text((l1_x, l1_y), text_gancho_v, fill=text_color, font=gancho_font,
                          stroke_width=self.text_stroke_width, stroke_fill=self.text_stroke_color)

        # ── L2 (complemento) — only when 2-line layout ─────
        if comp["text_lines"] >= 2 and text_complemento_v:
            comp_font_size = max(44, int(self.height * 0.09))
            comp_font = _find_font(comp_font_size, bold=True, font_name=self.font_family)
            if comp_font and 'l1_y' in locals():
                bbox_l2 = draw.textbbox((0, 0), text_complemento_v, font=comp_font)
                l2_w = bbox_l2[2] - bbox_l2[0]
                l2_x = (self.width - l2_w) // 2
                l2_y = l1_y + l1_h + 12

                for sx, sy in [(-stroke_w, 0), (stroke_w, 0), (0, -stroke_w), (0, stroke_w),
                               (-stroke_w, -stroke_w), (stroke_w, stroke_w)]:
                    draw.text((l2_x + sx, l2_y + sy), text_complemento_v, fill=shadow_color, font=comp_font)
                draw.text((l2_x, l2_y), text_complemento_v, fill=text_color, font=comp_font,
                          stroke_width=self.text_stroke_width, stroke_fill=self.text_stroke_color)

        # ── Badge stamp (top-right, conditional on layout) ───────
        badge_text_v = badge_text if badge_text else ""
        if comp["show_badge"] and badge_text_v:
            self._draw_badge_stamp(draw, badge_text_v, accent_rgb)
        elif comp["show_badge"] and self.show_4k_badge:
            self._draw_4k_badge(draw)

        # ── Border (width varies by layout) ─────────────────
        if comp["show_border"]:
            border_w = max(2, int((self.border_width + 2) * comp["border_width_factor"]))
            for i in range(border_w):
                draw.rectangle(
                    [i, i, self.width - 1 - i, self.height - 1 - i],
                    outline=self.border_color,
                )

        # ── Classified stamp ────────────────────────────────
        if resolved_layout == "classified_document":
            self._add_classified_overlay(draw, {
                "accent": accent_rgb,
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

        # ── Medical-themed overlays (clinical_mystery) ──────────
        if self.medical_ecg:
            self._draw_ecg_waveform(draw)
        if self.medical_cross:
            self._draw_medical_cross_stamp(draw)
        if self.medical_diagnosis:
            self._draw_diagnosis_badge(draw)

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

    # ── Inset recuadros helper ──────────────────────────────

    @staticmethod
    def _pick_inset_image(
        scene_images: list | None,
        base_image: Path | None = None,
    ) -> Path | None:
        """Pick one existing video scene image for the inset recuadro.

        Accepts the two shapes produced by callers:
        - nested: ``[[path_a], [path_b], ...]`` (orchestrator / generation_service)
        - flat:   ``[path_a, path_b, ...]``

        The base (main) image is excluded so the inset never repeats the
        primary visual. Returns ``None`` when no usable image is found —
        the caller then falls back to a short text label.

        Args:
            scene_images: Scene image paths from the video media assets.
            base_image: Path of the main thumbnail image to exclude.

        Returns:
            A Path to a usable scene image, or None.
        """
        import random

        candidates: list[Path] = []
        if scene_images:
            for item in scene_images:
                if isinstance(item, (list, tuple)):
                    candidates.extend(Path(p) for p in item if p)
                else:
                    candidates.append(Path(item))

        base_resolved = Path(base_image).resolve() if base_image else None
        usable = [
            p for p in candidates
            if p.exists()
            and p.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}
            and (base_resolved is None or p.resolve() != base_resolved)
        ]
        return random.choice(usable) if usable else None

    def _draw_insets(
        self,
        img: Image.Image,
        draw: ImageDraw.ImageDraw,
        accent_rgb: tuple,
        title: str,
        badge_text: str = "",
        inset_count: int = 2,
        inset_image_path: Path | None = None,
    ) -> None:
        """Draw 1–2 inset boxes at the bottom of the thumbnail.

        Each box uses ``_fit_text_to_box`` to ensure its label
        never overflows.  If a label cannot fit at the minimum
        font size, that inset is silently skipped.

        When *inset_image_path* is provided, inset A will embed
        a thumbnail crop of that image instead of a text-only box.
        """
        # ── Shared dimensions ─────────────────────────────────
        inset_w = int(self.width * 0.22)
        inset_h = int(self.height * 0.20)
        inset_x = 15
        inset_y = self.height - inset_h - 15

        # ── Inset A : bottom-left ─────────────────────────────
        # Short label only — never paint a raw image prompt here.
        # The real visual content comes from inset_image_path when available.
        label_a = "DOCUMENTO REAL" if "documento" in title.lower() else "EVIDENCIA"

        # Determine whether to embed an image or draw text
        use_image_inset = bool(inset_image_path and inset_image_path.exists())

        if use_image_inset:
            # Embed a cropped thumbnail version of the secondary image
            try:
                secondary_img = Image.open(inset_image_path).convert("RGB")
                secondary_img = self._resize_center_crop(secondary_img)
                # Crop to inset dimensions
                inset_crop = secondary_img.resize(
                    (inset_w, inset_h), Image.LANCZOS
                )
                # Build a rounded-rectangle mask
                mask = Image.new("L", (inset_w, inset_h), 0)
                mask_draw = ImageDraw.Draw(mask)
                mask_draw.rounded_rectangle(
                    [0, 0, inset_w, inset_h], radius=10, fill=255
                )
                # Paste the cropped image onto the main RGB canvas
                # using the mask for smooth rounded corners
                img.paste(inset_crop, (inset_x, inset_y), mask)
                # Draw accent border *after* paste so it sits on top
                draw.rounded_rectangle(
                    [
                        inset_x - 1,
                        inset_y - 1,
                        inset_x + inset_w + 1,
                        inset_y + inset_h + 1,
                    ],
                    radius=10,
                    outline=accent_rgb,
                    width=3,
                )
            except Exception as exc:
                logger.warning("Inset image embedding failed: %s — falling back to text", exc)
                use_image_inset = False

        if not use_image_inset:
            # Text-only box with fitted label
            fitted = self._fit_text_to_box(
                draw, label_a, inset_w - 8, inset_h - 8, bold=True, min_font_size=11
            )
            if fitted:
                inset_overlay_a = Image.new(
                    "RGBA", (inset_w, inset_h), (15, 15, 20, 180)
                )
                inset_draw_a = ImageDraw.Draw(inset_overlay_a)
                inset_draw_a.rounded_rectangle(
                    [2, 2, inset_w - 2, inset_h - 2],
                    radius=8,
                    outline=accent_rgb + (200,),
                    width=2,
                )
                # Centre all lines vertically using actual line bounding boxes
                lines = fitted["lines"]
                line_bboxes = [inset_draw_a.textbbox((0, 0), l, font=fitted["font"]) for l in lines]
                line_heights = [b[3] - b[1] for b in line_bboxes]   # actual pixel height
                line_tops = [b[1] for b in line_bboxes]             # anchor → top of text
                line_bottoms = [b[3] for b in line_bboxes]          # anchor → bottom of text
                gap = max(2, int(fitted["font_size"] * 0.18))
                total_block_h = sum(line_heights) + gap * (len(lines) - 1)
                current_y = (inset_h - total_block_h) // 2 - line_tops[0]
                for i, line in enumerate(lines):
                    w_line = line_bboxes[i][2] - line_bboxes[i][0]
                    tx_a = (inset_w - w_line) // 2
                    inset_draw_a.text(
                        (tx_a, current_y),
                        line,
                        fill=(220, 220, 220, 255),
                        font=fitted["font"],
                    )
                    if i < len(lines) - 1:
                        current_y += line_bottoms[i] + gap - line_tops[i + 1]

                img.paste(inset_overlay_a, (inset_x, inset_y), inset_overlay_a)
                draw = ImageDraw.Draw(img)

        # ── Inset B : bottom-right (only when inset_count >= 2) ─
        if inset_count < 2:
            return

        inset_w2 = int(self.width * 0.16)
        inset_h2 = int(self.height * 0.18)
        inset_x2 = self.width - inset_w2 - 15
        inset_y2 = self.height - inset_h2 - 15

        label_b = badge_text if badge_text else "DOCUMENTAL"
        fitted_b = self._fit_text_to_box(
            draw, label_b, inset_w2 - 8, inset_h2 - 8, bold=True, min_font_size=10
        )
        if fitted_b:
            inset_overlay_b = Image.new(
                "RGBA", (inset_w2, inset_h2), (15, 15, 25, 150)
            )
            inset_draw_b = ImageDraw.Draw(inset_overlay_b)
            inset_draw_b.rounded_rectangle(
                [2, 2, inset_w2 - 2, inset_h2 - 2],
                radius=8,
                outline=accent_rgb + (170,),
                width=2,
            )
            lines_b = fitted_b["lines"]
            line_bboxes_b = [inset_draw_b.textbbox((0, 0), l, font=fitted_b["font"]) for l in lines_b]
            line_heights_b = [b[3] - b[1] for b in line_bboxes_b]
            line_tops_b = [b[1] for b in line_bboxes_b]
            line_bottoms_b = [b[3] for b in line_bboxes_b]
            gap_b = max(2, int(fitted_b["font_size"] * 0.18))
            total_block_h_b = sum(line_heights_b) + gap_b * (len(lines_b) - 1)
            current_b = (inset_h2 - total_block_h_b) // 2 - line_tops_b[0]
            for i, line in enumerate(lines_b):
                w_line_b = line_bboxes_b[i][2] - line_bboxes_b[i][0]
                tx_b = (inset_w2 - w_line_b) // 2
                inset_draw_b.text(
                    (tx_b, current_b),
                    line,
                    fill=(200, 200, 200, 255),
                    font=fitted_b["font"],
                )
                if i < len(lines_b) - 1:
                    current_b += line_bottoms_b[i] + gap_b - line_tops_b[i + 1]

            img.paste(inset_overlay_b, (inset_x2, inset_y2), inset_overlay_b)
            draw = ImageDraw.Draw(img)

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

    def _draw_badge_stamp(self, draw: ImageDraw.ImageDraw, text: str, accent_rgb: tuple) -> None:
        """Draw a confidence badge/stamp in the top-right corner (replaces/extends 4K)."""
        badge_font = _find_font(26, bold=True, font_name=self.font_family)
        if not badge_font:
            return
        
        # Measure text
        bbox = draw.textbbox((0, 0), text.upper(), font=badge_font)
        tw = bbox[2] - bbox[0] + 30
        th = bbox[3] - bbox[1] + 16
        x = self.width - tw - 15
        y = 15
        
        # Semi-transparent dark background
        draw.rounded_rectangle(
            [x, y, x + tw, y + th],
            radius=10,
            fill=(10, 10, 12),
            outline=accent_rgb,
            width=3,
        )
        # Text
        tx = x + (tw - (bbox[2] - bbox[0])) // 2
        ty = y + (th - (bbox[3] - bbox[1])) // 2
        draw.text((tx, ty), text.upper(), font=badge_font, fill=(255, 255, 240))

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

    # ── Medical-themed overlays (clinical_mystery style) ────────

    def _draw_ecg_waveform(self, draw: ImageDraw.ImageDraw) -> None:
        """Draw an ECG heartbeat waveform across the bottom of the thumbnail.

        A glowing cyan medical monitor line with a characteristic
        PQRST complex (normal sinus rhythm) that pulses across
        the bottom edge, giving a clinical monitoring aesthetic.
        """
        medical_cyan = (0, 180, 216)      # bright medical cyan
        dark_cyan = (0, 60, 80)
        glow_cyan = (0, 220, 255)          # glow highlight

        # ── ECG baseline strip at bottom ──────────────────────
        ecg_y_center = self.height - 55
        strip_h = 40
        draw.rectangle(
            [0, ecg_y_center - strip_h // 2, self.width, ecg_y_center + strip_h // 2],
            fill=(6, 12, 24),               # deep navy background
        )
        # Subtle top edge glow
        draw.line(
            [(0, ecg_y_center - strip_h // 2), (self.width, ecg_y_center - strip_h // 2)],
            fill=(*medical_cyan, 80), width=1,
        )

        # ── ECG waveform (simplified PQRST complex x3) ────────
        # Each complex spans ~140px, with flat line between
        complex_width = 140
        gap_width = 180
        start_x = 60
        amp = 18  # amplitude (pixels from baseline)

        for rep in range(3):
            cx = start_x + rep * (complex_width + gap_width)
            if cx > self.width - 40:
                break

            # P wave (small bump)
            draw.arc(
                [cx, ecg_y_center - amp // 2, cx + 20, ecg_y_center + amp // 2],
                180, 360, fill=medical_cyan, width=2,
            )
            # Flat return to baseline
            draw.line(
                [(cx + 20, ecg_y_center), (cx + 35, ecg_y_center)],
                fill=medical_cyan, width=2,
            )
            # Q dip (small downward spike)
            draw.line(
                [(cx + 35, ecg_y_center), (cx + 42, ecg_y_center + 8),
                 (cx + 48, ecg_y_center)],
                fill=medical_cyan, width=2,
            )
            # R spike (tall upward spike — main beat)
            draw.line(
                [(cx + 48, ecg_y_center), (cx + 54, ecg_y_center - amp),
                 (cx + 60, ecg_y_center)],
                fill=glow_cyan, width=3,
            )
            # S dip (downward)
            draw.line(
                [(cx + 60, ecg_y_center), (cx + 66, ecg_y_center + 10),
                 (cx + 72, ecg_y_center)],
                fill=medical_cyan, width=2,
            )
            # T wave (broad bump)
            draw.arc(
                [cx + 72, ecg_y_center - 10, cx + 100, ecg_y_center + 10],
                180, 360, fill=medical_cyan, width=2,
            )
            # Return to baseline
            draw.line(
                [(cx + 100, ecg_y_center), (cx + complex_width, ecg_y_center)],
                fill=medical_cyan, width=2,
            )

        # ── Glowing dot (heartbeat indicator) at top-left of strip
        dot_x, dot_y = 18, ecg_y_center - strip_h // 2 + 12
        for r in range(3, 0, -1):
            alpha = 100 - r * 25
            draw.ellipse(
                [dot_x - r * 2, dot_y - r * 2, dot_x + r * 2, dot_y + r * 2],
                fill=(*glow_cyan, alpha),
            )
        draw.ellipse([dot_x - 3, dot_y - 3, dot_x + 3, dot_y + 3], fill=glow_cyan)

        # ── Small "HR" label
        hr_font = _find_font(11, bold=True)
        draw.text((32, ecg_y_center - strip_h // 2 + 3), "HR: 72 BPM",
                  font=hr_font, fill=(0, 180, 216))

    def _draw_medical_cross_stamp(self, draw: ImageDraw.ImageDraw) -> None:
        """Draw a glowing medical cross (+) stamp in the top-left corner.

        A semi-transparent red pill with a bold white '+' symbol,
        surrounded by a subtle cyan glow to create a medical
        urgency aesthetic. Positioned opposite the 4K badge.
        """
        medical_red = (230, 57, 70)       # emergency red
        glow_cyan = (0, 220, 255)
        padding = 15

        # ── Cross stamp pill ──────────────────────────────────
        cross_size = 54
        cross_x = padding
        cross_y = padding

        # Glow halo behind
        for r in range(4, 0, -1):
            draw.rounded_rectangle(
                [cross_x - r, cross_y - r, cross_x + cross_size + r, cross_y + cross_size + r],
                radius=14,
                fill=(*glow_cyan, 25 - r * 5),
            )

        # Red pill background
        draw.rounded_rectangle(
            [cross_x, cross_y, cross_x + cross_size, cross_y + cross_size],
            radius=12,
            fill=medical_red,
            outline=(*glow_cyan, 180),
            width=2,
        )

        # White '+' cross
        cross_font = _find_font(36, bold=True)
        cross_label = "+"
        bbox = draw.textbbox((0, 0), cross_label, font=cross_font)
        tw = bbox[2] - bbox[0]
        th = bbox[3] - bbox[1]
        tx = cross_x + (cross_size - tw) // 2
        ty = cross_y + (cross_size - th) // 2 - 2
        # Shadow
        draw.text((tx + 1, ty + 1), cross_label, font=cross_font,
                  fill=(0, 0, 0, 80))
        # Main text
        draw.text((tx, ty), cross_label, font=cross_font,
                  fill=(255, 255, 255))

    def _draw_diagnosis_badge(self, draw: ImageDraw.ImageDraw) -> None:
        """Draw a 'DIAGNÓSTICO' badge pill in the top-right corner.

        Positioned below the 4K badge (if present) in the same column.
        Uses an emergency red pill with cyan border for a
        'medical alert / case file' aesthetic.
        """
        medical_red = (200, 40, 45)
        medical_cyan = (0, 200, 230)

        # Position: below 4K badge if present
        badge_offset = 80 if self.show_4k_badge else 0
        badge_x = self.width - 155
        badge_y = 15 + badge_offset
        badge_w = 140
        badge_h = 36

        # Glow halo
        for r in range(3, 0, -1):
            draw.rounded_rectangle(
                [badge_x - r, badge_y - r, badge_x + badge_w + r, badge_y + badge_h + r],
                radius=10,
                fill=(*medical_cyan, 20 - r * 6),
            )

        # Red pill background
        draw.rounded_rectangle(
            [badge_x, badge_y, badge_x + badge_w, badge_y + badge_h],
            radius=8,
            fill=medical_red,
            outline=medical_cyan,
            width=2,
        )

        # "DIAGNÓSTICO" text
        badge_font = _find_font(16, bold=True)
        label = "DIAGNÓSTICO"
        bbox = draw.textbbox((0, 0), label, font=badge_font)
        tw = bbox[2] - bbox[0]
        th = bbox[3] - bbox[1]
        tx = badge_x + (badge_w - tw) // 2
        ty = badge_y + (badge_h - th) // 2 - 1
        draw.text((tx, ty), label, font=badge_font, fill=(255, 255, 255))

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

    def _measure_text_size(self, draw, text: str, font) -> tuple:
        """Return (width, height) of text with given font."""
        bbox = draw.textbbox((0, 0), text, font=font)
        return bbox[2] - bbox[0], bbox[3] - bbox[1]

    def _fit_text_to_box(
        self,
        draw: ImageDraw.ImageDraw,
        text: str,
        box_width: int,
        box_height: int,
        bold: bool = True,
        min_font_size: int = 10,
    ) -> dict | None:
        """Fit text inside a fixed-size box by wrapping and scaling font.

        Strategy:
        1. Start with font at 40 % of box height.
        2. Measure text width → if it overflows box, try word-wrapping
           into 2 lines and re-measure.
        3. If 2-line wrap still overflows, iteratively shrink font by
           2 px down to *min_font_size*.
        4. If even the minimum font overflows, truncate with "…".
        5. Returns None when text is empty or cannot be rendered at
           min_font_size + 1-char.

        Returns:
            dict with keys ``lines``, ``font``, ``font_size``,
            ``total_h``, ``line_spacing`` — or None.
        """
        if not text or not text.strip():
            return None

        text = text.strip()

        def _wrap_two(text_str: str, max_w: int, font) -> list[str]:
            """Pure word-wrap into at most 2 lines."""
            words = text_str.split()
            if not words:
                return [text_str]
            line1, line2 = "", ""
            best = [text_str]  # fallback: single line
            # Try split point
            for i in range(1, len(words) + 1):
                l1 = " ".join(words[:i]).strip()
                l2 = " ".join(words[i:]).strip()
                w1, _ = self._measure_text_size(draw, l1, font)
                w2, _ = self._measure_text_size(draw, l2, font) if l2 else (0, 0)
                if w1 <= max_w and w2 <= max_w:
                    best = [l1, l2] if l2 else [l1]
                    break
            return best

        # Start with a generous font size
        font_size = max(min_font_size + 2, int(box_height * 0.38))
        font = _find_font(font_size, bold=bold, font_name=self.font_family)
        if not font:
            return None

        max_w = box_width - 12  # internal padding

        for _ in range(20):  # safety cap
            w, _ = self._measure_text_size(draw, text, font)
            if w <= max_w:
                # Single line fits
                h, _ = self._measure_text_size(draw, text, font)
                if getattr(font, 'size', font_size) is not None:
                    _, asc, _, _ = draw.textbbox((0, 0), "Ag", font=font)
                    line_h = asc
                else:
                    line_h = font_size
                return {
                    "lines": [text],
                    "font": font,
                    "font_size": font_size,
                    "total_h": line_h,
                    "line_spacing": 2,
                }

            # Try 2-line wrap
            wrapped = _wrap_two(text, max_w, font)
            if len(wrapped) == 2:
                w1, _ = self._measure_text_size(draw, wrapped[0], font)
                w2, _ = self._measure_text_size(draw, wrapped[1], font)
            else:
                w1, w2 = w, 0

            if w1 <= max_w and w2 <= max_w:
                # Both lines fit
                _, asc, _, _ = draw.textbbox((0, 0), "Ag", font=font)
                line_h = int(asc * 1.15)
                total_h = line_h * len(wrapped)
                if total_h <= box_height - 6:
                    return {
                        "lines": wrapped,
                        "font": font,
                        "font_size": font_size,
                        "total_h": total_h,
                        "line_spacing": int(asc * 0.15),
                    }

            # Shrink and retry
            if font_size <= min_font_size:
                break
            font_size -= 2
            font = _find_font(font_size, bold=bold, font_name=self.font_family)
            if not font:
                return None

        # Last resort: truncate with ellipsis
        font = _find_font(min_font_size, bold=bold, font_name=self.font_family)
        if not font:
            return None
        truncated = text[:8] + "…" if len(text) > 8 else text
        w_trunc, _ = self._measure_text_size(draw, truncated, font)
        if w_trunc <= max_w:
            _, asc, _, _ = draw.textbbox((0, 0), "Ag", font=font)
            return {
                "lines": [truncated],
                "font": font,
                "font_size": min_font_size,
                "total_h": asc,
                "line_spacing": 0,
            }

        return None

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
