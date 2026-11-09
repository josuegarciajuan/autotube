"""Canal 6 — Viral o Falso (local, sin egress todavía)."""

from config._nuevos_2026 import *  # noqa: F401,F403  (estructuras comunes)

# ── Identidad ────────────────────────────────────────────────────
CANAL_NAME = "canal6"
CANAL_DISPLAY_NAME = "Viral o Falso"
CANAL_TAGLINE = "Lo que se hace viral no siempre es verdad. Lo comprobamos."
CANAL_OUTRO_TAGLINE = "No te fíes del titular. Comprueba la fuente."
YOUTUBE_HANDLE = "@ViralOFalso"
YOUTUBE_CHANNEL_URL = "https://www.youtube.com/@ViralOFalso"
CANAL_NARRATIVE_STYLE = "fact-checking documental rápido"
CANAL_STYLE_DESCRIPTION = (
    "Periodismo de verificación en formato documental corto: se presenta la "
    "afirmación viral, se rastrean sus fuentes, se muestran las pruebas y se "
    "llega a un veredicto claro. Tono sobrio, sin gritos ni sensacionalismo."
)
CHANNEL_ABOUT_SECTION = """Bienvenido a Viral o Falso.

Cada semana analizamos los vídeos, capturas y noticias que más se comparten para separar lo verdadero de lo falso. Mostramos las fuentes, explicamos el contexto y te damos las claves para no caer en bulos.

Sin gritos, sin milagros y sin titulares engañosos: solo verificación.

Aquí encontrarás:
- Casos virales de la semana, comprobados.
- Bulos clásicos que siguen circulando.
- Deepfakes, IA y manipulaciones explicadas.
- Herramientas para verificar por ti mismo.

Si quieres saber si algo es real antes de compartirlo, este es tu canal."""
CHANNEL_KEYWORDS = [
    "viral o falso", "verificación de noticias", "fact checking", "bulos",
    "fake news", "noticias falsas", "desmentir bulos", "verificar vídeos",
    "periodismo", "alfabetización mediática", "deepfakes", "IA",
    "manipulación digital", "redes sociales", "actualidad", "comprobar fuentes",
    "engaños virales", "misinformation",
]
CANAL_INITIALS = "VF"
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
    "Periodístico, escéptico y claro. Explica sin condescendencia, muestra las "
    "fuentes y distingue hechos de interpretaciones. No ridiculiza a quien cae "
    "en un bulo: le enseña a comprobarlo."
)
TARGET_AUDIENCE = (
    "Adultos de 25-55 años en España y Latinoamérica, con hábitos digitales, "
    "que consumen redes sociales y desconfían de la desinformación."
)
TARGET_AUDIENCE_PSYCHOGRAPHIC = {
    "motivaciones": "Entender la actualidad y no ser engañados.",
    "miedos": "Compartir algo falso y quedar mal; ser manipulados.",
    "habitos": "WhatsApp, X, TikTok y YouTube; leen titulares y comparten rápido.",
    "valores": "Pensamiento crítico, transparencia y fuentes verificables.",
}

# ── Títulos ──────────────────────────────────────────────────────
TITLE_FORMULAS = [
    "¿Es real {claim}? Lo comprobamos",
    "{claim}: la verdad detrás del vídeo viral",
    "No, {claim} no es lo que parece",
    "El bulo de {topic} que todos compartieron",
    "{number} señales de que {claim} es falso",
    "Lo que {source} no te contó sobre {claim}",
    "Verificado: {claim} es {verdict}",
    "La manipulación de {topic} que se hizo viral",
    "¿Quién creó el bulo de {claim}?",
    "{claim}: lo que dice la evidencia",
]
TITLE_MAX_CHARS = 65

# ── Voz ──────────────────────────────────────────────────────────
TTS_ENGINE = "edgetts"
VOICE_ID = "es-ES-AlvaroNeural"
VOICE_RATE = "+2%"
VOICE_PITCH = "+0Hz"
INTRO_VOICE_TEXT = "Bienvenidos a Viral o Falso, el canal donde comprobamos lo que se hace viral."

