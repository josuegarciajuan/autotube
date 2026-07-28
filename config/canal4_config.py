from config.settings import DEFAULT_VIDEO_PROVIDERS, DEFAULT_VIDEO_FALLBACK_QUERIES

"""Configuration for Canal 4: Expediciones sin Retorno.

Meta-niche: "historias reales de expediciones que terminaron catastróficamente mal
— barcos atrapados en el hielo, montañeros perdidos en tormentas, travesías
mortales por desiertos y selvas. Y los pocos que lograron volver."

Formato: video-essay documental de supervivencia, 8-14 min, narrado con imágenes
cinematográficas frías y tensas.

Estilo: "documental de supervivencia" — expediciones al límite de lo humano.
"""

# ═══════════════════════════════════════════════════════════════════
# IDENTITY
# ═══════════════════════════════════════════════════════════════════

CANAL_NAME = "canal4"
CANAL_DISPLAY_NAME = "Expediciones sin retorno"
CANAL_TAGLINE = (
    "Historias reales de expediciones que terminaron catastróficamente mal... "
    "y los pocos que lograron volver."
)
CANAL_OUTRO_TAGLINE = (
    "La historia de esta expedición es real. Los nombres, las fechas, "
    "lo que encontraron... todo ocurrió."
)

# ── Narrative Style ─────────────────────────────────────────────
CANAL_NARRATIVE_STYLE = "documental de supervivencia"
CANAL_STYLE_DESCRIPTION = (
    "Expediciones reales llevadas al límite. Hielo, desierto, montaña, océano. "
    "Historias de personas que empujaron las fronteras humanas... y pagaron el "
    "precio. El formato sin rostro no es una limitación: es una feature. "
    "Las imágenes de los entornos más hostiles del planeta hablan solas."
)

# ── Channel About Section (indexado por YouTube search) ─────────
CHANNEL_ABOUT_SECTION = """Bienvenido a Expediciones sin retorno.

Documentales sobre las exploraciones que terminaron en desastre: expediciones al Artico atrapadas en el hielo, naufragios en medio del océano, escaladas imposibles y travesias del desierto que nadie completo. Historias reales de personas que empujaron los limites humanos... y a veces no regresaron.

🎬 Formato: video ensayos documentales (8-14 minutos)
🗓️ Nuevas historias: cada semana
🎙️ Narracion documental con fuentes verificadas

📩 Contacto: {email}

⛵ Si te fascinan las historias de exploracion, supervivencia extrema y el drama humano en los entornos mas hostiles del planeta... este canal es para ti.

Suscribete y activa la campana para no perderte ninguna expedicion."""

