"""Configuration for Canal 1: Psicología Oculta.

Meta-niche: "los experimentos psicológicos reales más perturbadores de la historia
que revelan verdades oscuras sobre la naturaleza humana"

Formato: video-essay documental, 8-14 min, narrado con imágenes cinematográficas.
Estilo: "archivo oscuro" — documentos clasificados de la psicología.
"""

# ═══════════════════════════════════════════════════════════════════
# IDENTITY
# ═══════════════════════════════════════════════════════════════════

CANAL_NAME = "canal1"
CANAL_DISPLAY_NAME = "Psicología Oculta"
CANAL_TAGLINE = (
    "Experimentos psicológicos reales que la ciencia ocultó... "
    "y lo que revelan sobre la naturaleza humana."
)
CANAL_OUTRO_TAGLINE = (
    "Esto no es ficción. Esto pasó. Y podría volver a pasar."
)

# ── Narrative Style ─────────────────────────────────────────────
CANAL_NARRATIVE_STYLE = "archivo oscuro"
CANAL_STYLE_DESCRIPTION = (
    "Documentos clasificados de la psicología que nadie te mostró. "
    "Archivos reales, experimentos reales, consecuencias reales. "
    "El formato sin rostro no es una limitación: es una feature. "
    "Estás viendo material de archivo, no un presentador."
)

# ── Channel About Section (indexado por YouTube search) ─────────
CHANNEL_ABOUT_SECTION = """Bienvenido a Psicología Oculta.

Analizamos los experimentos psicológicos más oscuros de la historia — Milgram, Stanford, MKUltra, y muchos más. Experimentos reales que la ciencia llevó al límite... y que revelan verdades inquietantes sobre la naturaleza humana.

🎬 Formato: video ensayos documentales (8-14 minutos)
🗓️ Nuevos análisis: cada semana
🎙️ Narración documental con fuentes académicas verificadas

📩 Contacto: {email}

🔬 Si te interesa la psicología social, la psicología conductual, los experimentos científicos reales y el lado oscuro de la mente humana... este canal es para ti.

Suscríbete y activa la campana para no perderte ningún experimento."""

# ── Channel Keywords (YouTube Studio → Settings → Channel) ──────
CHANNEL_KEYWORDS = [
    "psicología oscura",
    "experimentos psicológicos reales",
    "documentales de psicología",
    "Milgram",
    "Stanford prison experiment",
    "MKUltra",
    "psicología social",
    "experimentos perturbadores",
    "naturaleza humana",
    "historia de la psicología",
    "psicología conductual",
    "manipulación psicológica",
    "video ensayo",
    "psicología para estudiantes",
    "true crime psicológico",
    "experimentos científicos impactantes",
    "oscuridad de la mente",
    "control mental",
    "psicología explicada",
    "documental en español",
]

# ═══════════════════════════════════════════════════════════════════
# TEST MODE
# ═══════════════════════════════════════════════════════════════════

TEST_MODE = False

TEST_SCRIPT_WORDS_MIN = 200
TEST_SCRIPT_WORDS_MAX = 400
TEST_SCRIPT_SCENES_MIN = 10
TEST_SCRIPT_SCENES_MAX = 12
TEST_SCRIPT_BLOCKS_MIN = 3       # blocks for test mode
TEST_SCRIPT_BLOCKS_MAX = 5
TEST_VIDEO_DURATION_TARGET = 1

# ── Quick test mode (ultra-fast, ~30s video, ~5-8 min render) ──
QUICK_TEST_SCRIPT_WORDS_MIN = 80
QUICK_TEST_SCRIPT_WORDS_MAX = 120
QUICK_TEST_SCRIPT_SCENES_MIN = 3
QUICK_TEST_SCRIPT_SCENES_MAX = 4
QUICK_TEST_SCRIPT_BLOCKS_MIN = 2
QUICK_TEST_SCRIPT_BLOCKS_MAX = 3
QUICK_TEST_VIDEO_DURATION_TARGET = 0.5  # 30 sec target
QUICK_TEST_IMAGES_PER_SCENE = 3

PROD_SCRIPT_WORDS_MIN = 2000
PROD_SCRIPT_WORDS_MAX = 3500
PROD_SCRIPT_SCENES_MIN = 10
PROD_SCRIPT_SCENES_MAX = 18
PROD_SCRIPT_BLOCKS_MIN = 10      # blocks for production
PROD_SCRIPT_BLOCKS_MAX = 18
PROD_VIDEO_DURATION_MIN = 8
PROD_VIDEO_DURATION_MAX = 14

# ── Average video duration target (approx, in minutes) ──
# Used as reference; actual videos will vary roughly ±30% around this target.
# Example: 15 → videos typically range 11–19 minutes.
VIDEO_AVERAGE_DURATION_MIN = 15

# ═══════════════════════════════════════════════════════════════════
# NARRATIVE TONE
# ═══════════════════════════════════════════════════════════════════

CANAL_TONE = (
    "Grave, clínico y envolvente. Narrativa documental con autoridad. "
    "Oscilar entre lo clínico (procedimiento experimental) y lo profundamente "
    "humano (consecuencias y sufrimiento de los participantes). "
    "Más documental que sensacionalista. Riguroso en los hechos, oscuro en "
    "la atmósfera, humano en la narración."
)

# ═══════════════════════════════════════════════════════════════════
# TARGET AUDIENCE
# ═══════════════════════════════════════════════════════════════════

TARGET_AUDIENCE = (
    "18-34 años (core), LATAM (MX 35%, CO 20%, AR 15%, PE 10%, ES 10%, otros 10%). "
    "Educación universitaria, interés en psicología, true crime, filosofía, "
    "comportamiento humano, autoconocimiento. 60% hombres / 40% mujeres. "
    "65%+ mobile. Sesiones de 8-12 min. Pico de consumo: 21:00-01:00 local."
)

