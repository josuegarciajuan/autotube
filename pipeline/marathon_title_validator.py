"""
Validates marathon titles before publishing using LLM.
Checks virality score, clickbait detection, and ToS compliance.

Uses the fast/cheap model (LLM_MODEL, typically deepseek-v4-flash) with
thinking DISABLED to keep validation costs minimal.
"""

from __future__ import annotations

import logging
from typing import Optional

from config.llm_client import create_llm_client
from config.llm_helpers import llm_json_call_or_fallback
from config.settings import LLM_MODEL

logger = logging.getLogger(__name__)

# ── System prompt for the evaluator ──────────────────────────────

MARATHON_TITLE_SYSTEM_PROMPT = """Eres un experto en YouTube viralidad y copywriting.
Evalúas títulos de documentales/maratones en YouTube.

Para cada título, puntúas 3 dimensiones (1-10):

1. CURIOSIDAD (1-10): ¿El título genera una necesidad inmediata de hacer clic?
   - 10: "TENGO que saber de qué habla"
   - 1: "Ya sé lo que dice, no me interesa"

2. PRECISIÓN (1-10): ¿El título refleja fielmente el contenido?
   - 10: Describe exactamente lo que el video contiene
   - 1: Es clickbait engañoso (promete algo que no cumple)

3. PODER (1-10): ¿Usa power words, números, patrones virales?
   - 10: Usa múltiples técnicas de copywriting viral
   - 1: Es genérico y olvidable

Reglas de rechazo AUTOMÁTICO (responde con score=0 en precisión):
- Promete revelar un "secreto definitivo" si el video solo resume información pública
- Usa mayúsculas en TODA la frase
- Incluye "NO CREERÁS", "ALUCINARÁS", "ÚLTIMA HORA" sin justificación
- Hace afirmaciones médicas, financieras o legales no verificables
- Usa lenguaje de estafa o timo ("GANAR DINERO", "HÁGASE RICO")

PUNTUACIÓN FINAL = (CURIOSIDAD × 0.5 + PODER × 0.3) / PRECISIÓN
Si PRECISIÓN < 5 → RECHAZADO (clickbait)
Si PUNTUACIÓN FINAL < 7 → RECHAZADO (no suficientemente viral)

Responde SOLO con JSON, sin texto adicional:
{
  "curiosity_score": <int 1-10>,
  "precision_score": <int 1-10>,
  "power_score": <int 1-10>,
  "final_score": <float>,
  "rejection_reason": "<razón si rechazado, o vacío si aprobado>",
  "feedback": "<qué mejorar si no aprueba, o vacío si aprueba>",
  "alternative_1": "<título alternativo mejorado>",
  "alternative_2": "<segundo título alternativo>"
}"""


# ── Public API ────────────────────────────────────────────────────

def validate_marathon_title(
    title: str,
    topic: str,
    content_summary: str,
    hook_type: str,
) -> dict:
    """Validate a marathon title via LLM scoring.

    Args:
        title: The candidate title to evaluate.
        topic: The marathon's main topic (e.g. "El manuscrito Voynich").
        content_summary: First ~2000 chars of the script/content for context.
        hook_type: One of the MARATHON_HOOK_TYPES (e.g. "misterio_sin_resolver").

    Returns:
        {
            "approved": bool,
            "curiosity_score": int,
            "precision_score": int,
            "power_score": int,
            "final_score": float,
            "feedback": str,
            "alternative_titles": list[str],
        }
    """
    # Build the user prompt with full context
    user_prompt = f"""Evalúa este título de documental/maratón de YouTube.

TÍTULO: {title}

TEMA PRINCIPAL: {topic}

TIPO DE HOOK EMOCIONAL: {hook_type}
(Define la emoción que el título debe evocar)

RESUMEN DEL CONTENIDO:
{content_summary[:2000]}

Evalúa según las dimensiones y reglas definidas. Responde SOLO con JSON."""

    # Fallback result in case LLM fails
    fallback = {
        "curiosity_score": 5,
        "precision_score": 5,
        "power_score": 5,
        "final_score": 3.0,
        "rejection_reason": "LLM evaluation failed — manual review required",
        "feedback": "",
        "alternative_1": title,
        "alternative_2": title,
    }

    try:
        client = create_llm_client(enable_thinking=False)

        raw = llm_json_call_or_fallback(
            client,
            fallback,
            max_retries=2,
            retry_delay=1.5,
            model=LLM_MODEL,
            messages=[
                {"role": "system", "content": MARATHON_TITLE_SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.3,  # low temp for consistent evaluation
            max_tokens=600,
        )

        curiosity = _safe_int(raw.get("curiosity_score", 5), 1, 10)
        precision = _safe_int(raw.get("precision_score", 5), 1, 10)
        power = _safe_int(raw.get("power_score", 5), 1, 10)

        # Compute final score (sanity-check the LLM's math)
        if precision > 0:
            final = round((curiosity * 0.5 + power * 0.3) / precision, 2)
        else:
            final = 0.0

        # Use the computed score, not the LLM's potentially-wrong one
        approved = precision >= 5 and final >= 7.0

        feedback = str(raw.get("feedback", "") or raw.get("rejection_reason", ""))
        alternative_titles = [
            str(raw.get("alternative_1", "")),
            str(raw.get("alternative_2", "")),
        ]
        # Filter out empty alternatives
        alternative_titles = [t for t in alternative_titles if t.strip()]

        if not alternative_titles:
            alternative_titles = [title]

        logger.info(
            "Marathon title validation: title='%s' approved=%s "
            "curiosity=%d precision=%d power=%d final=%.2f",
            title[:60], approved, curiosity, precision, power, final,
        )

        return {
            "approved": approved,
            "curiosity_score": curiosity,
            "precision_score": precision,
            "power_score": power,
            "final_score": final,
            "feedback": feedback,
            "alternative_titles": alternative_titles,
        }

    except Exception as exc:
        logger.error("Marathon title validation crashed: %s — using fallback approval", exc)
        return {
            "approved": True,  # Don't block publication due to validator failure
            "curiosity_score": 5,
            "precision_score": 5,
            "power_score": 5,
            "final_score": 0.0,
            "feedback": f"Validator error: {exc}",
            "alternative_titles": [title],
        }


# ── Helpers ───────────────────────────────────────────────────────

def _safe_int(value, min_val: int = 1, max_val: int = 10) -> int:
    """Clamp a value to [min_val, max_val] and ensure it's an int."""
    try:
        v = int(value)
        return max(min_val, min(max_val, v))
    except (ValueError, TypeError):
        return (min_val + max_val) // 2  # mid-point fallback
