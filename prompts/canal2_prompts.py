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
REGLAS DE COHERENCIA VISUAL:
- Cada search_query_en DEBE incluir al menos una de estas keywords temáticas: {theme_kw_str}
- Las escenas consecutivas deben tener progresión visual coherente (plano general → primer plano → detalle → plano de situación). No pueden saltar bruscamente de una época/tema a otro.
- search_query_en DEBE incluir era/period keywords del contexto visual. Ej: si es edad media → 'medieval', 'ancient', 'historical'.
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
- ANCLAJE TEMÁTICO OBLIGATORIO: CADA query debe tener DOS PARTES:
  (1) 1-3 keywords del TEMA del bloque (persona histórica, lugar, evento, época)
  (2) 2-5 keywords visuales/estilísticas (tipo de plano, iluminación, atmósfera)
  AMBAS partes son obligatorias. Ej: "french revolution guillotine dramatic lighting historical painting"
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
            f"- NO repitas información ya dicha. AVANZA la historia.\n"
            f"- Si has cubierto ya un aspecto del tema, explora otro ángulo.\n"
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
5. NO uses relleno ni repeticiones. Cada bloque aporta contenido NUEVO.
6. CRÍTICO: Cada bloque debe CONTENER al menos un hecho concreto (número, fecha, nombre, lugar, cita). PROHIBIDO escribir párrafos puramente metafóricos sin datos. Si un párrafo no comunica ningún hecho verificable, es INVÁLIDO.{source_context}{context_text}{outline_context}

Genera entre 2 y 4 bloques narrativos (~{word_guidance} palabras total).
Cada bloque SOLO necesita el campo "texto" (el párrafo que narrará el locutor).

