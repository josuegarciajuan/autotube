"""Canal 7 — ¿Sabías Esto? (local, sin egress todavía)."""

from config._nuevos_2026 import *  # noqa: F401,F403

# ── Identidad ────────────────────────────────────────────────────
CANAL_NAME = "canal7"
CANAL_DISPLAY_NAME = "¿Sabías Esto?"
CANAL_TAGLINE = "Datos, curiosidades e historias que no conocías."
CANAL_OUTRO_TAGLINE = "Cada vídeo, un dato nuevo para contar."
YOUTUBE_HANDLE = "@SabiasEsto"
YOUTUBE_CHANNEL_URL = "https://www.youtube.com/@SabiasEsto"
CANAL_NARRATIVE_STYLE = "curiosidades documentales de asombro"
CANAL_STYLE_DESCRIPTION = (
    "Documental de curiosidades rápido y visual: un dato sorprendente, su "
    "contexto y una explicación clara. Mezcla ciencia, historia, naturaleza y "
    "rarezas cotidianas con fuentes trazables."
)
CHANNEL_ABOUT_SECTION = """Bienvenido a ¿Sabías Esto?

Un canal para descubrir datos, curiosidades e historias que casi nadie conoce. Cada vídeo te deja una idea nueva para contar en una cena: ciencia, historia, naturaleza, tecnología y rarezas del mundo, explicadas en minutos y con fuentes.

Aquí encontrarás:
- Datos que no sabías sobre cosas que usas todos los días.
- Historias sorprendentes de la historia y la ciencia.
- Curiosidades de animales, lugares y culturas.
- Explicaciones claras, sin relleno.

Si te gusta aprender algo nuevo cada día, este es tu canal."""
CHANNEL_KEYWORDS = [
    "sabías esto", "datos curiosos", "curiosidades", "cosas que no sabías",
    "datos asombrosos", "historia", "ciencia", "naturaleza", "tecnología",
    "rarezas del mundo", "cultura general", "aprender cosas nuevas",
    "datos impactantes", "curiosidades del mundo", "documental corto",
    "¿sabías que", "datos históricos", "ciencia para curiosos",
]
CANAL_INITIALS = "SE"
LOGO_SIZE = 140

# ── Producción ───────────────────────────────────────────────────
PROD_SCRIPT_WORDS_MIN = 1900
PROD_SCRIPT_WORDS_MAX = 3200
PROD_SCRIPT_SCENES_MIN = 10
PROD_SCRIPT_SCENES_MAX = 16
PROD_VIDEO_DURATION_MIN = 8
PROD_VIDEO_DURATION_MAX = 14
VIDEO_AVERAGE_DURATION_MIN = 10

# ── Audiencia y tono ─────────────────────────────────────────────
CANAL_TONE = (
    "Cercano, rápido y asombrado. Explica como si le contaras un dato increíble "
    "a un amigo: primero el gancho, luego el contexto y al final el porqué. "
    "Sin pedantería y sin exagerar lo que no está demostrado."
)
TARGET_AUDIENCE = (
    "Jóvenes y adultos de 18-45 años en España y Latinoamérica, curiosos "
    "generales que consumen vídeos cortos y documentales ligeros."
)
TARGET_AUDIENCE_PSYCHOGRAPHIC = {
    "motivaciones": "Aprender algo nuevo y tener temas interesantes que contar.",
    "miedos": "Perder el tiempo con contenido repetitivo o falso.",
    "habitos": "YouTube, TikTok, Instagram; consumen en móvil y comparten datos.",
    "valores": "Curiosidad, aprendizaje continuo y conversación.",
}

# ── Títulos ──────────────────────────────────────────────────────
TITLE_FORMULAS = [
    "¿Sabías que {fact}?",
    "El dato de {topic} que casi nadie conoce",
    "{number} cosas que no sabías sobre {topic}",
    "Por qué {fenomeno} ocurre (y no es lo que crees)",
    "La historia de {thing} en {minutes} minutos",
    "{topic}: el detalle que lo cambia todo",
    "Lo que {thing} esconde desde hace {years} años",
    "¿Por qué {question}? La respuesta te va a sorprender",
    "{number} datos de {topic} que parecen mentira",
    "El origen de {thing} que nunca te contaron",
]
TITLE_MAX_CHARS = 65

