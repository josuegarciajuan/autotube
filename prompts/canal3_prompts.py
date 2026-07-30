"""GPT prompt templates for Canal 3: Civilizaciones Olvidadas.

System and user prompts instructing GPT to generate video-essay
documentary scripts about lost civilisations, archaeological
mysteries, and ancient ruins, combining Wikipedia articles,
Atlas Obscura entries, Reddit threads, and RSS feeds into
cinematic narratives of discovery, awe and historical rigour.

v1: Block-based script generation with per-block media search queries,
     emotional voice mapping, and hybrid video/image media strategy.
"""

from config import canal3_config as _default_config


def _extract_structure_text(cfg) -> str:
    """Build the narrative structure section from the config."""
    structure = getattr(cfg, "SCRIPT_STRUCTURE", [])
    if not structure:
        return ""

    lines = []
    for i, item in enumerate(structure):
        if isinstance(item, dict):
            step = item.get("step", f"Paso {i+1}")
            desc = item.get("description", "")
            anchor = item.get("retention_anchor", "")
            time_pct = item.get("time_pct", "")
            time_str = f" [{time_pct}]" if time_pct else ""
            lines.append(f"   ({chr(97 + i)}) {step}{time_str}: {desc}")
            if anchor:
                lines.append(f"       ⚡ ANCLA DE RETENCIÓN: {anchor}")
        else:
            lines.append(f"   ({chr(97 + i)}) {str(item)}")
    return "\n".join(lines)


def _extract_emotions_text(cfg) -> str:
    """Build the emotional arc description."""
    arc = getattr(cfg, "SCRIPT_EMOTIONAL_ARC", {})
    if isinstance(arc, dict):
        return ", ".join(f"{k}→{v}" for k, v in arc.items())
    if isinstance(arc, list):
        return ", ".join(arc)
    return "curiosidad, asombro, intriga, misterio, revelación, solemnidad, reflexión"


