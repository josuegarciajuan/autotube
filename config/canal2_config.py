from config.settings import DEFAULT_VIDEO_PROVIDERS, DEFAULT_VIDEO_FALLBACK_QUERIES

"""Configuration for Canal 2: Sincronías (Milagros y Casualidades).

Meta-niche: "historias reales de milagros, casualidades imposibles
y fenómenos que la ciencia aún no puede explicar"

Formato: video-essay documental, 8-14 min, narrado con imágenes
cinematográficas cálidas y atmosféricas.

Estilo: "documental de asombro" — archivos de lo inexplicable.
"""

# ═══════════════════════════════════════════════════════════════════
# IDENTITY
# ═══════════════════════════════════════════════════════════════════

CANAL_NAME = "canal2"
CANAL_DISPLAY_NAME = "Sincronías"
CANAL_TAGLINE = (
    "Historias reales de milagros, casualidades imposibles "
    "y fenómenos que la ciencia aún no puede explicar."
)
CANAL_OUTRO_TAGLINE = (
    "La realidad siempre supera la ficción. Y esto que acabas de ver es real."
)

# ── YouTube Handle ───────────────────────────────────────────
YOUTUBE_HANDLE = "@cleanthelistemaillistclean7103"
YOUTUBE_CHANNEL_URL = "https://www.youtube.com/@cleanthelistemaillistclean7103"

# ── Narrative Style ─────────────────────────────────────────────
CANAL_NARRATIVE_STYLE = "documental de asombro"
CANAL_STYLE_DESCRIPTION = (
    "Historias extraordinarias que desafían toda explicación. "
    "Coincidencias imposibles, milagros documentados, fenómenos que "
    "la ciencia aún no entiende. El formato sin rostro permite que "
    "las imágenes hablen por sí solas — estas historias no necesitan presentador."
)

# ── Channel About Section (indexado por YouTube search) ─────────
CHANNEL_ABOUT_SECTION = """Bienvenido a Sincronías.

Exploramos las casualidades más increíbles, los milagros mejor documentados y los fenómenos inexplicables que desafían toda lógica. Historias reales de personas que estuvieron en el lugar exacto en el momento exacto... y lo que la ciencia todavía intenta explicar.

🎬 Formato: video ensayos documentales (8-14 minutos)
🗓️ Nuevas historias: cada semana
🎙️ Narración documental con fuentes verificadas

📩 Contacto: {email}

✨ Si te fascinan las casualidades imposibles, los milagros modernos, los sucesos inexplicables y las historias que te dejan sin palabras... este canal es para ti.

Suscríbete y activa la campana para no perderte ninguna historia que desafía lo imposible."""