# ── Fuentes ──────────────────────────────────────────────────────
REDDIT_SUBREDDITS = [
    "IsItBullshit", "OutOfTheLoop", "InternetMyths", "QuitYourBullshit",
    "DataIsBeautiful", "science", "todayilearned", "worldnews",
]
WIKIPEDIA_CATEGORIES = [
    "Fact-checking", "Misinformation", "Urban legends", "Media manipulation",
    "Deepfakes", "Internet fraud", "Conspiracy theories", "Propaganda",
    "Verificación de hechos", "Desinformación",
]
SCRAPE_SOURCES = [
    {"plugin": "reddit", "priority": 1},
    {"plugin": "wikipedia", "priority": 2},
    {"plugin": "rss", "priority": 3},
    {"plugin": "google_news", "priority": 4},
]
ATLAS_OBSCURA_CATEGORIES = ["unique", "technology"]
RSS_FEEDS = []
GOOGLE_NEWS_QUERIES = [
    "bulo viral desmentido", "fake news verificada", "noticia falsa desmentida",
    "verificación viral", "manipulación vídeo viral", "deepfake desmentido",
]

# ── Estilo visual ────────────────────────────────────────────────
COLOR_PALETTE = {
    "primary": (200, 30, 45), "secondary": (12, 20, 38),
    "accent": (0, 180, 220), "text": (245, 245, 250),
    "text_shadow": (4, 6, 12), "tertiary": (30, 40, 55),
    "warning": (255, 190, 60),
}
IMAGE_STYLE_MODIFIERS = (
    "periodistic documentary photography, fact-checking graphics, split screen "
    "true/false, clean newsroom lighting, red and cyan accents, realistic, "
    "16:9, high contrast, sharp, no text artifacts"
)
FILM_GRAIN_OPACITY = 3
KEN_BURNS_ZOOM_MIN = 3
KEN_BURNS_ZOOM_MAX = 8
THUMBNAIL_VISUAL_STYLE = (
    "Split composition: left side 'VIRAL' with red tint, right side 'FALSO' "
    "with cyan tint, central magnifying glass or evidence marker, bold white "
    "text, high contrast, documentary news aesthetic."
)
THUMBNAIL_MANUAL_STYLE = "Red/cyan split, evidence, magnifying glass, bold sans-serif, no clutter."
THUMBNAIL_BORDER_COLOR = (200, 30, 45)
THUMBNAIL_TEXT_STROKE_COLOR = (0, 0, 0)
THUMBNAIL_BORDER_WIDTH = 6
THUMBNAIL_FONT_FAMILY = "DejaVuSans-Bold"
THUMBNAIL_SHOW_4K_BADGE = False

# ── Prompts Pollo AI (banner/logo) ───────────────────────────────
AI_NEGATIVE_PROMPT = 'text, letters, words, typography, watermark, logo, signature, blurry, low quality, deformed, extra limbs, cluttered'
AI_BANNER_PROMPT = 'Cinematic YouTube channel banner background, 16:9, dramatic split composition red versus cyan, a large magnifying glass over fragmented news screens, evidence markers, subtle digital glitch, clean newsroom atmosphere, high contrast, professional documentary style, no text, no letters, no watermark, center safe area for title.'
AI_AVATAR_PROMPT = 'Minimalist emblem for a fact-checking channel, a stylized magnifying glass combined with a check mark and an X, red and cyan palette, dark navy background, bold geometric shapes, high contrast, centered, no text, no letters, no watermark, clean vector-like logo.'
AI_LOGO_STYLE = 'red/cyan split, evidence, magnifying glass, check/cross, newsroom, bold geometric'

# ── Intro / CTA / outro ──────────────────────────────────────────
INTRO_FONT_SIZE = 64
INTRO_BG_COLOR = (12, 20, 38)
OUTRO_FONT_SIZE = 56
OUTRO_BG_COLOR = (12, 20, 38)
OUTRO_TEXT = "Suscríbete"
CTA_TEXT = (
    "Antes de compartir, comprueba. Si este vídeo te ha ayudado, suscríbete.\n"
    "¿Has visto otro bulo? Déjalo en comentarios y lo investigamos."
)
CTA_VOICE_TEXT = "Antes de compartir, comprueba. Suscríbete para más verificaciones."
OUTRO_VOICE_TEXT = "Gracias por ver. No te fíes del titular: comprueba la fuente."

