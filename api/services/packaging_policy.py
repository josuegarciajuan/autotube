"""Config-driven, evidence-first packaging checks.

These checks are deliberately advisory: they reject unsafe packaging before a
new upload, but never mutate an already published video.
"""
import re
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageStat

from pipeline.title_tokens import (
    contains_banned_token,
    has_dangling_tail,
    is_all_caps,
    uppercase_words,
)


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

    # ── Shared-token checks (v49) ───────────────────────────────────
    if contains_banned_token(text):
        reasons.append("banned_token")
    if has_dangling_tail(text):
        reasons.append("incomplete_phrase")
    if "|" in text or "[" in text or "]" in text:
        reasons.append("injected_suffix")

    # Capitalisation is policy-driven: channels that intentionally use
    # Title Case must not be penalised for it.
    caps_policy = str(getattr(config, "TITLE_CAPS_POLICY", "sentence") or "sentence").lower()
    if caps_policy in {"sentence", "one_word_caps"} and len(uppercase_words(text)) > 1:
        reasons.append("excessive_caps")
    if is_all_caps(text):
        reasons.append("all_caps")

    return ValidationResult(not reasons, tuple(dict.fromkeys(reasons)))


_OVERLAY_CLICHES = {
    "oculto", "oculta", "real", "prohibido", "impactante", "increíble",
    "increible", "secreto", "secreta", "nadie", "exclusivo", "inédito",
    "inedito", "shock", "impensable",
}


def validate_thumbnail_overlay(overlay: str, max_chars: int = 32) -> ValidationResult:
    text = " ".join(str(overlay or "").split())
    reasons: list[str] = []
    # A "L1 | L2" overlay is two independent lines: the char budget applies to
    # each line, not to the joined string (otherwise every 2-line overlay would
    # be rejected at the fail-closed upload gate).
    lines = [part.strip() for part in text.split("|")] if "|" in text else [text]
    if any(len(line) > max_chars for line in lines if line):
        reasons.append("length")
    tokens = [t.strip("|,:;.!?()[]").casefold() for t in text.split()]
    if len(set(tokens) & _OVERLAY_CLICHES) >= 2:
        reasons.append("repetitive_claims")
    # A pure-cliché overlay ("OCULTO REAL") carries no specific information.
    if tokens and set(tokens) <= _OVERLAY_CLICHES:
        reasons.append("generic_overlay")
    if text.count("|") > 1:
        reasons.append("too_many_lines")
    return ValidationResult(not reasons, tuple(dict.fromkeys(reasons)))


def validate_thumbnail_diversity(
    new: dict,
    recent: list[dict] | None,
    config,
) -> ValidationResult:
    """Advisory diversity check between consecutive thumbnails of a channel.

    ``new``/``recent`` dicts carry ``layout``, ``color_key`` and ``overlay``.
    Unlike the upload gate, this never blocks a publish: callers typically use
    it to regenerate once when a repeat is detected.
    """
    reasons: list[str] = []
    recent = [r for r in (recent or []) if r]
    if not recent:
        return ValidationResult(True, ())

    depth = int(getattr(config, "THUMBNAIL_LAYOUT_HISTORY_DEPTH", 3))
    blocked_layouts = {
        str(r.get("layout") or "") for r in recent[:depth] if r.get("layout")
    }
    if new.get("layout") and str(new["layout"]) in blocked_layouts:
        reasons.append("layout_repeated")

    # Hue distance vs the closest recent dominant colour.
    new_key = str(new.get("color_key") or "")
    try:
        from pipeline.thumbnail_color import hue_distance, hue_of_key
    except Exception:  # pragma: no cover - defensive
        hue_distance = hue_of_key = None  # type: ignore
    if new_key and hue_distance and hue_of_key:
        new_hue = hue_of_key(new_key)
        min_distance = int(getattr(config, "THUMBNAIL_ACCENT_HUE_DISTANCE_MIN", 40))
        if new_hue is not None:
            for r in recent[:depth]:
                old_hue = hue_of_key(str(r.get("color_key") or ""))
                if old_hue is None:
                    continue
                if hue_distance(new_hue, old_hue) < min_distance:
                    reasons.append("color_repeated")
                    break

    new_overlay = " ".join(str(new.get("overlay") or "").split()).casefold()
    if new_overlay:
        for r in recent[:depth]:
            old_overlay = " ".join(str(r.get("overlay") or "").split()).casefold()
            if old_overlay and old_overlay == new_overlay:
                reasons.append("overlay_repeated")
                break

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