# ── Channel Keywords (YouTube Studio → Settings → Channel) ──────
CHANNEL_KEYWORDS = [
    "expediciones fallidas",
    "exploracion y supervivencia",
    "naufragios historicos",
    "tragedias en el everest",
    "documentales de supervivencia",
    "expediciones al artico",
    "explotadores desaparecidos",
    "historia real de exploracion",
    "accidentes de montaña",
    "supervivencia extrema",
    "naufragio real",
    "expediciones perdidas",
    "documental exploracion",
    "historias de supervivencia",
    "tragedias en expediciones",
    "exploracion polar",
    "expedicion franklin",
    "donner party",
    "documental en español",
    "historias increibles reales",
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

PROD_SCRIPT_WORDS_MIN = 900        # ~5 min × 150 wpm × 1.05 rate × 1.15 cushion
PROD_SCRIPT_WORDS_MAX = 1200       # 30 % headroom
PROD_SCRIPT_SCENES_MIN = 6
PROD_SCRIPT_SCENES_MAX = 10
PROD_SCRIPT_BLOCKS_MIN = 5         # ~180 words / block average
PROD_SCRIPT_BLOCKS_MAX = 10
PROD_VIDEO_DURATION_MIN = 4        # minutos
PROD_VIDEO_DURATION_MAX = 7        # minutos

# ── Average video duration target (in minutes) ──
# These are the single source of truth for production — read via the
# panel "Duración — Objetivo" and used by _get_word_target().
VIDEO_AVERAGE_DURATION_MIN = 5     # producción: ~4–6 min con variación
VIDEO_DURATION_DISCREPANCY_MIN = 1 # random.uniform(4, 6)

# ═══════════════════════════════════════════════════════════════════
# NARRATIVE TONE
# ═══════════════════════════════════════════════════════════════════

CANAL_TONE = (
    "Grave, tenso y profundamente humano. Narrativa documental que oscila "
    "entre el asombro (la ambición del viaje, la belleza de lo desconocido) "
    "y el horror (el momento exacto en que todo se tuerce). Riguroso en los "
    "hechos, implacable en la atmósfera, profundamente empático con las "
    "personas que vivieron la pesadilla. Como un documental de National "
    "Geographic sobre supervivencia extrema. El espectador debe sentir el "
    "frío, el hambre, la desesperación... y la esperanza."
)

# ═══════════════════════════════════════════════════════════════════
# TARGET AUDIENCE
# ═══════════════════════════════════════════════════════════════════

TARGET_AUDIENCE = (
    "18-45 años (amplio), LATAM (MX 30%, CO 20%, AR 15%, PE 10%, ES 15%, "
    "otros 10%). Curiosos, amantes de la aventura, historia, documentales "
    "de naturaleza y supervivencia. 60% hombres / 40% mujeres. "
    "60%+ mobile. Sesiones de 8-12 min. Pico de consumo: 20:00-00:00 local."
)

TARGET_AUDIENCE_PSYCHOGRAPHIC = {
    "The Armchair Explorer": (
        "Consume contenido de exploración desde el sillón. "
        "Vive las expediciones vicariamente. Le encanta el drama humano."
    ),
    "The Survival Enthusiast": (
        "Ve estos documentales para aprender de los errores de otros. "
        "Analiza cada decisión: 'yo habría hecho X'."
    ),
    "The History Lover": (
        "Busca historias reales bien documentadas. "
        "Valora las fuentes, mapas, datos concretos y contexto histórico."
    ),
    "The Thrill Seeker": (
        "Adrenalina desde la pantalla. Quiere sentir la tensión, "
        "el peligro, la cercanía de la muerte. Comparte los más impactantes."
    ),
}

# ═══════════════════════════════════════════════════════════════════
# TITLE OPTIMIZATION
# ═══════════════════════════════════════════════════════════════════

TITLE_FORMULAS = [
    # Pattern 1: Named Expedition + Numbers
    "La Expedicion {name}: {number} Partieron, Solo {survivors} Regresaron",
    # Pattern 2: Trapped / Stranded
    "Atrapados en {location}: {number} Dias Sin Comida a {temperature} Bajo Cero",
    # Pattern 3: The One Who Stayed
    "El Explorador que su Propia Tripulacion Abandono a la Deriva",
    # Pattern 4: Search + Horror
    "Buscaban {goal}. Encontraron la Muerte en {horror_detail}.",
    # Pattern 5: The Last Survivor
    "Sobrevivio {number} Dias en {location}. Nadie Sabe Como.",
    # Pattern 6: Nobody Returned
    "{number} Personas Entraron en {location}. Ninguna Salio.",
    # Pattern 7: Would You Survive?
    "La Expedicion que Te Hara Preguntarte si Habrias Sobrevivido",
]

TITLE_POWER_WORDS = [
    # ⚡ URGENCIA / EXCLUSIVIDAD
    "revelado", "filtrado", "censurado", "inédito", "clasificado",
    "confidencial", "prohibido", "archivado",
    # 💥 IMPACTO EMOCIONAL
    "escalofriante", "desgarrador", "inexplicable", "demoledor",
    "sobrecogedor", "estremecedor", "alucinante", "aterrador",
    "implacable", "extremo",
    # 🔍 CURIOSIDAD / MISTERIO
    "oculto", "secreto", "perturbador", "siniestro", "enigmático",
    "impactante", "increíble", "insólito",
    # Danger / Death (canal4 specific)
    "desapareció", "nunca regresó", "atrapados", "perdidos", "abandonados",
    "muertos", "congelados", "hundidos", "sepultados",
    # Survival / Rescue (canal4 specific)
    "sobrevivió", "rescatado", "encontrado", "escapó", "volvió",
    # Authority / Reality
    "real", "documentado", "verificado", "demostrado", "confirmado",
    # Environment / Scale (canal4 specific)
    "hielo", "nieve", "tormenta", "océano", "desierto", "montaña",
    "selva", "abisal",
]

TITLE_MAX_CHARS = 65

# ═══════════════════════════════════════════════════════════════════
# SCRIPT STRUCTURE — "Espiral del Hielo" method
# ═══════════════════════════════════════════════════════════════════

SCRIPT_HOOK_RULE = (
    "ATENCION: La primera frase del guion DEBE ser el hecho mas "
    "impactante de la expedicion, con un NUMERO y un HECHO CONCRETO. "
    "NUNCA empezar con contexto historico, definiciones, ni presentaciones. "
    "NUNCA 'Hola, bienvenidos a...' ni 'En este video vamos a hablar de...'.\n\n"
    "EJEMPLO CORRECTO: 'El 19 de mayo de 1845, 129 hombres zarparon de "
    "Inglaterra en dos barcos de ultima generacion. Ninguno volvio a ver "
    "a su familia jamas.'\n"
    "EJEMPLO INCORRECTO: 'Las expediciones polares del siglo XIX fueron "
    "un periodo de intensa exploracion artica que...'.\n\n"
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
        "step": "EL FRIO",
        "time_pct": "0-10%",
        "description": (
            "El hecho mas impactante en frio. Sin contexto. La imagen mas "
            "dramatica de la expedicion (el barco en el hielo, la tormenta, "
            "el cadaver congelado). Cerrar con promesa: 'Al final de este "
            "video vas a entender por que esta expedicion salio tan mal.'"
        ),
    },
    {
        "step": "EL SUEÑO",
        "time_pct": "10-20%",
        "description": (
            "Que buscaban, por que zarparon, la ambicion y la preparacion. "
            "Construir anticipacion tragica: 'Lo tenian todo planeado. "
            "Mapas, provisiones, los mejores barcos. Nada podia fallar.'"
        ),
    },
    {
        "step": "LOS PROTAGONISTAS",
        "time_pct": "20-30%",
        "description": (
            "Las personas reales detras de la historia. Marineros, "
            "exploradores, sus familias. Gente normal que creyo en el "
            "sueño. Humanizar para que el espectador sienta el golpe."
        ),
        "retention_anchor": (
            "CLIFFHANGER al 25%: 'Pero lo que esta persona no sabia... "
            "es que en exactamente 72 horas, su vida cambiaria para siempre.'"
        ),
    },
    {
        "step": "EL DESCENSO",
        "time_pct": "30-55%",
        "description": (
            "Todo empieza a torcerse. El hielo que no cede, la tormenta "
            "inesperada, la comida que se agota. Escalar la tension. "
            "'Y entonces llego lo que nadie habia previsto...'"
        ),
        "retention_anchor": (
            "CLIFFHANGER al 50%: Silencio 2s. Cambio de imagen. "
            "'Recapitulemos: [1 frase]. Ahora viene lo peor.'"
        ),
    },
    {
        "step": "EL COLAPSO",
        "time_pct": "55-70%",
        "description": (
            "El momento exacto de la catastrofe. Peak de tension dramatica. "
            "El barco se parte, la avalancha sepulta, el agua entra. "
            "Musica fuera. Silencio. Zoom lento sobre el horror."
        ),
    },
    {
        "step": "LA SUPERVIVENCIA",
        "time_pct": "70-85%",
        "description": (
            "Lo que hicieron para intentar sobrevivir. Decisiones "
            "desesperadas, sacrificios, canibalismo (si aplica, tratado "
            "con respeto documental). Los pocos que aguantaron."
        ),
        "retention_anchor": (
            "EL ESPEJO al 70%: Dirigirse directamente al viewer. "
            "'Ahora piensa: ¿que habrias hecho tu? ¿Te habrias rendido "
            "el dia 3 o habrias aguantado hasta el final?' Hacerlo personal."
        ),
    },
    {
        "step": "EL RESCATE / LEGADO",
        "time_pct": "85-100%",
        "description": (
            "Como termino. Los que sobrevivieron (si hubo), los que no. "
            "Lo que cambio en la exploracion, las lecciones aprendidas. "
            "Conexion con el presente. Pregunta al viewer. End hook + CTA."
        ),
    },
]