def _build_block_rules(cfg, theme_context=None, word_target=None) -> str:
    """Build the bloque structure rules for the prompt.

    block duration is now computed dynamically from the video duration target
    so that longer videos naturally get longer blocks (vs the old hardcoded
    10-30 s which forced every block to be ~25-75 words max).
    """
    tts = getattr(cfg, "TTS_STRATEGY", {})
    media = getattr(cfg, "MEDIA_STRATEGY", {})

    video_min = media.get("video_min_duration", 4)
    video_max = media.get("video_max_duration", 20)

    # ── Scene duration envelope (matching SCENE_DURATION_MIN/MAX in video_editor) ──
    SCENE_MIN_SEC = 5
    SCENE_MAX_SEC = 12

    # ── Dynamic block duration from word_target (clamped to 5-12s envelope) ──
    if word_target and "duration_target" in word_target and word_target.get("duration_target", 0) >= 2:
        dur_min = word_target["duration_target"]
        blk_min = max(3, word_target.get("blocks_min", 8))
        blk_max = max(4, word_target.get("blocks_max", 15))
        avg_blocks = (blk_min + blk_max) / 2.0
        ideal_sec = (dur_min * 60.0) / avg_blocks
        block_dur_min = max(SCENE_MIN_SEC, min(SCENE_MAX_SEC, int(ideal_sec * 0.55)))
        block_dur_max = max(block_dur_min + 3, min(SCENE_MAX_SEC, int(ideal_sec * 1.3)))
        words_per_block_min = max(12, int(ideal_sec * 2.5 * 0.35))
        min_frases = max(1, int(ideal_sec / 15))
        max_frases = max(min_frases + 1, int(ideal_sec / 8))
    else:
        block_dur_min, block_dur_max = SCENE_MIN_SEC, SCENE_MAX_SEC
        words_per_block_min = 15
        min_frases, max_frases = 1, 2

    block_dur_range = f"{block_dur_min}-{block_dur_max} segundos"

    theme_rules = ""
    if theme_context and theme_context.theme_keywords_en:
        theme_kw_str = ", ".join(theme_context.theme_keywords_en[:5])
        theme_rules = f"""
REGLAS DE COHERENCIA VISUAL (¡OBLIGATORIO!):
- CADA search_query_en debe ser una FUSIÓN de DOS partes (ambas obligatorias, en este orden):
  (1) SUJETO NARRATIVO (VA PRIMERO, ~60% de la query): 2-3 keywords que describen
      EXACTAMENTE lo que se narra en ESTE bloque — la civilización, lugar, artefacto,
      construcción o descubrimiento mencionado en la narración. Esto es el sujeto principal.
  (2) AMBIENTACIÓN TEMÁTICA (VA DESPUÉS, ~40% de la query): 1-2 keywords de época/ambiente
      extraídas de: {theme_kw_str}. Esto ancla la escena en la civilización/era correcta.
  ✅ BUENO: "stone carvings close up ancient temple mayan ruins golden hour"
          → narra las tallas de piedra → anclado en ruinas mayas
  ❌ MALO: "ancient ruins historical civilization mystery"
          → solo tema genérico, NO refleja lo que se narra
  ❌ MALO: "modern architecture glass building"
          → describe la acción pero FUERA de época (rompe el contexto)
- LO QUE VES = LO QUE OYES: la query debe traducir visualmente lo que el locutor
  está narrando en este momento exacto. Si la narración dice "los arqueólogos encontraron
  jeroglíficos en la cámara funeraria", la query debe ser sobre "hieroglyphics burial
  chamber ancient temple" — NO sobre "ancient egypt archaeology" genérico.
- PROHIBIDO que dos escenas consecutivas usen la MISMA keyword de anclaje temático.
  Rota entre las keywords disponibles: {theme_kw_str}
- Las escenas consecutivas deben compartir al menos UN elemento visual (color, luz,
  textura de piedra, tipo de ruina, luz de antorcha) para crear un HILO VISUAL
  que una todo el video. No pueden saltar bruscamente entre civilizaciones.
- PROHIBIDO: {', '.join(theme_context.forbidden_elements) if theme_context.forbidden_elements else 'elementos modernos, edificios actuales, tecnología, personas con ropa contemporánea, vehículos'}"""

    return f"""ESTRUCTURA DE BLOQUES NARRATIVOS:
El guion debe organizarse en bloques semánticos cohesivos. Cada bloque es un párrafo completo ({block_dur_range} de narración) que forma una unidad de significado. Para cada bloque debes generar:

- "tipo": uno de ["hook", "desarrollo", "climax", "reflexion", "cierre"]
- "emocion": la emoción dominante del bloque (curiosidad, asombro, intriga, misterio, solemnidad, revelación, reflexión, maravilla, admiración)
- "texto": el texto exacto que narra el locutor en este bloque (sin marcadores, solo texto limpio)
- "escena_descripcion": descripción cinematográfica DETALLADA de qué se ve en pantalla durante este bloque. Mencionar: tipo de plano (primer plano, plano general, picado, travelling), iluminación (hora dorada, luz de antorcha, contraluz, rayos de sol entre ruinas), ambiente, objetos, colores dominantes (tierras, dorados, ocres, piedras)
- "search_query_en": entre 5 y 8 keywords en INGLÉS para buscar el visual en bancos de stock (Unsplash, Pexels). NADA de adjetivos abstractos ("beautiful", "amazing"). Usar términos concretos y visuales. Incluir estilo: "golden hour", "ancient ruins", "archaeological site", "stone texture", "dust motes" según aplique
- "media_tipo": "video" si el plano tiene movimiento natural (drone sobre ruinas, time-lapses de amaneceres, nubes sobre templos, agua fluyendo, polvo en el aire, tracking shots). "imagen" si el plano es estático (mapas antiguos, jeroglíficos, artefactos, estatuas, documentos, manuscritos)
- "media_duracion": duración ideal del clip en segundos (entre {video_min} y {video_max} si es video; mismo valor que la duración estimada si es imagen)

REGLAS PARA search_query_en:
- FORMATO OBLIGATORIO DE DOS PARTES (ambas necesarias, en este orden):
  (1) [SUJETO NARRATIVO]: 2-4 keywords del contenido EXACTO del bloque
      (civilización, lugar, artefacto, descubrimiento mencionado en la narración)
  (2) [ANCLAJE ÉPOCA/ESTILO]: 1-2 keywords de ambientación temática del video
  Ej correcto: "hieroglyphics burial chamber ancient egyptian temple golden hour"
  Ej INCORRECTO: "ancient egypt pharaoh pyramid history" (solo keywords temáticas, NO refleja lo que se narra)
- LO QUE VES = LO QUE OYES: si el bloque narra "los arqueólogos descubrieron una
  cámara oculta bajo la pirámide", la query debe ser sobre "hidden chamber beneath
  pyramid archaeological discovery", NO sobre "egyptian pyramids ancient history".
- SIEMPRE en inglés (las APIs de stock funcionan mejor en inglés)
- Equilibra especificidad con disponibilidad: "roman colosseum ancient architecture" (OK) vs "Colosseum Rome Italy 80 AD" (demasiado específico)
- Keywords concretas: "ancient temple ruins golden hour", "stone carvings close-up", "drone shot archaeological site", "dust particles in sunbeams"
- Incluir modificadores de estilo: "cinematic", "ancient atmosphere", "golden hour", "archaeological", "16:9"
- Para video, añadir: "drone shot", "slow motion", "tracking shot", "time lapse" según el plano
- Evitar términos que requieran personas específicas, marcas o material con copyright
- Si el bloque es conceptual (datos, reflexiones), usar metáforas visuales: "sunlight breaking through ancient columns" en lugar de "enlightenment"
- LIMITARSE a términos que EXISTAN en bancos de stock gratuitos (Unsplash, Pexels, Pixabay)

REGLAS PARA media_tipo:
- "video" SOLO cuando el concepto visual tenga movimiento natural y abundante en stock
- "imagen" para conceptos visuales muy específicos, artefactos, o con poca oferta de video
- Aproximadamente 30-40% de los bloques deberían ser "video" (drone de ruinas, time-lapses)
- Si dudas, elige "imagen" (más seguro, siempre hay fallback)

REGLAS DE FRAGMENTACIÓN:
- Cada bloque debe durar entre 5 y 12 segundos de narración (aproximadamente 15-35 palabras).
- Divide el contenido en bloques cortos y concisos. No agrupes demasiadas ideas en un mismo bloque.
- Si una idea requiere más de 12 segundos, divídela en varios bloques consecutivos.
- Los bloques deben tener search_query_en de 4-7 palabras clave en inglés, describiendo exactamente lo que aparece en pantalla durante ese bloque.
- Mantén coherencia visual entre bloques consecutivos.
- Entre bloques de un mismo párrafo, la descripción visual debe fluir naturalmente.
- Los bloques consecutivos deben tener progresión visual coherente (plano general de ruinas → primer plano de piedras → detalle de inscripciones → plano de situación geográfica).{theme_rules}"""