# ── YouTube / SEO ────────────────────────────────────────────────
YT_CATEGORY_ID = "25"  # News & Politics
PUBLISH_MODE = "scheduled"
YT_DEFAULT_TAGS = [
    "viral o falso", "verificación", "fact checking", "bulos", "fake news",
    "noticias falsas", "desinformación", "deepfake", "verificar vídeos",
    "periodismo", "actualidad", "español",
]
SEO_PRIMARY_KEYWORD = "verificar noticias virales"
SEO_SECONDARY_KEYWORDS = [
    "viral o falso", "bulos desmentidos", "fact checking español",
    "fake news", "deepfakes", "cómo verificar una noticia",
]
SEO_HASHTAGS = ["#ViralOFalso", "#FactChecking", "#Bulos", "#FakeNews", "#Verificación"]
SHORTS_PER_DAY = 1
SHORTS_HASHTAGS = ["#shorts", "#ViralOFalso", "#Bulos", "#SabíasQue"]

DESCRIPTION_TEMPLATE = """🔎 {titulo}
———

{descripcion_seo}

📌 EN ESTE VÍDEO
- Qué se hizo viral y por qué
- Qué dicen las fuentes originales
- Dónde está la manipulación o el matiz
- Veredicto y cómo comprobarlo tú mismo

———

🎙️ Viral o Falso — comprobamos lo que se comparte sin verificar.

📚 Fuentes: medios originales, comunicados, verificadores independientes, registros públicos y archivos.
⚠️ Si detectas un error, escríbenos: corregimos con transparencia.

🔔 Suscríbete para no compartir bulos.
💬 ¿Has visto otro caso? Déjalo en comentarios.

#ViralOFalso #FactChecking #Bulos #FakeNews"""

# ── Monetización ─────────────────────────────────────────────────
MONETIZATION_TARGET_CPM = 8.0
MONETIZATION_VERTICALS = [
    "medios y educación", "tecnología e IA", "seguridad digital",
    "finanzas responsables", "consumo",
]

# ── Playlists ────────────────────────────────────────────────────
PLAYLISTS = [
    {"slug": "viral-o-falso-casos", "name": "Viral o Falso — Casos",
     "description": "Los casos virales más compartidos, comprobados uno a uno.",
     "type": "main"},
    {"slug": "bulos-clasicos", "name": "Bulos Clásicos",
     "description": "Mentiras que siguen circulando años después.",
     "type": "thematic"},
    {"slug": "deepfakes-ia", "name": "Deepfakes e IA",
     "description": "Vídeos manipulados y cómo detectarlos.",
     "type": "thematic"},
    {"slug": "verificacion-paso-a-paso", "name": "Verificación Paso a Paso",
     "description": "Herramientas y métodos para comprobar una noticia.",
     "type": "onboarding"},
    {"slug": "actualidad-desmentida", "name": "Actualidad Desmentida",
     "description": "Los bulos de la semana, explicados.",
     "type": "thematic"},
]

CONTENT_PILLARS = [
    {"name": "Caso viral de la semana", "ratio": 50,
     "desc": "Una afirmación viral, sus fuentes y el veredicto."},
    {"name": "Bulos clásicos", "ratio": 30,
     "desc": "Mentiras recurrentes que conviene conocer."},
    {"name": "Herramientas de verificación", "ratio": 20,
     "desc": "Cómo comprobar vídeos, imágenes y cuentas."},
]
NICHE_KEYWORDS_ENG = [
    "fact checking", "viral claims", "fake news", "misinformation",
    "debunking", "deepfake detection", "media literacy", "internet myths",
    "verification", "viral video analysis", "hoax", "disinformation",
    "news verification", "ai manipulation", "fact check video",
]
VIRAL_PLAYLIST_KEYWORDS = [
    "fact checking", "viral news", "debunked", "fake news", "deepfake",
]
