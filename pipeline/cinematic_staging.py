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

# English action verbs/gerunds commonly found in stock-film queries.  Public
# so query simplification, the fallback-tier classifier and the candidate
# scorer all share ONE vocabulary (Fase 4a).
ACTION_VERBS_EN: frozenset[str] = frozenset({
    "walk", "walks", "walking", "run", "runs", "running", "cross", "crosses",
    "crossing", "flee", "flees", "fleeing", "fight", "fights", "fighting",
    "discover", "discovers", "discovering", "find", "finds", "finding",
    "build", "builds", "building", "construct", "constructs", "constructing",
    "dig", "digs", "digging", "excavate", "excavates", "excavating",
    "sail", "sails", "sailing", "navigate", "navigates", "navigating",
    "write", "writes", "writing", "read", "reads", "reading", "steal",
    "steals", "stealing", "attack", "attacks", "attacking", "defend",
    "defends", "defending", "escape", "escapes", "escaping", "enter",
    "enters", "entering", "leave", "leaves", "leaving", "climb", "climbs",
    "climbing", "open", "opens", "opening", "close", "closes", "closing",
    "burn", "burns", "burning", "search", "searches", "searching",
    "observe", "observes", "observing", "carry", "carries", "carrying",
    "shoot", "shoots", "shooting", "sign", "signs", "signing", "paint",
    "paints", "painting", "work", "works", "working", "row", "rows",
    "rowing", "ride", "rides", "riding", "pray", "prays", "praying",
    "trade", "trades", "trading", "march", "marches", "marching",
    "examine", "examines", "examining", "study", "studies", "studying",
    "measure", "measures", "measuring", "lift", "lifts", "lifting", "pull",
    "pulls", "pulling", "push", "pushes", "pushing", "load", "loads",
    "loading", "repair", "repairs", "repairing", "plant", "plants",
    "planting", "harvest", "harvests", "harvesting", "float", "floats",
    "floating",
})


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


# Narration action → observable, camera-ready English staging.  Conservative:
# only used when an explicit action verb is present; never invents facts.
_ACTION_STAGING: tuple[tuple[str, str], ...] = (
    (r"\b(cross|crosses|crossing)\b", "traveler crossing open terrain"),
    (r"\b(walk|walks|walking|march|marches|marching)\b", "person walking along a path"),
    (r"\b(enter|enters|entering)\b", "person entering through a doorway"),
    (r"\b(open|opens|opening)\b", "hands opening an old book"),
    (r"\b(close|closes|closing)\b", "hands closing a heavy door"),
    (r"\b(build|builds|building|construct|constructs|constructing)\b", "workers constructing a stone structure"),
    (r"\b(dig|digs|digging|excavate|excavates|excavating)\b", "archaeologist excavating with hand tools"),
    (r"\b(sail|sails|sailing|navigate|navigates|navigating)\b", "sailors navigating a wooden vessel"),
    (r"\b(fight|fights|fighting)\b", "people fighting in close combat"),
    (r"\b(work|works|working)\b", "worker engaged in manual labor"),
    (r"\b(discover|discovers|discovering|find|finds|finding)\b", "explorer discovering an ancient artifact"),
    (r"\b(write|writes|writing)\b", "person writing at a desk"),
    (r"\b(read|reads|reading)\b", "person reading an old manuscript"),
    (r"\b(examine|examines|examining|study|studies|studying)\b", "researcher examining documents"),
    (r"\b(climb|climbs|climbing)\b", "climber ascending a rocky slope"),
    (r"\b(carry|carries|carrying)\b", "person carrying supplies"),
    (r"\b(search|searches|searching)\b", "person searching through old records"),
    (r"\b(observe|observes|observing)\b", "observer watching the horizon"),
    (r"\b(float|floats|floating|row|rows|rowing)\b", "small boat floating on calm water"),
    (r"\b(trade|trades|trading)\b", "merchants trading goods at a market"),
    (r"\b(pray|prays|praying)\b", "person praying in a dim interior"),
)