def build_content_only_prompt(config=None, previous_blocks: list = None, word_guidance: int = 300, source_text: str = None, outline: dict = None, batch_num: int = 0) -> str:
    """Lightweight prompt for sequential block-by-block content generation.

    Strips ALL structural requirements so the LLM focuses exclusively
    on writing compelling narrative content.

    Args:
        config: Canal config module.
        previous_blocks: List of previously generated blocks for continuity.
        word_guidance: Approximate word count to aim for in this batch.
        source_text: Source content to draw from.

    Returns:
        System prompt string (~300 tokens instead of ~3000).
    """
    cfg = config or _default_config
    tone = getattr(cfg, "CANAL_TONE", "Grave, misterioso y envolvente.")
    style = getattr(cfg, "CANAL_NARRATIVE_STYLE", "documental arqueológico")
    audience = getattr(cfg, "TARGET_AUDIENCE", "público LATAM adulto curioso")

    context_text = ""
    if previous_blocks:
        last_texts = []
        for b in previous_blocks[-6:]:
            if isinstance(b, dict):
                last_texts.append(b.get("texto", ""))
        all_text = " ".join(last_texts)
        first_words = " ".join(
            b.get("texto", "") for b in (previous_blocks[:3] if len(previous_blocks) >= 3 else previous_blocks)
            if isinstance(b, dict)
        )[:200]
        context_text = (
            f"\n\n--- CONTINUIDAD NARRATIVA ---\n"
            f"La historia empezó así: \"{first_words}...\"\n"
            f"Lo ÚLTIMO que narraste: \"{all_text[-400:]}\"\n\n"
            f"INSTRUCCIONES DE CONTINUIDAD:\n"
            f"- Continúa la narración desde donde quedó, de forma NATURAL.\n"
            f"- El texto debe fluir como si fuera un solo documento escrito.\n"
            f"- AVANZA la historia: cada bloque debe aportar contenido GENUINAMENTE NUEVO.\n"
            f"  PROHIBIDO repetir los mismos ejemplos, metáforas o analogías ya usadas.\n"
            f"  Si ya mencionaste templos, proporciones o calendarios, NO los uses otra vez.\n"
            f"- Si has cubierto ya un aspecto del tema, explora otro ángulo DISTINTO.\n"
            f"- Los bloques de cierre deben SINTETIZAR (no repetir) lo ya dicho.\n"
            f"- Mantén el mismo tono y estilo que los bloques anteriores.\n"
        )

    source_context = ""
    if source_text:
        source_context = f"\nCONTENIDO FUENTE (úsalo como base):\n{source_text[:2000]}\n"

    return f"""Eres un guionista documental para YouTube especializado en arqueología, civilizaciones antiguas y misterios históricos. Escribe narraciones en español latinoamericano neutro.

TONO: {tone}
ESTILO: "{style}" — documentales que evocan exploración, asombro y rigor histórico.
AUDIENCIA: {audience}

REGLAS ESTRICTAS:
1. Español latinoamericano neutro. PROHIBIDO "vosotros", "os", conjugaciones ibéricas.
2. NO inventes datos arqueológicos. Usa SOLO la información de las fuentes.
3. Cada bloque debe ser un PÁRRAFO COMPLETO y sustancial (no frases sueltas).
4. Incluye detalles sensoriales, descripciones de lugares, fechas y contexto.
5. NO uses relleno ni repeticiones. Cada bloque aporta contenido GENUINAMENTE NUEVO. PROHIBIDO repetir los mismos ejemplos, metáforas o analogías en bloques diferentes. Si ya mencionaste un sitio arqueológico o concepto, NO lo uses otra vez.
6. El tono debe ser solemne, evocador, como un explorador narrando su descubrimiento.
7. ENGANCHE INICIAL: Los primeros bloques deben ser ALTAMENTE intrigantes. Plantea un misterio, un dato impactante o una pregunta que el espectador NECESITE ver respondida. NUNCA empieces con frases como "En este video vamos a..." o "Hoy exploraremos...". Entra directo al contenido más fascinante.{source_context}{context_text}

Genera entre 2 y 4 bloques narrativos (~{word_guidance} palabras total).
Cada bloque SOLO necesita el campo "texto" (el párrafo que narrará el locutor).

Responde ÚNICAMENTE con JSON: {{"bloques": [{{"texto": "párrafo completo aquí..."}}, ...]}}
Sin explicaciones, sin markdown, sin texto fuera del JSON."""


