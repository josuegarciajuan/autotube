"""Era → stock-search-term mapping and anachronism detection.

Deterministic helpers (NO LLM) that anchor stock-media search queries to
the correct historical period and let the pipeline reject anachronistic
stock candidates (modern footage shown while narrating, e.g., a 1611
mutiny).

Typical flow::

    from pipeline.era_terms import era_anchor, normalize_era, ANACHRONISM_WORDS

    phrase = era_anchor("17th century", "siglo_XVII")  # "17th century wooden sailing ship"
    phrase = era_anchor("", "presente")                 # None (timeless)
    norm   = normalize_era("siglo_XIII")                # "medieval"
"""

from __future__ import annotations

import re

# ── Century helpers ───────────────────────────────────────────────────

_ORDINAL_SUFFIX = {1: "st", 2: "nd", 3: "rd"}
_ORDINAL_WORDS = {
    "first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5,
    "sixth": 6, "seventh": 7, "eighth": 8, "ninth": 9, "tenth": 10,
    "eleventh": 11, "twelfth": 12, "thirteenth": 13, "fourteenth": 14,
    "fifteenth": 15, "sixteenth": 16, "seventeenth": 17, "eighteenth": 18,
    "nineteenth": 19, "twentieth": 20,
}
_ROMAN = {
    "i": 1, "v": 5, "x": 10, "l": 50, "c": 100, "d": 500, "m": 1000,
}


def _ordinal_suffix(n: int) -> str:
    if 11 <= (n % 100) <= 13:
        return "th"
    return _ORDINAL_SUFFIX.get(n % 10, "th")


def _century_phrase(n: int) -> str:
    """Canonical English century key, e.g. 17 → ``'17th century'``."""
    return f"{n}{_ordinal_suffix(n)} century"


def _roman_to_int(text: str) -> int | None:
    """Convert a roman numeral (i..xx and beyond) to an int, or None."""
    total = 0
    prev = 0
    for ch in reversed(text.strip().lower()):
        v = _ROMAN.get(ch)
        if v is None:
            return None
        if v < prev:
            total -= v
        else:
            total += v
        prev = v
    return total or None


def _century_stock_phrase(n: int) -> str | None:
    """Stock anchor phrase for a century number (bucketed).

    Returns None for modern centuries (20th+) — those are treated as
    non-historical for anachronism purposes.
    """
    if n <= 4:
        return "ancient historical"
    if n <= 15:
        return "medieval historical"
    if n == 16:
        return "16th century historical"
    if n == 17:
        return "17th century wooden sailing ship"
    if n == 18:
        return "18th century historical"
    if n == 19:
        return "19th century historical"
    return None  # 20th century and beyond → modern, no historical anchor


# ── Explicit era → stock phrase dictionary ────────────────────────────

ERA_STOCK_TERMS: dict[str, str] = {
    # Medieval (also covered by century buckets 5–15)
    "medieval": "medieval historical",
    "edad media": "medieval historical",
    "feudal": "medieval historical",
    # Early modern / age of sail
    "17th century": "17th century wooden sailing ship",
    "seventeenth": "17th century wooden sailing ship",
    "16th century": "16th century wooden caravel sailing vessel",
    "sixteenth": "16th century wooden caravel sailing vessel",
    "18th century": "18th century historical",
    "eighteenth": "18th century historical",
    "19th century": "19th century historical",
    "nineteenth": "19th century historical",
    "victorian": "19th century historical",
    # Ancient world
    "ancient": "ancient historical",
    "antigua": "ancient historical",
    "antiguedad": "ancient historical",
    "antigüedad": "ancient historical",
    "roma": "ancient historical",
    "romano": "ancient historical",
    "imperio romano": "ancient historical",
    "egipto": "ancient historical",
    "egipcio": "ancient historical",
    "grecia": "ancient historical",
    "griego": "ancient historical",
    "prehistoria": "ancient historical",
    "prehistoric": "ancient historical",
    # Decades → vintage retro anchor (per decade)
    "1920s": "1920s vintage retro",
    "1930s": "1930s vintage retro",
    "1940s": "1940s vintage retro",
    "1950s": "1950s vintage retro",
    "1960s": "1960s vintage retro",
    "1970s": "1970s vintage retro",
    "1980s": "1980s vintage retro",
    "1990s": "1990s vintage retro",
}

# Spanish decade words → decade key
_DECADE_WORDS: dict[str, str] = {
    "veinte": "1920s", "treinta": "1930s", "cuarenta": "1940s",
    "cincuenta": "1950s", "sesenta": "1960s", "setenta": "1970s",
    "ochenta": "1980s", "noventa": "1990s",
}

# Era values that are timeless / present / future → no anchor needed
_TIMELESS_MARKERS = (
    "atemporal", "presente", "actualidad", "actual", "hoy",
    "futuro", "moderno", "contemporaneo", "contemporáneo",
)

# ── Normalization ─────────────────────────────────────────────────────

_CENTURY_RE = re.compile(r"siglo\s+([ivxlcdm]+|\d{1,2})")
_CENTURY_EN_RE = re.compile(r"(\d{1,2})(?:st|nd|rd|th)\s*century")
_CENTURY_WORD_RE = re.compile(r"([a-z]+)\s*century")
_DECADE_NUM_RE = re.compile(r"\b(\d{4})s\b")
_DECADE_SHORT_RE = re.compile(r"\b(\d{2})s\b")
_ANOS_RE = re.compile(r"años?\s+(\d{1,2})")
_YEAR_RE = re.compile(r"(\d{3,4})")


