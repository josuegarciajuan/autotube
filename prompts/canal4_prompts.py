"""GPT prompt templates for Canal 4: Expediciones sin Retorno.

System and user prompts instructing GPT to generate video-essay
documentary scripts about real expedition disasters, survival stories,
shipwrecks, and mountaineering tragedies, combining Wikipedia articles
and Reddit threads into tense, cinematic survival narratives.

v2: Block-based script generation with per-block media search queries,
     emotional voice mapping, and hybrid video/image media strategy.
"""

from config import canal4_config as _default_config


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
                lines.append(f"       ⚡ ANCLA DE RETENCION: {anchor}")
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
    return "impacto, tension creciente, horror, desesperacion, reflexion, respeto"


def _build_block_rules(cfg, theme_context=None, word_target=None) -> str:
    """Build the bloque structure rules for the prompt."""
    tts = getattr(cfg, "TTS_STRATEGY", {})
    media = getattr(cfg, "MEDIA_STRATEGY", {})

    video_min = media.get("video_min_duration", 4)
    video_max = media.get("video_max_duration", 20)

    theme_rules = ""
    if theme_context and getattr(theme_context, "theme_keywords_en", None):
        theme_kw_str = ", ".join(theme_context.theme_keywords_en[:5])
        theme_rules = f"""
REGLAS DE COHERENCIA VISUAL (¡OBLIGATORIO!):
- CADA search_query_en debe ser una FUSIÓN de DOS partes (ambas obligatorias, en este orden):
  (1) SUJETO NARRATIVO (VA PRIMERO, ~60% de la query): 2-3 keywords que describen
      EXACTAMENTE lo que se narra en ESTE bloque — la expedición, lugar, desastre,
      explorador o evento mencionado en la narración. Esto es el sujeto principal.
  (2) AMBIENTACIÓN TEMÁTICA (VA DESPUÉS, ~40% de la query): 1-2 keywords de época/ambiente
      extraídas de: {theme_kw_str}. Esto ancla la escena en la geografía/era correcta.
  ✅ BUENO: "franklin expedition ship trapped arctic ice cinematic"
          → narra el barco atrapado → anclado en expedición ártica
  ❌ MALO: "arctic expedition ice snow documentary"
          → solo tema genérico, NO refleja lo que se narra
  ❌ MALO: "cruise ship tropical ocean vacation"
          → describe la acción pero FUERA de contexto geográfico/época
- LO QUE VES = LO QUE OYES: la query debe traducir visualmente lo que el locutor
  está narrando en este momento exacto. Si la narración dice "los exploradores
  cruzaron el desierto sin agua durante días", la query debe ser sobre "explorers
  crossing desert heat survival" — NO sobre "desert landscape" genérico.
- PROHIBIDO que dos escenas consecutivas usen la MISMA keyword de anclaje temático.
  Rota entre las keywords disponibles: {theme_kw_str}
- Las escenas consecutivas deben compartir al menos UN elemento visual (color, luz,
  paisaje, clima, textura del terreno) para crear un HILO VISUAL que una todo el video.
  No pueden saltar bruscamente entre geografías o épocas.
- search_query_en DEBE incluir era/period keywords del contexto visual. Ej: si es expedicion artica → 'arctic', 'frozen', 'ice', '19th century', 'expedition'; si es desierto → 'desert', 'sand', 'hot', 'sun', 'survival'; si es selva → 'jungle', 'rainforest', 'expedition', 'green'. Adapta el ambiente al tema REAL, NO impongas frio/nieve si la expedicion no es polar.
- PROHIBIDO mostrar elementos de: {', '.join(theme_context.forbidden_elements) if theme_context.forbidden_elements else 'ninguno'}"""

    return f"""ESTRUCTURA DE BLOQUES NARRATIVOS:
El guion debe organizarse en bloques semanticos cohesivos agrupados en parrafos tematicos. Cada bloque debe durar entre 5 y 12 segundos de narracion (aproximadamente 15-35 palabras). Para cada bloque debes generar:

- "tipo": uno de ["hook", "desarrollo", "climax", "reflexion", "cierre"]
- "emocion": la emocion dominante del bloque
- "texto": el texto exacto que narra el locutor en este bloque
- "escena_descripcion": descripcion cinematografica DETALLADA de que se ve en pantalla
- "search_query_en": 5-8 keywords en INGLES para buscar en Unsplash/Pexels. REGLAS ESTRICTAS:
  * FORMATO OBLIGATORIO DE DOS PARTES (ambas necesarias, en este orden):
    (1) [SUJETO NARRATIVO]: 2-4 keywords del contenido EXACTO del bloque
        (expedición, lugar, desastre, explorador mencionado en la narración)
    (2) [ANCLAJE ÉPOCA/GEOGRAFÍA]: 1-2 keywords de ambientación temática del video
  * LO QUE VES = LO QUE OYES: la query debe traducir visualmente lo que el locutor
    está narrando en este momento exacto, anclado en la geografía/época del video.
  * Ej: "franklin expedition ship trapped arctic ice cinematic" (narra barco atrapado en Ártico)
  * Ej: "donner party snow pioneer wagon documentary style" (narra caravana en nieve)
  * Ej: "egypt desert expedition sand cinematic" (narra expedición en desierto egipcio)
  * SOLO terminos que EXISTAN en bancos de stock gratuitos (Unsplash, Pexels, Pixabay)
  * Equilibra especificidad con disponibilidad: "arctic exploration 19th century" (OK) vs "HMS Erebus trapped in ice 1846" (demasiado especifico)
  * Si el concepto es muy especifico, MANTEN el sujeto narrativo pero simplifica la locación: "Franklin Expedition" → "arctic exploration ship ice", NO lo conviertas en algo generico sin tema
  * Incluye UN modificador de estilo: "cinematic", "hot atmosphere", "dramatic lighting", "documentary style", "wilderness", "expedition"
  * Para video, usa queries de 4-7 palabras con sujeto narrativo + geografía + visual: "frozen ocean ship expedition cinematic", "desert sandstorm survival aerial", "jungle river expedition canopy", "storm clouds survival time lapse"
  * Para video, añade "slow motion" o "aerial" o "time lapse" SOLO si realmente aplica — no fuerces estos terminos si no corresponden
- "media_tipo": Decide si este bloque se vera mejor con un minivideo o una imagen fija:
  * "video" si el plano tiene movimiento natural: paisajes, tormentas, oceano, nubes, time-lapses, olas, dunas de arena, niebla, cascadas, animales en movimiento, caminata en desierto o selva, paneos sobre paisajes
  * "imagen" SOLO si el plano es inherentemente estatico: un retrato fotografico, un mapa antiguo, un diagrama de supervivencia, un documento historico, una carta manuscrita
  * En caso de duda, elige "video" — el sistema tiene fallback a imagen si no encuentra video
  * Aproximadamente el 60-70% de los bloques deberian ser video
- "media_duracion": duracion ideal del clip en segundos (entre {video_min} y {video_max} si es video; mismo valor que la duracion estimada si es imagen)

REGLAS DE FRAGMENTACION:
- Cada bloque debe durar entre 5 y 12 segundos de narracion (aproximadamente 15-35 palabras).
- Divide el contenido en bloques cortos y concisos. No agrupes demasiadas ideas en un mismo bloque.
- Si una idea requiere mas de 12 segundos, dividela en varios bloques consecutivos.
- Los bloques deben tener search_query_en de 4-7 palabras clave en ingles, describiendo exactamente lo que aparece en pantalla durante ese bloque.
- Manten coherencia visual entre bloques consecutivos.
- Entre bloques de un mismo parrafo, la descripcion visual debe fluir naturalmente.
- Los bloques consecutivos deben tener progresion visual coherente.{theme_rules}

EJEMPLOS DE BUENAS QUERIES:
  - "sahara desert expedition sand cinematic"
  - "amazon rainforest river expedition documentary"
  - "frozen ocean arctic landscape cinematic"
  - "old wooden ship sails storm documentary style"
  - "jungle waterfall tropical expedition dramatic"
  - "snowstorm wilderness dramatic lighting"
  - "storm clouds time lapse dark sky"
  - "person silhouette mountain peak contemplative"
  - "compass navigation map vintage exploration"
  - "ocean waves storm ship survival"

EJEMPLOS DE MALAS QUERIES (NO USAR):
  - "HMS Erebus trapped in ice 1846" (demasiado especifico)
  - "Donner Party survival cannibalism" (abstracto, no existe en stock)
  - "Sir John Franklin portrait expedition" (demasiado especifico)
  - "Napoleon crossing Alps with army" (especifico, mejor usar generic landscape)"""


