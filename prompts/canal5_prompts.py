"""GPT prompt templates for Canal 5: Anomalias Medicas.
 
Marathon mode outline for ~1h deep-dive documentary videos about
unexplained medical cases, rare diseases, and syndromes that
challenge medical science.
 
For regular script generation, all four required functions are now
provided: build_system_prompt, format_user_prompt (for normal video
generation), build_content_only_prompt (for sequential block-by-block
generation), and build_marathon_outline_prompt (for marathon mode).
"""
 
from config import canal5_config as _default_config
 
 
# ═══════════════════════════════════════════════════════════════════
# REGULAR SCRIPT GENERATION — build_system_prompt + format_user_prompt
# ═══════════════════════════════════════════════════════════════════

def build_system_prompt(config=None, word_count_emphasis: float = 1.0,
                        chunk_context: dict = None, theme_context=None,
                        word_target: dict = None) -> str:
    """Build the system prompt for regular (non-marathon) video generation.

    Adapted from canal3_prompts pattern for the medical anomalies niche:
    precise clinical tone, data-driven, Spanish LATAM neutral.
    """
    cfg = config or _default_config

    # ── Core identity ────────────────────────────────────────
    tone = getattr(cfg, "CANAL_TONE",
                   "Preciso, clinico y profundamente humano. Riguroso en los hechos, "
                   "luminoso en la atmosfera, intimo en la narracion.")
    style = getattr(cfg, "CANAL_NARRATIVE_STYLE", "documental medico de asombro")
    style_desc = getattr(cfg, "CANAL_STYLE_DESCRIPTION",
                         "Combina rigor clinico con una narrativa humana y accesible.")
    audience = getattr(cfg, "TARGET_AUDIENCE",
                       "18-45 años LATAM, curiosos, interesados en medicina, "
                       "ciencia y misterios.")
    outro = getattr(cfg, "CANAL_OUTRO_TAGLINE",
                    "La medicina avanza cada dia. Pero este caso... "
                    "la ciencia todavia no tiene respuesta.")

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
            f"\nCONTEXTO VISUAL: genero={theme_context.genre}, "
            f"epoca={theme_context.era}, "
            f"estilo={theme_context.visual_style}. "
            f"Keywords: {', '.join(theme_context.theme_keywords_en[:5])}. "
            f"Prohibido: {', '.join(theme_context.forbidden_elements) if theme_context.forbidden_elements else 'ninguno'}.\n"
        )

    # ── Build the prompt ─────────────────────────────────────
    return f"""Eres un guionista de documentales medicos. Escribes guiones para video-ensayos de YouTube en español latinoamericano neutro. Tu especialidad: casos clinicos inexplicables, enfermedades raras, sindromes que desafian la ciencia y recuperaciones milagrosas.{mode_banner}{chunk_banner}
 
TONO: {tone}
ESTILO: {style} — {style_desc}
AUDIENCIA: {audience}
{theme_banner}
REGLAS ESENCIALES:
 
1. ESPAÑOL LATINOAMERICANO. Nada de vosotros, os, conjugaciones ibericas. Usa ustedes, tu o usted.
 
2. HOOK IMPACTANTE. La primera frase debe ser un dato clinico demoledor, un sintoma inexplicable o un caso que ningun medico pudo diagnosticar. NUNCA: "Hola", "Bienvenidos", "En este video", "Hoy hablaremos de". Entra directo al caso mas fascinante.
 
3. NO INVENTES DATOS MEDICOS. Sintomas, diagnosticos, valores de laboratorio y nombres de medicos/hospitales deben ser fieles a las fuentes. Si hay debate medico, menciona las distintas hipotesis. La rigurosidad clinica es fundamental.
 
4. PROGRESION NARRATIVA. Cada seccion debe aportar informacion NUEVA que haga avanzar el caso. Nada de repetir ideas con sinonimos. Si no tienes contenido nuevo, termina antes.
 
5. ESTRUCTURA CLARA. El guion debe tener: introduccion con el caso clinico impactante, desarrollo con datos medicos concretos, climax con la revelacion o el misterio sin resolver, y cierre reflexivo sobre lo que la medicina aprendio.
 
6. CIERRE. El final debe incluir: \"{outro}\" como reflexion de cierre, pero NO incluyas llamadas a la accion (suscribete, like, etc.) — eso se añade automaticamente.
 
7. LONGITUD. Apunta a {duration_target} minutos de video ({words_guide} palabras). Es una guia, no una regla rigida — prioriza calidad sobre cantidad.
 
Responde exclusivamente con JSON valido, sin markdown, sin explicaciones fuera del JSON."""


