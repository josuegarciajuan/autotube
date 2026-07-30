from config.settings import DEFAULT_VIDEO_PROVIDERS, DEFAULT_VIDEO_FALLBACK_QUERIES

"""Configuration for Canal 5: Anomalias Medicas.

Meta-niche: "casos clinicos reales que la ciencia no puede explicar
— enfermedades raras, sindromes inexplicables, anomalias medicas
documentadas y recuperaciones que desafian toda logica medica."

Formato: video-essay documental medico, 8-14 min, narrado con imagenes
cinematograficas de ambiente clinico y cientifico.

Estilo: "documental medico de asombro" — archivos de lo que la medicina
aun no comprende.
"""

# ═══════════════════════════════════════════════════════════════════
# IDENTITY
# ═══════════════════════════════════════════════════════════════════

CANAL_NAME = "canal5"
CANAL_DISPLAY_NAME = "Anomalias Medicas"
CANAL_TAGLINE = (
    "Casos clinicos reales que la ciencia aun no puede explicar"
)
CANAL_OUTRO_TAGLINE = (
    "La medicina avanza cada dia. Pero este caso... la ciencia "
    "todavia no tiene respuesta."
)

# ── YouTube Handle ───────────────────────────────────────────
YOUTUBE_HANDLE = "@AnomaliasMedicas"
YOUTUBE_CHANNEL_URL = "https://www.youtube.com/channel/UCDZi5NrlYnncYVlnZ0O7wKA"

# ── Narrative Style ─────────────────────────────────────────────
CANAL_NARRATIVE_STYLE = "documental medico de asombro"
CANAL_STYLE_DESCRIPTION = (
    "Casos medicos reales que desafian la ciencia. Enfermedades que "
    "ningun medico ha podido explicar, sindromes tan raros que solo "
    "afectan a una persona en el mundo, recuperaciones que contradicen "
    "todo pronostico. El formato sin rostro permite que las imagenes "
    "clinicas, los diagramas y los testimonios hablen solos."
)

# ── Channel About Section (indexado por YouTube search) ─────────
CHANNEL_ABOUT_SECTION = """Bienvenido a Anomalias Medicas.

Documentales sobre los casos clinicos mas inexplicables de la historia: enfermedades que ningun medico ha podido diagnosticar, sindromes tan raros que solo afectan a una persona en el mundo, y recuperaciones que desafian todo pronostico medico. Historias reales de pacientes que vivieron lo imposible.

🎬 Formato: video ensayos documentales (8-14 minutos)
🗓️ Nuevos casos: cada semana
🎙️ Narracion documental con fuentes medicas verificadas

📩 Contacto: {email}

🧬 Si te fascinan las enfermedades raras, los misterios del cuerpo humano, los casos medicos inexplicables y las historias que desafian a la ciencia... este canal es para ti.

Suscribete y activa la campana para no perderte ningun caso que la medicina no puede explicar."""

# ── Channel Keywords (YouTube Studio → Settings → Channel) ──────
CHANNEL_KEYWORDS = [
    "enfermedades raras",
    "casos medicos inexplicables",
    "anomalias medicas",
    "sindromes extraños",
    "documental medico",
    "casos clinicos reales",
    "enfermedades misteriosas",
    "fenomenos medicos",
    "ciencia medica",
    "casos medicos sorprendentes",
    "historias medicas reales",
    "misterios del cuerpo humano",
    "enfermedades raras documental",
    "sindromes inexplicables",
    "casos medicos impactantes",
    "documental salud español",
    "medicina y misterio",
    "anomalias del cuerpo humano",
    "trastornos raros",
    "historias clinicas reales",
]

# ═══════════════════════════════════════════════════════════════════
# TEST MODE
# ═══════════════════════════════════════════════════════════════════

TEST_MODE = False

TEST_SCRIPT_WORDS_MIN = 450
TEST_SCRIPT_WORDS_MAX = 700
TEST_SCRIPT_SCENES_MIN = 10
TEST_SCRIPT_SCENES_MAX = 12
TEST_SCRIPT_BLOCKS_MIN = 3
TEST_SCRIPT_BLOCKS_MAX = 5
TEST_VIDEO_DURATION_TARGET = 3.5

QUICK_TEST_SCRIPT_WORDS_MIN = 80
QUICK_TEST_SCRIPT_WORDS_MAX = 120
QUICK_TEST_SCRIPT_SCENES_MIN = 3
QUICK_TEST_SCRIPT_SCENES_MAX = 4
QUICK_TEST_SCRIPT_BLOCKS_MIN = 2
QUICK_TEST_SCRIPT_BLOCKS_MAX = 3
QUICK_TEST_VIDEO_DURATION_TARGET = 0.5
QUICK_TEST_IMAGES_PER_SCENE = 3

PROD_SCRIPT_WORDS_MIN = 900
PROD_SCRIPT_WORDS_MAX = 1200
PROD_SCRIPT_SCENES_MIN = 6
PROD_SCRIPT_SCENES_MAX = 12
PROD_SCRIPT_BLOCKS_MIN = 6
PROD_SCRIPT_BLOCKS_MAX = 12
PROD_VIDEO_DURATION_MIN = 6
PROD_VIDEO_DURATION_MAX = 10

# ── Average video duration target (in minutes) ──
# These are the single source of truth for production — read via the
# panel "Duración — Objetivo" and used by _get_word_target().
VIDEO_AVERAGE_DURATION_MIN = 8
VIDEO_DURATION_DISCREPANCY_MIN = 2

# ═══════════════════════════════════════════════════════════════════
# NARRATIVE TONE
# ═══════════════════════════════════════════════════════════════════

CANAL_TONE = (
    "Preciso, clinico y profundamente humano. Narrativa documental que "
    "oscila entre el asombro cientifico y la empatia medica. Riguroso "
    "en los hechos, luminoso en la atmosfera, intimo en la narracion. "
    "Como un documental de National Geographic sobre misterios medicos. "
    "El espectador debe sentir fascinacion por el cuerpo humano y "
    "respeto por los pacientes que viven estos casos."
)

# ═══════════════════════════════════════════════════════════════════
# TARGET AUDIENCE
# ═══════════════════════════════════════════════════════════════════

TARGET_AUDIENCE = (
    "18-45 años (amplio), LATAM (MX 30%, CO 20%, AR 15%, PE 10%, ES 15%, "
    "otros 10%). Curiosos, interesados en medicina, ciencia, cuerpo "
    "humano y misterios. 55% mujeres / 45% hombres. "
    "60%+ mobile. Sesiones de 8-12 min. Pico de consumo: 20:00-00:00 local."
)

