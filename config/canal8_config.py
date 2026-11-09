"""Canal 8 — Internet Oculto (local, sin egress todavía)."""

from config._nuevos_2026 import *  # noqa: F401,F403

# ── Identidad ────────────────────────────────────────────────────
CANAL_NAME = "canal8"
CANAL_DISPLAY_NAME = "Internet Oculto"
CANAL_TAGLINE = "Misterios, Backrooms y rincones de internet que no deberían existir."
CANAL_OUTRO_TAGLINE = "La red es más profunda de lo que parece."
YOUTUBE_HANDLE = "@InternetOculto"
YOUTUBE_CHANNEL_URL = "https://www.youtube.com/@InternetOculto"
CANAL_NARRATIVE_STYLE = "documental de misterio digital"
CANAL_STYLE_DESCRIPTION = (
    "Documental de misterio digital: casos reales de internet, lost media, "
    "Backrooms, ARGs, webs abandonadas y leyendas de foros. Contexto, fechas "
    "y fuentes; se separa lo documentado de lo especulativo."
)
CHANNEL_ABOUT_SECTION = """Bienvenido a Internet Oculto.

Exploramos los misterios que nacen en la red: Backrooms, lost media, ARGs, webs abandonadas, leyendas de foros y fenómenos digitales que nadie ha explicado del todo.

Cada vídeo documenta lo que se sabe, lo que se dice y lo que sigue sin respuesta. Con fechas, capturas y fuentes, sin morbo fácil.

Aquí encontrarás:
- Casos de internet sin resolver.
- Lost media y archivos recuperados.
- Backrooms, espacios liminales y estética analógica.
- ARGs, webs raras y leyendas de foros.

Si alguna vez te has preguntado qué hay detrás de la red que usas, este es tu canal."""
CHANNEL_KEYWORDS = [
    "internet oculto", "backrooms", "lost media", "misterios de internet",
    "arg", "creepypasta", "webs abandonadas", "deep web", "leyendas urbanas",
    "foros misteriosos", "videos raros", "internet misterioso",
    "analog horror", "liminal spaces", "unexplained internet",
    "archivos perdidos", "misterio digital", "documental de internet",
]
CANAL_INITIALS = "IO"
LOGO_SIZE = 140

# ── Producción ───────────────────────────────────────────────────
PROD_SCRIPT_WORDS_MIN = 2000
PROD_SCRIPT_WORDS_MAX = 3400
PROD_SCRIPT_SCENES_MIN = 10
PROD_SCRIPT_SCENES_MAX = 18
PROD_VIDEO_DURATION_MIN = 9
PROD_VIDEO_DURATION_MAX = 15
VIDEO_AVERAGE_DURATION_MIN = 11

# ── Audiencia y tono ─────────────────────────────────────────────
CANAL_TONE = (
    "Intrigante, cinematográfico y preciso. El misterio se construye con datos, "
    "no con gritos. Se distingue claramente entre hechos documentados, teorías "
    "y especulación. Nada de contenido ilegal, gore o acoso."
)
TARGET_AUDIENCE = (
    "Jóvenes y adultos de 16-35 años en España y Latinoamérica, consumidores de "
    "misterio, creepypasta, ARGs y cultura de internet."
)
TARGET_AUDIENCE_PSYCHOGRAPHIC = {
    "motivaciones": "Explorar lo desconocido y sentir que descubren algo oculto.",
    "miedos": "Que el contenido sea falso, sensacionalista o repetido.",
    "habitos": "YouTube, TikTok, foros, wikis y comunidades de misterio.",
    "valores": "Curiosidad, escepticismo y respeto por las fuentes.",
}

# ── Títulos ──────────────────────────────────────────────────────
TITLE_FORMULAS = [
    "El misterio de {case} que internet no pudo resolver",
    "Backrooms: {detail}",
    "La lost media de {thing} que apareció {years} años después",
    "{number} webs que no deberían existir",
    "El ARG que se volvió real: {case}",
    "Nadie sabe quién creó {case}",
    "La historia oculta detrás de {case}",
    "{case}: lo que se sabe y lo que no",
    "El vídeo de {case} que nadie ha podido explicar",
    "¿Qué pasó realmente con {case}?",
]
TITLE_MAX_CHARS = 65

