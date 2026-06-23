"""YouTube SEO metadata generator for the Autotube pipeline.

Generates viral-optimized titles, descriptions, tags, and thumbnail overlay text
using the LLM (OpenAI/DeepSeek). Synchronous — callable from both CLI orchestrator
and API generation service.

Follows YouTube SEO best practices:
- Title: ≤100 chars, primary keyword in first 3 words, 5 angle variants
- Description: max 5000 bytes, first 125 chars as hook, chapters, CTAs, 2-3 hashtags
- Tags: 5-10 tags, 500 chars total, primary exact keyword first
- Thumbnail text: 3-5 words, complementary to title, high emotional impact
"""

import json
import logging
from typing import Optional

from openai import OpenAI

from config.settings import (
    LLM_API_KEY,
    LLM_BASE_URL,
    LLM_MODEL,
    LLM_PROVIDER,
)

logger = logging.getLogger(__name__)

# Pricing per 1M tokens (USD)
PRICING = {
    "deepseek": {"input": 0.14, "output": 0.28},
    "openai": {"input": 0.15, "output": 0.60},
}
PRICE_INPUT_PER_M = PRICING.get(LLM_PROVIDER, PRICING["openai"])["input"]
PRICE_OUTPUT_PER_M = PRICING.get(LLM_PROVIDER, PRICING["openai"])["output"]

_METADATA_SYSTEM_PROMPT = """Eres un EXPERTO en YouTube SEO, marketing digital y copywriting viral en español. Tu especialidad es crear títulos y thumbnails que generan CLICKS masivos.

Tu misión: crear metadatos que maximicen CTR (Click-Through Rate), retención y posicionamiento en YouTube.

═══ REGLAS DE ORO ═══

📝 TÍTULO VIRAL (1 ÚNICO título optimizado):
- Máximo 100 caracteres (límite YouTube).
- La keyword principal al frente (primeras 3 palabras).
- USA PATRONES DE ALTO CTR que están científicamente probados:
  • "El X que [verbo impactante]" — ej: "El experimento que destrozó la psicología"
  • Números impares + adjetivo extremo — ej: "3 secretos perturbadores de..."
  • Pregunta retórica que genera necesidad de respuesta — ej: "¿Qué pasó realmente en...?"
  • Revelación exclusiva — ej: "Lo que NADIE te contó sobre..."
  • Contraste emocional extremo — ej: "Prometió curarlos. Los destruyó a todos."
- Power words de alto engagement: impactante, increíble, secreto, oculto, perturbador, prohibido, aterrador, desgarrador, inexplicable, demoledor, estremecedor, alucinante, sobrecogedor, siniestro.
- Clickbait ÉTICO: el título promete algo que el video REALMENTE entrega.
- NO uses MAYÚSCULAS completas (solo una palabra clave estratégica).
- El título debe crear una sensación de "TENGO que ver esto" al hacer scroll.

🧠 PSICOLOGÍA DEL CLICK (APLICADA AL TÍTULO):
- Curiosity Gap: crea una pregunta que solo se responde al hacer clic.
- Zeigarnik Effect: información incompleta → ansiedad → necesidad de cerrar el ciclo.
- Emotional Arousal (high-arousal words): sorpresa, ira, miedo, asombro → comparten más.
- Von Restorff Effect: el título debe destacar entre los demás resultados de búsqueda.
- Números impares: 20% más CTR que los pares. Usa 3, 5, 7.

📄 DESCRIPCIÓN (SEO completa):
- PRIMERAS 125 caracteres: hook irresistible + keyword principal + propuesta de valor.
- Luego desarrolla el tema en 2-3 párrafos cortos con keywords secundarias.
- Incluye CAPÍTULOS en formato "0:00 — Título del capítulo" (mínimo 3).
- Añade 2-3 HASHTAGS relevantes al final (ej: #psicologia #experimentos).
- Incluye CTA de suscripción atractivo.
- Máximo 5000 bytes total.

🏷️ TAGS:
- Entre 5 y 10 tags.
- El PRIMER tag debe ser la keyword principal exacta.
- Incluye: keyword exacta + variantes + temas relacionados + errores comunes de escritura.
- Máximo 500 caracteres en total.
- Tags en español e inglés si aplica.

🖼️ TEXTO MINIATURA (OVERLAY):
- Máximo 24 caracteres (2-4 palabras de altísimo impacto visual).
- DEBE ser intrigante y complementario al título (no repetir sus primeras palabras — añade el gancho que falta).
- Formatos de alto CTR probados:
  • Pregunta corta que genera curiosidad: "¿QUÉ OCULTARON?", "¿CÓMO SOBREVIVIÓ?"
  • Cifra impactante: "3 MINUTOS", "NINGUNO SALIÓ"
  • Palabra-gancho con signos: "NADIE LO VIO", "PROHIBIDO", "SECRETO"
  • Afirmación extrema: "CAMBIÓ TODO", "FUE REAL"
- USA MAYÚSCULAS (más legible en miniatura).
- El texto debe hacer que el espectador NECESITE hacer clic para resolver la intriga.

═══ COHERENCIA TÍTULO ↔ IMAGEN ═══
El título y el texto de la miniatura deben trabajar JUNTOS, sin repetirse:
- TÍTULO: promete el tema principal + el gancho ("El experimento que volvió locos a 5 personas").
- THUMBNAIL_TEXT: añade la pieza de intriga que falta ("¿QUÉ LES HICIERON?").
- Juntos deben contar una mini-historia de 2 frases que obligue a hacer clic.

Responde SIEMPRE en formato JSON con exactamente estas claves:
{
  "title": "EL TÍTULO VIRAL ÚNICO (max 100 chars)",
  "description": "descripcion completa con emojis, chapters, CTAs y hashtags",
  "tags": ["tag1", "tag2", "tag3", "tag4", "tag5"],
  "thumbnail_text": "TEXTO INTRIGANTE MAX 24 CHARS"
}"""


