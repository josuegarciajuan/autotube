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
    return create_llm_client(timeout=60.0, max_retries=2)


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
        response = client.chat.completions.create(
            model=LLM_MODEL,
            messages=[
                {"role": "system", "content": _MARKETING_SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.9,
            max_tokens=1500,
        )
        
        content = response.choices[0].message.content.strip()
        
        # Extract JSON from response (handle markdown code blocks)
        if content.startswith("```"):
            lines = content.split("\n")
            content = "\n".join(lines[1:-1]) if len(lines) > 2 else content
            content = content.replace("```json", "").replace("```", "").strip()
        
        result = json.loads(content)
        
        title = result.get("title", "")
        if not title:
            title = "Historia Impactante que No Creerás"
        
        return {
            "title": title[:100],
            "description": result.get("description", ""),
            "tags": result.get("tags", []),
            "thumbnail_text": result.get("thumbnail_text", ""),
        }
    except Exception as e:
        # Fallback: return basic metadata
        return {
            "title": "Historia Impactante que No Creerás",
            "description": "Una historia real que te dejará sin palabras...\n\n#historiasreales #impactante",
            "tags": keywords or ["historias reales", "impactante", "increíble"],
            "thumbnail_text": "IMPACTANTE",
        }


async def generate_title_options(script_text: str, count: int = 1) -> list[str]:
    """Generate a single viral title option."""
    client = get_marketing_client()
    
    user_prompt = f"""Genera 1 ÚNICO título viral para YouTube basado en este contenido.
Debe ser IMPOSIBLE de ignorar, generar curiosidad extrema, y usar palabras de alto impacto emocional.
Máximo 100 caracteres.
Responde SOLO con un string JSON (sin array).

CONTENIDO:
{script_text[:2000]}"""

    try:
        response = client.chat.completions.create(
            model=LLM_MODEL,
            messages=[
                {"role": "system", "content": "Eres un experto en títulos virales de YouTube. Responde solo JSON."},
                {"role": "user", "content": user_prompt},
            ],
            temperature=1.0,
            max_tokens=500,
        )
        content = response.choices[0].message.content.strip()
        if content.startswith("```"):
            lines = content.split("\n")
            content = "\n".join(lines[1:-1]) if len(lines) > 2 else content
            content = content.replace("```json", "").replace("```", "").strip()
        return json.loads(content)
    except Exception:
        return ["La Historia Real Más Impactante que Escucharás Hoy"]
