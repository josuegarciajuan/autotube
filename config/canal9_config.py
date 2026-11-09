"""Canal 9 — Un Minuto de Locura (local, sin egress todavía)."""

from config._nuevos_2026 import *  # noqa: F401,F403

# ── Identidad ────────────────────────────────────────────────────
CANAL_NAME = "canal9"
CANAL_DISPLAY_NAME = "Un Minuto de Locura"
CANAL_TAGLINE = "Historias intensas en menos de un minuto."
CANAL_OUTRO_TAGLINE = "Un minuto. Un giro. Ninguna salida fácil."
YOUTUBE_HANDLE = "@UnMinutoDeLocura"
YOUTUBE_CHANNEL_URL = "https://www.youtube.com/@UnMinutoDeLocura"
CANAL_NARRATIVE_STYLE = "microdrama narrativo rápido"
CANAL_STYLE_DESCRIPTION = (
    "Microdramas originales y relatos intensos de menos de un minuto: una "
    "situación límite, una decisión imposible y un giro final. Para long-form, "
    "compilaciones temáticas con hilo narrativo. Nada de morbo gratuito ni "
    "historias que exploten tragedias reales sin contexto."
)
CHANNEL_ABOUT_SECTION = """Bienvenido a Un Minuto de Locura.

Historias intensas, giros inesperados y decisiones imposibles en menos de un minuto. Microdramas originales y relatos basados en hechos verificados, contados con ritmo y sin relleno.

Aquí encontrarás:
- Microdramas con final inesperado.
- Historias reales contadas con respeto.
- Decisiones imposibles y sus consecuencias.
- Terror breve, suspense y drama en formato corto.

Si te gusta que una historia te atrape en sesenta segundos, este es tu canal."""
CHANNEL_KEYWORDS = [
    "un minuto de locura", "microdramas", "historias cortas", "relatos impactantes",
    "giros argumentales", "historias reales", "shorts", "drama", "suspense",
    "historias de terror", "decisiones imposibles", "finales inesperados",
    "microteatro", "relatos cortos", "historias virales", "suspense en un minuto",
]
CANAL_INITIALS = "ML"
LOGO_SIZE = 140

# ── Producción ───────────────────────────────────────────────────
PROD_SCRIPT_WORDS_MIN = 1800
PROD_SCRIPT_WORDS_MAX = 3000
PROD_SCRIPT_SCENES_MIN = 10
PROD_SCRIPT_SCENES_MAX = 16
PROD_VIDEO_DURATION_MIN = 8
PROD_VIDEO_DURATION_MAX = 13
VIDEO_AVERAGE_DURATION_MIN = 10

# ── Audiencia y tono ─────────────────────────────────────────────
CANAL_TONE = (
    "Dramático, directo y adictivo. Cada frase empuja hacia el siguiente segundo. "
    "Se cuida la dignidad de las personas reales: si el relato se basa en un caso, "
    "se anonimiza y se evita el morbo. Los microdramas son ficción original."
)
TARGET_AUDIENCE = (
    "Jóvenes de 16-35 años en España y Latinoamérica, consumidores de shorts, "
    "microdramas, suspense y relatos rápidos."
)
TARGET_AUDIENCE_PSYCHOGRAPHIC = {
    "motivaciones": "Sentir una emoción intensa en poco tiempo y compartir el giro.",
    "miedos": "Aburrirse; que la historia sea predecible o falsa.",
    "habitos": "TikTok, Shorts, Reels; consumen en ráfagas y reenvían.",
    "valores": "Intensidad, justicia narrativa y sorpresa.",
}

# ── Títulos ──────────────────────────────────────────────────────
TITLE_FORMULAS = [
    "Entró a {place}. Lo que encontró cambió todo.",
    "Tenía {time} para decidir. Eligió mal.",
    "Nadie creyó a {person}. Hasta que {event}.",
    "El último mensaje de {person} antes de desaparecer",
    "Un minuto para {action}. {twist}.",
    "Le ofrecieron {thing}. Lo que pasó después no tiene nombre.",
    "La puerta estaba abierta. Ese fue el error.",
    "{person} dijo que estaba solo. No lo estaba.",
    "El giro que nadie vio venir en {case}",
    "Todo terminó en {number} segundos.",
]
TITLE_MAX_CHARS = 65