# ── Voz ──────────────────────────────────────────────────────────
TTS_ENGINE = "edgetts"
VOICE_ID = "es-MX-JorgeNeural"
VOICE_RATE = "+0%"
VOICE_PITCH = "+2Hz"
INTRO_VOICE_TEXT = "Bienvenidos a ¿Sabías Esto?, el canal donde cada vídeo te deja un dato nuevo."

# ── Fuentes ──────────────────────────────────────────────────────
REDDIT_SUBREDDITS = [
    "todayilearned", "interestingasfuck", "Damnthatsinteresting",
    "YouShouldKnow", "AskHistorians", "science", "NatureIsFuckingLit",
    "HistoryMemes", "coolguides", "educationalgifs",
]
WIKIPEDIA_CATEGORIES = [
    "Curiosities", "Lists of unsolved problems", "History", "Science",
    "Nature", "Technology", "Culture", "World records",
    "Curiosidades", "Historia", "Ciencia", "Naturaleza",
]
SCRAPE_SOURCES = [
    {"plugin": "reddit", "priority": 1},
    {"plugin": "wikipedia", "priority": 2},
    {"plugin": "atlas_obscura", "priority": 3},
    {"plugin": "rss", "priority": 4},
    {"plugin": "google_news", "priority": 5},
]
ATLAS_OBSCURA_CATEGORIES = ["history", "science", "unique", "technology"]
RSS_FEEDS = []
GOOGLE_NEWS_QUERIES = [
    "curiosidad científica", "dato histórico sorprendente", "descubrimiento asombroso",
    "récord mundial curioso", "curiosidad animal", "dato desconocido",
]

# ── Estilo visual ────────────────────────────────────────────────
COLOR_PALETTE = {
    "primary": (250, 200, 40), "secondary": (20, 35, 70),
    "accent": (255, 120, 30), "text": (250, 250, 250),
    "text_shadow": (8, 10, 18), "tertiary": (40, 55, 85),
    "warning": (255, 90, 60),
}
IMAGE_STYLE_MODIFIERS = (
    "bright curiosity documentary, bold graphic facts, colorful infographics, "
    "dynamic composition, editorial photography, 16:9, high saturation, clean, "
    "sharp, no text artifacts"
)
FILM_GRAIN_OPACITY = 2
KEN_BURNS_ZOOM_MIN = 4
KEN_BURNS_ZOOM_MAX = 10
THUMBNAIL_VISUAL_STYLE = (
    "Big bold number or question mark, central object, bright yellow and blue "
    "palette, clean infographic style, white text with dark stroke, high "
    "curiosity, no clutter."
)
THUMBNAIL_MANUAL_STYLE = "Yellow/blue, big number, question mark, bold sans-serif, high contrast."
THUMBNAIL_BORDER_COLOR = (250, 200, 40)
THUMBNAIL_TEXT_STROKE_COLOR = (20, 35, 70)
THUMBNAIL_BORDER_WIDTH = 6
THUMBNAIL_FONT_FAMILY = "DejaVuSans-Bold"
THUMBNAIL_SHOW_4K_BADGE = False

# ── Intro / CTA / outro ──────────────────────────────────────────
INTRO_FONT_SIZE = 66
INTRO_BG_COLOR = (20, 35, 70)
OUTRO_FONT_SIZE = 56
OUTRO_BG_COLOR = (20, 35, 70)
OUTRO_TEXT = "Suscríbete"
CTA_TEXT = (
    "Si hoy has aprendido algo nuevo, suscríbete y activa la campana.\n"
    "¿Conocías este dato? Cuéntanoslo en comentarios."
)
CTA_VOICE_TEXT = "Si has aprendido algo nuevo, suscríbete. Y cuéntanos qué dato te ha sorprendido más."
OUTRO_VOICE_TEXT = "Gracias por aprender con nosotros. Hasta el próximo dato."

