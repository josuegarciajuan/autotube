"""Shared title token vocabulary — single source of truth.

This module is intentionally dependency-free (stdlib only) so it can be
imported by both ``pipeline.content_safety`` and
``api.services.packaging_policy`` without creating circular imports.

It centralises three things that previously lived in divergent lists:

* :data:`BANNED_TOKENS` — clickbait markers (``maldit``, ``increible``,
  ``imposible`` ...) plus the grammatically broken injected tails produced by
  the old ``title_enricher`` safety net (``"la verdad ocultado"``,
  ``"real para siempre"`` ...).
* :data:`CONNECTOR_TAIL_WORDS` — function words that must never close a title.
* :data:`KNOWN_ACRONYMS` — all-caps tokens that are legitimate and should not
  be counted as excessive capitalisation.

All comparisons are accent-insensitive and case-insensitive.
"""

from __future__ import annotations

import re

# ── Normalisation ────────────────────────────────────────────────────
# Lowercase first, then strip accents/diacritics (including ñ → n) so that
# "Increíble" == "increible" and "extraño" == "extrano".
_ACCENT_MAP = str.maketrans({
    "á": "a", "à": "a", "ä": "a", "â": "a", "ã": "a",
    "é": "e", "è": "e", "ë": "e", "ê": "e",
    "í": "i", "ì": "i", "ï": "i", "î": "i",
    "ó": "o", "ò": "o", "ö": "o", "ô": "o", "õ": "o",
    "ú": "u", "ù": "u", "ü": "u", "û": "u",
    "ñ": "n", "ç": "c",
})


def normalize(text: str) -> str:
    """Return *text* lowercased and accent-stripped (``ñ`` → ``n``)."""
    if not text:
        return ""
    return str(text).lower().translate(_ACCENT_MAP)


# ── Banned tokens ────────────────────────────────────────────────────
# Values are stored NORMALISED. Do not add "real", "oculto", "secreto" or
# "universo": they are legitimate words in narrative contexts; only the exact
# broken/clickbait patterns below are banned.
BANNED_TOKENS: frozenset[str] = frozenset(normalize(t) for t in (
    # High-risk clickbait markers (over-represented in removed videos).
    "maldit",
    "que nadie te conto",
    "increible",
    "imposible",
    # Injected, grammatically broken tails from the old enricher.
    "la verdad ocultado",
    "el secreto primera",
    "real para siempre",
    "extrano para siempre",
    "el secreto remision",
    "historia grabado",
    "caso archivado",
    "historia enterrado",
    "historia filtrado",
    "sumergido al fin",
))

# Word-boundary prefix matcher for single tokens (so "maldit" catches
# "maldito"/"maldita" and "imposible" catches "imposibles"). Multi-word
# tokens are matched as plain substrings.
_SINGLE_TOKEN_RES: dict[str, re.Pattern[str]] = {
    token: re.compile(r"(?<![a-z0-9])" + re.escape(token))
    for token in BANNED_TOKENS
    if " " not in token
}


def contains_banned_token(text: str) -> bool:
    """True if *text* contains any :data:`BANNED_TOKENS` entry.

    Multi-word tokens match as substrings; single tokens match at a word
    start boundary (prefix) so inflections are caught.
    """
    norm = normalize(text)
    if not norm:
        return False
    for token in BANNED_TOKENS:
        if " " in token:
            if token in norm:
                return True
        elif _SINGLE_TOKEN_RES[token].search(norm):
            return True
    return False


# ── Dangling connectors ──────────────────────────────────────────────
# Function words that cannot grammatically end a title.
CONNECTOR_TAIL_WORDS: frozenset[str] = frozenset(normalize(w) for w in (
    "de", "del", "la", "el", "los", "las", "un", "una", "unos", "unas",
    "que", "y", "o", "a", "en", "con", "por", "para", "sin", "sobre",
    "entre", "desde", "hacia", "su", "sus", "mi", "tu", "al", "se", "lo",
    "ni", "pero", "porque", "cuando", "donde", "si", "no", "mas", "muy",
    "tan", "como", "anda", "the", "of", "to", "in", "and", "or",
))