def normalize_era(text: str) -> str:
    """Normalize a raw era string to a canonical key.

    Examples:
        ``"siglo_xvii"``        → ``"17th century"``
        ``"1611"``              → ``"17th century"``
        ``"los años ochenta"``  → ``"1980s"``
        ``"años 80"``           → ``"1980s"``
        ``"siglo_XIII"``        → ``"medieval"``
        ``"17th century"``      → unchanged
        ``"presente"``          → unchanged

    Unrecognized input is returned unchanged (never empty-drops real data).
    """
    if not text:
        return ""
    t = text.strip().lower().replace("_", " ").replace("  ", " ").strip()
    if not t:
        return ""

    # 1. Spanish decade words → "1980s"
    for word, decade in _DECADE_WORDS.items():
        if word in t:
            return decade

    # 2. "siglo xvii" / "siglo XVII" / "siglo 17"
    m = _CENTURY_RE.search(t)
    if m:
        tok = m.group(1)
        n = _roman_to_int(tok) if tok.isalpha() else int(tok)
        if n and 1 <= n <= 21:
            return _century_phrase(n)

    # 3. "17th century" / "18th century"
    m = _CENTURY_EN_RE.fullmatch(t)
    if m:
        return _century_phrase(int(m.group(1)))

    # 4. "seventeenth century"
    m = _CENTURY_WORD_RE.fullmatch(t)
    if m:
        n = _ORDINAL_WORDS.get(m.group(1))
        if n:
            return _century_phrase(n)

    # 5. Decades: "1980s", "80s", "años 80"
    m = _DECADE_NUM_RE.search(t)
    if m:
        return f"{m.group(1)}s"
    m = _DECADE_SHORT_RE.search(t)
    if m:
        yy = int(m.group(1))
        return f"19{yy:02d}s" if yy >= 20 else f"20{yy:02d}s"
    m = _ANOS_RE.search(t)
    if m:
        yy = int(m.group(1))
        decade = (yy // 10) * 10
        return f"19{decade:02d}s" if decade >= 20 else f"20{decade:02d}s"

    # 6. Bare year → century ("1611" → "17th century")
    m = _YEAR_RE.fullmatch(t)
    if m:
        year = int(m.group(1))
        return _century_phrase((year - 1) // 100 + 1)

    return t


def _is_timeless(text: str) -> bool:
    """True if the (normalized) era refers to present/future/timeless."""
    return any(marker in text for marker in _TIMELESS_MARKERS)


# ── Anchor resolution ─────────────────────────────────────────────────

def era_anchor(era_decade: str, era: str) -> str | None:
    """Return the stock-search anchor phrase for an era.

    ``era_decade`` takes priority over ``era`` (it is the normalized field).

    Returns:
        - The stock phrase (e.g. ``"17th century wooden sailing ship"``)
          when the era is a recognizable historical period.
        - ``None`` when the scene is timeless / present / future or the
          era is not recognized (caller keeps current behavior).
    """
    raw = (era_decade or era or "").strip()
    if not raw:
        return None

    norm = normalize_era(raw)
    if not norm:
        return None

    # Direct dictionary hit (medieval / ancient / decades / 17th century...)
    phrase = ERA_STOCK_TERMS.get(norm)
    if phrase:
        return phrase

    # "<n>th century" pattern → century bucket (may return None for modern)
    m = _CENTURY_EN_RE.fullmatch(norm)
    if m:
        return _century_stock_phrase(int(m.group(1)))

    if _is_timeless(norm):
        return None

    return None


# ── Anachronism vocabulary ────────────────────────────────────────────

ANACHRONISM_WORDS: set[str] = {
    # Modern urban / infrastructure
    "city", "cities", "urban", "canal", "canals", "skyline", "skyscraper",
    "skyscrapers", "building", "buildings", "downtown", "nightclub", "neon",
    "highway", "highways", "freeway", "subway", "metro", "airport",
    "harbor city", "harbour city", "traffic", "traffic jam",
    # Modern vehicles
    "car", "cars", "automobile", "automobiles", "truck", "trucks", "bus",
    "buses", "taxi", "taxis", "motorcycle", "motorcycles", "speedboat",
    "speedboats", "yacht", "yachts", "container", "containers", "bulldozer",
    # Modern technology / electronics
    "drone", "drones", "smartphone", "smartphones", "laptop", "laptops",
    "computer", "computers", "television", "tv", "screen", "screens",
    "digital", "technology", "electric", "electrical", "led",
    "cellphone", "cellphones", "internet", "wifi",
    # Modern industry / materials
    "concrete", "factory", "factories", "industrial", "parking",
    # Modern lifestyle
    "modern", "contemporary", "fashion", "selfie",
}

# Precomputed word-boundary-safe entries (single words checked with \b,
# phrases checked as plain substrings).
_ANACHRONISM_PHRASES = tuple(sorted(p for p in ANACHRONISM_WORDS if " " in p))
_ANACHRONISM_WORDS = tuple(sorted(w for w in ANACHRONISM_WORDS if " " not in w))


def anachronism_hits(text: str) -> list[str]:
    """Return the subset of :data:`ANACHRONISM_WORDS` found in *text*.

    Single words use word-boundary matching (so ``"car"`` does not match
    ``"cart"``); multi-word phrases use plain substring matching.
    """
    if not text:
        return []
    t = text.lower()
    hits: list[str] = []
    for phrase in _ANACHRONISM_PHRASES:
        if phrase in t:
            hits.append(phrase)
    for word in _ANACHRONISM_WORDS:
        if re.search(r"\b" + re.escape(word) + r"\b", t):
            hits.append(word)
    return hits