TARGET_AUDIENCE_PSYCHOGRAPHIC = {
    "The Armchair Psychologist": (
        "Consume contenido para entender por qué la gente hace cosas terribles. "
        "Se ve a sí mismo como más inteligente que el promedio."
    ),
    "The Self-Improver": (
        "Ve estos videos para entender su propia mente. "
        "Busca aplicabilidad personal en cada experimento."
    ),
    "The Dark Curious": (
        "Fascinación mórbida con los límites humanos. "
        "Comparte el contenido como 'mira lo que descubrí'."
    ),
    "The Student": (
        "Usa YouTube para complementar estudios de psicología. "
        "Valora el rigor académico y las fuentes citadas."
    ),
}

# ═══════════════════════════════════════════════════════════════════
# TITLE OPTIMIZATION
# ═══════════════════════════════════════════════════════════════════

TITLE_FORMULAS = [
    # Pattern 1: Named Experiment + Dark Hook (captura búsquedas por nombre)
    "El Experimento {name}: {shocking_fact}",
    # Pattern 2: Question + Revelation (target: búsquedas tipo pregunta)
    "¿{question}? La Psicología Tiene una Respuesta Oscura",
    # Pattern 3: This-Changed-Everything
    "El Experimento que Cambió la Psicología para Siempre (y Fue Prohibido)",
    # Pattern 4: The-Dark-Truth-About
    "La Oscura Verdad Sobre {topic} que Nadie te Contó",
    # Pattern 5: Explained + Why-It-Matters
    "{name} EXPLICADO: Por Qué Este Experimento Sigue Siendo Perturbador",
    # Pattern 6: Forbidden Knowledge
    "El Experimento que {authority} Intentó Ocultar Durante {years} Años",
    # Pattern 7: Numbers + Shock
    "{number} de Cada {total} Personas Harían {terrible_act}. La Ciencia lo Comprobó.",
]

TITLE_POWER_WORDS = [
    # Shock / Fear
    "perturbador", "oscuro", "aterrador", "escalofriante", "impactante",
    "estremecedor", "macabro", "siniestro", "brutal", "desgarrador",
    # Forbidden / Hidden
    "prohibido", "oculto", "secreto", "censurado", "clasificado",
    # Authority / Revelation
    "real", "demostró", "reveló", "confirmó", "comprobó",
    # Emotional
    "demoledor", "inquietante", "despiadado", "cruel", "inexplicable",
]

TITLE_MAX_CHARS = 65

# ═══════════════════════════════════════════════════════════════════
# SCRIPT STRUCTURE — "Espiral Oscura" method
# ═══════════════════════════════════════════════════════════════════

SCRIPT_HOOK_RULE = (
    "ATENCIÓN: La primera frase del guion DEBE ser un dato impactante "
    "del clímax del experimento, con un NÚMERO y un HECHO CONCRETO. "
    "NUNCA empezar con contexto histórico, definiciones, ni presentaciones. "
    "NUNCA 'Hola, bienvenidos a...' ni 'En este video vamos a hablar de...'.\n\n"
    "EJEMPLO CORRECTO: 'El 65% de las personas normales electrocutaría a un "
    "extraño hasta la muerte si alguien con bata blanca se lo ordena.'\n"
    "EJEMPLO INCORRECTO: 'El experimento de Milgram fue un estudio sobre la "
    "obediencia realizado en 1961 por Stanley Milgram...'"
)

# 7-step structure with cliffhanger positions
SCRIPT_STRUCTURE = [
    {
        "step": "EL IMPACTO",
        "time_pct": "0-10%",
        "description": (
            "Resultado impactante en frío. Sin contexto. Imagen: la más "
            "perturbadora del experimento. Cerrar con promesa: 'Al final de "
            "este video vas a entender por qué esto cambió la psicología.'"
        ),
    },
    {
        "step": "EL MUNDO ANTES",
        "time_pct": "10-20%",
        "description": (
            "Qué creía la sociedad antes del experimento. Construir tensión: "
            "'La respuesta parecía obvia... hasta que alguien la puso a prueba.'"
        ),
    },
    {
        "step": "EL EXPERIMENTADOR",
        "time_pct": "20-30%",
        "description": (
            "El psicólogo como personaje. Sus obsesiones, sus fallos, qué le "
            "llevó a diseñar el experimento. Humanizar al científico."
        ),
        "retention_anchor": (
            "CLIFFHANGER al 25%: 'Pero lo que este psicólogo no esperaba... "
            "es que su propio experimento se le fuera de las manos.'"
        ),
    },
    {
        "step": "EL EXPERIMENTO",
        "time_pct": "30-55%",
        "description": (
            "El procedimiento paso a paso. Escalar la intensidad. "
            "'Pero entonces pasó algo que nadie había anticipado...'"
        ),
        "retention_anchor": (
            "CLIFFHANGER al 50%: Silencio 2s. Cambio de imagen (fotos → "
            "documentos/diagramas). Voz secundaria si aplica. 'Recapitulemos: "
            "[1 frase]. Ahora viene lo peor.'"
        ),
    },
    {
        "step": "EL PUNTO DE QUIEBRE",
        "time_pct": "55-70%",
        "description": (
            "El momento exacto en que el experimento cruzó la línea. Peak de "
            "intensidad emocional. Música fuera. Silencio. Zoom lento."
        ),
    },
    {
        "step": "LAS CONSECUENCIAS",
        "time_pct": "70-85%",
        "description": (
            "Caída inmediata + impacto a largo plazo en psicología, ética, "
            "leyes. 'Los sujetos nunca volvieron a ser los mismos.'"
        ),
        "retention_anchor": (
            "EL ESPEJO al 70%: Dirigirse directamente al viewer. "
            "'Ahora piensa en tu trabajo. En tu jefe. En las órdenes que has "
            "seguido sin cuestionar.' Hacerlo personal."
        ),
    },
    {
        "step": "EL CIERRE",
        "time_pct": "85-100%",
        "description": (
            "Reflexión: qué revela sobre la naturaleza humana. Conexión con "
            "el presente. Pregunta al viewer. End hook + CTA."
        ),
    },
]