TARGET_AUDIENCE_PSYCHOGRAPHIC = {
    "The Medical Curious": (
        "Fascinacion por el cuerpo humano y sus misterios. "
        "Busca entender lo inexplicable desde la ciencia."
    ),
    "The Empathetic": (
        "Conecta con las historias humanas detras de cada caso. "
        "Las enfermedades raras le recuerdan la fragilidad y fuerza humanas."
    ),
    "The Science Skeptic": (
        "Entra con dudas, se queda por el rigor de los hechos. "
        "Comparte para debatir: 'esto no puede ser cierto... pero lo es.'"
    ),
    "The Student / Professional": (
        "Estudiantes de medicina, enfermeria, psicologia. "
        "Consumen contenido como complemento a su formacion."
    ),
}

# ═══════════════════════════════════════════════════════════════════
# TITLE OPTIMIZATION
# ═══════════════════════════════════════════════════════════════════

TITLE_FORMULAS = [
    # Pattern 1: Named Condition + Shocking Detail
    "El Sindrome de {name}: {shocking_fact}",
    # Pattern 2: One in a Million
    "La Unica Persona en el Mundo con {condition}",
    # Pattern 3: The Case That Baffled Doctors
    "El Caso de {name}: {number} Medicos y Ningun Diagnostico",
    # Pattern 4: Medical Miracle
    "Le Dieron {number} Dias de Vida. {years} Años Despues Sigue Vivo.",
    # Pattern 5: What Science Can't Explain
    "{condition}: La Enfermedad que la Ciencia No Puede Explicar",
    # Pattern 6: Impossible Recovery
    "Desperto del Coma Hablando un Idioma que Nunca Aprendio",
    # Pattern 7: Body Superpowers
    "{number} Personas con Habilidades que la Ciencia No Entiende",
]

TITLE_POWER_WORDS = [
    # ⚡ URGENCIA / EXCLUSIVIDAD
    "revelado", "filtrado", "censurado", "inédito", "clasificado",
    "confidencial", "prohibido", "archivado",
    # 💥 IMPACTO EMOCIONAL
    "escalofriante", "desgarrador", "inexplicable", "demoledor",
    "sobrecogedor", "estremecedor", "alucinante", "aterrador",
    "asombroso", "desconcertante", "fascinante",
    # 🔍 CURIOSIDAD / MISTERIO
    "oculto", "secreto", "perturbador", "siniestro", "enigmático",
    "impactante", "increíble", "insólito", "misterioso", "único",
    # Medical / Clinical (canal5 specific)
    "síndrome", "enfermedad", "diagnóstico", "caso", "paciente",
    "curación", "tratamiento", "pronóstico",
    # Authority / Verification
    "documentado", "real", "verificado", "confirmado", "demostrado",
    # Scale / Numbers (canal5 specific)
    "rara", "única", "primera", "última", "ningún",
    # Survival / Hope (canal5 specific)
    "sobrevivió", "venció", "superó", "desafió", "contradijo",
    "imposible", "increíble",
]

TITLE_MAX_CHARS = 65

# ═══════════════════════════════════════════════════════════════════
# SCRIPT STRUCTURE — "Diagnostico del Asombro" method
# ═══════════════════════════════════════════════════════════════════

SCRIPT_HOOK_RULE = (
    "ATENCION: La primera frase del guion DEBE ser el hecho mas "
    "impactante del caso, con un NUMERO y un HECHO CONCRETO. "
    "NUNCA empezar con contexto historico, definiciones, ni presentaciones. "
    "NUNCA 'Hola, bienvenidos a...' ni 'En este video vamos a hablar de...'.\n\n"
    "EJEMPLO CORRECTO: 'En 2009, una mujer de 28 años entro en el hospital "
    "con un dolor de cabeza. 72 horas despues, 14 medicos de 3 paises "
    "seguian sin saber que tenia.'\n"
    "EJEMPLO INCORRECTO: 'Las enfermedades raras afectan a millones de "
    "personas en todo el mundo y son un desafio para la medicina moderna...'.\n\n"
    "RETENCION: Justo DESPUES del primer minuto del guion, DEBES incluir una "
    "frase de retencion explicita que invite al espectador a quedarse hasta "
    "el final del video. Debe ser CONTEXTUAL y DISTINTA en cada guion. "
    "Ejemplos orientativos (NO los copies, el LLM debe generar uno nuevo): "
    "'Si quieres saber que paso despues, quedate hasta el final', "
    "'Si quieres conocer como termina esta historia, no te vayas', "
    "'Quedate, porque lo que viene a continuacion es aun mas increible'. "
    "Esta frase debe colocarse inmediatamente despues de enumerar lo que el "
    "espectador va a descubrir, como cierre de la introduccion."
)

# 7-step structure with retention anchors
SCRIPT_STRUCTURE = [
    {
        "step": "EL SINTOMA",
        "time_pct": "0-10%",
        "description": (
            "El hecho mas impactante en frio. Sin contexto. La imagen mas "
            "evocadora del caso (radiografia, paciente, hospital). "
            "Cerrar con promesa: 'Al final de este video vas a entender "
            "por que la medicina sigue sin poder explicarlo.'"
        ),
    },
    {
        "step": "EL HISTORIAL",
        "time_pct": "10-20%",
        "description": (
            "Lo que se sabia antes de este caso. El conocimiento medico "
            "de la epoca. Construir anticipacion: 'En los libros de "
            "medicina, esto no existia. No habia precedentes.'"
        ),
    },
    {
        "step": "EL PACIENTE",
        "time_pct": "20-30%",
        "description": (
            "La persona real detras del caso clinico. Su vida antes "
            "del diagnostico. Gente normal, vidas normales, hasta que "
            "el cuerpo hizo algo imposible. Humanizar para que el "
            "espectador conecte."
        ),
        "retention_anchor": (
            "CLIFFHANGER al 25%: 'Pero lo que este paciente no sabia... "
            "es que su caso apareceria en revistas medicas de todo el mundo.'"
        ),
    },
    {
        "step": "EL DIAGNOSTICO",
        "time_pct": "30-55%",
        "description": (
            "Los sintomas, las pruebas, los medicos desconcertados. "
            "Escalar la intriga. 'Tres hospitales. Doce especialistas. "
            "Y nadie podia decir que estaba pasando.'"
        ),
        "retention_anchor": (
            "CLIFFHANGER al 50%: Silencio 2s. Cambio de imagen. "
            "'Recapitulemos: [1 frase]. Ahora viene lo mas increible.'"
        ),
    },
    {
        "step": "LA REVELACION",
        "time_pct": "55-70%",
        "description": (
            "El momento clave. El hallazgo que cambio todo. Peak de "
            "asombro cientifico. Musica crece. La pieza del puzzle "
            "que nadie esperaba encontrar."
        ),
    },
    {
        "step": "LAS CONSECUENCIAS",
        "time_pct": "70-85%",
        "description": (
            "Como cambio la vida del paciente. Que aprendio la medicina. "
            "Las publicaciones, los estudios, el legado. 'Hoy, este "
            "caso se estudia en facultades de medicina de todo el mundo.'"
        ),
        "retention_anchor": (
            "EL ESPEJO al 70%: Dirigirse directamente al viewer. "
            "'Ahora piensa en tu propio cuerpo. En ese dolor que "
            "ignoraste. En ese sintoma que nunca consultaste. "
            "El cuerpo humano guarda secretos que ni los medicos conocen.'"
        ),
    },
    {
        "step": "EL LEGADO",
        "time_pct": "85-100%",
        "description": (
            "Que nos ensena este caso. Lo que la medicina aprendio. "
            "La conexion con otros casos similares. Pregunta al viewer. "
            "End hook + CTA."
        ),
    },
]

