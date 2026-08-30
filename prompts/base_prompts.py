"""Base prompt templates — parameterized for any channel.

All prompt functions receive a channel config object and derive
niche-specific language from ``CANAL_NARRATIVE_STYLE``, ``CANAL_TONE``,
``CANAL_STYLE_DESCRIPTION``, ``TARGET_AUDIENCE``, etc.

This replaces per-channel ``prompts/canal*_prompts.py`` files.
No more ``from prompts.canal2_prompts import ...`` fallbacks needed.
"""

from typing import Optional


# ═══════════════════════════════════════════════════════════════════
# Shared helpers (identical across all channels)
# ═══════════════════════════════════════════════════════════════════

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
                lines.append(f"       ANCLA DE RETENCION: {anchor}")
        else:
            lines.append(f"   ({chr(97 + i)}) {str(item)}")
    return "\n".join(lines)


def _extract_emotions_text(cfg) -> str:
    """Build the emotional arc description."""
    arc = getattr(cfg, "SCRIPT_EMOTIONAL_ARC", {})
    if isinstance(arc, dict):
        return ", ".join(f"{k}->{v}" for k, v in arc.items())
    if isinstance(arc, list):
        return ", ".join(arc)
    return "asombro, curiosidad, intriga, anticipacion, reflexion"


def _niche_substance_rule(cfg) -> str:
    """Return a niche-specific credibility rule based on the channel's style."""
    style = str(getattr(cfg, "CANAL_NARRATIVE_STYLE", "")).lower()
    if "arqueologico" in style or "civilizacion" in style or "historia" in style:
        return "El rigor historico es fundamental."
    if "supervivencia" in style or "expedicion" in style:
        return "El respeto por las victimas es fundamental."
    if "medic" in style or "clinico" in style:
        return "La rigurosidad clinica es fundamental."
    return "La credibilidad es lo mas importante."


def packaging_rules(cfg) -> str:
    """Shared evidence-first rules for scripts and metadata prompts."""
    formulas = ", ".join(getattr(cfg, "TITLE_FORMULAS", [])[:6])
    return f"""REGLAS DE PACKAGING EVIDENCE-FIRST:
- El título debe identificar un caso concreto (persona/lugar y fecha o año cuando existan), no una promesa genérica.
- Separa siempre HECHO documentado, INTERPRETACIÓN propuesta y DESCONOCIDO; no presentes hipótesis como hechos.
- Evita fórmulas repetitivas de shock, "oculto", "real", "prohibido" y superlativos vacíos.
- Usa una sola idea visual legible en la miniatura y texto corto; no uses sellos de credibilidad como sustituto de evidencia.
- Fórmulas configuradas para este canal: {formulas}
"""


def _build_block_rules(cfg, theme_context=None, word_target=None) -> str:
    """Build the bloque structure rules for the prompt.

    Block duration is computed dynamically from the video duration target.
    """
    media = getattr(cfg, "MEDIA_STRATEGY", {})
    video_min = media.get("video_min_duration", 4)
    video_max = media.get("video_max_duration", 20)
    SCENE_MIN_SEC = 5
    SCENE_MAX_SEC = 12

    # Dynamic block duration from word_target
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
    if theme_context and hasattr(theme_context, "theme_keywords_en") and theme_context.theme_keywords_en:
        theme_kw_str = ", ".join(theme_context.theme_keywords_en[:5])
        forbidden = getattr(theme_context, "forbidden_elements", [])
        forbidden_str = ", ".join(forbidden) if forbidden else "ninguno"
        tema = getattr(theme_context, "genre", "historico")
        tema_desc = "de epoca/ambiente" if not ("medic" in tema.lower() or "clinico" in tema.lower()) else "de ambiente clinico/cientifico"

        theme_rules = f"""
REGLAS DE COHERENCIA VISUAL (OBLIGATORIO):
- CADA search_query_en debe ser una FUSION de DOS partes (ambas obligatorias, en este orden):
  (1) SUJETO NARRATIVO (~60%): 2-3 keywords que describen EXACTAMENTE lo que se narra en ESTE bloque.
  (2) AMBIENTACION TEMATICA (~40%): 1-2 keywords {tema_desc} extraidas de: {theme_kw_str}.
  PROHIBIDO que dos escenas consecutivas usen la MISMA keyword de anclaje tematico.
  PROHIBIDO mostrar elementos de: {forbidden_str}"""

    return f"""ESTRUCTURA DE BLOQUES NARRATIVOS:
El guion debe organizarse en bloques semanticos cohesivos. Cada bloque es un parrafo completo ({block_dur_range} de narracion) que forma una unidad de significado. Para cada bloque debes generar:

- "tipo": uno de ["hook", "desarrollo", "climax", "reflexion", "cierre"]
- "emocion": la emocion dominante del bloque
- "texto": el texto exacto que narra el locutor en este bloque (sin marcadores, solo texto limpio)
- "escena_descripcion": descripcion cinematografica DETALLADA de que se ve en pantalla durante este bloque
- "search_query_en": entre 5 y 8 keywords en INGLES para buscar en bancos de stock. Usar terminos concretos y visuales. Incluir estilo: "golden hour", "cinematic lighting", "16:9" segun aplique
- "media_tipo": "video" si el plano tiene movimiento natural (paisajes, agua, multitudes, tracking shots). "imagen" si el plano es estatico (documentos, retratos, objetos fijos)
- "media_duracion": duracion ideal del clip en segundos (entre {video_min} y {video_max} si es video)

REGLAS PARA search_query_en:
- FORMATO OBLIGATORIO DE DOS PARTES: [SUJETO NARRATIVO] + [ANCLAJE TEMATICO]
- LO QUE VES = LO QUE OYES: la query debe traducir visualmente lo que se narra
- SIEMPRE en ingles
- Incluir modificadores de estilo: "cinematic", "16:9", "documentary style"
- Limitarse a terminos que EXISTAN en bancos de stock gratuitos (Unsplash, Pexels, Pixabay)

REGLAS DE FRAGMENTACION:
- Cada bloque debe durar aprox {min_frases}-{max_frases} frases (min {words_per_block_min} palabras).
- Si una idea requiere mas de {block_dur_max}s, dividela en varios bloques consecutivos.
- Los bloques consecutivos deben tener progresion visual coherente.{theme_rules}"""


