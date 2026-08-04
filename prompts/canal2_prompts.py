"""GPT prompt templates for Canal 2: Sincronías (Milagros y Casualidades).

System and user prompts instructing GPT to generate video-essay
documentary scripts about real miracles, impossible coincidences,
and unexplained phenomena, combining Wikipedia articles and Reddit
threads into cinematic narratives of wonder.

v2: Block-based script generation with per-block media search queries,
     emotional voice mapping, and hybrid video/image media strategy.
"""

from config import canal2_config as _default_config


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
    return "asombro, curiosidad, intriga, anticipación, estupefacción, reflexión"


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
    # 150 words / minute is the canonical rate used everywhere.
    if word_target and "duration_target" in word_target and word_target.get("duration_target", 0) >= 2:
        dur_min = word_target["duration_target"]          # float, minutes
        blk_min = max(3, word_target.get("blocks_min", 8))
        blk_max = max(4, word_target.get("blocks_max", 15))
        avg_blocks = (blk_min + blk_max) / 2.0
        ideal_sec = (dur_min * 60.0) / avg_blocks
        # Clamp to 5-12s envelope — blocks are now short and punchy
        block_dur_min = max(SCENE_MIN_SEC, min(SCENE_MAX_SEC, int(ideal_sec * 0.55)))
        block_dur_max = max(block_dur_min + 3, min(SCENE_MAX_SEC, int(ideal_sec * 1.3)))
        # 150 words/min → 2.5 words/sec; target ~40% of ideal per-block word count as minimum
        words_per_block_min = max(12, int(ideal_sec * 2.5 * 0.35))
        # fragmentation guidance
        min_frases = max(1, int(ideal_sec / 15))
        max_frases = max(min_frases + 1, int(ideal_sec / 8))
    else:
        # Test-mode / tiny-video fallback
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
      EXACTAMENTE lo que se narra en ESTE bloque — la persona, acción, lugar u objeto
      mencionado en la narración. Esto es el sujeto principal de la búsqueda.
  (2) AMBIENTACIÓN TEMÁTICA (VA DESPUÉS, ~40% de la query): 1-2 keywords de época/ambiente
      extraídas de: {theme_kw_str}. Esto ancla la escena en el mundo del video.
  ✅ BUENO: "royal physician examining patient medieval castle torchlight"
          → narra al médico medieval → anclado en castillo medieval
  ❌ MALO: "medieval castle ancient history dark atmosphere"
          → solo tema genérico, NO refleja lo que se narra
  ❌ MALO: "doctor medical examination hospital modern"
          → describe la acción pero FUERA de época (rompe el contexto)
- LO QUE VES = LO QUE OYES: la query debe traducir visualmente lo que el locutor
  está narrando en este momento exacto. Si la narración dice "el rey desenvainó
  su espada", la query debe ser sobre una espada siendo desenvainada en un castillo
  medieval — NO sobre "rey medieval genérico".
- PROHIBIDO que dos escenas consecutivas usen la MISMA keyword de anclaje temático.
  Rota entre las keywords disponibles: {theme_kw_str}
- Las escenas consecutivas deben compartir al menos UN elemento visual (color,
  iluminación, material arquitectónico, tipo de locación) para crear un HILO VISUAL
  que una todo el video. No pueden saltar bruscamente.
- PROHIBIDO mostrar elementos de: {', '.join(theme_context.forbidden_elements) if theme_context.forbidden_elements else 'ninguno'}"""

    return f"""ESTRUCTURA DE BLOQUES NARRATIVOS:
El guion debe organizarse en bloques semánticos cohesivos. Cada bloque es un párrafo completo ({block_dur_range} de narración) que forma una unidad de significado. Para cada bloque debes generar:

