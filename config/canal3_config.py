"""Configuration for Canal 3: Civilizaciones Olvidadas.

Meta-niche: "las civilizaciones que la historia enterró durante siglos,
los secretos que aún guardan sus ruinas y los misterios que la arqueología
todavía no ha podido resolver"

Formato: video-essay documental, 10-15 min, narrado con imágenes
cinematográficas de ruinas, artefactos y reconstrucciones.

Estilo: "documental arqueológico" — archivos de lo que el tiempo olvidó.
"""

# ═══════════════════════════════════════════════════════════════════
# IDENTITY
# ═══════════════════════════════════════════════════════════════════

CANAL_NAME = "canal3"
CANAL_DISPLAY_NAME = "Civilizaciones Olvidadas"
CANAL_TAGLINE = (
    "Las civilizaciones que la historia enterró durante siglos... "
    "y los secretos que aún guardan sus ruinas."
)
CANAL_OUTRO_TAGLINE = (
    "El pasado nunca desaparece del todo. Solo espera a ser descubierto."
)

# ── YouTube Handle ───────────────────────────────────────────
YOUTUBE_HANDLE = "@CivilizacionesOlvidadas-r7f"
YOUTUBE_CHANNEL_URL = "https://www.youtube.com/@CivilizacionesOlvidadas-r7f"

# ── Narrative Style ─────────────────────────────────────────────
CANAL_NARRATIVE_STYLE = "documental arqueológico"
CANAL_STYLE_DESCRIPTION = (
    "Las civilizaciones que el tiempo borró, los templos que la jungla "
    "devoró, los secretos que la arqueología aún no ha podido explicar. "
    "Cada video es una expedición al pasado. El formato sin rostro permite "
    "que las piedras, los mapas y las ruinas hablen por sí solas — estas "
    "historias no necesitan presentador. Necesitan testigos."
)

# ── Channel About Section (indexado por YouTube search) ─────────
CHANNEL_ABOUT_SECTION = """Bienvenido a Civilizaciones Olvidadas.

Exploramos las civilizaciones perdidas, las ruinas antiguas y los secretos históricos que la humanidad ha dejado atrás. Desde Göbekli Tepe hasta los Mayas, desde el Valle del Indo hasta Angkor Wat — cada video es una expedición al pasado que la historia oficial no te contó.

🎬 Formato: video ensayos documentales (10-15 minutos)
🗓️ Nuevos descubrimientos: 2-3 por semana
🎙️ Narración documental con fuentes históricas y arqueológicas verificadas

📩 Contacto: {email}

🏛️ Si te fascinan las civilizaciones antiguas, las ruinas misteriosas, la arqueología, los secretos de la historia y los enigmas que la ciencia aún no ha resuelto... este canal es para ti.

Suscríbete y activa la campana para no perderte ningún secreto del pasado."""

