"""GPT prompt templates for Canal 5: Anomalias Medicas.

Marathon mode outline for ~1h deep-dive documentary videos about
unexplained medical cases, rare diseases, and syndromes that
challenge medical science.

For regular script generation, canal5 currently falls back to
canal2_prompts via the dynamic import in script_generator.py.
"""

from config import canal5_config as _default_config


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
