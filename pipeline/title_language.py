"""Cheap, dependency-free language guard for video titles.

Spanish channels occasionally received English titles (e.g. "Dreamt the
earthquake before it happened..."). This module detects the dominant language
with function-word heuristics and lets the title engine avoid/block non-Spanish
candidates without adding a new dependency.

Fail-open: short titles, proper nouns and ambiguous text return "unknown" and
are accepted.
"""
from __future__ import annotations

import re

_TOKEN_RE = re.compile(r"[a-záéíóúüñ]+", re.IGNORECASE)

_ES_STOPWORDS = {
    "el", "la", "los", "las", "un", "una", "unos", "unas", "de", "del",
    "que", "y", "en", "por", "para", "con", "sin", "sobre", "entre",
    "como", "cuando", "donde", "porque", "pero", "más", "su", "sus",
    "al", "se", "lo", "le", "les", "es", "son", "fue", "era", "ha",
    "han", "hay", "este", "esta", "esto", "estos", "estas", "aquel",
    "aquella", "muy", "ya", "no", "ni", "o", "si", "también", "tras",
}

_EN_STOPWORDS = {
    "the", "a", "an", "and", "or", "but", "of", "to", "in", "on", "at",
    "for", "with", "without", "from", "by", "as", "is", "are", "was",
    "were", "be", "been", "has", "have", "had", "this", "that", "these",
    "those", "it", "its", "he", "she", "they", "we", "you", "his", "her",
    "their", "not", "no", "what", "when", "where", "why", "how", "who",
    "before", "after", "into", "over", "under", "about", "which",
}

_ES_CHARS_RE = re.compile(r"[áéíóúüñ¿¡]", re.IGNORECASE)


def _get(cfg, key, default=None):
    if isinstance(cfg, dict):
        return cfg.get(key, default)
    return getattr(cfg, key, default)


def detect_language(text: str) -> str:
    """Return ``"es"``, ``"en"`` or ``"unknown"`` for *text*."""
    raw = str(text or "").strip()
    if not raw:
        return "unknown"
    tokens = [t.lower() for t in _TOKEN_RE.findall(raw)]
    if len(tokens) < 3:
        return "unknown"
    es = sum(1 for t in tokens if t in _ES_STOPWORDS)
    en = sum(1 for t in tokens if t in _EN_STOPWORDS)
    has_es_chars = bool(_ES_CHARS_RE.search(raw))
    if es == 0 and en == 0:
        # No function words: rely on Spanish-specific characters only.
        return "es" if has_es_chars else "unknown"
    if en >= 2 and en > es and not has_es_chars:
        return "en"
    if es >= en or has_es_chars:
        return "es"
    return "unknown"


def is_title_language_ok(text: str, cfg) -> bool:
    """True when the title language is allowed for the channel (fail-open)."""
    if not bool(_get(cfg, "TITLE_LANGUAGE_GUARD_ENABLED", True)):
        return True
    allowed = _get(cfg, "TITLE_ALLOWED_LANGUAGES", ("es",))
    if isinstance(allowed, str):
        allowed = [allowed]
    allowed = {str(a).lower() for a in (allowed or ["es"])}
    lang = detect_language(text)
    if lang == "unknown":
        return True
    return lang in allowed