def _staged_action_phrase(source: str) -> str:
    """First matching observable staging phrase for a narration, or ``""``."""
    for pattern, phrase in _ACTION_STAGING:
        if re.search(pattern, source):
            return phrase
    return ""


def _candidate_metadata(candidate: dict) -> str:
    """Join all available candidate metadata into lowercase text."""
    try:
        tags = candidate.get("tags") or []
        if isinstance(tags, str):
            tags = [t.strip() for t in tags.split(",")]
        return " ".join([
            str(candidate.get("title") or ""),
            str(candidate.get("description") or ""),
            " ".join(str(t) for t in tags),
            str(candidate.get("page_url") or ""),
        ]).lower()
    except Exception:
        return ""


def _words(text: str, min_len: int = 3) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", (text or "").lower()) if len(w) >= min_len}


def _context_action_words(scene_context: Any) -> set[str]:
    if scene_context is None:
        return set()
    return _words(getattr(scene_context, "action", "") or "", min_len=3)


def scene_action_words(scene: Any) -> set[str]:
    """Action words present in a raw scene dict or a ``SceneVisualContext``.

    Fase 4b: ``_classify_scenes`` only sees raw scene dicts, so this helper
    exposes the Fase 4a action vocabulary without forcing callers to build a
    full context object.  Accepts either a dict (``action`` / ``fragment_text``
    / ``texto`` / ``search_query_en``) or an object with ``action``/``fragment``.
    Never raises.
    """
    try:
        if scene is None:
            return set()
        if isinstance(scene, dict):
            text = _text(
                scene.get("action"),
                scene.get("fragment_text"),
                scene.get("texto"),
                scene.get("search_query_en"),
            )
        else:
            text = _text(
                getattr(scene, "action", ""),
                getattr(scene, "fragment", ""),
            )
        return _words(text, min_len=3) & ACTION_VERBS_EN
    except Exception:
        return set()


def scene_has_action(scene: Any) -> bool:
    """True when the scene narrates a concrete, camera-observable action.

    Combines the shared ``ACTION_VERBS_EN`` vocabulary with the conservative
    staging phrases from ``_ACTION_STAGING``.  Fail-open (False on error).
    """
    try:
        if scene_action_words(scene):
            return True
        if isinstance(scene, dict):
            source = _text(
                scene.get("fragment_text"),
                scene.get("texto"),
                scene.get("search_query_en"),
            )
        else:
            source = _text(getattr(scene, "fragment", ""), getattr(scene, "action", ""))
        return bool(_staged_action_phrase(source))
    except Exception:
        return False


def _scene_is_historical(theme_ctx: Any, scene_context: Any = None) -> bool:
    """Historical check that prefers the per-scene era (Fase 3 exemptions)."""
    if scene_context is not None:
        era = getattr(scene_context, "era", "") or ""
        if era:
            try:
                return era_anchor("", era) is not None
            except Exception:
                return False
    return _historical(theme_ctx)


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

    # Fase 4a: translate an explicit narrated action into an observable shot.
    staged_action = _staged_action_phrase(source)
    if staged_action:
        if _historical(theme_ctx):
            try:
                era_phrase = era_anchor(
                    getattr(theme_ctx, "era_decade", ""),
                    getattr(theme_ctx, "era", ""),
                )
            except Exception:
                era_phrase = None
            if era_phrase and era_phrase.lower() not in staged_action.lower():
                staged_action = f"{staged_action}, {era_phrase}"
        parts.append(staged_action)

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


# ── Fase 4a: unified, action/context-aware candidate scoring ─────────

_ACTION_WEIGHT = 3.0
_CONTEXT_WEIGHT = 1.5
_NARRATIVE_WEIGHT = 1.0
_MUST_AVOID_PENALTY = 6.0
_PERIOD_BONUS = 1.0
_PERIOD_TERMS = ("wooden", "caravel", "sailing", "historical", "ancient")