# Legacy constant for backwards compatibility
SYSTEM_PROMPT = build_system_prompt()


USER_PROMPT_TEMPLATE = """Titulo de la fuente: {title}
Origen: {source}
Subreddit: {subreddit}
Puntuacion/Relevancia: {score}
Categoria del caso: {category}
 
Contenido original:
---
{text}
---
 
Transforma el contenido anterior en un guion documental de video-ensayo sobre este caso medico inexplicable, siguiendo TODAS las reglas del sistema.
Si el contenido describe multiples casos o sindromes, enfocate en el mas impactante o enigmatico y menciona brevemente los otros en la conclusion.
 
Genera UNICAMENTE el JSON de respuesta."""


def format_user_prompt(content_item: dict) -> str:
    """Format the user prompt template with content item fields.

    Args:
        content_item: Dict with keys: title, source, subreddit, score, text, category.

    Returns:
        Formatted user prompt string.
    """
    return USER_PROMPT_TEMPLATE.format(
        title=content_item.get("title", "Sin titulo"),
        source=content_item.get("source", "desconocida"),
        subreddit=content_item.get("subreddit", "N/A"),
        score=content_item.get("score", 0),
        category=content_item.get("category", "anomalias medicas"),
        text=content_item.get("text", ""),
    )


def build_content_only_prompt(config=None, previous_blocks: list = None,
                               word_guidance: int = 300, source_text: str = None,
                               outline: dict = None, batch_num: int = 0) -> str:
    """Lightweight prompt for sequential block-by-block content generation.

    Strips ALL structural requirements so the LLM focuses exclusively on
    writing compelling narrative content about medical anomalies.

    When an outline is provided, includes the current chapter's context
    (title, central idea, concrete facts, visual keywords).
    """
    cfg = config or _default_config
    tone = getattr(cfg, "CANAL_TONE",
                   "Preciso, clinico y profundamente humano. Riguroso en los hechos, "
                   "luminoso en la atmosfera, intimo en la narracion.")
    style = getattr(cfg, "CANAL_NARRATIVE_STYLE", "documental medico de asombro")
    audience = getattr(cfg, "TARGET_AUDIENCE",
                       "18-45 anos LATAM, curiosos, interesados en medicina, "
                       "ciencia y misterios.")

    # ── Outline context injection ──────────────────────
    outline_context = ""
    if outline and outline.get("chapters"):
        chapters = outline["chapters"]
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
--- CONTEXTO DEL CAPITULO ---
Estas escribiendo el CAPITULO {chapter_idx + 1}/{len(chapters)}: "{chapter_title}"
IDEA CENTRAL: {chapter_idea}
EMOCION OBJETIVO: {chapter_emotion}
VISUAL KEYWORDS: {chapter_visual}
HECHOS CONCRETOS QUE DEBES INCLUIR:
{facts_text}
PROXIMO CAPITULO: {next_chapter}
 