SCRIPT_END_HOOK = (
    "Y si crees que esto fue perturbador, espera a ver {next_experiment}. "
    "Porque ese experimento fue tan extremo que {shocking_teaser}. "
    "Ese es el próximo video. Suscríbete y activa la campana."
)

SCRIPT_EMOTIONAL_ARC = {
    "0-10%": "shock",
    "10-20%": "curiosidad",
    "20-30%": "intriga",
    "30-45%": "tensión",
    "45-55%": "incomodidad",
    "55-65%": "horror",
    "65-75%": "tristeza → indignación",
    "75-85%": "reflexión",
    "85-95%": "inquietud",
    "95-100%": "urgencia",
}

# Retention anchors (anti-drop-off)
RETENTION_ANCHORS = {
    "at_25_pct": {
        "trigger": "cliffhanger_mid_video",
        "action": (
            "Insertar mini-cliffhanger: 'Pero lo que descubrieron 3 días "
            "después cambió todo.' Tratar el video como capítulos, no como ensayo."
        ),
    },
    "at_50_pct": {
        "trigger": "the_reset",
        "action": (
            "Música fuera 2s. Nueva imagen o diagrama. Voz secundaria si hay "
            "cita directa. 'Recapitulemos: [resumen 1 frase]. Ahora viene lo peor.'"
        ),
    },
    "at_70_pct": {
        "trigger": "the_mirror",
        "action": (
            "Dirigirse al viewer directamente. Hacerlo personal: 'Ahora piensa "
            "en tu vida. ¿Cuántas órdenes has seguido sin cuestionar?' "
            "Cerrar con teaser: 'En 60 segundos te digo por qué esto es relevante HOY.'"
        ),
    },
}

# ═══════════════════════════════════════════════════════════════════
# VIRALITY TRIGGERS
# ═══════════════════════════════════════════════════════════════════

VIRALITY_TRIGGERS = [
    {
        "name": "Identity Signaling",
        "mechanism": (
            "Compartir este video dice: 'Yo sé cosas que los demás no.' "
            "Enmarcar el contenido como conocimiento prohibido."
        ),
    },
    {
        "name": "Moral Outrage",
        "mechanism": (
            "Cada guion debe identificar QUIÉN permitió que el experimento "
            "siguiera. Dar al viewer un villano. La indignación es la emoción "
            "que más se comparte."
        ),
    },
    {
        "name": "Conversation Starter",
        "mechanism": (
            "Cerrar con pregunta: '¿Crees que esto podría pasar hoy en tu país?' "
            "Comentarios = señal de engagement = más impresiones = más shares."
        ),
    },
    {
        "name": "Practical Utility",
        "mechanism": (
            "Incluir insight accionable: '3 señales de que alguien está usando "
            "manipulación psicológica contigo.' El contenido útil se guarda y comparte."
        ),
    },
    {
        "name": "Forbidden Fruit",
        "mechanism": (
            "'Esto fue real. La universidad lo permitió. El gobierno lo financió. "
            "Y casi nadie lo sabe.' La exclusividad percibida dispara el compartir."
        ),
    },
]

# ═══════════════════════════════════════════════════════════════════
# VOICE (TTS)
# ═══════════════════════════════════════════════════════════════════

# ═══════════════════════════════════════════════════════════════════
# TTS STRATEGY (segmented by block emotion)
# ═══════════════════════════════════════════════════════════════════
# Primary voice: es-ES-AlvaroNeural — Spanish from Spain, expressive,
# good tonal range, less monotonous than JorgeNeural.
# Secondary voice: es-MX-DaliaNeural — Mexican female, for quotes/contrast.
#
# Each block type gets its own rate/pitch applied via edge-tts
# <prosody> SSML during segmented synthesis.

TTS_STRATEGY = {
    # ── Voice selection ──────────────────────────────────
    "voice_primary": "es-ES-AlvaroNeural",
    "voice_secondary": "es-MX-DaliaNeural",

    # ── Base (default) ───────────────────────────────────
    "rate_base": "-3%",          # natural pace (AlvaroNeural is faster than Jorge)
    "pitch_base": "+0Hz",        # natural pitch
    "volume": "+0%",

    # ── Hook (opening impact) ────────────────────────────
    "rate_hook": "+2%",          # moderate urgency
    "pitch_hook": "-2Hz",        # slight gravity for authority

    # ── Desarrollo (body — neutral storytelling) ────────
    "rate_desarrollo": "-3%",    # natural pace
    "pitch_desarrollo": "+0Hz",

    # ── Climax (emotional peak) ─────────────────────────
    "rate_climax": "-8%",        # slower for tension
    "pitch_climax": "-8Hz",      # deeper gravity

    # ── Reflexion (contemplative) ───────────────────────
    "rate_reflexion": "-5%",     # contemplative pace
    "pitch_reflexion": "-2Hz",

    # ── Cierre (closing call-to-action) ──────────────────
    "rate_cierre": "+0%",        # normal pace
    "pitch_cierre": "+2Hz",      # slightly warmer
}

# Legacy voice config keys kept for backward compatibility
VOICE_ID = TTS_STRATEGY["voice_primary"]
VOICE_SECONDARY = TTS_STRATEGY["voice_secondary"]
VOICE_RATE = TTS_STRATEGY["rate_base"]
VOICE_PITCH = TTS_STRATEGY["pitch_base"]
VOICE_VOLUME = TTS_STRATEGY["volume"]