# ── Voz ──────────────────────────────────────────────────────────
TTS_ENGINE = "edgetts"
VOICE_ID = "es-ES-ElviraNeural"
VOICE_RATE = "-6%"
VOICE_PITCH = "-2Hz"
INTRO_VOICE_TEXT = "Bienvenidos a Internet Oculto, donde exploramos los misterios que la red no ha terminado de explicar."

# ── Fuentes ──────────────────────────────────────────────────────
REDDIT_SUBREDDITS = [
    "InternetMysteries", "Backrooms", "UnresolvedMysteries", "lostmedia",
    "ARG", "creepy", "LiminalSpace", "AnalogHorror", "ObscureMedia",
    "tipofmytongue",
]
WIKIPEDIA_CATEGORIES = [
    "Backrooms", "Lost media", "Alternate reality games", "Creepypasta",
    "Deep web", "Internet memes", "Urban legends", "Liminal spaces",
    "Internet mysteries", "Misterios de internet",
]
SCRAPE_SOURCES = [
    {"plugin": "reddit", "priority": 1},
    {"plugin": "wikipedia", "priority": 2},
    {"plugin": "atlas_obscura", "priority": 3},
    {"plugin": "rss", "priority": 4},
    {"plugin": "google_news", "priority": 5},
]
ATLAS_OBSCURA_CATEGORIES = ["unique", "technology", "history"]
RSS_FEEDS = []
GOOGLE_NEWS_QUERIES = [
    "backrooms misterio", "lost media encontrado", "misterio internet",
    "arg viral explicado", "web abandonada misteriosa", "video raro internet",
]

# ── Estilo visual ────────────────────────────────────────────────
COLOR_PALETTE = {
    "primary": (0, 220, 180), "secondary": (8, 10, 18),
    "accent": (200, 40, 160), "text": (230, 240, 245),
    "text_shadow": (0, 0, 0), "tertiary": (20, 28, 38),
    "warning": (255, 200, 60),
}
IMAGE_STYLE_MODIFIERS = (
    "liminal space photography, glitch aesthetic, CRT scanlines, dark web "
    "mystery, neon green and magenta accents, cinematic, eerie, 16:9, "
    "high contrast, atmospheric, no text artifacts"
)
FILM_GRAIN_OPACITY = 10
KEN_BURNS_ZOOM_MIN = 3
KEN_BURNS_ZOOM_MAX = 9
THUMBNAIL_VISUAL_STYLE = (
    "Dark liminal corridor or glitch screen, neon green/magenta accents, "
    "central eerie object, bold white text, VHS/CRT texture, high contrast, "
    "mystery and depth."
)
THUMBNAIL_MANUAL_STYLE = "Liminal/glitch, neon green/magenta, bold text, VHS texture."
THUMBNAIL_BORDER_COLOR = (0, 220, 180)
THUMBNAIL_TEXT_STROKE_COLOR = (0, 0, 0)
THUMBNAIL_BORDER_WIDTH = 6
THUMBNAIL_FONT_FAMILY = "DejaVuSans-Bold"
THUMBNAIL_SHOW_4K_BADGE = False

# ── Intro / CTA / outro ──────────────────────────────────────────
INTRO_FONT_SIZE = 62
INTRO_BG_COLOR = (8, 10, 18)
OUTRO_FONT_SIZE = 54
OUTRO_BG_COLOR = (8, 10, 18)
OUTRO_TEXT = "Suscríbete"
CTA_TEXT = (
    "Si este misterio te ha atrapado, suscríbete para el siguiente.\n"
    "¿Conoces otro rincón oculto de internet? Cuéntanoslo en comentarios."
)
CTA_VOICE_TEXT = "Si te ha atrapado este misterio, suscríbete. Y cuéntanos qué otro rincón de internet deberíamos investigar."
OUTRO_VOICE_TEXT = "Gracias por explorar con nosotros. La red es más profunda de lo que parece."