class MetadataGenerator:
    """Generate YouTube-optimized metadata (titles, description, tags, thumbnail text)
    using the same LLM provider as the script generator.
    
    Synchronous — use directly from orchestrator or wrap in asyncio.to_thread for API.
    """

    def __init__(self, canal_config):
        """Initialize the metadata generator.
        
        Args:
            canal_config: Channel config object (SimpleNamespace from config_bridge
                          or module with CANAL_DISPLAY_NAME, etc.)
        """
        self.config = canal_config
        self.channel_name = getattr(canal_config, "CANAL_DISPLAY_NAME", "Canal de Historias")
        self.channel_tone = getattr(canal_config, "CANAL_TONE", "misterioso y cautivador")
        self.yt_category_id = getattr(canal_config, "YT_CATEGORY_ID", "27")
        
        # Build channel context for the prompt
        self._channel_context = f"""CANAL: {self.channel_name}
TONO DEL CANAL: {self.channel_tone}
CATEGORÍA: Educación (ID 27)"""

    def generate(self, script: dict, source_content: dict = None) -> dict:
        """Generate viral-optimized YouTube metadata for a video script.
        
        Args:
            script: Script dict with keys: guion, keywords, titulo_options, escenas, etc.
            source_content: Optional raw_content dict for additional context (title, source)
            
        Returns:
            dict with keys:
                titles: list with 1 viral title
                selected_title: the viral title
                description: full SEO-optimized description
                tags: list of 5-10 tags
                thumbnail_text: short overlay text (max 15 chars)
                category_id: YouTube category ID string
                token_count: estimated tokens used
                cost_estimate: estimated USD cost
        """
        import time
        
        start = time.time()
        
        # Extract content for prompt
        guion = script.get("guion", "")
        if not guion:
            logger.error("MetadataGenerator: no guion in script")
            return self._fallback_metadata(script)
        
        # Build keywords string
        keywords_raw = script.get("keywords") or script.get("keywords_json", "[]")
        if isinstance(keywords_raw, str):
            try:
                keywords = json.loads(keywords_raw)
            except json.JSONDecodeError:
                keywords = []
        else:
            keywords = keywords_raw or []
        
        keywords_str = ", ".join(keywords[:10]) if keywords else "historias impactantes, psicología, experimentos"
        
        # Build scene chapters for description
        escenas_raw = script.get("escenas") or script.get("escenas_json", "[]")
        if isinstance(escenas_raw, str):
            try:
                escenas = json.loads(escenas_raw)
            except json.JSONDecodeError:
                escenas = []
        else:
            escenas = escenas_raw or []
        
        scene_summaries = []
        for i, escena in enumerate(escenas[:8]):  # max 8 scenes for token budget
            desc = escena if isinstance(escena, str) else escena.get("descripcion", str(escena))
            scene_summaries.append(f"  - {desc}")
        scenes_text = "\n".join(scene_summaries) if scene_summaries else "(escenas no disponibles)"
        
        # Source context
        source_text = ""
        if source_content:
            source_title = source_content.get("title", "")
            source_url = source_content.get("source", "")
            source_text = f"\nFUENTE ORIGINAL: {source_title} (vía {source_url})"
        
        # Build user prompt
        script_snippet = guion[:4000] if len(guion) > 4000 else guion
        
        user_prompt = f"""{self._channel_context}

CONTENIDO DEL VIDEO:
Tema principal: {keywords_str}
{source_text}

RESUMEN DEL GUIÓN:
{script_snippet[:2500]}

ESCENAS DEL VIDEO:
{scenes_text}

INSTRUCCIONES:
1. Genera 1 ÚNICO título viral optimizado: keyword al inicio, patrón de alto CTR, power words, curiosidad extrema. IMPOSIBLE de ignorar al hacer scroll.
2. Crea una descripción SEO completa con chapters, emojis estratégicos y hashtags, hook irresistible en las primeras 125 chars.
3. Genera 5-10 tags optimizados (keyword exacta primero, variantes después).
4. Crea un texto de miniatura INTRIGANTE Y COMPLEMENTARIO (máx 24 caracteres, 2-4 palabras, MAYÚSCULAS). No repitas el título. Añade la pieza de curiosidad que falta. Formatos probados: pregunta corta, cifra impactante, palabra-gancho extrema.

IMPORTANTE: Responde SOLO con el objeto JSON, sin markdown, sin texto adicional."""

        client = OpenAI(api_key=LLM_API_KEY, base_url=LLM_BASE_URL)
        
        try:
            response = client.chat.completions.create(
                model=LLM_MODEL,
                messages=[
                    {"role": "system", "content": _METADATA_SYSTEM_PROMPT},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=0.9,
                max_tokens=2000,
            )
            
            content = response.choices[0].message.content.strip()
            
            # Extract JSON from response (handle markdown code blocks)
            if content.startswith("```"):
                lines = content.split("\n")
                content = "\n".join(lines[1:-1]) if len(lines) > 2 else content
                content = content.replace("```json", "").replace("```", "").strip()
            
            result = json.loads(content)
            
            # Validate and sanitize — single title
            title = result.get("title", "")
            if not title or not isinstance(title, str):
                title = self._fallback_titles(script)[0]
            
            # Ensure title ≤ 100 chars
            title = title[:100]
            
            description = result.get("description", "")
            # Truncate description to 5000 bytes
            desc_bytes = description.encode("utf-8")
            if len(desc_bytes) > 5000:
                # Truncate safely at UTF-8 boundary
                description = desc_bytes[:4997].decode("utf-8", errors="ignore") + "..."
            
            tags = result.get("tags", [])
            # Validate total tags chars ≤ 500
            tags_validated = self._validate_tags(tags[:10])
            
            thumbnail_text = result.get("thumbnail_text", "")
            if thumbnail_text:
                thumbnail_text = thumbnail_text[:24].strip().upper()
            else:
                thumbnail_text = title[:24] if title else "¿QUÉ OCULTAN?"
            
            # Track token usage
            usage = response.usage
            token_count = usage.total_tokens if usage else 0
            cost_estimate = 0.0
            if usage:
                cost_estimate = (
                    (usage.prompt_tokens / 1_000_000) * PRICE_INPUT_PER_M
                    + (usage.completion_tokens / 1_000_000) * PRICE_OUTPUT_PER_M
                )
            
            elapsed = time.time() - start
            logger.info(
                "Metadata generated in %.1fs: title='%s', %d tags, %d tokens ($%.4f)",
                elapsed, title[:60], len(tags_validated), token_count, cost_estimate,
            )
            
            return {
                "titles": [title],
                "selected_title": title,
                "description": description,
                "tags": tags_validated,
                "thumbnail_text": thumbnail_text,
                "category_id": self.yt_category_id,
                "token_count": token_count,
                "cost_estimate": cost_estimate,
            }
            
        except json.JSONDecodeError as e:
            logger.error("MetadataGenerator: failed to parse JSON response: %s", e)
            logger.debug("Raw response: %s", content[:500] if 'content' in dir() else "N/A")
            return self._fallback_metadata(script)
        except Exception as e:
            logger.error("MetadataGenerator: LLM call failed: %s", e)
            return self._fallback_metadata(script)

    def _validate_tags(self, tags: list[str]) -> list[str]:
        """Validate tags fit within YouTube's 500-char limit."""
        valid = []
        total_chars = 0
        for tag in tags:
            tag_str = str(tag).strip().lower()
            if not tag_str or len(tag_str) < 2:
                continue
            # Each tag costs its length + 2 for quoting if contains space + 1 for comma
            tag_cost = len(tag_str) + (4 if " " in tag_str else 0) + 1
            if total_chars + tag_cost > 500:
                break
            valid.append(tag_str)
            total_chars += tag_cost
        return valid[:10]

    def _fallback_titles(self, script: dict) -> list[str]:
        """Generate a single fallback title from script data."""
        titulo_raw = script.get("titulo_options") or script.get("titulo_options", "[]")
        if isinstance(titulo_raw, str):
            try:
                titles = json.loads(titulo_raw)
            except json.JSONDecodeError:
                titles = []
        else:
            titles = titulo_raw or []
        
        if titles and isinstance(titles, list) and len(titles) > 0:
            return [str(titles[0])[:100]]
        
        # Ultimate fallback
        keywords = script.get("keywords", [])
        if isinstance(keywords, str):
            try:
                keywords = json.loads(keywords)
            except json.JSONDecodeError:
                keywords = []
        
        main_kw = keywords[0] if keywords else "Historia Impactante"
        return [f"El {main_kw} que Nadie Te Contó — La Verdad Oculta"]

    def _fallback_metadata(self, script: dict) -> dict:
        """Return basic metadata when LLM generation fails."""
        logger.warning("MetadataGenerator: using fallback metadata")
        
        titles = self._fallback_titles(script)
        title = titles[0] if titles else "Video sin título"
        
        keywords_raw = script.get("keywords") or script.get("keywords_json", "[]")
        if isinstance(keywords_raw, str):
            try:
                keywords = json.loads(keywords_raw)
            except json.JSONDecodeError:
                keywords = []
        else:
            keywords = keywords_raw or []
        
        tags = keywords[:10] if keywords else ["historias", "impactante", "documental"]
        
        # Build basic description from template
        channel_desc = getattr(self.config, "DESCRIPTION_TEMPLATE", "")
        if channel_desc and "{titulo}" in channel_desc:
            description = channel_desc.format(
                titulo=title,
                descripcion_seo=script.get("descripcion_seo", ""),
                chapters="0:00 — Introducción\n2:00 — Desarrollo\n5:00 — Conclusión",
            )
        else:
            description = f"{title}\n\nUna historia que te dejará sin palabras...\n\n#historias #documental"
        
        return {
            "titles": titles,
            "selected_title": title,
            "description": description[:5000],
            "tags": self._validate_tags(tags),
            "thumbnail_text": "LO INCREÍBLE",
            "category_id": self.yt_category_id,
            "token_count": 0,
            "cost_estimate": 0.0,
        }
