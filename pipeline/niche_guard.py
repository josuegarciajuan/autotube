"""Channel niche guard (W4 packaging).

Off-niche long-forms (e.g. "Alejandro Magno" on a medical channel) poison the
audience signal: YouTube cannot build a coherent audience and browse/suggested
stays at zero. This module scores how well a candidate theme/title fits the
channel's declared niche.

Design constraints:

* **Data-driven**: anchors live in ``NICHE_ANCHORS`` (config/DB), never in code.
* **Fail-open**: no anchors configured → everything is on-niche; a scoring error
  never blocks generation.
* **No starvation**: callers must only drop off-niche items when at least one
  on-niche candidate remains.
"""

from __future__ import annotations

import logging
import re

from pipeline.title_tokens import normalize

logger = logging.getLogger("autotube.niche_guard")

_TOKEN_RE = re.compile(r"[^\W_]+", re.UNICODE)
NEUTRAL_FIT = 0.5

_STOPWORDS = frozenset(normalize(w) for w in (
    "el", "la", "los", "las", "un", "una", "unos", "unas", "de", "del",
    "que", "y", "o", "a", "en", "con", "por", "para", "sin", "sobre",
    "entre", "desde", "hacia", "su", "sus", "al", "se", "lo", "ni",
    "pero", "porque", "cuando", "donde", "si", "no", "mas", "muy", "tan",
    "como", "es", "fue", "son", "the", "of", "to", "in", "and", "or",
))


def _tokens(text: str) -> list[str]:
    return [normalize(t) for t in _TOKEN_RE.findall(text or "")]


def _significant(text: str) -> list[str]:
    out = []
    for tok in _tokens(text):
        if len(tok) < 3 or tok in _STOPWORDS:
            continue
        if tok not in out:
            out.append(tok)
    return out


def _anchors(cfg) -> set[str]:
    raw = getattr(cfg, "NICHE_ANCHORS", None) or []
    return {t for a in raw for t in _significant(str(a))}


def _channel_vocabulary(cfg) -> set[str]:
    vocab: set[str] = set()
    for attr in ("CHANNEL_KEYWORDS", "SEO_SECONDARY_KEYWORDS", "TITLE_GOOD_EXAMPLES"):
        for value in (getattr(cfg, attr, []) or []):
            vocab.update(_significant(str(value)))
    vocab.update(_significant(str(getattr(cfg, "SEO_PRIMARY_KEYWORD", "") or "")))
    return vocab


def _adjacent_terms(cfg) -> set[str]:
    out: set[str] = set()
    for value in (getattr(cfg, "NICHE_ADJACENT_TOPICS", []) or []):
        out.update(_significant(str(value)))
    return out


def _blocked_terms(cfg) -> set[str]:
    out: set[str] = set()
    for value in (getattr(cfg, "NICHE_BLOCKED_TOPICS", []) or []):
        out.update(_significant(str(value)))
    return out


def _token_matches(anchor: str, token: str) -> bool:
    """Prefix/fuzzy match so cerebro≈cerebral, sincron≈sincronía."""
    if not anchor or not token:
        return False
    if anchor == token or token.startswith(anchor) or anchor.startswith(token):
        return True
    # Conservative common-prefix match for inflections (>=5 chars).
    if len(anchor) >= 5 and len(token) >= 5:
        return anchor[:5] == token[:5]
    return False


def is_guard_enabled(cfg) -> bool:
    if not bool(getattr(cfg, "NICHE_GUARD_ENABLED", True)):
        return False
    return bool(_anchors(cfg))


def niche_fit_score(text: str, cfg) -> float:
    """Return 0..1 niche fit. 0 = clearly off-niche, 0.5 = neutral/fail-open."""
    if not is_guard_enabled(cfg):
        return NEUTRAL_FIT
    norm = normalize(text or "")
    tokens = set(_significant(text or ""))
    if not tokens:
        return NEUTRAL_FIT
    blocked = _blocked_terms(cfg)
    if blocked and any(_token_matches(b, t) for b in blocked for t in tokens):
        return 0.0
    anchors = _anchors(cfg)
    anchor_hits = {
        a for a in anchors
        if a in tokens or a in norm or any(_token_matches(a, t) for t in tokens)
    }
    if anchor_hits:
        return min(1.0, 0.6 + 0.1 * min(len(anchor_hits) - 1, 4))
    adjacent = _adjacent_terms(cfg)
    adjacent_hits = {
        a for a in adjacent
        if a in tokens or a in norm or any(_token_matches(a, t) for t in tokens)
    }
    if adjacent_hits:
        # Adjacent themes must land in the stretch band (below the core
        # on-niche threshold, above the exploration floor).
        return min(0.28, 0.22 + 0.02 * min(len(adjacent_hits), 3))
    vocab = _channel_vocabulary(cfg)
    if vocab:
        overlap = len(tokens & vocab) / max(1, min(len(tokens), 6))
        if overlap > 0:
            return min(0.55, 0.2 + overlap)
    return 0.0


def niche_decision(text: str, cfg) -> tuple[str, float]:
    """Return (decision, score) where decision is on_niche|stretch|off_niche."""
    score = niche_fit_score(text, cfg)
    on_threshold = float(getattr(cfg, "TITLE_NICHE_FIT_MIN", 0.3) or 0.3)
    stretch_threshold = float(getattr(cfg, "NICHE_STRETCH_MIN_SCORE", 0.20) or 0.20)
    is_soft = str(getattr(cfg, "NICHE_GUARD_MODE", "strict") or "strict").lower() == "soft"
    if score >= on_threshold:
        return "on_niche", score
    if is_soft and score >= stretch_threshold:
        return "stretch", score
    return "off_niche", score


def is_on_niche(text: str, cfg) -> bool:
    """True unless the text clearly lacks any niche anchor/vocabulary.

    In soft mode, adjacent themes above ``NICHE_STRETCH_MIN_SCORE`` are also
    kept here; the caller enforces the weekly exploration quota.
    """
    if not is_guard_enabled(cfg):
        return True
    decision, _ = niche_decision(text, cfg)
    return decision in ("on_niche", "stretch")


def filter_on_niche(items: list, text_getter, cfg,
                    strict: bool = False) -> tuple[list, int]:
    """Drop off-niche items only when at least one on-niche item remains.

    Returns ``(kept, dropped_count)``. Con ``strict=False`` nunca deja al
    llamador sin contenido (si no hay nada on-niche, devuelve la lista original).
    Con ``strict=True`` (F4) se prefiere **diferir** antes que publicar fuera de
    nicho: si nada encaja, devuelve ``([], len(items))``.
    """
    if not is_guard_enabled(cfg) or not items:
        return list(items), 0
    on_niche = [it for it in items if is_on_niche(text_getter(it), cfg)]
    if not on_niche:
        if strict:
            return [], len(items)
        return list(items), 0
    return on_niche, len(items) - len(on_niche)
