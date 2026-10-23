"""P01-C — per-final-scene query generation.

The existing fallback ladder (``pipeline.cinematic_staging``) is preserved: its
queries are appended verbatim after the new scene-specific variants.  The new
variants are derived only from :class:`SceneSpec` fields and the scene's own
narration fragment, so two sub-scenes of the same block no longer share one
inherited query.

``must_avoid`` terms are never appended as positive keywords.  A generated
variant that would be nothing but forbidden terms is dropped instead.
"""

from __future__ import annotations

import re
from typing import Any

from .contracts import SceneSpec

_WORD_RE = re.compile(r"[a-zA-Z0-9áéíóúüñ]+")
_TIMELESS_ERAS = {"", "atemporal", "presente", "present", "timeless", "modern"}


def _words(text: Any) -> list[str]:
    return [w.lower() for w in _WORD_RE.findall(str(text or ""))]


def _join(*parts: Any) -> str:
    """Deduplicate words while preserving the first-seen order."""
    seen: set[str] = set()
    out: list[str] = []
    for part in parts:
        for word in _WORD_RE.findall(str(part or "")):
            key = word.lower()
            if key in seen:
                continue
            seen.add(key)
            out.append(word)
    return " ".join(out)


def _fit(query: str, max_len: int = 100) -> str:
    query = re.sub(r"\s+", " ", (query or "").strip())
    if len(query) <= max_len:
        return query
    return query[:max_len].rsplit(" ", 1)[0].rstrip(" ,.")


def _avoid_words(must_avoid: list[str]) -> set[str]:
    avoid: set[str] = set()
    for term in must_avoid:
        avoid.update(_words(term))
    return avoid


def _is_avoid_only(query: str, must_avoid: list[str]) -> bool:
    if not must_avoid:
        return False
    qwords = set(_words(query))
    if not qwords:
        return True
    avoid_words = _avoid_words(must_avoid)
    return bool(avoid_words) and qwords.issubset(avoid_words)


def _strip_forbidden(query: str, must_avoid: list[str]) -> str:
    """Remove ``must_avoid`` words from a positive query (never add them)."""
    avoid = _avoid_words(must_avoid)
    if not avoid:
        return query
    return " ".join(
        w for w in _WORD_RE.findall(str(query or "")) if w.lower() not in avoid
    )


def _narration_query(spec: SceneSpec, limit: int = 8) -> str:
    """Dependency-free query derived from the FINAL narration slice."""
    words = [
        w.lower() for w in _WORD_RE.findall(str(spec.narration_fragment or ""))
        if len(w) > 1
    ]
    stop = {
        "el", "la", "los", "las", "un", "una", "de", "del", "en", "y", "o",
        "que", "con", "por", "para", "al", "se", "su", "the", "a", "an",
        "of", "in", "on", "and", "or", "to", "with",
    }
    meaningful = [w for w in words if w not in stop]
    return " ".join(meaningful[:limit])


def generate_scene_query_variants(
    spec: SceneSpec,
    base_queries: list[str] | None = None,
    *,
    max_variants: int = 10,
    max_len: int = 100,
) -> list[dict[str, str]]:
    """Return ordered ``{query, tier, source}`` variants for one scene.

    The first variants are the most specific (subject+action+environment);
    the ladder degrades gradually and finally reuses the legacy query pool.
    """
    from pipeline.cinematic_staging import classify_query_tier, fit_query

    subject = (spec.subject or "").strip()
    action = (spec.action or "").strip()
    environment = (spec.environment or "").strip()
    era = (spec.era or "").strip()
    era_ok = era.lower() not in _TIMELESS_ERAS
    concept = (spec.visual_concept or "").strip()
    shot = (spec.desired_shot or "").strip()
    include = [str(x).strip() for x in spec.must_include if str(x).strip()]

    variants: list[dict[str, str]] = []
    seen: set[str] = set()

    def add(query: str, source: str) -> None:
        q = fit_query(_fit(_strip_forbidden(query, spec.must_avoid), max_len), max_len)
        if not q:
            return
        key = q.lower()
        if key in seen:
            return
        if _is_avoid_only(q, spec.must_avoid):
            return
        seen.add(key)
        variants.append({
            "query": q,
            "tier": classify_query_tier(q, None),
            "source": source,
        })

    # 1. Most specific combinations.
    add(_join(subject, action, environment), "subject_action_environment")
    add(_join(subject, environment), "subject_environment")
    add(_join(action, environment), "action_environment")
    add(_join(action, subject), "action_subject")
    for phrase in include[:2]:
        add(_join(phrase, subject, action), "must_include")
    narration = _narration_query(spec)
    if narration:
        add(narration, "narration_fragment")

    # 2. Era-aware (only when the era is meaningful and not already present).
    if era_ok:
        era_base = _join(subject, action) or _join(subject, environment)
        if era_base:
            add(_join(era_base, era), "era_aware")

    # 3. Shot-aware (only when it adds a camera-observable direction).
    if shot and (subject or action):
        add(_join(shot, subject, action), "shot_aware")

    # 4. Visual concept / symbolic direction.
    if concept:
        add(_join(concept, subject), "visual_concept")
        add(concept, "visual_concept")

    # 5. Progressive simplification (avoid combinatorial explosion).
    add(_join(subject, action), "simplified_2")
    add(subject or action, "simplified_1")

    # 6. Preserve the legacy ladder exactly as-is.
    for query in base_queries or []:
        add(str(query or ""), "legacy_ladder")

    return variants[: max(1, int(max_variants))]


def generate_scene_queries(
    spec: SceneSpec,
    base_queries: list[str] | None = None,
    *,
    max_variants: int = 10,
    max_len: int = 100,
) -> list[str]:
    """Convenience wrapper returning only the query strings."""
    return [
        item["query"]
        for item in generate_scene_query_variants(
            spec,
            base_queries,
            max_variants=max_variants,
            max_len=max_len,
        )
    ]
