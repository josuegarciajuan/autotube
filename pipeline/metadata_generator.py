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

import hashlib
import json
import logging
import random
import re as _re
from typing import Optional

from config.llm_client import create_llm_client

from config.settings import (
    LLM_MODEL,
    LLM_MODEL_CREATIVE,
    LLM_PROVIDER,
)
from config.llm_helpers import llm_json_call, _derive_hook_from_title
from pipeline.title_enricher import (
    enforce_power_words,
    build_power_words_prompt_section,
    resolve_title_max_chars,
)
from pipeline.seo_researcher import SEOResearcher, _format_seconds
from prompts.base_prompts import packaging_rules
from api.services.packaging_policy import validate_title

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
  • Sufijo factual opcional al final SOLO si aporta contexto verificable — ej: "(2026)", "(Caso de 1921)". NUNCA "(REAL)", "(CASO REAL)", "(IMPACTANTE)", "(REVELACIÓN)", "(ARCHIVOS CIA)", "(EXPEDIENTE)": son señal clásica de spam/clickbait para YouTube.
- POWER WORDS por categoría (usa al menos 1 de cada categoría a lo largo de los metadatos, no solo en el título):
  ⚡ URGENCIA / EXCLUSIVIDAD: REVELADO, FILTRADO, CENSURADO, INÉDITO, CLASIFICADO, CONFIDENCIAL, PROHIBIDO, EXCLUSIVA
  💥 IMPACTO EMOCIONAL: ESCALOFRIANTE, DESGARRADOR, INEXPLICABLE, DEMOLEDOR, SOBRECOGEDOR, ESTREMECEDOR, ALUCINANTE, ATERRADOR
  🔍 CURIOSIDAD / MISTERIO: OCULTO, SECRETO, PERTURBADOR, SINIESTRO, ENIGMÁTICO, IMPACTANTE, INCREÍBLE, INSÓLITO
- Clickbait ÉTICO: el título promete algo que el video REALMENTE entrega.
- CAPITALIZACIÓN: escribe el título como una frase normal en español — SOLO la primera letra en mayúscula y los nombres propios. NUNCA pongas cada palabra en mayúscula (Title Case). Puedes poner UNA palabra clave en MAYÚSCULAS para destacar (máximo 1 por título).
- PROHIBIDO añadir sufijos de credibilidad clickbait: (REAL), (CASO REAL), (REVELACIÓN), (IMPACTANTE), (ARCHIVOS CIA), (EXPEDIENTE). YouTube los trata como spam y multiplican el riesgo de strike. La credibilidad se construye con DETALLES concretos del título, no con etiquetas vacías. A lo sumo un sufijo factual verificable como (2026).
- El título debe crear una sensación de "TENGO que ver esto" al hacer scroll.

🧠 PSICOLOGÍA DEL CLICK (APLICADA AL TÍTULO):
- Curiosity Gap: crea una pregunta que solo se responde al hacer clic.
- Zeigarnik Effect: información incompleta → ansiedad → necesidad de cerrar el ciclo.
- Emotional Arousal (high-arousal words): sorpresa, ira, miedo, asombro → comparten más.
- Von Restorff Effect: el título debe destacar entre los demás resultados de búsqueda.
- Números impares: 20% más CTR que los pares. Usa 3, 5, 7, 9, 11.
- La credibilidad NO se declara, se demuestra: un sufijo vacío "(REAL)" no aporta información y es señal clásica de spam/clickbait. Mejor un dato concreto en el cuerpo del título.

Ejemplos de buena capitalización:
  CORRECTO: "Nadie creyó su predicción. 3 días después ocurrió"
  CORRECTO: "5 médicos vieron esto y NO pudieron explicarlo"
  CORRECTO: "El experimento que volvió LOCOS a 5 personas"
  INCORRECTO: "El Milagro Que Dejó Sin Palabras A 5 Médicos"
  INCORRECTO (empieza con artículo débil sin impacto): "La historia que nadie te contó sobre..."

📄 DESCRIPCIÓN (SEO completa):
- **LO PRIMERO de todo**: CAPÍTULOS con timestamps en formato "0:00 — Título del capítulo" (mínimo 3, máximo 8). Esta es la PRIMERA LÍNEA de la descripción, sin NINGUNA introducción previa. YouTube indexa los timestamps y los muestra como "Key Moments" en los resultados de búsqueda, lo que aumenta el CTR un 20-30%.
- **DESPUÉS de los capítulos**: PRIMERAS 2-3 LÍNEAS de resumen envolvente que explique de qué va el vídeo a alguien que NO lo ha visto. Incluye la keyword principal de forma natural.
- Luego desarrolla el tema en 1-2 párrafos cortos con keywords secundarias y propuesta de valor del vídeo.
- CTA de suscripción atractivo y natural.
- Los primeros 125 caracteres DE LA DESCRIPCIÓN (sin contar los timestamps) deben ser el HOOK principal (es lo que se ve sin expandir).
- 3-5 hashtags al final.
- Máximo 5000 caracteres total.

