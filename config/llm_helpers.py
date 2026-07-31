"""Shared LLM helpers — retry logic and hook-text derivation.

Provides:
  - ``llm_json_call()`` — wrapper with 3 semantic retries (empty content,
    JSON parse errors, general exceptions) and exponential backoff.
  - ``llm_json_call_or_fallback()`` — same but returns ``fallback`` instead
    of raising after exhaustion.
  - ``_derive_hook_from_title()`` — extracts a unique L1|L2 pair from
    the video title, never returning generic text like "¿QUÉ PASÓ?".

Intended to replace the ad-hoc single-shot try/except patterns found in
many pipeline modules.  Pattern based on ``script_generator.py:_llm_json_call()``.
"""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Any, Callable, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")


# ── Reasoning-content extraction (DeepSeek thinking-mode compatibility) ─

def _extract_reasoning_content(response) -> str | None:
    """Extract content from a chat completion response with thinking-mode fallback.

    DeepSeek models with ``enable_thinking=True`` may route the actual JSON
    response into ``reasoning_content`` while ``content`` remains empty.
    The standard OpenAI SDK v2.x ``ChatCompletionMessage`` model does NOT
    expose ``reasoning_content`` — it is available via ``model_extra`` dict
    on newer SDK versions, or as a direct attribute on the raw response.

    Returns the best available content string, preferring ``content`` over
    ``reasoning_content``. Returns ``None`` if both are empty/missing.
    """
    msg = response.choices[0].message

    # 1. Primary field (standard response)
    content = msg.content
    if content and content.strip():
        return content.strip()

    # 2. Direct attribute (works with some SDK versions)
    reasoning = getattr(msg, 'reasoning_content', None)
    if reasoning and reasoning.strip():
        return reasoning.strip()

    # 3. model_extra dict (OpenAI SDK v2.3+ stores unknown fields here)
    try:
        model_extra = getattr(msg, 'model_extra', None)
        if isinstance(model_extra, dict):
            reasoning = model_extra.get('reasoning_content', '')
            if reasoning and reasoning.strip():
                return reasoning.strip()
    except Exception:
        pass

    # 4. Raw response fallback (access underlying dict for maximum compatibility)
    try:
        response_dict = response.model_dump() if hasattr(response, 'model_dump') else response.dict()
        choices = response_dict.get('choices', [])
        if choices:
            msg_dict = choices[0].get('message', {})
            reasoning = msg_dict.get('reasoning_content', '')
            if reasoning and reasoning.strip():
                return reasoning.strip()
    except Exception:
        pass

    return None


# ── Retry wrapper ──────────────────────────────────────────────────────

def llm_json_call(
    client,
    max_retries: int = 3,
    retry_delay: float = 2.0,
    **call_kwargs,
) -> dict:
    """Call ``client.chat.completions.create(**call_kwargs)`` and parse JSON.

    Retries up to *max_retries* times with exponential backoff on:
      - Empty / whitespace-only content
      - JSON parse failures
      - Any other exception

    On the first retry, the temperature is bumped slightly (+0.05) to
    encourage a different output.  On DeepSeek, alternate thinking-mode
    is not touched here — callers should configure the client accordingly.

    Raises the last exception if all retries are exhausted.
    """
    last_exc: Exception | None = None

    for attempt in range(max_retries):
        try:
            response = client.chat.completions.create(**call_kwargs)
            content = _extract_reasoning_content(response)
            if content is None or not content.strip():
                raise ValueError(
                    "LLM returned empty content "
                    f"(attempt {attempt + 1}/{max_retries})"
                )

            # Robust JSON extraction from LLM responses.
            # DeepSeek thinking-mode may leak preamble text ("Let me
            # analyze...") before the actual JSON.  Use regex to extract
            # JSON from anywhere in the response, not just when it starts
            # with a markdown fence.
            text = content.strip()

            # 1. Try markdown code fence extraction (anywhere in the text)
            m = re.search(r"```(?:json)?\s*\n(.*?)\n```", text, re.DOTALL)
            if m:
                text = m.group(1).strip()
            else:
                # 2. Try to extract the first JSON object { ... } from
                #    anywhere in the text (handles preamble / thinking text)
                m = re.search(r"\{.*\}", text, re.DOTALL)
                if m:
                    text = m.group(0)

            return json.loads(text)

        except json.JSONDecodeError as exc:
            last_exc = exc
            if attempt < max_retries - 1:
                delay = retry_delay * (2 ** attempt)
                logger.warning(
                    "LLM JSON parse failed (attempt %d/%d): %s — "
                    "retrying in %.1fs",
                    attempt + 1, max_retries, exc, delay,
                )
                time.sleep(delay)
                # Bump temperature slightly for diversity
                if "temperature" in call_kwargs:
                    call_kwargs["temperature"] = min(
                        1.0, call_kwargs["temperature"] + 0.05
                    )

        except ValueError as exc:
            last_exc = exc
            if attempt < max_retries - 1:
                delay = retry_delay * (2 ** attempt)
                logger.warning(
                    "%s — retrying in %.1fs", exc, delay,
                )
                time.sleep(delay)
                if "temperature" in call_kwargs:
                    call_kwargs["temperature"] = min(
                        1.0, call_kwargs["temperature"] + 0.05
                    )

        except Exception as exc:
            last_exc = exc
            if attempt < max_retries - 1:
                delay = retry_delay * (2 ** attempt)
                logger.warning(
                    "LLM call failed (attempt %d/%d): %s — "
                    "retrying in %.1fs",
                    attempt + 1, max_retries, exc, delay,
                )
                time.sleep(delay)

    raise last_exc  # type: ignore[misc]