⚠️ REGLAS DE CONTENIDO (¡OBLIGATORIO!):
- Incluye AL MENOS 2 de los hechos concretos listados arriba.
- NADA de metaforas vacias ni lenguaje poetico sin datos clinicos.
- Cada bloque debe CONTAR algo real, no "hablar por hablar".
- El video completo tiene {all_facts_id} hechos concretos documentados. No escribas parrafos sin sustancia clinica.
- Conecta este capitulo con el siguiente: "{next_chapter}"."""

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
            f"  Si ya mencionaste ciertos casos o fenomenos, NO los uses otra vez.\n"
            f"- Si has cubierto ya un aspecto del tema, explora otro angulo DISTINTO.\n"
            f"- Los bloques de cierre deben SINTETIZAR (no repetir) lo ya dicho.\n"
            f"- Manten el mismo tono y estilo que los bloques anteriores.\n"
        )

    source_context = ""
    if source_text:
        source_context = f"\nCONTENIDO FUENTE (usalo como base):\n{source_text[:2000]}\n"

    return f"""Eres un guionista documental para YouTube especializado en casos medicos inexplicables y anomalias clinicas. Escribe narraciones en español latinoamericano neutro.
 
TONO: {tone}
ESTILO: "{style}" — documentales que mezclan asombro clinico con rigor medico.
AUDIENCIA: {audience}
 
REGLAS ESTRICTAS:
1. Español latinoamericano neutro. PROHIBIDO "vosotros", "os", conjugaciones ibericas.
2. NO inventes datos clinicos. Usa SOLO la informacion de las fuentes proporcionadas.
3. Cada bloque debe ser un parrafo completo y sustancial (no frases sueltas).
4. Incluye detalles sensoriales, descripciones vividas y contexto clinico.
5. NO uses relleno ni repeticiones. Cada bloque aporta contenido GENUINAMENTE NUEVO.
6. CRITICO: Cada bloque debe CONTENER al menos un hecho concreto (diagnostico, sintoma, valor de laboratorio, nombre de medico/hospital, fecha). PROHIBIDO escribir parrafos puramente metaforicos sin datos.
7. ENGANCHE INICIAL: Los primeros bloques deben ser ALTAMENTE intrigantes. Plantea un misterio clinico, un dato impactante o una pregunta que el espectador NECESITE ver respondida. NUNCA empieces con frases como "En este video vamos a..." o "Hoy hablaremos de...". Entra directo al contenido mas fascinante.{source_context}{context_text}{outline_context}
 
Genera entre 2 y 4 bloques narrativos (~{word_guidance} palabras total).
Cada bloque SOLO necesita el campo "texto" (el parrafo que narrara el locutor).
 
Responde UNICAMENTE con JSON: {{"bloques": [{{"texto": "parrafo completo aqui..."}}, ...]}}
Sin explicaciones, sin markdown, sin texto fuera del JSON."""


# ═══════════════════════════════════════════════════════════════════
# MARATHON MODE — Outline for ~1h deep-dive documentary videos
# ═══════════════════════════════════════════════════════════════════

def build_marathon_outline_prompt(config=None, duration_min: float = 60,
                                   num_sections: int = 12,
                                   narrative_format: str = "top_cases",
                                   word_target: int = 8500) -> str:
    """Generate a structured outline for a ~1h marathon documentary video.

    For canal5 (Anomalias Medicas): unexplained medical cases, rare
    diseases, syndromes that challenge science, and miraculous recoveries.
    """
    cfg = config or _default_config
    tone = getattr(cfg, "CANAL_TONE",
                    "Preciso, clinico y profundamente humano. Riguroso en los hechos, "
                    "luminoso en la atmosfera, intimo en la narracion.")
    style = getattr(cfg, "CANAL_NARRATIVE_STYLE", "documental medico de asombro")
    audience = getattr(cfg, "TARGET_AUDIENCE",
                       "18-45 años LATAM, curiosos, interesados en medicina, "
                       "ciencia y misterios. 55% mujeres / 45% hombres.")
    outro = getattr(cfg, "CANAL_OUTRO_TAGLINE",
                     "La medicina avanza cada dia. Pero este caso... la ciencia "
                     "todavia no tiene respuesta.")
    n_chapters = num_sections

    return f"""Eres el guionista jefe de una serie documental de alto presupuesto. Tu especialidad son los documentales largos sobre los casos clinicos mas inexplicables de la historia de la medicina: enfermedades que ningun medico ha podido diagnosticar, sindromes tan raros que solo afectan a una persona en el mundo, y recuperaciones que desafian todo pronostico medico.