💡 HASHTAGS (AL FINAL DE LA DESCRIPCIÓN):
- Añade ENTRE 3 Y 5 hashtags al final de la descripción.
- El PRIMER hashtag debe ser la keyword principal del vídeo.
- Los hashtags deben reflejar ideas que un usuario podría buscar en YouTube.
- NO uses más de 15 hashtags (YouTube ignora todos si pones más de 15).
- Los 3 primeros son los que aparecen sobre el título del vídeo → elíguelos con máximo cuidado.
- Formato: #PalabraClave

🏷️ TAGS (METADATOS OCULTOS) — ESTRATEGIA LONG-TAIL:
- Entre 7 y 10 tags.
- **RELA prioridad: tags ultra-específicos y long-tail (3-5 palabras) sobre tags genéricos.**
- El PRIMER tag debe ser la keyword principal exacta (debe COINCIDIR con el primer hashtag).
- Tags 2-5: frases de búsqueda long-tail que alguien REALMENTE escribiría en YouTube para encontrar este video. Ejemplos:
  • NO uses "misterio" → usa "misterio del parque jerome nueva york"
  • NO uses "documental español" → usa "documental español fabrica abandonada 1912"
  • NO uses "historia real" → usa "historia real naufragio nao portuguesa atlantico"
- Tags 6-8: variantes, errores comunes, y búsquedas relacionadas.
- Tags 9-10: tags en inglés (si aplica) o búsquedas complementarias.
- **PROHIBIDO:** tags de una sola palabra genérica ("misterio", "historia", "documental", "real", "2025", "video viral").
- **PROHIBIDO:** poner el nombre del canal como tag (no uses "expediciones sin retorno" ni "sincronías" como tag).
- Máximo 500 caracteres en total para todos los tags juntos.

🖼️ TEXTO MINIATURA (DOS LÍNEAS DE OVERLAY):
Ahora la miniatura tiene DOS líneas de texto en vez de una:
- LÍNEA 1 (gancho principal, texto GRANDE): 1-2 palabras en MAYÚSCULAS. Máximo 12 caracteres. Debe ser la palabra o frase más impactante que haga DETENER el scroll. **DEBE contener una palabra CLAVE del título del video — NUNCA uses frases genéricas.** Formatos probados:
  • Palabra-clave del título: "SIN SANGRE", "COLAPSO", "ODÍSEA", "DEVORADA"
  • Cifra impactante: "3 MINUTOS", "NINGUNO SALIÓ", "5 MÉDICOS"
  • Afirmación extrema: "FUE REAL", "CAMBIÓ TODO"
- LÍNEA 2 (complemento, texto MEDIANO debajo de L1): 2-4 palabras. Máximo 24 caracteres. Complementa a L1 y al título sin repetirlos. Añade la pieza de intriga que falta. Formatos probados:
  • "Nadie lo explicó", "Lo que ocultaron", "La verdad sale"
  • "El informe secreto", "Dijeron que era imposible"
- Regla de oro: L1 + L2 + título deben contar una mini-historia de 3 frases que obligue a hacer clic. NINGUNA línea repite a las otras ni al título.

═══ COHERENCIA TÍTULO ↔ MINIATURA ═══
El título, L1 y L2 deben trabajar JUNTOS, sin repetirse:
- TÍTULO: promete el tema principal + el gancho ("5 médicos vieron esto y NO pudieron explicarlo").
- L1 (thumbnail): palabra-gancho extraída del título ("5 MÉDICOS" o "SIN EXPLICACIÓN").
- L2 (thumbnail): el complemento intrigante ("El informe secreto").
- Juntos deben contar una mini-historia de 3 actos que obligue a hacer clic.

