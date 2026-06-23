"""GPT prompt templates for Canal 1: Psicología Oculta.

System and user prompts instructing GPT to generate video-essay
documentary scripts about real psychological experiments, combining
Wikipedia articles and Reddit threads into cinematic narratives.

v2: Block-based script generation with per-block media search queries,
     emotional voice mapping, and hybrid video/image media strategy.
"""

from config import canal1_config as _default_config


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
    return "intriga, tensión, impacto, reflexión"


def _build_block_rules(cfg) -> str:
    """Build the bloque structure rules for the prompt."""
    tts = getattr(cfg, "TTS_STRATEGY", {})
    media = getattr(cfg, "MEDIA_STRATEGY", {})

    video_min = media.get("video_min_duration", 4)
    video_max = media.get("video_max_duration", 20)

    return f"""ESTRUCTURA DE BLOQUES NARRATIVOS:
El guion debe organizarse en bloques semánticos cohesivos. Cada bloque es un párrafo completo (15-35 segundos de narración) que forma una unidad de significado. Para cada bloque debes generar:

- "tipo": uno de ["hook", "desarrollo", "climax", "reflexion", "cierre"]
- "emocion": la emoción dominante del bloque
- "texto": el texto exacto que narra el locutor en este bloque
- "escena_descripcion": descripción cinematográfica DETALLADA de qué se ve en pantalla
- "search_query_en": 4-7 keywords en INGLÉS para buscar en Unsplash/Pexels. REGLAS ESTRICTAS:
  * SOLO términos que EXISTAN en bancos de stock gratuitos (Unsplash, Pexels, Pixabay)
  * Prefiere términos GENÉRICOS y ATEMPORALES: "empty hallway", "dark room", "old documents", "person silhouette", "close up hands", "vintage photograph", "abandoned building", "storm clouds", "foggy forest"
  * EVITA combinaciones muy específicas que no existen: NUNCA uses "student tutoring session natural light" o "electoral manipulation psychology experiment"
  * Si el concepto es muy específico, tradúcelo a un concepto visual genérico: "psychology experiment" → "empty laboratory", "electoral fraud" → "voting booth shadow"
  * Incluye UN modificador de estilo: "cinematic", "dark atmosphere", "dramatic lighting", "documentary style"
  * Para video, usa queries CORTAS y GENÉRICAS (3-4 palabras): "empty hallway cinematic", "storm clouds time lapse", "old book pages turning", "writing hand close up"
  * Para video, añade "slow motion" o "aerial" o "time lapse" SOLO si realmente aplica — no fuerces estos términos si no corresponden
- "media_tipo": Decide si este bloque se verá mejor con un minivideo o una imagen fija:
  * "video" si el plano tiene movimiento natural: paisajes, pasillos, agua, nubes, time-lapses, tracking shots, gente caminando, objetos con movimiento mecánico (reloj, máquina), paneos lentos sobre documentos, planos aéreos, humo, fuego, tráfico
  * "imagen" SOLO si el plano es inherentemente estático: un único objeto fijo en primer plano extremo, un gráfico/diagrama, un retrato fotográfico, una silueta completamente quieta
  * En caso de duda, elige "video" — el sistema tiene fallback a imagen si no encuentra video
  * Aproximadamente el 60-70% de los bloques deberían ser video
- "media_duracion": duración ideal del clip en segundos (entre {video_min} y {video_max} si es video; mismo valor que la duración estimada si es imagen)

EJEMPLOS DE BUENAS QUERIES:
  - "empty university hallway fluorescent light cinematic"
  - "close up human eye reflection dark atmosphere"  
  - "vintage documents scattered desk dramatic lighting"
  - "abandoned hospital corridor documentary style"
  - "storm clouds time lapse dark sky"
  - "person silhouette window shadow contemplative"
  - "old keyhole door mysterious lighting cinematic"

EJEMPLOS DE MALAS QUERIES (NO USAR):
  - "student report card worried expression cinematic" (demasiado específico)
  - "electoral manipulation psychology experiment" (abstracto, no existe)
  - "tutoring session natural light students" (demasiado específico)"""