- "tipo": uno de ["hook", "desarrollo", "climax", "reflexion", "cierre"]
- "emocion": la emoción dominante del bloque (asombro, curiosidad, empatía, intriga, anticipación, estupefacción, esperanza, inspiración, reflexión, gratitud, maravilla)
- "texto": el texto exacto que narra el locutor en este bloque (sin marcadores, solo texto limpio)
- "escena_descripcion": descripción cinematográfica DETALLADA de qué se ve en pantalla durante este bloque. Mencionar: tipo de plano (primer plano, plano general, picado), iluminación (hora dorada, luz cálida, contraluz, rayos de luz), ambiente, objetos, colores dominantes
- "search_query_en": entre 5 y 8 keywords en INGLÉS para buscar el visual en bancos de stock (Unsplash, Pexels). NADA de adjetivos abstractos ("beautiful", "amazing"). Usar términos concretos y visuales. Incluir estilo: "golden hour", "cinematic lighting", "warm atmosphere", "ethereal light", "sunrise" según aplique
- "media_tipo": "video" si el plano tiene movimiento natural (paisajes, amaneceres, nubes, agua, multitudes, time-lapses, tracking shots, gente caminando, luz filtrándose, reflejos). "imagen" si el plano es estático (documentos, retratos, objetos fijos, gráficos, siluetas, fotografías antiguas)
- "media_duracion": duración ideal del clip en segundos (entre {video_min} y {video_max} si es video; mismo valor que la duración estimada si es imagen)

REGLAS PARA search_query_en:
- FORMATO OBLIGATORIO DE DOS PARTES (ambas necesarias, en este orden):
  (1) [SUJETO NARRATIVO]: 2-4 keywords del contenido EXACTO del bloque
      (persona, acción, lugar, objeto mencionado en la narración)
  (2) [ANCLAJE ÉPOCA/ESTILO]: 1-2 keywords de ambientación temática del video
  Ej correcto: "sword drawn betrayal medieval castle" (narra la espada → anclado en castillo)
  Ej INCORRECTO: "medieval king sword ancient history" (solo keywords temáticas, NO refleja lo que se narra)
- LO QUE VES = LO QUE OYES: si el bloque narra "el médico examinó al paciente
  con instrumentos rudimentarios", la query debe ser sobre "physician examining patient
  medieval instruments", NO sobre "medieval medicine history".
- SIEMPRE en inglés (las APIs de stock funcionan mejor en inglés)
- Equilibra especificidad con disponibilidad: "18th century French revolution" (OK) vs "Robespierre guillotining Danton 1794" (demasiado específico)
- Keywords concretas: "sunlight through window", "old photograph", "golden field", "person looking at sky"
- Incluir modificadores de estilo: "cinematic", "warm atmosphere", "golden hour", "hopeful", "16:9"
- Para video, añadir: "slow motion", "tracking shot", "aerial", "time lapse" según el plano
- Evitar términos que requieran personas específicas, marcas o material con copyright
- Si el bloque es abstracto/conceptual, usar metáforas visuales: "light breaking through dark clouds" en lugar de "hope arriving"
- LIMITARSE a términos que EXISTAN en bancos de stock gratuitos (Unsplash, Pexels, Pixabay)

REGLAS PARA media_tipo:
- "video" SOLO cuando el concepto visual tenga movimiento natural y abundante en stock
- "imagen" para conceptos visuales muy específicos, abstractos o con poca oferta de video
- Aproximadamente 30-50% de los bloques deberían ser "video", el resto "imagen"
- Si dudas, elige "imagen" (más seguro, siempre hay fallback)

