"""Color-analysis helpers for content-driven thumbnail palettes.

The old pipeline tinted every thumbnail with a fixed per-channel palette
(gold for canal3, cold blue for canal4, ...). That made all videos look the
same. This module derives the palette from the *content image* instead and
chooses an accent colour that (a) contrasts with the image and (b) does not
repeat the dominant hue of the last few videos of the same channel.

All functions are pure and dependency-light (PIL + ``colorsys``) so they can be
unit-tested without touching the network or the generator.
"""

from __future__ import annotations

import colorsys
import random
from pathlib import Path

from PIL import Image

# 12 hue buckets of 30 degrees: 0 = reds, 1 = oranges, ... 11 = magenta-reds.
HUE_BUCKET_DEGREES = 30

# A small, saturated accent bank used when the image gives no strong signal.
_ACCENT_BANK: list[tuple[int, int, int]] = [
    (230, 57, 70),    # alert red
    (255, 122, 0),    # emergency orange
    (255, 209, 102),  # warm gold
    (6, 214, 160),    # teal
    (17, 138, 178),   # cyan blue
    (58, 134, 255),   # cobalt
    (157, 78, 221),   # violet
    (255, 45, 149),   # magenta
]

_NEUTRAL_SATURATION = 0.18  # below this a colour reads as grey/black/white


def _as_image(image) -> Image.Image:
    """Accept a path, an ``Image`` or an ndarray-like and return an RGB image."""
    if isinstance(image, Image.Image):
        return image.convert("RGB")
    path = Path(image)
    return Image.open(path).convert("RGB")


def dominant_palette(image, k: int = 5) -> list[tuple[int, int, int]]:
    """Return up to *k* dominant colours, ordered by coverage (most first).

    Uses a downscaled median-cut quantisation. Kept intentionally simple: it
    only needs to be stable and fast, not perceptually perfect.
    """
    img = _as_image(image)
    img = img.resize((96, 54), Image.LANCZOS)
    try:
        quant = img.quantize(colors=max(2, k), method=Image.MEDIANCUT)
    except Exception:
        return [(128, 128, 128)]

    palette = quant.getpalette() or []
    counts = quant.getcolors(maxcolors=1 << 16) or []
    # counts = [(count, palette_index), ...]
    counts.sort(key=lambda item: item[0], reverse=True)

    out: list[tuple[int, int, int]] = []
    for _count, idx in counts[:k]:
        base = idx * 3
        if base + 2 >= len(palette):
            continue
        out.append((palette[base], palette[base + 1], palette[base + 2]))
    return out or [(128, 128, 128)]


def rgb_to_hsv(rgb: tuple[int, int, int]) -> tuple[float, float, float]:
    r, g, b = (channel / 255.0 for channel in rgb)
    return colorsys.rgb_to_hsv(r, g, b)


def hsv_to_rgb(h: float, s: float, v: float) -> tuple[int, int, int]:
    r, g, b = colorsys.hsv_to_rgb(h % 1.0, max(0.0, min(1.0, s)),
                                  max(0.0, min(1.0, v)))
    return (int(round(r * 255)), int(round(g * 255)), int(round(b * 255)))