SCRIPT_END_HOOK = (
    "Y si este caso te parecio increible, espera a ver el de {next_case}. "
    "Porque lo que le ocurrio a {next_patient} es todavia mas "
    "inexplicable. Ese es el proximo video. Dale like, suscribete y "
    "activa la campana."
)

SCRIPT_EMOTIONAL_ARC = {
    "0-10%": "shock",
    "10-20%": "curiosidad cientifica",
    "20-30%": "empatia",
    "30-45%": "intriga clinica",
    "45-55%": "anticipacion",
    "55-65%": "asombro",
    "65-75%": "admiracion → comprension",
    "75-85%": "reflexion",
    "85-95%": "inspiracion",
    "95-100%": "fascinacion",
}

# Retention anchors
RETENTION_ANCHORS = {
    "at_25_pct": {
        "trigger": "cliffhanger_mid_video",
        "action": (
            "Insertar mini-cliffhanger: 'Pero lo que los analisis "
            "revelaron 3 dias despues cambio todo.' Tratar el video "
            "como capitulos, no como ensayo."
        ),
    },
    "at_50_pct": {
        "trigger": "the_reset",
        "action": (
            "Musica fuera 2s. Nueva imagen o radiografia real. "
            "'Recapitulemos: [resumen 1 frase]. Ahora viene lo "
            "que ningun medico esperaba.'"
        ),
    },
    "at_70_pct": {
        "trigger": "the_mirror",
        "action": (
            "Dirigirse al viewer directamente. Hacerlo personal: "
            "'Tu cuerpo tambien esconde misterios que ni los "
            "medicos conocen.' Cerrar con teaser: 'En 60 segundos "
            "te cuento como termino todo.'"
        ),
    },
}

# ═══════════════════════════════════════════════════════════════════
# VIRALITY TRIGGERS
# ═══════════════════════════════════════════════════════════════════

VIRALITY_TRIGGERS = [
    {
        "name": "Medical Awe",
        "mechanism": (
            "'No vas a creer lo que el cuerpo humano puede hacer.' "
            "El asombro cientifico es altamente compartible. La gente "
            "comparte para provocar la misma reaccion en otros."
        ),
    },
    {
        "name": "One in a Million",
        "mechanism": (
            "La rareza extrema como gancho: 'Esta enfermedad solo "
            "la tienen 12 personas en el mundo.' La exclusividad "
            "genera curiosidad y FOMO."
        ),
    },
    {
        "name": "Conversation Starter",
        "mechanism": (
            "Cerrar con pregunta: '¿Conocias esta enfermedad?' o "
            "'¿Crees que la medicina algun dia entendera el cuerpo "
            "humano por completo?' Comentarios = engagement."
        ),
    },
    {
        "name": "Identity Signaling",
        "mechanism": (
            "Compartir este contenido dice: 'Yo se cosas que la "
            "mayoria no sabe sobre el cuerpo humano.' Conocimiento "
            "especial como moneda social."
        ),
    },
    {
        "name": "Hope Trigger",
        "mechanism": (
            "Historias de pacientes que vencieron pronosticos "
            "imposibles. 'Si esta persona pudo, cualquiera puede.' "
            "La esperanza medica es altamente compartible."
        ),
    },
]

# ═══════════════════════════════════════════════════════════════════
# VOICE (TTS)
# ═══════════════════════════════════════════════════════════════════

TTS_STRATEGY = {
    # ── Voice selection ──────────────────────────────────
    "voice_primary": "es-ES-AlvaroNeural",
    "voice_secondary": "es-MX-DaliaNeural",

    # ── Base (default) ───────────────────────────────────
    "rate_base": "-12%",         # moderate documentary pace, clinical precision
    "pitch_base": "+0Hz",        # natural pitch, authoritative

    # ── Hook (opening impact) ────────────────────────────
    "rate_hook": "-6%",          # measured pace, direct impact
    "pitch_hook": "-2Hz",        # slight gravity for authority

    # ── Desarrollo (body — neutral storytelling) ────────
    "rate_desarrollo": "-12%",
    "pitch_desarrollo": "+0Hz",

    # ── Climax (revelation of the diagnosis) ────────────
    "rate_climax": "-18%",       # slow — weight of revelation
    "pitch_climax": "-6Hz",      # gravity for importance

    # ── Reflexion (contemplative) ───────────────────────
    "rate_reflexion": "-14%",
    "pitch_reflexion": "-2Hz",

    # ── Cierre (closing call-to-action) ──────────────────
    "rate_cierre": "-8%",
    "pitch_cierre": "+2Hz",      # slightly warmer for CTA
}

VOICE_ID = TTS_STRATEGY["voice_primary"]
VOICE_SECONDARY = TTS_STRATEGY["voice_secondary"]
VOICE_RATE = TTS_STRATEGY["rate_base"]
VOICE_PITCH = TTS_STRATEGY["pitch_base"]
VOICE_VOLUME = "+0%"

VOICE_SSML = {
    "break_after_hook": '<break time="800ms"/>',
    "break_before_climax": '<break time="1200ms"/>',
    "emphasis_numbers": '<emphasis level="strong">',
    "emphasis_end": '</emphasis>',
    "prosody_rate_slow": '<prosody rate="slow" pitch="-2st">',
    "prosody_end": '</prosody>',
}