REGLAS DE FRAGMENTACIÓN:
- Cada bloque debe durar entre 5 y 12 segundos de narración (aproximadamente 15-35 palabras).
- Divide el contenido en bloques cortos y concisos. No agrupes demasiadas ideas en un mismo bloque.
- Si una idea requiere más de 12 segundos, divídela en varios bloques consecutivos.
- Los bloques deben tener search_query_en de 4-7 palabras clave en inglés, describiendo exactamente lo que aparece en pantalla durante ese bloque.
- Mantén coherencia visual entre bloques consecutivos.
- Entre bloques de un mismo párrafo, la descripción visual debe fluir naturalmente.
- Los bloques consecutivos deben tener progresión visual coherente (plano general → primer plano → detalle → plano de situación). No pueden saltar bruscamente de una época/tema a otro.{theme_rules}"""


def build_outline_prompt(config=None, duration_min: float = 15, word_target: int = 2500) -> str:
    """Generate a structured outline BEFORE writing any narrative blocks.

    This produces 4-6 chapters with concrete facts, preventing the LLM from
    producing rambling, repetitive, or factually empty narration.

    Returns a system prompt that demands FACTS, STRUCTURE, and PROGRESSION.
    """
    cfg = config or _default_config
    tone = getattr(cfg, "CANAL_TONE", "Cálido, curioso y envolvente.")
    style = getattr(cfg, "CANAL_NARRATIVE_STYLE", "documental de asombro")
    audience = getattr(cfg, "TARGET_AUDIENCE", "público LATAM adulto curioso")
    n_chapters = min(6, max(4, int(duration_min / 3)))  # ~3 min per chapter

    return f"""Eres un editor de documentales y divulgador científico especializado en fenómenos inexplicables, casualidades imposibles y milagros documentados.

TONO: {tone}
ESTILO: {style}
AUDIENCIA: {audience}
DURACIÓN OBJETIVO: {duration_min} minutos (~{word_target} palabras)

Tu tarea es generar UN SOLO OUTLINE ESTRUCTURADO para un video documental. NO escribas el guion — solo el outline.

REGLAS INQUEBRANTABLES:

1. Genera EXACTAMENTE {n_chapters} capítulos. Cada capítulo debe cubrir un aspecto DISTINTO del tema.
2. Cada capítulo DEBE incluir AL MENOS 2 HECHOS CONCRETOS: números, fechas, nombres, lugares, estadísticas, citas documentadas.
3. El outline debe mostrar PROGRESIÓN NARRATIVA: cada capítulo construye sobre el anterior.
4. PROHIBIDO el lenguaje puramente metafórico o poético sin sustancia. Las frases como "el río del alma fluye hacia la sanación" NO son contenido válido.
5. Los hechos deben ser verificables. Si el contenido fuente no tiene suficientes datos, usa conocimientos generales bien establecidos.
6. Cada capítulo necesita keywords visuales en INGLÉS para la búsqueda de stock media.
7. La emoción objetivo de cada capítulo debe seguir un arco: asombro → curiosidad → intriga → revelación → inspiración.

FORMATO DE SALIDA (JSON):
{{
  "summary": "Resumen de 1-2 frases del arco narrativo completo del video",
  "chapters": [
    {{
      "chapter": 1,
      "titulo": "Título del capítulo en español",
      "idea_central": "Qué revela este capítulo — una frase",
      "hechos_concretos": [
        "Hecho 1: fecha, nombre, dato numérico concreto",
        "Hecho 2: otro dato verificable",
        "Hecho 3: tercer dato o cita documentada"
      ],
      "visual_keywords_en": "english keywords for stock media search",
      "emocion_objetivo": "asombro|curiosidad|intriga|revelacion|inspiracion",
      "words_approx": 500
    }}
  ]
}}