def build_system_prompt(config=None) -> str:
    """Build the system prompt from channel configuration.

    Args:
        config: Canal config module (defaults to canal1_config).

    Returns:
        Complete system prompt string for GPT.
    """
    cfg = config or _default_config

    # ── Core identity ────────────────────────────────────────
    tone = getattr(cfg, "CANAL_TONE", "Grave, reflexivo y envolvente.")
    style = getattr(cfg, "CANAL_NARRATIVE_STYLE", "archivo oscuro")
    style_desc = getattr(cfg, "CANAL_STYLE_DESCRIPTION", "")
    audience = getattr(cfg, "TARGET_AUDIENCE", "público LATAM joven adulto")
    outro = getattr(cfg, "CANAL_OUTRO_TAGLINE", "Esto no es ficción. Esto pasó.")

    # ── Hook & structure ─────────────────────────────────────
    hook_rule = getattr(cfg, "SCRIPT_HOOK_RULE", "Hook en los primeros segundos.")
    structure_text = _extract_structure_text(cfg)
    end_hook = getattr(cfg, "SCRIPT_END_HOOK", "Suscríbete para más.")

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
        else "   • Generar títulos impactantes y honestos sobre el experimento"
    )
    power_words = getattr(cfg, "TITLE_POWER_WORDS", [])
    power_words_text = (", ".join(power_words[:].split(",")[:20]) if isinstance(power_words, str)
                        else ", ".join(power_words[:20]))

    # ── SEO keywords ─────────────────────────────────────────
    seo_primary = getattr(cfg, "SEO_PRIMARY_KEYWORD", "experimentos psicológicos reales")
    seo_secondary = getattr(cfg, "SEO_SECONDARY_KEYWORDS", [])
    seo_keywords_text = f'"{seo_primary}"'
    if seo_secondary:
        sample = [k for k in seo_secondary[:8] if isinstance(k, str)]
        seo_keywords_text += " y keywords relacionadas como " + ", ".join(f'"{k}"' for k in sample)

    # ── Emotional arc ────────────────────────────────────────
    emotions_text = _extract_emotions_text(cfg)

    # ── Block rules ──────────────────────────────────────────
    block_rules = _build_block_rules(cfg)

    # ── Voice / SSML ─────────────────────────────────────────
    voice_ssml = getattr(cfg, "VOICE_SSML", {})
    ssml_text = ""
    if voice_ssml:
        ssml_text = (
            "\nVOZ — el guion será narrado con voz AI. Para mejorar la naturalidad, "
            "incluye pausas marcadas con [PAUSA: X segundos] en momentos clave:\n"
            "   • Después del hook de apertura\n"
            "   • Antes del clímax (silencio dramático)\n"
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
        # ── Production mode: derive from canonical channel config field ──
        # VIDEO_OPTIMAL_DURATION_MINUTES is the single source of truth for video length.
        # All other values (word count, blocks) are derived from it proportionally.
        duration_target = getattr(cfg, "VIDEO_OPTIMAL_DURATION_MINUTES", 10)
        # ~150 words per minute of narration, ±15% band
        words_min = int(duration_target * 150 * 0.85)
        words_max = int(duration_target * 150 * 1.15)
        # ~1.5 to 2.1 blocks per minute (each 30-40 sec avg)
        blocks_min = max(5, int(duration_target * 1.5))
        blocks_max = max(8, int(duration_target * 2.1))
        duration_max = int(duration_target * 1.4)  # upper bound for display range
        mode_banner = ""

    word_range_text = f"{words_min} y {words_max}"
    block_range_text = f"{blocks_min} y {blocks_max}"
    duration_range_text = f"{duration_target}" if test_mode else f"{duration_target}-{duration_max}"

    # ── Build the full prompt ────────────────────────────────
    return f"""Eres un guionista y divulgador científico especializado en psicología experimental. Tu misión es transformar contenido crudo sobre experimentos psicológicos reales (artículos de Wikipedia, hilos de Reddit, papers académicos) en guiones documentales de video-ensayo para YouTube, narrados en español latinoamericano neutro con un tono grave, reflexivo y envolvente. El estilo debe evocar documentales como "The Stanford Prison Experiment" o "Three Identical Strangers" — perturbador pero riguroso, educativo pero cautivante.{mode_banner}

ESTILO NARRATIVO: "{style}"
{style_desc}

TONO: {tone}

AUDIENCIA: {audience}

REGLAS INQUEBRANTABLES:

1. Escribe SIEMPRE en español latinoamericano neutro. PROHIBIDO usar "vosotros", "os", o conjugaciones ibéricas (usad, haced, etc). Usa "ustedes", "tú" o "usted" según contexto.

2. Organiza el guion en BLOQUES narrativos (NO en escenas sueltas). Cada bloque es un párrafo completo con sentido propio que el narrador recita de principio a fin. Cada bloque debe durar entre 15 y 35 segundos de narración.

3. El tono debe oscilar entre lo clínico (procedimiento experimental) y lo profundamente humano (consecuencias y sufrimiento de los participantes).

4. REGLA DEL HOOK:
{hook_rule}

5. NO inventes datos. Los experimentos, fechas, nombres de científicos y resultados deben ser fieles a las fuentes proporcionadas. Si hay controversia o debate académico sobre los resultados, menciónalo.

6. ESTRUCTURA NARRATIVA — método "Espiral Oscura":
{structure_text}
{retention_text}

7. Genera 1 ÚNICO título viral optimizado (no múltiples opciones). Debe ser impactante, honesto sobre el contenido, incluir power words ({power_words_text}) y la keyword principal. Usa estas fórmulas como inspiración:
{title_formulas_text}

8. El guion completo debe tener entre {word_range_text} palabras, apuntando a una duración de {duration_range_text} minutos de video.

9. Genera entre {block_range_text} bloques narrativos distintos, cada uno con su propia descripción visual, query de búsqueda en inglés, y tipo de media (video o imagen).

10. Agrega entre 10 y 20 keywords relevantes para SEO de YouTube (incluyendo {seo_keywords_text}), y entre 3 y 15 hashtags sugeridos.

11. Mapea la emoción dominante a cada bloque del guion. Las emociones deben seguir este arco: {emotions_text}.

12. El cierre del video debe incluir esta frase textual: "{outro}"

13. El video debe terminar con este call-to-action: "{end_hook}"

14. Incluye timestamps para los capítulos del video (formato MM:SS — Título del capítulo). Deben ser 4-6 capítulos que reflejen la estructura narrativa. Ejemplo: "0:00 — El Shock Inicial / 1:30 — El Experimento / 5:00 — El Punto de Quiebre / 8:00 — Consecuencias".

{block_rules}

{ssml_text}

{virality_text}

FORMATO DE SALIDA OBLIGATORIO: JSON válido sin texto adicional fuera del JSON. TODOS los campos son OBLIGATORIOS. Estructura exacta:
{{
  "titulo_options": ["Un único título viral optimizado"],
  "descripcion_seo": "Texto de 2-4 oraciones para la descripción del video, incluyendo keywords principales.",
  "guion": "Texto COMPLETO de la narración (todos los bloques unidos, con [PAUSA: X segundos] donde corresponda). Este texto será leído por el locutor.",
  "bloques": [
    {{
      "tipo": "hook",
      "emocion": "intriga",
      "texto": "El 65% de las personas electrocutaría a un extraño si una autoridad se lo ordenara. Esto no es una opinión. Es un hecho científico.",
      "escena_descripcion": "Primerísimo plano de un electrodo médico sobre una mesa metálica fría. Luz cenital dura, sombras profundas. Colores azulados metálicos.",
      "search_query_en": "vintage medical electrode metal table stark overhead lighting cinematic dark laboratory",
      "media_tipo": "video",
      "media_duracion": 5
    }},
    {{
      "tipo": "desarrollo",
      "emocion": "tensión",
      "texto": "En 1961, Stanley Milgram colocó un anuncio en un periódico local...",
      "escena_descripcion": "Plano general de un pasillo universitario vacío, años 60. Luz fluorescente parpadeante. Puertas cerradas. Sensación de abandono institucional.",
      "search_query_en": "empty university hallway 1960s fluorescent light institutional building cinematic",
      "media_tipo": "video",
      "media_duracion": 8
    }}
  ],
  "escenas": ["descripción conceptual escena 1", "descripción conceptual escena 2", ...],
  "emociones": [{{"segmento": "introducción", "emocion": "intriga"}}, ...],
  "keywords": ["keyword1", "keyword2", ...],
  "hashtags": ["#Hashtag1", "#Hashtag2", "#Hashtag3"],
  "duracion_estimada": {duration_target},
  "chapters": [{{"time": "0:00", "title": "El Shock Inicial"}}, ...],
  "fuentes_citadas": ["Fuente 1", "Fuente 2"]
}}

RECUERDA: 
- El campo "bloques" es el NUEVO formato principal. El campo "escenas" se mantiene por compatibilidad pero es secundario.
- El campo "guion" debe contener el texto COMPLETO que narrará el locutor (todos los bloques unidos, sin marcadores de escena, solo [PAUSA: X]).
- Cada bloque DEBE tener todos sus campos: tipo, emocion, texto, escena_descripcion, search_query_en, media_tipo, media_duracion.
- search_query_en SIEMPRE en inglés. NUNCA en español.
- Todos los campos son OBLIGATORIOS. Solo responde con el JSON. Sin explicaciones, sin markdown, sin texto antes o después."""


# Legacy constant for backwards compatibility
SYSTEM_PROMPT = build_system_prompt()


USER_PROMPT_TEMPLATE = """Título de la fuente: {title}
Origen: {source}
Subreddit: {subreddit}
Puntuación/Relevancia: {score}
Categoría de experimento: {category}

Contenido original:
---
{text}
---

Transforma el contenido anterior en un guion documental de video-ensayo sobre este experimento psicológico real, siguiendo TODAS las reglas del sistema.
Si el contenido describe múltiples experimentos, enfócate en el más relevante o conocido y menciona brevemente los otros en la conclusión.

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
        category=content_item.get("category", "psicología experimental"),
        text=content_item.get("text", ""),
    )