# ═══════════════════════════════════════════════════════════════════
# TTS ENGINE SELECTION
# ═══════════════════════════════════════════════════════════════════
# canal5 uses edge-tts (same as canal4, same Google account)

TTS_ENGINE = "edgetts"

# ── Kokoro configuration (activar con TTS_ENGINE = "kokoro") ──
KOKORO_VOICE = "em_santa"
KOKORO_BLOCK_SPEEDS = {
    "hook": 0.90,
    "desarrollo": 0.85,
    "climax": 0.78,
    "reflexion": 0.85,
    "cierre": 0.90,
}
KOKORO_PAUSE_BETWEEN_BLOCKS = 0.8

# Batch unload: reload Kokoro every N blocks to free RAM between batches.
# 0 = disabled.  Set to e.g. 10 for long scripts (>5000 words) to avoid
# memory exhaustion during TTS.  Overhead: ~5s per reload.
KOKORO_UNLOAD_EVERY_N_BLOCKS = 10

# ═══════════════════════════════════════════════════════════════════
# CONTENT SOURCES
# ═══════════════════════════════════════════════════════════════════

REDDIT_SUBREDDITS = [
    # Medical / clinical
    "Radiology",
    "medizzy",
    "MedicalMysteries",
    "medlabprofessionals",
    "ems",
    "nursing",
    # General viral / fascinating
    "Damnthatsinteresting",
    "interestingasfuck",
    "todayilearned",
    "UnresolvedMysteries",
    "TrueReddit",
    # Human stories
    "HumanPorn",
    "HumansAreMetal",
    # Science
    "science",
    "everythingScience",
]

REDDIT_SORT = "top"
REDDIT_TIME = "month"
REDDIT_LIMIT = 25

WIKIPEDIA_CATEGORIES = [
    # English — primary research
    "Rare diseases",
    "Medical anomalies",
    "Unexplained medical conditions",
    "Genetic disorders",
    "Syndromes",
    "Medical mysteries",
    "Neurological disorders",
    "Congenital disorders",
    "Autoimmune diseases",
    "List of syndromes",
    "Medical curiosities",
    "People with rare diseases",
    "Undiagnosed diseases",
    "Spontaneous remission",
    "Medical miracles",
    # Spanish Wikipedia
    "Enfermedades raras",
    "Sindromes",
    "Trastornos neurologicos",
    "Anomalias congenitas",
    "Casos medicos sin resolver",
]

# ═══════════════════════════════════════════════════════════════════
# SCRAPE SOURCES (multi-source plugin system)
# ═══════════════════════════════════════════════════════════════════

SCRAPE_SOURCES = [
    {"plugin": "reddit", "priority": 1},
    {"plugin": "wikipedia", "priority": 2},
    {"plugin": "atlas_obscura", "priority": 3},
    {"plugin": "rss", "priority": 4},
    {"plugin": "google_news", "priority": 5},
]

# Atlas Obscura categories for Anomalias Medicas
ATLAS_OBSCURA_CATEGORIES = ["medical", "unique"]

# RSS feeds for Anomalias Medicas
RSS_FEEDS = []

# Google News queries for Anomalias Medicas
GOOGLE_NEWS_QUERIES = [
    "enfermedad rara descubierta",
    "caso medico inexplicable",
    "sindrome extraño diagnosticado",
    "anomalia medica",
    "milagro medico",
]
GOOGLE_NEWS_LANGUAGE = "es"
GOOGLE_NEWS_COUNTRY = "ES"

# ═══════════════════════════════════════════════════════════════════
# VISUAL STYLE
# ═══════════════════════════════════════════════════════════════════

IMAGE_STYLE_MODIFIERS = (
    "medical documentary cinematography, clinical lighting, 16:9 aspect ratio, "
    "professional scientific photography, clean sterile aesthetic, "
    "cool teal and blue tones, medical equipment, hospital environment, "
    "anatomical precision, cellular imagery, laboratory setting"
)

COLOR_PALETTE = {
    "primary": (0, 95, 115),           # teal quirurgico — medical scrubs blue-green
    "secondary": (8, 20, 38),          # dark navy — cinematic night, clinical depth
    "accent": (220, 130, 40),          # amber medical alert — contrast for CTR
    "text": (240, 245, 250),           # clinical white — clean, readable
    "text_shadow": (4, 6, 12),         # deep navy shadow
    "tertiary": (30, 40, 50),          # dark steel — safe background
    "warning": (220, 130, 40),         # amber for thumbnail CTR accent
}

FILM_GRAIN_OPACITY = 4               # subtle — medical = clean aesthetic
FILM_GRAIN_FRAMES = 6
KEN_BURNS_ZOOM_MIN = 4
KEN_BURNS_ZOOM_MAX = 10

# ── Scene pacing ────────────────────────────────────────────────
# ─ Oct 2025: reduced MAX from 20 → 10 for more dynamic visual rotation (~10s scenes).
#   MIN lowered from 8 → 5 to avoid excessive TTS block merging with the tighter MAX.
SCENE_DURATION_MIN = 8.0
SCENE_DURATION_MAX = 16.0

# ── Render resolution ──────────────────────────────────────────
VIDEO_RESOLUTION = (1280, 720)

# ── Thumbnail style (per-channel coherence) ────────────────────
THUMBNAIL_VISUAL_STYLE = "clinical_mystery"
THUMBNAIL_STYLE_OVERRIDE = True

# Manual style config for clinical_mystery — viral medical documentary
THUMBNAIL_MANUAL_STYLE = {
    "visual_style": "clinical_mystery",
    "color_palette": {
        "primary": "#E63946",      # red: medical urgency / emergency
        "accent": "#00B4D8",       # cyan: medical technology / contrast
        "text": "#FFFFFF",
        "shadow": "#0A0A0F",
    },
    "base_composition": "dark_reveal",
    "effects": {
        "contrast_boost": 1.15,    # moderate — clinical but natural
        "saturation": 0.95,        # balanced — professional, not garish
        "vignette": 0.35,          # subtle focus
    },
    "text_style": {
        "uppercase": True,
        "max_words": 4,
    },
    "pollo_prompt_suffix": (
        "professional medical photography, human anatomy close-ups, DNA helix "
        "visualization, cellular structures under microscope, X-ray aesthetic, "
        "clinical lighting with soft shadows, heart monitor ECG waveforms, "
        "scientific documentary style, 16:9 aspect ratio, photorealistic, "
        "no text overlay, no gore, no explicit blood or open wounds, "
        "medical journal quality photography"
    ),
}