# SSML directives (applied to generated scripts)
VOICE_SSML = {
    "break_after_hook": '<break time="800ms"/>',
    "break_before_climax": '<break time="1200ms"/>',
    "emphasis_numbers": '<emphasis level="strong">',
    "emphasis_end": '</emphasis>',
    "prosody_rate_slow": '<prosody rate="slow" pitch="-2st">',
    "prosody_end": '</prosody>',
}

# ═══════════════════════════════════════════════════════════════════
# CONTENT SOURCES
# ═══════════════════════════════════════════════════════════════════

# Reddit — mix of Spanish-language and English psychology communities
REDDIT_SUBREDDITS = [
    # Spanish-language (priority — native content for LATAM audience)
    "psicologia",
    "HistoriasDeTerror",
    "preguntaleareddit",
    # English psychology / narrative (proven viral formats)
    "psychology",
    "TrueReddit",
    "todayilearned",
    "AskReddit",
    "UnresolvedMysteries",
    "Damnthatsinteresting",
    "creepy",
    "interestingasfuck",
    "NoSleep",         # narrative structure gold mine
    "medizzy",         # medical oddities — adjacent to psych experiments
    "explainlikeimfive",  # how to make complex psychology accessible
    "DarkPsychology",
    "science",
]

REDDIT_SORT = "top"
REDDIT_TIME = "month"
REDDIT_LIMIT = 25

# Wikipedia — English + Spanish categories
WIKIPEDIA_CATEGORIES = [
    # English — primary research
    "Unethical human experimentation",
    "Psychology experiments",
    "Social psychology",
    "Cognitive biases",
    "Human subject research",
    "History of psychology",
    "Psychological theories",
    "Abnormal psychology",
    "Mass hysteria",
    "Psychological warfare",
    "Sensory deprivation",
    "Cult psychology",
    "Psychiatry controversies",
    "History of psychiatry",
    "Brainwashing",
    "False memory",
    "Controversial psychologists",
    # Spanish Wikipedia — different content, different keywords
    "Experimentos psicológicos",
    "Tortura psicológica",
    "Lobotomía",
    "Control mental",
    "Historia de la psiquiatría",
]

# ═══════════════════════════════════════════════════════════════════
# VISUAL STYLE
# ═══════════════════════════════════════════════════════════════════

IMAGE_STYLE_MODIFIERS = (
    "cinematic photography, dramatic lighting, atmospheric, 16:9, "
    "professional photography, moody, high contrast"
)

COLOR_PALETTE = {
    "primary": (160, 22, 22),        # deep dried-blood crimson
    "secondary": (12, 10, 10),       # true warm-black
    "accent": (161, 117, 55),        # tarnished brass
    "text": (225, 220, 215),         # warm parchment off-white
    "text_shadow": (4, 3, 3),        # subtle warm shadow
    "tertiary": (45, 42, 38),        # dark warm gray — safe background
    "warning": (180, 50, 50),        # thumbnail CTR accent
}

FILM_GRAIN_OPACITY = 8
FILM_GRAIN_FRAMES = 12
KEN_BURNS_ZOOM_MIN = 3
KEN_BURNS_ZOOM_MAX = 8

# ── Thumbnail style (per-channel coherence) ────────────────────
# One of: dark_cinematic, vintage_archive, realistic_documentary,
#         institutional_cold, dramatic_contrast, moody_atmospheric,
#         minimalist_clean, or "auto" (LLM decides).
# All thumbnails in this channel share this visual identity.
THUMBNAIL_VISUAL_STYLE = "dark_cinematic"
THUMBNAIL_STYLE_OVERRIDE = True  # True = use THUMBNAIL_VISUAL_STYLE; False/None = auto-detect

# Manual style config — used when THUMBNAIL_STYLE_OVERRIDE is set
# or as fallback when the LLM style engine is unavailable.
THUMBNAIL_MANUAL_STYLE = {
    "visual_style": "dark_cinematic",
    "color_palette": {
        "primary": "#8B0000",
        "accent": "#DAA520",
        "text": "#F5F0E8",
        "shadow": "#0A0A0A",
    },
    "base_composition": "dark_reveal",
    "effects": {
        "contrast_boost": 1.3,
        "saturation": 0.85,
        "vignette": 0.45,
    },
    "text_style": {
        "uppercase": True,
        "max_words": 4,
    },
    "pollo_prompt_suffix": (
        "dark atmospheric cinematography, desaturated color palette, "
        "deep crimson and black tones, institutional cold lighting, "
        "film grain texture, documentary photography style, "
        "16:9 aspect ratio, high contrast, no text overlay, "
        "no human faces or if present blurred obscured, no gore"
    ),
}

# ── Subtitle style ─────────────────────────────────────────────
SUBTITLE_FONT_SIZE = 52
SUBTITLE_SHADOW_WIDTH = 3
SUBTITLE_POSITION_X = 0.5       # horizontal centre (0–1 relative)
SUBTITLE_POSITION_Y = 0.88      # vertical position (0–1 relative, bottom area)
SUBTITLE_POP_START = 0.95       # scale at phrase start
SUBTITLE_POP_END = 1.05         # scale at phrase end
SUBTITLE_MAX_CHARS = 50         # max chars per phrase before flush
SUBTITLE_PHRASE_GAP = 0.4       # seconds of silence to split phrases

# ═══════════════════════════════════════════════════════════════════
# MEDIA STRATEGY (video + image hybrid with fallback chain)
# ═══════════════════════════════════════════════════════════════════
# One media asset per block. Video preferred where available.
# Fallback chain: Pexels Video → Unsplash Image → Pexels Image
# → simplified query → generic fallback → placeholder.