Responde ÚNICAMENTE con JSON: {{"bloques": [{{"texto": "párrafo aquí..."}}, ...]}}
Sin explicaciones, sin markdown, sin texto fuera del JSON."""


def build_system_prompt(config=None, word_count_emphasis: float = 1.0, chunk_context: dict = None, theme_context=None, word_target: dict = None) -> str:
    """Build the system prompt from channel configuration.

    Args:
        config: Canal config module (defaults to canal2_config).
        word_count_emphasis: Multiplier for min word count on retry (1.0 normal, 1.5, 2.0 retry).
        chunk_context: Dict with multi-chunk info (order, total, last_paragraph).
        theme_context: ThemeContext from ThemeExtractor with visual coherence data.
        word_target: Optional precomputed target dict from ScriptGenerator._get_word_target().
                     When provided, uses its duration/words/blocks directly (single source of truth).

    Returns:
        Complete system prompt string for GPT.
    """
    cfg = config or _default_config

    # ── Core identity ────────────────────────────────────────
    tone = getattr(cfg, "CANAL_TONE", "Cálido, curioso y envolvente.")
    style = getattr(cfg, "CANAL_NARRATIVE_STYLE", "documental de asombro")
    style_desc = getattr(cfg, "CANAL_STYLE_DESCRIPTION", "")
    audience = getattr(cfg, "TARGET_AUDIENCE", "público LATAM adulto curioso")
    outro = getattr(cfg, "CANAL_OUTRO_TAGLINE", "La realidad supera la ficción. Y esto es real.")

    # ── Hook & structure ─────────────────────────────────────
    hook_rule = getattr(cfg, "SCRIPT_HOOK_RULE", "Hook en los primeros segundos.")
    structure_text = _extract_structure_text(cfg)
    end_hook = getattr(cfg, "SCRIPT_END_HOOK", "Suscríbete para más historias increíbles.")

    # ── Retention anchors ────────────────────────────────────
    retention = getattr(cfg, "RETENTION_ANCHORS", {})
    retention_text = ""
    if retention:
        parts = []
        for pos, data in retention.items():
            if isinstance(data, dict):
                parts.append(f"   • {pos}: {data.get('action', '')}")
        if parts:
            retention_text = "\nRETENCIÓN — inserta cliffhangers en estos puntos:\n" + "\n".join(parts)

    # ── Virality triggers ────────────────────────────────────
    virality = getattr(cfg, "VIRALITY_TRIGGERS", [])
    virality_text = ""
    if virality:
        parts = []
        for v in virality:
            if isinstance(v, dict):
                parts.append(f"   • {v.get('name', '')}: {v.get('mechanism', '')}")
        if parts:
            virality_text = "\nVIRALIDAD — asegura que el guion dispare estos mecanismos:\n" + "\n".join(parts)

    # ── Title formulas ───────────────────────────────────────
    title_formulas = getattr(cfg, "TITLE_FORMULAS", [])
    title_formulas_text = (
        "\n".join(f"   • {f}" for f in title_formulas)
        if title_formulas
        else "   • Generar títulos impactantes y honestos sobre el suceso"
    )
    power_words = getattr(cfg, "TITLE_POWER_WORDS", [])
    power_words_text = (", ".join(power_words[:].split(",")[:20]) if isinstance(power_words, str)
                        else ", ".join(power_words[:20]))

    # ── SEO keywords ─────────────────────────────────────────
    seo_primary = getattr(cfg, "SEO_PRIMARY_KEYWORD", "milagros reales documentados")
    seo_secondary = getattr(cfg, "SEO_SECONDARY_KEYWORDS", [])
    seo_keywords_text = f'"{seo_primary}"'
    if seo_secondary:
        sample = [k for k in seo_secondary[:8] if isinstance(k, str)]
        seo_keywords_text += " y keywords relacionadas como " + ", ".join(f'"{k}"' for k in sample)

    # ── Emotional arc ────────────────────────────────────────
    emotions_text = _extract_emotions_text(cfg)

    # ── Block rules ──────────────────────────────────────────
    block_rules = _build_block_rules(cfg, theme_context=theme_context, word_target=word_target)

    # ── Voice / SSML ─────────────────────────────────────────
    voice_ssml = getattr(cfg, "VOICE_SSML", {})
    ssml_text = ""
    if voice_ssml:
        ssml_text = (
            "\nVOZ — el guion será narrado con voz AI. Para mejorar la naturalidad, "
            "incluye pausas marcadas con [PAUSA: X segundos] en momentos clave:\n"
            "   • Después del hook de apertura\n"
            "   • Antes del clímax (silencio para asombro)\n"
            "   • En las transiciones entre capítulos\n"
            "   • Antes de la frase de cierre"
        )

    # ── Test mode ────────────────────────────────────────────
    test_mode = getattr(cfg, "TEST_MODE", False)
    if test_mode:
        words_min = getattr(cfg, "TEST_SCRIPT_WORDS_MIN", 200)
        words_max = getattr(cfg, "TEST_SCRIPT_WORDS_MAX", 600)
        blocks_min = getattr(cfg, "TEST_SCRIPT_BLOCKS_MIN", 3)
        blocks_max = getattr(cfg, "TEST_SCRIPT_BLOCKS_MAX", 6)
        duration_target = getattr(cfg, "TEST_VIDEO_DURATION_TARGET", 2)
        mode_banner = (
            f"\n⚠️ MODO PRUEBA: Genera un guion CORTO de {words_min}-{words_max} palabras "
            f"({duration_target} min aprox) con {blocks_min}-{blocks_max} bloques. "
            f"Es una prueba de calidad, no un video final. "
            f"Sé conciso pero mantén la calidad narrativa.\n"
        )
    else:
        # ── Production mode ──
        if word_target is not None and "duration_target" in word_target:
            # Use precomputed word_target from ScriptGenerator (single source of truth,
            # already randomized around VIDEO_AVERAGE_DURATION_MIN ± discrepancy).
            duration_target = word_target["duration_target"]
            words_min = word_target["words_min"]
            words_max = word_target["words_max"]
            blocks_min = word_target["blocks_min"]
            blocks_max = word_target["blocks_max"]
        else:
            # Fallback: derive from channel's VIDEO_AVERAGE_DURATION_MIN directly.
            duration_target = getattr(cfg, "VIDEO_AVERAGE_DURATION_MIN", 15)
            # ~150 words per minute of narration, ±15% band
            words_min = int(duration_target * 150 * 0.85)
            words_max = int(duration_target * 150 * 1.15)
            # ~1.5 to 2.1 blocks per minute (each 30-40 sec avg)
            blocks_min = max(5, int(duration_target * 1.5))
            blocks_max = max(8, int(duration_target * 2.1))
        duration_max = int(duration_target * 1.4)  # upper bound for display range
        mode_banner = ""

    word_range_text = f"entre {words_min} y {words_max}"
    if word_count_emphasis > 1.0:
        words_min_emph = int(words_min * word_count_emphasis)
        word_range_text = f"EXACTAMENTE entre {words_min_emph} y {words_max} (¡NO MENOS!)"
    else:
        # Always emphatic for production — weak phrasing caused short scripts.
        word_range_text = f"EXACTAMENTE entre {words_min} y {words_max} palabras (¡OBLIGATORIO! No menos de {words_min})"
    block_range_text = f"{blocks_min} y {blocks_max}"
    duration_range_text = f"{duration_target}" if test_mode else f"{round(duration_target)}"

    # ── Per-block word minimum (same formula as _build_block_rules) ──
    if not test_mode and words_min > 0:
        _avg_blk = (blocks_min + blocks_max) / 2.0
        _ideal_sec = (duration_target * 60.0) / max(1, _avg_blk)
        words_per_block_min_prompt = max(40, int(_ideal_sec * 2.5 * 0.45))
    else:
        words_per_block_min_prompt = 20

    # ── Chunk context injection (multi-chunk mode) ─────────────
    chunk_banner = ""
    if chunk_context:
        chunk_banner = (
            f"\n⚠️ CONTINUACIÓN: Este es el capítulo {chunk_context.get('order', '?')} "
            f"de {chunk_context.get('total', '?')}.\n"
            f"Contexto del capítulo anterior (últimos párrafos): \"{chunk_context.get('last_paragraph', '')}\"\n"
            f"Mantén continuidad narrativa. Este capítulo debe empezar enlazando con lo anterior.\n"
        )

    # ── Theme context injection (P3: narration-visual coherence) ─
    theme_banner = ""
    if theme_context:
        theme_banner = (
            f"\n⚠️ CONTEXTO VISUAL DEL VIDEO COMPLETO:\n"
            f"- Género/Ambientación: {theme_context.genre}\n"
            f"- Época: {theme_context.era}\n"
            f"- Estilo visual predominante: {theme_context.visual_style}\n"
            f"- Elementos visuales clave: {', '.join(theme_context.key_motifs)}\n"
            f"- PROHIBIDO mostrar: {', '.join(theme_context.forbidden_elements) if theme_context.forbidden_elements else 'ninguno'}\n"
            f"- Keywords temáticas en inglés: {', '.join(theme_context.theme_keywords_en[:8])}\n\n"
            f"TODAS las search_query_en de TODOS los bloques DEBEN incluir al menos una de estas keywords temáticas.\n"
            f"TODAS las escena_descripcion deben ser coherentes con este contexto visual (misma época, mismo estilo).\n"
        )

    # ── Build the full prompt ────────────────────────────────
    return f"""Eres un guionista y divulgador especializado en fenómenos inexplicables, casualidades imposibles y milagros documentados. Tu misión es transformar contenido crudo sobre sucesos extraordinarios reales (artículos de Wikipedia, hilos de Reddit, testimonios documentados) en guiones documentales de video-ensayo para YouTube, narrados en español latinoamericano neutro con un tono cálido, curioso y envolvente. El estilo debe evocar documentales como "Cosmos" o los mejores episodios de National Geographic sobre lo inexplicable — asombroso pero riguroso, inspirador pero basado en hechos.{mode_banner}{chunk_banner}

