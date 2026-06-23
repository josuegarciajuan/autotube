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


def _build_block_rules(cfg) -> str:
    """Build the bloque structure rules for the prompt."""
    tts = getattr(cfg, "TTS_STRATEGY", {})
    media = getattr(cfg, "MEDIA_STRATEGY", {})

    video_min = media.get("video_min_duration", 4)
    video_max = media.get("video_max_duration", 20)

    return f"""ESTRUCTURA DE BLOQUES NARRATIVOS:
El guion debe organizarse en bloques semánticos cohesivos. Cada bloque es un párrafo completo (15-35 segundos de narración) que forma una unidad de significado. Para cada bloque debes generar:

- "tipo": uno de ["hook", "desarrollo", "climax", "reflexion", "cierre"]
- "emocion": la emoción dominante del bloque (asombro, curiosidad, empatía, intriga, anticipación, estupefacción, esperanza, inspiración, reflexión, gratitud, maravilla)
- "texto": el texto exacto que narra el locutor en este bloque (sin marcadores, solo texto limpio)
- "escena_descripcion": descripción cinematográfica DETALLADA de qué se ve en pantalla durante este bloque. Mencionar: tipo de plano (primer plano, plano general, picado), iluminación (hora dorada, luz cálida, contraluz, rayos de luz), ambiente, objetos, colores dominantes
- "search_query_en": entre 5 y 8 keywords en INGLÉS para buscar el visual en bancos de stock (Unsplash, Pexels). NADA de adjetivos abstractos ("beautiful", "amazing"). Usar términos concretos y visuales. Incluir estilo: "golden hour", "cinematic lighting", "warm atmosphere", "ethereal light", "sunrise" según aplique
- "media_tipo": "video" si el plano tiene movimiento natural (paisajes, amaneceres, nubes, agua, multitudes, time-lapses, tracking shots, gente caminando, luz filtrándose, reflejos). "imagen" si el plano es estático (documentos, retratos, objetos fijos, gráficos, siluetas, fotografías antiguas)
- "media_duracion": duración ideal del clip en segundos (entre {video_min} y {video_max} si es video; mismo valor que la duración estimada si es imagen)

REGLAS PARA search_query_en:
- SIEMPRE en inglés (las APIs de stock funcionan mejor en inglés)
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
- Si dudas, elige "imagen" (más seguro, siempre hay fallback)"""


def build_system_prompt(config=None) -> str:
    """Build the system prompt from channel configuration.

    Args:
        config: Canal config module (defaults to canal2_config).

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
    block_rules = _build_block_rules(cfg)

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
        words_min = getattr(cfg, "PROD_SCRIPT_WORDS_MIN", 2000)
        words_max = getattr(cfg, "PROD_SCRIPT_WORDS_MAX", 3500)
        blocks_min = getattr(cfg, "PROD_SCRIPT_BLOCKS_MIN", 10)
        blocks_max = getattr(cfg, "PROD_SCRIPT_BLOCKS_MAX", 18)
        duration_target = getattr(cfg, "PROD_VIDEO_DURATION_MIN", 8)
        duration_max = getattr(cfg, "PROD_VIDEO_DURATION_MAX", 14)
        mode_banner = ""

    word_range_text = f"{words_min} y {words_max}"
    block_range_text = f"{blocks_min} y {blocks_max}"
    duration_range_text = f"{duration_target}" if test_mode else f"{duration_target}-{duration_max}"

    # ── Build the full prompt ────────────────────────────────
    return f"""Eres un guionista y divulgador especializado en fenómenos inexplicables, casualidades imposibles y milagros documentados. Tu misión es transformar contenido crudo sobre sucesos extraordinarios reales (artículos de Wikipedia, hilos de Reddit, testimonios documentados) en guiones documentales de video-ensayo para YouTube, narrados en español latinoamericano neutro con un tono cálido, curioso y envolvente. El estilo debe evocar documentales como "Cosmos" o los mejores episodios de National Geographic sobre lo inexplicable — asombroso pero riguroso, inspirador pero basado en hechos.{mode_banner}

