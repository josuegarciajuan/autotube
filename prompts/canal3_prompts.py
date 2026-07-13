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
REGLAS DE COHERENCIA VISUAL:
- Cada search_query_en DEBE incluir al menos una de estas keywords temáticas: {theme_kw_str}
- Las escenas consecutivas deben tener progresión visual coherente (plano general de ruinas → primeros planos de piedras y jeroglíficos → planos de situación con contexto geográfico → detalle de artefactos). No pueden saltar bruscamente entre épocas o civilizaciones.
- search_query_en DEBE incluir keywords de era/período del contexto. Ej: 'ancient ruins', 'archaeological site', 'mesopotamian', 'mayan temple', 'roman empire'.
- PROHIBIDO mostrar elementos modernos, tecnología actual, o edificios contemporáneos.
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
- ANCLAJE TEMÁTICO OBLIGATORIO: CADA query debe tener DOS PARTES:
  (1) 1-3 keywords del TEMA del bloque (civilización, lugar, artefacto, período)
  (2) 2-5 keywords visuales/estilísticas (tipo de plano, iluminación, atmósfera)
  AMBAS partes son obligatorias. Ej: "mayan temple chichen itza drone shot ancient ruins golden hour"
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

    # ── Hook & structure ─────────────────────────────────────
    hook_rule = getattr(cfg, "SCRIPT_HOOK_RULE", "Hook en los primeros segundos.")
    structure_text = _extract_structure_text(cfg)
    end_hook = getattr(cfg, "SCRIPT_END_HOOK", "Suscríbete para más expediciones al pasado.")

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
        else "   • Generar títulos impactantes y honestos sobre el descubrimiento arqueológico"
    )
    power_words = getattr(cfg, "TITLE_POWER_WORDS", [])
    power_words_text = (", ".join(power_words[:].split(",")[:20]) if isinstance(power_words, str)
                        else ", ".join(power_words[:20]))

    # ── SEO keywords ─────────────────────────────────────────
    seo_primary = getattr(cfg, "SEO_PRIMARY_KEYWORD", "civilizaciones antiguas documental")
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
            "   • Después del hook de apertura (silencio para impacto)\n"
            "   • Antes de la revelación del misterio (pausa dramática)\n"
            "   • En las transiciones entre civilizaciones o épocas\n"
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
        if word_target is not None and "duration_target" in word_target:
            duration_target = word_target["duration_target"]
            words_min = word_target["words_min"]
            words_max = word_target["words_max"]
            blocks_min = word_target["blocks_min"]
            blocks_max = word_target["blocks_max"]
        else:
            duration_target = getattr(cfg, "VIDEO_AVERAGE_DURATION_MIN", 15)
            words_min = int(duration_target * 150 * 0.85)
            words_max = int(duration_target * 150 * 1.15)
            blocks_min = max(5, int(duration_target * 1.5))
            blocks_max = max(8, int(duration_target * 2.1))
        duration_max = int(duration_target * 1.4)
        mode_banner = ""

    word_range_text = f"entre {words_min} y {words_max}"
    if word_count_emphasis > 1.0:
        words_min_emph = int(words_min * word_count_emphasis)
        word_range_text = f"EXACTAMENTE entre {words_min_emph} y {words_max} (¡NO MENOS!)"
    else:
        word_range_text = f"EXACTAMENTE entre {words_min} y {words_max} palabras (¡OBLIGATORIO! No menos de {words_min})"
    block_range_text = f"{blocks_min} y {blocks_max}"
    duration_range_text = f"{duration_target}" if test_mode else f"{round(duration_target)}"

    # ── Per-block word minimum ───────────────────────────────
    if not test_mode and words_min > 0:
        _avg_blk = (blocks_min + blocks_max) / 2.0
        _ideal_sec = (duration_target * 60.0) / max(1, _avg_blk)
        words_per_block_min_prompt = max(40, int(_ideal_sec * 2.5 * 0.45))
    else:
        words_per_block_min_prompt = 20

    # ── Chunk context injection ──────────────────────────────
    chunk_banner = ""
    if chunk_context:
        chunk_banner = (
            f"\n⚠️ CONTINUACIÓN: Este es el capítulo {chunk_context.get('order', '?')} "
            f"de {chunk_context.get('total', '?')}.\n"
            f"Contexto del capítulo anterior (últimos párrafos): \"{chunk_context.get('last_paragraph', '')}\"\n"
            f"Mantén continuidad narrativa. Este capítulo debe empezar enlazando con lo anterior.\n"
        )

    # ── Theme context injection ──────────────────────────────
    theme_banner = ""
    if theme_context:
        theme_banner = (
            f"\n⚠️ CONTEXTO VISUAL DEL VIDEO COMPLETO:\n"
            f"- Género/Ambientación: {theme_context.genre}\n"
            f"- Época: {theme_context.era}\n"
            f"- Estilo visual predominante: {theme_context.visual_style}\n"
            f"- Elementos visuales clave: {', '.join(theme_context.key_motifs)}\n"
            f"- PROHIBIDO mostrar: {', '.join(theme_context.forbidden_elements) if theme_context.forbidden_elements else 'elementos modernos, tecnología actual, ropa contemporánea, vehículos'}\n"
            f"- Keywords temáticas en inglés: {', '.join(theme_context.theme_keywords_en[:8])}\n\n"
            f"TODAS las search_query_en de TODOS los bloques DEBEN incluir al menos una de estas keywords temáticas.\n"
            f"TODAS las escena_descripcion deben ser coherentes con este contexto visual (misma época, mismo estilo).\n"
        )

    # ── Build the full prompt ────────────────────────────────
    return f"""Eres un guionista y divulgador especializado en arqueología, civilizaciones antiguas, ciudades perdidas y misterios históricos. Tu misión es transformar contenido crudo sobre descubrimientos arqueológicos reales (artículos de Wikipedia, entradas de Atlas Obscura, hilos de Reddit, noticias de arqueología) en guiones documentales de video-ensayo para YouTube, narrados en español latinoamericano neutro con un tono grave, misterioso y profundamente envolvente. El estilo debe evocar documentales como los de National Geographic sobre el antiguo Egipto, Mesopotamia o las culturas precolombinas — riguroso en los datos pero cinematográfico en la narración, inspirador pero basado en hechos arqueológicos verificables.{mode_banner}{chunk_banner}

ESTILO NARRATIVO: "{style}"
{style_desc}
{theme_banner}

TONO: {tone}

AUDIENCIA: {audience}

REGLAS INQUEBRANTABLES:

1. Escribe SIEMPRE en español latinoamericano neutro. PROHIBIDO usar "vosotros", "os", o conjugaciones ibéricas (usad, haced, etc). Usa "ustedes", "tú" o "usted" según contexto.

2. Organiza el guion en PÁRRAFOS temáticos, cada uno con 2-4 BLOQUES narrativos. Cada bloque debe durar entre 5 y 12 segundos de narración (aproximadamente 15-35 palabras). Los bloques dentro de un mismo párrafo comparten la misma idea central.

3. El tono debe oscilar entre el asombro arqueológico (fechas, ubicaciones, datos de excavaciones) y lo profundamente humano (cómo vivían, qué creían, qué dejaron atrás). Las piedras y ruinas deben cobrar vida.

4. REGLA DEL HOOK:
{hook_rule}

5. NO inventes datos arqueológicos. Las fechas, nombres de sitios, descubrimientos, civilizaciones y teorías deben ser fieles a las fuentes proporcionadas. Si hay debate académico sobre una teoría, menciónalo con honestidad ("algunos arqueólogos sostienen...", "hay evidencia que sugiere...", "el debate continúa..."). El canal gana credibilidad al reconocer lo que no está comprobado.

6. ESTRUCTURA NARRATIVA — método "Expedición al Pasado":
{structure_text}
{retention_text}

REGLA DEL ENGANCHE INICIAL (primeros 2-3 minutos — ¡CRÍTICO para retención!):
Los primeros minutos son los que deciden si el espectador se queda hasta el final. Debes:
- Abrir con un misterio arqueológico, dato impactante o pregunta que el espectador NECESITE ver resuelta.
- Crear una "promesa narrativa": el espectador debe intuir que si se queda, descubrirá algo fascinante sobre una civilización perdida.
- NUNCA empezar con frases como "En este video vamos a hablar de...", "Hoy exploraremos..." o "Bienvenidos a...".
- La primera oración del guion debe ser IMPACTANTE. Entra directo al descubrimiento más asombroso.

REGLA ANTI-REPETICIÓN TEMÁTICA (¡OBLIGATORIO!):
Cada párrafo debe aportar una idea GENUINAMENTE NUEVA que haga AVANZAR la narrativa. PROHIBIDO:
- Repetir los mismos ejemplos, sitios arqueológicos o conceptos en diferentes párrafos. Si ya hablaste de un templo o civilización, NO lo menciones otra vez.
- Reformular la misma tesis con sinónimos. Cada párrafo debe explorar un ÁNGULO DIFERENTE del descubrimiento.
- Usar la misma metáfora, analogía o recurso retórico más de una vez.
- Los bloques de cierre deben SINTETIZAR (no repetir) las ideas ya expuestas.
- Si no tienes contenido realmente nuevo que aportar, es MEJOR terminar el guion antes que repetir ideas.

7. Genera 1 ÚNICO título viral optimizado (no múltiples opciones). Debe ser impactante, honesto sobre el contenido, incluir power words ({power_words_text}) y la keyword principal. Usa estas fórmulas como inspiración:
{title_formulas_text}

8. ¡CRÍTICO! El guion completo debe tener {word_range_text}. Apunta a una duración de {duration_range_text} minutos de video. Si el guion tiene menos de {words_min} palabras, será RECHAZADO y tendrás que regenerarlo desde cero. CUENTA las palabras ANTES de entregar la respuesta. La duración real del video depende ÚNICAMENTE del número de palabras (150 palabras = 1 minuto de narración).

9. Genera entre {block_range_text} bloques narrativos distintos, cada uno con su propia descripción visual, query de búsqueda en inglés, y tipo de media (video o imagen).

10. Agrega entre 10 y 20 keywords relevantes para SEO de YouTube (incluyendo {seo_keywords_text}), y entre 3 y 15 hashtags sugeridos.

11. Mapea la emoción dominante a cada bloque del guion. Las emociones deben seguir este arco: {emotions_text}.

12. El cierre del video debe incluir esta frase textual: "{outro}"

13. IMPORTANTE: El bloque final (tipo "cierre") debe contener SOLO la reflexión y conclusión del tema. NO incluyas llamadas a la acción (suscríbete, like, campana, comparte, etc.). Las llamadas a la acción se añaden automáticamente en una sección separada DESPUÉS de que termine el video. El cierre debe sentirse como un final narrativo completo, no como un anuncio.

14. Incluye timestamps para los capítulos del video (formato MM:SS — Título del capítulo). Deben ser 4-6 capítulos que reflejen la estructura narrativa de la Expedición al Pasado. Ejemplo: "0:00 — El Descubrimiento / 1:30 — La Civilización Perdida / 5:00 — Lo que las Piedras Revelan / 8:00 — El Legado que Permanece".

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
      "idea_central": "En las profundidades de la selva guatemalteca, la tecnología LiDAR reveló una mega-ciudad maya de más de 60.000 estructuras que cambió todo lo que sabíamos sobre esta civilización",
      "cambio_tematico": 3,
      "bloques": [
        {{
          "tipo": "hook",
          "emocion": "asombro",
          "texto": "Durante siglos, la selva del Petén guardó un secreto. Bajo el dosel verde, oculta por la vegetación más densa del continente, yacía una de las ciudades más grandes del mundo antiguo. Nadie la había visto. Hasta que un avión equipado con láser lo cambió todo.",
          "escena_descripcion": "Vista aérea de la selva guatemalteca. Amanecer entre la niebla. Rayos de sol dorados perforando el dosel. La cámara desciende lentamente hacia la vegetación. Sensación de misterio y anticipación.",
          "search_query_en": "drone shot guatemalan jungle mist golden hour cinematic aerial 16:9",
          "media_tipo": "video",
          "media_duracion": 6
        }},
        {{
          "tipo": "desarrollo",
          "emocion": "curiosidad",
          "texto": "El escáner LiDAR disparó miles de pulsos láser por segundo contra el suelo. Los rayos atravesaban las hojas, las ramas, las lianas, y rebotaban revelando lo que había debajo.",
          "escena_descripcion": "Visualización de datos LiDAR. Puntos láser transformándose en mapa topográfico. Las formas de pirámides y plataformas emergiendo del ruido digital. Look tecnológico pero con paleta de colores tierra.",
          "search_query_en": "lidar visualization archaeological mapping ancient ruins data scan",
          "media_tipo": "imagen",
          "media_duracion": 6
        }}
      ]
    }}
  ],
  "cta": {{
    "tipo": "cta",
    "texto": "Si quieres acompañarnos en la próxima expedición al pasado, suscríbete y activa la campana"
  }},
  "escenas": ["descripción conceptual escena 1", "descripción conceptual escena 2", ...],
  "emociones": [{{"segmento": "introducción", "emocion": "asombro"}}, ...],
  "keywords": ["keyword1", "keyword2", ...],
  "hashtags": ["#Hashtag1", "#Hashtag2", "#Hashtag3"],
  "duracion_estimada": 0,
  "chapters": [{{"time": "0:00", "title": "El Descubrimiento"}}, ...],
  "fuentes_citadas": ["Fuente 1", "Fuente 2"]
}}

REGLAS PARA PARRAFOS Y CTA:
- Cada "parrafo" agrupa 2-4 bloques que cubren una misma idea o subtema.
- "idea_central" es un resumen de una oración que se usará como transición visual entre párrafos.
- "cambio_tematico" es un número del 1 al 10 que indica qué tan grande es el salto temático respecto al párrafo ANTERIOR. 1 = misma idea con leve variación (transición rápida de ~1s). 10 = cambio total de tema (transición más larga de ~5s). Para el PRIMER párrafo, usar 0 (no hay transición antes de él).
- El "cta" va DESPUÉS de todos los párrafos y contiene el call-to-action de cierre.
- CTA: 1-2 oraciones, cálido y personal. Debe incluir llamada a suscribirse y/o compartir. Máximo 80 caracteres. El narrador habla directamente al espectador. Usa metáforas de viaje/expedición ("la próxima expedición", "nos vemos entre las ruinas").

RECUERDA: 
- ANTES de entregar la respuesta, CUENTA el número de palabras del campo "guion". Si es menor a {words_min}, el guion es INVÁLIDO. Debes expandir el contenido: añade más detalles arqueológicos, contexto histórico, descripciones de los sitios, citas de los arqueólogos, datos de las excavaciones, o reflexiones sobre el legado de la civilización hasta alcanzar el mínimo.
- El campo "parrafos" es el NUEVO formato principal. El campo "escenas" se mantiene por compatibilidad pero es secundario.
- El campo "guion" debe contener el texto COMPLETO que narrará el locutor (todos los bloques de todos los párrafos, unidos con [PAUSA: X]). El texto del CTA va APARTE en el campo "cta" y NO debe incluirse en el guion — la locución del CTA se sintetiza por separado para que aparezca en la sección final del video, después de la narración.
- Cada bloque DEBE tener todos sus campos: tipo, emocion, texto, escena_descripcion, search_query_en, media_tipo, media_duracion.
- CADA bloque debe tener al menos {words_per_block_min_prompt} palabras. Si un bloque es más corto, expándelo.
- search_query_en SIEMPRE en inglés. NUNCA en español.
- El campo "duracion_estimada" debe calcularse como: número total de palabras del guion / 150. NO copies el valor 0 del ejemplo.
- El tono general debe ser SOLEMNE, EVOCADOR, LLENO DE ASOMBRO ARQUEOLÓGICO. NUNCA frívolo, sarcástico o infantil. Como un explorador narrando lo que ve al adentrarse en un templo perdido.
- Todo el contenido debe ser HISTÓRICAMENTE RIGUROSO. Cita fuentes verificables.
- PROHIBIDO usar términos modernos, comparaciones con tecnología actual, o referencias a cultura pop.
- Todos los campos son OBLIGATORIOS. Solo responde con el JSON. Sin explicaciones, sin markdown, sin texto antes o después."""


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