MEDIA_STRATEGY = {
    # ── Fetching ─────────────────────────────────────────
    "media_per_block": 1,              # one asset per block (not 5)
    "prefer_video": True,              # try video before image
    "max_video_blocks_pct": 30,        # cap video % for render perf & quality matching
    "video_fallback_to_image": True,   # if no video found, use image

    # ── Video clip constraints ───────────────────────────
    "video_min_duration": 4,           # seconds minimum clip
    "video_max_duration": 20,          # seconds maximum clip
    "video_sources": ["pexels"],       # pexels, pixabay (free APIs)

    # ── Fallback queries ─────────────────────────────────
    "fallback_query": "dark cinematic atmosphere dramatic lighting 16:9",
    "fallback_query_simple": "dark moody cinematic",

    # ── Ken Burns (image fallback) ───────────────────────
    "ken_burns_zoom_min": 3,
    "ken_burns_zoom_max": 8,

    # ── Transitions ──────────────────────────────────────
    "crossfade_min": 0.3,
    "crossfade_max": 0.7,
}

# ── Subtitle toggle ────────────────────────────────────────────
SUBTITLES_ENABLED = False          # set to True to re-enable burned-in subtitles

# ── Scene composition (legacy — kept for fallback) ─────────────
SCENE_DURATION_MIN = 5.0
SCENE_DURATION_MAX = 8.0
CROSSFADE_MIN = MEDIA_STRATEGY["crossfade_min"]
CROSSFADE_MAX = MEDIA_STRATEGY["crossfade_max"]

# ── Image pacing (legacy — kept for video_editor fallback) ─────
PHRASES_PER_IMAGE = 2
IMAGES_PER_SCENE = 5
NO_REPEAT_IMAGES = True

# ── Intro / Outro ──────────────────────────────────────────────
INTRO_DURATION_SEC = 3.0
INTRO_FONT_SIZE = 68
INTRO_SUBTITLE_FONT_SIZE = 28
INTRO_SUBTITLE = "Documentales de Psicología"
INTRO_BG_COLOR = (5, 5, 5)      # near-black
OUTRO_DURATION_SEC = 6.0
OUTRO_FONT_SIZE = 52             # reduced to prevent clipping
OUTRO_BG_COLOR = (5, 5, 5)
OUTRO_TEXT = "Suscríbete"
OUTRO_CTA_LIKE = "👍 Like"
OUTRO_CTA_SUBSCRIBE = "🔔 Suscríbete"
OUTRO_CTA_BELL = "📢 Comparte"
CANAL_INITIALS = "PO"            # used for auto-generated logo badge (Psicología Oculta)
LOGO_SIZE = 180                  # logo diameter in px — larger for visibility
LOGO_PATH = ""                   # optional: set to custom logo PNG path to override auto-gen

# ── Vignette overlay ───────────────────────────────────────────
VIGNETTE_RADIUS_FACTOR = 0.65   # vignette starts at this fraction of max radius
VIGNETTE_INTENSITY = 10         # dark colour value (0=black, 255=no vignette)

# ═══════════════════════════════════════════════════════════════════
# YOUTUBE METADATA
# ═══════════════════════════════════════════════════════════════════

YT_CATEGORY_ID = "27"              # Education
YT_PRIVACY_STATUS = "unlisted"

YT_DEFAULT_TAGS = [
    # Tier 1: Primary keywords (broad match)
    "psicología oscura",
    "experimentos psicológicos reales",
    "psicología social",
    "naturaleza humana",
    "experimentos perturbadores",
    # Tier 2: Named experiments (high-intent search)
    "experimento Milgram",
    "experimento cárcel Stanford",
    "MKUltra documental",
    "experimentos prohibidos",
    "control mental psicología",
    # Tier 3: Format tags (captures documentary/video-essay intent)
    "video ensayo psicología",
    "documental psicología español",
    "psicología documental",
    "dark psychology español",
    "true crime psicológico",
    # Tier 4: Long-tail / adjacent
    "psicología conductual",
    "historia de la psicología",
    "manipulación psicológica real",
    "lado oscuro de la psicología",
    "experimentos científicos reales",
]

# ═══════════════════════════════════════════════════════════════════
# SEO
# ═══════════════════════════════════════════════════════════════════

SEO_PRIMARY_KEYWORD = "experimentos psicológicos reales"

SEO_SECONDARY_KEYWORDS = [
    # Named experiments (massive search volume)
    "experimento Milgram explicado",
    "experimento cárcel Stanford",
    "MKUltra documental",
    "experimentos psicológicos famosos",
    # Niche / brand
    "psicología oscura",
    "experimentos perturbadores",
    "naturaleza humana",
    "psicología social",
    # Format
    "documental psicología español",
    "video ensayo psicología",
    # Audience intent
    "psicología para curiosos",
    "estudiantes de psicología",
    "curiosidades psicológicas",
    # Adjacent
    "oscuridad de la mente humana",
    "experimentos prohibidos psicología",
    "psicología conductual extrema",
    "manipulación psicológica real",
    "lado oscuro de la psicología",
    "historia de la psicología experimentos",
]

SEO_HASHTAGS = [
    # Branded (top 3 — always visible above title)
    "#PsicologíaOculta",
    "#PsicologíaReal",
    "#ExperimentosPsicológicos",
    # Named experiments (per-video relevance)
    "#Milgram",
    "#MKUltra",
    "#Stanford",
    # Community / discoverability
    "#Ciencia",
    "#Psicología",
    "#DatosCuriosos",
    "#AprendeEnYouTube",
    "#Documental",
    "#Educación",
    "#MenteHumana",
    "#HistoriaReal",
    "#SabíasQue",
    "#PsicologíaSocial",
]

