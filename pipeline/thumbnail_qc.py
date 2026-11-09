"""Thumbnail quality control — cheap, deterministic, auto-verifiable checks.

The feed shows thumbnails at ~100 px tall. A thumbnail that looks good at
1280x720 can be unreadable in the feed, so every generated image is scored
before it is eligible for upload:

* no overlay line ends in a dangling connector (``... BAJO LA``);
* text is large enough at feed size and has local contrast;
* text does not cover the subject / face;
* the image is neither crushed to black nor washed out;
* at most two accent colours (brand + content) compete;
* L1 still anchors to the title (advisory).

The module only depends on Pillow + numpy and is pure enough to unit-test.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Sequence

import numpy as np
from PIL import Image

from pipeline.packaging_brief import _DANGLING_TAIL, _tokens, normalize, title_terms

FEED_HEIGHT_PX = 100.0
MIN_L1_FEED_PX = 15.0
MIN_L2_FEED_PX = 11.0
MIN_BADGE_FEED_PX = 4.0
MAX_DARK_RATIO = 0.55
MAX_BRIGHT_RATIO = 0.10
MIN_TEXT_CONTRAST = 4.5
MAX_TEXT_SUBJECT_OVERLAP = 0.10
MAX_ACCENT_COLORS = 2

_L1_WEIGHT = 20
_L2_WEIGHT = 14
_BADGE_WEIGHT = 5
_OVERLAP_WEIGHT = 22
_DARK_WEIGHT = 12
_BRIGHT_WEIGHT = 8
_CONTRAST_WEIGHT = 16
_ACCENT_WEIGHT = 8
_EMPTY_WEIGHT = 40
_OFF_TOPIC_WEIGHT = 5

_BOX_RE = re.compile(r"^\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*$")


@dataclass
class QCResult:
    """Outcome of the visual QC pass."""

    score: int = 100
    issues: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    metrics: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.issues

    def as_db_fields(self) -> dict:
        return {
            "thumbnail_qc_score": int(self.score),
            "thumbnail_qc_issues": ",".join(self.issues),
        }


def _clamp_box(box: Sequence[float], width: int, height: int) -> tuple[int, int, int, int]:
    x0, y0, x1, y1 = (int(round(v)) for v in box)
    x0 = max(0, min(x0, width - 1))
    y0 = max(0, min(y0, height - 1))
    x1 = max(x0 + 1, min(x1, width))
    y1 = max(y0 + 1, min(y1, height))
    return x0, y0, x1, y1


def parse_box(value) -> tuple[int, int, int, int] | None:
    """Parse ``"x0,y0,x1,y1"`` (or a 4-sequence) into a box, else ``None``."""
    if value is None:
        return None
    if isinstance(value, str):
        match = _BOX_RE.match(value)
        if not match:
            return None
        return tuple(int(g) for g in match.groups())  # type: ignore[return-value]
    try:
        if len(value) == 4:
            return tuple(int(v) for v in value)  # type: ignore[return-value]
    except TypeError:
        return None
    return None


def relative_luminance(rgb: Sequence[float]) -> float:
    """WCAG relative luminance of an sRGB triple (0-255)."""
    def _channel(v: float) -> float:
        c = max(0.0, min(255.0, float(v))) / 255.0
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = (float(x) for x in rgb[:3])
    return 0.2126 * _channel(r) + 0.7152 * _channel(g) + 0.0722 * _channel(b)


def contrast_ratio(rgb_a: Sequence[float], rgb_b: Sequence[float]) -> float:
    la, lb = relative_luminance(rgb_a), relative_luminance(rgb_b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def line_is_dangling(text: str) -> bool:
    """True when a line ends in an article/preposition/conjunction."""
    words = str(text or "").split()
    if not words:
        return False
    last = words[-1].strip(".,;:!?¿¡()[]«»\"'")
    if not last:
        return False
    return normalize(last) in _DANGLING_TAIL


def _region_mean_luma(luma: np.ndarray, box: tuple[int, int, int, int]) -> float:
    x0, y0, x1, y1 = box
    region = luma[y0:y1, x0:x1]
    return float(region.mean()) if region.size else 0.0


def _box_text_contrast(luma: np.ndarray,
                       box: tuple[int, int, int, int]) -> float:
    """Contrast between the brightest and darkest 5% inside a text box.

    White text + black stroke on any background produces a wide luminance
    spread inside the box; flat/low-contrast copy does not. This is a better
    proxy than comparing the box mean with its surround.
    """
    x0, y0, x1, y1 = box
    region = luma[y0:y1, x0:x1]
    if region.size < 16:
        return 1.0
    hi = float(np.percentile(region, 95))
    lo = float(np.percentile(region, 5))
    return contrast_ratio((hi, hi, hi), (lo, lo, lo))


def _surround_mean_luma(luma: np.ndarray, box: tuple[int, int, int, int],
                        pad: int = 18) -> float:
    height, width = luma.shape[:2]
    x0, y0, x1, y1 = box
    ox0, oy0 = max(0, x0 - pad), max(0, y0 - pad)
    ox1, oy1 = min(width, x1 + pad), min(height, y1 + pad)
    outer = luma[oy0:oy1, ox0:ox1]
    inner = luma[y0:y1, x0:x1]
    if outer.size == inner.size:
        return float(outer.mean())
    total = float(outer.sum()) - float(inner.sum())
    count = outer.size - inner.size
    return total / count if count > 0 else float(outer.mean())


def _box_intersection(a: tuple[int, int, int, int],
                      b: tuple[int, int, int, int]) -> float:
    ax0, ay0, ax1, ay1 = a
    bx0, by0, bx1, by1 = b
    ix0, iy0 = max(ax0, bx0), max(ay0, by0)
    ix1, iy1 = min(ax1, bx1), min(ay1, by1)
    if ix1 <= ix0 or iy1 <= iy0:
        return 0.0
    inter = (ix1 - ix0) * (iy1 - iy0)
    area = max(1, (ax1 - ax0) * (ay1 - ay0))
    return inter / area


def _feed_px(font_px: float | None, image_height: int) -> float | None:
    if not font_px or image_height <= 0:
        return None
    return float(font_px) * (FEED_HEIGHT_PX / float(image_height))


def run_qc(
    image,
    *,
    l1: str = "",
    l2: str = "",
    badge: str = "",
    title: str = "",
    text_boxes: Iterable[Sequence[float]] | None = None,
    text_font_px: dict | None = None,
    subject_bbox: Sequence[float] | None = None,
    accent_colors: Iterable[Sequence[float]] | None = None,
    require_text: bool = True,
) -> QCResult:
    """Score a thumbnail against the feed-visibility rules.

    ``image`` may be a path or a PIL image. ``text_font_px`` maps
    ``{"l1": px, "l2": px, "badge": px}`` to the *canvas* size actually used.
    ``text_boxes`` are canvas-space boxes; when omitted, a conservative
    bottom-third region is used for contrast checks only.
    """
    result = QCResult()
    if isinstance(image, Image.Image):
        img = image.convert("RGB")
    else:
        img = Image.open(Path(image)).convert("RGB")
    width, height = img.size
    luma = np.asarray(img.convert("L"), dtype=np.float32)

    mean_luma = float(luma.mean())
    dark_ratio = float((luma < 20).mean())
    bright_ratio = float((luma > 245).mean())
    result.metrics.update({
        "width": width,
        "height": height,
        "mean_luma": round(mean_luma, 1),
        "dark_ratio": round(dark_ratio, 3),
        "bright_ratio": round(bright_ratio, 3),
    })

    if require_text and not (str(l1 or "").strip() or str(l2 or "").strip()):
        result.issues.append("overlay_empty")
        result.score -= _EMPTY_WEIGHT

    for name, line in (("l1", l1), ("l2", l2)):
        if line and line_is_dangling(line):
            result.issues.append(f"text_dangling_{name}")
            result.score -= _L1_WEIGHT if name == "l1" else _L2_WEIGHT

    # Moody documentary art is fine when a bright focal point survives: the
    # 8.72% CTR benchmark is 49% dark. Flag only when the frame is both mostly
    # black AND has no bright subject (mean luma very low).
    if dark_ratio > MAX_DARK_RATIO and mean_luma < 55:
        result.issues.append("too_dark")
        result.score -= _DARK_WEIGHT
    if bright_ratio > MAX_BRIGHT_RATIO:
        result.issues.append("washed_out")
        result.score -= _BRIGHT_WEIGHT

    font_px = text_font_px or {}
    l1_feed = _feed_px(font_px.get("l1"), height)
    l2_feed = _feed_px(font_px.get("l2"), height)
    badge_feed = _feed_px(font_px.get("badge"), height)
    if l1_feed is not None:
        result.metrics["l1_feed_px"] = round(l1_feed, 1)
        if l1_feed < MIN_L1_FEED_PX:
            result.issues.append("l1_too_small")
            result.score -= _L1_WEIGHT
    if l2_feed is not None:
        result.metrics["l2_feed_px"] = round(l2_feed, 1)
        if l2_feed < MIN_L2_FEED_PX:
            result.issues.append("l2_too_small")
            result.score -= _L2_WEIGHT
    if badge_feed is not None:
        result.metrics["badge_feed_px"] = round(badge_feed, 1)
        if badge_feed < MIN_BADGE_FEED_PX:
            result.issues.append("badge_too_small")
            result.score -= _BADGE_WEIGHT

    boxes = []
    for raw in (text_boxes or []):
        box = parse_box(raw)
        if box:
            boxes.append(_clamp_box(box, width, height))
    if boxes:
        # Local contrast: luminance spread inside the painted text box.
        worst = min(_box_text_contrast(luma, box) for box in boxes)
        result.metrics["text_contrast"] = round(worst, 2)
        if worst < MIN_TEXT_CONTRAST:
            result.issues.append("low_text_contrast")
            result.score -= _CONTRAST_WEIGHT

    subject = parse_box(subject_bbox)
    if subject:
        _sx0, _sy0, _sx1, _sy1 = _clamp_box(subject, width, height)
        subject_area = (_sx1 - _sx0) * (_sy1 - _sy0) / float(width * height)
        if subject_area > 0.55:
            # A subject that fills the frame cannot be "avoided"; overlap is
            # not actionable and the panel/contrast checks handle readability.
            result.metrics["subject_area_ratio"] = round(subject_area, 3)
            subject = None
    if subject and boxes:
        overlap = max(_box_intersection(box, _clamp_box(subject, width, height))
                      for box in boxes)
        result.metrics["text_subject_overlap"] = round(overlap, 3)
        if overlap > MAX_TEXT_SUBJECT_OVERLAP:
            result.issues.append("text_overlaps_subject")
            result.score -= _OVERLAP_WEIGHT
    elif subject:
        # Without a text box we still record the subject so callers can pass it
        # to a later compositor pass.
        result.metrics["subject_bbox"] = list(_clamp_box(subject, width, height))

    accents = []
    for color in (accent_colors or []):
        if color is None:
            continue
        rgb = tuple(int(float(c)) for c in list(color)[:3])
        if rgb not in accents:
            accents.append(rgb)
    result.metrics["accent_count"] = len(accents)
    if len(accents) > MAX_ACCENT_COLORS:
        result.issues.append("too_many_accents")
        result.score -= _ACCENT_WEIGHT

    if l1 and title:
        l1_tokens = set(_tokens(l1))
        if l1_tokens and not (l1_tokens & set(title_terms(title))):
            result.warnings.append("off_topic")
            result.score -= _OFF_TOPIC_WEIGHT

    result.score = max(0, min(100, int(result.score)))
    return result


def qc_from_path(path, **kwargs) -> QCResult:
    """Convenience wrapper for ``run_qc(Path, ...)``."""
    return run_qc(path, **kwargs)