SCRIPT_END_HOOK = (
    "Y si crees que esta expedicion fue tragica, espera a ver {next_expedition}. "
    "Porque lo que le ocurrio a esa tripulacion fue todavia mas increible. "
    "Ese es el proximo video. Dale like, suscribete y activa la campana."
)

SCRIPT_EMOTIONAL_ARC = {
    "0-10%": "impacto",
    "10-20%": "anticipacion tragica",
    "20-30%": "empatia",
    "30-45%": "tension creciente",
    "45-55%": "angustia",
    "55-65%": "horror",
    "65-75%": "desesperacion → instinto",
    "75-85%": "alivio amargo / duelo",
    "85-95%": "reflexion",
    "95-100%": "respeto",
}

# Retention anchors
RETENTION_ANCHORS = {
    "at_25_pct": {
        "trigger": "cliffhanger_mid_video",
        "action": (
            "Insertar mini-cliffhanger: 'Pero lo que ocurrio 3 dias despues "
            "cambio todo.' Tratar el video como capitulos, no como ensayo."
        ),
    },
    "at_50_pct": {
        "trigger": "the_reset",
        "action": (
            "Musica fuera 2s. Nueva imagen o fotografia real de la expedicion. "
            "'Recapitulemos: [resumen 1 frase]. Ahora viene lo peor.'"
        ),
    },
    "at_70_pct": {
        "trigger": "the_mirror",
        "action": (
            "Dirigirse al viewer directamente. Hacerlo personal: 'Ahora "
            "piensa: ¿que habrias hecho tu con -40 grados y sin comida?' "
            "Cerrar con teaser: 'En 60 segundos te cuento que paso al final.'"
        ),
    },
}

# ═══════════════════════════════════════════════════════════════════
# VIRALITY TRIGGERS
# ═══════════════════════════════════════════════════════════════════