# Shorts-specific hashtags (different strategy — more discoverability-focused)
SHORTS_HASHTAGS = [
    "#Psicología",
    "#DatosCuriosos",
    "#Shorts",
    "#AprendeEnYouTube",
    "#Ciencia",
    "#Milgram",
    "#PsicologíaOculta",
    "#SabíasQue",
    "#MenteHumana",
    "#Curiosidades",
    "#Educación",
    "#PsicologíaSocial",
]

# ═══════════════════════════════════════════════════════════════════
# DESCRIPTION TEMPLATE
# ═══════════════════════════════════════════════════════════════════

DESCRIPTION_TEMPLATE = """📋 {titulo}
———

{descripcion_seo}

🔬 EN ESTE VIDEO
- Contexto histórico del experimento
- Metodología paso a paso
- Resultados y controversia
- Lo que revela sobre la naturaleza humana

⏱️ CAPÍTULOS
{chapters}

———

🎙️ Bienvenido a **Psicología Oculta** — el canal donde analizamos los experimentos psicológicos reales más oscuros de la historia. Milgram, Stanford, MKUltra y muchos más. Cada video revela verdades inquietantes sobre la naturaleza humana que la ciencia descubrió... y a veces prefirió callar.

📚 Fuentes: Wikipedia, artículos académicos, hilos de Reddit (r/psychology, r/todayilearned) y archivos históricos de experimentos psicológicos reales.

⚠️ Todo el contenido tiene fines educativos y de divulgación científica.

🔔 Suscríbete y activa la campana para descubrir lo que la psicología reveló sobre nosotros mismos.

💬 ¿Qué experimento te impactó más? Déjalo en los comentarios.

#PsicologíaOculta #PsicologíaReal #ExperimentosPsicológicos"""

# ═══════════════════════════════════════════════════════════════════
# THUMBNAIL
# ═══════════════════════════════════════════════════════════════════

THUMBNAIL_WIDTH = 1280
THUMBNAIL_HEIGHT = 720
THUMBNAIL_FONT_SIZE = 56
THUMBNAIL_BORDER_WIDTH = 5

THUMBNAIL_STYLE = {
    "layout": "image_left_text_right",
    "max_text_words": 4,
    "text_color": "warm_offwhite_on_dark",
    "font_style": "bold_sans_serif_condensed",
    "image_treatment": "dramatic_filter_high_contrast_desaturated",
    "background": "#0C0A0A",
    "accent_color": "#A01616",
    "face_policy": (
        "Real faces (public domain) YES — especially eyes cropped close. "
        "Perpetrator faces: B&W, high contrast. Victims: blurred/censored. "
        "AI-generated faces: NO. Siluetas, ojos vendados, manos esposadas OK."
    ),
    "number_preference": "odd_numbers_for_lists",
    "red_accent_rule": (
        "Red underline or red circle on key element boosts CTR ~12%. "
        "NEVER use yellow text (looks clickbait, damages documentary trust)."
    ),
}

THUMBNAIL_TEMPLATES = {
    "the_face": {
        "description": "Close-up of subject/perpetrator face, desaturated, red-tinted shadows",
        "text_position": "bottom_third",
        "text_words": "2-3",
        "accent": "red_circle_on_eyes_or_key_element",
        "best_for": "Perpetrator-focused videos (Milgram, Zimbardo, MKUltra scientists)",
    },
    "the_evidence": {
        "description": "Vintage document, lab photo, or institutional image with 'clasificado' stamp",
        "text_position": "left_or_bottom_over_gradient",
        "text_words": "3-4",
        "accent": "clasificado_stamp_graphic",
        "best_for": "Experiment-procedure videos (Stanford Prison, Robbers Cave)",
    },
    "the_contrast": {
        "description": "Left: innocuous image. Right: red-tinted disturbing counterpart",
        "text_position": "bridging_center",
        "text_words": "3",
        "accent": "red_arrow_or_split_line",
        "best_for": "Before/after, expectation vs reality, innocence vs horror",
    },
}

# ═══════════════════════════════════════════════════════════════════
# VIDEO TIMING & MONETIZATION
# ═══════════════════════════════════════════════════════════════════

VIDEO_OPTIMAL_DURATION_MINUTES = 10

VIDEO_MIDROLL_STRATEGY = (
    "Colocar mid-rolls en pausas naturales entre capítulos narrativos. "
    "NUNCA en medio de una frase ni durante el clímax. "
    "Cada mid-roll debe preceder un mini-gancho que mantenga al espectador."
)

MONETIZATION_TARGET_CPM = "$7–$15 USD"

MONETIZATION_VERTICALS = [
    "Educación online / EdTech",
    "Salud mental / Bienestar",
    "Finanzas personales",
    "Libros / Audiolibros",
    "Tecnología",
]

# ═══════════════════════════════════════════════════════════════════
# END SCREEN
# ═══════════════════════════════════════════════════════════════════

END_SCREEN_STRATEGY = {
    "left_card": {
        "type": "playlist",
        "content": "most_relevant_playlist",
        "purpose": "Keep viewer in a session loop — thematic rabbit hole",
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
        "Si este experimento te pareció perturbador, el siguiente en la lista "
        "lo es todavía más. Te dejo el enlace en pantalla. Suscríbete si quieres "
        "entender lo que la psicología oculta sobre la naturaleza humana."
    ),
}

# ═══════════════════════════════════════════════════════════════════
# PLAYLISTS
# ═══════════════════════════════════════════════════════════════════

