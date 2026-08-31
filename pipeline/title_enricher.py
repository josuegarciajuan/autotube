"""Title Power Word Enforcer — Mandatory title enrichment module.

Architecture (3 layers):
  1. Prompt injection: LLM receives channel power words + obligation to include >=1
  2. Deterministic safety net: if LLM failed, randomly inject one power word
  3. Analytics refiner: weekly regenerates power word lists from real data

The safety net uses multiple integration strategies (randomized) so titles don't
look formulaic. The LLM is always the primary mechanism — the safety net only
fires when the LLM-generated title has zero power words.

Integration points (everywhere a final title is produced):
  - metadata_generator.py::MetadataGenerator.generate()
  - metadata_optimizer.py::MetadataOptimizer.reoptimize()
  - viral_cloner.py::clone_title_description()
  - api/services/marketing_service.py::generate_title_options()
"""

from __future__ import annotations

import logging
import random
import re
from typing import Optional

logger = logging.getLogger(__name__)

# ── Integration strategies ──────────────────────────────────────────
# Each strategy is a (weight, formatter) pair.  Weights control
# probability distribution so some patterns don't dominate.
#
# Formatter receives the word (capitalized) and the original title,
# and MUST return a string <= max_chars.

_STRATEGIES: list[tuple[int, str, callable]] = []


def _strategy_suffix_pipe(title: str, word: str, max_chars: int) -> Optional[str]:
    """Strategy: append ' | Phrase'"""
    phrases = [
        f"El Secreto {word}",
        f"La Verdad {word}",
        f"Lo {word}",
        f"El Misterio {word}",
        f"La Historia {word}",
        f"{word} al Descubierto",
        f"El Enigma {word}",
        f"{word} para Siempre",
        f"El Caso {word}",
        f"El Hallazgo {word}",
    ]
    suffix = f" | {random.choice(phrases)}"
    return _fit(title, suffix, max_chars)


def _strategy_suffix_emdash(title: str, word: str, max_chars: int) -> Optional[str]:
    """Strategy: append ' — Phrase'"""
    phrases = [
        f"La Verdad {word}",
        f"El Secreto {word}",
        f"Lo que Era {word}",
        f"El Misterio {word}",
        f"{word} al Fin",
    ]
    suffix = f" — {random.choice(phrases)}"
    return _fit(title, suffix, max_chars)


def _strategy_parenthetical(title: str, word: str, max_chars: int) -> Optional[str]:
    """Strategy: append ' (WORD)' or ' (phrase with word)'"""
    variants = [
        f" ({word.upper()})",
        f" ({word})",
        f" (Caso {word})",
        f" (Historia {word})",
    ]
    suffix = random.choice(variants)
    return _fit(title, suffix, max_chars)


def _strategy_standalone(title: str, word: str, max_chars: int) -> Optional[str]:
    """Strategy: end with '. WORD.' as standalone emphasis"""
    suffix = f". {word.upper()}."
    return _fit(title, suffix, max_chars)


def _strategy_bracket(title: str, word: str, max_chars: int) -> Optional[str]:
    """Strategy: append ' [WORD]'"""
    suffix = f" [{word.upper()}]"
    return _fit(title, suffix, max_chars)


def _strategy_question(title: str, word: str, max_chars: int) -> Optional[str]:
    """Strategy: transform ending into question with power word"""
    phrases = [
        f" | ¿{word}?",
        f" — ¿{word}?",
        f" | ¿El Secreto {word}?",
        f" — ¿La Verdad {word}?",
    ]
    suffix = random.choice(phrases)
    return _fit(title, suffix, max_chars)


# ── Strategy registry ───────────────────────────────────────────────
# (weight, name, callable)

_STRATEGIES = [
    (30, "suffix_pipe",       _strategy_suffix_pipe),
    (20, "suffix_emdash",     _strategy_suffix_emdash),
    (15, "standalone",        _strategy_standalone),
    (15, "parenthetical",     _strategy_parenthetical),
    (10, "bracket",           _strategy_bracket),
    (10, "question",          _strategy_question),
]


def resolve_title_max_chars(config, default: int = 100) -> int:
    """Return the single configured title limit used by all title stages."""
    try:
        return max(1, int(getattr(config, "TITLE_MAX_CHARS", default)))
    except (TypeError, ValueError):
        return default

# ── Helpers ──────────────────────────────────────────────────────────

def _fit(base: str, suffix: str, max_chars: int) -> Optional[str]:
    """Try to fit base+suffix within max_chars. Truncate base if needed."""
    candidate = base.rstrip() + suffix
    if len(candidate) <= max_chars:
        return candidate
    # Truncate base to make room
    available = max_chars - len(suffix)
    if available < 15:
        return None  # can't fit meaningfully
    # Truncate at word boundary
    truncated = base[:available].rstrip()
    # Remove trailing partial word
    if " " in truncated:
        truncated = truncated[:truncated.rfind(" ")]
    result = truncated + suffix
    return result if len(result) <= max_chars else None


def _capitalize_word(word: str) -> str:
    """Capitalize first letter, leave rest as-is."""
    if not word:
        return word
    return word[0].upper() + word[1:]


def _word_boundary_match(text: str, word: str) -> bool:
    """Check if `word` appears as a whole-word boundary match in `text`.
    
    Case-insensitive. Matches at word boundaries (including
    punctuation boundaries for Spanish accented words).
    """
    pattern = r'(?<![a-záéíóúüñA-ZÁÉÍÓÚÜÑ])' + re.escape(word) + r'(?![a-záéíóúüñA-ZÁÉÍÓÚÜÑ])'
    return bool(re.search(pattern, text, re.IGNORECASE))


