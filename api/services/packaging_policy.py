"""Config-driven, evidence-first packaging checks.

These checks are deliberately advisory: they reject unsafe packaging before a
new upload, but never mutate an already published video.
"""
import re
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageStat


@dataclass(frozen=True)
class ValidationResult:
    valid: bool
    reasons: tuple[str, ...] = ()


def validate_title(title: str, config) -> ValidationResult:
    text = " ".join(str(title or "").split())
    reasons: list[str] = []
    minimum = int(getattr(config, "TITLE_MIN_CHARS", 28))
    maximum = int(getattr(config, "TITLE_MAX_CHARS", 65))
    if len(text) < minimum or len(text) > maximum:
        reasons.append("length")
    lowered = text.casefold()
    if any(str(p).casefold() in lowered for p in getattr(config, "TITLE_BANNED_PATTERNS", ())):
        reasons.append("generic_sensationalism")
    required = set(getattr(config, "TITLE_REQUIRED_SPECIFICITY", ()))
    if "year" in required and not re.search(r"\b(?:18|19|20)\d{2}\b", text):
        reasons.append("specificity")
    # A named place/person is represented by a capitalized token beyond the
    # initial article. This stays language-agnostic and avoids a place list.
    if "place_or_person" in required or "person_or_place" in required:
        words = re.findall(r"\b[A-ZÁÉÍÓÚÑ][\wÁÉÍÓÚÑ-]{2,}\b", text)
        if not any(word.casefold() not in {"el", "la", "los", "las", "un", "una"} for word in words):
            reasons.append("specificity")
    return ValidationResult(not reasons, tuple(dict.fromkeys(reasons)))


def validate_thumbnail_overlay(overlay: str, max_chars: int = 32) -> ValidationResult:
    text = " ".join(str(overlay or "").split())
    reasons: list[str] = []
    if len(text) > max_chars:
        reasons.append("length")
    tokens = [t.strip("|,:;.!?()[]").casefold() for t in text.split()]
    banned = {"oculto", "real", "prohibido", "impactante", "increíble", "increible"}
    if len(set(tokens) & banned) >= 2:
        reasons.append("repetitive_claims")
    if text.count("|") > 1:
        reasons.append("too_many_lines")
    return ValidationResult(not reasons, tuple(dict.fromkeys(reasons)))


def validate_thumbnail_file(path, min_width: int = 640, min_height: int = 360) -> ValidationResult:
    reasons: list[str] = []
    try:
        with Image.open(Path(path)) as image:
            image.verify()
        with Image.open(Path(path)).convert("RGB") as image:
            if image.width < min_width or image.height < min_height:
                reasons.append("dimensions")
            if all(high - low < 8 for low, high in ImageStat.Stat(image).extrema):
                reasons.append("blank_artwork")
    except (OSError, ValueError):
        reasons.append("invalid_image")
    return ValidationResult(not reasons, tuple(reasons))


def validate_video_packaging(video: dict, config) -> ValidationResult:
    """Final upload gate for title, overlay, and rendered thumbnail."""
    results = [validate_title(video.get("titulo_final", ""), config)]
    results.append(validate_thumbnail_overlay(
        video.get("thumbnail_text", ""),
        int(getattr(config, "THUMBNAIL_MAX_OVERLAY_CHARS", 32)),
    ))
    results.append(validate_thumbnail_file(video.get("thumbnail_path", "")))
    reasons = tuple(dict.fromkeys(r for result in results for r in result.reasons))
    return ValidationResult(not reasons, reasons)