# ═══════════════════════════════════════════════════════════════════
# OUTLINE PROMPT
# ═══════════════════════════════════════════════════════════════════

def build_outline_prompt(config, duration_min: float = 15, word_target: int = 2500) -> str:
    """Generate a structured outline BEFORE writing any narrative blocks.

    Produces 4-6 chapters with concrete facts, preventing the LLM from
    producing rambling, repetitive, or factually empty narration.
    """
    tone = getattr(config, "CANAL_TONE", "Documental, riguroso.")
    style = getattr(config, "CANAL_NARRATIVE_STYLE", "documental")
    audience = getattr(config, "TARGET_AUDIENCE", "publico LATAM adulto curioso")
    n_chapters = min(6, max(4, int(duration_min / 3)))

    return f"""Eres un editor de documentales y divulgador especializado.
TONO: {tone}
ESTILO: {style}
AUDIENCIA: {audience}
DURACION OBJETIVO: {duration_min} minutos (~{word_target} palabras)

Tu tarea es generar UN SOLO OUTLINE ESTRUCTURADO para un video documental. NO escribas el guion — solo el outline.

REGLAS INQUEBRANTABLES:
1. Genera EXACTAMENTE {n_chapters} capitulos. Cada uno cubre un aspecto DISTINTO del tema.
2. Cada capitulo DEBE incluir AL MENOS 2 HECHOS CONCRETOS: numeros, fechas, nombres, lugares, estadisticas.
3. El outline debe mostrar PROGRESION NARRATIVA: cada capitulo construye sobre el anterior.
4. PROHIBIDO el lenguaje puramente metaforico o poetico sin sustancia.
5. Los hechos deben ser verificables.
6. Cada capitulo necesita keywords visuales en INGLES para la busqueda de stock media.

FORMATO DE SALIDA (JSON):
{{
  "summary": "Resumen de 1-2 frases del arco narrativo completo del video",
  "chapters": [
    {{
      "chapter": 1,
      "titulo": "Titulo del capitulo en espanol",
      "idea_central": "Que revela este capitulo — una frase",
      "hechos_concretos": ["Hecho 1", "Hecho 2", "Hecho 3"],
      "visual_keywords_en": "english keywords for stock media search",
      "emocion_objetivo": "asombro|curiosidad|intriga|revelacion|inspiracion",
      "words_approx": 500
    }}
  ]
}}

RECUERDA: NADA de metaforas vacias. Solo hechos, datos, y estructura narrativa clara."""


# ═══════════════════════════════════════════════════════════════════
# CONTENT-ONLY PROMPT (sequential block generation)
# ═══════════════════════════════════════════════════════════════════