# ── Channel Keywords (YouTube Studio → Settings → Channel) ──────
CHANNEL_KEYWORDS = [
    "civilizaciones perdidas",
    "secretos de la historia",
    "civilizaciones antiguas",
    "ruinas misteriosas",
    "arqueología",
    "misterios de la historia",
    "civilizaciones olvidadas",
    "documental historia",
    "enigmas de la humanidad",
    "ciudades perdidas",
    "historia antigua",
    "descubrimientos arqueológicos",
    "culturas antiguas",
    "templos perdidos",
    "documental arqueología",
    "misterios sin resolver",
    "historia universal",
    "grandes imperios",
    "secretos del pasado",
    "documental en español",
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

PROD_SCRIPT_WORDS_MIN = 2200
PROD_SCRIPT_WORDS_MAX = 3800
PROD_SCRIPT_SCENES_MIN = 10
PROD_SCRIPT_SCENES_MAX = 18
PROD_SCRIPT_BLOCKS_MIN = 10
PROD_SCRIPT_BLOCKS_MAX = 18
PROD_VIDEO_DURATION_MIN = 10
PROD_VIDEO_DURATION_MAX = 15

# ── Average video duration target (approx, in minutes) ──
VIDEO_AVERAGE_DURATION_MIN = 12
VIDEO_DURATION_DISCREPANCY_MIN = 3

# ═══════════════════════════════════════════════════════════════════
# NARRATIVE TONE
# ═══════════════════════════════════════════════════════════════════

CANAL_TONE = (
    "Grave, misterioso y profundamente envolvente. Narrativa documental "
    "que oscila entre el rigor histórico y el asombro arqueológico. "
    "Como un documental de National Geographic sobre las grandes "
    "civilizaciones perdidas. La voz debe transmitir la solemnidad de "
    "quien camina entre ruinas milenarias y la emoción del descubrimiento. "
    "El espectador debe sentir que está explorando un templo prohibido, "
    "no que le están dando una clase de historia."
)

# ═══════════════════════════════════════════════════════════════════
# TARGET AUDIENCE
# ═══════════════════════════════════════════════════════════════════

TARGET_AUDIENCE = (
    "18-55 años (amplio), LATAM (MX 35%, CO 20%, AR 15%, PE 10%, ES 10%, otros 10%). "
    "Curiosos, mente abierta, interés en historia, arqueología, misterios y culturas antiguas. "
    "55% hombres / 45% mujeres. "
    "65%+ mobile. Sesiones de 10-15 min. Pico de consumo: 20:00-00:00 local."
)

TARGET_AUDIENCE_PSYCHOGRAPHIC = {
    "The Explorer": (
        "Siente fascinación por lo desconocido. Quiere descubrir mundos "
        "perdidos y viajar en el tiempo desde su sofá."
    ),
    "The History Buff": (
        "Consume documentales históricos con pasión. Sabe de historia "
        "pero siempre busca el ángulo que no le contaron."
    ),
    "The Conspiracy Curious": (
        "Entra por el misterio, se queda por los hechos. Comparte para "
        "debatir: 'esto cambia todo lo que creíamos saber'."
    ),
    "The Travel Dreamer": (
        "Sueña con visitar Machu Picchu, Petra, Angkor Wat. "
        "Cada video es un viaje virtual a un destino imposible."
    ),
}

# ═══════════════════════════════════════════════════════════════════
# TITLE OPTIMIZATION
# ═══════════════════════════════════════════════════════════════════

TITLE_FORMULAS = [
    # Pattern 1: Lost Civilization + Shocking Fact
    "La Civilización que {action} y Nadie Puede Explicar",
    # Pattern 2: Place + Discovery
    "Encontraron {descubrimiento} en {lugar} y Cambió la Historia",
    # Pattern 3: Number + Mystery
    "{número} Civilizaciones que Desaparecieron Sin Dejar Rastro",
    # Pattern 4: Question + Secrets
    "¿Qué Pasó Realmente con {civilización}? El Misterio que la Ciencia No Resuelve",
    # Pattern 5: Archaeological Revelation
    "El Descubrimiento en {lugar} que Reescribe la Historia Antigua",
    # Pattern 6: Before and After
    "Antes de {civilización_famosa}, Existió {civilización_olvidada}",
    # Pattern 7: Forbidden Knowledge
    "El Oscuro Secreto de {civilización} que la Historia Oficial Ocultó",
    # Pattern 8: Technology vs Ancient
    "Tenían Tecnología que No Deberían Tener: El Misterio de {lugar}",
]

TITLE_POWER_WORDS = [
    # Discovery / Revelation
    "descubrieron", "encontraron", "revelado", "oculto", "secreto",
    "olvidado", "prohibido", "censurado", "enterrado",
    # Mystery / Awe
    "misterio", "enigma", "inexplicable", "imposible", "asombroso",
    "desconcertante", "fascinante", "alucinante",
    # Ancient / Lost
    "perdida", "milenaria", "ancestral", "desaparecida", "sumergida",
    "maldita", "sagrada", "prohibida",
    # Authority / Science
    "demostrado", "confirmado", "verificado", "documentado",
    # Scale / Impact
    "reescribió", "cambió", "revolucionó", "sacudió",
]

TITLE_MAX_CHARS = 65

# ═══════════════════════════════════════════════════════════════════
# SCRIPT STRUCTURE — "Expedición al Pasado" method
# ═══════════════════════════════════════════════════════════════════

SCRIPT_HOOK_RULE = (
    "ATENCIÓN: La primera frase del guion DEBE ser el hecho más "
    "impactante del descubrimiento o civilización, con un NÚMERO y un "
    "HECHO CONCRETO. NUNCA empezar con contexto histórico genérico, "
    "definiciones, ni presentaciones. NUNCA 'Hola, bienvenidos a...' "
    "ni 'En este video vamos a hablar de...'.\n\n"
    "EJEMPLO CORRECTO: 'En 1994, un pastor kurdo encontró una piedra "
    "tallada que cambió todo lo que sabíamos sobre la civilización "
    "humana. Tenía 12.000 años de antigüedad.'\n"
    "EJEMPLO INCORRECTO: 'Las civilizaciones antiguas han fascinado a "
    "la humanidad durante siglos por sus misterios y secretos...'"
)

# 7-step structure with retention anchors
SCRIPT_STRUCTURE = [
    {
        "step": "EL DESCUBRIMIENTO",
        "time_pct": "0-10%",
        "description": (
            "El hallazgo más impactante. Quién lo encontró, cómo, por qué "
            "es tan importante. Imagen: la ruina o artefacto más evocador. "
            "Cerrar con promesa: 'Al final de este video vas a entender "
            "por qué este descubrimiento cambió la historia para siempre.'"
        ),
    },
    {
        "step": "EL CONTEXTO",
        "time_pct": "10-20%",
        "description": (
            "Lo que se creía antes de este descubrimiento. Cómo este hallazgo "
            "contradice la historia oficial. Construir anticipación: "
            "'Los libros de historia decían una cosa... pero las piedras "
            "contaban otra muy distinta.'"
        ),
    },
    {
        "step": "LA CIVILIZACIÓN",
        "time_pct": "20-30%",
        "description": (
            "Quiénes eran. Cómo vivían. Qué construyeron. Qué creían. "
            "Humanizar a los protagonistas del pasado. 'Imagina caminar "
            "por sus calles hace 5.000 años...'"
        ),
        "retention_anchor": (
            "CLIFFHANGER al 25%: 'Pero lo que los arqueólogos no esperaban "
            "encontrar... es que esta civilización guardaba un secreto "
            "que desafía todo lo que sabemos.'"
        ),
    },
    {
        "step": "EL MISTERIO",
        "time_pct": "30-55%",
        "description": (
            "El enigma central. Lo que no encaja. La anomalía. Escalar "
            "el asombro. 'Y entonces, entre las ruinas, encontraron algo "
            "que ningún historiador podía explicar.'"
        ),
        "retention_anchor": (
            "CLIFFHANGER al 50%: Silencio 2s. Cambio de imagen a "
            "primer plano del artefacto/ruina. 'Recapitulemos: [1 frase]. "
            "Ahora prepárate para lo más increíble.'"
        ),
    },
    {
        "step": "LA REVELACIÓN",
        "time_pct": "55-70%",
        "description": (
            "El instante de comprensión. El hallazgo que lo cambia todo. "
            "Música crece. Imagen del descubrimiento clave. 'Esto es lo "
            "que encontraron... y es mucho más antiguo de lo que creían.'"
        ),
    },
    {
        "step": "LAS CONSECUENCIAS",
        "time_pct": "70-85%",
        "description": (
            "Cómo este descubrimiento cambió la historia. Qué dice la "
            "arqueología moderna. Lo que aún no sabemos. 'Los libros de "
            "historia tuvieron que ser reescritos.'"
        ),
        "retention_anchor": (
            "EL ESPEJO al 70%: Conectar con el presente. 'Ahora piensa: "
            "¿cuántas civilizaciones más siguen enterradas bajo tus pies? "
            "¿Cuántos secretos guarda la tierra que pisas cada día?'"
        ),
    },
    {
        "step": "EL LEGADO",
        "time_pct": "85-100%",
        "description": (
            "Lo que nos dejaron. Por qué importa hoy. Pregunta al espectador. "
            "Reflexión sobre el paso del tiempo y la fragilidad de las "
            "civilizaciones. End hook + CTA."
        ),
    },
]

SCRIPT_END_HOOK = (
    "Y si crees que esta historia es increíble, espera a ver lo que "
    "descubrieron en {next_place}. Porque lo que los arqueólogos "
    "encontraron allí es todavía más inexplicable. Ese es el próximo "
    "video. Dale like, suscríbete y activa la campana."
)

SCRIPT_EMOTIONAL_ARC = {
    "0-10%": "asombro",
    "10-20%": "curiosidad",
    "20-30%": "fascinación",
    "30-45%": "intriga",
    "45-55%": "misterio",
    "55-65%": "revelación",
    "65-75%": "estupefacción → comprensión",
    "75-85%": "reflexión",
    "85-95%": "solemnidad",
    "95-100%": "maravilla",
}

# Retention anchors
RETENTION_ANCHORS = {
    "at_25_pct": {
        "trigger": "cliffhanger_archaeological",
        "action": (
            "Insertar mini-cliffhanger: 'Pero bajo esa piedra, a solo "
            "3 metros de profundidad, encontraron algo que no debería "
            "estar allí.' Tratar el video como una expedición."
        ),
    },
    "at_50_pct": {
        "trigger": "the_reset",
        "action": (
            "Música fuera 2s. Nueva imagen del hallazgo clave. Voz en off: "
            "'Recapitulemos: [resumen 1 frase]. Ahora viene lo que ningún "
            "arqueólogo esperaba.'"
        ),
    },
    "at_70_pct": {
        "trigger": "the_mirror",
        "action": (
            "Conectar con el espectador directamente: 'Ahora mira a tu "
            "alrededor. ¿Cuántas civilizaciones enterradas hay donde "
            "estás ahora mismo?' Cerrar con teaser: 'En 60 segundos "
            "te cuento qué significa esto para nuestra historia.'"
        ),
    },
}

# ═══════════════════════════════════════════════════════════════════
# VIRALITY TRIGGERS
# ═══════════════════════════════════════════════════════════════════

VIRALITY_TRIGGERS = [
    {
        "name": "Awe & Discovery",
        "mechanism": (
            "'No sabías que esto existía.' El asombro arqueológico es "
            "la emoción que más se comparte. La gente comparte para que "
            "otros también sepan que 'todo lo que creían saber era mentira.'"
        ),
    },
    {
        "name": "Time Travel",
        "mechanism": (
            "Transportar al espectador a otra época. Contenido que hace "
            "sentir al viewer como un explorador del tiempo. La inmersión "
            "histórica genera altísima retención y reenvío."
        ),
    },
    {
        "name": "Conspiracy Adjacent",
        "mechanism": (
            "Cerrar con idea que desafía la narrativa oficial sin caer "
            "en lo conspiranoico. 'La historia oficial dice X, pero las "
            "piedras cuentan otra cosa.' Debate = comentarios = engagement."
        ),
    },
    {
        "name": "Identity Signaling",
        "mechanism": (
            "Compartir este video dice: 'Yo sé cosas que los demás no.' "
            "o 'Yo tengo cultura y curiosidad intelectual.' "
            "Enmarcar el contenido como conocimiento exclusivo."
        ),
    },
    {
        "name": "Mystery Hook",
        "mechanism": (
            "Cada guion debe incluir un 'vacío de información' que el "
            "espectador NECESITA llenar. 'Pero hay algo más... algo que "
            "los arqueólogos encontraron y no se atrevieron a publicar.' "
            "El cliffhanger arqueológico genera retención extrema."
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
    "rate_base": "-12%",        # slower, ~145 WPM documentary pace
    "pitch_base": "-2Hz",       # slightly deeper, gravitas

    # ── Hook (opening impact) ────────────────────────────
    "rate_hook": "-8%",         # slightly faster for hook energy
    "pitch_hook": "+0Hz",

    # ── Desarrollo (body — narrative storytelling) ──────
    "rate_desarrollo": "-10%",
    "pitch_desarrollo": "-2Hz",

    # ── Climax (archaeological revelation peak) ─────────
    "rate_climax": "-18%",      # slowest — awe and gravity
    "pitch_climax": "-4Hz",     # deeper for mystery

    # ── Reflexion (contemplative) ───────────────────────
    "rate_reflexion": "-12%",
    "pitch_reflexion": "-2Hz",

    # ── Cierre (closing call-to-action) ──────────────────
    "rate_cierre": "-8%",
    "pitch_cierre": "+0Hz",
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

TTS_ENGINE = "kokoro"

# ── Kokoro configuration ─────────────────────────────────────
KOKORO_VOICE = "em_santa"

KOKORO_BLOCK_SPEEDS = {
    "hook": 1.04,        # captura atención — ligeramente rápido
    "desarrollo": 0.92,  # narración pausada, tono documental
    "climax": 0.84,      # revelación máxima — lento, dramático
    "reflexion": 0.90,   # contemplativo, solemne
    "cierre": 0.96,      # conclusión — casi normal
}

KOKORO_PAUSE_BETWEEN_BLOCKS = 0.8

# ═══════════════════════════════════════════════════════════════════
# CONTENT SOURCES
# ═══════════════════════════════════════════════════════════════════

REDDIT_SUBREDDITS = [
    # Archaeology / Ancient History
    "Archaeology",
    "AlternativeHistory",
    "history",
    "LostCivilizations",
    "ancientrome",
    "mesoamerica",
    "ancientegypt",
    "AncientCivilizations",
    # Mystery / Unsolved
    "UnresolvedMysteries",
    "HighStrangeness",
    "nonmurdermysteries",
    # General knowledge
    "Damnthatsinteresting",
    "interestingasfuck",
    "todayilearned",
    "TrueReddit",
    # Artifacts
    "ArtefactPorn",
    "castles",
    "ImaginaryLandscapes",
]

REDDIT_SORT = "top"
REDDIT_TIME = "month"
REDDIT_LIMIT = 25

WIKIPEDIA_CATEGORIES = [
    # English — primary research
    "Lost cities",
    "Ancient civilizations",
    "Archaeological mysteries",
    "Unsolved problems in archaeology",
    "Ruins",
    "Megalithic monuments",
    "Prehistoric sites",
    "Archaeological sites by country",
    "Former empires",
    "Extinct states",
    "Ancient peoples",
    "Disappeared peoples",
    "Underwater archaeological sites",
    "Petroglyphs",
    "Rock art",
    "Ancient technology",
    "History of writing",
    "Ancient languages",
    "Ancient warfare",
    "List of archaeological periods",
    "World Heritage Sites by country",
    # Spanish Wikipedia
    "Civilizaciones antiguas",
    "Ciudades perdidas",
    "Yacimientos arqueológicos",
    "Lugares misteriosos",
    "Ruinas",
    "Monumentos megalíticos",
    "Historia antigua",
    "Imperios desaparecidos",
    "Arqueología",
]

# ═══════════════════════════════════════════════════════════════════
# SCRAPE SOURCES (multi-source plugin system)
# ═══════════════════════════════════════════════════════════════════

SCRAPE_SOURCES = [
    {"plugin": "wikipedia", "priority": 1},
    {"plugin": "reddit", "priority": 2},
    {"plugin": "atlas_obscura", "priority": 3},
    {"plugin": "google_news", "priority": 4},
    {"plugin": "rss", "priority": 5},
]

# Atlas Obscura categories for archaeological/historical wonders
ATLAS_OBSCURA_CATEGORIES = ["wonders", "history", "ruins", "ancient", "unique"]

# RSS feeds for archaeology and history news
RSS_FEEDS = [
    "https://www.archaeology.org/news?format=feed",
    "https://www.ancient-origins.net/rss.xml",
]

# Google News queries for archaeology and lost civilizations
GOOGLE_NEWS_QUERIES = [
    "descubrimiento arqueológico",
    "civilización perdida",
    "ruinas antiguas",
    "ciudad antigua encontrada",
    "nuevo hallazgo arqueología",
    "misterio histórico resuelto",
    "tumba antigua descubierta",
    "templo perdido encontrado",
]
GOOGLE_NEWS_LANGUAGE = "es"
GOOGLE_NEWS_COUNTRY = "ES"

# ═══════════════════════════════════════════════════════════════════
# VISUAL STYLE
# ═══════════════════════════════════════════════════════════════════

IMAGE_STYLE_MODIFIERS = (
    "ancient ruins, archaeological sites, historical artifacts, "
    "cinematic 16:9, warm golden hour photography, stone textures, "
    "desert landscapes, temple interiors, atmospheric lighting, "
    "professional documentary photography, mysterious and solemn mood, "
    "earthy tones, dust particles in light rays"
)

COLOR_PALETTE = {
    "primary": (194, 154, 75),       # sand gold / warm ochre
    "secondary": (62, 38, 22),       # deep earth brown
    "accent": (168, 104, 52),        # terracotta / clay
    "text": (245, 238, 220),          # warm parchment off-white
    "text_shadow": (10, 6, 2),       # subtle warm dark shadow
    "tertiary": (45, 38, 28),        # dark earth (safe background)
    "warning": (212, 160, 45),       # bright gold for thumbnail CTR accent
}

FILM_GRAIN_OPACITY = 8              # noticeable grain for ancient/archival feel
FILM_GRAIN_FRAMES = 8
KEN_BURNS_ZOOM_MIN = 4
KEN_BURNS_ZOOM_MAX = 10

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
    "target_video_pct": 50,            # governor target: ~50% video scenes
    "max_placeholder_pct": 0,          # governor: 0% placeholder scenes tolerated
    "video_fallback_to_image": True,
    "video_min_duration": 4,
    "video_max_duration": 20,
    "video_sources": ["pexels"],
    "video_providers": [
        {"name": "pexels", "api_key_env": "PEXELS_API_KEY"},
        {"name": "pixabay", "api_key_env": "PIXABAY_API_KEY"},
        {"name": "mixkit"},
        {"name": "youtube_cc"},            # last resort — no API key (uses yt-dlp)
    ],
    "fallback_query": "ancient ruins archaeological site cinematic 16:9",
    "fallback_query_simple": "ancient temple ruins stone architecture",
    # ── Video rescue: generic queries when specific ones fail ─────
    "video_fallback_queries": [
        "drone aerial ancient ruins desert pyramids cinematic",
        "cinematic museum artifacts exhibition historical documentary",
        "mysterious temple interior torchlight dark atmosphere",
        "archaeological excavation dig historical site documentary",
        "ancient civilization legacy golden hour ruins landscape",
    ],
    "min_video_pct": 30,              # trigger second pass if below this %
    "ken_burns_zoom_min": 4,
    "ken_burns_zoom_max": 10,
    "crossfade_min": 0.4,
    "crossfade_max": 0.8,
    # ── Pollo AI fallback ─────────────────────────────────────────
    "ai_image_fallback": True,           # enable Pollo AI when stock fails
    "ai_max_per_video": 2,               # hard cap: max 2 Pollo gen/video
}

SUBTITLES_ENABLED = False

# ── Inter-paragraph transitions ──────────────────────────────────
TRANSITION_ENABLED = True
TRANSITION_DURATION_MIN = 1.0   # seconds for cambio_tematico=1
TRANSITION_DURATION_MAX = 5.0   # seconds for cambio_tematico=10

# ── Background music ────────────────────────────────────────────
BACKGROUND_MUSIC_ENABLED = True
BACKGROUND_MUSIC_VOLUME = -18.0       # dB during transitions/silence
BACKGROUND_MUSIC_DUCK_VOLUME = -28.0  # dB during narration

# ── Intro / Outro ──────────────────────────────────────────────
INTRO_DURATION_SEC = 3.0
INTRO_FONT_SIZE = 72
INTRO_BG_COLOR = (22, 18, 12)      # dark earth
OUTRO_DURATION_SEC = 5.0
OUTRO_FONT_SIZE = 60
OUTRO_BG_COLOR = (22, 18, 12)
OUTRO_TEXT = "Suscríbete"
OUTRO_CTA_LIKE = "👍 Like"
OUTRO_CTA_SUBSCRIBE = "🔔 Suscríbete"
OUTRO_CTA_BELL = "📢 Comparte"
# ── CTA visual text (shown on screen during the CTA segment) ──
CTA_TEXT = (
    "Si te apasiona descubrir civilizaciones olvidadas,\n"
    "¡dale like, suscríbete y acompáñanos en la próxima expedición!"
)
# ── Template voice-over texts (TTS with channel narrator voice) ──
INTRO_VOICE_TEXT = "Bienvenidos a Civilizaciones Olvidadas, la historia que el tiempo enterró."
CTA_VOICE_TEXT = "Si esta historia despertó tu curiosidad, suscríbete y desentierra con nosotros el próximo secreto."
OUTRO_VOICE_TEXT = "Gracias por vernos. Nos vemos en la próxima civilización."

CANAL_INITIALS = "CO"             # Civilizaciones Olvidadas
LOGO_SIZE = 140
LOGO_PATH = ""

# ── Vignette ──────────────────────────────────────────────────
# Now hardcoded in video_editor._create_vignette_clip() for uniform subtle effect across all channels

# ═══════════════════════════════════════════════════════════════════
# YOUTUBE METADATA
# ═══════════════════════════════════════════════════════════════════

YT_CATEGORY_ID = "27"              # Education
YT_PRIVACY_STATUS = "public"

# ── Scheduled Publishing ──────────────────────────────────────────
PUBLISH_MODE = "scheduled"
# ── 3-Phase Pipeline (v9) ─────────────────────────────────────────
GENERATION_LEAD_HOURS = 36    # Max hours ahead to generate
UPLOAD_WINDOW_START = 9       # Upload window: 9:00 AM
UPLOAD_WINDOW_END = 11        # Upload window: 11:00 AM
PUBLISH_TIMEZONE = "Europe/Madrid"
PUBLISH_JITTER_MIN = 20
PUBLISH_WARMUP_MIN = 120
# PUBLISH_TARGET_HOUR not set — niche heuristic auto-detects (historia_documental → 20:00)

YT_DEFAULT_TAGS = [
    # Tier 1: Primary keywords (broad match)
    "civilizaciones perdidas",
    "civilizaciones antiguas",
    "secretos de la historia",
    "historia antigua",
    "arqueología",
    # Tier 2: Named phenomena (high-intent search)
    "ciudades perdidas",
    "ruinas antiguas",
    "descubrimientos arqueológicos",
    "misterios históricos",
    "civilizaciones olvidadas",
    # Tier 3: Format tags
    "documental historia español",
    "video ensayo historia",
    "misterios sin resolver historia",
    "enigmas de la humanidad",
    "documental arqueología",
    # Tier 4: Long-tail / adjacent
    "templos antiguos",
    "culturas perdidas",
    "imperios desaparecidos",
    "historia universal documental",
    "los secretos del pasado",
]

# ═══════════════════════════════════════════════════════════════════
# SEO
# ═══════════════════════════════════════════════════════════════════

SEO_PRIMARY_KEYWORD = "civilizaciones antiguas documental"

SEO_SECONDARY_KEYWORDS = [
    # Core niche
    "civilizaciones perdidas",
    "secretos de la historia",
    "ciudades antiguas misterios",
    "civilizaciones olvidadas",
    "ruinas misteriosas del mundo",
    # Discoveries
    "descubrimientos arqueológicos recientes",
    "hallazgos que cambiaron la historia",
    "tumbas antiguas encontradas",
    "templos perdidos descubiertos",
    # Mysteries
    "misterios de la humanidad sin resolver",
    "enigmas históricos inexplicables",
    "secretos de las pirámides",
    "civilizaciones desaparecidas sin rastro",
    # Format / channel
    "documental historia español",
    "video ensayo arqueología",
    "mejores documentales historia antigua",
    "historias del pasado fascinantes",
    # Audience intent
    "datos curiosos de historia",
    "culturas antiguas del mundo",
    "imperios más poderosos de la historia",
    "lugares misteriosos del mundo",
    "historia para reflexionar",
]

SEO_HASHTAGS = [
    "#CivilizacionesOlvidadas",
    "#HistoriaAntigua",
    "#Arqueología",
    "#Misterios",
    "#SecretosDeLaHistoria",
    "#CivilizacionesPerdidas",
    "#Documental",
    "#Historia",
    "#Ruinas",
    "#Curiosidades",
    "#SabíasQue",
    "#Enigmas",
    "#CulturasAntiguas",
    "#MaravillasDelMundo",
    "#Descubrimientos",
]

SHORTS_ENABLED = True
SHORTS_PER_DAY = 3
SHORTS_MAX_CLIPS_PER_VIDEO = 5
SHORTS_CLIP_SCHEDULE = [
    {"offset_days": 1, "count": 1},
    {"offset_days": 3, "count": 1},
    {"offset_days": 5, "count": 1},
]

SHORTS_HASHTAGS = [
    "#CivilizacionesOlvidadas",
    "#Historia",
    "#SabíasQue",
    "#Shorts",
    "#Curiosidades",
    "#Arqueología",
    "#Misterios",
    "#CivilizacionesPerdidas",
    "#Secretos",
    "#RuinasAntiguas",
]

# ── Cross-promotion ──────────────────────────────────────────
# Link shorts to long-form videos for conversion funnel
SHORTS_LONGFORM_LINK_ENABLED = True
SHORTS_PLAYLIST_AUTO = True
SHORTS_FIRST_COMMENT_LINK = True
SHORTS_PER_VIDEO_PLAYLIST = True
SHORTS_PLAYLIST_NAME = "Shorts"

# ═══════════════════════════════════════════════════════════════════
# DESCRIPTION TEMPLATE
# ═══════════════════════════════════════════════════════════════════

DESCRIPTION_TEMPLATE = """🏛️ {titulo}
———

{descripcion_seo}

🔍 EN ESTE VIDEO
- El descubrimiento que cambió la historia
- La civilización que el tiempo olvidó
- Lo que la arqueología ha revelado (y lo que aún no puede explicar)
- El legado que nos dejaron

⏱️ CAPÍTULOS
{chapters}

———

🏛️ Bienvenido a **Civilizaciones Olvidadas** — el canal donde exploramos las civilizaciones perdidas, las ruinas antiguas y los secretos históricos que la humanidad ha dejado atrás. Desde Göbekli Tepe hasta los Mayas, desde el Valle del Indo hasta Angkor Wat.

📚 Fuentes: Wikipedia, artículos de arqueología, publicaciones científicas, hilos de Reddit (r/Archaeology, r/AncientCivilizations) y archivos históricos verificados.

🏺 Todo el contenido está basado en hechos históricos documentados.

🔔 Suscríbete y activa la campana para descubrir más secretos del pasado.

💬 ¿Qué civilización antigua te fascina más? Déjalo en los comentarios.

#CivilizacionesOlvidadas #HistoriaAntigua #Arqueología #Misterios"""

# ═══════════════════════════════════════════════════════════════════
# THUMBNAIL
# ═══════════════════════════════════════════════════════════════════

THUMBNAIL_WIDTH = 1280
THUMBNAIL_HEIGHT = 720
THUMBNAIL_FONT_SIZE = 56
THUMBNAIL_BORDER_WIDTH = 5

# ── Per-channel thumbnail customisation (v2.1) ────────────────
# Civilizaciones Olvidadas: ancient-temple aesthetic,
# golden terracotta palette, serif font, no 4K badge.
THUMBNAIL_FONT_FAMILY = "DejaVuSerif-Bold"      # serif → ancient inscription feel
THUMBNAIL_BORDER_COLOR = "#D4A843"              # golden terracotta
THUMBNAIL_SHOW_4K_BADGE = False                 # no modern badge for ancient channel
THUMBNAIL_TEXT_STROKE_WIDTH = 3                 # outline for readability on varied backgrounds
THUMBNAIL_TEXT_STROKE_COLOR = "#1A0F08"         # dark earth outline

# ── Per-channel visual style ──────────────────────────────────
THUMBNAIL_VISUAL_STYLE = "ancient_mystery"
THUMBNAIL_STYLE_OVERRIDE = True

THUMBNAIL_MANUAL_STYLE = {
    "visual_style": "ancient_mystery",
    "color_palette": {
        "primary": "#6B4C3B",
        "accent": "#D4A843",
        "text": "#D4A843",       # golden text (ancient inscription style)
        "shadow": "#1A0F08",     # dark earth shadow
    },
    "base_composition": "ruins_reveal",
    "effects": {
        "contrast_boost": 1.15,
        "saturation": 0.75,
        "vignette": 0.40,
    },
    "text_style": {
        "uppercase": False,
        "max_words": 5,
    },
    "pollo_prompt_suffix": (
        "ancient ruins, archaeological site, mysterious atmosphere, "
        "stone textures, golden hour lighting, dust motes in sunbeams, "
        "cinematic composition, 16:9 aspect ratio, warm earthy tones, "
        "professional archaeology photography"
    ),
}

THUMBNAIL_STYLE = {
    "layout": "image_full_background_text_overlay",
    "max_text_words": 4,
    "text_color": "warm_gold_on_dark_earth",
    "font_style": "bold_elegant_serif",
    "image_treatment": "ancient_ruins_golden_hour_mysterious",
    "background": "#1A0F08",
    "accent_color": "#D4A843",
    "face_policy": (
        "Ancient statues and faces from artifacts YES. Close-up of stone "
        "carvings, golden masks, sculptures. AI-generated faces: NO. "
        "Siluetas de exploradores, arqueólogos de espaldas contemplando "
        "ruinas OK. Manos tocando piedras antiguas, jeroglíficos OK."
    ),
    "number_preference": "odd_numbers_for_lists",
    "gold_accent_rule": (
        "Golden light ray or dust motes illuminating ruins boosts CTR. "
        "Ancient, solemn aesthetic. NEVER use neon or modern styling. "
        "NEVER use bright colors or white backgrounds for this channel."
    ),
}

THUMBNAIL_TEMPLATES = {
    "the_ruins": {
        "description": "Wide shot of ancient ruins at golden hour, sunbeams through columns",
        "text_position": "bottom_third_centered",
        "text_words": "2-3",
        "accent": "golden_dust_light_ray",
        "best_for": "Lost cities, temple discoveries, architectural marvels",
    },
    "the_artifact": {
        "description": "Close-up of mysterious artifact, stone carving, or golden mask",
        "text_position": "center_over_gradient",
        "text_words": "3-4",
        "accent": "spotlight_on_artifact",
        "best_for": "Mysterious objects, technological anomalies, forbidden artifacts",
    },
    "the_map": {
        "description": "Ancient map, parchment texture, compass rose, sepia tones",
        "text_position": "bottom_over_gradient",
        "text_words": "3-4",
        "accent": "magnifying_glass_or_compass",
        "best_for": "Lost cities, expeditions, geographic mysteries",
    },
}

# ═══════════════════════════════════════════════════════════════════
# VIDEO TIMING & MONETIZATION
# ═══════════════════════════════════════════════════════════════════

VIDEO_OPTIMAL_DURATION_MINUTES = 12

VIDEO_MIDROLL_STRATEGY = (
    "Colocar mid-rolls en pausas naturales entre capítulos de la "
    "expedición narrativa. NUNCA en medio del clímax arqueológico. "
    "Cada mid-roll debe preceder un mini-gancho: 'Pero lo que sigue "
    "a continuación es lo que ningún arqueólogo esperaba...'"
)

MONETIZATION_TARGET_CPM = "$8–$18 USD"

MONETIZATION_VERTICALS = [
    "Educación y aprendizaje",
    "Viajes y turismo cultural",
    "Libros y audiolibros",
    "Tecnología",
    "Inversión y finanzas",
]

# ═══════════════════════════════════════════════════════════════════
# END SCREEN
# ═══════════════════════════════════════════════════════════════════

END_SCREEN_STRATEGY = {
    "left_card": {
        "type": "playlist",
        "content": "most_relevant_playlist",
        "purpose": "Keep viewer exploring ancient worlds — archaeological rabbit hole",
    },
    "center": {
        "type": "subscribe",
        "purpose": "Convert explorer to subscriber",
    },
    "right_card": {
        "type": "video",
        "content": "most_recent_upload",
        "purpose": "Push newest expedition to engaged viewers",
    },
    "spoken_cta": (
        "Si este descubrimiento te dejó sin palabras, el siguiente "
        "en la lista es todavía más impactante. Te dejo el enlace en "
        "pantalla. Suscríbete si quieres seguir explorando las "
        "civilizaciones que la historia olvidó."
    ),
}

# ═══════════════════════════════════════════════════════════════════
# PLAYLISTS
# ═══════════════════════════════════════════════════════════════════

PLAYLISTS = [
    {
        "slug": "expediciones-completas",
        "name": "Expediciones Completas",
        "description": (
            "Documentales en profundidad sobre las civilizaciones perdidas, "
            "ciudades antiguas y descubrimientos arqueológicos más impactantes "
            "de la historia. Cada video: el hallazgo, la civilización, el "
            "misterio y el legado."
        ),
        "type": "main",
    },
    {
        "slug": "lo-mas-impactante",
        "name": "Lo Más Impactante",
        "description": (
            "Los 5 descubrimientos arqueológicos más asombrosos del canal. "
            "Si eres nuevo aquí, empieza por esta lista. Bienvenido a "
            "Civilizaciones Olvidadas."
        ),
        "type": "onboarding",
    },
    {
        "slug": "civilizaciones-perdidas",
        "name": "Civilizaciones Perdidas",
        "description": (
            "Sumerios, Mayas, Valle del Indo, Anasazi, Olmecas... Las "
            "grandes civilizaciones que desaparecieron dejando solo "
            "ruinas y preguntas sin respuesta."
        ),
        "type": "thematic",
    },
    {
        "slug": "misterios-arqueologicos",
        "name": "Misterios Arqueológicos",
        "description": (
            "Los enigmas que la arqueología aún no ha podido resolver. "
            "Göbekli Tepe, Líneas de Nazca, Mecanismo de Anticitera... "
            "artefactos que desafían la historia oficial."
        ),
        "type": "thematic",
    },
    {
        "slug": "maravillas-del-mundo",
        "name": "Maravillas del Mundo Antiguo",
        "description": (
            "Petra, Angkor Wat, Machu Picchu, Stonehenge, Pirámides de "
            "Giza... Las construcciones más impresionantes de la humanidad "
            "y los secretos que esconden."
        ),
        "type": "thematic",
    },
]

# ═══════════════════════════════════════════════════════════════════
# FIRST 48 HOURS STRATEGY
# ═══════════════════════════════════════════════════════════════════

FIRST_48H_STRATEGY = {
    "pre_upload_24h": [
        "Community Tab poll: '¿Qué civilización antigua te fascina más?'",
        "YouTube Story: imagen de ruinas + 'Mañana. 9PM MX. Un secreto de hace 5.000 años.'",
    ],
    "hour_0": [
        "Publish at 9PM Mexico City time (peak curiosity consumption window)",
        "First comment (immediate, pinned): pregunta para disparar debate histórico",
    ],
    "hours_1_6": [
        "Reddit r/Archaeology + r/AlternativeHistory: TEXT post with key discovery",
        "Facebook groups: Historia Antigua, Arqueología, Misterios del Mundo",
    ],
    "hours_6_24": [
        "Reply to EVERY comment in first 24h (3x algorithm weight on engagement)",
        "Twitter/X thread: 5-7 tweets narrando el descubrimiento, final tweet = YouTube link",
    ],
    "hours_24_48": [
        "Analyze CTR and retention in YouTube Studio",
        "If CTR < 5%: swap thumbnail variant (artifact closeup vs ruins wide shot)",
        "Second Community Tab: cerrar el loop de la encuesta pre-upload",
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
            "example": "¿Cuál de estos misterios arqueológicos te parece más fascinante?",
            "options": ["Göbekli Tepe", "Líneas de Nazca", "Pirámides de Egipto", "Stonehenge"],
        },
        "wednesday": {
            "type": "image_fact",
            "example": (
                "Fotografía de ruinas + 'Esta ciudad tenía 200.000 habitantes "
                "cuando Londres era un pueblo de 15.000. ¿Cómo desapareció?'"
            ),
        },
        "friday": {
            "type": "teaser",
            "example": (
                "Este sábado: el descubrimiento arqueológico que cambió todo "
                "lo que sabíamos sobre las primeras civilizaciones. Activa la campana."
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
            "En {año}, un {persona} encontró {descubrimiento} "
            "que cambió la historia para siempre."
        ),
        "structure": "Hook → El descubrimiento (20s) → El misterio (15s) → 'Video completo en YouTube, link en bio' (10s)",
        "cadence": "3x/day from one long-form video (days 1, 3, 5)",
    },
    "youtube_shorts": {
        "format": "15-30s most incredible discovery moment",
        "end_cta": "'Video completo en el canal' linked to long-form",
        "purpose": "Shorts feed → channel page → long-form viewer conversion",
    },
    "twitter_x": {
        "format": "Thread — 1 discovery = 1 thread per week",
        "template": "'HOY en Civilizaciones Olvidadas: La civilización que...' + 5-7 tweets + link",
    },
    "spotify_podcast": {
        "format": "Audio-only export of each video",
        "title_format": "Civilizaciones Olvidadas | {story_title} | {key_discovery}",
        "purpose": "Minimal effort, massive discovery platform for commuters",
    },
}

# ═══════════════════════════════════════════════════════════════════
# COLLABORATION TARGETS
# ═══════════════════════════════════════════════════════════════════

COLLABORATION_TARGETS = {
    "tier_1_direct": [
        {"name": "Pero eso es otra Historia", "niche": "Historia y mitología, audiencia masiva"},
        {"name": "Descifrando la Historia", "niche": "Misterios históricos y conspiraciones"},
        {"name": "Agujeros de Guion", "niche": "Curiosidades históricas y cinematográficas"},
        {"name": "Bully Magnets", "niche": "Historia visual de alta calidad, narrativa similar"},
    ],
    "tier_2_adjacent": [
        {"name": "Mundo Desconocido", "niche": "Misterios, civilizaciones — enorme audiencia LATAM"},
        {"name": "VM Granmisterio", "niche": "Conspiración y misterio, audiencia afín"},
    ],
    "collab_formats": [
        "React: 'Un historiador reacciona a Civilizaciones Olvidadas' — cross-promote",
        "Topic trade: cubrir una civilización sugerida por otro creador, cross-promote",
        "Mention strategy: 'Como dijo [Creator] en su video...' — goodwill",
    ],
}

# ═══════════════════════════════════════════════════════════════════
# TRENDING TOPIC HOOKS
# ═══════════════════════════════════════════════════════════════════

TRENDING_TOPIC_HOOKS = {
    "type_a_news": {
        "trigger": "Noticia de nuevo descubrimiento arqueológico o hallazgo histórico",
        "pivot": (
            "Esto que acaban de encontrar en {country}... la historia tiene "
            "docenas de hallazgos igual de inexplicables."
        ),
    },
    "type_b_anniversary": {
        "trigger": "Aniversario de grandes descubrimientos arqueológicos",
        "calendar": {
            "july": "Descubrimiento de Machu Picchu (24 julio 1911)",
            "november": "Tutankamón (4 noviembre 1922)",
            "december": "Göbekli Tepe y misterios del origen de la civilización",
        },
    },
    "type_c_pop_culture": {
        "trigger": "Estreno de película/serie sobre civilizaciones antiguas",
        "strategy": "'La historia REAL detrás de {show/movie}'",
        "examples": "Indiana Jones, La Momia, Apocalypto, Ancient Apocalypse → realidad vs ficción",
    },
    "type_d_calendar": {
        "name": "Calendario de las Civilizaciones",
        "months": {
            "january": "Civilizaciones del hielo y la prehistoria",
            "march": "Equinoccio — templos alineados astronómicamente",
            "june": "Solsticio — Stonehenge, pirámides y alineaciones solares",
            "september": "Equinoccio otoñal — cosechas y rituales antiguos",
            "october": "Civilizaciones malditas y ciudades fantasma (Halloween)",
            "december": "Solsticio de invierno — mitos del origen, calendarios antiguos",
        },
    },
}

# ═══════════════════════════════════════════════════════════════════
# CONTENT PILLARS
# ═══════════════════════════════════════════════════════════════════

CONTENT_PILLARS = [
    {
        "name": "La Civilización",
        "ratio": 55,
        "desc": "Documental profundo de una civilización individual",
    },
    {
        "name": "Listas y Descubrimientos",
        "ratio": 30,
        "desc": "Compilación temática: '5 civilizaciones que desaparecieron sin rastro'",
    },
    {
        "name": "El Enigma",
        "ratio": 15,
        "desc": "Video breve sobre un misterio arqueológico concreto",
    },
]

# ═══════════════════════════════════════════════════════════════════
# SEASON 1 EPISODE PLAN
# ═══════════════════════════════════════════════════════════════════

SEASON1_EPISODES = [
    {
        "ep": 1,
        "title": "Göbekli Tepe: El Templo que Reescribe la Historia",
        "civilization": "Cultura Neolítica Pre-Cerámica",
        "hook": "Este templo fue construido 7.000 años antes que las pirámides de Egipto. Cambió todo lo que creíamos saber sobre el origen de la civilización.",
    },
    {
        "ep": 2,
        "title": "El Valle del Indo: La Civilización más Avanzada que Desapareció Sin Dejar Rastro",
        "civilization": "Civilización del Valle del Indo (Harappa y Mohenjo-Daro)",
        "hook": "Tenían alcantarillado hace 5.000 años mientras Europa vivía en chozas. Un día, desaparecieron. Nadie sabe por qué.",
    },
    {
        "ep": 3,
        "title": "Los Anasazi: El Pueblo que Desapareció de los Acantilados",
        "civilization": "Anasazi / Pueblos Ancestrales",
        "hook": "Construyeron palacios en acantilados imposibles. Luego, en menos de una generación, 30.000 personas se esfumaron.",
    },
    {
        "ep": 4,
        "title": "Los Olmecas: Las Cabezas Colosales que Nadie Puede Explicar",
        "civilization": "Civilización Olmeca",
        "hook": "Escondidas en la jungla mexicana, estas cabezas de piedra de 40 toneladas fueron transportadas sin ayuda de animales de carga. ¿Cómo lo hicieron?",
    },
    {
        "ep": 5,
        "title": "Angkor Wat: La Metrópolis que la Jungla Devoró",
        "civilization": "Imperio Jemer",
        "hook": "Fue la ciudad más grande del mundo preindustrial. Un millón de personas. La jungla se la tragó en silencio durante 400 años.",
    },
    {
        "ep": 6,
        "title": "Petra: La Ciudad Esculpida en Piedra que el Mundo Olvidó",
        "civilization": "Nabateos",
        "hook": "Esculpieron una ciudad entera en la roca del desierto. Controlaron las rutas del incienso. Y luego... silencio.",
    },
    {
        "ep": 7,
        "title": "Los Mayas: El Colapso que Nadie Puede Explicar",
        "civilization": "Civilización Maya",
        "hook": "Ciudades de 100.000 habitantes, matemáticas avanzadas, calendarios precisos. Y un día, simplemente se fueron. 10 millones de personas... ¿adónde?",
    },
    {
        "ep": 8,
        "title": "La Isla de Pascua: El Misterio de los Moáis",
        "civilization": "Cultura Rapa Nui",
        "hook": "La isla más remota del planeta. 900 estatuas de 80 toneladas. Nadie sabe exactamente cómo las movieron... y por qué dejaron de construirlas.",
    },
    {
        "ep": 9,
        "title": "Las Líneas de Nazca: Mensajes para los Dioses",
        "civilization": "Cultura Nazca",
        "hook": "Solo se ven desde el cielo. Animales de cientos de metros dibujados en el desierto hace 2.000 años. ¿Para quién eran?",
    },
    {
        "ep": 10,
        "title": "Los 5 Descubrimientos Arqueológicos que Cambiaron la Historia",
        "civilization": "Compilación (Tutankamón, Troya, Machu Picchu, Terracota, Rosetta)",
        "hook": "Estos hallazgos demostraron que todo lo que creíamos saber sobre la historia antigua... era mentira.",
    },
]

# ═══════════════════════════════════════════════════════════════════
# VIRAL MIRROR
# ═══════════════════════════════════════════════════════════════════
VIRAL_ENABLED = True
VIRAL_MAX_AGE_DAYS = 29  # Max days since publication (videos older are discarded)

NICHE_KEYWORDS_ENG = [
    "lost civilizations",
    "ancient mysteries",
    "forgotten civilizations",
    "ancient technology documentary",
    "archaeological discoveries",
    "ancient ruins unexplained",
    "lost cities found",
    "mysterious archaeological sites",
    "ancient artifacts unexplained",
    "hidden history documentary",
    "ancient civilizations documentary",
    "prehistoric discoveries",
]

VIRAL_PLAYLIST_KEYWORDS = {
    "expediciones-completas": [
        "lost civilizations full documentary",
        "ancient cities discovered",
        "archaeological expedition documentary",
        "forgotten history documentary",
    ],
    "lo-mas-impactante": [
        "most amazing archaeological discoveries",
        "unbelievable ancient technology",
        "discoveries that changed history",
        "most mysterious ancient artifacts",
    ],
    "civilizaciones-perdidas": [
        "vanished civilizations documentary",
        "sumerian mesopotamia documentary",
        "mysterious ancient civilizations",
        "advanced prehistoric civilizations",
    ],
    "misterios-arqueologicos": [
        "archaeological mysteries unsolved",
        "ancient artifacts scientists can't explain",
        "impossible ancient structures",
        "out of place artifacts documentary",
    ],
    "maravillas-del-mundo": [
        "ancient wonders of the world documentary",
        "greatest archaeological sites",
        "amazing ancient monuments",
        "forgotten temples documentary",
    ],
}
