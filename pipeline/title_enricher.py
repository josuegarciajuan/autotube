"""Title Power Word helpers — NON-DESTRUCTIVE.

History: this module used to *inject* a power word into any title that lacked
one, appending grammatically broken phrases (``"La Verdad Ocultado"``,
``"El Secreto Primera"``, ``"Real para Siempre"``) and cutting titles mid-word.
That behaviour is gone.

Current contract:
  * ``enforce_power_words`` NEVER appends anything. If the title already
    contains a power word it is returned as-is; if not, the title is returned
    INTACT (only word-boundary truncated) and a log line is emitted.
  * ``build_power_words_prompt_section`` feeds the LLM with the channel's
    power words (already filtered against the shared banned tokens) and asks
    for organic integration — the LLM is the only mechanism.
  * The real decision-making lives in ``pipeline.title_engine``.

Integration points:
  - metadata_generator.py::MetadataGenerator
  - metadata_optimizer.py::MetadataOptimizer.reoptimize()
  - viral_cloner.py::clone_title_description()
  - api/services/marketing_service.py::generate_title_options()
  - pipeline/video_validator.py (belt-and-suspenders, now advisory)
"""

from __future__ import annotations

import logging
import random
import re

from pipeline.title_tokens import contains_banned_token

logger = logging.getLogger(__name__)


def _truncate_at_word(text: str, max_chars: int) -> str:
    """Truncate at a word boundary; never split a word."""
    text = " ".join(str(text or "").split())
    try:
        limit = int(max_chars)
    except (TypeError, ValueError):
        return text
    if limit <= 0 or len(text) <= limit:
        return text
    cut = text[:limit]
    if " " in cut:
        return cut[: cut.rfind(" ")].rstrip(" .,;:·•–—|-")
    return text


def _filter_power_words(power_words) -> list[str]:
    """Drop empty / banned / duplicated power words."""
    if not power_words:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for word in power_words:
        text = str(word or "").strip()
        key = text.casefold()
        if not text or key in seen:
            continue
        if contains_banned_token(text):
            logger.info("title_enricher: power word '%s' filtered (banned token)", text)
            continue
        seen.add(key)
        out.append(text)
    return out


def resolve_title_max_chars(config, default: int = 100) -> int:
    """Return the single configured title limit used by all title stages."""
    try:
        return max(1, int(getattr(config, "TITLE_MAX_CHARS", default)))
    except (TypeError, ValueError):
        return default

# ── Helpers ──────────────────────────────────────────────────────────

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
    """Return *title* unchanged (word-truncated) — never injects a suffix.

    Kept as a stable public API for existing callers. If the title already
    contains a power word it is returned as-is; if it does not, the title is
    returned INTACT and a warning is logged. The old forced-suffix behaviour
    produced broken titles and was removed.

    Args:
        title: The original title (already trimmed/validated).
        power_words: List of power words from channel config (used only to
            decide whether to log a warning).
        max_chars: Maximum allowed title length (default 100 per YouTube).
        _rng: Unused; kept for backward-compatible call signatures.

    Returns:
        The title, unchanged except for word-boundary truncation.
    """
    if not title or not title.strip():
        logger.warning("title_enricher: empty title, cannot enrich")
        return title

    # Collapse duplicate trailing parentheticals (e.g. "(Impactante) (IMPACTANTE)")
    title = _collapse_trailing_parentheticals(title)

    clean_words = _filter_power_words(power_words)
    if clean_words and not _any_power_word_present(title, clean_words):
        # Non-destructive: we do NOT touch the title. The TitleEngine is the
        # component responsible for producing a good title.
        logger.info(
            "title_enricher: title lacks any power word (%d configured) — "
            "leaving title intact (no injection)",
            len(clean_words),
        )

    return _truncate_at_word(title, max_chars)


def build_power_words_prompt_section(power_words: list[str]) -> str:
    """Build the prompt section that instructs the LLM to include power words.

    Only *suggestions* for organic integration are sent — the enricher no
    longer forces them into the title. Banned / duplicate words are filtered
    out against the shared token list.

    Args:
        power_words: List of power words from channel config.

    Returns:
        A prompt string section to insert into the system prompt.
    """
    clean = _filter_power_words(power_words)
    if not clean:
        return ""

    # Show a representative sample (max 25) so the prompt isn't bloated
    sample = random.sample(clean, min(25, len(clean)))
    words_str = ", ".join(sample)

    return f"""
POWER WORDS DEL CANAL (SUGERENCIAS OPCIONALES):
Si encaja con naturalidad, integra UNA de estas palabras en el título.
NUNCA la añadas como sufijo forzado ni sacrifiques la gramática por incluirlas:
si no encaja, no la uses.

Palabras de alto impacto para este canal:
{words_str}
"""


def build_optimizer_power_words_section(power_words: list[str]) -> str:
    """Same as build_power_words_prompt_section but tuned for the
    metadata optimizer (reoptimization) prompt context."""
    clean = _filter_power_words(power_words)
    if not clean:
        return ""

    sample = random.sample(clean, min(20, len(clean)))
    words_str = ", ".join(sample)

    return f"""
Power words sugeridas para el nuevo título (integra como máximo UNA,
solo si encaja de forma natural):
{words_str}
"""