# ── Subtitle style ─────────────────────────────────────────────
SUBTITLE_FONT_SIZE = 52
SUBTITLE_SHADOW_WIDTH = 3
SUBTITLE_POSITION_X = 0.5
SUBTITLE_POSITION_Y = 0.88
SUBTITLE_POP_START = 0.95
SUBTITLE_POP_END = 1.05
SUBTITLE_MAX_CHARS = 50
SUBTITLE_PHRASE_GAP = 0.4

# ═══════════════════════════════════════════════════════════════════
# MEDIA STRATEGY (video + image hybrid with fallback chain)
# ═══════════════════════════════════════════════════════════════════

MEDIA_STRATEGY = {
    "media_per_block": 1,
    "prefer_video": True,
    "max_video_blocks_pct": 50,
    "target_video_pct": 50,
    "max_placeholder_pct": 0,
    "video_fallback_to_image": True,
    "video_min_duration": 4,
    "video_max_duration": 20,
    "video_sources": ["pexels"],
    "video_providers": DEFAULT_VIDEO_PROVIDERS,
    "video_fallback_queries": DEFAULT_VIDEO_FALLBACK_QUERIES,
    "fallback_query": "medical hospital laboratory clinical scientific 16:9",
    "fallback_query_simple": "medical science hospital laboratory clinical",
    "ken_burns_zoom_min": 4,
    "ken_burns_zoom_max": 10,
    "crossfade_min": 0.3,
    "crossfade_max": 0.7,
    # ── Pollo AI fallback ─────────────────────────────────────────
    "ai_image_fallback": True,           # enable Pollo AI when stock fails
    "ai_max_per_video": 5,               # hard cap: max 5 Pollo gen/video
}

SUBTITLES_ENABLED = False

# ── Inter-paragraph transitions ──────────────────────────────────
TRANSITION_ENABLED = True
TRANSITION_DURATION_MIN = 1.0
TRANSITION_DURATION_MAX = 5.0

# ── Background music ────────────────────────────────────────────
BACKGROUND_MUSIC_ENABLED = True
BACKGROUND_MUSIC_VOLUME = -18.0
BACKGROUND_MUSIC_DUCK_VOLUME = -28.0

# ── Intro / Outro ──────────────────────────────────────────────
INTRO_DURATION_SEC = 3.0
INTRO_FONT_SIZE = 68
INTRO_BG_COLOR = (8, 20, 38)        # dark navy
OUTRO_DURATION_SEC = 5.0
OUTRO_FONT_SIZE = 52
OUTRO_BG_COLOR = (8, 20, 38)
OUTRO_TEXT = "Suscribete"
OUTRO_CTA_LIKE = "👍 Like"
OUTRO_CTA_SUBSCRIBE = "❤️ Suscribete"
OUTRO_CTA_BELL = "📢 Comparte"
# ── CTA visual text ────────────────────────────────────────────
CTA_TEXT = (
    "Si has llegado hasta aqui y este caso te ha fascinado,\n"
    "suscribete y dale like\n"
    "para seguir revelando los misterios del cuerpo humano."
)
CTA_TEXT_VARIANTS = [
    (
        "Si has llegado hasta aqui y este caso te ha fascinado,\n"
        "suscribete y dale like\n"
        "para seguir revelando los misterios del cuerpo humano."
    ),
    (
        "Gracias por investigar este misterio medico con nosotros.\n"
        "Suscribete y dale like:\n"
        "el proximo caso es aun mas increible."
    ),
    (
        "El cuerpo humano guarda secretos que desafian a la ciencia.\n"
        "Suscribete, dale like y comparte\n"
        "para que juntos sigamos desvelando estas anomalias."
    ),
]
# ── Template voice-over texts ──────────────────────────────────
INTRO_VOICE_TEXT = "Bienvenidos a Anomalias Medicas, donde el cuerpo humano guarda secretos que la ciencia aun no comprende."
CTA_VOICE_TEXT = "Si has llegado hasta aqui y este caso te ha dejado sin palabras, suscribete y dale like. Sigamos investigando los misterios del cuerpo humano juntos."
OUTRO_VOICE_TEXT = "Gracias por acompanarnos. Hasta el proximo misterio medico."

CANAL_INITIALS = "AM"              # Anomalias Medicas
LOGO_SIZE = 180
LOGO_PATH = ""

# ═══════════════════════════════════════════════════════════════════
# YOUTUBE METADATA
# ═══════════════════════════════════════════════════════════════════

YT_CATEGORY_ID = "27"              # Education
YT_PRIVACY_STATUS = "public"

# Auto-mark videos as AI-generated content after upload (browser automation)
AUTO_MARK_ALTERED_CONTENT = True

# Auto-configure end screens (Subscribe + Video recommendation) after IA mark
AUTO_END_SCREENS = True

# ── Scheduled Publishing ──────────────────────────────────────────
PUBLISH_MODE = "scheduled"
PUBLISH_TIMEZONE = "Europe/Madrid"
PUBLISH_JITTER_MIN = 20
PUBLISH_WARMUP_MIN = 120
# PUBLISH_TARGET_HOUR not set — niche heuristic auto-detects (historia_documental → 20:00)

# ── 3-Phase Pipeline (v9) ─────────────────────────────────────────
GENERATION_LEAD_HOURS = 36    # Max hours ahead to generate (1.5 days)
# Upload windows (franjas de subida): videos suben en estas franjas a horas random
UPLOAD_WINDOWS = [
    {"start": 10, "end": 13},   # Mañana: 10:00-13:00
    {"start": 20, "end": 22},   # Tarde: 20:00-22:00
]
PUBLISH_WINDOW_SPREAD_MIN = 90     # ±90min alrededor del peak = ventana de publicación de 3h

YT_DEFAULT_TAGS = [
    # Tier 1: Primary keywords (broad match)
    "enfermedades raras",
    "anomalias medicas",
    "casos medicos inexplicables",
    "documental medico",
    "misterios del cuerpo humano",
    # Tier 2: Named conditions (high-intent search)
    "sindrome inexplicable",
    "enfermedad sin diagnostico",
    "caso clinico sorprendente",
    "anomalia genetica",
    "milagro medico real",
    # Tier 3: Format tags
    "video ensayo medicina",
    "documental español salud",
    "historias medicas reales",
    "medicina y misterio",
    "ciencia medica español",
    # Tier 4: Long-tail / adjacent
    "sindromes raros",
    "cuerpo humano misterios",
    "enfermedades extrañas documental",
    "casos clinicos historicos",
    "historias increibles de medicina",
]

# ═══════════════════════════════════════════════════════════════════
# SEO
# ═══════════════════════════════════════════════════════════════════