def llm_json_call_or_fallback(
    client,
    fallback: dict,
    max_retries: int = 3,
    retry_delay: float = 2.0,
    **call_kwargs,
) -> dict:
    """Like :func:`llm_json_call` but returns *fallback* on exhaustion."""
    try:
        return llm_json_call(
            client,
            max_retries=max_retries,
            retry_delay=retry_delay,
            **call_kwargs,
        )
    except Exception as exc:
        logger.error(
            "LLM JSON call failed after %d retries: %s — using fallback",
            max_retries, exc,
        )
        return fallback


# ── Hook-text derivation from title ────────────────────────────────────

# Rotated set of generic hooks — only used as absolute last resort when
# the title is empty or contains no usable keywords.  Excludes "¿QUÉ PASÓ?".
_FALLBACK_HOOKS = [
    ("NADIE LO VIO", "La verdad oculta"),
    ("FUE REAL", "Los detalles completos"),
    ("IMPACTANTE", "Lo que no se contó"),
    ("INCREÍBLE", "El caso completo"),
    ("SIN EXPLICACIÓN", "La historia real"),
    ("OCULTO", "Nadie lo había visto"),
]


def _derive_hook_from_title(title: str) -> str:
    """Derive a two-line thumbnail hook (L1 | L2) from the video title.

    Returns a string in the format ``"L1 | L2"`` where:
      - **L1** is a short, uppercase hook word/phrase extracted from the title.
      - **L2** is a complementary intrigue phrase.

    Never returns ``"¿QUÉ PASÓ?"`` — that phrase is explicitly excluded.
    """
    if not title or not title.strip():
        return _random_hook_fallback()

    clean = title.strip()

    # ── Strategy 1: Extract a number → "NUMBER" hook ─────────────────
    number_match = re.search(r'\b(\d+)\b', clean)
    if number_match:
        num = number_match.group(1)
        # Look for a unit word after the number
        unit_match = re.search(
            rf'{num}\s+(minutos?|segundos?|años?|horas?|días?|siglos?|millones?|médicos?|hombres?|naves?|sueños?|casos?|veces?)',
            clean, re.IGNORECASE,
        )
        if unit_match:
            l1 = f"{num} {unit_match.group(1).upper()}"
            if len(l1) > 14:
                l1 = f"{num} {unit_match.group(1)[:4].upper()}"
            l2 = "La historia completa"
            return f"{l1} | {l2}"
        else:
            l1 = num
            l2 = "Lo inexplicable"
            return f"{l1} | {l2}"

    # ── Strategy 2: First impactful keyword from title ──────────────
    # Remove common stop words and extract first meaningful word
    stopwords = {
        "de", "la", "el", "los", "las", "un", "una", "unos", "unas",
        "en", "con", "por", "para", "que", "del", "al", "lo", "le",
        "se", "su", "sus", "y", "o", "a", "e", "ni", "no", "es",
        "the", "a", "an", "of", "in", "on", "to", "for", "and", "or",
        "is", "it", "that", "this", "with", "was", "are", "be", "from",
        "by", "at", "has", "had", "but", "not", "you", "we", "they",
    }

    words = re.findall(r'\b[\wáéíóúñÁÉÍÓÚÑ]+\b', clean)
    keywords = [
        w for w in words
        if len(w) > 3
        and w.lower() not in stopwords
        and not w.isdigit()
    ]

    if keywords:
        # Use the most impactful keyword (prefer words with 4+ chars,
        # capitalized words, or words after numbers)
        for w in keywords:
            if len(w) >= 5 and (w[0].isupper() or w.isupper()):
                l1 = w.upper()[:14]
                l2 = "El caso real"
                return f"{l1} | {l2}"

        # Fallback: use first keyword
        l1 = keywords[0].upper()[:14]
        l2 = "Lo que ocultaron"
        return f"{l1} | {l2}"

    # ── Strategy 3: Random non-"¿QUÉ PASÓ?" hook ─────────────────────
    return _random_hook_fallback()


def _random_hook_fallback() -> str:
    """Return a randomly selected hook pair (never "¿QUÉ PASÓ?")."""
    import random
    l1, l2 = random.choice(_FALLBACK_HOOKS)
    return f"{l1} | {l2}"
