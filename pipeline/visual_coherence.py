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

# ── Colour-temperature arc (position 0..1 → prompt modifier) ──────
_COLOR_ARC = [
    (0.00, 0.15, "warm golden hour light, rich amber tones, inviting glow"),
    (0.15, 0.40, "neutral warm daylight, natural soft light, balanced tones"),
    (0.40, 0.70, "cool muted shadows, overcast atmosphere, desaturated midtones"),
    (0.70, 0.85, "desaturated cold light, dramatic contrast, stark shadows"),
    (0.85, 1.00, "warm hopeful light, soft golden glow, resolution and peace"),
]

# ── Global negative prompt (shared across ALL generations) ────────
NEGATIVE_PROMPT = (
    "text, letters, watermark, logo, signature, "
    "blurry, low quality, jpeg artifacts, "
    "deformed, ugly, extra limbs, bad anatomy, disfigured, duplicate, mutated, "
    "frame, border, collage, split screen, multiple views, photomontage, "
    "deep fried, oversaturated, cartoon, 3d render, plastic, doll-like, "
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

    def get_scene_style(self, scene_idx: int, total_scenes: int) -> str:
        """Return the full style prefix for a specific scene index.

        The base prefix is augmented with a colour-temperature modifier
        that shifts along a pre-defined arc as the video progresses
        (warm → neutral → cool → desaturated → warm resolution).

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
            ``"simple"`` → shallow DoF, minimal background.
            ``"balanced"`` → natural depth.
            ``"rich"`` → deep DoF, detailed textures.
        """
        base = "16:9 aspect ratio, no text, no watermark, no blur, sharp focus"

        extras: dict[str, str] = {
            "simple": (
                "shallow depth of field, blurred background, "
                "minimal elements, clean composition"
            ),
            "balanced": (
                "natural depth of field, balanced composition, "
                "cinematic lighting"
            ),
            "rich": (
                "deep depth of field, detailed environment, "
                "rich textures, complex lighting"
            ),
        }
        extra = extras.get(density, extras["balanced"])
        return f"{base}, {extra}"

    # ── Internal ────────────────────────────────────────────────

    def _build_base_prefix(self) -> str:
        """Build the fixed style prefix from channel config.

        Combines:
          - ``IMAGE_STYLE_MODIFIERS`` (free-text style modifiers)
          - ``COLOR_PALETTE`` → human-readable colour hints
          - ``CANAL_NARRATIVE_STYLE`` (optional narrative flavour)
        """
        cfg = self._config

        # Core style from channel
        style_mod: str = getattr(
            cfg, "IMAGE_STYLE_MODIFIERS",
            "cinematic documentary photography, 16:9, atmospheric",
        )
        # Normalize — ensure it's a clean comma-separated string.
        if isinstance(style_mod, tuple):
            style_mod = ", ".join(s for s in style_mod if s)

        # Colour palette hints (human-readable terms)
        palette = getattr(cfg, "COLOR_PALETTE", {})
        colour_hint = self._palette_to_hint(palette)
        if colour_hint:
            style_mod = f"{style_mod}, {colour_hint}"

        # Narrative style adds flavour
        narrative: str = getattr(cfg, "CANAL_NARRATIVE_STYLE", "")
        if narrative and narrative.lower() not in style_mod.lower():
            style_mod = f"{narrative} style, {style_mod}"

        return style_mod

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

        # --- brightness ---
        avg = (r + g + b) / 3.0
        if avg < 70:
            lum = "dark moody"
        elif avg < 140:
            lum = "muted"
        elif avg < 200:
            lum = "bright"
        else:
            lum = "high-key"

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