def build_system_prompt(config=None, word_count_emphasis: float = 1.0, chunk_context: dict = None, theme_context=None, word_target: dict = None) -> str:
    """Build the system prompt from channel configuration.

    Args:
        config: Canal config module (defaults to canal3_config).
        word_count_emphasis: Multiplier for min word count on retry (1.0 normal, 1.5, 2.0 retry).
        chunk_context: Dict with multi-chunk info (order, total, last_paragraph).
        theme_context: ThemeContext from ThemeExtractor with visual coherence data.
        word_target: Optional precomputed target dict from ScriptGenerator._get_word_target().

    Returns:
        Complete system prompt string for GPT.
    """
    cfg = config or _default_config

    # ── Core identity ────────────────────────────────────────
    tone = getattr(cfg, "CANAL_TONE", "Grave, misterioso y envolvente.")
    style = getattr(cfg, "CANAL_NARRATIVE_STYLE", "documental arqueológico")
    style_desc = getattr(cfg, "CANAL_STYLE_DESCRIPTION", "")
    audience = getattr(cfg, "TARGET_AUDIENCE", "público LATAM adulto curioso")
    outro = getattr(cfg, "CANAL_OUTRO_TAGLINE", "El pasado nunca desaparece del todo. Solo espera a ser descubierto.")
    hook_rule = getattr(cfg, "SCRIPT_HOOK_RULE", "Hook en los primeros segundos.")

    # ── Duration / word guidance ─────────────────────────────
    test_mode = getattr(cfg, "TEST_MODE", False)
    if test_mode:
        duration_target = getattr(cfg, "TEST_VIDEO_DURATION_TARGET", 2)
        words_guide = f"~{getattr(cfg, 'TEST_SCRIPT_WORDS_MIN', 200)}-{getattr(cfg, 'TEST_SCRIPT_WORDS_MAX', 600)}"
        mode_banner = f"\nMODO PRUEBA: guion corto de {duration_target} min (~{words_guide} palabras).\n"
    elif word_target and "duration_target" in word_target:
        duration_target = word_target["duration_target"]
        words_guide = f"~{word_target['words_min']}-{word_target['words_max']}"
        mode_banner = ""
    else:
        duration_target = getattr(cfg, "VIDEO_AVERAGE_DURATION_MIN", 15)
        words_guide = f"~{int(duration_target * 150 * 0.85)}-{int(duration_target * 150 * 1.15)}"
        mode_banner = ""

    # ── Chunk context ────────────────────────────────────────
    chunk_banner = ""
    if chunk_context:
        chunk_banner = (
            f"\nCONTINUACION: capitulo {chunk_context.get('order', '?')} "
            f"de {chunk_context.get('total', '?')}. "
            f"Enlaza con: \"{chunk_context.get('last_paragraph', '')}\"\n"
        )

    # ── Theme context ────────────────────────────────────────
    theme_banner = ""
    if theme_context:
        theme_banner = (
            f"\nCONTEXTO VISUAL: genero={theme_context.genre}, epoca={theme_context.era}, "
            f"estilo={theme_context.visual_style}. "
            f"Keywords: {', '.join(theme_context.theme_keywords_en[:5])}. "
            f"Prohibido: {', '.join(theme_context.forbidden_elements) if theme_context.forbidden_elements else 'ninguno'}.\n"
        )

    # ── Build the simplified prompt (v22) ────────────────────
    return f"""Eres un guionista de documentales. Escribes guiones para video-ensayos de YouTube en español latinoamericano neutro. Tu especialidad: arqueologia, civilizaciones antiguas, ciudades perdidas y misterios historicos.{mode_banner}{chunk_banner}

TONO: {tone}
ESTILO: {style} — {style_desc if style_desc else 'Riguroso en los datos, cinematografico en la narracion.'}
AUDIENCIA: {audience}
{theme_banner}
REGLAS ESENCIALES:

1. ESPAÑOL LATINOAMERICANO. Nada de vosotros, os, conjugaciones ibericas. Usa ustedes, tu o usted.

2. HOOK IMPACTANTE. La primera frase debe ser un dato demoledor, un hallazgo concreto con fecha y lugar, o un misterio intrigante. NUNCA: "Hola", "Bienvenidos", "En este video", "Hoy exploraremos". Entra directo al descubrimiento mas fascinante.

3. NO INVENTES DATOS. Fechas, nombres de arqueologos, ubicaciones y hallazgos deben ser fieles a las fuentes. Si hay debate academico, menciona las distintas teorias. El rigor historico es fundamental.

4. PROGRESION NARRATIVA. Cada seccion debe aportar informacion NUEVA que haga avanzar la historia. Nada de repetir ideas con sinonimos. Si no tienes contenido nuevo, termina antes.

5. ESTRUCTURA CLARA. El guion debe tener: introduccion impactante con el descubrimiento, desarrollo con datos arqueologicos concretos, climax con la revelacion o misterio, y cierre reflexivo.

6. CIERRE. El final debe incluir: \"{outro}\" como reflexion de cierre, pero NO incluyas llamadas a la accion (suscribete, like, etc.) — eso se añade automaticamente.

7. LONGITUD. Apunta a {duration_target} minutos de video ({words_guide} palabras). Es una guia, no una regla rigida — prioriza calidad sobre cantidad.

Responde exclusivamente con JSON valido, sin markdown, sin explicaciones fuera del JSON."""