# Trailing punctuation that signals an obviously cut/injected title.
_DANGLING_PUNCT = ("—", "–", "-", "|", "(", "[", ",", ":", "«", '"', "'", "/")
# Punctuation that is a valid sentence terminator (stripped before the
# connector-word check).
_SENTENCE_END = " .,;:!?…·•«»\"'()[]"


def has_dangling_tail(text: str) -> bool:
    """True if *text* ends mid-phrase.

    Triggers when the raw text ends in dangling punctuation (``— | ( [ , : «``)
    or when its last word (after stripping sentence terminators) is a
    connector/function word.
    """
    raw = (text or "").strip()
    if not raw:
        return False
    if raw.endswith(_DANGLING_PUNCT):
        return True
    core = raw.rstrip(_SENTENCE_END).strip()
    if not core:
        return True
    last_word = core.split()[-1]
    last_norm = normalize(last_word).strip("«»\"'()[]")
    return last_norm in CONNECTOR_TAIL_WORDS


# ── Capitalisation helpers ───────────────────────────────────────────
# All-caps tokens that are legitimate (acronyms / brands) and must not be
# counted as "excessive caps" nor force-penalised.
KNOWN_ACRONYMS: frozenset[str] = frozenset({
    "ONU", "OTAN", "EEUU", "ADN", "OVNI", "NASA", "CIA", "FBI", "RTVE",
    "UE", "USA", "VIH", "SIDA", "ISBN", "PIB", "BBC", "CNN", "OMS",
    "ONG", "URSS", "UNESCO", "DNI", "IVA", "GPS", "PDF", "IA", "TVE",
    "FIFA", "UEFA", "ADN", "III", "IV", "XX",
})

_UPPER_WORD_RE = re.compile(r"\b[A-ZÁÉÍÓÚÜÑ0-9]{2,}\b")


def uppercase_words(text: str) -> list[str]:
    """Return fully-uppercase alphabetic words (length >= 2) in *text*.

    Legitimate acronyms (:data:`KNOWN_ACRONYMS`) and tokens containing digits
    are excluded.
    """
    out: list[str] = []
    for token in _UPPER_WORD_RE.findall(text or ""):
        letters = [c for c in token if c.isalpha()]
        if len(letters) < 2:
            continue
        if token.upper() in KNOWN_ACRONYMS:
            continue
        if any(c.isdigit() for c in token):
            continue
        out.append(token)
    return out


def is_all_caps(text: str) -> bool:
    """True when the whole text is uppercase (at least one cased letter)."""
    stripped = (text or "").strip()
    if not stripped:
        return False
    return stripped == stripped.upper() and any(c.isalpha() for c in stripped)


# ── Clickbait credibility suffix ─────────────────────────────────────
# A credibility label glued to the end behind a separator/parenthesis:
# "… — Real", "… (Caso real)", "… | Revelación". A plain trailing "real"
# as a normal adjective ("el mundo real") is NOT matched because the label
# must be preceded by a separator or an opening bracket.
_CLICKBAIT_SUFFIX_RE = re.compile(
    r"(?:[|–—\-]\s*|\()\s*(?:real|caso\s+real|revelaci[oó]n|impactante|"
    r"expediente|archivos?\s+cia|clasificado)\s*\)?\s*$",
    re.IGNORECASE,
)


def has_clickbait_suffix(text: str) -> bool:
    """True if *text* ends with a credibility clickbait label.

    Matches labels glued behind a separator/parenthesis ("— Real",
    "(Caso real)", "| Revelación") without flagging a legitimate trailing
    adjective such as "...el mundo real".
    """
    return bool(_CLICKBAIT_SUFFIX_RE.search((text or "").strip()))


def has_unbalanced_punctuation(text: str) -> bool:
    """True when an opening question/exclamation mark is never closed.

    Catches truncated questions such as ``"¿quién. ESTREMECEDOR"``. Only the
    opening-without-closing direction is flagged: a closing mark without its
    Spanish opening mark (common in LLM output) is tolerated.
    """
    t = text or ""
    if "¿" in t and "?" not in t:
        return True
    if "¡" in t and "!" not in t:
        return True
    return False