RECUERDA: NADA de metáforas vacías. Solo hechos, datos, y estructura narrativa clara."""


def build_content_only_prompt(config=None, previous_blocks: list = None, word_guidance: int = 300, source_text: str = None, outline: dict = None, batch_num: int = 0) -> str:
    """Lightweight prompt for sequential block-by-block content generation.

    Strips ALL structural requirements (SCRIPT_STRUCTURE, EMOTIONAL_ARC,
    RETENTION_ANCHORS, VIRALITY_TRIGGERS, TITLE_FORMULAS, SEO) so the LLM
    focuses exclusively on writing compelling narrative content.

    NEW: When an outline is provided, includes the current chapter's context
    (title, central idea, concrete facts, visual keywords) so each batch
    writes with narrative coherence and factual substance.

    Args:
        config: Canal config module.
        previous_blocks: List of previously generated blocks for continuity.
        word_guidance: Approximate word count to aim for in this batch.
        source_text: Source content to draw from.
        outline: Structured outline from _generate_outline() with chapters list.
        batch_num: Current batch number (used to select the right chapter from outline).

    Returns:
        System prompt string (~300 tokens instead of ~3000).
    """
    cfg = config or _default_config
    tone = getattr(cfg, "CANAL_TONE", "Cálido, curioso y envolvente.")
    style = getattr(cfg, "CANAL_NARRATIVE_STYLE", "documental de asombro")
    audience = getattr(cfg, "TARGET_AUDIENCE", "público LATAM adulto curioso")

    # ── Outline context injection (NEW) ──────────────────────
    outline_context = ""
    if outline and outline.get("chapters"):
        chapters = outline["chapters"]
        # Progress through chapters as batches advance
        # Rough heuristic: ~3-4 batches per chapter
        completed_blocks = len(previous_blocks) if previous_blocks else 0
        chapter_idx = min(len(chapters) - 1, max(0, completed_blocks // 3))
        current_chapter = chapters[chapter_idx]

        chapter_title = current_chapter.get("titulo", "?")
        chapter_idea = current_chapter.get("idea_central", "")
        chapter_facts = current_chapter.get("hechos_concretos", [])
        chapter_visual = current_chapter.get("visual_keywords_en", "")
        chapter_emotion = current_chapter.get("emocion_objetivo", "")
        next_chapter = (
            chapters[chapter_idx + 1].get("titulo", "")
            if chapter_idx + 1 < len(chapters) else "(final)"
        )

        facts_text = "\n".join(f"  • {f}" for f in (chapter_facts[:3] if chapter_facts else []))
        all_facts_id = sum(1 for ch in chapters for f in ch.get("hechos_concretos", []))

        outline_context = f"""
--- CONTEXTO DEL CAPÍTULO ---
Estás escribiendo el CAPÍTULO {chapter_idx + 1}/{len(chapters)}: "{chapter_title}"
IDEA CENTRAL: {chapter_idea}
EMOCIÓN OBJETIVO: {chapter_emotion}
VISUAL KEYWORDS: {chapter_visual}
HECHOS CONCRETOS QUE DEBES INCLUIR:
{facts_text}
PRÓXIMO CAPÍTULO: {next_chapter}

⚠️ REGLAS DE CONTENIDO (¡OBLIGATORIO!):
- Incluye AL MENOS 2 de los hechos concretos listados arriba.
- NADA de metáforas vacías ni lenguaje poético sin datos.
- Cada bloque debe CONTAR algo real, no "hablar por hablar".
- El video completo tiene {all_facts_id} hechos concretos documentados. No escribas párrafos sin sustancia.
- Conecta este capítulo con el siguiente: "{next_chapter}"."""
    cfg = config or _default_config
    tone = getattr(cfg, "CANAL_TONE", "Cálido, curioso y envolvente.")
    style = getattr(cfg, "CANAL_NARRATIVE_STYLE", "documental de asombro")
    audience = getattr(cfg, "TARGET_AUDIENCE", "público LATAM adulto curioso")

    context_text = ""
    if previous_blocks:
        last_texts = []
        for b in previous_blocks[-6:]:  # más contexto que antes
            if isinstance(b, dict):
                last_texts.append(b.get("texto", ""))
        all_text = " ".join(last_texts)

        # Brief summary of the narrative so far (first 200 chars)
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
            f"  Si ya mencionaste ciertos casos o fenómenos, NO los uses otra vez.\n"
            f"- Si has cubierto ya un aspecto del tema, explora otro ángulo DISTINTO.\n"
            f"- Los bloques de cierre deben SINTETIZAR (no repetir) lo ya dicho.\n"
            f"- Mantén el mismo tono y estilo que los bloques anteriores.\n"
        )

    source_context = ""
    if source_text:
        source_context = f"\nCONTENIDO FUENTE (úsalo como base):\n{source_text[:2000]}\n"

    return f"""Eres un guionista documental para YouTube especializado en fenómenos inexplicables y casualidades imposibles. Escribe narraciones en español latinoamericano neutro.

TONO: {tone}
ESTILO: "{style}" — documentales que mezclan asombro con rigor.
AUDIENCIA: {audience}