# Legacy constant for backwards compatibility
SYSTEM_PROMPT = build_system_prompt()


USER_PROMPT_TEMPLATE = """Título de la fuente: {title}
Origen: {source}
Subreddit: {subreddit}
Puntuación/Relevancia: {score}
Categoría del hallazgo: {category}

Contenido original:
---
{text}
---

Transforma el contenido anterior en un guion documental de video-ensayo sobre este descubrimiento arqueológico o civilización perdida, siguiendo TODAS las reglas del sistema.
Si el contenido describe múltiples sitios o civilizaciones, enfócate en el más impactante o enigmático y menciona brevemente los otros en la conclusión.

Genera ÚNICAMENTE el JSON de respuesta."""


def format_user_prompt(content_item: dict) -> str:
    """Format the user prompt template with content item fields.

    Args:
        content_item: Dict with keys: title, source, subreddit, score, text, category.

    Returns:
        Formatted user prompt string.
    """
    return USER_PROMPT_TEMPLATE.format(
        title=content_item.get("title", "Sin título"),
        source=content_item.get("source", "desconocida"),
        subreddit=content_item.get("subreddit", "N/A"),
        score=content_item.get("score", 0),
        category=content_item.get("category", "civilizaciones antiguas"),
        text=content_item.get("text", ""),
    )