# ── Channel Keywords (YouTube Studio → Settings → Channel) ──────
CHANNEL_KEYWORDS = [
    "casualidades imposibles",
    "milagros reales",
    "fenómenos inexplicables",
    "historias increíbles reales",
    "sincronías",
    "destino",
    "coincidencias sorprendentes",
    "historias que desafían la lógica",
    "milagros modernos",
    "sucesos paranormales",
    "predicciones cumplidas",
    "experiencias inexplicables",
    "documental misterio",
    "historias reales impactantes",
    "sucesos inexplicables",
    "casualidades del destino",
    "lo inexplicable",
    "misterios reales",
    "documental en español",
    "historias que inspiran",
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

PROD_SCRIPT_WORDS_MIN = 2000
PROD_SCRIPT_WORDS_MAX = 3500
PROD_SCRIPT_SCENES_MIN = 10
PROD_SCRIPT_SCENES_MAX = 18
PROD_SCRIPT_BLOCKS_MIN = 10
PROD_SCRIPT_BLOCKS_MAX = 18
PROD_VIDEO_DURATION_MIN = 10
PROD_VIDEO_DURATION_MAX = 14

# ── Scene duration enforcement (overrides video_editor defaults) ──
# Scenes shorter than MIN are merged with neighbors.
# Scenes longer than MAX are split into sub-scenes (each with its own media asset).
# ─ Jul 2026: raised MIN from 3→8, MAX from 10→16 for faster generation.
SCENE_DURATION_MIN = 8.0
SCENE_DURATION_MAX = 16.0

# ── Hard cap: maximum accumulated duration for any composited clip ──
# When a clip has been extended (via scene merges) beyond this limit,
# the next scene with no media forces a NEW clip from the fallback pool
# instead of further extending the same image. Prevents "same image for 200s".
# ─ Jul 2026: raised from 15→16 to match SCENE_DURATION_MAX.
MAX_CLIP_EXTEND_SEC = 16.0

# ── Image reuse: STRICTLY FORBIDDEN (Jul 2026) ──
# Images are NEVER reused within the same video to prevent YouTube
# demonetization from duplicate frames. The video_editor now blocks
# any image path/content_hash already seen in the current build.
# Old config key (deprecated — no longer read):
# IMAGE_REUSE_MIN_GAP = 2

# ── Average video duration target (approx, in minutes) ──
# These are the single source of truth for production — read via the
# panel "Duración — Objetivo" and used by _get_word_target().
VIDEO_AVERAGE_DURATION_MIN = 12
VIDEO_DURATION_DISCREPANCY_MIN = 2

# ═══════════════════════════════════════════════════════════════════
# NARRATIVE TONE
# ═══════════════════════════════════════════════════════════════════

CANAL_TONE = (
    "Cálido, envolvente y profundamente curioso. Narrativa documental "
    "que oscila entre el asombro científico y la emoción humana. "
    "Riguroso en los hechos, luminoso en la atmósfera, íntimo en "
    "la narración. Como un documental de National Geographic sobre "
    "lo inexplicable. El espectador debe sentir que está descubriendo "
    "algo maravilloso, no que le están dando una lección."
)

# ═══════════════════════════════════════════════════════════════════
# TARGET AUDIENCE
# ═══════════════════════════════════════════════════════════════════

TARGET_AUDIENCE = (
    "18-45 años (amplio), LATAM (MX 35%, CO 20%, AR 15%, PE 10%, ES 10%, otros 10%). "
    "Curiosos, mente abierta, interés en lo inexplicable, espiritualidad sin dogma, "
    "ciencia y misterio. 55% mujeres / 45% hombres. "
    "60%+ mobile. Sesiones de 8-12 min. Pico de consumo: 20:00-00:00 local."
)

TARGET_AUDIENCE_PSYCHOGRAPHIC = {
    "The Wonder Seeker": (
        "Busca contenido que le haga sentir asombro y maravilla. "
        "Quiere creer que el universo es más misterioso de lo que parece."
    ),
    "The Hopeful": (
        "Ve estas historias porque necesita inspiración y esperanza. "
        "Los milagros y casualidades le recuerdan que todo es posible."
    ),
    "The Skeptic": (
        "Entra para desmentir, se queda por la calidad de los hechos. "
        "Comparte para debatir: 'esto no puede ser cierto... ¿o sí?'"
    ),
    "The Spiritual": (
        "Busca señales de que hay algo más grande. "
        "Las sincronías y milagros validan su visión del mundo."
    ),
}

# ═══════════════════════════════════════════════════════════════════
# TITLE OPTIMIZATION
# ═══════════════════════════════════════════════════════════════════

TITLE_FORMULAS = [
    # Pattern 1: Impossible Coincidence + Hook
    "La Casualidad que {action} y Nadie Puede Explicar",
    # Pattern 2: Named Miracle + Shocking Detail
    "El Milagro de {name}: {shocking_fact}",
    # Pattern 3: Prediction That Came True
    "Predijo {event} Años Antes de que Ocurriera",
    # Pattern 4: Question + Wonder
    "¿{question}? La Respuesta Desafía Toda Lógica",
    # Pattern 5: When the Universe Conspired
    "Cuando el Universo Conspiró: La Increíble Historia de {name}",
    # Pattern 6: Seconds That Changed Everything
    "{number} Segundos que Desafiaron las Leyes de la Física",
    # Pattern 7: The Sincrony
    "La Sincronía Más Increíble de la Historia te Dejará Sin Palabras",
]

TITLE_POWER_WORDS = [
    # ⚡ URGENCIA / EXCLUSIVIDAD
    "revelado", "filtrado", "censurado", "inédito", "clasificado",
    "confidencial", "prohibido", "exclusiva", "urgente", "limitado",
    "desclasificado", "archivado", "ocultado", "silenciado", "suprimido",
    "enterrado", "sellado", "bloqueado",
    # 💥 IMPACTO EMOCIONAL
    "escalofriante", "desgarrador", "inexplicable", "demoledor",
    "sobrecogedor", "estremecedor", "alucinante", "aterrador",
    "conmovedor", "inspirador", "revelador", "imposible",
    "extraordinario", "asombroso", "fascinante", "impactante",
    "devastador", "magnético", "irresistible", "sobrehumano",
    "desconcertante", "perturbador", "increíble", "insólito",
    # 🔍 CURIOSIDAD / MISTERIO
    "oculto", "secreto", "siniestro", "enigmático", "misterio",
    "enigma", "intriga", "sorprendente", "desconocido",
    "inexplorado", "indescifrable", "inquietante",
    # Authority / Verification
    "demostrado", "documentado", "real", "comprobado", "verificado",
    "confirmado", "científico", "probado",
    # Spiritual / Destiny (canal2 specific)
    "milagro", "destino", "sincronía", "casualidad", "profecía",
    "sobrevivió", "regresó", "salvó", "predijo", "anticipó",
    "señal", "coincidencia", "universo", "cósmico", "divino",
    "transformación", "despertar", "conexión", "energía",
    "revelación", "visionario", "premonición", "presagio",
    # Unexpected / Twist
    "inesperado", "impensable", "improbable", "insospechado",
]

TITLE_MAX_CHARS = 65

# ═══════════════════════════════════════════════════════════════════
# SCRIPT STRUCTURE — "Espiral de Asombro" method
# ═══════════════════════════════════════════════════════════════════

SCRIPT_HOOK_RULE = (
    "ATENCIÓN: La primera frase del guion DEBE ser el hecho más "
    "impactante de la historia, con un NÚMERO y un HECHO CONCRETO. "
    "NUNCA empezar con contexto histórico, definiciones, ni presentaciones. "
    "NUNCA 'Hola, bienvenidos a...' ni 'En este video vamos a hablar de...'.\n\n"
    "EJEMPLO CORRECTO: 'El 4 de diciembre de 1971, 300 personas se salvaron "
    "de morir porque una mujer soñó con un accidente aéreo tres días antes.'\n"
    "EJEMPLO INCORRECTO: 'Las casualidades son eventos que ocurren sin una "
    "causa aparente y han fascinado a la humanidad durante siglos...'.\n\n"
    "RETENCIÓN: Justo DESPUÉS del primer minuto del guion, DEBES incluir una "
    "frase de retención explícita que invite al espectador a quedarse hasta "
    "el final del video. Debe ser CONTEXTUAL y DISTINTA en cada guion. "
    "Ejemplos orientativos (NO los copies, el LLM debe generar uno nuevo): "
    "'Si quieres saber qué pasó después, quédate hasta el final', "
    "'Si quieres conocer cómo termina esta historia, no te vayas', "
    "'Quédate, porque lo que viene a continuación es aún más increíble'. "
    "Esta frase debe colocarse inmediatamente después de enumerar lo que el "
    "espectador va a descubrir, como cierre de la introducción."
)

# 7-step structure with retention anchors
SCRIPT_STRUCTURE = [
    {
        "step": "EL GANCHO",
        "time_pct": "0-10%",
        "description": (
            "El hecho más impactante en frío. Sin contexto. Imagen: la más "
            "evocadora de la historia. Cerrar con promesa: 'Al final de "
            "este video vas a entender por qué la ciencia sigue sin explicarlo.'"
        ),
    },
    {
        "step": "EL CONTEXTO",
        "time_pct": "10-20%",
        "description": (
            "Lo que se creía imposible antes de este suceso. Construir "
            "anticipación: 'Las probabilidades de que esto ocurriera "
            "eran de una entre un millón. Pero ocurrió.'"
        ),
    },
    {
        "step": "LOS PROTAGONISTAS",
        "time_pct": "20-30%",
        "description": (
            "Las personas reales detrás de la historia. Gente normal, "
            "vidas normales, hasta que ocurrió lo imposible. Humanizar "
            "para que el espectador se identifique."
        ),
        "retention_anchor": (
            "CLIFFHANGER al 25%: 'Pero lo que esta persona no sabía... "
            "es que en exactamente 72 horas, su vida cambiaría para siempre.'"
        ),
    },
    {
        "step": "EL SUCESO",
        "time_pct": "30-55%",
        "description": (
            "El milagro, la coincidencia o el fenómeno paso a paso. "
            "Escalar el asombro. 'Y entonces ocurrió algo que desafía "
            "todo lo que creemos saber.'"
        ),
        "retention_anchor": (
            "CLIFFHANGER al 50%: Silencio 2s. Cambio de imagen. "
            "'Recapitulemos: [1 frase]. Ahora viene lo más increíble.'"
        ),
    },
    {
        "step": "EL MOMENTO CUMBRE",
        "time_pct": "55-70%",
        "description": (
            "El instante exacto de lo inexplicable. Peak de asombro. "
            "Música crece. Luz. Emoción. El momento que deja sin palabras."
        ),
    },
    {
        "step": "LAS CONSECUENCIAS",
        "time_pct": "70-85%",
        "description": (
            "Cómo cambió sus vidas. Qué dice la ciencia (si puede decir algo). "
            "Testimonios, documentos, evidencias. 'Nadie volvió a ser el mismo.'"
        ),
        "retention_anchor": (
            "EL ESPEJO al 70%: Dirigirse directamente al viewer. "
            "'Ahora piensa en tu vida. En esas coincidencias que has ignorado. "
            "En esa vez que algo te salvó sin explicación.' Hacerlo personal."
        ),
    },
    {
        "step": "EL CIERRE",
        "time_pct": "85-100%",
        "description": (
            "Reflexión: qué nos dice esto sobre el universo, el destino, "
            "o simplemente sobre lo mucho que nos queda por entender. "
            "Pregunta al viewer. End hook + CTA."
        ),
    },
]

SCRIPT_END_HOOK = (
    "Y si crees que esta historia es increíble, espera a ver {next_story}. "
    "Porque lo que le ocurrió a {next_protagonist} es todavía más "
    "inexplicable. Ese es el próximo video. Dale like, suscríbete y activa la campana."
)

SCRIPT_EMOTIONAL_ARC = {
    "0-10%": "asombro",
    "10-20%": "curiosidad",
    "20-30%": "empatía",
    "30-45%": "intriga",
    "45-55%": "anticipación",
    "55-65%": "estupefacción",
    "65-75%": "esperanza → inspiración",
    "75-85%": "reflexión",
    "85-95%": "gratitud",
    "95-100%": "maravilla",
}

# Retention anchors
RETENTION_ANCHORS = {
    "at_25_pct": {
        "trigger": "cliffhanger_mid_video",
        "action": (
            "Insertar mini-cliffhanger: 'Pero lo que ocurrió 3 días después "
            "cambió todo.' Tratar el video como capítulos, no como ensayo."
        ),
    },
    "at_50_pct": {
        "trigger": "the_reset",
        "action": (
            "Música fuera 2s. Nueva imagen o fotografía real. Voz en off: "
            "'Recapitulemos: [resumen 1 frase]. Ahora viene lo más increíble.'"
        ),
    },
    "at_70_pct": {
        "trigger": "the_mirror",
        "action": (
            "Dirigirse al viewer directamente. Hacerlo personal: 'Ahora "
            "piensa en tu vida. ¿Cuántas coincidencias has ignorado?' "
            "Cerrar con teaser: 'En 60 segundos te cuento por qué esto importa.'"
        ),
    },
}

# ═══════════════════════════════════════════════════════════════════
# VIRALITY TRIGGERS
# ═══════════════════════════════════════════════════════════════════

VIRALITY_TRIGGERS = [
    {
        "name": "Awe & Wonder",
        "mechanism": (
            "'No vas a creer lo que pasó.' El asombro genuino es la emoción "
            "que más se comparte en redes sociales. La gente comparte para "
            "provocar la misma reacción en otros."
        ),
    },
    {
        "name": "Emotional Uplift",
        "mechanism": (
            "Contenido que hace sentir bien, da esperanza, inspira. "
            "La gente guarda y reenvía lo que la eleva emocionalmente. "
            "Historias de milagros = altísima tasa de reenvío."
        ),
    },
    {
        "name": "Conversation Starter",
        "mechanism": (
            "Cerrar con pregunta existencial: '¿Crees en las casualidades "
            "o en el destino?' Comentarios = señal de engagement = más "
            "impresiones = más shares. El debate crea comunidad."
        ),
    },
    {
        "name": "Identity Signaling",
        "mechanism": (
            "Compartir esta historia dice: 'Yo tengo la mente abierta.' "
            "o 'Yo sé cosas que la ciencia aún no explica.' "
            "Enmarcar el contenido como conocimiento especial."
        ),
    },
    {
        "name": "Hope Trigger",
        "mechanism": (
            "Cada guion debe incluir un momento de esperanza genuina. "
            "'Si esto pudo pasarle a esta persona, tal vez...' "
            "La esperanza es la emoción más compartible en WhatsApp."
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
    "rate_base": "-10%",         # slower, ~150 WPM documentary pace
    "pitch_base": "+2Hz",        # slightly warmer

    # ── Hook (opening impact) ────────────────────────────
    "rate_hook": "-5%",          # slightly faster than base for hook energy
    "pitch_hook": "+0Hz",

    # ── Desarrollo (body — neutral storytelling) ────────
    "rate_desarrollo": "-8%",
    "pitch_desarrollo": "+2Hz",

    # ── Climax (emotional peak of wonder) ───────────────
    "rate_climax": "-15%",       # slowest — awe and impact
    "pitch_climax": "-4Hz",      # slight gravity for importance

    # ── Reflexion (contemplative) ───────────────────────
    "rate_reflexion": "-10%",
    "pitch_reflexion": "+2Hz",

    # ── Cierre (closing call-to-action) ──────────────────
    "rate_cierre": "-5%",
    "pitch_cierre": "+4Hz",      # warmth for CTA
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
# Supported engines: "edgetts" (Microsoft Edge TTS, free, cloud)
#                     "kokoro"  (Kokoro-82M, local, Apache 2.0)

TTS_ENGINE = "kokoro"

# ── Kokoro configuration ─────────────────────────────────────
# Voice: ef_dora (female), em_alex (male), em_santa (male)
KOKORO_VOICE = "em_santa"

# Block speed multipliers (1.0 = normal). Each block type gets
# a different speed to create emotional dynamics. Lower = slower,
# more dramatic. Higher = faster, more energetic.
KOKORO_BLOCK_SPEEDS = {
    "hook": 0.85,        # antes 1.06 — más lento para que el hook impacte sin atropellar
    "desarrollo": 0.80,  # antes 0.94 — -20% más lento, narración pausada y clara
    "climax": 0.75,      # antes 0.87 — máxima tensión, muy lento y dramático
    "reflexion": 0.78,   # antes 0.92 — contemplativo, ritmo relajado
    "cierre": 0.85,      # antes 0.98 — conclusión pausada y cálida
}

# Silence inserted between narrative blocks (seconds)
KOKORO_PAUSE_BETWEEN_BLOCKS = 0.9  # antes 0.7 — pausas más largas entre bloques

# ═══════════════════════════════════════════════════════════════════
# CONTENT SOURCES
# ═══════════════════════════════════════════════════════════════════

REDDIT_SUBREDDITS = [
    # Unexplained phenomena / wonder
    "Glitch_in_the_Matrix",
    "nevertellmetheodds",
    "HighStrangeness",
    "Thetruthishere",
    "Synchronicities",
    "paranormal",
    "Unexplained",
    # Inspiring human stories
    "HumanPorn",
    "Damnthatsinteresting",
    "interestingasfuck",
    "todayilearned",
    "Coincidence",
    # Dreams / NDE / premonitions
    "Dreams",
    "NDE",
    "precognition",
    # General viral
    "TrueReddit",
    "AskReddit",
]

REDDIT_SORT = "top"
REDDIT_TIME = "month"
REDDIT_LIMIT = 25

WIKIPEDIA_CATEGORIES = [
    # English — primary research
    "Coincidences",
    "Miracle",
    "Parapsychology",
    "Synchronicity",
    "Prophecy",
    "List of people who disappeared mysteriously",
    "Unexplained phenomena",
    "Forteana",
    "Unusual articles",
    "Paranormal terminology",
    "Spiritualism",
    "Near-death experiences",
    "Survival",
    "Luck",
    "Precognition",
    "Guardian angels",
    # Spanish Wikipedia
    "Fenómenos paranormales",
    "Milagros",
    "Coincidencias",
    "Profecías",
    "Sincronicidad",
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

# Atlas Obscura categories for Sincronías
ATLAS_OBSCURA_CATEGORIES = ["wonders", "history", "unique"]

# RSS feeds for Sincronías
RSS_FEEDS = []

# Google News queries for Sincronías
GOOGLE_NEWS_QUERIES = [
    "sincronicidades historias reales",
    "milagros inexplicables",
    "casualidades increíbles",
    "coincidencias misteriosas",
]
GOOGLE_NEWS_LANGUAGE = "es"
GOOGLE_NEWS_COUNTRY = "ES"

# ═══════════════════════════════════════════════════════════════════
# VISUAL STYLE
# ═══════════════════════════════════════════════════════════════════

IMAGE_STYLE_MODIFIERS = (
    "warm cinematic photography, golden hour lighting, atmospheric, 16:9, "
    "professional photography, hopeful luminous mood, ethereal light rays, "
    "soft focus, inspiring"
)

COLOR_PALETTE = {
    "primary": (212, 175, 55),        # warm gold / amber
    "secondary": (15, 32, 62),        # deep warm indigo
    "accent": (200, 120, 80),         # soft terracotta / coral
    "text": (245, 240, 230),          # warm cream off-white
    "text_shadow": (8, 6, 4),         # subtle warm shadow
    "tertiary": (40, 35, 30),         # deep warm brown (safe background)
    "warning": (230, 180, 30),        # bright gold for thumbnail CTR accent
}

FILM_GRAIN_OPACITY = 5              # lighter grain for luminous feel
FILM_GRAIN_FRAMES = 8
KEN_BURNS_ZOOM_MIN = 4
KEN_BURNS_ZOOM_MAX = 12    # antes 8 — zoom más pronunciado para efecto cinematico visible

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
    "max_video_blocks_pct": 80,
    "target_video_pct": 80,            # governor target: ~80% video scenes (antes 60)
    "max_placeholder_pct": 0,          # governor: 0% placeholder scenes tolerated
    "video_fallback_to_image": True,
    "video_min_duration": 4,
    "video_max_duration": 20,
    "video_sources": ["pexels"],
    "video_providers": DEFAULT_VIDEO_PROVIDERS,
    "video_fallback_queries": DEFAULT_VIDEO_FALLBACK_QUERIES,
    "fallback_query": "warm golden hour cinematic atmosphere light rays 16:9",
    "fallback_query_simple": "warm atmospheric hopeful cinematic",
    "ken_burns_zoom_min": 4,
    "ken_burns_zoom_max": 12,
    "crossfade_min": 0.3,
    "crossfade_max": 0.7,
    # ── Pollo AI fallback ─────────────────────────────────────────
    "ai_image_fallback": True,           # enable Pollo AI when stock fails
    "ai_max_per_video": 5,               # hard cap: max 5 Pollo gen/video
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
INTRO_BG_COLOR = (15, 32, 62)     # deep indigo
OUTRO_DURATION_SEC = 5.0
OUTRO_FONT_SIZE = 60
OUTRO_BG_COLOR = (15, 32, 62)
OUTRO_TEXT = "Suscríbete"
OUTRO_CTA_LIKE = "👍 Like"
OUTRO_CTA_SUBSCRIBE = "❤️ Suscríbete"
OUTRO_CTA_BELL = "📢 Comparte"
# ── CTA visual text (shown on screen during the CTA segment) ──
# Single CTA (used when template is pre-generated)
CTA_TEXT = (
    "Si has llegado hasta aquí y esta historia te ha resonado,\n"
    "suscríbete y dale like\n"
    "para que más almas encuentren las señales del universo."
)
# Variants for visual CTA rotation (randomly picked per video)
CTA_TEXT_VARIANTS = [
    (
        "Si has llegado hasta aquí y esta historia te ha resonado,\n"
        "suscríbete y dale like\n"
        "para que más almas encuentren las señales del universo."
    ),
    (
        "Gracias por acompañarnos hasta el final de esta sincronía.\n"
        "Suscríbete y dale like,\n"
        "que la próxima señal ya está vibrando en el éter."
    ),
    (
        "El universo ha hablado y tú has escuchado.\n"
        "Suscríbete, dale like y comparte:\n"
        "estas historias necesitan llegar a más almas."
    ),
]
# ── Template voice-over texts (TTS with channel narrator voice) ──
INTRO_VOICE_TEXT = "Bienvenidos a Sincronías, donde nada es casualidad."
CTA_VOICE_TEXT = "Si has llegado hasta aquí, esta historia era para ti. Suscríbete y dale like: la próxima señal puede cambiarlo todo."
OUTRO_VOICE_TEXT = "Gracias por acompañarnos. Hasta la próxima sincronía."

CANAL_INITIALS = "SX"             # Sincronías
LOGO_SIZE = 140
LOGO_PATH = ""

# ── Vignette ──────────────────────────────────────────────────
# Now hardcoded in video_editor._create_vignette_clip() for uniform subtle effect across all channels

# ═══════════════════════════════════════════════════════════════════
# YOUTUBE METADATA
# ═══════════════════════════════════════════════════════════════════
YT_CATEGORY_ID = "27"              # Education (better indexing/search reach than Entertainment)

YT_PRIVACY_STATUS = "public"

# Auto-mark videos as AI-generated content after upload (browser automation)
AUTO_MARK_ALTERED_CONTENT = True

# Auto-configure end screens (Subscribe + Video recommendation) after IA mark
AUTO_END_SCREENS = True

# ── Scheduled Publishing ──────────────────────────────────────────
# "immediate" = pública al subir inmediatamente (sin warmup largo)
# "scheduled" = sube en privado, se publica solo a la hora pico
PUBLISH_MODE = "immediate"
# ── 3-Phase Pipeline (v9) ─────────────────────────────────────────
GENERATION_LEAD_HOURS = 36    # Max hours ahead to generate
# Upload windows (franjas de subida): videos suben en estas franjas a horas random
UPLOAD_WINDOWS = [
    {"start": 10, "end": 13},   # Mañana: 10:00-13:00
    {"start": 16, "end": 19},   # Tarde: 16:00-19:00 (cubre optimal 18:37)
    {"start": 20, "end": 22},   # Noche: 20:00-22:00
]
PUBLISH_TIMEZONE = "Europe/Madrid"
# PUBLISH_TARGET_HOUR = None — usa optimal_slots calculados por datos
PUBLISH_JITTER_MIN = 15            # ±15 min jitter: evita colisiones exactas si los guards fallan
PUBLISH_WARMUP_MIN = 5             # Mínimo 5 min entre subida y publicación (immediate mode)
PUBLISH_WINDOW_SPREAD_MIN = 90      # ±90 min spread around peak hour to avoid collisions (v23)

YT_DEFAULT_TAGS = [
    # Tier 1: Primary keywords (broad match)
    "casualidades imposibles",
    "milagros reales",
    "fenómenos inexplicables",
    "sincronías",
    "historias increíbles",
    # Tier 2: Named phenomena (high-intent search)
    "coincidencias sorprendentes",
    "predicciones cumplidas",
    "experiencias inexplicables reales",
    "milagros modernos documentados",
    "sueños premonitorios",
    "edgar mitchell astronauta misterio",
    "bligh motin bounty supervivencia",
    "mardani khel arte marcial india",
    "cern aceleracion del tiempo",
    "angeles reales grabados en video",
    # Tier 3: Ultra-long-tail (low competition)
    "sueños que predijeron el futuro casos reales",
    "coincidencias imposibles que cambiaron la historia",
    "milagros reales documentados por la ciencia",
    "experiencias inexplicables que te dejaran sin palabras",
    "fenómenos paranormales captados en camara",
    "glitches en la matrix experiencias reales",
    "sincronicidades de carl jung explicadas",
    "casos reales de telepatia documentados",
    "personas que predijeron su propia muerte",
    "milagros de supervivencia contra todo pronostico",
    "momentos inexplicables grabados en directo",
    "historias reales que la ciencia no puede explicar",
    "señales del universo sincronías inexplicables",
    "avistamientos inexplicables grabados en video real",
    "profecías que se cumplieron exactamente",
    # Tier 4: Format / audience intent
    "documental misterio español",
    "video ensayo inexplicable",
    "historias que desafían la lógica",
    "misterios reales documental",
    "historias para reflexionar",
]

# ═══════════════════════════════════════════════════════════════════
# SEO
# ═══════════════════════════════════════════════════════════════════

SEO_PRIMARY_KEYWORD = "milagros reales documentados"

SEO_SECONDARY_KEYWORDS = [
    # Core niche
    "casualidades imposibles",
    "coincidencias inexplicables",
    "sincronías del universo",
    "fenómenos inexplicables reales",
    "milagros modernos",
    # Phenomena (specific)
    "predicciones cumplidas reales",
    "sueños premonitorios reales",
    "experiencias inexplicables documentadas",
    "casualidades sorprendentes historia",
    "sucesos paranormales reales",
    "glitches en la matrix casos reales",
    "sincronicidades jung ejemplos reales",
    "edgar mitchell telepatia espacio",
    "motin de la bounty supervivencia real",
    "colisionador de hadrones percepcion tiempo",
    "angeles grabados en video real",
    # Long-tail audience intent (low competition)
    "sueños que predijeron el futuro historias reales",
    "coincidencias imposibles que te dejaran helado",
    "milagros reales que la ciencia no puede explicar",
    "momentos inexplicables captados en video",
    "casos de telepatia documentados por la ciencia",
    "personas que predijeron su propia muerte real",
    "historias de supervivencia contra todo pronostico",
    "señales del universo que no puedes ignorar",
    "fenómenos paranormales documental español",
    "profecías que se hicieron realidad",
    # Format / channel
    "documental misterio español",
    "video ensayo inexplicable",
    "historias que desafían la lógica",
    "misterios reales documental",
    # Audience intent
    "historias para reflexionar",
    "datos curiosos inexplicables",
    "historias reales que inspiran",
    "fenómenos que la ciencia no explica",
]

SEO_HASHTAGS = [
    "#Sincronías",
    "#MilagrosReales",
    "#Casualidades",
    "#Destino",
    "#HistoriasReales",
    "#LoInexplicable",
    "#Misterio",
    "#Documental",
    "#Fenómenos",
    "#Curiosidades",
    "#Inspiración",
    "#Universo",
    "#SabíasQue",
    "#Coincidencias",
    "#HistoriasQueInspiran",
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
    "#Sincronías",
    "#MilagrosReales",
    "#SabíasQue",
    "#Shorts",
    "#Curiosidades",
    "#Destino",
    "#Misterio",
    "#Casualidades",
    "#LoInexplicable",
    "#HistoriasReales",
]

# ── Subscribe CTA variants (rotated, ~40% of native shorts) ──
SHORTS_SUBSCRIBE_CTA_VARIANTS = [
    "Suscríbete para más historias como esta",
    "Dale like y suscríbete si quieres más",
    "En nuestro canal hay muchas más como esta",
    "Suscríbete y activa la campana para no perderte nada",
    "Si te gustó, suscríbete. Publicamos a diario.",
    "Únete al canal, cada día hay algo nuevo",
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

DESCRIPTION_TEMPLATE = """✨ {titulo}
———

{descripcion_seo}

🌟 EN ESTE VIDEO
- La historia real detrás de este suceso inexplicable
- Los protagonistas y sus testimonios
- Lo que dice la ciencia (y lo que no puede explicar)
- La lección que nos deja sobre el destino y las casualidades

⏱️ CAPÍTULOS
{chapters}

———

🎙️ Bienvenido a **Sincronías** — el canal donde exploramos las casualidades más increíbles, los milagros mejor documentados y los fenómenos inexplicables que desafían toda lógica. Historias reales de personas comunes que vivieron lo imposible.

📚 Fuentes: Wikipedia, artículos periodísticos, hilos de Reddit (r/Glitch_in_the_Matrix, r/nevertellmetheodds) y archivos históricos verificados.

✨ Todo el contenido está basado en hechos reales documentados.

🔔 Suscríbete y activa la campana para descubrir más historias que desafían lo imposible.

💬 ¿Crees en las casualidades o en el destino? Déjalo en los comentarios.

#Sincronías #MilagrosReales #CasualidadesImposibles"""

# ═══════════════════════════════════════════════════════════════════
# THUMBNAIL
# ═══════════════════════════════════════════════════════════════════

THUMBNAIL_WIDTH = 1280
THUMBNAIL_HEIGHT = 720
THUMBNAIL_FONT_SIZE = 56
THUMBNAIL_BORDER_WIDTH = 5

# ── Per-channel thumbnail customisation (v2.1) ────────────────
THUMBNAIL_FONT_FAMILY = "DejaVuSans-Bold"       # sans-serif bold (Sincronías style)
THUMBNAIL_BORDER_COLOR = "#CC0000"              # brand red (classic)
THUMBNAIL_SHOW_4K_BADGE = True                  # keep 4K badge
THUMBNAIL_TEXT_STROKE_WIDTH = 3                 # outline for readability (matching canal3)
THUMBNAIL_TEXT_STROKE_COLOR = "#000000"         # unused (stroke=0)

# ── Per-channel visual style (coherent across all videos) ───────
THUMBNAIL_VISUAL_STYLE = "moody_atmospheric"
THUMBNAIL_STYLE_OVERRIDE = True

# Manual style config for moody_atmospheric (deterministic, no LLM needed)
# v2.2 — overhaul for higher CTR: bold contrast, vivid colors, golden text
THUMBNAIL_MANUAL_STYLE = {
    "visual_style": "moody_atmospheric",
    "color_palette": {
        "primary": "#1A1A3E",       # deep blue — dramatic, better contrast with gold
        "accent": "#D4AF37",        # metallic gold — vibrant, eye-catching
        "text": "#FFD700",          # bright gold — maximum contrast on dark backgrounds
        "shadow": "#0A0A0F",        # near-black — deeper depth, better readability
    },
    "base_composition": "dark_reveal",
    "effects": {
        "contrast_boost": 1.15,     # moderate — natural cinematic look (canal3-style)
        "saturation": 0.85,         # slightly desaturated — elegant, not garish
        "vignette": 0.35,           # subtle — draws eye without darkening image
    },
    "text_style": {
        "uppercase": False,
        "max_words": 5,
    },
    "pollo_prompt_suffix": (
        "soft cinematic lighting, mysterious atmosphere, warm golden accents, "
        "contemplative mood, golden hour glow, subtle color grading, artistic "
        "photography, 16:9 aspect ratio, photorealistic, professional documentary "
        "photography, rich textures, atmospheric depth"
    ),
}

# Legacy descriptive style (kept for reference)
THUMBNAIL_STYLE = {
    "layout": "image_full_background_text_overlay",
    "max_text_words": 4,
    "text_color": "warm_gold_on_deep_indigo",
    "font_style": "bold_elegant_sans_or_serif",
    "image_treatment": "warm_luminous_golden_hour_ethereal_glow",
    "background": "#0F203E",
    "accent_color": "#D4AF37",
    "face_policy": (
        "Real faces (public domain or stock) YES — eyes looking up, expressions of wonder. "
        "Survivors, protagonists. Warm lighting, natural expressions. "
        "AI-generated faces: NO. Siluetas a contraluz, manos juntas, personas "
        "mirando al horizonte OK."
    ),
    "number_preference": "odd_numbers_for_lists",
    "gold_accent_rule": (
        "Golden underline or light ray on key element boosts CTR. "
        "Warm, hopeful aesthetic. NEVER use dark horror styling. "
        "NEVER use blood red or black backgrounds for this channel."
    ),
}

THUMBNAIL_TEMPLATES = {
    "the_light": {
        "description": "Backlit figure, golden hour, ray of light, hopeful expression",
        "text_position": "bottom_third_centered",
        "text_words": "2-3",
        "accent": "golden_light_ray_or_glow",
        "best_for": "Survivor stories, miracle interventions, NDE accounts",
    },
    "the_coincidence": {
        "description": "Split composition: two related elements connected by visual thread/line",
        "text_position": "center_bridging",
        "text_words": "3-4",
        "accent": "golden_connecting_line",
        "best_for": "Coincidence stories, synchronicities, parallel lives",
    },
    "the_evidence": {
        "description": "Vintage document, newspaper clipping, or photograph with warm sepia treatment",
        "text_position": "bottom_over_gradient",
        "text_words": "3-4",
        "accent": "magnifying_glass_or_circle_highlight",
        "best_for": "Documented miracles, verified predictions, historical evidence",
    },
}

# ═══════════════════════════════════════════════════════════════════
# VIDEO TIMING & MONETIZATION
# ═══════════════════════════════════════════════════════════════════

VIDEO_MIDROLL_STRATEGY = (
    "Colocar mid-rolls en pausas naturales entre capítulos narrativos. "
    "NUNCA en medio de una frase ni durante el clímax de asombro. "
    "Cada mid-roll debe preceder un mini-gancho que mantenga al espectador."
)

MONETIZATION_TARGET_CPM = "$5–$12 USD"

MONETIZATION_VERTICALS = [
    "Bienestar y espiritualidad",
    "Libros / Audiolibros",
    "Viajes y experiencias",
    "Tecnología",
    "Salud y bienestar mental",
]

# ═══════════════════════════════════════════════════════════════════
# END SCREEN
# ═══════════════════════════════════════════════════════════════════

END_SCREEN_STRATEGY = {
    "left_card": {
        "type": "playlist",
        "content": "most_relevant_playlist",
        "purpose": "Keep viewer in a session of wonder — thematic rabbit hole",
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
        "Si esta historia te dejó sin palabras, la siguiente en la lista "
        "es todavía más increíble. Te dejo el enlace en pantalla. "
        "Suscríbete si quieres descubrir más historias que desafían lo imposible."
    ),
}

# ═══════════════════════════════════════════════════════════════════
# PLAYLISTS
# ═══════════════════════════════════════════════════════════════════

PLAYLISTS = [
    {
        "slug": "historias-completas",
        "name": "Historias Completas",
        "description": (
            "Documentales en profundidad sobre los milagros, casualidades y "
            "fenómenos inexplicables más sorprendentes de la historia. "
            "Cada video: contexto, protagonistas, el suceso y las consecuencias."
        ),
        "type": "main",
    },
    {
        "slug": "lo-mas-increible",
        "name": "Lo Más Increíble",
        "description": (
            "Las 5 historias más asombrosas del canal. Si eres nuevo aquí, "
            "empieza por esta lista. Bienvenido a Sincronías."
        ),
        "type": "onboarding",
    },
    {
        "slug": "milagros-modernos",
        "name": "Milagros Modernos",
        "description": (
            "Milagros documentados del siglo XX y XXI. Evidencias, "
            "testimonios y el contexto científico de cada caso."
        ),
        "type": "thematic",
    },
    {
        "slug": "casualidades-imposibles",
        "name": "Casualidades Imposibles",
        "description": (
            "Las coincidencias más alucinantes de la historia. Probabilidades "
            "de una entre millones que ocurrieron. El universo conspirando."
        ),
        "type": "thematic",
    },
    {
        "slug": "predicciones-que-se-cumplieron",
        "name": "Predicciones que se Cumplieron",
        "description": (
            "Sueños premonitorios, profecías acertadas y personas que vieron "
            "el futuro antes de que ocurriera. Documentado y verificado."
        ),
        "type": "thematic",
    },
]

# ═══════════════════════════════════════════════════════════════════
# FIRST 48 HOURS STRATEGY
# ═══════════════════════════════════════════════════════════════════

FIRST_48H_STRATEGY = {
    "pre_upload_24h": [
        "Community Tab poll: '¿Crees en las casualidades o en el destino?'",
        "YouTube Story: imagen evocadora + 'Mañana. 9PM MX. Esta historia es real.'",
    ],
    "hour_0": [
        "Publish at 9PM Mexico City time (peak curiosity consumption window)",
        "First comment (immediate, pinned): pregunta existencial para disparar debate",
    ],
    "hours_1_6": [
        "Reddit r/Glitch_in_the_Matrix: TEXT post with compelling summary",
        "Facebook groups: Misterios, Curiosidades, Historias Reales",
    ],
    "hours_6_24": [
        "Reply to EVERY comment in first 24h (3x algorithm weight on engagement)",
        "Twitter/X thread: 5-7 tweets contando la historia, final tweet = YouTube link",
    ],
    "hours_24_48": [
        "Analyze CTR and retention in YouTube Studio",
        "If CTR < 5%: swap thumbnail variant",
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
            "example": "¿Te ha pasado alguna casualidad imposible?",
            "options": ["Sí, varias veces", "Una vez", "Nunca"],
        },
        "wednesday": {
            "type": "image_fact",
            "example": (
                "Fotografía histórica + 'Las probabilidades de que esta "
                "persona sobreviviera eran de 1 entre 12 millones. Sobrevivió.'"
            ),
        },
        "friday": {
            "type": "teaser",
            "example": (
                "Este sábado: la casualidad más increíble jamás documentada. "
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
            "Las probabilidades de que esto ocurriera eran de "
            "1 entre {number}. Pero ocurrió."
        ),
        "structure": "Hook → El suceso (20s) → El momento increíble (15s) → 'Video completo en YouTube, link en bio' (10s)",
        "cadence": "3x/day from one long-form video (days 1, 3, 5)",
    },
    "youtube_shorts": {
        "format": "15-30s most incredible moment",
        "end_cta": "'Video completo en el canal' linked to long-form",
        "purpose": "Shorts feed → channel page → long-form viewer conversion",
    },
    "twitter_x": {
        "format": "Thread — 1 story = 1 thread per week",
        "template": "'HOY en Sincronías: La historia de la persona que...' + 5-7 tweets + link",
    },
    "spotify_podcast": {
        "format": "Audio-only export of each video",
        "title_format": "Sincronías | {story_title} | {key_detail}",
        "purpose": "Minimal effort, massive discovery platform",
    },
}

# ═══════════════════════════════════════════════════════════════════
# COLLABORATION TARGETS
# ═══════════════════════════════════════════════════════════════════

COLLABORATION_TARGETS = {
    "tier_1_direct": [
        {"name": "Mundo Desconocido", "niche": "Misterios y fenómenos inexplicables, audiencia afín"},
        {"name": "VM Granmisterio", "niche": "Conspiración y misterio, enorme audiencia LATAM"},
        {"name": "BreakMan", "niche": "Misterio positivo y curiosidades"},
        {"name": "Pandora", "niche": "Misterio documental, tono similar"},
    ],
    "tier_2_adjacent": [
        {"name": "DrossRotzank", "niche": "Dark/pioneer — estrategia de mención"},
        {"name": "Relatos del Lado Oscuro", "niche": "Narración sin rostro, mismo formato"},
    ],
    "collab_formats": [
        "React: 'Un escéptico reacciona a Sincronías' — feature en tu canal",
        "Topic trade: cubrir el caso sugerido por otro creador, cross-promote",
        "Mention strategy: 'Como dijo [Creator] en su video...' — goodwill",
    ],
}

# ═══════════════════════════════════════════════════════════════════
# TRENDING TOPIC HOOKS
# ═══════════════════════════════════════════════════════════════════

TRENDING_TOPIC_HOOKS = {
    "type_a_news": {
        "trigger": "Noticia de supervivencia imposible o coincidencia en prensa",
        "pivot": (
            "Esto que acaba de pasar en {country}... la historia tiene "
            "docenas de casos igual de inexplicables."
        ),
    },
    "type_b_anniversary": {
        "trigger": "Aniversario de milagros o coincidencias famosas",
        "calendar": {
            "december": "Milagros navideños y casualidades de fin de año",
            "october": "Fenómenos inexplicables (Halloween search spike — enfoque positivo)",
            "february": "Historias de amor imposibles y destino (San Valentín)",
        },
    },
    "type_c_pop_culture": {
        "trigger": "Estreno de película/serie sobre destino, casualidades o milagros",
        "strategy": "'La historia REAL que inspiró {show/movie}'",
        "examples": "Manifest, The OA, Touched by an Angel → milagros documentados",
    },
    "type_d_calendar": {
        "name": "Calendario de la Maravilla",
        "months": {
            "january": "Casualidades de año nuevo / propósitos cumplidos",
            "february": "Destino y amor / encuentros imposibles",
            "march": "Suerte y coincidencias (St. Patrick's — temática fortuna)",
            "may": "Milagros modernos (Mes de la Salud Mental — enfoque esperanza)",
            "october": "Fenómenos inexplicables (Halloween — versión positiva)",
            "december": "Milagros navideños / casualidades de fin de año",
        },
    },
}

# ═══════════════════════════════════════════════════════════════════
# CONTENT PILLARS
# ═══════════════════════════════════════════════════════════════════

CONTENT_PILLARS = [
    {
        "name": "La Historia",
        "ratio": 55,
        "desc": "Documental profundo de un suceso inexplicable individual",
    },
    {
        "name": "Listas y Recopilaciones",
        "ratio": 30,
        "desc": "Compilación temática: '5 casualidades que desafían la lógica'",
    },
    {
        "name": "La Reflexión",
        "ratio": 15,
        "desc": "Video más corto conectando el tema con la vida cotidiana",
    },
]

# ═══════════════════════════════════════════════════════════════════
# VIRAL MIRROR
# ═══════════════════════════════════════════════════════════════════
VIRAL_ENABLED = True
VIRAL_CONTENT_MODE = "rewrite"  # "rewrite" = via ScriptGenerator (original content)
                                  # "direct"  = use scraper output directly (legacy, risk of translation)
VIRAL_MAX_AGE_DAYS = 29  # Max days since publication (videos older are discarded)

NICHE_KEYWORDS_ENG = [
    "unexplained mysteries",
    "incredible coincidences",
    "synchronicity explained",
    "miracles caught on camera",
    "mind blowing coincidences",
    "strange synchronicities",
    "real life miracles",
    "unexplained phenomena",
    "incredible true stories",
    "mysteries science can't explain",
    "paranormal stories real",
    "strange but true stories",
]

# English keywords per playlist — used by viral_query_builder for diverse searches
# ═══════════════════════════════════════════════════════════════════
# MARATHON MODE — Video largo de ~1 hora (v1.0)
# ═══════════════════════════════════════════════════════════════════
MARATHON_ENABLED = True
MARATHON_VIDEO_DURATION_TARGET = 60          # minutos
MARATHON_NUM_SECTIONS = 12                   # "Los 12 Sincronismos Más Increíbles"
MARATHON_NARRATIVE_FORMAT = "top_cases"      # "top_cases" | "deep_story" | "historical_collapse"
MARATHON_SCRIPT_WORDS_MIN = 8000             # ~60 min × 165 wpm × 0.85 Kokoro speed × 1.05 cushion
MARATHON_SCRIPT_WORDS_MAX = 12000
MARATHON_SCRIPT_BLOCKS_MIN = 50
MARATHON_SCRIPT_BLOCKS_MAX = 90
MARATHON_OUTLINE_CHAPTERS = 15               # capítulos en el outline (sin el cap de 6 habitual)
MARATHON_MEDIA_VIDEO_PCT = 20                # % video (reducido para 1h, más imágenes)
MARATHON_TITLE_FORMAT = "Los {N} Sincronismos Más Increíbles de la Historia"
MARATHON_LLM_MAX_BATCHES = 150               # batches extra para guiones largos
MARATHON_LLM_MAX_EMPTY_STRIKES = 20          # tolerancia extra en generación larga
MARATHON_PUBLISH_MODE = "scheduled"          # siempre programado en prime time

VIRAL_PLAYLIST_KEYWORDS = {
    "historias-completas": [
        "deep documentary mystery",
        "amazing true stories full documentary",
        "incredible unexplained phenomena",
        "in depth mystery documentary",
    ],
    "lo-mas-increible": [
        "most amazing stories ever",
        "unbelievable true stories compilation",
        "most shocking unsolved mysteries",
        "stories that will blow your mind",
    ],
    "milagros-modernos": [
        "documented miracles true stories",
        "modern day miracles caught on camera",
        "scientific evidence for miracles",
        "medical miracles unexplained recovery",
    ],
    "casualidades-imposibles": [
        "impossible coincidences true stories",
        "historical coincidences too strange to be random",
        "scientists explain synchronicity",
        "probability one in a million true stories",
    ],
    "predicciones-que-se-cumplieron": [
        "predictions that actually came true",
        "prophetic dreams verified",
        "people who predicted the future correctly",
        "prophecies that were fulfilled",
    ],
}