REGLAS ESTRICTAS:
1. Español latinoamericano neutro. PROHIBIDO "vosotros", "os", conjugaciones ibéricas.
2. NO inventes datos. Usa SOLO la información de las fuentes proporcionadas.
3. Cada bloque debe ser un párrafo completo y sustancial (no frases sueltas).
4. Incluye detalles sensoriales, descripciones vívidas y contexto.
5. NO uses relleno ni repeticiones. Cada bloque aporta contenido GENUINAMENTE NUEVO. PROHIBIDO repetir los mismos ejemplos, metáforas o analogías en bloques diferentes. Si ya mencionaste un caso concreto, NO lo uses otra vez.
6. CRÍTICO: Cada bloque debe CONTENER al menos un hecho concreto (número, fecha, nombre, lugar, cita). PROHIBIDO escribir párrafos puramente metafóricos sin datos. Si un párrafo no comunica ningún hecho verificable, es INVÁLIDO.
7. ENGANCHE INICIAL: Los primeros bloques deben ser ALTAMENTE intrigantes. Plantea un misterio, un dato impactante o una pregunta que el espectador NECESITE ver respondida. NUNCA empieces con frases como "En este video vamos a..." o "Hoy hablaremos de...". Entra directo al contenido más fascinante.{source_context}{context_text}{outline_context}

Genera entre 2 y 4 bloques narrativos (~{word_guidance} palabras total).
Cada bloque SOLO necesita el campo "texto" (el párrafo que narrará el locutor).

Responde ÚNICAMENTE con JSON: {{"bloques": [{{"texto": "párrafo aquí..."}}, ...]}}
Sin explicaciones, sin markdown, sin texto fuera del JSON."""


def build_system_prompt(config=None, word_count_emphasis: float = 1.0, chunk_context: dict = None, theme_context=None, word_target: dict = None) -> str:
    """Build a concise system prompt focused on narrative quality.

    v22 simplified — format validation and block enrichment are handled
    by dedicated post-generation steps (ScriptValidator + _enrich_blocks).
    The prompt focuses on what the LLM does best: writing compelling content.
    """
    cfg = config or _default_config

    # ── Core identity ────────────────────────────────────────
    tone = getattr(cfg, "CANAL_TONE", "Cálido, curioso y envolvente.")
    style = getattr(cfg, "CANAL_NARRATIVE_STYLE", "documental de asombro")
    style_desc = getattr(cfg, "CANAL_STYLE_DESCRIPTION", "")
    audience = getattr(cfg, "TARGET_AUDIENCE", "público LATAM adulto curioso")
    outro = getattr(cfg, "CANAL_OUTRO_TAGLINE", "La realidad supera la ficción. Y esto es real.")
    hook_rule = getattr(cfg, "SCRIPT_HOOK_RULE", "Hook en los primeros segundos.")

    # ── Duration / word guidance (hint, not rigid rule) ──────
    test_mode = getattr(cfg, "TEST_MODE", False)
    if test_mode:
        duration_target = getattr(cfg, "TEST_VIDEO_DURATION_TARGET", 2)
        words_guide = f"~{getattr(cfg, 'TEST_SCRIPT_WORDS_MIN', 200)}-{getattr(cfg, 'TEST_SCRIPT_WORDS_MAX', 600)}"
        mode_banner = f"\nMODO PRUEBA: guion corto de {duration_target} min (~{words_guide} palabras).\n"
    elif word_target and "duration_target" in word_target:
        duration_target = word_target["duration_target"]
        words_min = word_target["words_min"]
        words_guide = f"~{words_min}-{word_target['words_max']}"
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

    # ── Theme context (keep for visual anchoring spirit) ─────
    theme_banner = ""
    if theme_context:
        theme_banner = (
            f"\nCONTEXTO VISUAL: genero={theme_context.genre}, epoca={theme_context.era}, "
            f"estilo={theme_context.visual_style}. "
            f"Keywords: {', '.join(theme_context.theme_keywords_en[:5])}. "
            f"Prohibido: {', '.join(theme_context.forbidden_elements) if theme_context.forbidden_elements else 'ninguno'}.\n"
        )

    # ── Build the simplified prompt ──────────────────────────
    return f"""Eres un guionista de documentales. Escribes guiones para video-ensayos de YouTube en español latinoamericano neutro. Tu especialidad: fenómenos inexplicables, casualidades imposibles y milagros documentados.{mode_banner}{chunk_banner}