# Canonical fallback ladder, most specific → most generic.  A caller must
# preserve this order and must NOT re-sort afterwards.
FALLBACK_LADDER: tuple[str, ...] = (
    "action_exact",
    "action_compatible",
    "context",
    "symbolic",
)
# Tiers considered "generic fallback" for max_generic_fallback_pct accounting.
GENERIC_TIERS: frozenset[str] = frozenset({"context", "symbolic"})


def score_candidate(
    candidate: dict,
    scene_text: str = "",
    theme_ctx: Any = None,
    scene_context: Any = None,
    action_scene_boost: bool = True,
) -> float:
    """Single deterministic relevance score (Fase 4a).

    Combines, in priority order:
      - **action** match (weight 3.0/word) — narrated action dominates generic
        label coincidence;
      - **context** match (subject/object/setting, 1.5/word);
      - **narrative** overlap (fragment/brief/scene_text, 1.0/word);
      - **period** bonus for period-anchored metadata;
      - **must_avoid** penalty (−6.0/term) so forbidden elements never win.

    Candidates without metadata score 0.0 (caller treats that as "unknown",
    never as "bad").
    """
    metadata = _candidate_metadata(candidate)
    if not metadata:
        return 0.0
    score = 0.0

    if action_scene_boost:
        action_words = _context_action_words(scene_context)
        if action_words:
            score += _ACTION_WEIGHT * sum(
                1 for w in action_words
                if re.search(r"\b" + re.escape(w) + r"\b", metadata)
            )

    if scene_context is not None:
        ctx_words: set[str] = set()
        for field in ("subject", "object", "setting"):
            ctx_words |= _words(getattr(scene_context, field, "") or "", 3)
        if ctx_words:
            score += _CONTEXT_WEIGHT * sum(
                1 for w in ctx_words
                if re.search(r"\b" + re.escape(w) + r"\b", metadata)
            )

    narrative_parts = [scene_text]
    if scene_context is not None:
        narrative_parts += [
            getattr(scene_context, "fragment", "") or "",
            getattr(scene_context, "visual_concept", "") or "",
            getattr(scene_context, "bridge_from_prev", "") or "",
            getattr(scene_context, "central_entity", "") or "",
            *list(getattr(scene_context, "recurring_elements", []) or []),
        ]
    narrative_words: set[str] = set()
    for part in narrative_parts:
        narrative_words |= _words(part, 3)
    if narrative_words:
        score += _NARRATIVE_WEIGHT * sum(
            1 for w in narrative_words
            if re.search(r"\b" + re.escape(w) + r"\b", metadata)
        )

    if any(term in metadata for term in _PERIOD_TERMS):
        score += _PERIOD_BONUS

    if scene_context is not None:
        for term in list(getattr(scene_context, "must_avoid", []) or []):
            t = str(term or "").strip().lower()
            if not t:
                continue
            if t in metadata and (
                " " in t or re.search(r"\b" + re.escape(t) + r"\b", metadata)
            ):
                score -= _MUST_AVOID_PENALTY

    return float(score)


def candidate_matches_action(candidate: dict, scene_context: Any) -> bool:
    """True when the candidate depicts (any word of) the scene's action.

    Returns True when there is no action to match, so callers can use it as a
    non-blocking filter (fail-open).
    """
    action_words = _context_action_words(scene_context)
    if not action_words:
        return True
    metadata = _candidate_metadata(candidate)
    if not metadata:
        return False
    return any(
        re.search(r"\b" + re.escape(w) + r"\b", metadata) for w in action_words
    )