def _any_power_word_present(title: str, power_words: list[str]) -> bool:
    """Check if any power word appears in the title (word-boundary match)."""
    if not power_words:
        return True  # no words to enforce → nothing broken
    for word in power_words:
        if _word_boundary_match(title, word):
            return True
    return False


# ── Trailing parenthetical collapse (belt-and-suspenders) ────────────

_TRAILING_PAREN_RE = re.compile(r"(\s*\([^()]*\))\s*$")


def _paren_content(paren: str) -> str:
    return paren.strip(" ()（）[]").lower()


def _collapse_trailing_parentheticals(title: str) -> str:
    """Collapse repeated identical (case-insensitive) trailing parentheticals.

    E.g. "Título (Impactante) (IMPACTANTE)" → "Título (Impactante)".
    Leaves a single parenthetical — or two different ones — untouched.
    """
    while True:
        m = _TRAILING_PAREN_RE.search(title)
        if not m:
            break
        rest = title[:m.start()].rstrip()
        m2 = _TRAILING_PAREN_RE.search(rest)
        if m2 and _paren_content(m.group(1)) == _paren_content(m2.group(1)):
            title = rest
        else:
            break
    return title


# ── Public API ───────────────────────────────────────────────────────

def enforce_power_words(
    title: str,
    power_words: list[str],
    max_chars: int = 100,
    *,
    _rng: random.Random | None = None,
) -> str:
    """Ensure *title* contains at least one word from *power_words*.
    
    If a power word is already present (case-insensitive, word-boundary),
    the title is returned unchanged.
    
    Otherwise, a random power word is integrated using one of several
    strategies (chosen randomly by weight) to keep titles varied and
    natural-looking.
    
    Args:
        title: The original title (already trimmed/validated).
        power_words: List of power words from channel config.
        max_chars: Maximum allowed title length (default 100 per YouTube).
    
    Returns:
        The title, guaranteed to contain >=1 power word, <= max_chars.
    """
    if not title or not title.strip():
        logger.warning("title_enricher: empty title, cannot enrich")
        return title

    # Collapse duplicate trailing parentheticals (e.g. "(Impactante) (IMPACTANTE)")
    title = _collapse_trailing_parentheticals(title)

    if not power_words:
        logger.debug("title_enricher: no power_words list, skipping")
        return title[:max_chars]
    
    # Already has a power word? Nothing to do.
    if _any_power_word_present(title, power_words):
        logger.debug("title_enricher: power word already present in title")
        return title[:max_chars]
    
    # ── Select a power word ──
    rng = _rng or random
    word = rng.choice(power_words)
    capped = _capitalize_word(word)
    
    # ── Try strategies in weighted-random order ──
    strategies = list(_STRATEGIES)
    rng.shuffle(strategies)
    
    # Weighted selection: build weighted pool
    weighted = []
    for weight, name, func in strategies:
        weighted.extend([(name, func)] * weight)
    rng.shuffle(weighted)
    
    for name, func in weighted:
        result = func(title, capped, max_chars)
        if result is not None:
            logger.info(
                "title_enricher: injected power_word='%s' via strategy='%s' → %d chars",
                word, name, len(result),
            )
            return result
    
    # ── Ultimate fallback: brute-force truncation + suffix ──
    fallback_suffix = f" | {capped}"
    if len(fallback_suffix) >= max_chars:
        return capped[:max_chars]
    truncated = title[:max_chars - len(fallback_suffix)].rstrip()
    # Remove trailing partial word
    if " " in truncated:
        truncated = truncated[:truncated.rfind(" ")]
    result = truncated + fallback_suffix
    
    logger.info(
        "title_enricher: brute-force injected power_word='%s' → %d chars",
        word, len(result),
    )
    return result


def build_power_words_prompt_section(power_words: list[str]) -> str:
    """Build the prompt section that instructs the LLM to include power words.
    
    Injects the channel-specific power word list into the metadata generator
    prompt so the LLM can incorporate them organically during generation.
    
    Args:
        power_words: List of power words from channel config.
    
    Returns:
        A prompt string section to insert into the system prompt.
    """
    if not power_words:
        return ""
    
    # Show a representative sample (max 25) so the prompt isn't bloated
    sample = random.sample(power_words, min(25, len(power_words)))
    words_str = ", ".join(sample)
    
    return f"""
⚠️ OBLIGATORIO — POWER WORDS DEL CANAL ⚠️
DEBES incluir AL MENOS UNA de estas palabras en el título final.
Intégrala de forma NATURAL y orgánica — no como un sufijo forzado:
Puede ir en medio del título, en mayúsculas para destacar, como sufijo
elegante, o reemplazando una palabra débil. Lo importante es que el
título fluya y la power word potencie el impacto sin parecer artificial.

Palabras de alto impacto para este canal (usa al menos 1):
{words_str}
"""


def build_optimizer_power_words_section(power_words: list[str]) -> str:
    """Same as build_power_words_prompt_section but tuned for the
    metadata optimizer (reoptimization) prompt context."""
    if not power_words:
        return ""
    
    sample = random.sample(power_words, min(20, len(power_words)))
    words_str = ", ".join(sample)
    
    return f"""
⚠️ El NUEVO título DEBE contener al menos UNA de estas palabras
de alto impacto. Intégrala de forma natural:
{words_str}
"""