def build_outline_prompt(config=None, duration_min: float = 15, word_target: int = 2500) -> str:
    """Generate a structured outline BEFORE writing any narrative blocks.

    For canal4 (Expediciones sin retorno): focuses on historical expeditions,
    survival stories, shipwrecks, and mountaineering tragedies.
    """
    cfg = config or _default_config
    tone = getattr(cfg, "CANAL_TONE", "Grave, tenso y profundamente humano.")
    style = getattr(cfg, "CANAL_NARRATIVE_STYLE", "documental de supervivencia")
    audience = getattr(cfg, "TARGET_AUDIENCE", "publico LATAM adulto curioso")
    n_chapters = min(6, max(4, int(duration_min / 3)))

    return f"""Eres un editor de documentales y divulgador especializado en expediciones historicas, tragedias de supervivencia, naufragios y montañismo.

TONO: {tone}
ESTILO: {style}
AUDIENCIA: {audience}
DURACIÓN OBJETIVO: {duration_min} minutos (~{word_target} palabras)

Tu tarea es generar UN SOLO OUTLINE ESTRUCTURADO para un video documental. NO escribas el guion — solo el outline.

REGLAS INQUEBRANTABLES:

1. Genera EXACTAMENTE {n_chapters} capítulos. Cada capítulo debe cubrir un aspecto DISTINTO del tema.
2. Cada capítulo DEBE incluir AL MENOS 2 HECHOS CONCRETOS: fechas, nombres, lugares, distancias, altitudes, temperaturas, número de víctimas, citas de diarios de expedición.
3. El outline debe mostrar PROGRESIÓN NARRATIVA: cada capítulo construye sobre el anterior.
4. PROHIBIDO el lenguaje puramente metafórico o poético sin sustancia. Frases como "la montaña susurra secretos al viento" NO son contenido válido.
5. Los hechos deben ser verificables. Usa datos históricos reales.
6. Cada capítulo necesita keywords visuales en INGLÉS para la búsqueda de stock media.
7. La emoción objetivo de cada capítulo debe seguir un arco: intriga → tensión → crisis → lucha → desenlace.

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
      "emocion_objetivo": "intriga|tension|crisis|lucha|desenlace",
      "words_approx": 500
    }}
  ]
}}

RECUERDA: NADA de metáforas vacías. Solo hechos, datos, fechas, nombres y estructura narrativa clara."""