TONO: {tone}
ESTILO: {style} — {style_desc if style_desc else 'Combina asombro cientifico con profundidad humana.'}
AUDIENCIA: {audience}
{theme_banner}
REGLAS ESENCIALES:

1. ESPAÑOL LATINOAMERICANO. Nada de vosotros, os, conjugaciones ibéricas. Usa ustedes, tu o usted.

2. HOOK IMPACTANTE. La primera frase debe ser un dato demoledor, un hecho concreto con fecha y numero, o un cliffhanger. NUNCA: "Hola", "Bienvenidos", "En este video", "Hoy vamos a hablar de". Entra directo al fenomeno mas fascinante.

3. NO INVENTES DATOS. Fechas, nombres, lugares y testimonios deben ser fieles a las fuentes. Si hay debate, menciona la incertidumbre. La credibilidad es lo mas importante.

4. PROGRESION NARRATIVA. Cada seccion debe aportar informacion NUEVA que haga avanzar la historia. Nada de repetir ideas con sinonimos. Si no tienes contenido nuevo, termina antes.

5. ESTRUCTURA CLARA. El guion debe tener: introduccion impactante, desarrollo con datos concretos, climax, y cierre reflexivo. Cada bloque debe ser autocontenido y visualizable.

6. CIERRE. El final debe incluir: \"{outro}\" como reflexion de cierre. Incluye una mencion natural al proximo tema o descubrimiento que exploraras en el siguiente video (ej. \"en el proximo video\", \"en la siguiente entrega\"). PERO NO uses formulas genericas de YouTube como \"suscribete\", \"dale like\", \"activa la campanita\" ni similares.

7. LONGITUD. Apunta a {duration_target} minutos de video ({words_guide} palabras). Es una guia, no una regla rigida — prioriza calidad sobre cantidad.

Responde exclusivamente con JSON valido, sin markdown, sin explicaciones fuera del JSON."""

# Legacy constant for backwards compatibility
SYSTEM_PROMPT = build_system_prompt()


USER_PROMPT_TEMPLATE = """Título de la fuente: {title}
Origen: {source}
Subreddit: {subreddit}
Puntuación/Relevancia: {score}
Categoría del suceso: {category}

Contenido original:
---
{text}
---

Transforma el contenido anterior en un guion documental de video-ensayo sobre este suceso inexplicable real, siguiendo TODAS las reglas del sistema.
Si el contenido describe múltiples sucesos, enfócate en el más impactante o conocido y menciona brevemente los otros en la conclusión.

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
        category=content_item.get("category", "fenómenos inexplicables"),
        text=content_item.get("text", ""),
    )


# ═══════════════════════════════════════════════════════════════════
# MARATHON MODE — Outline for ~1h deep-dive videos
# ═══════════════════════════════════════════════════════════════════