SEO_PRIMARY_KEYWORD = "enfermedades raras documental"

SEO_SECONDARY_KEYWORDS = [
    # Core niche
    "casos medicos inexplicables",
    "anomalias medicas reales",
    "sindromes extraños documental",
    "enfermedades misteriosas",
    "fenomenos medicos inexplicables",
    # Named conditions
    "sindrome de capgras",
    "sindrome del acento extranjero",
    "insensibilidad al dolor",
    "fibrodisplasia osificante",
    "sindrome de cotard",
    # Format / channel
    "documental medico español",
    "video ensayo medicina",
    "historias medicas reales",
    "misterios del cuerpo humano",
    # Audience intent
    "datos curiosos medicina",
    "enfermedades mas raras del mundo",
    "historias clinicas sorprendentes",
    "lo que la medicina no explica",
    "descubrimientos medicos impactantes",
]

SEO_HASHTAGS = [
    "#AnomaliasMedicas",
    "#EnfermedadesRaras",
    "#Medicina",
    "#CasosReales",
    "#Documental",
    "#Ciencia",
    "#Misterio",
    "#Salud",
    "#Curiosidades",
    "#CuerpoHumano",
    "#SabiasQue",
    "#HistoriasReales",
    "#Sindromes",
    "#MedicinaMisteriosa",
    "#CienciaMedica",
]

SHORTS_ENABLED = True
SHORTS_PER_DAY = 2
SHORTS_MAX_CLIPS_PER_VIDEO = 5
SHORTS_CLIP_SCHEDULE = [
    {"offset_days": 1, "count": 1},
    {"offset_days": 3, "count": 1},
    {"offset_days": 5, "count": 1},
]

SHORTS_HASHTAGS = [
    "#AnomaliasMedicas",
    "#EnfermedadesRaras",
    "#Shorts",
    "#SabiasQue",
    "#AprendeEnYouTube",
    "#Medicina",
    "#CuerpoHumano",
    "#Curiosidades",
    "#Documental",
    "#Ciencia",
]

# ── Cross-promotion ──────────────────────────────────────────
SHORTS_LONGFORM_LINK_ENABLED = True
SHORTS_PLAYLIST_AUTO = True
SHORTS_FIRST_COMMENT_LINK = True
SHORTS_PER_VIDEO_PLAYLIST = True
SHORTS_PLAYLIST_NAME = "Shorts"

# ═══════════════════════════════════════════════════════════════════
# DESCRIPTION TEMPLATE
# ═══════════════════════════════════════════════════════════════════

DESCRIPTION_TEMPLATE = """🧬 {titulo}
———

{descripcion_seo}

🔬 EN ESTE VIDEO
- La historia real detras de este caso medico inexplicable
- Quien era el paciente y como empezo todo
- Por que los medicos no podian encontrar un diagnostico
- Lo que la ciencia aprendio de este caso

⏱️ CAPITULOS
{chapters}

———

🎙️ Bienvenido a **Anomalias Medicas** — el canal donde documentamos los casos clinicos mas inexplicables de la historia: enfermedades que ningun medico ha podido diagnosticar, sindromes tan raros que solo afectan a una persona en el mundo, y recuperaciones que desafian todo pronostico. Historias reales de pacientes que vivieron lo imposible.

📚 Fuentes: Wikipedia, articulos cientificos, revistas medicas, hilos de Reddit (r/Radiology, r/medizzy, r/MedicalMysteries) y archivos clinicos verificados.

⚠️ Todo el contenido tiene fines educativos y de divulgacion cientifica.

🔔 Suscribete y activa la campana para descubrir mas misterios del cuerpo humano que la ciencia aun no puede explicar.

💬 ¿Conocias este caso? Dejalo en los comentarios.

#AnomaliasMedicas #EnfermedadesRaras #CasosReales"""

# ═══════════════════════════════════════════════════════════════════
# THUMBNAIL
# ═══════════════════════════════════════════════════════════════════

THUMBNAIL_WIDTH = 1280
THUMBNAIL_HEIGHT = 720
THUMBNAIL_FONT_SIZE = 56
THUMBNAIL_BORDER_WIDTH = 5

# ── Per-channel thumbnail customisation ────────────────
THUMBNAIL_FONT_FAMILY = "DejaVuSans-Bold"
THUMBNAIL_BORDER_COLOR = "#FF2D55"       # hot pink/red — viral medical alert
THUMBNAIL_SHOW_4K_BADGE = True
THUMBNAIL_TEXT_STROKE_WIDTH = 3
THUMBNAIL_TEXT_STROKE_COLOR = "#000000"

# ── Medical-themed overlays (clinical_mystery style) ──────────
THUMBNAIL_MEDICAL_ECG = True             # ECG heartbeat waveform across bottom
THUMBNAIL_MEDICAL_CROSS = False          # disabled — reduces visual clutter
THUMBNAIL_MEDICAL_DIAGNOSIS = False      # disabled — reduces visual clutter

# ── Per-channel concept directive ──
# Allow faces for emotional connection (doctor/patient expressions)
THUMBNAIL_ALLOW_FACES = True
THUMBNAIL_CONCEPT_DIRECTIVE = (
    "Canal medico-cientifico. El rostro humano PUEDE aparecer con expresion de "
    "preocupacion/asombro PROFESIONAL (medico mirando un diagnostico, paciente con "
    "expresion de alivio o intriga). Evitar expresiones exageradas tipo MrBeast. "
    "Priorizar imagenes clinicas y cientificas de alto impacto: primeros planos "
    "anatomicos, radiografias y resonancias magneticas con iluminacion dramatica, "
    "helices de ADN, "
    "estructuras celulares al microscopio, monitores cardiacos ECG, instrumental quirurgico, "
    "siluetas humanas contra luz clinica fria, manos sosteniendo diagnosticos, "
    "tubos de ensayo y laboratorios, X-rays y tomografias. Estilo documental medico "
    "de alto contraste, fotorealista, cinematografico. SIN caras, SIN sonrisas, SIN sangre visible. "
    "SE PERMITEN siluetas humanas contra luz clinica, figuras de espaldas o en "
    "penumbra, y manos sosteniendo objetos medicos para añadir humanidad sin recurrir "
    "a expresiones faciales."
)