def build_content_only_prompt(config=None, previous_blocks: list = None, word_guidance: int = 300, source_text: str = None, outline: dict = None, batch_num: int = 0) -> str:
    """Lightweight prompt for sequential block-by-block content generation."""
    cfg = config or _default_config
    tone = getattr(cfg, "CANAL_TONE", "Grave, tenso y profundamente humano.")
    style = getattr(cfg, "CANAL_NARRATIVE_STYLE", "documental de supervivencia")
    audience = getattr(cfg, "TARGET_AUDIENCE", "publico LATAM adulto curioso")

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
            f"La historia empezo asi: \"{first_words}...\"\n"
            f"Lo ULTIMO que narraste: \"{all_text[-400:]}\"\n\n"
            f"INSTRUCCIONES DE CONTINUIDAD:\n"
            f"- Continua la narracion desde donde quedo, de forma NATURAL.\n"
            f"- El texto debe fluir como si fuera un solo documento escrito.\n"
            f"- AVANZA la historia: cada bloque debe aportar contenido GENUINAMENTE NUEVO.\n"
            f"  PROHIBIDO repetir los mismos ejemplos, metaforas o analogias ya usadas.\n"
            f"  Si ya narraste un evento o detalle, NO lo vuelvas a contar.\n"
            f"- Si has cubierto ya un aspecto de la expedicion, explora otro angulo DISTINTO.\n"
            f"- Los bloques de cierre deben SINTETIZAR (no repetir) lo ya dicho.\n"
            f"- Manten el mismo tono y estilo que los bloques anteriores.\n"
        )

    source_context = ""
    if source_text:
        source_context = f"\nCONTENIDO FUENTE (usalo como base):\n{source_text[:2000]}\n"

    return f"""Eres un guionista y divulgador historico especializado en expediciones tragicas y supervivencia extrema. Escribe narraciones documentales en español latinoamericano neutro.

TONO: {tone}
ESTILO: "{style}" — expediciones reales al limite de lo humano, documentales que mezclan rigor con dramatismo cinematografico.
AUDIENCIA: {audience}

REGLAS ESTRICTAS:
1. Español latinoamericano neutro. PROHIBIDO "vosotros", "os", conjugaciones ibericas.
2. NO inventes datos. Usa SOLO la informacion de las fuentes proporcionadas.
3. Cada bloque debe ser un PARRAFO COMPLETO y sustancial (no frases sueltas).
4. Incluye detalles de la expedicion: fechas, nombres, lugares, condiciones extremas, decisiones criticas.
5. NO uses relleno ni repeticiones. Cada bloque aporta contenido GENUINAMENTE NUEVO. PROHIBIDO repetir los mismos ejemplos, eventos o detalles en bloques diferentes. Si ya narraste un momento de la expedicion, NO lo cuentes otra vez.
6. Oscila entre la escala epica (la inmensidad del hielo, el poder de la naturaleza) y lo intimo (el miedo, la desesperacion, las decisiones imposibles).
7. ENGANCHE INICIAL: Los primeros bloques deben ser ALTAMENTE intrigantes. Plantea un misterio, un momento de tension extrema o una pregunta que el espectador NECESITE ver respondida. NUNCA empieces con frases como "En este video vamos a..." o "Hoy conoceremos...". Entra directo al momento mas dramatico de la expedicion.{source_context}{context_text}

Genera entre 2 y 4 bloques narrativos (~{word_guidance} palabras total).
Cada bloque SOLO necesita el campo "texto" (el parrafo que narrara el locutor).

Responde UNICAMENTE con JSON: {{"bloques": [{{"texto": "parrafo completo aqui..."}}, ...]}}
Sin explicaciones, sin markdown, sin texto fuera del JSON."""