VIRALITY_TRIGGERS = [
    {
        "name": "Survival Instinct",
        "mechanism": (
            "'¿Habrias sobrevivido tu?' La pregunta personal activa el "
            "instinto de supervivencia del viewer. La gente se proyecta "
            "en la historia y comparte el video como 'esto me podria pasar'."
        ),
    },
    {
        "name": "Awe of Nature",
        "mechanism": (
            "La naturaleza como antagonista imponente. El asombro ante "
            "la escala del desastre natural genera respeto y se comparte. "
            "Imagenes del hielo, la tormenta, el oceano enfurecido."
        ),
    },
    {
        "name": "Moral Dilemma",
        "mechanism": (
            "Cerrar con pregunta: '¿Abandonarias a tu compañero herido "
            "para salvar al resto del grupo?' Comentarios = señal de "
            "engagement = mas impresiones = mas shares."
        ),
    },
    {
        "name": "Conversation Starter",
        "mechanism": (
            "'No sabia que esto habia pasado.' Las historias de "
            "expediciones fallidas suelen ser poco conocidas. La gente "
            "comparte para sorprender a otros: 'mira lo que descubri.'"
        ),
    },
    {
        "name": "Respect for the Fallen",
        "mechanism": (
            "Tratar a los exploradores fallecidos con profundo respeto. "
            "No como victimas de un morbo, sino como heroes tragicos de "
            "la exploracion. La audiencia conecta emocionalmente."
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
    "rate_base": "-15%",         # moderately slow, documentary gravitas
    "pitch_base": "+0Hz",        # natural pitch, authoritative

    # ── Hook (opening impact) ────────────────────────────
    "rate_hook": "-5%",          # measured pace, direct impact
    "pitch_hook": "-2Hz",        # slight gravity for authority

    # ── Desarrollo (body — neutral storytelling) ────────
    "rate_desarrollo": "-15%",
    "pitch_desarrollo": "+0Hz",

    # ── Climax (catastrophic peak) ──────────────────────
    "rate_climax": "-22%",       # slow — weight of disaster
    "pitch_climax": "-8Hz",      # deep gravity

    # ── Reflexion (contemplative) ───────────────────────
    "rate_reflexion": "-18%",    # contemplative pace
    "pitch_reflexion": "-2Hz",

    # ── Cierre (closing call-to-action) ──────────────────
    "rate_cierre": "-10%",
    "pitch_cierre": "+2Hz",      # slightly warmer for CTA
}

VOICE_ID = TTS_STRATEGY["voice_primary"]
VOICE_SECONDARY = TTS_STRATEGY["voice_secondary"]
VOICE_RATE = TTS_STRATEGY["rate_base"]
VOICE_PITCH = TTS_STRATEGY["pitch_base"]
VOICE_VOLUME = "+0%"

VOICE_SSML = {
    "break_after_hook": '<break time="800ms"/>',
    "break_before_climax": '<break time="1500ms"/>',
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
# canal4 uses edge-tts by default (kokoro config ready below)

TTS_ENGINE = "edgetts"

# ── Kokoro configuration (activar con TTS_ENGINE = "kokoro") ──
KOKORO_VOICE = "em_alex"  # ef_dora | em_alex | em_santa
KOKORO_BLOCK_SPEEDS = {
    "hook": 1.02,          # ligera urgencia
    "desarrollo": 0.95,    # narracion calmada
    "climax": 0.82,        # tension maxima — muy lento
    "reflexion": 0.90,     # contemplativo
    "cierre": 0.97,        # casi normal
}
KOKORO_PAUSE_BETWEEN_BLOCKS = 0.8

# ═══════════════════════════════════════════════════════════════════
# CONTENT SOURCES
# ═══════════════════════════════════════════════════════════════════

REDDIT_SUBREDDITS = [
    # Survival / expeditions
    "Survival",
    "expedition",
    "Mountaineering",
    "shipwrecks",
    "Maritime",
    "WildernessBackpacking",
    # History / true stories
    "History",
    "TrueReddit",
    "todayilearned",
    "AskHistorians",
    "HistoryAnecdotes",
    # Adventure / extreme
    "Damnthatsinteresting",
    "interestingasfuck",
    "AbandonedPorn",
    "natureismetal",
    "HumanPorn",
    "CatastrophicFailure",
    # General viral discovery
    "AskReddit",
    "UnsolvedMysteries",
    "HighStrangeness",
]

REDDIT_SORT = "top"
REDDIT_TIME = "month"
REDDIT_LIMIT = 25

WIKIPEDIA_CATEGORIES = [
    # English
    "Exploration disasters",
    "Shipwrecks",
    "Maritime disasters",
    "Mountaineering deaths",
    "Explorers lost at sea",
    "Lost explorers",
    "Arctic expeditions",
    "Antarctic expeditions",
    "Survival",
    "People lost at sea",
    "Shipwrecks in the Arctic Ocean",
    "Disasters in Antarctica",
    "Missing aviators",
    "Sole survivors",
    "Cannibalism",
    "Desert survival",
    "Mountain disasters",
    "Failed expeditions",
    # Spanish
    "Naufragios",
    "Expediciones al Artico",
    "Expediciones a la Antartida",
    "Exploradores de España",
    "Accidentes de montaña",
    "Desastres maritimos",
    "Naufragios en el Atlantico",
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

# Atlas Obscura categories for Expediciones
ATLAS_OBSCURA_CATEGORIES = ["abandoned", "natural-wonders", "unique", "maritime"]

# RSS feeds for Expediciones
RSS_FEEDS = []

# Google News queries for Expediciones
GOOGLE_NEWS_QUERIES = [
    "expedicion desaparecida",
    "rescate montaña",
    "naufragio historico",
    "explorador perdido",
    "supervivencia extrema",
]
GOOGLE_NEWS_LANGUAGE = "es"
GOOGLE_NEWS_COUNTRY = "ES"

# ═══════════════════════════════════════════════════════════════════
# VISUAL STYLE
# ═══════════════════════════════════════════════════════════════════

IMAGE_STYLE_MODIFIERS = (
    "cinematic documentary photography, natural lighting, wide shot, 16:9, "
    "professional photography, dramatic atmosphere, expedition landscape"
)

COLOR_PALETTE = {
    "primary": (15, 40, 65),          # arctic deep blue
    "secondary": (18, 28, 50),        # dark navy — cinematic vignette, balanced
    "accent": (255, 92, 0),           # rescue orange neon — universal distress signal
    "text": (235, 240, 245),          # frost white
    "text_shadow": (4, 6, 12),        # deep navy shadow
    "tertiary": (35, 45, 55),         # dark steel blue — safe background
    "warning": (255, 92, 0),          # rescue orange for thumbnail CTR
}

# Per-channel image tint (decoupled from brand palette): neutral desaturated
# so warm expeditions (Egypt, desert) don't get a cold blue cast from the
# brand palette primary (15,40,65). Falls back to COLOR_PALETTE.primary
# when absent. Used by pipeline/image_processor.py _color_grade().
IMAGE_TINT_COLOR = (40, 42, 48)  # neutral warm-gray, barely perceptible

FILM_GRAIN_OPACITY = 5
FILM_GRAIN_FRAMES = 10
KEN_BURNS_ZOOM_MIN = 10
KEN_BURNS_ZOOM_MAX = 18

# ── Scene pacing ────────────────────────────────────────────────
SCENE_DURATION_MIN = 8
SCENE_DURATION_MAX = 20  # Fewer sub-scenes = less media asset pressure

# ── Render resolution ──────────────────────────────────────────
# 720p to stay within RAM budget on CPU-only renders (18 GB total).
# 1080p + 12 video clips + Ken Burns + vignette + grain exceeds
# the memory guard threshold and triggers mid-render ffmpeg kills.
VIDEO_RESOLUTION = (1280, 720)

# ── Thumbnail style (per-channel coherence) ────────────────────
THUMBNAIL_VISUAL_STYLE = "distress_signal"
THUMBNAIL_STYLE_OVERRIDE = True

# ── Rescue-themed overlay flags ─────────────────────────────────
# Activates visual emergency elements on every thumbnail:
#   - MAYDAY banner across the top
#   - GPS coordinates overlay (bottom-left corner)
#   - "SIN SEÑAL" red stamp (top-right, below 4K badge)
THUMBNAIL_RESCUE_MAYDAY = False           # disabled — too much visual clutter
THUMBNAIL_RESCUE_COORDINATES = True
THUMBNAIL_RESCUE_SIN_SENAL = True

# ── Per-channel concept directive (small face + environment-first) ──
THUMBNAIL_ALLOW_FACES = True
THUMBNAIL_CONCEPT_DIRECTIVE = (
    "El rostro humano con expresion de sorpresa/impacto PUEDE aparecer pero "
    "PEQUEÑO (max ~25-30% del encuadre), integrado en el entorno de la expedicion. "
    "El PAISAJE/ENTORNO de la expedicion es el protagonista y ocupa la mayor parte "
    "de la imagen. EVITA primerisimos planos extremos tipo MrBeast; prioriza planos "
    "medios o generales que muestren la escala epica del entorno. La cara debe ser "
    "un elemento secundario que añade emocion, NO el foco dominante. "
    "Composicion: entorno inmenso (60-70%) + cara pequeña en una esquina o tercio "
    "lateral (25-30%), dejando espacio negativo para texto overlay."
)

# Manual style config for distress_signal
# v2.2 — overhaul for higher CTR: pure white text, boosted contrast, warm-cold pop
THUMBNAIL_MANUAL_STYLE = {
    "visual_style": "distress_signal",
    "color_palette": {
        "primary": "#0F2841",
        "accent": "#FF5C00",
        "text": "#FFFFFF",          # pure white — maximum contrast against dark arctic
        "shadow": "#060C18",
    },
    "base_composition": "dark_reveal",
    "effects": {
        "contrast_boost": 1.20,     # moderate — avoids muddy dark areas
        "saturation": 0.80,         # slightly desaturated — cold, realistic documentary look
        "vignette": 0.35,
    },
    "text_style": {
        "uppercase": True,
        "max_words": 4,
    },
    "pollo_prompt_suffix": (
        "cold desaturated cinematography, arctic survival atmosphere, "
        "emergency orange accents, dramatic natural lighting, photorealistic, "
        "16:9 aspect ratio, National Geographic documentary style, "
        "professional expedition photography, cinematic composition, "
        "epic scale wilderness, no text overlay"
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
    "fallback_query": "dramatic expedition landscape cinematic 16:9",
    "fallback_query_simple": "expedition wilderness dramatic nature",
    "ken_burns_zoom_min": 10,
    "ken_burns_zoom_max": 18,
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
INTRO_BG_COLOR = (6, 12, 24)      # deep navy
OUTRO_DURATION_SEC = 6.0
OUTRO_FONT_SIZE = 52
OUTRO_BG_COLOR = (6, 12, 24)
OUTRO_TEXT = "Suscribete"
OUTRO_CTA_LIKE = "👍 Like"
OUTRO_CTA_SUBSCRIBE = "🔔 Suscribete"
OUTRO_CTA_BELL = "📢 Comparte"
# ── CTA visual text (shown on screen during the CTA segment) ──
CTA_TEXT = (
    "Si estas historias de supervivencia te atrapan,\n"
    "¡dale like, suscribete y activa la campana!"
)
# ── Template voice-over texts (TTS with channel narrator voice) ──
INTRO_VOICE_TEXT = "Bienvenidos a Expediciones sin retorno, donde no todos vuelven para contarlo."
CTA_VOICE_TEXT = "Si has llegado hasta aquí, ya eres parte de la expedición. Suscríbete y no te quedes atrás."
OUTRO_VOICE_TEXT = "Gracias por ver. Nos vemos en la próxima expedición."

CANAL_INITIALS = "ESR"             # Expediciones Sin Retorno
LOGO_SIZE = 180
LOGO_PATH = ""

# ── Vignette ──────────────────────────────────────────────────
# Now hardcoded in video_editor._create_vignette_clip() for uniform subtle effect across all channels

# ═══════════════════════════════════════════════════════════════════
# YOUTUBE METADATA
# ═══════════════════════════════════════════════════════════════════

YT_CATEGORY_ID = "27"              # Education
YT_PRIVACY_STATUS = "public"     # Public — upload directly visible

# Auto-mark videos as AI-generated content after upload (browser automation)
AUTO_MARK_ALTERED_CONTENT = True

# Auto-configure end screens (Subscribe + Video recommendation) after IA mark
AUTO_END_SCREENS = True

# ── Scheduled Publishing ──────────────────────────────────────────
PUBLISH_MODE = "scheduled"
# ── 3-Phase Pipeline (v9) ─────────────────────────────────────────
GENERATION_LEAD_HOURS = 36    # Max hours ahead to generate
# Upload windows (franjas de subida): videos suben en estas franjas a horas random
UPLOAD_WINDOWS = [
    {"start": 10, "end": 13},   # Mañana: 10:00-13:00
    {"start": 20, "end": 22},   # Tarde: 20:00-22:00
]
PUBLISH_TIMEZONE = "Europe/Madrid"
PUBLISH_JITTER_MIN = 20            # ±20 min de variación aleatoria (legacy, reemplazado por PUBLISH_WINDOW_SPREAD_MIN)
PUBLISH_WARMUP_MIN = 120
PUBLISH_WINDOW_SPREAD_MIN = 90     # ±90min alrededor del peak = ventana de publicación de 3h
# PUBLISH_TARGET_HOUR not set — niche heuristic auto-detects (historia_documental → 20:00)

YT_DEFAULT_TAGS = [
    # Tier 1: Primary keywords (broad match)
    "expediciones fallidas",
    "documentales de supervivencia",
    "naufragios historicos",
    "exploracion y aventura",
    "tragedias reales",
    # Tier 2: Named expeditions (high-intent search)
    "expedicion franklin",
    "donner party",
    "everest tragedias",
    "naufragios famosos",
    "exploradores perdidos",
    # Tier 3: Format tags
    "video ensayo historia",
    "documental español supervivencia",
    "historias reales de exploracion",
    "desastres historicos",
    "survival documental español",
    # Tier 4: Long-tail / adjacent
    "historia de la navegacion",
    "accidentes de montaña",
    "supervivencia extrema real",
    "expediciones al artico",
    "historias increibles documental",
]

# ═══════════════════════════════════════════════════════════════════
# SEO
# ═══════════════════════════════════════════════════════════════════

SEO_PRIMARY_KEYWORD = "expediciones fallidas reales"

SEO_SECONDARY_KEYWORDS = [
    # Core niche
    "naufragios historicos documental",
    "exploraciones que salieron mal",
    "tragedias en expediciones",
    "supervivencia extrema documental",
    "explotadores desaparecidos",
    # Named expeditions
    "expedicion franklin documental",
    "donner party historia real",
    "naufragio endurance",
    "everest desastre 1996",
    "exploracion artica tragedias",
    # Format / channel
    "documental supervivencia español",
    "video ensayo exploracion",
    "historias de naufragios",
    "montañas mortales documental",
    # Audience intent
    "historias que inspiran respeto",
    "documentales de aventura real",
    "lo peor de la exploracion",
    "como murieron los exploradores",
    "tragedias maritimas documental",
]

SEO_HASHTAGS = [
    "#ExpedicionesSinRetorno",
    "#Supervivencia",
    "#Documental",
    "#Naufragios",
    "#Historia",
    "#Exploracion",
    "#Aventura",
    "#Naturaleza",
    "#Curiosidades",
    "#HistoriasReales",
    "#AprendeEnYouTube",
    "#Montaña",
    "#Oceano",
    "#Desierto",
    "#Exploradores",
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
    "#ExpedicionesSinRetorno",
    "#Supervivencia",
    "#Shorts",
    "#SabiasQue",
    "#AprendeEnYouTube",
    "#Historia",
    "#Naufragio",
    "#Curiosidades",
    "#Documental",
    "#Aventura",
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

DESCRIPTION_TEMPLATE = """⛵ {titulo}
———

{descripcion_seo}

❄️ EN ESTE VIDEO
- La historia real detras de esta expedicion tragica
- Quienes eran los exploradores y que buscaban
- El momento exacto en que todo salio mal
- Las consecuencias y el legado que dejaron

⏱️ CAPITULOS
{chapters}

———

🎙️ Bienvenido a **Expediciones sin retorno** — el canal donde documentamos las exploraciones que terminaron en desastre: expediciones al Artico atrapadas en el hielo, naufragios en medio del oceano, escaladas imposibles y travesias del desierto que nadie completo. Historias reales de personas que empujaron los limites humanos.

📚 Fuentes: Wikipedia, archivos historicos, hilos de Reddit (r/History, r/Survival, r/shipwrecks) y documentos de exploracion verificados.

⚠️ Todo el contenido tiene fines educativos y de divulgacion historica.

🔔 Suscribete y activa la campana para descubrir mas expediciones que desafiaron a la naturaleza... y perdieron.

💬 ¿Crees que habrias sobrevivido en esta expedicion? Dejalo en los comentarios.

#ExpedicionesSinRetorno #Supervivencia #HistoriasReales"""

# ═══════════════════════════════════════════════════════════════════
# THUMBNAIL
# ═══════════════════════════════════════════════════════════════════

THUMBNAIL_WIDTH = 1280
THUMBNAIL_HEIGHT = 720
THUMBNAIL_FONT_SIZE = 56
THUMBNAIL_BORDER_WIDTH = 7

# ── Per-channel thumbnail customisation ────────────────
THUMBNAIL_FONT_FAMILY = "DejaVuSans-Bold"
THUMBNAIL_BORDER_COLOR = "#FF5C00"       # rescue orange — emergency distress signal
THUMBNAIL_SHOW_4K_BADGE = True
THUMBNAIL_TEXT_STROKE_WIDTH = 3
THUMBNAIL_TEXT_STROKE_COLOR = "#000000"

THUMBNAIL_STYLE = {
    "layout": "image_full_background_text_overlay",
    "max_text_words": 4,
    "text_color": "frost_white_on_arctic_blue",
    "font_style": "bold_sans_serif_condensed",
    "image_treatment": "cold_desaturated_dramatic_contrast_documentary",
    "background": "#060C18",
    "accent_color": "#FF5C00",
    "face_policy": (
        "Real faces (public domain or stock) YES — weathered, cold, "
        "expeditionary expressions. Historical portraits in B&W. "
        "AI-generated faces: NO. Siluetas contra tormenta, manos "
        "congeladas, barcos en el hielo OK."
    ),
    "number_preference": "odd_numbers_for_lists",
    "rescue_orange_accent_rule": (
        "Orange distress accents on danger elements boost CTR. "
        "MAYDAY banner at top, GPS coordinates at bottom-left, "
        "SIN SEÑAL stamp at top-right. Contrast cold blues with "
        "emergency orange. NEVER use yellow or red text."
    ),
}

THUMBNAIL_TEMPLATES = {
    "the_ship": {
        "description": "Ship trapped in ice, dramatic scale, cold blue tones, red overlay text",
        "text_position": "bottom_third",
        "text_words": "2-3",
        "accent": "red_highlight_on_ice_or_ship",
        "best_for": "Naufragios y expediciones maritimas (Franklin, Endurance, Titanic)",
    },
    "the_mountain": {
        "description": "Summit or cliff face, figure for scale, storm clouds, windswept",
        "text_position": "upper_or_center_third",
        "text_words": "2-3",
        "accent": "red_line_on_route_or_summit",
        "best_for": "Expediciones de montaña (Everest, K2, Andes)",
    },
    "the_survivor": {
        "description": "Close-up portrait or silhouette, weather-beaten, hopeful/defeated expression",
        "text_position": "bottom_over_dark_gradient",
        "text_words": "3-4",
        "accent": "red_arrow_or_circle_on_eyes_or_hands",
        "best_for": "Historias de supervivientes y rescates",
    },
}

# ═══════════════════════════════════════════════════════════════════
# VIDEO TIMING & MONETIZATION
# ═══════════════════════════════════════════════════════════════════

VIDEO_MIDROLL_STRATEGY = (
    "Colocar mid-rolls en pausas naturales entre capitulos narrativos. "
    "NUNCA en medio de una frase ni durante el clímax. "
    "Cada mid-roll debe preceder un mini-gancho que mantenga al espectador."
)

MONETIZATION_TARGET_CPM = "$5–$12 USD"

MONETIZATION_VERTICALS = [
    "Aventura y outdoor",
    "Viajes y experiencias",
    "Libros / Audiolibros",
    "Educacion online",
    "Documentales y streaming",
    "Equipamiento de supervivencia",
]

# ═══════════════════════════════════════════════════════════════════
# END SCREEN
# ═══════════════════════════════════════════════════════════════════

END_SCREEN_STRATEGY = {
    "left_card": {
        "type": "playlist",
        "content": "most_relevant_playlist",
        "purpose": "Keep viewer in a survival session — thematic rabbit hole",
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
        "Si esta expedicion te parecio tragica, la siguiente en la lista "
        "lo es todavia mas. Te dejo el enlace en pantalla. "
        "Suscribete si quieres descubrir mas historias de exploracion "
        "que desafiaron a la naturaleza."
    ),
}

# ═══════════════════════════════════════════════════════════════════
# PLAYLISTS
# ═══════════════════════════════════════════════════════════════════

PLAYLISTS = [
    {
        "slug": "tragedias-polares",
        "name": "Tragedias Polares",
        "description": (
            "Expediciones articas y antarticas: Franklin, Shackleton, Scott, "
            "y los exploradores que desafiaron el hielo eterno. Documentales "
            "sobre los limites de la resistencia humana."
        ),
        "type": "thematic",
    },
    {
        "slug": "naufragios-historicos",
        "name": "Naufragios Historicos",
        "description": (
            "Barcos que nunca llegaron a puerto. Tormentas, icebergs, errores "
            "de navegacion y tragedias maritimas que cambiaron la historia."
        ),
        "type": "thematic",
    },
    {
        "slug": "montanas-mortales",
        "name": "Montañas Mortales",
        "description": (
            "Everest, K2, Annapurna, los Andes. Escaladas y expediciones "
            "de alta montaña que terminaron en tragedia."
        ),
        "type": "thematic",
    },
    {
        "slug": "desiertos-y-selvas",
        "name": "Desiertos y Selvas",
        "description": (
            "Travesias terrestres extremas: desiertos, junglas, pantanos. "
            "Lugares donde el calor y la desorientacion mataron exploradores."
        ),
        "type": "thematic",
    },
    {
        "slug": "lo-mas-impactante",
        "name": "Lo Mas Impactante",
        "description": (
            "Las 5 expediciones mas tragicas del canal. Si eres nuevo aqui, "
            "empieza por esta lista. Bienvenido a Expediciones sin retorno."
        ),
        "type": "onboarding",
    },
]

# ═══════════════════════════════════════════════════════════════════
# FIRST 48 HOURS STRATEGY
# ═══════════════════════════════════════════════════════════════════

FIRST_48H_STRATEGY = {
    "pre_upload_24h": [
        "Community Tab poll: '¿En que entorno extremo crees que sobrevivirias menos: Artico, desierto, alta montaña o mar abierto?'",
        "YouTube Story: imagen dramatica + 'Mañana. 9PM MX. Esta historia es real.'",
    ],
    "hour_0": [
        "Publish at 9PM Mexico City time",
        "First comment (immediate, pinned): pregunta sobre dilema de supervivencia",
    ],
    "hours_1_6": [
        "Reddit r/History: TEXT post with compelling summary",
        "Facebook groups: Historia, Documentales, Aventura",
    ],
    "hours_6_24": [
        "Reply to EVERY comment in first 24h",
        "Twitter/X thread: 5-7 tweets contando la expedicion, final tweet = YouTube link",
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
            "example": "¿Que expedicion tragica te parece mas increible?",
            "options": ["Franklin (Artico)", "Donner Party", "Everest 1996", "Barco Endurance"],
        },
        "wednesday": {
            "type": "image_fact",
            "example": (
                "Fotografia de la expedicion + '129 hombres zarparon en 1845 "
                "con los mejores barcos de la epoca. Ninguno volvio a ver "
                "a su familia.'"
            ),
        },
        "friday": {
            "type": "teaser",
            "example": (
                "Este sabado: la expedicion al desierto que ninguna persona "
                "habia intentado antes. Activa la campana."
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
            "{number} personas entraron en {location}. "
            "Solo {survivors} salieron con vida."
        ),
        "structure": "Hook → Contexto (20s) → La catastrofe (15s) → 'Video completo en YouTube, link en bio' (10s)",
        "cadence": "3x/day from one long-form video (days 1, 3, 5)",
    },
    "youtube_shorts": {
        "format": "15-30s most dramatic moment",
        "end_cta": "'Video completo en el canal' linked to long-form",
        "purpose": "Shorts feed → channel page → long-form viewer conversion",
    },
    "twitter_x": {
        "format": "Thread — 1 expedition = 1 thread per week",
        "template": "'HOY en Expediciones sin retorno: {expedition_name}...' + 5-7 tweets + link",
    },
    "spotify_podcast": {
        "format": "Audio-only export of each video",
        "title_format": "Expediciones sin retorno | {expedition_name} | {key_detail}",
        "purpose": "Minimal effort, massive discovery platform",
    },
}

# ═══════════════════════════════════════════════════════════════════
# COLLABORATION TARGETS
# ═══════════════════════════════════════════════════════════════════

COLLABORATION_TARGETS = {
    "tier_1_direct": [
        {"name": "Ciencia de Sofa", "niche": "Divulgacion, comparte audiencia curiosa"},
        {"name": "El Robot de Platon", "niche": "Historia y ciencia, tono documental"},
        {"name": "Antroporama", "niche": "Antropologia y exploracion"},
    ],
    "tier_2_adjacent": [
        {"name": "Mundo Desconocido", "niche": "Misterio y fenomenos, audiencia amplia LATAM"},
    ],
    "collab_formats": [
        "React: 'Un superviviente reacciona a Expediciones sin retorno'",
        "Topic trade: cubrir la expedicion sugerida por otro creador, cross-promote",
    ],
}

# ═══════════════════════════════════════════════════════════════════
# TRENDING TOPIC HOOKS
# ═══════════════════════════════════════════════════════════════════

TRENDING_TOPIC_HOOKS = {
    "type_a_news": {
        "trigger": "Rescate de montañeros, naufragio en noticias",
        "pivot": (
            "Esto que acaba de pasar... ya ocurrio antes. Y fue mucho peor."
        ),
    },
    "type_b_anniversary": {
        "trigger": "Aniversario de expediciones famosas (search spikes)",
        "calendar": {
            "may": "Everest (primera ascension 29 mayo 1953) + desastre 1996",
            "december": "Tragedias navideñas (Donner Party, Shackleton)",
            "april": "Titanic (15 abril) — naufragios en general",
        },
    },
    "type_c_pop_culture": {
        "trigger": "Estreno de pelicula/serie sobre expediciones o supervivencia",
        "strategy": "'La historia REAL detras de {show/movie}'",
        "examples": "The Terror → Franklin Expedition, Society of the Snow → Andes 1972",
    },
    "type_d_calendar": {
        "name": "Calendario de Expediciones Tragicas",
        "months": {
            "january": "Expediciones de año nuevo / Shackleton",
            "march": "Expediciones polares de primavera",
            "may": "Everest y montaña",
            "september": "Huracanes y naufragios",
            "december": "Tragedias navideñas historicas",
        },
    },
}

# ═══════════════════════════════════════════════════════════════════
# CONTENT PILLARS
# ═══════════════════════════════════════════════════════════════════

CONTENT_PILLARS = [
    {
        "name": "La Expedicion",
        "ratio": 55,
        "desc": "Documental profundo de una expedicion tragica individual",
    },
    {
        "name": "Recopilaciones",
        "ratio": 25,
        "desc": "Compilacion tematica: '5 naufragios que cambiaron la historia'",
    },
    {
        "name": "El Analisis",
        "ratio": 20,
        "desc": "Video mas corto analizando que salio mal, lecciones de supervivencia",
    },
]

# ═══════════════════════════════════════════════════════════════════
# VIRAL MIRROR
# ═══════════════════════════════════════════════════════════════════
VIRAL_ENABLED = True
VIRAL_MAX_AGE_DAYS = 29  # Max days since publication (videos older are discarded)

NICHE_KEYWORDS_ENG = [
    "survival stories",
    "expeditions gone wrong",
    "unexplained disappearances",
    "survival documentary",
    "lost in the wilderness",
    "expedition mysteries",
    "true survival stories",
    "missing explorers",
    "wilderness survival documentary",
    "deadliest expeditions",
    "survival against all odds",
    "mysterious disappearances documentary",
]

VIRAL_PLAYLIST_KEYWORDS = {
    "tragedias-polares": [
        "arctic expedition disaster documentary",
        "antarctic survival stories",
        "polar exploration tragedies",
        "franklin expedition documentary",
    ],
    "naufragios-historicos": [
        "shipwreck survival stories",
        "famous maritime disasters",
        "lost ships found documentary",
        "ocean survival true stories",
    ],
    "montanas-mortales": [
        "mount everest disaster documentary",
        "deadliest mountain expeditions",
        "k2 climbing tragedy stories",
        "high altitude survival stories",
    ],
}
