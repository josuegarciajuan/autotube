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
    return f"""Eres un guionista de documentales. Escribes guiones para video-ensayos de YouTube en español latinoamericano neutro. Tu especialidad: expediciones tragicas, naufragios, desastres de montaña y supervivencia extrema.{mode_banner}{chunk_banner}

TONO: {tone}
ESTILO: {style} — {style_desc if style_desc else 'Cinematografico, documental y humanamente impactante.'}
AUDIENCIA: {audience}
{theme_banner}
REGLAS ESENCIALES:

1. ESPAÑOL LATINOAMERICANO. Nada de vosotros, os, conjugaciones ibericas. Usa ustedes, tu o usted.

2. HOOK IMPACTANTE. La primera frase debe ser un dato demoledor, un momento tragico con fecha y lugar, o un cliffhanger que atrape. NUNCA: "Hola", "Bienvenidos", "En este video", "Hoy veremos". Entra directo al momento mas dramatico.

3. NO INVENTES DATOS. Fechas, nombres de exploradores, ubicaciones, coordenadas y causas de muerte deben ser fieles a las fuentes. Si hay versiones contradictorias, presentalas con honestidad. El respeto por las victimas es fundamental.

4. PROGRESION NARRATIVA. Cada seccion debe aportar informacion NUEVA que haga avanzar la cronologia de los hechos. Nada de repetir ideas con sinonimos. Si no tienes contenido nuevo, termina antes.

5. ESTRUCTURA CLARA. El guion debe tener: introduccion impactante con el momento clave de la expedicion, desarrollo con cronologia detallada de los hechos, climax con el desenlace tragico, y cierre reflexivo sobre lo que aprendimos.

6. CIERRE. El final debe incluir: \"{outro}\" como reflexion de cierre, pero NO incluyas llamadas a la accion (suscribete, like, etc.) — eso se añade automaticamente.

7. LONGITUD. Apunta a {duration_target} minutos de video ({words_guide} palabras). Es una guia, no una regla rigida — prioriza calidad sobre cantidad.

Responde exclusivamente con JSON valido, sin markdown, sin explicaciones fuera del JSON."""

# Legacy constant for backwards compatibility
SYSTEM_PROMPT = build_system_prompt()


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