PLAYLISTS = [
    {
        "slug": "expedientes-completos",
        "name": "Expedientes Completos",
        "description": (
            "Análisis documental en profundidad de los experimentos psicológicos "
            "más oscuros de la historia. Milgram, Stanford, MKUltra y más. "
            "Cada video: contexto, metodología, resultados y consecuencias."
        ),
        "type": "main",
    },
    {
        "slug": "lo-mas-perturbador",
        "name": "Lo Más Perturbador",
        "description": (
            "Los 5 experimentos más impactantes del canal. Si eres nuevo aquí, "
            "empieza por esta lista. Esto es Psicología Oculta."
        ),
        "type": "onboarding",
    },
    {
        "slug": "la-trilogia-oscura",
        "name": "La Trilogía Oscura",
        "description": (
            "Series temáticas de 3 videos conectados. Obediencia, control mental, "
            "manipulación. Mira los tres en orden para la experiencia completa."
        ),
        "type": "series",
    },
    {
        "slug": "mente-y-control",
        "name": "Mente y Control",
        "description": (
            "MKUltra, condicionamiento extremo y los experimentos secretos sobre "
            "control mental. Lo que la CIA y los gobiernos hicieron... y lo que "
            "la psicología aprendió."
        ),
        "type": "thematic",
    },
    {
        "slug": "experimentos-por-pais",
        "name": "Experimentos por País",
        "description": (
            "Estados Unidos, Unión Soviética, América Latina. Los experimentos "
            "psicológicos más oscuros organizados por región. Descubre qué pasó "
            "en cada rincón del mundo."
        ),
        "type": "geo",
    },
]

# ═══════════════════════════════════════════════════════════════════
# FIRST 48 HOURS STRATEGY
# ═══════════════════════════════════════════════════════════════════

FIRST_48H_STRATEGY = {
    "pre_upload_24h": [
        "Community Tab poll: '¿Cuál experimento te perturba más? A) Milgram B) Stanford Prison C) MKUltra'",
        "YouTube Story: blurred image + 'Mañana. 9PM MX. Esto fue real.'",
    ],
    "hour_0": [
        "Publish at 9PM Mexico City time (peak psychology consumption window)",
        "Thumbnail A/B test: 3 variants auto-rotating via YouTube test feature",
        "First comment (immediate, pinned): pregunta provocadora para disparar comment velocity",
    ],
    "hours_1_6": [
        "Reddit r/psicologia: TEXT post (not link) with compelling summary. 'Video completo en mi perfil.'",
        "Facebook groups: Psicología para todos, Datos curiosos, Historia oscura — text post + link in comments",
    ],
    "hours_6_24": [
        "Reply to EVERY comment in first 24h (3x algorithm weight on engagement)",
        "Identify top-performing comment thread → create Community post expanding on it",
        "Twitter/X thread: 5-7 tweets summarizing experiment, final tweet = YouTube link",
    ],
    "hours_24_48": [
        "Analyze CTR and retention in YouTube Studio",
        "If CTR < 5%: swap thumbnail variant",
        "If retention drop > 40% at any point: trim section in YouTube Editor",
        "Second Community Tab: close the loop from pre-upload poll with video results",
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
            "example": "¿Habrías obedecido al experimentador en el experimento de Milgram?",
            "options": ["Sí", "No", "No lo sé"],
        },
        "wednesday": {
            "type": "image_fact",
            "example": (
                "Imagen de la cárcel de Stanford + 'El experimento iba a durar "
                "14 días. Fue cancelado en el día 6 porque los guardias comenzaron "
                "a abusar psicológicamente de los prisioneros.'"
            ),
        },
        "friday": {
            "type": "teaser",
            "example": (
                "Este sábado: el experimento más cruel sobre indefensión aprendida. "
                "Activa la campana."
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
            "{number} personas participaron en este experimento. "
            "Solo {number} salieron igual."
        ),
        "structure": "Hook → Experiment setup (15s) → Result (15s) → 'Video completo en YouTube, link en bio' (10s)",
        "cadence": "3x/day from one long-form video (days 1, 3, 5)",
    },
    "youtube_shorts": {
        "format": "15-30s most shocking moment",
        "end_cta": "'Video completo en el canal' linked to long-form",
        "purpose": "Shorts feed → channel page → long-form viewer conversion",
    },
    "twitter_x": {
        "format": "Thread — 1 experiment = 1 thread per week",
        "template": "'HOY en Psicología Oculta: El experimento que demostró que...' + 5-7 tweets + link",
    },
    "spotify_podcast": {
        "format": "Audio-only export of each video",
        "title_format": "Psicología Oculta | {experiment_name} | {shocking_detail}",
        "purpose": "Minimal effort, massive discovery platform (Spotify podcast search)",
    },
}

# ═══════════════════════════════════════════════════════════════════
# COLLABORATION TARGETS
# ═══════════════════════════════════════════════════════════════════

COLLABORATION_TARGETS = {
    "tier_1_direct": [
        {"name": "La Hiperactina", "niche": "Biología explicada, audiencia comparte interés en psicología"},
        {"name": "Cordura Artificial", "niche": "Filosofía + psicología, tono similar"},
        {"name": "El Robot de Platón", "niche": "Divulgación científica, episodios de historia oscura"},
        {"name": "Ciencia de Sofá", "niche": "Ciencia accesible, potencial colaboración en psicología"},
        {"name": "Antroporama", "niche": "Psicología y antropología"},
    ],
    "tier_2_adjacent": [
        {"name": "Mundo Creepy", "niche": "Misterio, enorme audiencia LATAM"},
        {"name": "DrossRotzank", "niche": "Dark content pioneer — estrategia de mención, no colaboración directa"},
        {"name": "Relatos del Lado Oscuro", "niche": "Horror/misterio narrado, mismo formato (sin rostro)"},
    ],
    "tier_3_english": [
        {"name": "Vsauce", "niche": "Crear videos respuesta en español: 'Vsauce explicó Milgram... pero omitió esto'"},
        {"name": "Kurzgesagt", "niche": "Remix de sus temas psicológicos con ángulo dark-thriller"},
    ],
    "collab_formats": [
        "React: 'Un psicólogo reacciona a Psicología Oculta' — feature en tu canal",
        "Expert interview: 5-min audio cameo de estudiante/profesor de psicología",
        "Topic trade: cubrir el experimento sugerido por otro creador, cross-promote",
        "Mention strategy: 'Como dijo [Creator] en su video sobre...' — genera goodwill",
    ],
}