TONO: {tone}
ESTILO: {style}
AUDIENCIA: {audience}
DURACION OBJETIVO: {duration_min} minutos (~{word_target} palabras)
FORMATO: {narrative_format}

Tu tarea es generar UN OUTLINE ESTRUCTURADO para un documental de {duration_min} minutos sobre {n_chapters} casos clinicos inexplicables. NO escribas el guion — solo el outline.

CADA SECCION CUBRE UN CASO DISTINTO ({n_chapters} casos en total):
- Nombre del sindrome/enfermedad (si tiene nombre medico), o descripcion del caso.
- Paciente: edad, genero, pais, ano del caso (si esta documentado).
- Sintomas y datos clinicos: que experimentaba el paciente, valores de laboratorio, pruebas realizadas, diagnosticos descartados.
- Que lo hace inexplicable: por que la medicina no pudo explicarlo, que teorias existen, que medicos lo estudiaron.
- Desenlace: que paso con el paciente (recuperacion inexplicable, fallecimiento, estado actual).
- Legado medico: que aprendio la ciencia de este caso, si cambio algun protocolo o abrio una nueva linea de investigacion.

REGLAS INQUEBRANTABLES:
1. CADA seccion DEBE tener AL MENOS 3 HECHOS CONCRETOS: fechas, nombres de medicos/hospitales, valores de laboratorio, sintomas especificos, procedimientos documentados.
2. NO inventes datos medicos. Usa casos documentados en la literatura medica y verificables.
3. NO especules sobre causas sin mencionar que son teorias no confirmadas. Diferencia claramente entre hechos y especulacion medica.
4. Los casos deben ser reales y documentados. NO inventes pacientes ni sindromes.
5. PROHIBIDO el sensacionalismo barato. "Los medicos quedaron en shock" NO es contenido valido. Describe los hechos clinicos con precision.
6. Cada seccion: ~{word_target // n_chapters} palabras.
7. CADA seccion necesita keywords visuales en INGLES especificas para busqueda de stock media (hospital, microscope, DNA, genetics, rare disease, medical mystery).

FORMATO DE SALIDA (JSON):
{{{{
  "summary": "Resumen del arco narrativo: de los casos mas documentados a los mas enigmaticos, como la medicina avanza resolviendo algunos misterios mientras otros permanecen sin respuesta",
  "chapters": [
    {{{{
      "chapter": 1,
      "titulo": "Titulo impactante del caso en español (estilo titulo de YouTube medico)",
      "idea_central": "Que define a este caso — una frase que capture el asombro clinico",
      "hechos_concretos": [
        "Paciente, edad, ubicacion, ano, medico/hospital que lo trato",
        "Sintomas documentados, pruebas realizadas, diagnosticos descartados con datos clinicos concretos",
        "Por que es inexplicable: que teorias medicas existen, que contradice el caso",
        "Desenlace y legado medico concreto: que cambio en la practica clinica"
      ],
      "visual_keywords_en": "english keywords for stock media (hospital, rare disease, medical mystery, DNA sequencing)",
      "emocion_objetivo": "intriga|asombro|tension|revelacion|empatia|reflexion",
      "words_approx": {word_target // n_chapters}
    }}}}
  ]
}}}}

⚠️ CRITICO: El documental dura {duration_min} MINUTOS. Cada caso merece ~5 minutos de narracion densa, clinica y profundamente humana. El espectador debe sentir fascinacion por los misterios del cuerpo humano y empatia por los pacientes que viven estas realidades.

RECUERDA: Solo hechos medicos verificables. Solo casos documentados. Solo historias clinicas reales. El cuerpo humano es el mayor misterio que la ciencia aun intenta comprender.

OUTRO RECURRENTE: "{outro}" """