# ── YouTube / SEO ────────────────────────────────────────────────
YT_CATEGORY_ID = "24"  # Entertainment
PUBLISH_MODE = "scheduled"
YT_DEFAULT_TAGS = [
    "sabías esto", "datos curiosos", "curiosidades", "datos asombrosos",
    "historia", "ciencia", "naturaleza", "cultura general", "documental",
    "aprender", "español", "datos impactantes",
]
SEO_PRIMARY_KEYWORD = "datos curiosos"
SEO_SECONDARY_KEYWORDS = [
    "sabías esto", "curiosidades del mundo", "datos asombrosos",
    "cosas que no sabías", "historia sorprendente", "ciencia para curiosos",
]
SEO_HASHTAGS = ["#SabíasEsto", "#DatosCuriosos", "#Curiosidades", "#Historia", "#Ciencia"]
SHORTS_PER_DAY = 1
SHORTS_HASHTAGS = ["#shorts", "#SabíasEsto", "#DatosCuriosos", "#Curiosidades"]

DESCRIPTION_TEMPLATE = """💡 {titulo}
———

{descripcion_seo}

📌 EN ESTE VÍDEO
- El dato que casi nadie conoce
- De dónde viene y por qué importa
- La explicación sin relleno
- Una curiosidad extra para contar

———

🎙️ ¿Sabías Esto? — datos, curiosidades e historias que no conocías.

📚 Fuentes: Wikipedia, artículos divulgativos, archivos históricos y medios especializados.
⚠️ Si detectas un error, escríbenos: corregimos con transparencia.

🔔 Suscríbete para aprender algo nuevo cada día.
💬 ¿Ya lo sabías? Déjanos tu comentario.

#SabíasEsto #DatosCuriosos #Curiosidades #Historia"""

# ── Monetización ─────────────────────────────────────────────────
MONETIZATION_TARGET_CPM = 7.0
MONETIZATION_VERTICALS = [
    "educación", "tecnología", "consumo", "viajes", "cultura",
]

# ── Playlists ────────────────────────────────────────────────────
PLAYLISTS = [
    {"slug": "datos-que-no-sabias", "name": "Datos que no sabías",
     "description": "Curiosidades sorprendentes explicadas en minutos.",
     "type": "main"},
    {"slug": "historia-en-minutos", "name": "Historia en minutos",
     "description": "Historias y hechos históricos que parecen increíbles.",
     "type": "thematic"},
    {"slug": "ciencia-para-curiosos", "name": "Ciencia para curiosos",
     "description": "Fenómenos científicos explicados de forma sencilla.",
     "type": "thematic"},
    {"slug": "rarezas-del-mundo", "name": "Rarezas del mundo",
     "description": "Lugares, animales y objetos que no parecen reales.",
     "type": "thematic"},
    {"slug": "sabias-que-shorts", "name": "¿Sabías que? — Shorts",
     "description": "Datos rápidos para llevar en el bolsillo.",
     "type": "onboarding"},
]

CONTENT_PILLARS = [
    {"name": "Dato que no sabías", "ratio": 50,
     "desc": "Una curiosidad concreta con contexto y explicación."},
    {"name": "Historia sorprendente", "ratio": 30,
     "desc": "Un episodio histórico con un detalle inesperado."},
    {"name": "Ciencia cotidiana", "ratio": 20,
     "desc": "Por qué ocurre algo que vemos todos los días."},
]
NICHE_KEYWORDS_ENG = [
    "did you know", "fun facts", "interesting facts", "curiosities",
    "history facts", "science facts", "amazing facts", "trivia",
    "world records", "nature facts", "educational video", "things you didn't know",
    "mind blowing facts", "curiosity", "explained",
]
VIRAL_PLAYLIST_KEYWORDS = [
    "fun facts", "did you know", "curiosities", "history", "science",
]