def build_content_only_prompt(config, previous_blocks: list = None,
                               word_guidance: int = 300, source_text: str = None,
                               outline: dict = None, batch_num: int = 0) -> str:
    """Lightweight prompt for sequential block-by-block content generation.

    Strips ALL structural requirements so the LLM focuses exclusively
    on writing compelling narrative content.
    """
    tone = getattr(config, "CANAL_TONE", "Documental, riguroso.")
    style = getattr(config, "CANAL_NARRATIVE_STYLE", "documental")
    audience = getattr(config, "TARGET_AUDIENCE", "publico LATAM adulto curioso")

    # ── Detect marathon mode early (used by both outline + continuity) ──
    is_marathon = previous_blocks and len(previous_blocks) > 40
    n_last = 12 if is_marathon else 6
    n_first = 6 if is_marathon else 3

    # Outline context injection
    outline_context = ""
    if outline and outline.get("chapters"):
        chapters = outline["chapters"]
        completed_blocks = len(previous_blocks) if previous_blocks else 0

        # ── Smart chapter advancement (v24: anti-repetition) ──
        # Marathon: more blocks per chapter (5 vs 3) + fact-coverage check.
        # The LLM only advances to the next chapter when it has actually
        # used the current chapter's key facts in its recent blocks.
        blocks_per_chapter = 5 if is_marathon else 3
        chapter_idx = min(len(chapters) - 1, max(0, completed_blocks // blocks_per_chapter))

        # Fact-coverage check: if recent blocks don't contain keywords from
        # the current chapter's facts, stay on the current chapter.
        if is_marathon and completed_blocks > 0 and chapter_idx < len(chapters) - 1:
            current_facts = chapters[chapter_idx].get("hechos_concretos", [])
            if current_facts:
                # Extract significant words (4+ chars) from facts
                fact_keywords = set()
                for f in current_facts:
                    for w in f.lower().split():
                        if len(w) >= 4:
                            fact_keywords.add(w)
                # Check recent blocks for these keywords
                recent_text = " ".join(
                    b.get("texto", "") for b in previous_blocks[-n_last:]
                    if isinstance(b, dict)
                ).lower()
                matches = sum(1 for kw in fact_keywords if kw in recent_text)
                # Need at least 2 keyword matches to consider chapter "covered"
                if matches < 2:
                    # Stay on current chapter (don't advance)
                    chapter_idx = max(0, chapter_idx - 1)

        current_chapter = chapters[chapter_idx]

        facts_text = "\n".join(f"  - {f}" for f in current_chapter.get("hechos_concretos", [])[:3])
        next_chapter = chapters[chapter_idx + 1].get("titulo", "(final)") if chapter_idx + 1 < len(chapters) else "(final)"

        outline_context = f"""
--- CONTEXTO DEL CAPITULO ---
Escribiendo CAPITULO {chapter_idx + 1}/{len(chapters)}: "{current_chapter.get('titulo', '?')}"
IDEA CENTRAL: {current_chapter.get('idea_central', '')}
EMOCION OBJETIVO: {current_chapter.get('emocion_objetivo', '')}
HECHOS CONCRETOS A INCLUIR:
{facts_text}
PROXIMO CAPITULO: {next_chapter}

REGLAS DE CONTENIDO (OBLIGATORIO):
- Incluye AL MENOS 2 de los hechos concretos listados arriba.
- NADA de metaforas vacias ni lenguaje poetico sin datos.
- Cada bloque debe CONTAR algo real, no 'hablar por hablar'.
- Conecta este capitulo con el siguiente."""

    context_text = ""
    if previous_blocks:
        last_tail_chars = 1200 if is_marathon else 400
        first_head_chars = 500 if is_marathon else 200

        last_texts = []
        for b in previous_blocks[-n_last:]:
            if isinstance(b, dict):
                last_texts.append(b.get("texto", ""))
        all_text = " ".join(last_texts)
        first_blocks = previous_blocks[:n_first] if len(previous_blocks) >= n_first else previous_blocks
        first_words = " ".join(b.get("texto", "") for b in first_blocks if isinstance(b, dict))[:first_head_chars]

        # Marathon: extract already-covered topics to prevent repetition
        covered_topics = ""
        if is_marathon:
            # Sample key phrases from blocks spread across the narrative
            sample_idx = [0, len(previous_blocks) // 4, len(previous_blocks) // 2,
                          3 * len(previous_blocks) // 4, len(previous_blocks) - 1]
            sampled = set()
            for idx in sample_idx:
                if 0 <= idx < len(previous_blocks):
                    b = previous_blocks[idx]
                    if isinstance(b, dict):
                        txt = b.get("texto", "")[:120]
                        if txt:
                            sampled.add(txt)
            if sampled:
                topics = "\n  - ".join(sampled)
                covered_topics = (
                    f"\n⛔ TEMAS YA CUBIERTOS (NO repetir):\n"
                    f"  - {topics}\n"
                )

        context_text = (
            f"\n\n--- CONTINUIDAD NARRATIVA ---\n"
            f"La historia empezo asi: \"{first_words}...\"\n"
            f"Lo ULTIMO que narraste: \"{all_text[-last_tail_chars:]}\"\n"
            f"{covered_topics}"
            f"INSTRUCCIONES DE CONTINUIDAD:\n"
            f"- Continua la narracion desde donde quedo, de forma NATURAL.\n"
            f"- AVANZA la historia: cada bloque debe aportar contenido GENUINAMENTE NUEVO.\n"
            f"- PROHIBIDO repetir los mismos ejemplos o metaforas ya usadas.\n"
            f"- Los bloques de cierre deben SINTETIZAR (no repetir) lo ya dicho.\n"
        )

    source_context = ""
    if source_text:
        source_context = f"\nCONTENIDO FUENTE (usalo como base):\n{source_text[:2000]}\n"

    base_style = str(getattr(config, "CANAL_NARRATIVE_STYLE", "documental"))
    if "medic" in base_style.lower() or "clinico" in base_style.lower():
        specialty = "casos clinicos inexplicables, enfermedades raras y misterios medicos"
    elif "supervivencia" in base_style.lower() or "expedicion" in base_style.lower():
        specialty = "expediciones tragicas, naufragios y supervivencia extrema"
    elif "arqueologico" in base_style.lower() or "civilizacion" in base_style.lower():
        specialty = "civilizaciones perdidas, ruinas antiguas y misterios arqueologicos"
    else:
        specialty = "fenomenos inexplicables e historias asombrosas"

    return f"""Eres un guionista documental para YouTube. Escribes narraciones en espanol latinoamericano neutro. Tu especialidad: {specialty}.

TONO: {tone}
ESTILO: "{style}"
AUDIENCIA: {audience}

REGLAS ESTRICTAS:
1. Espanol latinoamericano neutro. PROHIBIDO vosotros, os, conjugaciones ibericas.
2. NO inventes datos. Usa SOLO la informacion de las fuentes proporcionadas.
3. Cada bloque debe ser un parrafo completo y sustancial.
4. Incluye detalles sensoriales, descripciones vividas y contexto.
5. NO uses relleno ni repeticiones. PROHIBIDO repetir los mismos ejemplos.
6. CRITICO: Cada bloque debe CONTENER al menos un hecho concreto (numero, fecha, nombre, lugar, cita).
7. ENGANCHE INICIAL: Los primeros bloques deben ser ALTAMENTE intrigantes. NUNCA empieces con frases como "En este video vamos a..." o "Hoy hablaremos de...".{source_context}{context_text}{outline_context}

Genera entre 2 y 4 bloques narrativos (~{word_guidance} palabras total).
Cada bloque SOLO necesita el campo "texto".
Responde UNICAMENTE con JSON: {{"bloques": [{{"texto": "parrafo aqui..."}}, ...]}}"""


# ═══════════════════════════════════════════════════════════════════
# MAIN SYSTEM PROMPT
# ═══════════════════════════════════════════════════════════════════

def build_system_prompt(config, word_count_emphasis: float = 1.0,
                         chunk_context: dict = None, theme_context=None,
                         word_target: dict = None) -> str:
    """Build the system prompt for script generation, parameterized per channel."""
    tone = getattr(config, "CANAL_TONE", "Documental, riguroso.")
    style = getattr(config, "CANAL_NARRATIVE_STYLE", "documental")
    style_desc = getattr(config, "CANAL_STYLE_DESCRIPTION", "")
    audience = getattr(config, "TARGET_AUDIENCE", "publico LATAM adulto curioso")
    outro = getattr(config, "CANAL_OUTRO_TAGLINE", "Esto es real.")
    substance = _niche_substance_rule(config)

    # Duration / word guidance
    test_mode = getattr(config, "TEST_MODE", False)
    if test_mode:
        duration_target = getattr(config, "TEST_VIDEO_DURATION_TARGET", 2)
        words_guide = f"~{getattr(config, 'TEST_SCRIPT_WORDS_MIN', 200)}-{getattr(config, 'TEST_SCRIPT_WORDS_MAX', 600)}"
        mode_banner = f"\nMODO PRUEBA: guion corto de {duration_target} min (~{words_guide} palabras).\n"
    elif word_target and "duration_target" in word_target:
        duration_target = word_target["duration_target"]
        words_guide = f"~{word_target['words_min']}-{word_target['words_max']}"
        mode_banner = ""
    else:
        duration_target = getattr(config, "VIDEO_AVERAGE_DURATION_MIN", 15)
        words_guide = f"~{int(duration_target * 150 * 0.85)}-{int(duration_target * 150 * 1.15)}"
        mode_banner = ""

    chunk_banner = ""
    if chunk_context:
        chunk_banner = (
            f"\nCONTINUACION: capitulo {chunk_context.get('order', '?')} "
            f"de {chunk_context.get('total', '?')}. "
            f"Enlaza con: \"{chunk_context.get('last_paragraph', '')}\"\n"
        )

    theme_banner = ""
    if theme_context:
        theme_banner = (
            f"\nCONTEXTO VISUAL: genero={getattr(theme_context, 'genre', '?')}, "
            f"epoca={getattr(theme_context, 'era', '?')}, "
            f"estilo={getattr(theme_context, 'visual_style', '?')}. "
            f"Keywords: {', '.join(getattr(theme_context, 'theme_keywords_en', [])[:5])}. "
            f"Prohibido: {', '.join(getattr(theme_context, 'forbidden_elements', [])) or 'ninguno'}.\n"
        )

    # Derive niche-specific intro line from config
    niche_intro = _specialty_intro(config)

    return f"""{niche_intro}{mode_banner}{chunk_banner}

TONO: {tone}
ESTILO: {style} — {style_desc if style_desc else 'Documental cinematografico.'}
AUDIENCIA: {audience}
{theme_banner}
REGLAS ESENCIALES:

1. ESPANOL LATINOAMERICANO. Nada de vosotros, os, conjugaciones ibericas. Usa ustedes, tu o usted.

2. HOOK IMPACTANTE + PROMESA EN 30-90 SEGUNDOS. La primera frase debe ser un dato demoledor, un hecho concreto con fecha y numero, o un cliffhanger. NUNCA: "Hola", "Bienvenidos", "En este video", "Hoy vamos a hablar de". Entra directo al contenido mas fascinante. Y en el PRIMER MINUTO Y MEDIO del guion el espectador debe saber EXACTAMENTE lo que va a descubrir y por que merece la pena quedarse: dile explicitamente el payoff ("En los proximos minutos vas a descubrir quien... / que paso... / la verdad sobre..."). NUNCA dejes la promesa para despues del minuto 1.5: si el espectador no conoce el payoff en 90 segundos, abandona. La introduccion completa (impacto + promesa + gancho de retencion) no debe superar los 90 segundos.

3. NO INVENTES DATOS. Fechas, nombres, lugares y testimonios deben ser fieles a las fuentes. Si hay debate, menciona la incertidumbre. {substance}

4. PROGRESION NARRATIVA. Cada seccion debe aportar informacion NUEVA que haga avanzar la historia. Nada de repetir ideas con sinonimos. Si no tienes contenido nuevo, termina antes.

5. ESTRUCTURA CLARA. El guion debe tener: introduccion impactante, desarrollo con datos concretos, climax, y cierre reflexivo. Cada bloque debe ser autocontenido y visualizable.

6. CIERRE. El final debe incluir: \"{outro}\" como reflexion de cierre y resolver el arco de ESTE video. No anticipes, anuncies ni enlaces al proximo/siguiente video, episodio o entrega. PERO NO uses formulas genericas de YouTube como \"suscribete\", \"dale like\", \"activa la campanita\" ni similares.

7. LONGITUD. Apunta a {duration_target} minutos de video ({words_guide} palabras). Es una guia, no una regla rigida — prioriza calidad sobre cantidad.

Responde exclusivamente con JSON valido, sin markdown, sin explicaciones fuera del JSON."""


def _specialty_intro(config) -> str:
    """Derive the niche-specific intro sentence from channel config."""
    style = str(getattr(config, "CANAL_NARRATIVE_STYLE", "documental")).lower()

    if "medic" in style or "clinico" in style:
        return ("Eres un guionista de documentales medicos. Escribes guiones para video-ensayos de YouTube "
                "en espanol latinoamericano neutro. Tu especialidad: casos clinicos inexplicables, "
                "enfermedades raras y sindromes que desafian la ciencia.")
    if "supervivencia" in style or "expedicion" in style:
        return ("Eres un guionista de documentales. Escribes guiones para video-ensayos de YouTube "
                "en espanol latinoamericano neutro. Tu especialidad: expediciones tragicas, "
                "naufragios, desastres de montana y supervivencia extrema.")
    if "arqueologico" in style or "civilizacion" in style:
        return ("Eres un guionista de documentales historicos. Escribes guiones para video-ensayos de YouTube "
                "en espanol latinoamericano neutro. Tu especialidad: civilizaciones perdidas, "
                "ruinas antiguas y misterios arqueologicos.")
    return ("Eres un guionista de documentales. Escribes guiones para video-ensayos de YouTube "
            "en espanol latinoamericano neutro. Tu especialidad: fenomenos inexplicables "
            "e historias asombrosas.")


# ═══════════════════════════════════════════════════════════════════
# USER PROMPT — shared across all channels
# ═══════════════════════════════════════════════════════════════════

def format_user_prompt(content_item: dict, config=None) -> str:
    """Format the user prompt with content item fields.

    The category label adapts slightly to the channel's niche,
    but the structure is identical across all channels.
    """
    style = str(getattr(config, "CANAL_NARRATIVE_STYLE", "")).lower() if config else ""

    if "medic" in style or "clinico" in style:
        category_label = "Categoria del caso"
        transform_text = "Transforma el contenido anterior en un guion documental sobre este caso medico inexplicable."
    elif "supervivencia" in style or "expedicion" in style:
        category_label = "Categoria de la expedicion"
        transform_text = "Transforma el contenido anterior en un guion documental sobre esta expedicion tragica."
    elif "arqueologico" in style or "civilizacion" in style:
        category_label = "Categoria del hallazgo"
        transform_text = "Transforma el contenido anterior en un guion documental sobre este descubrimiento arqueologico."
    else:
        category_label = "Categoria del suceso"
        transform_text = "Transforma el contenido anterior en un guion documental sobre este suceso inexplicable real."

    template = f"""Titulo de la fuente: {{title}}
Origen: {{source}}
Subreddit: {{subreddit}}
Puntuacion/Relevancia: {{score}}
{category_label}: {{category}}

Contenido original:
---
{{text}}
---

{transform_text}
Si el contenido describe multiples sucesos, enfocate en el mas impactante y menciona brevemente los otros en la conclusion.
Genera UNICAMENTE el JSON de respuesta."""

    return template.format(
        title=content_item.get("title", "Sin titulo"),
        source=content_item.get("source", "desconocida"),
        subreddit=content_item.get("subreddit", "N/A"),
        score=content_item.get("score", 0),
        category=content_item.get("category", "general"),
        text=content_item.get("text", ""),
    )


# ═══════════════════════════════════════════════════════════════════
# MARATHON MODE — Outline for ~1h deep-dive videos
# ═══════════════════════════════════════════════════════════════════

def build_marathon_outline_prompt(config, duration_min: float = 60,
                                   num_sections: int = 12,
                                   narrative_format: str = "top_cases",
                                   word_target: int = 8500) -> str:
    """Generate a structured outline for a ~1h marathon documentary video.

    Supports three formats:
      - top_cases: Independent sections on different cases/stories
      - deep_story: Single epic story told over N chapters
      - historical_collapse: N civilizations/empires that collapsed
    """
    tone = getattr(config, "CANAL_TONE", "Documental, riguroso.")
    style = getattr(config, "CANAL_NARRATIVE_STYLE", "documental")
    audience = getattr(config, "TARGET_AUDIENCE", "publico LATAM adulto curioso")
    n_chapters = num_sections

    format_instructions = {
        "top_cases": (
            f"Genera EXACTAMENTE {n_chapters} secciones INDEPENDIENTES. "
            "Cada seccion cubre UN caso distinto con 3+ hechos concretos. "
            "Progresion: de lo mas ligero a lo mas impactante."
        ),
        "deep_story": (
            f"Genera EXACTAMENTE {n_chapters} capitulos SECUENCIALES que cuentan "
            "UNA SOLA historia epica de principio a fin. "
            "Progresion: introduccion -> desarrollo -> climax -> reflexion."
        ),
        "historical_collapse": (
            f"Genera EXACTAMENTE {n_chapters} perfiles de civilizaciones/imperios "
            "que colapsaron. Cada seccion cubre UNA civilizacion completa. "
            "Progresion: de las mas antiguas a las mas recientes."
        ),
        "tragic_expeditions": (
            f"Genera EXACTAMENTE {n_chapters} secciones sobre expediciones tragicas. "
            "Cada seccion cubre UNA expedicion: contexto, que salio mal, consecuencias. "
            "Progresion: de las mas remotas a las mas recientes."
        ),
    }

    fmt_text = format_instructions.get(
        narrative_format,
        format_instructions["top_cases"],
    )

    return f"""Eres el guionista jefe de una serie documental de alto presupuesto. Tu especialidad son los documentales largos de inmersion profunda.

TONO: {tone}
ESTILO: {style}
AUDIENCIA: {audience}
DURACION OBJETIVO: {duration_min} minutos (~{word_target} palabras)
FORMATO: {narrative_format}

Tu tarea es generar UN OUTLINE ESTRUCTURADO para un documental de {duration_min} minutos. NO escribas el guion — solo el outline.

{fmt_text}

REGLAS INQUEBRANTABLES:
1. CADA seccion DEBE tener AL MENOS 3 HECHOS CONCRETOS: numeros, fechas, nombres, lugares.
2. CADA seccion necesita keywords visuales en INGLES para stock media.
3. PROHIBIDO el lenguaje puramente metaforico sin sustancia.
4. Los hechos deben ser verificables.
5. words_approx por seccion: ~{word_target // n_chapters} palabras.

FORMATO DE SALIDA (JSON):
{{
  "summary": "Resumen de 2-3 frases del arco narrativo del documental de {duration_min} minutos",
  "chapters": [
    {{
      "chapter": 1,
      "titulo": "Titulo de la seccion en espanol (impactante, estilo titulo YouTube)",
      "idea_central": "Que revela esta seccion — una frase potente",
      "hechos_concretos": ["Hecho 1", "Hecho 2", "Hecho 3", "Hecho 4"],
      "visual_keywords_en": "english keywords for stock media search",
      "emocion_objetivo": "asombro|curiosidad|intriga|tension|revelacion|inspiracion|reflexion",
      "words_approx": {word_target // n_chapters}
    }}
  ]
}}

CRITICO: Solo hechos verificables. Solo datos. Solo historias reales. NADA de metaforas vacias."""


# Legacy for backwards compatibility — not used in new code but avoids import errors
SYSTEM_PROMPT = None  # Set at runtime per channel via build_system_prompt


# ═══════════════════════════════════════════════════════════════════
# VISUAL BIBLE PROMPT (Phase 3)
# ═══════════════════════════════════════════════════════════════════

def build_visual_bible_prompt(config, num_scenes: int = 0) -> str:
    """Build the system prompt that asks the LLM to generate a visual bible.

    The visual bible is a JSON blueprint that drives AI image generation
    for every scene, ensuring visual coherence across the entire video.

    Parameters
    ----------
    config:
        Channel config object (used to derive narrative style, tone, etc.).
    num_scenes:
        Approximate number of scenes.  Used to hint the expected array
        size (the LLM will output exactly one entry per scene in the
        script it receives).
    """
    style = str(getattr(config, "CANAL_NARRATIVE_STYLE", "documental"))
    tone = str(getattr(config, "CANAL_TONE", "documental riguroso"))
    style_desc = str(getattr(config, "CANAL_STYLE_DESCRIPTION", ""))

    shot_dist = getattr(config, "MEDIA_STRATEGY", {}).get("shot_type_distribution", {})
    establishing_pct = int(shot_dist.get("establishing", 0.15) * 100)
    detail_pct = int(shot_dist.get("detail", 0.25) * 100)
    mood_pct = int(shot_dist.get("mood", 0.30) * 100)
    action_pct = int(shot_dist.get("action", 0.20) * 100)
    symbolic_pct = int(shot_dist.get("symbolic", 0.10) * 100)

    scene_hint = ""
    if num_scenes > 0:
        scene_hint = f" El guion tiene aproximadamente {num_scenes} escenas. Genera EXACTAMENTE ese numero de entradas en scene_visual_map."

    return f"""Eres un director de fotografia y diseno visual experto en documentales de estilo {style}.

TONO DEL CANAL: {tone}
{f'ESTILO: {style_desc}' if style_desc else ''}

Tu tarea: analizar el guion completo de un video de YouTube y generar una
"biblia visual" en JSON que guiara la generacion de imagenes IA para cada
escena. La biblia visual asegura que todas las escenas compartan el mismo
universo visual, paleta de colores y atmosfera.

REGLAS:

    1. ESCENIFICACION CINEMATOGRAFICA OBSERVABLE (cinematic staging; not a literal illustration), NO ILUSTRACION LITERAL NI
       METAFORA DESCONECTADA. Cada concepto debe partir del fragmento narrado
       de su escena final y proponer una puesta en escena que una camara pueda
       registrar: sujeto, accion, objetos, espacio, composicion, luz y
       profundidad. Debe relacionarse con lo que se oye sin representar cada
       palabra ni inventar un simbolo ajeno. Si el guion dice "dinero" en
       Egipto antiguo, muestra intercambio de bienes, pesas y mercaderes en un
       mercado antiguo; no billetes, tarjetas ni una ilustracion literal de la
       palabra. Conserva siempre epoca, cultura y tecnologia disponibles.
       JAMAS muestres texto en la imagen.

2. ENTIDAD CENTRAL. Si el guion tiene un protagonista humano o una entidad
   central recurrente (persona, criatura, lugar emblematico, objeto magico),
   describela en central_entity con precision quirurgica:
   - type: "person", "place", "object", o "none"
   - master_description: descripcion ultra-detallada (edad, rasgos faciales,
     vestimenta, cicatrices, iluminacion tipica, angulo de camara habitual).
     Esta descripcion se inyectara en CADA escena donde aparezca.
   - appears_in_scenes: lista de indices de escena donde aparece.
   - variation_by_scene: para cada escena, variacion de encuadre
     (ej. "medium shot walking", "distant wide shot", "silhouette against fire").
   Si NO hay entidad central clara, usa type="none".
   REGLA SI type="person": encuadra SIEMPRE en plano medio o general (nunca
   primer plano del rostro, nunca ocupando el primer plano del encuadre).
   Usa siluetas, contraluces o figuras a distancia para transmitir presencia
   humana sin exponer detalles faciales.

3. ELEMENTOS RECURRENTES. Hasta 5 elementos visuales que aparecen en
   multiples escenas como hilo conductor (ej. "columnas de marmol agrietadas",
   "humo de incienso", "pergaminos antiguos").

    4. TIMESTAMPS Y ESCENAS FINALES. El texto recibido incluye los limites de
       tiempo derivados de timestamps TTS. Genera exactamente una entrada por
       subescena final, conserva su indice y usa su fragmento como ancla. No
       agrupes ni vuelvas a dividir esas subescenas.

    5. PUENTES VISUALES. Cada escena (excepto la primera) debe tener un
   bridge_from_prev: un elemento visual compartido con la escena anterior
   (misma luz, mismo objeto, misma textura, mismo color dominante).

    6. TIPOS DE TOMA. Distribuye los shot_type aproximadamente asi:
   establishing={establishing_pct}%, detail={detail_pct}%, mood={mood_pct}%,
   action={action_pct}%, symbolic={symbolic_pct}%.
   - establishing: gran angular, situa al espectador.
   - detail: plano detalle de OBJETOS y texturas (intimidad con el objeto).
     NUNCA primer plano del rostro de una persona.
   - mood: atmosfera pura, luz, sombras, emocion sin accion.
   - action: movimiento, tension, dinamismo.
   - symbolic: simbolismo anclado en objetos y acciones reales; nunca una forma
     abstracta sin contexto historico o cultural.

5b. PERSONAS A DISTANCIA. Si una escena incluye personas (protagonista o
    figurantes), muestralas en plano medio o general: nunca primer plano del
    rostro ni como sujeto ocupando el primer plano del encuadre. Prefiere
    siluetas, contraluces o figuras pequenas integradas en el paisaje/escenario.

6. DENSIDAD VISUAL. Ajusta visual_density segun la cantidad de narracion:
   - Mucha narracion (>3 palabras/seg) -> "simple" (fondos desenfocados,
     composicion limpia, pocos elementos).
   - Narracion media (2-3 palabras/seg) -> "balanced".
   - Poca narracion (<2 palabras/seg) -> "rich" (paisajes detallados,
     texturas ricas, profundidad de campo amplia).

7. ARCO EMOCIONAL. Define visual_tone_arc como una secuencia de estados
   emocionales separados por flechas (ej. "majestuoso->intimo->tenso->
   tragico->esperanzador").

8. FORMATO. Responde EXCLUSIVAMENTE con un JSON valido, sin markdown,
   sin explicaciones fuera del JSON. Usa este esquema:{scene_hint}

{{
  "visual_universe": "descripcion del universo visual completo (epoca, locacion, atmosfera, paleta dominante, texturas)",
  "visual_tone_arc": "emocion1->emocion2->emocion3->...",
  "central_entity": {{
    "type": "person|place|object|none",
    "master_description": "descripcion ultra-detallada del protagonista/entidad",
    "appears_in_scenes": [0, 3, 7],
    "variation_by_scene": {{
      "0": "medium shot, walking through colonnade, backlit",
     "3": "medium shot of hands writing, candlelight"
    }}
  }},
  "recurring_elements": ["elemento1", "elemento2", "..."],
  "scene_visual_map": [
    {{
      "scene": 0,
      "shot_type": "establishing|detail|mood|action|symbolic",
     "visual_concept": "descripcion visual de una accion/objeto/espacio observable",
      "mood": "emocion que transmite la imagen",
      "has_protagonist": true|false,
      "bridge_from_prev": null,
      "visual_density": "simple|balanced|rich"
    }}
  ]
}}

IMPORTANTE: visual_concept debe estar en INGLES (se usara como prompt para
generacion de imagenes IA). El resto del JSON puede estar en espanol."""