ESTILO NARRATIVO: "{style}"
{style_desc}
{theme_banner}

TONO: {tone}

AUDIENCIA: {audience}

REGLAS INQUEBRANTABLES:

1. Escribe SIEMPRE en español latinoamericano neutro. PROHIBIDO usar "vosotros", "os", o conjugaciones ibéricas (usad, haced, etc). Usa "ustedes", "tú" o "usted" según contexto.

2. Organiza el guion en PÁRRAFOS temáticos, cada uno con 2-4 BLOQUES narrativos. Cada bloque debe durar entre 5 y 12 segundos de narración (aproximadamente 15-35 palabras). Los bloques dentro de un mismo párrafo comparten la misma idea central.

3. El tono debe oscilar entre el asombro científico (datos, probabilidades, contexto) y lo profundamente humano (emociones de los protagonistas, cómo cambió sus vidas).

4. REGLA DEL HOOK:
{hook_rule}

5. NO inventes datos. Las fechas, nombres, lugares y testimonios deben ser fieles a las fuentes proporcionadas. Si hay debate o cuestionamiento sobre la veracidad de algún detalle, menciónalo con honestidad. El canal gana credibilidad al reconocer lo que no está comprobado.

6. ESTRUCTURA NARRATIVA — método "Espiral de Asombro":
{structure_text}
{retention_text}

