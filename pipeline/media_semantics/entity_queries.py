"""P05-K — bounded entity-aware archive queries.

Archive metadata (Wikimedia/NASA) is organised around *entities* (people,
places, missions, works), not modern b-roll phrasing.  This module extracts a
small, deterministic entity/topic phrase from the SceneSpec and builds a few
archive-friendly queries.

It is deliberately not a knowledge graph: no Wikidata lookups, no LLM call, no
new network dependency.  Explicit ``scene_spec.extra`` hints win when present.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from .contracts import SceneSpec

_QUOTED_RE = re.compile(r"[\"“”']([^\"“”']{3,80})[\"“”']")
_TOKEN_RE = re.compile(r"[0-9A-Za-zÁÉÍÓÚÜÑáéíóúüñ][0-9A-Za-zÁÉÍÓÚÜÑáéíóúüñ'\-]*")

_GENERIC = frozenset({
    "a", "an", "the", "of", "in", "on", "at", "to", "for", "with", "and",
    "or", "is", "are", "was", "were", "be", "as", "by", "from", "into",
    "that", "this", "scene", "shot", "image", "video", "footage", "close",
    "closeup", "close-up", "wide", "view", "view of", "detail", "details",
    "people", "person", "man", "woman", "group", "modern", "ancient",
    "historical", "history", "documentary", "dramatic", "lighting",
    "background", "foreground", "atmosphere", "atmospheric", "cinematic",
    "de", "del", "la", "el", "los", "las", "un", "una", "y", "o", "en",
    "con", "por", "para", "al", "se", "su", "que",
})

_PLACE_HINTS = (
    "city", "ruins", "temple", "pyramid", "tomb", "cave", "island", "river",
    "mountain", "desert", "forest", "ocean", "sea", "lake", "valley", "region",
)
_PERSON_HINTS = ("king", "queen", "pharaoh", "emperor", "scientist", "doctor",
                 "explorer", "president", "general", "artist", "writer")


@dataclass
class ArchiveQueryPlan:
    entity: str = ""
    location: str = ""
    person: str = ""
    era: str = ""
    queries: list[str] = field(default_factory=list)
    source_hints: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "entity": self.entity,
            "location": self.location,
            "person": self.person,
            "era": self.era,
            "queries": list(self.queries),
            "source_hints": dict(self.source_hints),
        }


def _clean(text: str) -> str:
    return " ".join(str(text or "").split()).strip(" ,.;:—-")


def _meaningful_tokens(text: str) -> list[str]:
    return [
        token for token in _TOKEN_RE.findall(str(text or ""))
        if token.lower() not in _GENERIC and len(token) > 1
    ]


def _entity_from_subject(spec: SceneSpec) -> str:
    extra = spec.extra or {}
    for key in ("entity", "historical_entity", "subject_entity", "topic"):
        value = _clean(str(extra.get(key) or ""))
        if value:
            return value
    subject = _clean(spec.subject)
    if not subject:
        return ""
    quoted = _QUOTED_RE.search(subject)
    if quoted:
        return _clean(quoted.group(1))
    tokens = _meaningful_tokens(subject)
    if not tokens:
        return ""
    # Prefer the head of the phrase, but keep proper-noun-ish tokens (digits,
    # capitals are lost in normalised specs, so position is the best signal).
    head = tokens[:4]
    return _clean(" ".join(head))


def _hint(extra: dict[str, Any], keys: tuple[str, ...]) -> str:
    for key in keys:
        value = _clean(str(extra.get(key) or ""))
        if value:
            return value
    return ""


def build_archive_query_plan(spec: SceneSpec, *, max_queries: int = 4) -> ArchiveQueryPlan:
    extra = dict(spec.extra or {})
    entity = _entity_from_subject(spec)
    location = _hint(extra, ("location", "place", "site", "city", "region"))
    person = _hint(extra, ("person", "historical_figure", "figure"))
    era = _clean(spec.era if str(spec.era).lower() not in ("atemporal", "present", "presente", "timeless") else "")
    environment = _clean(spec.environment)

    if not location and entity:
        tokens = {token.lower() for token in _TOKEN_RE.findall(entity)}
        if tokens & set(_PLACE_HINTS):
            location = entity
    if not person and entity:
        tokens = {token.lower() for token in _TOKEN_RE.findall(entity)}
        if tokens & set(_PERSON_HINTS):
            person = entity

    subject = person or entity
    candidates: list[str] = []
    if subject and era:
        candidates.append(f"{subject} {era}")
    if subject and location and location.lower() != subject.lower():
        candidates.append(f"{subject} {location}")
    if subject and environment:
        candidates.append(f"{subject} {environment}")
    if subject:
        candidates.append(subject)
    if entity and entity.lower() != subject.lower() and era:
        candidates.append(f"{entity} {era}")

    # NASA benefits from mission/object phrasing; keep it deterministic.
    if str((spec.extra or {}).get("archive_source") or "").lower() == "nasa":
        if subject:
            candidates.append(f"{subject} NASA")

    queries: list[str] = []
    seen: set[str] = set()
    for query in candidates:
        query = _clean(query)[:120]
        key = query.lower()
        if not query or key in seen:
            continue
        seen.add(key)
        queries.append(query)
        if len(queries) >= max_queries:
            break
    return ArchiveQueryPlan(
        entity=entity,
        location=location,
        person=person,
        era=era,
        queries=queries,
        source_hints={
            k: v for k, v in extra.items()
            if k in ("entity", "location", "person", "wikidata_id", "archive_source")
        },
    )


def archive_queries(spec: SceneSpec, *, max_queries: int = 4) -> list[str]:
    return build_archive_query_plan(spec, max_queries=max_queries).queries
