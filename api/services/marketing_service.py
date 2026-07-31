"""Marketing service — AI-powered title, description, and tag generation.

Calls the LLM to generate viral-optimized YouTube metadata.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from config.llm_client import create_llm_client
from config.settings import LLM_MODEL

_MARKETING_SYSTEM_PROMPT = """Eres un experto en marketing digital y YouTube SEO especializado en hacer videos virales en español.

Tu trabajo es crear títulos, descripciones y etiquetas que:
1. Generen curiosidad extrema — la gente NO puede resistirse a hacer clic
2. Usen palabras de alto impacto emocional (impactante, increíble, escalofriante, etc.)
3. Crear FOMO (miedo a perderse algo)
4. Sean clickbait ético — prometen algo que el video realmente entrega
5. Usen mayúsculas estratégicas y emojis con moderación
6. Incluyan palabras clave SEO de alto volumen

Reglas de oro:
- El título debe ser IMPOSIBLE de ignorar — UN solo título perfecto, no múltiples opciones
- La descripción debe enganchar en las primeras 2 líneas (lo que se ve sin "mostrar más")
- Usa hooks psicológicos: curiosidad, urgencia, exclusividad, prueba social
- Piensa en qué haría que ALGUIEN que no le interesa el tema haga clic igual

Responde SIEMPRE en formato JSON con estas claves:
{
  "title": "EL TÍTULO VIRAL ÚNICO (max 100 chars)",
  "description": "descripcion completa con emojis, CTAs y hashtags",
  "tags": ["tag1", "tag2", ...],
  "thumbnail_text": "texto corto impactante para la miniatura (max 15 caracteres)"
}"""


def get_marketing_client():
    """Get OpenAI-compatible client for marketing generation."""
    return create_llm_client(enable_thinking=False, timeout=60.0, max_retries=2)


def _extract_keyword(text: str) -> str:
    """Extract a meaningful keyword from text for fallback metadata."""
    import re
    stopwords = {"de","la","el","los","las","un","una","en","con","por","para","que","del","al","lo","le","se","su","sus","y","o","a","e","ni","no","es","the","a","an","of","in","on","to","for","and","or","is","it","that","this","with","was","are","be","from","by"}
    words = re.findall(r'\b[\wáéíóúñÁÉÍÓÚÑ]+\b', text[:500])
    keywords = [w for w in words if len(w) > 3 and w.lower() not in stopwords]
    return keywords[0][:100] if keywords else ""


async def generate_marketing_content(script_text: str, keywords: list[str] = None,
                                      channel_name: str = "") -> dict:
    """Generate viral-optimized titles, description, tags, and thumbnail text.
    
    Args:
        script_text: The video script text (first 2000 chars to save tokens)
        keywords: Existing keywords to incorporate
        channel_name: Channel name for context
    
    Returns:
        dict with keys: titles, description, tags, thumbnail_text
    """
    client = get_marketing_client()
    
    # Truncate script for token efficiency
    script_snippet = script_text[:3000] if len(script_text) > 3000 else script_text
    
    user_prompt = f"""Genera metadatos virales para YouTube para este video:

CANAL: {channel_name or 'Canal de historias impactantes'}

CONTENIDO DEL VIDEO (resumen del guion):
{script_snippet}

PALABRAS CLAVE SUGERIDAS: {', '.join(keywords) if keywords else 'historias reales, impactante, increíble'}

INSTRUCCIONES ESPECIALES:
- El título debe tener máximo 100 caracteres — 1 ÚNICO título viral, no múltiples opciones
- Asegúrate de que el título use al menos 2 disparadores psicológicos
- La descripción debe tener emojis estratégicos y hashtags al final
- El texto de miniatura debe ser CORTO y GOLPEANTE (máximo 15 caracteres)
- Los tags deben ser 10-15 palabras/frases relevantes de alto volumen de búsqueda

IMPORTANTE: Responde SOLO con el objeto JSON, sin texto adicional."""

    try:
        from config.llm_helpers import llm_json_call
        result = llm_json_call(
            client,
            max_retries=3,
            retry_delay=2.0,
            model=LLM_MODEL,
            messages=[
                {"role": "system", "content": _MARKETING_SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.9,
            max_tokens=1500,
        )

        title = result.get("title", "")
        if not title:
            title = _derive_title_from_content(script_text[:200])
        
        return {
            "title": title[:100],
            "description": result.get("description", ""),
            "tags": result.get("tags", []),
            "thumbnail_text": result.get("thumbnail_text", ""),
        }
    except Exception as e:
        # Fallback: derive from content
        content_keyword = _extract_keyword(script_text)
        logger.warning("Marketing content generation failed after retries: %s", e)
        return {
            "title": content_keyword if content_keyword else "Historia Impactante que No Creerás",
            "description": f"{content_keyword}\n\nUna historia real que te dejará sin palabras...\n\n#historiasreales #impactante",
            "tags": keywords or ["historias reales", "impactante", "increíble"],
            "thumbnail_text": content_keyword.upper()[:15] if content_keyword else "IMPACTANTE",
        }


async def generate_title_options(script_text: str, count: int = 1,
                                 channel_slug: str = None) -> list[str]:
    """Generate a single viral title option."""
    client = get_marketing_client()

    # ── Channel power words (if available) ────────────────────────
    power_words = []
    if channel_slug:
        try:
            from config.config_bridge import get_channel_config
            cfg = get_channel_config(channel_slug)
            power_words = getattr(cfg, "TITLE_POWER_WORDS", [])
        except Exception:
            pass

    user_prompt = f"""Genera 1 ÚNICO título viral para YouTube basado en este contenido.
Debe ser IMPOSIBLE de ignorar, generar curiosidad extrema, y usar palabras de alto impacto emocional.
Máximo 100 caracteres.
Responde SOLO con un string JSON (sin array).

CONTENIDO:
{script_text[:2000]}"""

    try:
        from config.llm_helpers import llm_json_call
        result = llm_json_call(
            client,
            max_retries=3,
            retry_delay=2.0,
            model=LLM_MODEL,
            messages=[
                {"role": "system", "content": "Eres un experto en títulos virales de YouTube. Responde SOLO con JSON: {\"title\": \"...\"}."},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.9,
            max_tokens=500,
        )
        titles = result.get("title") if isinstance(result, dict) else result
        if isinstance(titles, str) and titles:
            # ── Safety net: enforce at least one power word ────────
            from pipeline.title_enricher import enforce_power_words
            return [enforce_power_words(titles[:100], power_words)]
        if isinstance(titles, list):
            from pipeline.title_enricher import enforce_power_words
            return [enforce_power_words(t[:100], power_words) for t in titles if isinstance(t, str)][:count]
        return ["La Historia Real Más Impactante que Escucharás Hoy"]
    except Exception as e:
        logger.warning("Title options generation failed after retries: %s", e)
        return ["La Historia Real Más Impactante que Escucharás Hoy"]