# ── Voz ──────────────────────────────────────────────────────────
TTS_ENGINE = "edgetts"
VOICE_ID = "es-MX-DaliaNeural"
VOICE_RATE = "+8%"
VOICE_PITCH = "+2Hz"
INTRO_VOICE_TEXT = "Bienvenidos a Un Minuto de Locura. Sesenta segundos. Un giro."

# ── Fuentes ──────────────────────────────────────────────────────
REDDIT_SUBREDDITS = [
    "WritingPrompts", "ShortScaryStories", "TwoSentenceHorror",
    "nosleep", "TrueOffMyChest", "AmITheAsshole", "tifu",
]
WIKIPEDIA_CATEGORIES = [
    "Short stories", "Flash fiction", "Suspense", "Urban legends",
    "Relatos cortos", "Microcuentos",
]
SCRAPE_SOURCES = [
    {"plugin": "reddit", "priority": 1},
    {"plugin": "wikipedia", "priority": 2},
    {"plugin": "rss", "priority": 3},
]
ATLAS_OBSCURA_CATEGORIES = ["unique"]
RSS_FEEDS = []
GOOGLE_NEWS_QUERIES = [
    "historia real increíble", "suceso extraño", "rescate increíble",
    "decisión difícil historia real", "misterio cotidiano",
]

# ── Estilo visual ────────────────────────────────────────────────
COLOR_PALETTE = {
    "primary": (255, 70, 40), "secondary": (25, 10, 40),
    "accent": (255, 210, 0), "text": (250, 245, 240),
    "text_shadow": (0, 0, 0), "tertiary": (45, 20, 60),
    "warning": (255, 120, 40),
}
IMAGE_STYLE_MODIFIERS = (
    "dramatic vertical microdrama, cinematic close-ups, high contrast, red and "
    "yellow accents, intense lighting, shallow depth of field, 9:16, realistic "
    "emotion, no text artifacts"
)
FILM_GRAIN_OPACITY = 6
KEN_BURNS_ZOOM_MIN = 5
KEN_BURNS_ZOOM_MAX = 12
THUMBNAIL_VISUAL_STYLE = (
    "Vertical dramatic close-up, red and yellow palette, bold white text with "
    "black stroke, one clear emotion, high contrast, sense of urgency."
)
THUMBNAIL_MANUAL_STYLE = "Vertical close-up, red/yellow, bold text, urgency, one face or object."
THUMBNAIL_BORDER_COLOR = (255, 70, 40)
THUMBNAIL_TEXT_STROKE_COLOR = (0, 0, 0)
THUMBNAIL_BORDER_WIDTH = 6
THUMBNAIL_FONT_FAMILY = "DejaVuSans-Bold"
THUMBNAIL_SHOW_4K_BADGE = False

# ── Prompts Pollo AI (banner/logo) ───────────────────────────────
AI_NEGATIVE_PROMPT = 'text, letters, words, typography, watermark, logo, signature, blurry, low quality, deformed, extra limbs, cluttered'
AI_BANNER_PROMPT = 'Dramatic cinematic YouTube channel banner background, 16:9, intense red and yellow palette, close-up of a clock and a lightning bolt, high contrast, sense of urgency, cinematic depth, center safe area for title, no text, no letters, no watermark.'
AI_AVATAR_PROMPT = 'Minimalist emblem for a microdrama channel, stylized broken clock combined with a lightning bolt, red and yellow on dark purple, bold geometric, centered, no text, no letters, no watermark, vector-like logo.'
AI_LOGO_STYLE = 'red/yellow, broken clock, lightning, urgency, dramatic, bold geometric'

