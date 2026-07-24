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

from config.llm_client import create_llm_client

from config.settings import (
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
- LONGITUD OBJETIVO: 40-65 caracteres (rango de máximo CTR comprobado). MÁXIMO ABSOLUTO: 100 caracteres.
- La keyword principal al frente (primeras 3 palabras). NO empieces con artículos débiles ("El", "La", "Un", "Una", "Los", "Las") a menos que formen parte de una frase de impacto deliberada. Mejor empieza con el número, la keyword o "Este/Esta" si necesitas un determinante.
- USA PATRONES DE ALTO CTR científicamente probados:
  • Número impar + adjetivo extremo — ej: "3 médicos vieron esto y NO pudieron explicarlo"
  • Pregunta retórica que genera necesidad de respuesta — ej: "¿Qué filmó esta cámara a 11.000 metros?"
  • Revelación exclusiva — ej: "Lo que NADIE te contó sobre [tema]"
  • Contraste emocional extremo — ej: "Entró al quirófano riendo. Salió sin poder hablar."
  • Paréntesis informativo al final para añadir credibilidad — ej: "(REAL)", "(Documental)", "(2026)"
- POWER WORDS por categoría (usa al menos 1 de cada categoría a lo largo de los metadatos, no solo en el título):
  ⚡ URGENCIA / EXCLUSIVIDAD: REVELADO, FILTRADO, CENSURADO, INÉDITO, CLASIFICADO, CONFIDENCIAL, PROHIBIDO, EXCLUSIVA
  💥 IMPACTO EMOCIONAL: ESCALOFRIANTE, DESGARRADOR, INEXPLICABLE, DEMOLEDOR, SOBRECOGEDOR, ESTREMECEDOR, ALUCINANTE, ATERRADOR
  🔍 CURIOSIDAD / MISTERIO: OCULTO, SECRETO, PERTURBADOR, SINIESTRO, ENIGMÁTICO, IMPACTANTE, INCREÍBLE, INSÓLITO
- Clickbait ÉTICO: el título promete algo que el video REALMENTE entrega.
- CAPITALIZACIÓN: escribe el título como una frase normal en español — SOLO la primera letra en mayúscula y los nombres propios. NUNCA pongas cada palabra en mayúscula (Title Case). Puedes poner UNA palabra clave en MAYÚSCULAS para destacar (máximo 1 por título).
- AÑADE un sufijo entre paréntesis SIEMPRE que aporte credibilidad: (REAL), (CASO REAL), (DOCUMENTAL), (ARCHIVOS CIA), (2026), (EXPEDIENTE). El sufijo va al final del título, después de un espacio, y no cuenta para los 40-65 chars objetivo.
- El título debe crear una sensación de "TENGO que ver esto" al hacer scroll.

🧠 PSICOLOGÍA DEL CLICK (APLICADA AL TÍTULO):
- Curiosity Gap: crea una pregunta que solo se responde al hacer clic.
- Zeigarnik Effect: información incompleta → ansiedad → necesidad de cerrar el ciclo.
- Emotional Arousal (high-arousal words): sorpresa, ira, miedo, asombro → comparten más.
- Von Restorff Effect: el título debe destacar entre los demás resultados de búsqueda.
- Números impares: 20% más CTR que los pares. Usa 3, 5, 7, 9, 11.
- Credibilidad por señal: un sufijo entre paréntesis (REAL, DOCUMENTAL) actúa como señal de confianza → reduce el escepticismo del clickbait y aumenta CTR en nichos de curiosidad/misterio.

Ejemplos de buena capitalización:
  CORRECTO: "Nadie creyó su predicción. 3 días después ocurrió (REAL)"
  CORRECTO: "5 médicos vieron esto y NO pudieron explicarlo (Documental)"
  CORRECTO: "El experimento que volvió LOCOS a 5 personas (ARCHIVOS CIA)"
  INCORRECTO: "El Milagro Que Dejó Sin Palabras A 5 Médicos"
  INCORRECTO (empieza con artículo débil sin impacto): "La historia que nadie te contó sobre..."

📄 DESCRIPCIÓN (SEO completa):
- PRIMERAS 2-3 LÍNEAS: un resumen envolvente que explique de qué va el vídeo a alguien que NO lo ha visto. Incluye la keyword principal de forma natural. Debe ser la respuesta a "¿de qué trata este vídeo?" en 2-3 frases.
- Luego desarrolla el tema en 2-3 párrafos cortos con keywords secundarias y propuesta de valor del vídeo.
- Incluye CAPÍTULOS en formato "0:00 — Título del capítulo" (mínimo 3).
- CTA de suscripción atractivo y natural.
- Optimiza la descripción para que los primeros 125 caracteres sean el HOOK principal (es lo que se ve sin expandir).
- Máximo 5000 caracteres total.

💡 HASHTAGS (AL FINAL DE LA DESCRIPCIÓN):
- Añade ENTRE 3 Y 5 hashtags al final de la descripción.
- El PRIMER hashtag debe ser la keyword principal del vídeo.
- Los hashtags deben reflejar ideas que un usuario podría buscar en YouTube.
- NO uses más de 15 hashtags (YouTube ignora todos si pones más de 15).
- Los 3 primeros son los que aparecen sobre el título del vídeo → elíguelos con máximo cuidado.
- Formato: #PalabraClave

🏷️ TAGS (METADATOS OCULTOS):
- Entre 5 y 10 tags.
- El PRIMER tag debe ser la keyword principal exacta (debe COINCIDIR con el primer hashtag — esto refuerza el SEO).
- El RESTO de tags deben ser DIFERENTES de los hashtags (no los dupliques, salvo el primero):
  • Variantes de la keyword principal.
  • Errores comunes de escritura de la keyword.
  • Tags en inglés si aplica.
  • Temas relacionados y long-tail keywords.
- Máximo 500 caracteres en total para todos los tags juntos.

🖼️ TEXTO MINIATURA (DOS LÍNEAS DE OVERLAY):
Ahora la miniatura tiene DOS líneas de texto en vez de una:
- LÍNEA 1 (gancho principal, texto GRANDE): 1-2 palabras en MAYÚSCULAS. Máximo 12 caracteres. Debe ser la palabra o frase más impactante que haga DETENER el scroll. Formatos probados:
  • Palabra-gancho con signos: "¿QUÉ PASÓ?", "NADIE LO VIO", "PROHIBIDO"
  • Cifra impactante: "3 MINUTOS", "NINGUNO SALIÓ", "5 MÉDICOS"
  • Afirmación extrema: "FUE REAL", "CAMBIÓ TODO"
- LÍNEA 2 (complemento, texto MEDIANO debajo de L1): 2-4 palabras. Máximo 24 caracteres. Complementa a L1 y al título sin repetirlos. Añade la pieza de intriga que falta. Formatos probados:
  • "Nadie lo explicó", "Lo que ocultaron", "La verdad sale"
  • "El informe secreto", "Dijeron que era imposible"
- Regla de oro: L1 + L2 + título deben contar una mini-historia de 3 frases que obligue a hacer clic. NINGUNA línea repite a las otras ni al título.

═══ COHERENCIA TÍTULO ↔ MINIATURA ═══
El título, L1 y L2 deben trabajar JUNTOS, sin repetirse:
- TÍTULO: promete el tema principal + el gancho ("5 médicos vieron esto y NO pudieron explicarlo (REAL)").
- L1 (thumbnail): palabra-gancho visual ("¿QUÉ PASÓ?").
- L2 (thumbnail): el complemento intrigante ("El informe secreto").
- Juntos deben contar una mini-historia de 3 actos que obligue a hacer clic.

Responde SIEMPRE en formato JSON con exactamente estas claves:
{
  "title": "Título viral en español (40-65 chars ideal, max 100, solo primera letra mayúscula + 1 palabra en CAPS + sufijo entre paréntesis)",
  "title_suffix": "REAL | DOCUMENTAL | CASO REAL | ARCHIVOS CIA | 2026 | EXPEDIENTE | '' si no aplica",
  "description": "2-3 líneas de resumen + desarrollo + chapters + CTAs + 3-5 hashtags al final",
  "tags": ["tag1", "tag2", "tag3", "tag4", "tag5"],
  "thumbnail_text": "LÍNEA1 IMPACTO MAX 12 CHARS | línea2 complemento max 24 chars (separadas por |)",
  "badge_text": "DOCUMENTAL | CASO REAL | REAL | ARCHIVO | EXPEDIENTE | '' (texto para el sello/badge en la esquina de la miniatura)"
}"""


# ── Smart overlay-text truncation ──────────────────────────────

def _smart_overlay_text(text: str, soft: int = 24, hard: int = 34) -> str:
    """Truncate overlay text at word boundaries, never splitting a word.

    - If the whole phrase fits within *hard* chars, return it as-is
      (the thumbnail renderer wraps it across 2 lines anyway).
    - Otherwise, drop trailing WHOLE words until the result fits
      within *soft* chars.
    - Never returns an empty string (falls back to original truncated
      at *hard* as absolute last resort).
    """
    text = " ".join((text or "").split())  # normalize whitespace
    if not text:
        return text
    if len(text) <= hard:
        return text
    # Drop trailing whole words to fit within soft limit
    words = text.split()
    out = ""
    for w in words:
        cand = (out + " " + w).strip()
        if out and len(cand) > soft:
            break
        out = cand
    return out or text[:hard]  # never empty; last-resort hard cut


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
1. Genera 1 ÚNICO título viral optimizado (40-65 chars): keyword al inicio, patrón de alto CTR, power words, curiosidad extrema. IMPOSIBLE de ignorar al hacer scroll. Añade sufijo entre paréntesis si aporta credibilidad: (REAL), (DOCUMENTAL), etc.
2. Si el título lleva sufijo parentético, sepáralo en el campo "title_suffix". Si no, déjalo vacío.
3. Crea una descripción SEO completa con chapters, emojis estratégicos y hashtags, hook irresistible en las primeras 125 chars.
4. Genera 5-10 tags optimizados (keyword exacta primero, variantes después).
5. Crea DOS LÍNEAS de texto para la miniatura separadas por | : L1 (máx 12 chars, palabra-gancho en MAYÚSCULAS) | L2 (máx 24 chars, complemento intrigante). No repitas el título.
6. Define el texto para el badge/sello de la miniatura (DOCUMENTAL, CASO REAL, REAL, ARCHIVO, EXPEDIENTE, o vacío).

IMPORTANTE: Responde SOLO con el objeto JSON, sin markdown, sin texto adicional."""

        client = create_llm_client(enable_thinking=True, timeout=120.0, max_retries=2)
        
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
            
            # Extract title suffix if present (LLM may or may not include it in title)
            title_suffix = result.get("title_suffix", "").strip().upper()
            if title_suffix:
                # Remove suffix symbols the LLM might wrap it in
                title_suffix = title_suffix.strip("()（）[]")
                # If title doesn't already end with the suffix, append it
                suffix_formatted = f" ({title_suffix})"
                if not title.rstrip().endswith(suffix_formatted):
                    title = title.rstrip() + suffix_formatted
            
            # Ensure title ≤ 100 chars (trim suffix if needed)
            if len(title) > 100:
                # Try to fit by trimming suffix first, then title
                suffix_formatted = f" ({title_suffix})" if title_suffix else ""
                max_title_body = 100 - len(suffix_formatted)
                title = title[:max_title_body].rstrip() + suffix_formatted
                title = title[:100]  # final safety
            
            description = result.get("description", "")
            # Truncate description to 5000 bytes
            desc_bytes = description.encode("utf-8")
            if len(desc_bytes) > 5000:
                # Truncate safely at UTF-8 boundary
                description = desc_bytes[:4997].decode("utf-8", errors="ignore") + "..."
            
            tags = result.get("tags", [])
            # Validate total tags chars ≤ 500
            tags_validated = self._validate_tags(tags[:10])
            
            # Parse thumbnail text — new format: "L1 | L2" or legacy single line
            thumbnail_text_raw = result.get("thumbnail_text", "")
            if thumbnail_text_raw:
                thumbnail_text = _smart_overlay_text(thumbnail_text_raw).upper()
            else:
                thumbnail_text = _smart_overlay_text(title).upper() if title else "¿QUÉ PASÓ?"
            
            # Parse badge text for thumbnail seal
            badge_text = result.get("badge_text", "").strip().upper()
            if badge_text:
                badge_text = badge_text.strip("()（）[]")
            
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
                "Metadata generated in %.1fs: title='%s', tags=%d, badge='%s', %d tokens ($%.4f)",
                elapsed, title[:60], len(tags_validated), badge_text, token_count, cost_estimate,
            )
            
            return {
                "titles": [title],
                "selected_title": title,
                "description": description,
                "tags": tags_validated,
                "thumbnail_text": thumbnail_text,
                "badge_text": badge_text,
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
            "thumbnail_text": "¿QUÉ PASÓ? | La verdad detrás",
            "badge_text": "DOCUMENTAL",
            "category_id": self.yt_category_id,
            "token_count": 0,
            "cost_estimate": 0.0,
        }
