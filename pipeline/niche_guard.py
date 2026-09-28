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
    anchors = _anchors(cfg)
    anchor_hits = {a for a in anchors if a in tokens or a in norm}
    if anchor_hits:
        return min(1.0, 0.6 + 0.1 * min(len(anchor_hits) - 1, 4))
    vocab = _channel_vocabulary(cfg)
    if vocab:
        overlap = len(tokens & vocab) / max(1, min(len(tokens), 6))
        if overlap > 0:
            return min(0.55, 0.2 + overlap)
    return 0.0


def is_on_niche(text: str, cfg) -> bool:
    """True unless the text clearly lacks any niche anchor/vocabulary."""
    if not is_guard_enabled(cfg):
        return True
    threshold = float(getattr(cfg, "TITLE_NICHE_FIT_MIN", 0.3) or 0.3)
    try:
        return niche_fit_score(text, cfg) >= threshold
    except Exception as exc:  # noqa: BLE001 — fail-open
        logger.debug("niche fit error (fail-open): %s", exc)
        return True


def filter_on_niche(items: list, text_getter, cfg) -> tuple[list, int]:
    """Drop off-niche items only when at least one on-niche item remains.

    Returns ``(kept, dropped_count)``. Never starves the caller: if nothing is
    on-niche, the original list is returned unchanged.
    """
    if not is_guard_enabled(cfg) or not items:
        return list(items), 0
    on_niche = [it for it in items if is_on_niche(text_getter(it), cfg)]
    if not on_niche:
        return list(items), 0
    return on_niche, len(items) - len(on_niche)