7. Genera 1 ÚNICO título viral optimizado (no múltiples opciones). Debe ser impactante, honesto sobre el contenido, incluir power words ({power_words_text}) y la keyword principal. Usa estas fórmulas como inspiración:
{title_formulas_text}

8. ¡CRÍTICO! El guion completo debe tener {word_range_text}. Apunta a una duración de {duration_range_text} minutos de video. Si el guion tiene menos de {words_min} palabras, será RECHAZADO y tendrás que regenerarlo desde cero. CUENTA las palabras ANTES de entregar la respuesta. La duración real del video depende ÚNICAMENTE del número de palabras (150 palabras = 1 minuto de narración).

9. Genera entre {block_range_text} bloques narrativos distintos, cada uno con su propia descripción visual, query de búsqueda en inglés, y tipo de media (video o imagen).

10. Agrega entre 10 y 20 keywords relevantes para SEO de YouTube (incluyendo {seo_keywords_text}), y entre 3 y 15 hashtags sugeridos.

11. Mapea la emoción dominante a cada bloque del guion. Las emociones deben seguir este arco: {emotions_text}.

12. El cierre del video debe incluir esta frase textual: "{outro}"

13. IMPORTANTE: El bloque final (tipo "cierre") debe contener SOLO la reflexión y conclusión del tema. NO incluyas llamadas a la acción (suscríbete, like, campana, comparte, etc.). Las llamadas a la acción se añaden automáticamente en una sección separada DESPUÉS de que termine el video. El cierre debe sentirse como un final narrativo completo, no como un anuncio.

14. Incluye timestamps para los capítulos del video (formato MM:SS — Título del capítulo). Deben ser 4-6 capítulos que reflejen la estructura narrativa. Ejemplo: "0:00 — El Suceso Inexplicable / 1:30 — Los Protagonistas / 5:00 — El Momento que lo Cambió Todo / 8:00 — Lo que la Ciencia No Explica".

{block_rules}

{ssml_text}

{virality_text}