def build_marathon_outline_prompt(config=None, duration_min: float = 60,
                                   num_sections: int = 12,
                                   narrative_format: str = "top_cases",
                                   word_target: int = 8500) -> str:
    """Generate a structured outline for a ~1h marathon documentary video.

    Supports three formats:
      - top_cases: "Los 12 Sincronismos Más Increíbles de la Historia"
      - deep_story: Single epic story told over 12 chapters
      - historical_collapse: "12 Civilizaciones que la Historia Enterró"

    Args:
        config: Canal config module.
        duration_min: Target video duration in minutes (~60).
        num_sections: Number of sections/cases (~12).
        narrative_format: "top_cases" | "deep_story" | "historical_collapse".
        word_target: Target total word count (~8500).

    Returns:
        System prompt string for the outline generation.
    """
    cfg = config or _default_config
    tone = getattr(cfg, "CANAL_TONE", "Cálido, curioso y envolvente.")
    style = getattr(cfg, "CANAL_NARRATIVE_STYLE", "documental de asombro")
    audience = getattr(cfg, "TARGET_AUDIENCE", "público LATAM adulto curioso")
    n_chapters = num_sections  # marathon: one chapter per section

    # ── Format-specific instructions ──
    format_instructions = {
        "top_cases": (
            f"Genera EXACTAMENTE {n_chapters} secciones INDEPENDIENTES. "
            "Cada sección cubre UN fenómeno/caso distinto con 3+ hechos concretos. "
            "Las secciones NO dependen narrativamente unas de otras — son casos "
            "autocontenidos unidos por el tema común. "
            "Progresión: de lo más ligero a lo más impactante."
        ),
        "deep_story": (
            f"Genera EXACTAMENTE {n_chapters} capítulos SECUENCIALES que cuentan "
            "UNA SOLA historia épica de principio a fin. Cada capítulo es una "
            "etapa de la historia. No puede haber repetición de eventos. "
            "Progresión: introducción → desarrollo → clímax → reflexión."
        ),
        "historical_collapse": (
            f"Genera EXACTAMENTE {n_chapters} perfiles de civilizaciones/imperios "
            "que colapsaron. Cada sección cubre UNA civilización: origen, auge, "
            "señales de declive, colapso, legado. 4+ hechos concretos por sección. "
            "Progresión: de las más antiguas a las más recientes."
        ),
    }

    fmt_text = format_instructions.get(
        narrative_format,
        format_instructions["top_cases"],
    )

    return f"""Eres el guionista jefe de una serie documental de alto presupuesto al estilo Netflix. Tu especialidad son los documentales largos de inmersión profunda que mantienen al espectador pegado a la pantalla durante una hora entera.

TONO: {tone}
ESTILO: {style}
AUDIENCIA: {audience}
DURACIÓN OBJETIVO: {duration_min} minutos (~{word_target} palabras)
FORMATO: {narrative_format}

Tu tarea es generar UN OUTLINE ESTRUCTURADO para un documental de {duration_min} minutos. NO escribas el guion — solo el outline.

{fmt_text}

REGLAS INQUEBRANTABLES:

1. CADA sección DEBE tener AL MENOS 3 HECHOS CONCRETOS: números, fechas, nombres, lugares, estadísticas, citas documentadas. NO puede haber secciones sin datos.
2. CADA sección necesita keywords visuales en INGLÉS específicas para búsqueda de stock media.
3. PROHIBIDO el lenguaje puramente metafórico sin sustancia. Las frases como "el universo conspiró en un baile cósmico" NO son contenido válido.
4. Los hechos deben ser verificables. Si no hay datos suficientes en la fuente, usa conocimientos generales bien establecidos.
5. La emoción objetivo de cada sección debe seguir un arco narrativo que mantenga la retención.
6. words_approx por sección: ~{word_target // n_chapters} palabras.

FORMATO DE SALIDA (JSON):
{{
  "summary": "Resumen de 2-3 frases del arco narrativo completo del documental de {duration_min} minutos",
  "chapters": [
    {{
      "chapter": 1,
      "titulo": "Título de la sección en español (impactante, estilo título de YouTube)",
      "idea_central": "Qué revela esta sección — una frase potente",
      "hechos_concretos": [
        "Hecho 1: fecha, nombre, dato numérico concreto",
        "Hecho 2: otro dato verificable con números o nombres",
        "Hecho 3: tercer dato o cita documentada",
        "Hecho 4: cuarto dato (obligatorio para formato deep/historical)"
      ],
      "visual_keywords_en": "english keywords for stock media search (5-8 words, concrete, visual)",
      "emocion_objetivo": "asombro|curiosidad|intriga|tensión|revelacion|inspiracion|reflexion",
      "words_approx": {word_target // n_chapters}
    }}
  ]
}}

⚠️ CRÍTICO: El documental dura {duration_min} MINUTOS. Cada sección es una unidad narrativa completa. NO pueden ser breves ni superficiales. El espectador que ve esto durante 1 hora debe sentir que ha hecho un viaje épico.

RECUERDA: Solo hechos verificables. Solo datos. Solo historias reales. NADA de metáforas vacías. La verdad es más fascinante que la ficción."""
