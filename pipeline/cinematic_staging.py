"""Deterministic helpers for grounded, era-aware cinematic staging.

The LLM describes the narrative, but this module translates recurring abstract
ideas into things a camera can observe.  It is deliberately provider-agnostic:
callers still own query length limits, fallback order, quota, and deduplication.
"""

from __future__ import annotations

import re
from typing import Any

from pipeline.era_terms import anachronism_hits, era_anchor


_PERSON_WORDS = {
    "person", "people", "man", "woman", "men", "women", "archivist",
    "merchant", "sailor", "explorer", "crew", "worker", "soldier",
}


def _text(*values: Any) -> str:
    return " ".join(str(value or "") for value in values).lower()


def _historical(ctx: Any) -> bool:
    if not ctx:
        return False
    try:
        return era_anchor(getattr(ctx, "era_decade", ""), getattr(ctx, "era", "")) is not None
    except Exception:
        return False


def sanitize_shot_direction(direction: str, has_person: bool = False) -> str:
    """Keep directional prompts camera-observable and safe for people."""
    direction = (direction or "medium shot").strip()
    if not has_person:
        return direction
    direction = re.sub(r"\b(close[- ]up|extreme close[- ]up|headshot)\b", "", direction, flags=re.I)
    direction = re.sub(r"\s+", " ", direction).strip(" ,")
    if not direction or not re.search(r"\b(medium|wide|distant|long)\s+shot\b", direction, flags=re.I):
        direction = f"medium shot, {direction}" if direction else "medium shot"
    return f"{direction}, person integrated in the environment"


def has_person_reference(text: str) -> bool:
    """Return whether a query explicitly refers to a person."""
    return bool(_PERSON_WORDS.intersection(re.findall(r"[a-z]+", _text(text))))


def sanitize_person_query(query: str) -> str:
    """Remove face-close-up language from a query that names a person."""
    if not has_person_reference(query):
        return query
    clean = re.sub(r"\b(close[- ]up|extreme close[- ]up|headshot|portrait)\b", "", query, flags=re.I)
    clean = re.sub(r"\s+", " ", clean).strip(" ,")
    return f"{clean}, medium shot" if clean else "medium shot, person integrated in environment"


def fit_query(query: str, max_len: int = 100) -> str:
    """Fit a provider query at a complete-word boundary."""
    query = re.sub(r"\s+", " ", (query or "").strip())
    if len(query) <= max_len:
        return query
    return query[:max_len].rsplit(" ", 1)[0].rstrip(" ,")


def build_scene_brief(scene_text: str = "", base_query: str = "", theme_ctx: Any = None) -> str:
    """Return a concrete staged brief without erasing the source subject.

    These are conservative semantic anchors, not channel-specific mappings.
    They make stock and image prompts depict an action, object, or setting
    rather than a literal label (``money``) or an unfilmable abstraction.
    """
    source = _text(scene_text, base_query)
    parts: list[str] = []

    context_text = _text(
        getattr(theme_ctx, "primary_subject", ""),
        getattr(theme_ctx, "genre", ""),
        getattr(theme_ctx, "era", ""),
    )
    if _historical(theme_ctx) and re.search(r"\b(money|currency|cash|wealth|payment)\b", source):
        if "ancient" in source or "egypt" in source or "historical" in source or "ancient" in context_text:
            parts.append("historical exchange barter goods weighing scales")
        else:
            parts.append("period exchange trade goods")

    if re.search(r"\b(expedition|voyage|explor|cross(?:es|ing)? the atlantic)\b", source):
        era = _text(getattr(theme_ctx, "era_decade", ""), getattr(theme_ctx, "era", ""))
        if "16th" in era:
            parts.append("wooden caravel sailing vessel at sea")
        elif "17th" in era:
            parts.append("wooden sailing vessel at sea")

    if re.search(r"\b(archive|archival|investigation|documents?)\b", source):
        parts.append("archivist examining documents, turning pages in a historical archive")

    if not parts:
        parts.append((base_query or scene_text or "documentary scene").strip())

    return ", ".join(dict.fromkeys(p for p in parts if p))


def build_contextual_fallback(block_type: str, theme_ctx: Any, portrait: bool = False) -> str:
    """Build a useful fallback from context before generic block fallbacks."""
    subject = getattr(theme_ctx, "primary_subject", "") if theme_ctx else ""
    era = getattr(theme_ctx, "era_decade", "") or getattr(theme_ctx, "era", "") if theme_ctx else ""
    motif = (getattr(theme_ctx, "key_motifs", []) or [])[:1] if theme_ctx else []
    tokens = [subject, era, *motif]
    result = " ".join(str(token).replace("_", " ") for token in tokens if token).strip()
    result = result or ("documentary detail" if block_type != "hook" else "documentary establishing scene")
    return f"{result} vertical" if portrait else result


def rank_candidates(candidates: list[dict], scene_text: str, theme_ctx: Any = None) -> list[dict]:
    """Drop historical anachronisms and rank remaining metadata by overlap."""
    scene_words = {w for w in re.findall(r"[a-z0-9]+", _text(scene_text)) if len(w) > 2}
    ranked: list[tuple[float, int, dict]] = []
    for index, candidate in enumerate(candidates):
        metadata = " ".join([
            str(candidate.get("title") or ""),
            str(candidate.get("description") or ""),
            " ".join(map(str, candidate.get("tags") or [])),
            str(candidate.get("page_url") or ""),
        ]).lower()
        if _historical(theme_ctx) and anachronism_hits(metadata):
            continue
        overlap = sum(1 for word in scene_words if re.search(r"\b" + re.escape(word) + r"\b", metadata))
        period_bonus = 1 if any(term in metadata for term in ("wooden", "caravel", "sailing", "historical", "ancient")) else 0
        ranked.append((float(overlap + period_bonus), -index, candidate))
    ranked.sort(reverse=True, key=lambda item: (item[0], item[1]))
    return [candidate for _, _, candidate in ranked]


def enrich_scene_query(query: str, theme_ctx: Any = None, scene_text: str = "") -> str:
    """Ground an LLM query while retaining the original narrative keywords."""
    brief = build_scene_brief(scene_text, query, theme_ctx)
    original = (query or scene_text or "").strip()
    if brief == original.lower() or not original:
        return brief
    return f"{original}, {brief}"