def build_system_prompt(config=None, word_count_emphasis: float = 1.0, chunk_context: dict = None, theme_context=None, word_target: dict = None) -> str:
    """Build the system prompt from channel configuration.

    Args:
        config: Canal config module (defaults to canal4_config).
        word_count_emphasis: Multiplier for min word count on retry (1.0 normal, 1.5, 2.0 retry).
        chunk_context: Dict with multi-chunk info (order, total, last_paragraph).
        theme_context: ThemeContext from ThemeExtractor with visual coherence data.
        word_target: Optional precomputed target dict from ScriptGenerator._get_word_target().

    Returns:
        Complete system prompt string for GPT.
    """
    cfg = config or _default_config

    # ── Core identity ────────────────────────────────────────
    tone = getattr(cfg, "CANAL_TONE", "Grave, tenso y profundamente humano.")
    style = getattr(cfg, "CANAL_NARRATIVE_STYLE", "documental de supervivencia")
    style_desc = getattr(cfg, "CANAL_STYLE_DESCRIPTION", "")
    audience = getattr(cfg, "TARGET_AUDIENCE", "publico LATAM adulto curioso")
    outro = getattr(cfg, "CANAL_OUTRO_TAGLINE", "La historia de esta expedicion es real. Todo ocurrio.")

    # ── Hook & structure ─────────────────────────────────────
    hook_rule = getattr(cfg, "SCRIPT_HOOK_RULE", "Hook en los primeros segundos.")
    structure_text = _extract_structure_text(cfg)
    end_hook = getattr(cfg, "SCRIPT_END_HOOK", "Suscribete para mas expediciones.")

    # ── Retention anchors ────────────────────────────────────
    retention = getattr(cfg, "RETENTION_ANCHORS", {})
    retention_text = ""
    if retention:
        parts = []
        for pos, data in retention.items():
            if isinstance(data, dict):
                parts.append(f"   • {pos}: {data.get('action', '')}")
        if parts:
            retention_text = "\nRETENCION — inserta cliffhangers en estos puntos:\n" + "\n".join(parts)

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
        else "   • Generar titulos impactantes y honestos sobre la expedicion"
    )
    power_words = getattr(cfg, "TITLE_POWER_WORDS", [])
    power_words_text = (", ".join(power_words[:].split(",")[:20]) if isinstance(power_words, str)
                        else ", ".join(power_words[:20]))

    # ── SEO keywords ─────────────────────────────────────────
    seo_primary = getattr(cfg, "SEO_PRIMARY_KEYWORD", "expediciones fallidas reales")
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
            "\nVOZ — el guion sera narrado con voz AI. Para mejorar la naturalidad, "
            "incluye pausas marcadas con [PAUSA: X segundos] en momentos clave:\n"
            "   • Despues del hook de apertura\n"
            "   • Antes del climax (silencio para tension)\n"
            "   • En las transiciones entre capitulos de la expedicion\n"
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
            f"Se conciso pero mantén la calidad narrativa.\n"
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

    if not test_mode and words_min > 0:
        _avg_blk = (blocks_min + blocks_max) / 2.0
        _ideal_sec = (duration_target * 60.0) / max(1, _avg_blk)
        words_per_block_min_prompt = max(40, int(_ideal_sec * 2.5 * 0.45))
    else:
        words_per_block_min_prompt = 20

    # ── Chunk context injection ───────────────────────────────
    chunk_banner = ""
    if chunk_context:
        chunk_banner = (
            f"\n⚠️ CONTINUACION: Este es el capitulo {chunk_context.get('order', '?')} "
            f"de {chunk_context.get('total', '?')}.\n"
            f"Contexto del capitulo anterior (ultimos parrafos): \"{chunk_context.get('last_paragraph', '')}\"\n"
            f"Manten continuidad narrativa. Este capitulo debe empezar enlazando con lo anterior.\n"
        )

    # ── Theme context injection ───────────────────────────────
    theme_banner = ""
    if theme_context:
        mood_str = f"\n- Estado de animo: {theme_context.mood}" if theme_context.mood else ""
        light_str = f"\n- Iluminacion preferida: {theme_context.lighting}" if theme_context.lighting else ""
        comp_str = f"\n- Tipo de encuadre: {theme_context.composition}" if theme_context.composition else ""
        palette_str = ""
        if theme_context.color_palette:
            p = theme_context.color_palette
            palette_str = f"\n- Paleta de colores: primario={p.get('primary','?')}, secundario={p.get('secondary','?')}, acento={p.get('accent','?')}"
        theme_banner = (
            f"\n⚠️ CONTEXTO VISUAL DEL VIDEO COMPLETO — EL MUNDO DONDE TODO OCURRE:\n"
            f"- Genero/Ambientacion: {theme_context.genre}\n"
            f"- Epoca: {theme_context.era}\n"
            f"- Estilo visual predominante: {theme_context.visual_style}\n"
            f"- Elementos visuales clave: {', '.join(theme_context.key_motifs)}"
            f"{mood_str}{light_str}{comp_str}{palette_str}\n"
            f"- PROHIBIDO mostrar: {', '.join(theme_context.forbidden_elements) if theme_context.forbidden_elements else 'ninguno'}\n"
            f"- Keywords tematicas en ingles: {', '.join(theme_context.theme_keywords_en[:8])}\n\n"
            f"REGLA DE FUSION NARRATIVA + TEMATICA (¡OBLIGATORIO!):\n"
            f"Cada search_query_en debe ser UNA SOLA FRASE que fusione DOS conceptos:\n"
            f"  1. El SUJETO NARRATIVO — lo que se esta contando en ESTE BLOQUE concreto (la expedicion, el desastre, el explorador)\n"
            f"  2. La AMBIENTACION TEMATICA — {theme_context.genre}, {theme_context.era}\n"
            f"El sujeto narrativo SIEMPRE va primero (es el sujeto visual principal).\n"
            f"La ambientacion va despues (es el filtro de geografia/epoca).\n\n"
            f"CADA escena_descripcion debe:\n"
            f"- Describir EXACTAMENTE que se ve mientras se narra este bloque\n"
            f"- Estar ambientada en {theme_context.era} (misma epoca, misma geografia)\n"
            f"- Compartir al menos UN elemento visual (luz, paisaje, clima, textura del\n"
            f"  terreno) con la escena ANTERIOR para crear un HILO VISUAL CONTINUO\n"
        )

    # ── Build the full prompt ────────────────────────────────
    return f"""Eres un guionista y divulgador historico especializado en expediciones tragicas, naufragios, desastres de montaña y supervivencia extrema. Tu mision es transformar contenido crudo sobre expediciones reales (articulos de Wikipedia, hilos de Reddit, diarios de exploradores, documentos historicos) en guiones documentales de video-ensayo para YouTube, narrados en español latinoamericano neutro con un tono grave, tenso y profundamente humano. El estilo debe evocar documentales de supervivencia como "Free Solo", "Touching the Void" o los mejores episodios de National Geographic sobre exploracion polar — cinematografico y tenso, documental y riguroso, humanamente devastador.{mode_banner}{chunk_banner}{theme_banner}

IDENTIDAD DEL CANAL: {style_desc}

TONO NARRATIVO:
{tone}

ARCO EMOCIONAL que debe seguir la narracion:
{emotions_text}

PALABRAS CLAVE SEO: {seo_keywords_text}
PALABRAS DE PODER PARA TITULOS: {power_words_text}

ESTRUCTURA NARRATIVA "ESPIRAL DEL HIELO" (7 pasos, debes seguir este orden rigurosamente):
{structure_text}
{retention_text}

REGLA DE APERTURA (HOOK):
{hook_rule}

REGLA DE CIERRE:
El ultimo bloque (tipo "cierre") debe contener SOLO la reflexion final y la conclusion del tema.
NO incluyas llamadas a la accion (suscribete, like, campana, comparte, etc.).
Las llamadas a la accion se añaden automaticamente en una seccion separada DESPUES de que termine el video.
El cierre debe sentirse como un final narrativo completo, no como un anuncio.

⚠️ ZONA GANCHO BLINDADA — primeros 120-180 segundos (¡LA PARTE MAS IMPORTANTE DEL GUION!):
El espectador decide en los primeros 30 segundos si se queda o se va. No puedes fallar aqui.
Estructura obligatoria de 4 micro-fases:

FASE 1 — EL GOLPE (0:00-0:15):
  - UNA sola frase de IMPACTO PURO. Sin contexto. Sin presentacion.
  - NUNCA: "Hola", "Bienvenidos", "En este video", "Hoy vamos a", "Les voy a contar".
  - SIEMPRE: un DATO DEMOLEDOR, un HECHO CONCRETO con FECHA y NUMERO, o un CLIFFHANGER.
  - Ej: "El 19 de mayo de 1845, 129 hombres zarparon hacia el Artico. Ninguno volvio."

FASE 2 — LA PROMESA (0:15-0:45):
  - Enumera EXPLICITAMENTE lo que el espectador va a descubrir si se queda.
  - Crea un "contrato narrativo" en 3 puntos.

FASE 3 — PRIMERA REVELACION (0:45-1:30):
  - Suelta YA informacion fascinante y CONCRETA. No te la guardes para el final.
  - El espectador se queda porque YA le has dado valor.

FASE 4 — PRIMER CLIFFHANGER + LANZAMIENTO DE SUBTRAMAS (1:30-3:00):
  - Deja la PRIMERA pregunta sin responder.
  - PRESENTA las subtramas (ver estructura de subtramas mas abajo).

📺 ESTRUCTURA DE SUBTRAMAS PARALELAS (como serie documental de Netflix):
El video debe funcionar como una serie con multiples hilos narrativos, NO como un ensayo lineal.

REGLAS:
1. IDENTIFICA 3-4 SUBTRAMAS que se presentan en los primeros 3 minutos.
2. AVANZA cada subtrama en RONDAS como capitulos de serie.
3. REGLA DE ALTERNANCIA: NUNCA dediques mas de 2 bloques SEGUIDOS a la misma subtrama.

⚡ PATTERN INTERRUPTS — cada 2-3 minutos debes ROMPER el ritmo:
  • [TEXTO_PANTALLA: "129 hombres. 0 supervivientes."]
  • PREGUNTA RETORICA + PAUSA 2s
  • DATO NUMERICO aislado
  • TESTIMONIO de un superviviente o testigo

🔗 MICRO-CLIFFHANGERS — al final de CADA capitulo/seccion:
  "Pero lo que encontraron en el diario del capitan..."
  "La pregunta que los investigadores siguen sin responder..."
  "Y entonces, un satelite capto algo en el hielo..."

📊 DATOS EN PANTALLA [TEXTO_PANTALLA: "..."]:
Cada 2-3 minutos, inserta UN dato textual para quemar en pantalla:
- Formato: [TEXTO_PANTALLA: "frase de maximo 12 palabras"] dentro del texto del bloque.

REGLA DEL ENGANCHE INICIAL (primeros 2-3 minutos — ¡CRITICO para retencion!):
Los primeros minutos deciden si el espectador se queda hasta el final. Debes:
- Abrir con un momento de tension extrema, un dato tragico impactante o una pregunta que el espectador NECESITE ver respondida.
- Crear una "promesa narrativa": el espectador debe intuir que si se queda, conocera el desenlace de una expedicion tragica.
- NUNCA empezar con frases como "En este video vamos a...", "Hoy conoceremos..." o "Bienvenidos a...".
- La primera oracion del guion debe ser IMPACTANTE. Entra directo al momento mas dramatico.
- APLICA la ZONA GANCHO BLINDADA descrita arriba en los primeros bloques del guion.

REGLA ANTI-REPETICION TEMATICA (¡OBLIGATORIO!):
Cada seccion de la expedicion debe aportar informacion GENUINAMENTE NUEVA que haga AVANZAR la narrativa. PROHIBIDO:
- Repetir los mismos eventos, detalles o tragedias en diferentes bloques. Si ya narraste un momento, NO lo cuentes otra vez.
- Reformular la misma idea con sinonimos. Cada bloque debe explorar un ANGULO DIFERENTE de la expedicion.
- Usar la misma metafora, analogia o recurso retorico mas de una vez.
- Los bloques de cierre deben SINTETIZAR (no repetir) lo ya narrado.
- Si no tienes contenido realmente nuevo que aportar, es MEJOR terminar el guion antes que repetir.

{virality_text}

{ssml_text}

{block_rules}

FORMATO DE RESPUESTA:
Responde con un objeto JSON valido con esta estructura exacta:
{{
  "titulo": "titulo del video (max 65 caracteres, en español latinoamericano neutro, usando palabras de poder y formulas de titulo)",
  "descripcion_seo": "descripcion SEO para el video (2-4 frases en español latino, incluyendo la keyword primaria '{seo_primary}')",
  "hashtags": ["hashtag1", "hashtag2", ...],
  "bloques": [
    {{
      "tipo": "hook|desarrollo|climax|reflexion|cierre",
      "emocion": "emocion dominante",
      "texto": "texto exacto que narra el locutor en este bloque (OBLIGATORIO: CADA BLOQUE {words_per_block_min_prompt}+ palabras, parrafo completo sustancial de {words_per_block_min_prompt}-150 palabras minimo). El texto NO debe estar truncado ni cortado a mitad de frase.",
      "escena_descripcion": "descripcion cinematografica de que se ve en pantalla",
      "search_query_en": "4-7 keywords en ingles para buscar en Pexels/Unsplash",
      "media_tipo": "video|imagen",
      "media_duracion": duracion en segundos (numero)
    }},
    ...
  ]
}}

EL GUION COMPLETO DEBE TENER {word_range_text} DE TEXTO NARRATIVO (suma de campos "texto" de todos los bloques). NO MENOS DE {words_min} PALABRAS. Distribuidas en {block_range_text} bloques narrativos. El video durara aproximadamente {duration_range_text} minutos. CADA BLOQUE debe tener al menos {words_per_block_min_prompt} palabras.

IMPORTANTE: Responde UNICAMENTE con el JSON. Sin explicaciones, sin markdown, sin texto fuera del JSON. El JSON debe ser parseable por json.loads() de Python — asegura cerrar todas las comillas, comas y llaves correctamente. NUNCA trunques el ultimo bloque. Si te acercas al limite de tokens, acorta bloques anteriores en lugar de truncar el ultimo.

IMPORTANTE SOBRE EL TEXTO: Cada campo "texto" en los bloques debe ser un PARRAFO COMPLETO. No frases sueltas. No oraciones cortadas a la mitad. Parrafos de {words_per_block_min_prompt} a 150 palabras cada uno."""


USER_PROMPT_TEMPLATE = """Título de la fuente: {title}
Origen: {source} | Subreddit: {subreddit} | Puntuación: {score}
Categoría: {category}

Contenido:
{text}"""


def format_user_prompt(content_item: dict) -> str:
    """Format the user prompt template with content item fields."""
    return USER_PROMPT_TEMPLATE.format(
        title=content_item.get("title", "Sin título"),
        source=content_item.get("source", "desconocida"),
        subreddit=content_item.get("subreddit", "N/A"),
        score=content_item.get("score", 0),
        category=content_item.get("category", "expediciones"),
        text=content_item.get("text", ""),
    )