THUMBNAIL_STYLE = {
    "layout": "image_full_background_text_overlay",
    "max_text_words": 4,
    "text_color": "clinical_white_on_teal_dark",
    "font_style": "bold_sans_serif_clean",
    "image_treatment": "clinical_cool_contrast_documentary",
    "background": "#081426",
    "accent_color": "#DC8228",
    "face_policy": (
        "Real faces (public domain or stock) YES — patient portraits "
        "with dignified expressions, medical professionals. "
        "Historical medical photographs in B&W. "
        "AI-generated faces: NO. Siluetas contra luz clinica, "
        "radiografias, manos sosteniendo diagnosticos OK."
    ),
    "number_preference": "odd_numbers_for_lists",
    "medical_viral_rule": (
        "Red/cyan medical alert accents on key elements boost CTR dramatically. "
        "Contrast emergency reds with bright medical cyan for viral impact. "
        "Use red accents for urgency, cyan for technology/science contrast. "
        "NO gore, NO explicit procedures — medical drama without horror. "
        "Medical = high contrast, dramatic, viral documentary aesthetic."
    ),
}

THUMBNAIL_TEMPLATES = {
    "the_scan": {
        "description": "Radiografia o resonancia magnetica, contraste azul frio, detalle clinico",
        "text_position": "bottom_third",
        "text_words": "2-3",
        "accent": "amber_highlight_on_anomaly",
        "best_for": "Casos con evidencia radiologica (tumores raros, anomalias oseas)",
    },
    "the_patient": {
        "description": "Retrato o silueta del paciente, iluminacion clinica, expresion esperanzada",
        "text_position": "bottom_over_dark_gradient",
        "text_words": "3-4",
        "accent": "amber_light_on_face_or_eyes",
        "best_for": "Historias de supervivientes y milagros medicos",
    },
    "the_lab": {
        "description": "Microscopio, tubos de ensayo, entorno de laboratorio, iluminacion fria",
        "text_position": "center_or_bottom",
        "text_words": "2-3",
        "accent": "amber_accent_on_key_equipment",
        "best_for": "Casos sobre investigacion medica y descubrimientos",
    },
}

# ═══════════════════════════════════════════════════════════════════
# VIDEO TIMING & MONETIZATION
# ═══════════════════════════════════════════════════════════════════

VIDEO_MIDROLL_STRATEGY = (
    "Colocar mid-rolls en pausas naturales entre capitulos narrativos. "
    "NUNCA en medio de una frase ni durante la revelacion del diagnostico. "
    "Cada mid-roll debe preceder un mini-gancho que mantenga al espectador."
)

MONETIZATION_TARGET_CPM = "$8–$15 USD"

MONETIZATION_VERTICALS = [
    "Salud y bienestar",
    "Farmaceutica y medicina",
    "Seguros de salud",
    "Educacion online",
    "Libros / Audiolibros",
    "Tecnologia medica",
]

# ═══════════════════════════════════════════════════════════════════
# END SCREEN
# ═══════════════════════════════════════════════════════════════════

END_SCREEN_STRATEGY = {
    "left_card": {
        "type": "playlist",
        "content": "most_relevant_playlist",
        "purpose": "Keep viewer in a medical mystery session — thematic rabbit hole",
    },
    "center": {
        "type": "subscribe",
        "purpose": "Convert viewer to subscriber",
    },
    "right_card": {
        "type": "video",
        "content": "most_recent_upload",
        "purpose": "Push newest content to engaged viewers",
    },
    "spoken_cta": (
        "Si este caso te parecio increible, el siguiente en la lista "
        "es todavia mas inexplicable. Te dejo el enlace en pantalla. "
        "Suscribete si quieres descubrir mas misterios del cuerpo humano."
    ),
}

# ═══════════════════════════════════════════════════════════════════
# PLAYLISTS
# ═══════════════════════════════════════════════════════════════════

PLAYLISTS = [
    {
        "slug": "casos-completos",
        "name": "Casos Completos",
        "description": (
            "Documentales en profundidad sobre los casos medicos mas "
            "inexplicables de la historia. Cada video: sintomas, "
            "diagnostico, pacientes y consecuencias."
        ),
        "type": "main",
    },
    {
        "slug": "lo-mas-increible",
        "name": "Lo Mas Increible",
        "description": (
            "Los 5 casos medicos mas asombrosos del canal. Si eres "
            "nuevo aqui, empieza por esta lista. Bienvenido a "
            "Anomalias Medicas."
        ),
        "type": "onboarding",
    },
    {
        "slug": "enfermedades-raras",
        "name": "Enfermedades Raras",
        "description": (
            "Patologias que afectan a 1 de cada millon de personas. "
            "Documentales sobre enfermedades que la mayoria de los "
            "medicos nunca han visto."
        ),
        "type": "thematic",
    },
    {
        "slug": "sindromes-inexplicables",
        "name": "Sindromes Inexplicables",
        "description": (
            "Sindromes que la ciencia no ha podido explicar. Pacientes "
            "con sintomas que desafian los libros de medicina."
        ),
        "type": "thematic",
    },
    {
        "slug": "milagros-medicos",
        "name": "Milagros Medicos",
        "description": (
            "Recuperaciones que desafiaron todo pronostico. Pacientes "
            "que los medicos dieron por perdidos... y sobrevivieron."
        ),
        "type": "thematic",
    },
]

# ═══════════════════════════════════════════════════════════════════
# FIRST 48 HOURS STRATEGY
# ═══════════════════════════════════════════════════════════════════

FIRST_48H_STRATEGY = {
    "pre_upload_24h": [
        "Community Tab poll: '¿Conocias esta enfermedad o sindrome?'",
        "YouTube Story: imagen clinica evocadora + 'Mañana. 9PM MX. Este caso es real.'",
    ],
    "hour_0": [
        "Publish at 9PM Mexico City time",
        "First comment (immediate, pinned): pregunta sobre el caso para disparar debate",
    ],
    "hours_1_6": [
        "Reddit r/medizzy: TEXT post with compelling medical summary",
        "Facebook groups: Medicina, Curiosidades Cientificas, Salud",
    ],
    "hours_6_24": [
        "Reply to EVERY comment in first 24h",
        "Twitter/X thread: 5-7 tweets contando el caso, final tweet = YouTube link",
    ],
    "hours_24_48": [
        "Analyze CTR and retention in YouTube Studio",
        "If CTR < 5%: swap thumbnail variant",
    ],
}

# ═══════════════════════════════════════════════════════════════════
# COMMUNITY TAB
# ═══════════════════════════════════════════════════════════════════