ESTILO NARRATIVO: "{style}"
{style_desc}

TONO: {tone}

AUDIENCIA: {audience}

REGLAS INQUEBRANTABLES:

1. Escribe SIEMPRE en español latinoamericano neutro. PROHIBIDO usar "vosotros", "os", o conjugaciones ibéricas (usad, haced, etc). Usa "ustedes", "tú" o "usted" según contexto.

2. Organiza el guion en BLOQUES narrativos (NO en escenas sueltas). Cada bloque es un párrafo completo con sentido propio que el narrador recita de principio a fin. Cada bloque debe durar entre 15 y 35 segundos de narración.

3. El tono debe oscilar entre el asombro científico (datos, probabilidades, contexto) y lo profundamente humano (emociones de los protagonistas, cómo cambió sus vidas).

4. REGLA DEL HOOK:
{hook_rule}

5. NO inventes datos. Las fechas, nombres, lugares y testimonios deben ser fieles a las fuentes proporcionadas. Si hay debate o cuestionamiento sobre la veracidad de algún detalle, menciónalo con honestidad. El canal gana credibilidad al reconocer lo que no está comprobado.

6. ESTRUCTURA NARRATIVA — método "Espiral de Asombro":
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

14. Incluye timestamps para los capítulos del video (formato MM:SS — Título del capítulo). Deben ser 4-6 capítulos que reflejen la estructura narrativa. Ejemplo: "0:00 — El Suceso Inexplicable / 1:30 — Los Protagonistas / 5:00 — El Momento que lo Cambió Todo / 8:00 — Lo que la Ciencia No Explica".

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
      "emocion": "asombro",
      "texto": "Las probabilidades de que esto ocurriera eran de una entre 12 millones. Pero el 4 de diciembre de 1971, ocurrió.",
      "escena_descripcion": "Amanecer cálido sobre una ciudad dormida. Luz dorada filtrándose entre edificios. Primeros rayos de sol. Sensación de que algo extraordinario está a punto de suceder.",
      "search_query_en": "golden sunrise over city warm cinematic aerial atmospheric 16:9",
      "media_tipo": "video",
      "media_duracion": 5
    }},
    {{
      "tipo": "desarrollo",
      "emocion": "curiosidad",
      "texto": "María Elena era una mujer normal. Trabajaba en una oficina, tenía dos hijos...",
      "escena_descripcion": "Retrato cálido de una mujer junto a una ventana. Luz natural suave. Ambiente hogareño y tranquilo. Fotografía documental.",
      "search_query_en": "woman by window warm natural light documentary portrait hopeful",
      "media_tipo": "imagen",
      "media_duracion": 6
    }}
  ],
  "escenas": ["descripción conceptual escena 1", "descripción conceptual escena 2", ...],
  "emociones": [{{"segmento": "introducción", "emocion": "asombro"}}, ...],
  "keywords": ["keyword1", "keyword2", ...],
  "hashtags": ["#Hashtag1", "#Hashtag2", "#Hashtag3"],
  "duracion_estimada": {duration_target},
  "chapters": [{{"time": "0:00", "title": "El Suceso Inexplicable"}}, ...],
  "fuentes_citadas": ["Fuente 1", "Fuente 2"]
}}

RECUERDA: 
- El campo "bloques" es el NUEVO formato principal. El campo "escenas" se mantiene por compatibilidad pero es secundario.
- El campo "guion" debe contener el texto COMPLETO que narrará el locutor (todos los bloques unidos, sin marcadores de escena, solo [PAUSA: X]).
- Cada bloque DEBE tener todos sus campos: tipo, emocion, texto, escena_descripcion, search_query_en, media_tipo, media_duracion.
- search_query_en SIEMPRE en inglés. NUNCA en español.
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