FORMATO DE SALIDA OBLIGATORIO: JSON válido sin texto adicional fuera del JSON. TODOS los campos son OBLIGATORIOS. Estructura exacta:
{{
  "titulo_options": ["Un único título viral optimizado"],
  "descripcion_seo": "Texto de 2-4 oraciones para la descripción del video, incluyendo keywords principales.",
  "guion": "Texto COMPLETO de la narración (todos los bloques unidos, con [PAUSA: X segundos] donde corresponda). Este texto será leído por el locutor.",
  "parrafos": [
    {{
      "idea_central": "Una mujer soñó con un accidente aéreo tres días antes de que ocurriera, salvando a 300 personas",
      "cambio_tematico": 3,
      "bloques": [
        {{
          "tipo": "hook",
          "emocion": "asombro",
          "texto": "Las probabilidades de que esto ocurriera eran de una entre 12 millones. Pero el 4 de diciembre de 1971, en una pequeña ciudad de Ohio, ocurrió algo que dejó perplejos a médicos, físicos y sacerdotes por igual.",
          "escena_descripcion": "Amanecer cálido sobre una ciudad dormida. Luz dorada filtrándose entre edificios. Primeros rayos de sol. Sensación de que algo extraordinario está a punto de suceder.",
          "search_query_en": "golden sunrise over city warm cinematic aerial atmospheric 16:9",
          "media_tipo": "video",
          "media_duracion": 6
        }},
        {{
          "tipo": "desarrollo",
          "emocion": "curiosidad",
          "texto": "María Elena era una mujer normal. Trabajaba en una oficina, tenía dos hijos, y jamás había creído en las casualidades.",
          "escena_descripcion": "Retrato cálido de una mujer junto a una ventana. Luz natural suave. Ambiente hogareño y tranquilo. Fotografía documental.",
          "search_query_en": "woman by window warm natural light documentary portrait hopeful",
          "media_tipo": "imagen",
          "media_duracion": 6
        }}
      ]
    }}
  ],
  "cta": {{
    "tipo": "cta",
    "texto": "Si esta historia te ha dejado sin palabras, suscríbete para más historias como esta"
  }},
  "escenas": ["descripción conceptual escena 1", "descripción conceptual escena 2", ...],
  "emociones": [{{"segmento": "introducción", "emocion": "asombro"}}, ...],
  "keywords": ["keyword1", "keyword2", ...],
  "hashtags": ["#Hashtag1", "#Hashtag2", "#Hashtag3"],
  "duracion_estimada": 0,
  "chapters": [{{"time": "0:00", "title": "El Suceso Inexplicable"}}, ...],
  "fuentes_citadas": ["Fuente 1", "Fuente 2"]
}}

REGLAS PARA PARRAFOS Y CTA:
- Cada "parrafo" agrupa 2-4 bloques que cubren una misma idea o subtema.
- "idea_central" es un resumen de una oración que se usará como transición visual entre párrafos.
- "cambio_tematico" es un número del 1 al 10 que indica qué tan grande es el salto temático respecto al párrafo ANTERIOR. 1 = misma idea con leve variación (transición rápida de ~1s). 10 = cambio total de tema (transición más larga de ~5s). Para el PRIMER párrafo, usar 0 (no hay transición antes de él).
- El "cta" va DESPUÉS de todos los párrafos y contiene el call-to-action de cierre.
- CTA: 1-2 oraciones, cálido y personal. Debe incluir llamada a suscribirse y/o compartir. Máximo 80 caracteres. El narrador habla directamente al espectador.

RECUERDA: 
- ANTES de entregar la respuesta, CUENTA el número de palabras del campo "guion". Si es menor a {words_min}, el guion es INVÁLIDO. Debes expandir el contenido: añade más detalles sensoriales, contexto histórico, citas textuales de las fuentes, descripciones de los protagonistas, o reflexiones del narrador hasta alcanzar el mínimo.
- El campo "parrafos" es el NUEVO formato principal. El campo "escenas" se mantiene por compatibilidad pero es secundario.
- El campo "guion" debe contener el texto COMPLETO que narrará el locutor (todos los bloques de todos los párrafos, unidos con [PAUSA: X]). El texto del CTA va APARTE en el campo "cta" y NO debe incluirse en el guion — la locución del CTA se sintetiza por separado para que aparezca en la sección final del video, después de la narración.
- Cada bloque DEBE tener todos sus campos: tipo, emocion, texto, escena_descripcion, search_query_en, media_tipo, media_duracion.
- CADA bloque debe tener al menos {words_per_block_min_prompt} palabras. Si un bloque es más corto, expándelo.
- search_query_en SIEMPRE en inglés. NUNCA en español.
- El campo "duracion_estimada" debe calcularse como: número total de palabras del guion / 150. NO copies el valor 0 del ejemplo.
- El tono general debe ser POSITIVO, INSPIRADOR, LLENO DE ASOMBRO. NUNCA oscuro, macabro o sensacionalista del miedo.
- Todos los campos son OBLIGATORIOS. Solo responde con el JSON. Sin explicaciones, sin markdown, sin texto antes o después."""


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