# ── YouTube / SEO ────────────────────────────────────────────────
YT_CATEGORY_ID = "24"  # Entertainment
PUBLISH_MODE = "scheduled"
YT_DEFAULT_TAGS = [
    "internet oculto", "backrooms", "lost media", "misterios de internet",
    "arg", "creepypasta", "leyendas urbanas", "deep web", "analog horror",
    "liminal spaces", "documental", "español",
]
SEO_PRIMARY_KEYWORD = "misterios de internet"
SEO_SECONDARY_KEYWORDS = [
    "internet oculto", "backrooms", "lost media", "args explicados",
    "leyendas de internet", "webs misteriosas",
]
SEO_HASHTAGS = ["#InternetOculto", "#Backrooms", "#LostMedia", "#Misterio", "#ARG"]
SHORTS_PER_DAY = 1
SHORTS_HASHTAGS = ["#shorts", "#InternetOculto", "#Backrooms", "#Misterio"]

DESCRIPTION_TEMPLATE = """🌐 {titulo}
———

{descripcion_seo}

📌 EN ESTE VÍDEO
- Qué se sabe del caso
- Cómo nació y dónde apareció
- Las teorías y las pruebas
- Lo que sigue sin explicación

———

🎙️ Internet Oculto — misterios, Backrooms y rincones de la red que no deberían existir.

📚 Fuentes: archivos, capturas, hilos originales, wikis y medios especializados.
⚠️ Distinguimos hechos documentados de especulación. No publicamos contenido ilegal ni acoso.

🔔 Suscríbete para el siguiente misterio.
💬 ¿Conoces otro caso? Déjalo en comentarios.

#InternetOculto #Backrooms #LostMedia #Misterio"""

# ── Monetización ─────────────────────────────────────────────────
MONETIZATION_TARGET_CPM = 6.5
MONETIZATION_VERTICALS = [
    "entretenimiento", "tecnología", "videojuegos", "cultura digital", "educación",
]

# ── Playlists ────────────────────────────────────────────────────
PLAYLISTS = [
    {"slug": "backrooms-liminal", "name": "Backrooms y liminal",
     "description": "Backrooms, espacios liminales y estética analógica.",
     "type": "main"},
    {"slug": "lost-media", "name": "Lost media",
     "description": "Archivos, vídeos y webs que se creían perdidos.",
     "type": "thematic"},
    {"slug": "args-webs-raras", "name": "ARGs y webs raras",
     "description": "Juegos de realidad alternativa y webs que no deberían existir.",
     "type": "thematic"},
    {"slug": "leyendas-de-foros", "name": "Leyendas de foros",
     "description": "Historias que nacieron en foros y nunca se olvidaron.",
     "type": "thematic"},
    {"slug": "internet-sin-explicar", "name": "Internet sin explicar",
     "description": "Casos que siguen abiertos.",
     "type": "onboarding"},
]

CONTENT_PILLARS = [
    {"name": "Caso de internet", "ratio": 50,
     "desc": "Un misterio digital con contexto, fechas y fuentes."},
    {"name": "Lost media", "ratio": 30,
     "desc": "Archivos perdidos, recuperados o nunca encontrados."},
    {"name": "Leyendas y ARGs", "ratio": 20,
     "desc": "Historias de foros, juegos de realidad alternativa y webs raras."},
]
NICHE_KEYWORDS_ENG = [
    "internet mysteries", "backrooms", "lost media", "arg", "creepypasta",
    "unexplained internet", "liminal spaces", "analog horror", "deep web",
    "abandoned websites", "internet legends", "obscure media",
    "cursed images", "web mystery", "digital folklore",
]
VIRAL_PLAYLIST_KEYWORDS = [
    "backrooms", "lost media", "internet mystery", "arg", "creepy",
]
