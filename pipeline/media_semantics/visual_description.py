"""P02-D — compact visual description derived from :class:`SceneSpec`.

The textual search query is *not* a good image-encoder prompt.  This module
builds a short, observable visual description for CLIP/SigLIP/VLM grounding.
It never turns ``must_avoid`` into a desired element and strips forbidden
tokens from every positive field.
"""

from __future__ import annotations

import re
from typing import Any, Iterable

from .contracts import SceneSpec

_WORD_RE = re.compile(r"[a-zA-Z0-9áéíóúüñ]+")
_TIMELESS_ERAS = {
    "", "atemporal", "presente", "present", "timeless", "modern", "contemporary",
}
_DEFAULT_MAX_CHARS = 240


def _words(text: Any) -> list[str]:
    return _WORD_RE.findall(str(text or ""))


def _clean(text: Any, forbidden: set[str]) -> str:
    words = [w for w in _words(text) if w.lower() not in forbidden]
    return " ".join(words).strip()


def _uniq(parts: Iterable[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for part in parts:
        text = re.sub(r"\s+", " ", str(part or "")).strip(" ,.;")
        if not text:
            continue
        key = text.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(text)
    return out


def _fit(text: str, max_chars: int) -> str:
    text = re.sub(r"\s+", " ", text).strip(" ,.;")
    if len(text) <= max_chars:
        return text
    cut = text[:max_chars]
    if " " in cut:
        cut = cut.rsplit(" ", 1)[0]
    return cut.rstrip(" ,.;")


def build_visual_description(
    spec: SceneSpec,
    *,
    max_chars: int = _DEFAULT_MAX_CHARS,
) -> str:
    """Return the encoder-facing visual intent for one scene.

    Fields are ordered by visual utility: subject → action → environment →
    era → shot → concept → must_include.  The narration is deliberately not
    used as the primary description (it is abstract prose); it is only a last
    resort when the structured spec has no visual content at all.
    """
    forbidden: set[str] = set()
    for term in spec.must_avoid or []:
        forbidden.update(w.lower() for w in _words(term))

    subject = _clean(spec.subject, forbidden)
    action = _clean(spec.action, forbidden)
    environment = _clean(spec.environment, forbidden)
    era = _clean(spec.era, forbidden)
    if era.lower() in _TIMELESS_ERAS:
        era = ""
    shot = _clean(spec.desired_shot, forbidden)
    concept = _clean(spec.visual_concept, forbidden)
    include = [_clean(x, forbidden) for x in (spec.must_include or [])]
    include = [x for x in include if x]
    narration = _clean(spec.narration_fragment, forbidden)

    # Compose observable phrases, not a bag of comma-separated metadata.
    core = " ".join(x for x in (subject, action, environment) if x).strip()
    parts: list[str] = []
    if core:
        parts.append(core)
    elif subject or action or environment:
        parts.append(" ".join(x for x in (subject, action, environment) if x))
    if era:
        parts.append(era)
    if shot:
        parts.append(shot)
    if concept and concept.lower() not in core.lower():
        parts.append(concept)
    for item in include[:3]:
        parts.append(item)
    if not parts and narration:
        parts.append(narration)

    description = ", ".join(_uniq(parts))
    return _fit(description, max_chars=max(40, int(max_chars)))


def visual_negative_constraints(spec: SceneSpec) -> list[str]:
    """Canonical negative visual constraints (never positive description)."""
    out: list[str] = []
    for term in spec.must_avoid or []:
        clean = re.sub(r"\s+", " ", str(term or "")).strip()
        if clean and clean.lower() not in {x.lower() for x in out}:
            out.append(clean)
    return out