# ═══════════════════════════════════════════════════════════════════
# TRENDING TOPIC HOOKS
# ═══════════════════════════════════════════════════════════════════

TRENDING_TOPIC_HOOKS = {
    "type_a_news": {
        "trigger": "Crimen/evento con manipulación psicológica en las noticias",
        "pivot": (
            "Esto que acaba de pasar en {country}... la psicología ya lo "
            "explicó en {year}."
        ),
        "example": "Arresto de líder de secta → video sobre experimentos de culto",
    },
    "type_b_anniversary": {
        "trigger": "Aniversario de experimentos famosos (search spikes)",
        "calendar": {
            "august": "Milgram (1961) + Stanford Prison (1971) — producir 2 semanas antes",
            "may": "MKUltra revelations / Mental Health Awareness Month — subvertir el ángulo positivo",
            "october": "Halloween search spike — todos los experimentos horror-adjacent",
        },
    },
    "type_c_pop_culture": {
        "trigger": "Estreno de serie/película sobre psicología experimental",
        "strategy": "'La historia REAL detrás de {show/movie}'",
        "examples": "Stranger Things → MKUltra, Mindhunter → criminal psychology",
    },
    "type_d_social_media": {
        "trigger": "Experimento social viral en TikTok/Instagram",
        "pivot": "'Eso no es un experimento social. ESTO sí lo es.'",
    },
    "type_e_calendar": {
        "name": "Calendario de la Oscuridad Humana",
        "months": {
            "january": "Stanford Prison (temas de encarcelamiento/resolución)",
            "march": "Sleep deprivation experiments (World Sleep Day Mar 14)",
            "may": "MKUltra / mind control (Mental Health Awareness Month)",
            "august": "Milgram + Stanford Prison anniversaries",
            "october": "ALL horror-adjacent experiments (Halloween search spike)",
            "december": "Conformity experiments (holiday social pressure)",
        },
    },
}

# ═══════════════════════════════════════════════════════════════════
# CONTENT PILLARS
# ═══════════════════════════════════════════════════════════════════

CONTENT_PILLARS = [
    {
        "name": "El Experimento",
        "ratio": 60,
        "desc": "Documental profundo de un experimento individual",
    },
    {
        "name": "Listas y Rankings",
        "ratio": 25,
        "desc": "Compilación temática de múltiples experimentos",
    },
    {
        "name": "El Contexto",
        "ratio": 15,
        "desc": "Concepto psicológico breve nacido de un experimento",
    },
]

# ═══════════════════════════════════════════════════════════════════
# SEASON 1 EPISODE PLAN
# ═══════════════════════════════════════════════════════════════════

SEASON1_EPISODES = [
    {
        "ep": 1,
        "title": "El Botón que Podía Matar",
        "experiment": "Milgram (obediencia a la autoridad)",
        "hook": "El 65% de las personas normales electrocutaría a un extraño hasta la muerte si alguien con bata blanca se lo ordena.",
    },
    {
        "ep": 2,
        "title": "6 Días que se Volvieron Infierno",
        "experiment": "Stanford Prison (Zimbardo)",
        "hook": "Iba a durar 2 semanas. Lo cancelaron al sexto día porque los estudiantes ya estaban torturando a sus compañeros.",
    },
    {
        "ep": 3,
        "title": "El Bebé al que le Enseñaron el Terror",
        "experiment": "Little Albert (Watson y Rayner)",
        "hook": "Tomaron a un bebé sano de 9 meses y le enseñaron a tener fobia. Nadie sabe qué fue de él.",
    },
    {
        "ep": 4,
        "title": "El Experimento Más Cruel Sobre el Amor",
        "experiment": "Harlow's Monkeys",
        "hook": "Bebés monos separados de sus madres y criados con madres de alambre. Lo que descubrieron rompió la psicología.",
    },
    {
        "ep": 5,
        "title": "El Proyecto Secreto de Control Mental de la CIA",
        "experiment": "MKUltra",
        "hook": "Durante 20 años, la CIA drogó a ciudadanos sin su consentimiento. Destruyeron la mayoría de los archivos.",
    },
    {
        "ep": 6,
        "title": "Cómo Crear una Guerra en una Semana",
        "experiment": "Robbers Cave (Sherif)",
        "hook": "22 niños, un campamento de verano, y psicólogos que deliberadamente los convirtieron en enemigos.",
    },
    {
        "ep": 7,
        "title": "El Experimento que Demostró que Eres un Monstruo",
        "experiment": "Efecto Espectador / Kitty Genovese",
        "hook": "38 personas la vieron morir. Nadie llamó a la policía. La psicología aún intenta explicar por qué.",
    },
    {
        "ep": 8,
        "title": "El Estudio que Convirtió Huérfanos en Tartamudos",
        "experiment": "Monster Study (Wendell Johnson)",
        "hook": "Les dijeron a niños huérfanos sanos que eran tartamudos graves. Algunos nunca volvieron a hablar normalmente.",
    },
    {
        "ep": 9,
        "title": "Hicieron que Personas Normales Perdieran la Cabeza",
        "experiment": "Rosenhan Experiment",
        "hook": "8 personas sanas fingieron escuchar voces. Los psiquiatras las internaron. Luego, cuando dijeron la verdad... nadie les creyó.",
    },
    {
        "ep": 10,
        "title": "Los 5 Experimentos Más Crueles de la Historia",
        "experiment": "Compilación (Aversion Project, Tuskegee, Unit 731, más)",
        "hook": "Estos experimentos fueron tan atroces que los manuales de ética se escribieron para asegurarse de que nunca se repitieran.",
    },
]