# ── Intro / CTA / outro ──────────────────────────────────────────
INTRO_FONT_SIZE = 68
INTRO_BG_COLOR = (25, 10, 40)
OUTRO_FONT_SIZE = 58
OUTRO_BG_COLOR = (25, 10, 40)
OUTRO_TEXT = "Suscríbete"
CTA_TEXT = (
    "Si este giro te ha dejado helado, suscríbete para el siguiente.\n"
    "¿Qué habrías hecho tú? Te leemos en comentarios."
)
CTA_VOICE_TEXT = "Si este giro te ha dejado helado, suscríbete. Y cuéntanos qué habrías hecho tú."
OUTRO_VOICE_TEXT = "Gracias por un minuto de locura. Hasta la próxima historia."

# ── YouTube / SEO ────────────────────────────────────────────────
YT_CATEGORY_ID = "24"  # Entertainment
PUBLISH_MODE = "scheduled"
YT_DEFAULT_TAGS = [
    "un minuto de locura", "microdramas", "historias cortas", "relatos",
    "suspense", "giros argumentales", "historias reales", "shorts",
    "drama", "finales inesperados", "español",
]
SEO_PRIMARY_KEYWORD = "microdramas"
SEO_SECONDARY_KEYWORDS = [
    "un minuto de locura", "historias cortas", "relatos impactantes",
    "giros argumentales", "historias con giro", "suspense en un minuto",
]
SEO_HASHTAGS = ["#UnMinutoDeLocura", "#Microdramas", "#Historias", "#Suspense", "#Shorts"]
SHORTS_PER_DAY = 1
SHORTS_HASHTAGS = ["#shorts", "#UnMinutoDeLocura", "#Microdrama", "#Suspense"]

DESCRIPTION_TEMPLATE = """⏱️ {titulo}
———

{descripcion_seo}

📌 EN ESTE VÍDEO
- La situación límite
- La decisión imposible
- El giro que lo cambia todo
- Lo que queda después

———

🎙️ Un Minuto de Locura — historias intensas en menos de un minuto.

📚 Microdramas originales; los relatos basados en hechos reales se anonimizan y se contextualizan.
⚠️ Contenido de ficción/suspense. No se explotan tragedias reales sin respeto.

🔔 Suscríbete para el siguiente giro.
💬 ¿Qué habrías hecho tú? Déjalo en comentarios.

#UnMinutoDeLocura #Microdramas #Historias #Suspense"""

# ── Monetización ─────────────────────────────────────────────────
MONETIZATION_TARGET_CPM = 6.0
MONETIZATION_VERTICALS = [
    "entretenimiento", "streaming", "videojuegos", "apps", "consumo joven",
]

# ── Playlists ────────────────────────────────────────────────────
PLAYLISTS = [
    {"slug": "microdramas", "name": "Microdramas",
     "description": "Historias intensas con giro en menos de un minuto.",
     "type": "main"},
    {"slug": "finales-inesperados", "name": "Finales inesperados",
     "description": "Relatos que terminan donde no esperabas.",
     "type": "thematic"},
    {"slug": "historias-reales", "name": "Historias reales",
     "description": "Casos reales contados con respeto y contexto.",
     "type": "thematic"},
    {"slug": "terror-en-un-minuto", "name": "Terror en un minuto",
     "description": "Suspense y terror breve.",
     "type": "thematic"},
    {"slug": "decisiones-imposibles", "name": "Decisiones imposibles",
     "description": "Elegir mal nunca fue tan fácil.",
     "type": "onboarding"},
]

CONTENT_PILLARS = [
    {"name": "Microdrama original", "ratio": 60,
     "desc": "Ficción breve con situación límite y giro final."},
    {"name": "Historia real verificada", "ratio": 25,
     "desc": "Un caso real, anonimizado y contextualizado."},
    {"name": "Final alternativo", "ratio": 15,
     "desc": "La misma historia con otro desenlace."},
]
NICHE_KEYWORDS_ENG = [
    "micro drama", "short stories", "plot twist", "suspense", "short film",
    "dramatic story", "storytime", "unexpected ending", "thriller short",
    "scary stories", "emotional story", "decision story", "what would you do",
    "short narrative", "viral story",
]
VIRAL_PLAYLIST_KEYWORDS = [
    "plot twist", "micro drama", "short story", "suspense", "unexpected ending",
]
