"""Visual Coherence Engine — unified visual style across all scenes.

Produces a shared *style prompt prefix* injected into every AI-generated
image so all scenes share a consistent color palette, atmosphere, and mood.

Phase 1 (this module): derives style from channel config only.
Phase 3 (future): also incorporates the visual bible (protagonist, recurring
elements, scene visual concepts) for maximum coherence.

Architecture
------------
Each AI prompt is assembled from 4 layers::

    [STYLE PREFIX] + [VISUAL CONTEXT] + [SCENE CONCEPT] + [TECH SUFFIX]

This module handles layer 1 (style prefix) and provides helpers for
layer 4 (tech suffix, density control).
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

# ── Colour-treatment arc (position 0..1 → prompt modifier) ────────
# Intención: imágenes vívidas, de alto contraste y luminosas a lo largo de
# TODO el vídeo (retener al espectador). Nada de "deep shadows" ni tonos
# apagados: sombras ricas pero legibles, highlights luminosos, color saturado.
_COLOR_ARC = [
    (0.00, 0.15, "warm golden-hour light, rich amber tones, vivid readable detail, luminous glow"),
    (0.15, 0.40, "bright natural daylight, lively saturated colour, vivid readable subject separation"),
    (0.40, 0.70, "bold vibrant colour, high contrast with luminous highlights, vivid readable midtones"),
    (0.70, 0.85, "dynamic high-contrast cinematic grade, rich saturated colour, brilliant readable detail"),
    (0.85, 1.00, "warm radiant light, luminous glowing colour, vivid readable resolution"),
]

# ── Global negative prompt (shared across ALL generations) ────────
# Personas: se prohíben los primeros planos del rostro — en escenas, la gente
# debe aparecer a distancia (plano medio/general), nunca rellenando el encuadre
# como sujeto en primer plano. Las manos deformadas son el error más común de
# los modelos generativos al intentar acercar a una persona.
# Añadidos: términos que producen "filtro oscurecido" y fotos planas/borrosas.
# Ojo: solo Local SD consume el negative_prompt; Pollinations lo ignora, así
# que el lado POSITIVO del prompt (tech suffix + impact style) es el que
# realmente garantiza viveza/contraste en todos los proveedores.
NEGATIVE_PROMPT = (
    "text, letters, watermark, logo, signature, "
    "blurry, low quality, jpeg artifacts, "
    "deformed, ugly, extra limbs, bad anatomy, disfigured, duplicate, mutated, "
    "deformed hands, extra fingers, fused fingers, bad hands, "
    "close-up face, extreme close-up portrait, face filling frame, "
    "foreground face, recognizable face, "
    "frame, border, collage, split screen, multiple views, photomontage, "
    "deep fried, cartoon, 3d render, plastic, doll-like, "
    "dark, underexposed, muddy shadows, flat, dull, muted colours, washed out, "
    "grey haze, low contrast, soft focus, hazy, grainy, noisy, "
    "nudity, nsfw, gore, violence"
)


class VisualCoherenceEngine:
    """Derives a consistent visual style for an entire video.

    Parameters
    ----------
    config:
        Channel config object (must expose ``IMAGE_STYLE_MODIFIERS``,
        ``COLOR_PALETTE``, and optionally ``CANAL_NARRATIVE_STYLE``).
    visual_bible:
        Optional dict from ``VisualBibleGenerator`` (Phase 3). Not used
        in Phase 1 — the engine works with channel config alone.
    """

    def __init__(
        self,
        config: Any,
        visual_bible: dict | None = None,
    ) -> None:
        self._config = config
        self._visual_bible = visual_bible

        # Cache the base prefix so we only compute it once per video.
        self._base_prefix: str | None = None

    # ── Public API ──────────────────────────────────────────────

    @property
    def base_style_prefix(self) -> str:
        """Return the unchanging style prefix applied to all scenes."""
        if self._base_prefix is None:
            self._base_prefix = self._build_base_prefix()
        return self._base_prefix

    @property
    def impact_style_prefix(self) -> str:
        """Return the non-negotiable global impact and channel-grade layers."""
        return self._build_impact_style_prefix()

    def get_scene_style(self, scene_idx: int, total_scenes: int) -> str:
        """Return the full style prefix for a specific scene index.

        The base prefix is augmented with a colour-treatment modifier
        that shifts along a pre-defined arc while preserving vivid,
        readable colour throughout the video.

        If *total_scenes* is 0 the base prefix is returned unmodified.
        """
        if total_scenes <= 0:
            return self.base_style_prefix

        pos = scene_idx / total_scenes
        arc_modifier = self._arc_modifier(pos)
        if arc_modifier:
            return f"{self.base_style_prefix}, {arc_modifier}"
        return self.base_style_prefix

    @staticmethod
    def build_negative_prompt() -> str:
        """Global negative prompt shared by all AI generations."""
        return NEGATIVE_PROMPT

    def build_positive_exceptions(self) -> str:
        exceptions = getattr(self._config, "AI_IMAGE_PROMPT_EXCEPTIONS", {}) or {}
        values = exceptions.get("positive", []) if isinstance(exceptions, dict) else []
        return ", ".join(str(value).strip() for value in values if str(value).strip())

    def build_negative_prompt_for_config(self, config: Any = None) -> str:
        cfg = config or self._config
        exceptions = getattr(cfg, "AI_IMAGE_PROMPT_EXCEPTIONS", {}) or {}
        values = exceptions.get("negative", []) if isinstance(exceptions, dict) else []
        extra = ", ".join(str(value).strip() for value in values if str(value).strip())
        return f"{NEGATIVE_PROMPT}, {extra}" if extra else NEGATIVE_PROMPT

    @staticmethod
    def get_visual_density(palabras_por_segundo: float) -> str:
        """Map narration density to visual complexity.

        Heavy narration → simple images (shallow DoF, minimal elements).
        Sparse narration → rich images (detailed environments, deep DoF).

        Returns one of ``"simple"``, ``"balanced"``, or ``"rich"``.
        """
        if palabras_por_segundo <= 0:
            return "balanced"
        if palabras_por_segundo > 3.0:
            return "simple"      # too much to process — keep visuals clean
        if palabras_por_segundo > 2.0:
            return "balanced"
        return "rich"            # slow narration — room for visual detail

    @staticmethod
    def build_tech_suffix(density: str = "balanced") -> str:
        """Build the technical suffix (layer 4 of the AI prompt).

        Parameters
        ----------
        density:
            ``"simple"`` → DOF moderado, pocos elementos (sin empujar al sujeto
            al primer plano — evita rostros cercanos).
            ``"balanced"`` → natural depth.
            ``"rich"`` → deep DoF, detailed textures.
        """
        base = (
            "16:9 aspect ratio, real photograph, photorealistic documentary photo, "
            "bright clear sharp image, controlled exposure, no text, no watermark, crisp in-focus subject, "
            "8K, high resolution, intricate detail, ultra sharp, "
            "high contrast, vibrant saturated colour, dramatic cinematic lighting"
        )

        extras: dict[str, str] = {
            "simple": (
                "creamy bokeh background, moderate depth of field, "
                "subject at comfortable distance, "
                "minimal elements, clean composition"
            ),
            "balanced": (
                "natural depth of field, balanced composition, "
                "cinematic lighting, subtle background bokeh"
            ),
            "rich": (
                "deep depth of field, detailed environment, "
                "rich textures, dynamic lighting, bokeh highlights"
            ),
        }
        extra = extras.get(density, extras["balanced"])
        return f"{base}, {extra}"

    # ── Internal ────────────────────────────────────────────────

    def _build_base_prefix(self) -> str:
        """Build the fixed style prefix from channel config.

        Combines:
          - ``AI_VISUAL_IMPACT_STYLE`` (global, inheritable AI treatment)
          - ``AI_VISUAL_COLOR_GRADING`` (channel colour-grade override)
          - ``IMAGE_STYLE_MODIFIERS`` (free-text style modifiers)
          - ``COLOR_PALETTE`` → human-readable colour hints
          - ``CANAL_NARRATIVE_STYLE`` (optional narrative flavour)
        """
        cfg = self._config

        # Global impact treatment is inheritable from defaults and establishes
        # the common hybrid documentary / YouTube-impact look for all AI
        # providers. A channel may add a colour-grade without replacing it.
        impact_style = self.impact_style_prefix

        # Core style from channel
        style_mod: str = self._config_string(
            cfg,
            "IMAGE_STYLE_MODIFIERS",
            "cinematic documentary photography, 16:9, atmospheric",
        )
        # Normalize — ensure it's a clean comma-separated string.
        if isinstance(style_mod, tuple):
            style_mod = ", ".join(s for s in style_mod if s)

        style_parts = [impact_style, style_mod]
        style_mod = ", ".join(part for part in style_parts if part)

        # Colour palette hints (human-readable terms)
        palette = getattr(cfg, "COLOR_PALETTE", {})
        if not isinstance(palette, dict):
            palette = {}
        colour_hint = self._palette_to_hint(palette)
        if colour_hint:
            style_mod = f"{style_mod}, {colour_hint}"

        # Narrative style adds flavour
        narrative = self._config_string(cfg, "CANAL_NARRATIVE_STYLE")
        if narrative and narrative.lower() not in style_mod.lower():
            style_mod = f"{narrative} style, {style_mod}"

        return style_mod

    def _build_impact_style_prefix(self) -> str:
        """Build the global impact and per-channel colour-grade layers.

        The universal framing rules live HERE (not in the base prefix): this is
        the layer that the truncation in ``MediaFetcher._build_ai_prompt``
        PRESERVES (``protected_style``). The base prefix is dropped when a prompt
        is truncated, so any rule placed only there would vanish in production
        (where prompts always exceed the budget).
        """
        return ", ".join(
            part for part in (
                self._config_string(self._config, "AI_VISUAL_IMPACT_STYLE"),
                self._config_string(self._config, "AI_VISUAL_COLOR_GRADING"),
                # Universal framing rules (all channels, all AI providers):
                # composición off-center (regla de tercios) y personas SIEMPRE
                # a distancia. Refuerzo con NEGATIVE_PROMPT para los casos en
                # que el modelo ignore el texto positivo.
                "rule of thirds composition, off-center subject, "
                "if people appear, show them at a distance: medium or wide shot, "
                "never close-up or foreground",
            ) if part
        )

    @staticmethod
    def _config_string(config: Any, name: str, default: str = "") -> str:
        """Return a textual config value without leaking mock/non-text values."""
        value = getattr(config, name, default)
        if isinstance(value, str):
            return value
        if isinstance(value, tuple) and all(isinstance(item, str) for item in value):
            return ", ".join(item for item in value if item)
        return default

    @staticmethod
    def _palette_to_hint(palette: dict) -> str:
        """Convert an RGB palette dict into a human-readable colour hint.

        Example: primary=(212,175,55) → "warm golden tones".
        """
        primary = palette.get("primary")
        if not primary or len(primary) < 3:
            return ""

        r, g, b = float(primary[0]), float(primary[1]), float(primary[2])

        # --- temperature ---
        if r > g + 50 and r > b + 30:
            temp = "warm"
        elif b > r + 40 and b > g + 20:
            temp = "cool blue"
        else:
            temp = "neutral"

        # --- brightness (siempre positivo — nunca "dark moody"/"muted":
        #     esos hints empujaban al modelo hacia filtros oscurecidos) ---
        avg = (r + g + b) / 3.0
        if avg < 70:
            lum = "deep rich"
        elif avg < 140:
            lum = "rich saturated"
        elif avg < 200:
            lum = "bright vivid"
        else:
            lum = "luminous high-key"

        # --- dominant hue ---
        if g > r and g > b and g > 120:
            dom = "green-tinted"
        elif r > g and r > b and r > 160:
            dom = "warm amber"
        elif b > r and b > g and b > 100:
            dom = "blue-leaning"
        else:
            dom = "earthy"

        return f"{temp} {lum} {dom} palette"

    @staticmethod
    def _arc_modifier(pos: float) -> str:
        """Return the colour-temperature modifier for position *pos* (0..1)."""
        for lo, hi, text in _COLOR_ARC:
            if lo <= pos < hi:
                return text
        # Edge: pos == 1.0 (exactly the end) — use the last bucket.
        return _COLOR_ARC[-1][2]