COMMUNITY_TAB_PLAN = {
    "frequency": "3x/week",
    "schedule": {
        "monday": {
            "type": "poll",
            "example": "¿Cual de estas enfermedades raras te parece mas increible?",
            "options": [
                "Insensibilidad al dolor",
                "Fibrodisplasia osificante",
                "Sindrome del acento extranjero",
                "Alergia al agua",
            ],
        },
        "wednesday": {
            "type": "image_fact",
            "example": (
                "Radiografia o imagen medica + 'Esta enfermedad solo "
                "la tienen 12 personas en el mundo. Y una de ellas "
                "vive en Latinoamerica.'"
            ),
        },
        "friday": {
            "type": "teaser",
            "example": (
                "Este sabado: el caso medico que dejo sin palabras a "
                "14 especialistas. Activa la campana."
            ),
        },
    },
}

# ═══════════════════════════════════════════════════════════════════
# CROSS-PLATFORM
# ═══════════════════════════════════════════════════════════════════

CROSS_PLATFORM = {
    "tiktok": {
        "format": "60-90s vertical cut-downs",
        "style": "Same visual assets, cropped 9:16, burned-in Spanish captions",
        "hook_template": (
            "Esta enfermedad solo la tienen {number} personas en el mundo. "
            "Y esta es su historia."
        ),
        "structure": "Hook → El caso (20s) → El dato medico increible (15s) → 'Video completo en YouTube, link en bio' (10s)",
        "cadence": "2x/day from one long-form video (days 1, 3, 5)",
    },
    "youtube_shorts": {
        "format": "15-30s most shocking medical moment",
        "end_cta": "'Video completo en el canal' linked to long-form",
        "purpose": "Shorts feed → channel page → long-form viewer conversion",
    },
    "twitter_x": {
        "format": "Thread — 1 case = 1 thread per week",
        "template": "'HOY en Anomalias Medicas: el caso de...' + 5-7 tweets + link",
    },
    "spotify_podcast": {
        "format": "Audio-only export of each video",
        "title_format": "Anomalias Medicas | {case_title} | {key_detail}",
        "purpose": "Minimal effort, massive discovery platform",
    },
}

# ═══════════════════════════════════════════════════════════════════
# COLLABORATION TARGETS
# ═══════════════════════════════════════════════════════════════════

COLLABORATION_TARGETS = {
    "tier_1_direct": [
        {"name": "La Hiperactina", "niche": "Divulgacion medica y cientifica, audiencia curiosa"},
        {"name": "Dr. Borja Bandera", "niche": "Medico divulgador, enorme audiencia salud"},
        {"name": "Medicina Clara", "niche": "Divulgacion medica, tono accesible"},
    ],
    "tier_2_adjacent": [
        {"name": "QuantumFracture", "niche": "Ciencia y fisica, audiencia curiosa crossover"},
        {"name": "CdeCiencia", "niche": "Divulgacion cientifica, enorme audiencia LATAM"},
    ],
    "collab_formats": [
        "React: 'Un medico reacciona a Anomalias Medicas'",
        "Topic trade: cubrir el caso sugerido por otro creador, cross-promote",
    ],
}

# ═══════════════════════════════════════════════════════════════════
# TRENDING TOPIC HOOKS
# ═══════════════════════════════════════════════════════════════════

TRENDING_TOPIC_HOOKS = {
    "type_a_news": {
        "trigger": "Noticia de enfermedad rara o diagnostico sorprendente",
        "pivot": (
            "Esto que acaba de pasar en {country}... la medicina tiene "
            "docenas de casos igual de inexplicables."
        ),
    },
    "type_b_anniversary": {
        "trigger": "Aniversario de descubrimientos medicos (search spikes)",
        "calendar": {
            "february": "Dia Mundial de las Enfermedades Raras (28 feb)",
            "april": "Dia Mundial de la Salud (7 abril)",
            "october": "Mes de la concienciacion sobre sindromes geneticos",
        },
    },
    "type_c_pop_culture": {
        "trigger": "Estreno de pelicula/serie sobre medicina o enfermedades raras",
        "strategy": "'La historia REAL detras de {show/movie}'",
        "examples": "Brain on Fire, The Cure, Awakenings → casos medicos reales",
    },
    "type_d_calendar": {
        "name": "Calendario de Misterios Medicos",
        "months": {
            "january": "Casos medicos de inicio de año / nuevos tratamientos",
            "february": "Enfermedades raras (Dia Mundial 28 feb)",
            "april": "Salud y bienestar (Dia Mundial de la Salud)",
            "october": "Sindromes geneticos y anomalias congenitas",
            "december": "Milagros medicos navideños",
        },
    },
}

# ═══════════════════════════════════════════════════════════════════
# CONTENT PILLARS
# ═══════════════════════════════════════════════════════════════════

CONTENT_PILLARS = [
    {
        "name": "El Caso",
        "ratio": 55,
        "desc": "Documental profundo de un caso medico inexplicable individual",
    },
    {
        "name": "Recopilaciones",
        "ratio": 25,
        "desc": "Compilacion tematica: '5 enfermedades mas raras del mundo'",
    },
    {
        "name": "El Analisis",
        "ratio": 20,
        "desc": "Video mas corto analizando el caso desde la perspectiva cientifica",
    },
]

# ═══════════════════════════════════════════════════════════════════
# VIRAL MIRROR
# ═══════════════════════════════════════════════════════════════════

# Enable viral mirror discovery for this channel
VIRAL_ENABLED = True

# English niche keywords for YouTube viral search
NICHE_KEYWORDS_ENG = [
    "medical anomalies",
    "rare medical cases",
    "mysterious diseases",
    "unexplained medical conditions",
    "medical mysteries",
    "rare diseases explained",
    "medical phenomena science can't explain",
    "bizarre medical conditions",
    "most shocking medical cases",
    "doctors couldn't explain this",
    "rarest diseases in the world",
    "medical cases that changed science",
    "patients who baffled doctors",
    "unexplained recoveries medical",
    "medical miracles true stories",
    "strangest syndromes",
    "rare genetic disorders",
    "weird medical conditions",
    "undiagnosed diseases documentary",
    "medical documentary rare cases",
]

# Override viral scoring thresholds for this channel (optional)
# VIRAL_MIN_VIEWS = 300000  # Lower threshold for smaller niche

VIRAL_PLAYLIST_KEYWORDS = {
    "casos-completos": [
        "mysterious medical conditions documentary",
        "patients who baffled every doctor",
        "undiagnosed diseases full documentary",
        "medical mystery diagnosis explained",
    ],
    "lo-mas-increible": [
        "most bizarre medical cases ever",
        "unbelievable medical anomalies",
        "top rarest diseases in the world",
        "shocking medical mysteries compilation",
    ],
    "enfermedades-raras": [
        "rarest genetic disorders documentary",
        "one in a million medical conditions",
        "rare disease patient stories",
        "medical oddities explained",
    ],
}