def hue_bucket(rgb: tuple[int, int, int]) -> int:
    """Map a colour to one of 12 hue buckets (0..11)."""
    h, _s, _v = rgb_to_hsv(rgb)
    return int((h * 360.0) // HUE_BUCKET_DEGREES) % 12


def hue_distance(a: int, b: int) -> int:
    """Circular distance in degrees between two hues (0..180)."""
    diff = abs((a - b) % 360)
    return min(diff, 360 - diff)


def _dominant_hue(palette: list[tuple[int, int, int]]) -> tuple[int, float]:
    """Return (hue_degrees, saturation) of the most saturated dominant colour."""
    best_hue = 0.0
    best_sat = 0.0
    for rgb in palette:
        h, s, v = rgb_to_hsv(rgb)
        if s >= _NEUTRAL_SATURATION and v > 0.1 and s > best_sat:
            best_hue, best_sat = h * 360.0, s
    if best_sat == 0.0:
        # Fall back to the first colour's hue even if grey.
        best_hue = rgb_to_hsv(palette[0])[0] * 360.0 if palette else 0.0
    return int(round(best_hue)) % 360, best_sat


def _relative_luminance(rgb: tuple[int, int, int]) -> float:
    def channel(c: float) -> float:
        c = c / 255.0
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = (channel(c) for c in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast_ratio(a: tuple[int, int, int], b: tuple[int, int, int]) -> float:
    la, lb = _relative_luminance(a), _relative_luminance(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def contrast_text_color(bg_rgb: tuple[int, int, int]) -> str:
    """Pick white or near-black text for maximum contrast on *bg_rgb*."""
    white, black = (255, 255, 255), (17, 17, 17)
    return "#FFFFFF" if contrast_ratio(bg_rgb, white) >= contrast_ratio(bg_rgb, black) else "#111111"


def choose_accent(
    dominant: list[tuple[int, int, int]],
    recent_hues: list[int] | None = None,
    min_distance: int = 40,
    prefer_palette: list[tuple[int, int, int]] | None = None,
    seed: int | None = None,
    min_contrast: float = 1.6,
) -> str:
    """Choose an accent hex colour that contrasts and avoids *recent_hues*.

    Candidates are (in priority order): channel-brand accents (``prefer_palette``),
    complementary hues derived from the image's strongest hue, and a small
    saturated accent bank. The winner maximises the minimum hue distance to the
    recent history, then contrast against the image.
    """
    recent_hues = list(recent_hues or [])
    dominant_hue, _sat = _dominant_hue(dominant or [(128, 128, 128)])
    reference_bg = dominant[0] if dominant else (128, 128, 128)

    candidates: list[tuple[int, int, int]] = []
    if prefer_palette:
        candidates.extend(tuple(c) for c in prefer_palette)
    # Complementary / triadic accents derived from the image.
    for offset in (180, 150, 210, 90, 270):
        h = ((dominant_hue + offset) % 360) / 360.0
        candidates.append(hsv_to_rgb(h, 0.85, 0.90))
    candidates.extend(_ACCENT_BANK)

    def _score(rgb: tuple[int, int, int]) -> tuple[float, float]:
        h, s, _v = rgb_to_hsv(rgb)
        hue = h * 360.0
        if recent_hues:
            nearest = min(hue_distance(hue, rh) for rh in recent_hues)
        else:
            nearest = 180.0
        # Penalise colours too close to the image dominant (little contrast).
        hue_gap = hue_distance(hue, dominant_hue)
        contrast = contrast_ratio(rgb, reference_bg)
        if contrast < min_contrast:
            nearest *= 0.4
        return (min(nearest, float(min_distance * 3)), contrast)

    rng = random.Random(seed) if seed is not None else random
    best = max(candidates, key=lambda c: (_score(c), rng.random()))
    # If every candidate repeats a recent hue, nudge the hue to a fresh bucket.
    if recent_hues:
        h, s, v = rgb_to_hsv(best)
        hue = h * 360.0
        if min(hue_distance(hue, rh) for rh in recent_hues) < min_distance:
            fresh = _freshest_hue(recent_hues, min_distance)
            if fresh is not None:
                best = hsv_to_rgb(fresh / 360.0, max(0.75, s), max(0.8, v))
    return "#%02X%02X%02X" % best


def _freshest_hue(recent_hues: list[int], min_distance: int) -> int | None:
    """Return a hue at least *min_distance* from every recent hue, if any."""
    for hue in range(0, 360, 15):
        if all(hue_distance(hue, rh) >= min_distance for rh in recent_hues):
            return hue
    return None


def color_key(rgb_or_palette) -> str:
    """Stable bucket label for persistence and diversity checks."""
    if isinstance(rgb_or_palette, list):
        dominant = rgb_or_palette[0] if rgb_or_palette else (128, 128, 128)
    else:
        dominant = rgb_or_palette
    return f"hue_{hue_bucket(dominant) * HUE_BUCKET_DEGREES:03d}"


def hue_of_key(key: str) -> int | None:
    """Parse ``hue_210`` back to 210; return None if unparseable."""
    try:
        return int(str(key).split("_", 1)[1])
    except (IndexError, ValueError):
        return None