Responde SIEMPRE en formato JSON con exactamente estas claves:
{
  "title": "Título viral en español (40-65 chars ideal, max 100, solo primera letra mayúscula + 1 palabra en CAPS, SIN sufijos de credibilidad clickbait tipo (REAL))",
  "title_suffix": "SIEMPRE '' (vacío). PROHIBIDO: REAL, CASO REAL, REVELACIÓN, IMPACTANTE, ARCHIVOS CIA, EXPEDIENTE.",
  "description": "CAPÍTULOS CON TIMESTAMPS EN LA PRIMERA LÍNEA + luego 2-3 líneas de resumen + desarrollo + CTAs + 3-5 hashtags al final",
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


# ── Unique keyword selection per video ──────────────────────────

# Common Spanish stop words to filter out when extracting
# keywords from titles (avoid polluting tags with articles,
# prepositions, and generic terms).
_STOP_WORDS_ES: set[str] = {
    "antes", "aunque", "cada", "como", "cuando", "donde", "este",
    "esto", "hace", "hacer", "hasta", "mucho", "nuestro", "otro",
    "para", "pero", "puede", "pueden", "será", "serán", "sobre",
    "también", "tener", "tenía", "tenían", "todo", "todos", "tiene",
    "tienen", "tuvo", "unos", "unas", "usted", "veces",
    "video", "vídeo", "viral", "está", "están",
    "entre", "desde", "hacia", "cerca", "debajo", "ahora", "nunca",
    "siempre", "tampoco", "entonces", "después", "luego", "mientras",
    "porque", "durante", "ningún", "ninguna", "ninguno", "muchos",
    "muchas", "pocos", "pocas", "varios", "varias", "algunos",
    "algunas", "cualquier", "cualquiera", "quién", "quiénes",
    "cuánto", "cuántos", "cuál", "cuáles", "dónde", "adónde",
    "cuándo", "cómo", "porqué", "hecho", "hecha", "hechos",
    "había", "habían", "sería", "serían", "estaría", "estarían",
    "podría", "podrían", "debería", "deberían", "tendrían",
    "habrían", "hubiera", "hubieran", "dicha", "dicho", "dichos",
    "dichas", "vista", "visto", "verás", "verán", "descubrir",
    "descubrió", "descubrieron", "encontrar", "encontró",
    "encontraron", "saber", "supo", "supieron", "parece", "parecía",
    "parecen", "parecían", "existe", "existía", "existían",
    "existir", "existieron", "hablar", "habla", "hablan",
}

def select_video_keywords(
    config,
    script: dict = None,
    content_text: str = "",
    min_kw: int = 5,
    max_kw: int = 15,
) -> list[str]:
    """Select a unique keyword combination for each video.

    Builds a master pool from all channel keyword sources
    (YT_DEFAULT_TAGS, SEO_SECONDARY_KEYWORDS, SEO_PRIMARY_KEYWORD,
    CHANNEL_KEYWORDS), then selects a random subset using
    content-derived entropy so each video gets a different
    combination. Also extracts eligible words from the title.

    Args:
        config: Channel config object (SimpleNamespace or module).
        script: Optional script dict with keys 'selected_title',
                'titulo', 'guion' for content entropy.
        content_text: Additional text for entropy seeding
                      (e.g. block_texts from viral cloner).
        min_kw: Minimum number of keywords to return (default 5).
        max_kw: Maximum number of keywords to return (default 15).

    Returns:
        List of unique keyword strings, 5-15 items.
    """
    # ── Build master keyword pool ──────────────────────────
    pool: set[str] = set()

    for attr in ("YT_DEFAULT_TAGS", "SEO_SECONDARY_KEYWORDS", "CHANNEL_KEYWORDS"):
        tags = getattr(config, attr, []) or []
        for t in tags:
            stripped = str(t).strip().lower()
            if stripped and len(stripped) >= 3:
                pool.add(stripped)

    primary = getattr(config, "SEO_PRIMARY_KEYWORD", "")
    if primary:
        stripped = str(primary).strip().lower()
        if stripped and len(stripped) >= 3:
            pool.add(stripped)

    pool_list = sorted(pool)  # deterministic ordering for reproducible sampling
    if not pool_list:
        logger.warning("select_video_keywords: empty keyword pool for channel — returning empty list")
        return []

    # ── Derive entropy seed from content ────────────────────
    seed_parts: list[str] = [content_text or ""]
    if script:
        title = script.get("selected_title", "") or script.get("titulo", "") or ""
        guion = script.get("guion", "") or ""
        seed_parts.append(title)
        seed_parts.append(guion[:500])

    seed_str = "|".join(seed_parts)
    if not seed_str.strip("|"):
        # Truly no content — use timestamp for unique fallback
        import time as _time
        seed_str = str(_time.time())

    seed = int(hashlib.md5(seed_str.encode()).hexdigest()[:8], 16)
    rng = random.Random(seed)

    # ── Select random subset ───────────────────────────────
    n = rng.randint(min_kw, min(max_kw, len(pool_list)))
    selected = rng.sample(pool_list, n)

    # ── Extract additional keywords from title words ────────
    if script:
        title = script.get("selected_title", "") or script.get("titulo", "")
        if title:
            clean = _re.sub(r"\([^)]*\)", "", title)   # remove parentheticals
            clean = _re.sub(r"[^\w\s]", "", clean)     # remove punctuation
            words = [w.lower() for w in clean.split()
                     if len(w) > 4 and w.isalpha()
                     and w.lower() not in _STOP_WORDS_ES]
            added = 0
            for w in words:
                if w not in selected and not any(w in s or s in w for s in selected):
                    selected.append(w)
                    added += 1
                    if len(selected) >= max_kw:
                        break
            if added:
                logger.debug("select_video_keywords: added %d title words to keyword set", added)

    # ── Ensure minimum count ───────────────────────────────
    if len(selected) < min_kw and len(pool_list) >= min_kw:
        remaining = [p for p in pool_list if p not in selected]
        if remaining:
            needed = min(min_kw - len(selected), len(remaining))
            selected.extend(rng.sample(remaining, needed))

    return selected[:max_kw]


# ── YouTube chapter timestamps ─────────────────────────────────

def generate_timestamps(scenes: list[dict], script_text: str = "", total_duration_sec: float = 0.0) -> str:
    """Generate YouTube chapter timestamps from scene data.

    Args:
        scenes: List of scene dicts with keys: position, audio_start_sec, text, description.
        script_text: Full script text for context-aware chapter naming.
        total_duration_sec: Total video duration in seconds (used to cap final timestamp).

    Returns:
        Multi-line string in YouTube chapter format::

            0:00 - Introduccion: El Misterio Comienza
            2:15 - Capitulo 1: Los Primeros Descubrimientos
            5:30 - Capitulo 2: La Evidencia Oculta

    Rules:
        - First timestamp MUST be 0:00
        - Each chapter title: 3-7 words, descriptive
        - Min 3 chapters, max 20
    """
    if not scenes:
        return "0:00 - Introduccion\n3:00 - Desarrollo\n6:00 - Conclusion"

    chapters: list[tuple[int, str]] = []

    for i, scene in enumerate(scenes):
        if isinstance(scene, dict):
            # Try to get audio start time from scene data
            start_sec = scene.get("audio_start_sec")
            if start_sec is None:
                # Estimate from position: ~40-70 seconds per scene
                start_sec = i * 60
            else:
                start_sec = int(start_sec)

            # Generate chapter title from scene description
            desc = scene.get("description") or scene.get("text") or scene.get("descripcion", "")
            if isinstance(desc, dict):
                desc = desc.get("description", "")
            desc = str(desc).strip()

            # Extract a short title: first 5-7 words from description
            if desc:
                words = desc.split()[:7]
                title = " ".join(words)
                # Capitalize first letter
                if title and title[0].islower():
                    title = title[0].upper() + title[1:]
            else:
                # Fallback titles by position
                phase_labels = [
                    "Introduccion",
                    "El Contexto",
                    "Los Hechos",
                    "El Misterio",
                    "La Revelacion",
                    "Las Consecuencias",
                    "El Legado",
                    "Conclusion",
                ]
                idx = min(i, len(phase_labels) - 1)
                title = f"Capitulo {i + 1}: {phase_labels[idx]}"
        elif isinstance(scene, str):
            start_sec = i * 60
            words = scene.split()[:7]
            title = " ".join(words) if words else f"Capitulo {i + 1}"
        else:
            start_sec = i * 60
            title = f"Capitulo {i + 1}"

        chapters.append((int(start_sec), title))

    # Ensure first chapter starts at 0:00
    if chapters and chapters[0][0] != 0:
        chapters[0] = (0, chapters[0][1])

    # Cap at 3-20 chapters
    chapters = chapters[:20]
    if len(chapters) < 3:
        # Pad to minimum 3
        last_time = chapters[-1][0] + 120 if chapters else 0
        for i in range(len(chapters), 3):
            chapters.append((last_time + (i - len(chapters)) * 90, f"Capitulo {i + 1}"))

    # Build output string
    lines = []
    for ts, title in chapters:
        # Ensure title is 3-7 words
        words = title.split()
        if len(words) > 7:
            title = " ".join(words[:7]) + "..."

        formatted_ts = _format_seconds(ts)
        lines.append(f"{formatted_ts} - {title}")

    return "\n".join(lines)


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
        self.title_max_chars = resolve_title_max_chars(canal_config)
        
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
        
        # ── Unique keyword bank per video ──────────────────────
        # Each video gets a different random subset from the
        # channel's keyword pool, ensuring tag variety.
        keyword_bank = select_video_keywords(
            self.config,
            script=script,
            content_text=keywords_str,
            min_kw=5,
            max_kw=15,
        )
        keyword_bank_str = ", ".join(keyword_bank) if keyword_bank else keywords_str
        
        # ── Real-time SEO keyword research ──────────────────────
        # Get trending keywords from Google Trends / YouTube autocomplete
        # to boost CTR with fresh, high-volume search terms.
        trending_keywords_str = ""
        optimized_tags = None
        try:
            seo = SEOResearcher(self.config.CANAL_NAME, self.config)
            trending = seo.get_trending_keywords(keywords_str)
            if trending:
                trending_keywords_str = ", ".join(trending[:10])
                logger.debug("SEO: %d trending keywords for '%s': %s",
                           len(trending), keywords_str[:30], trending_keywords_str[:80])
            # Pre-compute optimized tags (will be used as fallback if LLM tags are empty)
            base_tags = getattr(self.config, "YT_DEFAULT_TAGS", [])
            optimized_tags = seo.optimize_tags(base_tags, keywords_str)
        except Exception as exc:
            logger.debug("SEO research failed (non-critical): %s", exc)
        
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

BANCO DE KEYWORDS ÚNICAS (selección variada para este video):
{keyword_bank_str}

{"KEYWORDS TRENDING ACTUALES (DEBES incluirlas naturalmente en titulo y descripcion):" if trending_keywords_str else ""}
{"  " + trending_keywords_str if trending_keywords_str else ""}

RESUMEN DEL GUIÓN:
{script_snippet[:2500]}

ESCENAS DEL VIDEO:
{scenes_text}

INSTRUCCIONES:
1. Genera 1 ÚNICO título viral optimizado (40-65 chars): keyword al inicio, patrón de alto CTR, power words, curiosidad extrema. IMPOSIBLE de ignorar al hacer scroll. PROHIBIDO añadir sufijos de credibilidad clickbait como (REAL), (CASO REAL), (IMPACTANTE), (REVELACIÓN), (ARCHIVOS CIA), (EXPEDIENTE).
2. Deja "title_suffix" SIEMPRE vacío (""). No uses sufijos de credibilidad clickbait.
3. Crea una descripción SEO. **LOS CAPÍTULOS CON TIMESTAMPS DEBEN SER LA PRIMERA LÍNEA**, sin introducción previa ni resumen delante. YouTube indexa los timestamps como Key Moments. Después de los capítulos, añade un hook irresistible en las primeras 125 chars (sin contar timestamps), desarrollo y hashtags al final.
4. Genera 5-10 tags optimizados (keyword exacta primero, variantes después).
5. Crea DOS LÍNEAS de texto para la miniatura separadas por | : L1 (máx 12 chars, palabra-gancho en MAYÚSCULAS) | L2 (máx 24 chars, complemento intrigante). No repitas el título.
6. Define el texto para el badge/sello de la miniatura (DOCUMENTAL, CASO REAL, REAL, ARCHIVO, EXPEDIENTE, o vacío).

IMPORTANTE: Responde SOLO con el objeto JSON, sin markdown, sin texto adicional."""

        user_prompt += "\n\n" + packaging_rules(self.config)

        # ── Inject channel power words into system prompt ──────────
        power_words = getattr(self.config, "TITLE_POWER_WORDS", [])
        pw_section = build_power_words_prompt_section(power_words)
        system_prompt = _METADATA_SYSTEM_PROMPT
        if pw_section:
            system_prompt = _METADATA_SYSTEM_PROMPT + "\n" + pw_section

        client = create_llm_client(enable_thinking=False, timeout=120.0, max_retries=2)
        
        try:
            result = llm_json_call(
                client,
                max_retries=3,
                retry_delay=2.0,
                model=LLM_MODEL_CREATIVE,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=0.9,
                max_tokens=4000,
            )
            
            # Validate and sanitize — single title
            title = result.get("title", "")
            if not title or not isinstance(title, str):
                title = self._fallback_titles(script)[0]
            
            # Extract title suffix if present (LLM may or may not include it in title)
            title = _append_title_suffix(title, result.get("title_suffix", ""))
            title_suffix_norm = _normalize_title_suffix(result.get("title_suffix", ""))
            
            # Ensure title ≤ 100 chars (trim suffix if needed)
            if len(title) > self.title_max_chars:
                # Try to fit by trimming suffix first, then title
                suffix_formatted = f" ({title_suffix_norm})" if title_suffix_norm else ""
                max_title_body = self.title_max_chars - len(suffix_formatted)
                title = title[:max_title_body].rstrip() + suffix_formatted
                title = title[:self.title_max_chars]  # final safety

            # ── Safety net: enforce at least one power word ────────
            title = enforce_power_words(title, power_words, max_chars=self.title_max_chars)

            # ── Antiban (ago 2026): quitar sufijos de credibilidad clickbait
            # tipo (REAL)/(CASO REAL) por si el LLM los incluyó igualmente.
            title = _strip_clickbait_suffix(title)
            title_check = validate_title(title, self.config)

            description = result.get("description", "")
            # Truncate description to 5000 bytes
            desc_bytes = description.encode("utf-8")
            if len(desc_bytes) > 5000:
                # Truncate safely at UTF-8 boundary
                description = desc_bytes[:4997].decode("utf-8", errors="ignore") + "..."
            
            tags = result.get("tags", [])
            # Validate total tags chars ≤ 500
            tags_validated = self._validate_tags(tags[:10])
            
            # ── SEO fallback: if LLM tags are insufficient, use optimized tags ──
            if not tags_validated or len(tags_validated) < 3:
                if optimized_tags and len(optimized_tags) >= 3:
                    tags_validated = self._validate_tags(optimized_tags)
                    logger.debug("MetadataGenerator: using SEO-optimized tags (LLM tags insufficient)")
                elif keyword_bank:
                    tags_validated = self._validate_tags(keyword_bank[:10])
                    logger.debug("MetadataGenerator: using keyword bank as tag fallback")
            
            # Parse thumbnail text — new format: "L1 | L2" or legacy single line
            thumbnail_text_raw = result.get("thumbnail_text", "")
            if thumbnail_text_raw:
                thumbnail_text = _smart_overlay_text(thumbnail_text_raw).upper()
            else:
                thumbnail_text = _smart_overlay_text(title).upper() if title else _derive_hook_from_title(title)
            
            # Parse badge text for thumbnail seal
            badge_text = result.get("badge_text", "").strip().upper()
            if badge_text:
                badge_text = badge_text.strip("()（）[]")
            
            # Track token usage (estimated — retry wrapper hides raw response)
            token_count = 0
            cost_estimate = 0.0
            
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
                "packaging_validation": {"valid": title_check.valid,
                                         "reasons": list(title_check.reasons)},
            }
            
        except json.JSONDecodeError as e:
            # Should not happen with llm_json_call's internal retry, but kept as safety
            logger.error("MetadataGenerator: JSON parse failed after retries: %s", e)
            return self._fallback_metadata(script)
        except Exception as e:
            logger.error("MetadataGenerator: LLM call failed after retries: %s", e)
            return self._fallback_metadata(script)

    def _validate_tags(self, tags: list[str]) -> list[str]:
        """Validate tags fit within YouTube's 500-char limit."""
        prohibited_generic = {"misterio", "historia", "documental", "real", "2025", "video viral"}
        valid = []
        total_chars = 0
        for tag in tags:
            tag_str = str(tag).strip().lower()
            if not tag_str or len(tag_str) < 2:
                continue
            if tag_str in prohibited_generic:
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
            return [str(titles[0])[:self.title_max_chars]]
        
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

        # ── Safety net: enforce at least one power word ────────────
        power_words = getattr(self.config, "TITLE_POWER_WORDS", [])
        title = enforce_power_words(title, power_words, max_chars=self.title_max_chars)

        # ── Antiban: quitar sufijos clickbait (REAL) también en fallback ──
        title = _strip_clickbait_suffix(title)
        
        # ── Unique keyword selection per video ─────────────────────
        # Even in fallback, avoid using the same static keyword set
        # for every video.
        tags = select_video_keywords(
            self.config,
            script=script,
            content_text=title,
            min_kw=5,
            max_kw=10,
        )
        if not tags:
            # Last resort: use only script-provided, content-specific keywords.
            keywords_raw = script.get("keywords") or script.get("keywords_json", "[]")
            if isinstance(keywords_raw, str):
                try:
                    keywords = json.loads(keywords_raw)
                except json.JSONDecodeError:
                    keywords = []
            else:
                keywords = keywords_raw or []
            tags = keywords[:10] if keywords else []
        
        # Build basic description from template
        channel_desc = getattr(self.config, "DESCRIPTION_TEMPLATE", "")
        if channel_desc and "{titulo}" in channel_desc:
            # ── v24: related_videos placeholder (cross-promotion) ──
            related = script.get("related_videos_text", "")
            if not related:
                related = (
                    "👉 Mira tambien nuestros documentales mas recientes en el canal\n"
                    "👉 Suscribete para mas expediciones reales cada semana"
                )
            description = channel_desc.format(
                titulo=title,
                descripcion_seo=script.get("descripcion_seo", ""),
                chapters="0:00 — Introducción\n2:00 — Desarrollo\n5:00 — Conclusión",
                related_videos=related,
            )
        else:
            description = f"{title}\n\nUna historia que te dejará sin palabras...\n\n#historias #documental"
        
        return {
            "titles": titles,
            "selected_title": title,
            "description": description[:5000],
            "tags": self._validate_tags(tags),
            "thumbnail_text": _derive_hook_from_title(title),
            "badge_text": "DOCUMENTAL",
            "category_id": self.yt_category_id,
            "token_count": 0,
            "cost_estimate": 0.0,
        }

    # ── A/B Testing: Alternative Title Generation ────────────────

    def generate_alternative_title(
        self,
        title_v1: str,
        script_text: str,
        keywords: list[str],
        ctr_v1: float,
    ) -> str:
        """Generate an alternative title for A/B testing when CTR is low.

        Strategy:
        1. Classify title_v1's formula type (question, curiosity_gap, shock, etc.)
        2. Pick a DIFFERENT formula type
        3. Generate title_v2 with the same length and keywords, but
           different angle — no repeated power words.

        Args:
            title_v1: Original title (low CTR).
            script_text: Full script text for content context.
            keywords: Primary keywords to preserve.
            ctr_v1: Current CTR (as percentage, e.g. 2.1 = 2.1%).

        Returns:
            Alternative title string. Falls back to title_v1 on error.
        """
        import time

        start = time.time()
        
        # Build keywords string
        kw_str = ", ".join(keywords[:10]) if keywords else "historias, documental, misterio"
        
        # Get power words from channel config
        power_words = getattr(self.config, "TITLE_POWER_WORDS", [])
        pw_str = ", ".join(power_words[:20]) if power_words else ""
        
        # Build formula type classifier
        formula_classifier = self._classify_title_formula(title_v1)
        
        system_prompt = """Eres un copywriter experto en YouTube SEO especializado en A/B testing de títulos.

Tu trabajo: generar un título ALTERNATIVO para un video cuyo CTR (Click-Through Rate) está bajo.

REGLAS CRÍTICAS:
1. El título nuevo DEBE tener un enfoque DIFERENTE al original.
2. Si el original era pregunta → nuevo debe ser afirmación/de dato
3. Si el original era misterio/curiosidad → nuevo debe ser urgencia/revelación
4. Si el original era shock → nuevo debe ser curiosidad/documental
5. MISMA longitud (caracteres), mismas keywords principales
6. NO repetir las mismas power words del título original
7. NO hacer el título más largo — mismo rango de caracteres

Devuelve SOLO el título nuevo, sin comillas ni explicaciones."""

        user_prompt = f"""CANAL: {self.channel_name}
TONO: {self.channel_tone}

TÍTULO ORIGINAL (CTR bajo: {ctr_v1}%):
"{title_v1}"

FÓRMULA DETECTADA: {formula_classifier}

KEYWORDS PRINCIPALES: {kw_str}
POWER WORDS DEL CANAL: {pw_str}

CONTENIDO DEL VIDEO: {script_text[:1500]}

INSTRUCCIÓN: Genera un título alternativo con enfoque OPUESTO al original.
Si era pregunta → dato. Si era misterio → revelación. Si era shock → curiosidad.
Mantén MISMA longitud. NO uses las mismas power words."""

        try:
            from datetime import datetime as _dt
            import hashlib as _hashlib
            
            # Seed for unique generation
            seed = int(_hashlib.md5(f"{title_v1}{_dt.now().isoformat()}".encode()).hexdigest()[:8], 16)
            
            result = llm_json_call(
                create_llm_client(enable_thinking=False, timeout=45.0, max_retries=2),
                model=LLM_MODEL_CREATIVE,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=0.85,
                max_tokens=150,
                max_retries=2,
                retry_delay=1.0,
            )
            
            if result and isinstance(result, dict):
                alt_title = result.get("alternative_title", "") or result.get("title", "") or ""
                if alt_title and len(alt_title) >= 20:
                    # Enforce power words on the new title
                    alt_title = enforce_power_words(
                        alt_title, power_words, max_chars=self.title_max_chars
                    ) if power_words else alt_title[:self.title_max_chars]
                    elapsed = time.time() - start
                    logger.info(
                        "Alternative title generated in %.1fs: '%s' → '%s' (CTR %.1f%%)",
                        elapsed, title_v1[:40], alt_title[:60], ctr_v1,
                    )
                    return alt_title[:100]
            
        except Exception as exc:
            logger.warning("Alternative title generation failed: %s — falling back to original", exc)

        # Fallback: return a tweaked version of the original
        logger.info("Fallback alternative title: applying prefix transformation to original")
        fallback = title_v1
        # Try simple transformations as last resort
        if "?" in fallback:
            fallback = fallback.replace("?", ". La verdad que NADIE contó.")
        elif fallback.startswith("El ") or fallback.startswith("La "):
            fallback = "Esto es " + fallback[0].lower() + fallback[1:]
        
        return fallback[:100]

    def _classify_title_formula(self, title: str) -> str:
        """Classify a title into its dominant formula type.
        
        Returns one of: question, curiosity_gap, shock, urgency, list,
                        how_to, statement, revelation, warning
        """
        t = title.strip()
        
        if "?" in t:
            return "question"
        if any(w in t.lower() for w in ["revelado", "filtrado", "censurado", "prohibido", "secreto", "exclusiva"]):
            return "revelation"
        if any(w in t.lower() for w in ["impactante", "shock", "aterrador", "horror", "pesadilla"]):
            return "shock"
        if any(w in t.lower() for w in ["urgente", "última hora", "ahora", "no verás", "antes de"]):
            return "urgency"
        if any(c.isdigit() for c in t[:10]) and any(w in t.lower() for w in ["cosas", "casos", "datos", "secretos", "razones", "historias"]):
            return "list"
        if t.lower().startswith("cómo") or t.lower().startswith("como"):
            return "how_to"
        if any(w in t for w in ["nadie", "nunca", "jamás", "imposible"]):
            return "curiosity_gap"
        return "statement"


def _normalize_title_suffix(raw_suffix: str) -> str:
    """Normaliza el campo title_suffix del LLM a una palabra limpia en mayúsculas.

    (antiban, ago 2026): los sufijos de credibilidad clickbait (REAL, CASO REAL,
    REVELACIÓN, IMPACTANTE, ARCHIVOS CIA, EXPEDIENTE) se neutralizan a '' para
    que NUNCA se añadan al título.
    """
    suffix = (raw_suffix or "").strip().upper().strip("()（）[]")
    if " ".join(suffix.lower().split()) in _SPAM_TITLE_SUFFIXES:
        return ""
    return suffix


# (antiban, ago 2026): sufijos de credibilidad clickbait — señal clásica de spam
# de YouTube. Normalización en minúsculas, palabras unidas por un espacio.
_SPAM_TITLE_SUFFIXES = frozenset({
    "real", "caso real", "revelación", "revelacion", "impactante",
    "archivos cia", "expediente",
})


def _strip_clickbait_suffix(title: str) -> str:
    """Quita sufijos de credibilidad clickbait del FINAL del título.

    Maneja sufijos repetidos "(REVELACIÓN) (REAL)" y colas decorativas tras el
    sufijo: "(REAL). ASOMBROSO.", "(REAL) [CLASIFICADO]", "(REAL) — El Misterio
    Antediluviana", "(REAL) — ¿Emergió?". No toca años "(2026)" ni otros
    paréntesis no spam en el cuerpo del título.
    """
    if not title:
        return title
    text = title.strip()
    while True:
        match = _SPAM_SUFFIX_TAIL_RE.search(text)
        if not match:
            break
        suffix_words = " ".join(
            _re_spam.findall(match.group("suffix").lower())
        )
        if suffix_words not in _SPAM_TITLE_SUFFIXES:
            break
        text = text[: match.start()].rstrip()
    return text.strip(" .·•–—-")


_SPAM_SUFFIX_TAIL_RE = _re.compile(
    r"(?P<suffix>\s*\([^()]+\)\s*)"
    r"(?P<tail>(?:\.\s+[^\n()]{1,60}|\[[^\n()]{1,40}\]|\s*[—–-]\s*[^\n()]{1,80}))?\s*$",
    _re.IGNORECASE | _re.DOTALL,
)
_re_spam = _re.compile(r"[a-záéíóúüñ0-9]+")


def _append_title_suffix(title: str, raw_suffix: str) -> str:
    """Añade el sufijo parentético normalizado al título, sin duplicar.

    La comparación es case-insensitive para que un sufijo ya presente en el
    título (p.ej. "(Impactante)") no desencadene un segundo paréntesis
    "(IMPACTANTE)" cuando el LLM rellena también el campo title_suffix.
    """
    suffix = _normalize_title_suffix(raw_suffix)
    if not suffix:
        return title
    suffix_formatted = f" ({suffix})"
    if title.rstrip().lower().endswith(suffix_formatted.lower()):
        return title
    return title.rstrip() + suffix_formatted