def rank_candidates(
    candidates: list[dict],
    scene_text: str,
    theme_ctx: Any = None,
    scene_context: Any = None,
    action_scene_boost: bool = True,
) -> list[dict]:
    """Drop historical anachronisms and rank candidates by the unified score.

    Fase 4a: ``scene_context.action`` dominates generic coincidence;
    ``subject``/``object``/``setting`` add context; ``must_avoid`` is a strong
    penalty.  Historical anachronisms are dropped unless the per-scene era is
    non-historical (Fase 3 deliberate time jumps).

    The returned list IS the final order — callers must not re-sort it.
    """
    historical = _scene_is_historical(theme_ctx, scene_context)
    ranked: list[tuple[float, int, dict]] = []
    for index, candidate in enumerate(candidates):
        try:
            metadata = _candidate_metadata(candidate)
            if historical and anachronism_hits(metadata):
                continue
            score = score_candidate(
                candidate, scene_text, theme_ctx, scene_context,
                action_scene_boost=action_scene_boost,
            )
        except Exception:
            score = 0.0
        ranked.append((score, -index, candidate))
    ranked.sort(reverse=True, key=lambda item: (item[0], item[1]))
    return [candidate for _, _, candidate in ranked]


# ── Fase 4a: explicit, bounded fallback ladder ────────────────────────

def build_fallback_ladder(tiers: Any, max_len: int = 100) -> list[str]:
    """Flatten ``{tier: [queries]}`` into the canonical ladder order.

    Guarantees ``action_exact → action_compatible → context → symbolic``,
    deduplicates preserving order and fits every query to ``max_len``. Unknown
    tier keys (defensive) are appended deterministically at the end.
    """
    ordered: list[str] = []
    seen: set[str] = set()

    def _emit(query: Any) -> None:
        q = fit_query(str(query or "").strip(), max_len)
        if q and q not in seen:
            seen.add(q)
            ordered.append(q)

    if not isinstance(tiers, dict):
        return ordered
    for tier in FALLBACK_LADDER:
        for query in tiers.get(tier, []) or []:
            _emit(query)
    for tier in sorted(k for k in tiers if k not in FALLBACK_LADDER):
        for query in tiers.get(tier, []) or []:
            _emit(query)
    return ordered


def is_generic_tier(tier: str) -> bool:
    """True when *tier* is a generic fallback (context or symbolic)."""
    return (tier or "") in GENERIC_TIERS


class GenericFallbackTracker:
    """Counts how many scenes land on a generic fallback tier.

    Advisory only: ``exceeds()`` drives a warning, never an abort.
    """

    def __init__(self, max_pct: float = 20.0) -> None:
        try:
            self.max_pct = max(0.0, min(100.0, float(max_pct)))
        except (TypeError, ValueError):
            self.max_pct = 20.0
        self.total = 0
        self.generic = 0

    def record(self, tier: str) -> None:
        self.total += 1
        if is_generic_tier(tier):
            self.generic += 1

    @property
    def generic_pct(self) -> float:
        if self.total <= 0:
            return 0.0
        return self.generic / self.total * 100.0

    def exceeds(self) -> bool:
        return self.total > 0 and self.generic_pct > self.max_pct


_GENERIC_QUERY_MARKERS: frozenset[str] = frozenset({
    "documentary", "establishing", "atmosphere", "cinematic", "historical",
    "ancient", "archival", "moody", "dramatic", "landscape",
})


def classify_query_tier(query: str, scene_context: Any = None) -> str:
    """Best-effort fallback-tier label for a query (accounting helper)."""
    words = _words(query, 2)
    action_words = _context_action_words(scene_context)
    if action_words and (action_words & words):
        return "action_exact"
    if words & ACTION_VERBS_EN:
        return "action_compatible"
    if words & _GENERIC_QUERY_MARKERS:
        return "context"
    return "symbolic"



def enrich_scene_query(query: str, theme_ctx: Any = None, scene_text: str = "") -> str:
    """Ground an LLM query while retaining the original narrative keywords."""
    brief = build_scene_brief(scene_text, query, theme_ctx)
    original = (query or scene_text or "").strip()
    if brief == original.lower() or not original:
        return brief
    return f"{original}, {brief}"
