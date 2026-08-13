"""Configuration for Canal 3: Civilizaciones Olvidadas.

Only CHANNEL-SPECIFIC parameters. Everything else inherits from config.defaults.py.
"""
from config.settings import DEFAULT_VIDEO_PROVIDERS

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
YOUTUBE_HANDLE = "@CivilizacionesOlvidadas-r7f"
YOUTUBE_CHANNEL_URL = "https://www.youtube.com/@CivilizacionesOlvidadas-r7f"
CANAL_NARRATIVE_STYLE = "documental arqueológico"
CANAL_STYLE_DESCRIPTION = (
    "Las civilizaciones que el tiempo borró, los templos que la jungla "
    "devoró, los secretos que la arqueología aún no ha podido explicar. "
    "Cada video es una expedición al pasado. El formato sin rostro permite "
    "que las piedras, los mapas y las ruinas hablen por sí solas — estas "
    "historias no necesitan presentador. Necesitan testigos."
)
CHANNEL_ABOUT_SECTION = """Bienvenido a Civilizaciones Olvidadas.

Exploramos las civilizaciones perdidas, las ruinas antiguas y los secretos históricos que la humanidad ha dejado atrás. Desde Göbekli Tepe hasta los Mayas, desde el Valle del Indo hasta Angkor Wat — cada video es una expedición al pasado que la historia oficial no te contó.

🎬 Formato: video ensayos documentales (10-15 minutos)
🗓️ Nuevos descubrimientos: 2-3 por semana
🎙️ Narración documental con fuentes históricas y arqueológicas verificadas

📩 Contacto: {email}

🏛️ Si te fascinan las civilizaciones antiguas, las ruinas misteriosas, la arqueología, los secretos de la historia y los enigmas que la ciencia aún no ha resuelto... este canal es para ti.

Suscríbete y activa la campana para no perderte ningún secreto del pasado."""
CHANNEL_KEYWORDS = [
    "civilizaciones perdidas", "secretos de la historia", "civilizaciones antiguas",
    "ruinas misteriosas", "arqueología", "misterios de la historia",
    "civilizaciones olvidadas", "documental historia", "enigmas de la humanidad",
    "ciudades perdidas", "historia antigua", "descubrimientos arqueológicos",
    "culturas antiguas", "templos perdidos", "documental arqueología",
    "misterios sin resolver", "historia universal", "grandes imperios",
    "secretos del pasado", "documental en español",
]
CANAL_INITIALS = "CO"
LOGO_SIZE = 140

# ═══════════════════════════════════════════════════════════════════
# PRODUCTION TARGETS
# ═══════════════════════════════════════════════════════════════════
PROD_SCRIPT_WORDS_MIN = 2200
PROD_SCRIPT_WORDS_MAX = 3800
PROD_SCRIPT_SCENES_MIN = 10
PROD_SCRIPT_SCENES_MAX = 18
PROD_SCRIPT_BLOCKS_MIN = 10
PROD_SCRIPT_BLOCKS_MAX = 18
PROD_VIDEO_DURATION_MIN = 12
PROD_VIDEO_DURATION_MAX = 16
VIDEO_AVERAGE_DURATION_MIN = 14
VIDEO_DURATION_DISCREPANCY_MIN = 2

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
TARGET_AUDIENCE = (
    "18-55 años (amplio), LATAM (MX 35%, CO 20%, AR 15%, PE 10%, ES 10%, otros 10%). "
    "Curiosos, mente abierta, interés en historia, arqueología, misterios y culturas antiguas. "
    "55% hombres / 45% mujeres. 65%+ mobile. Sesiones de 10-15 min. Pico de consumo: 20:00-00:00 local."
)
TARGET_AUDIENCE_PSYCHOGRAPHIC = {
    "The Explorer": ("Siente fascinación por lo desconocido. Quiere descubrir mundos perdidos y viajar en el tiempo desde su sofá."),
    "The History Buff": ("Consume documentales históricos con pasión. Sabe de historia pero siempre busca el ángulo que no le contaron."),
    "The Conspiracy Curious": ("Entra por el misterio, se queda por los hechos. Comparte para debatir: 'esto cambia todo lo que creíamos saber'."),
    "The Travel Dreamer": ("Sueña con visitar Machu Picchu, Petra, Angkor Wat. Cada video es un viaje virtual a un destino imposible."),
}

# ═══════════════════════════════════════════════════════════════════
# TITLE OPTIMIZATION
# ═══════════════════════════════════════════════════════════════════
TITLE_FORMULAS = [
    "La Civilización que {action} y Nadie Puede Explicar",
    "Encontraron {descubrimiento} en {lugar} y Cambió la Historia",
    "{número} Civilizaciones que Desaparecieron Sin Dejar Rastro",
    "¿Qué Pasó Realmente con {civilización}? El Misterio que la Ciencia No Resuelve",
    "El Descubrimiento en {lugar} que Reescribe la Historia Antigua",
    "Antes de {civilización_famosa}, Existió {civilización_olvidada}",
    "El Oscuro Secreto de {civilización} que la Historia Oficial Ocultó",
    "Tenían Tecnología que No Deberían Tener: El Misterio de {lugar}",
]
TITLE_POWER_WORDS = [
    "revelado", "filtrado", "censurado", "inédito", "clasificado",
    "confidencial", "prohibido", "enterrado", "sellado",
    "exclusivo", "desclasificado", "suprimido", "archivado", "silenciado",
    "protegido", "blindado", "vetado",
    "escalofriante", "desgarrador", "inexplicable", "demoledor",
    "sobrecogedor", "estremecedor", "alucinante", "aterrador",
    "asombroso", "desconcertante", "fascinante", "impactante",
    "colosal", "monumental", "imponente", "magnífico", "sobrecogedor",
    "devastador", "insospechado", "deslumbrante",
    "oculto", "secreto", "perturbador", "siniestro", "enigmático",
    "increíble", "insólito", "misterio", "enigma",
    "indescifrable", "desconocido", "enigmático",
    "perdida", "milenaria", "ancestral", "desaparecida", "sumergida",
    "maldita", "sagrada", "olvidado", "sepultado", "inhallable",
    "legendaria", "mítica", "prehistórica", "antediluviana",
    "sumergido", "subterráneo", "abandonado", "erosionado",
    "petrificado", "fosilizado",
    "descubrieron", "encontraron", "reescribió", "cambió", "revolucionó",
    "desenterraron", "hallaron", "excavaron", "revelaron", "desvelaron",
    "sacaron a la luz", "emergió",
    "demostrado", "confirmado", "verificado", "documentado",
    "imposible", "sacudió", "cambió todo",
    "gigantesca", "titánica", "faraónica", "imposible",
    "monumental", "inmensa", "descomunal",
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
    "la humanidad durante siglos por sus misterios y secretos...'.\n\n"
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
SCRIPT_STRUCTURE = [
    {"step": "EL DESCUBRIMIENTO", "time_pct": "0-10%",
     "description": ("El hallazgo más impactante. Quién lo encontró, cómo, por qué "
                     "es tan importante. Imagen: la ruina o artefacto más evocador. "
                     "Cerrar con promesa: 'Al final de este video vas a entender "
                     "por qué este descubrimiento cambió la historia para siempre.'")},
    {"step": "EL CONTEXTO", "time_pct": "10-20%",
     "description": ("Lo que se creía antes de este descubrimiento. Cómo este hallazgo "
                     "contradice la historia oficial.")},
    {"step": "LA CIVILIZACIÓN", "time_pct": "20-30%",
     "description": ("Quiénes eran. Cómo vivían. Qué construyeron. Qué creían. Humanizar a los protagonistas del pasado."),
     "retention_anchor": ("CLIFFHANGER al 25%: 'Pero lo que los arqueólogos no esperaban "
                          "encontrar... es que esta civilización guardaba un secreto "
                          "que desafía todo lo que sabemos.'")},
    {"step": "EL MISTERIO", "time_pct": "30-55%",
     "description": ("El enigma central. Lo que no encaja. La anomalía. Escalar el asombro."),
     "retention_anchor": ("CLIFFHANGER al 50%: Silencio 2s. Cambio de imagen a "
                          "primer plano del artefacto/ruina. 'Recapitulemos: [1 frase]. "
                          "Ahora prepárate para lo más increíble.'")},
    {"step": "LA REVELACIÓN", "time_pct": "55-70%",
     "description": ("El instante de comprensión. El hallazgo que lo cambia todo. Música crece.")},
    {"step": "LAS CONSECUENCIAS", "time_pct": "70-85%",
     "description": ("Cómo este descubrimiento cambió la historia. Lo que aún no sabemos."),
     "retention_anchor": ("EL ESPEJO al 70%: 'Ahora piensa: ¿cuántas civilizaciones más "
                          "siguen enterradas bajo tus pies?'")},
    {"step": "EL LEGADO", "time_pct": "85-100%",
     "description": ("Lo que nos dejaron. Por qué importa hoy. Reflexión. End hook + CTA.")},
]
SCRIPT_END_HOOK = (
    "Y si crees que esta historia es increíble, espera a ver lo que "
    "descubrieron en {next_place}. Porque lo que los arqueólogos "
    "encontraron allí es todavía más inexplicable. Ese es el próximo "
    "video. Dale like, suscríbete y activa la campana."
)
SCRIPT_EMOTIONAL_ARC = {
    "0-10%": "asombro", "10-20%": "curiosidad", "20-30%": "fascinación",
    "30-45%": "intriga", "45-55%": "misterio", "55-65%": "revelación",
    "65-75%": "estupefacción → comprensión", "75-85%": "reflexión",
    "85-95%": "solemnidad", "95-100%": "maravilla",
}
RETENTION_ANCHORS = {
    "at_25_pct": {"trigger": "cliffhanger_archaeological",
                  "action": ("Insertar mini-cliffhanger: 'Pero bajo esa piedra, a solo "
                             "3 metros de profundidad, encontraron algo que no debía estar allí.'")},
    "at_50_pct": {"trigger": "the_reset",
                  "action": ("Música fuera 2s. 'Recapitulemos: [resumen]. Ahora viene lo que ningún arqueólogo esperaba.'")},
    "at_70_pct": {"trigger": "the_mirror",
                  "action": ("Conectar con el espectador: 'Ahora mira a tu alrededor. ¿Cuántas civilizaciones enterradas hay donde estás ahora?'")},
}
VIRALITY_TRIGGERS = [
    {"name": "Awe & Discovery", "mechanism": "'No sabías que esto existía.' El asombro arqueológico es la emoción que más se comparte."},
    {"name": "Time Travel", "mechanism": "Transportar al espectador a otra época. La inmersión histórica genera altísima retención."},
    {"name": "Conspiracy Adjacent", "mechanism": "Cerrar con idea que desafía la narrativa oficial sin caer en lo conspiranoico."},
    {"name": "Identity Signaling", "mechanism": "Compartir este video dice: 'Yo sé cosas que los demás no.'"},
    {"name": "Mystery Hook", "mechanism": "Cada guion debe incluir un vacío de información que el espectador NECESITA llenar."},
]

# ═══════════════════════════════════════════════════════════════════
# VOICE (TTS)
# ═══════════════════════════════════════════════════════════════════
TTS_STRATEGY = {
    "voice_primary": "es-ES-AlvaroNeural", "voice_secondary": "es-MX-DaliaNeural",
    "rate_base": 0.80, "pitch_base": "-2Hz",
    "rate_hook": "-15%", "pitch_hook": "+0Hz",
    "rate_desarrollo": "-22%", "pitch_desarrollo": "-2Hz",
    "rate_climax": "-30%", "pitch_climax": "-4Hz",
    "rate_reflexion": "-25%", "pitch_reflexion": "-2Hz",
    "rate_cierre": "-18%", "pitch_cierre": "+0Hz",
}
VOICE_RATE = 0.80
VOICE_PITCH = "-2Hz"
TTS_ENGINE = "kokoro"
KOKORO_VOICE = "em_santa"
# KOKORO_BLOCK_SPEEDS ya no se define aquí — el voice resolver calcula
# las velocidades Kokoro automáticamente desde TTS_STRATEGY.rate_*
KOKORO_PAUSE_BETWEEN_BLOCKS = 1.3

# ═══════════════════════════════════════════════════════════════════
# CONTENT SOURCES
# ═══════════════════════════════════════════════════════════════════
REDDIT_SUBREDDITS = [
    "Archaeology", "AlternativeHistory", "history", "LostCivilizations",
    "ancientrome", "mesoamerica", "ancientegypt", "AncientCivilizations",
    "UnresolvedMysteries", "HighStrangeness", "nonmurdermysteries",
    "Damnthatsinteresting", "interestingasfuck", "todayilearned",
    "TrueReddit", "ArtefactPorn", "castles", "ImaginaryLandscapes",
]
WIKIPEDIA_CATEGORIES = [
    "Lost cities", "Ancient civilizations", "Archaeological mysteries",
    "Unsolved problems in archaeology", "Ruins", "Megalithic monuments",
    "Prehistoric sites", "Archaeological sites by country", "Former empires",
    "Extinct states", "Ancient peoples", "Disappeared peoples",
    "Underwater archaeological sites", "Petroglyphs", "Rock art",
    "Ancient technology", "History of writing", "Ancient languages",
    "Ancient warfare", "List of archaeological periods", "World Heritage Sites by country",
    "Civilizaciones antiguas", "Ciudades perdidas", "Yacimientos arqueológicos",
    "Lugares misteriosos", "Ruinas", "Monumentos megalíticos",
    "Historia antigua", "Imperios desaparecidos", "Arqueología",
]
SCRAPE_SOURCES = [
    {"plugin": "wikipedia", "priority": 1},
    {"plugin": "reddit", "priority": 2},
    {"plugin": "atlas_obscura", "priority": 3},
    {"plugin": "google_news", "priority": 4},
    {"plugin": "rss", "priority": 5},
]
ATLAS_OBSCURA_CATEGORIES = ["wonders", "history", "ruins", "ancient", "unique"]
RSS_FEEDS = [
    "https://www.archaeology.org/news?format=feed",
    "https://www.ancient-origins.net/rss.xml",
]
GOOGLE_NEWS_QUERIES = [
    "descubrimiento arqueológico", "civilización perdida", "ruinas antiguas",
    "ciudad antigua encontrada", "nuevo hallazgo arqueología",
    "misterio histórico resuelto", "tumba antigua descubierta", "templo perdido encontrado",
]

# ═══════════════════════════════════════════════════════════════════
# VISUAL STYLE
# ═══════════════════════════════════════════════════════════════════
IMAGE_STYLE_MODIFIERS = (
    "cinematic 16:9, warm golden hour photography, atmospheric lighting, "
    "professional documentary photography, mysterious and solemn mood, "
    "earthy tones, dust particles in light rays"
)
COLOR_PALETTE = {
    "primary": (194, 154, 75), "secondary": (62, 38, 22), "accent": (168, 104, 52),
    "text": (245, 238, 220), "text_shadow": (10, 6, 2), "tertiary": (45, 38, 28),
    "warning": (212, 160, 45),
}
FILM_GRAIN_OPACITY = 8
FILM_GRAIN_FRAMES = 8
KEN_BURNS_ZOOM_MIN = 4
KEN_BURNS_ZOOM_MAX = 10

# ═══════════════════════════════════════════════════════════════════
# MEDIA STRATEGY
# ═══════════════════════════════════════════════════════════════════
MEDIA_STRATEGY = {
    "media_per_block": 1, "prefer_video": True,
    "max_video_blocks_pct": 80, "target_video_pct": 80, "max_placeholder_pct": 0,
    "video_fallback_to_image": True, "video_min_duration": 4, "video_max_duration": 20,
    "video_sources": ["pexels"], "video_providers": DEFAULT_VIDEO_PROVIDERS,
    "fallback_query": "ancient ruins archaeological site cinematic 16:9",
    "fallback_query_simple": "ancient temple ruins stone architecture",
    "video_fallback_queries": [
        "drone aerial ancient ruins desert pyramids cinematic",
        "cinematic museum artifacts exhibition historical documentary",
        "mysterious temple interior torchlight dark atmosphere",
        "archaeological excavation dig historical site documentary",
        "ancient civilization legacy golden hour ruins landscape",
    ],
    "min_video_pct": 30,
    "ken_burns_zoom_min": 4, "ken_burns_zoom_max": 10,
    "crossfade_min": 0.4, "crossfade_max": 0.8,
    "ai_image_fallback": True, "ai_max_per_video": 5,
}

# ═══════════════════════════════════════════════════════════════════
# INTRO / OUTRO
# ═══════════════════════════════════════════════════════════════════
INTRO_FONT_SIZE = 72
INTRO_BG_COLOR = (22, 18, 12)
OUTRO_FONT_SIZE = 60
OUTRO_BG_COLOR = (22, 18, 12)
OUTRO_TEXT = "Suscríbete"
OUTRO_CTA_SUBSCRIBE = "❤️ Suscríbete"
CTA_TEXT = ("Si has llegado hasta aquí y te ha fascinado este viaje al pasado,\n"
            "suscríbete y dale like\npara seguir desenterrando juntos civilizaciones perdidas.")
CTA_TEXT_VARIANTS = [
    ("Si has llegado hasta aquí y te ha fascinado este viaje al pasado,\n"
     "suscríbete y dale like\npara seguir desenterrando juntos civilizaciones perdidas."),
    ("Gracias por explorar con nosotros esta historia olvidada.\n"
     "Suscríbete y dale like:\naún quedan cientos de ruinas por descubrir."),
    ("Cada civilización guarda un secreto, y tú acabas de descubrir uno.\n"
     "Suscríbete, dale like y comparte\npara que la historia no vuelva a quedar enterrada."),
]
INTRO_VOICE_TEXT = "Bienvenidos a Civilizaciones Olvidadas, la historia que el tiempo enterró."
CTA_VOICE_TEXT = "Si has llegado hasta aquí y despertó tu curiosidad, suscríbete y acompáñanos. El próximo secreto de la historia te espera."
OUTRO_VOICE_TEXT = "Gracias por vernos. Nos vemos en la próxima civilización."

# ═══════════════════════════════════════════════════════════════════
# YOUTUBE METADATA
# ═══════════════════════════════════════════════════════════════════
YT_CATEGORY_ID = "27"  # Education
PUBLISH_MODE = "scheduled"
PUBLISH_WARMUP_MIN = 30
UPLOAD_WINDOWS = [
    {"start": 10, "end": 13}, {"start": 14, "end": 17}, {"start": 20, "end": 22},
]
YT_DEFAULT_TAGS = [
    "civilizaciones perdidas", "civilizaciones antiguas", "secretos de la historia",
    "historia antigua", "arqueología", "ciudades perdidas", "ruinas antiguas",
    "descubrimientos arqueológicos", "misterios históricos", "civilizaciones olvidadas",
    "julio cesar guerras galicas", "ariovisto batalla germanica", "baalbek megalitos misterio",
    "pueblos del mar civilizaciones", "blas de lezo batalla cartagena",
    "como construyeron las piramides realmente", "civilizaciones que desaparecieron sin explicacion",
    "ruinas mas misteriosas del mundo", "descubrimientos arqueologicos que cambiaron la historia",
    "templos antiguos que aun existen", "ciudades perdidas encontradas recientemente",
    "misterios de la humanidad sin resolver", "enigmas historicos que la ciencia no explica",
    "secretos de civilizaciones antiguas documental", "culturas antiguas mas avanzadas",
    "imperios que desaparecieron misteriosamente", "lugares arqueologicos prohibidos",
    "mapas antiguos que no deberian existir", "tecnologia antigua imposible de explicar",
    "historia universal documental español", "documental historia español",
    "video ensayo historia", "mejores documentales historia antigua",
    "historias del pasado fascinantes", "historia para reflexionar",
]

# ═══════════════════════════════════════════════════════════════════
# SEO
# ═══════════════════════════════════════════════════════════════════
SEO_PRIMARY_KEYWORD = "civilizaciones antiguas documental"
SEO_SECONDARY_KEYWORDS = [
    "civilizaciones perdidas", "secretos de la historia", "ciudades antiguas misterios",
    "civilizaciones olvidadas", "ruinas misteriosas del mundo",
    "descubrimientos arqueológicos recientes", "hallazgos que cambiaron la historia",
    "tumbas antiguas encontradas", "templos perdidos descubiertos",
    "misterios de la humanidad sin resolver", "enigmas históricos inexplicables",
    "secretos de las pirámides", "civilizaciones desaparecidas sin rastro",
    "como construyeron baalbek los romanos", "guerras galicas julio cesar documental",
    "pueblos del mar quienes fueron realmente", "mapa de piri reis inexplicable",
    "piedra de la mujer embarazada baalbek", "tecnologia antigua imposible de explicar",
    "ciudades subterraneas antiguas descubiertas", "imperios mas misteriosos de la historia",
    "documental historia español", "video ensayo arqueología",
    "mejores documentales historia antigua", "historias del pasado fascinantes",
    "datos curiosos de historia", "culturas antiguas del mundo",
    "imperios más poderosos de la historia", "lugares misteriosos del mundo",
    "historia para reflexionar",
]
SEO_HASHTAGS = [
    "#CivilizacionesOlvidadas", "#HistoriaAntigua", "#Arqueología", "#Misterios",
    "#SecretosDeLaHistoria", "#CivilizacionesPerdidas", "#Documental", "#Historia",
    "#Ruinas", "#Curiosidades", "#SabíasQue", "#Enigmas",
    "#CulturasAntiguas", "#MaravillasDelMundo", "#Descubrimientos",
]

# ═══════════════════════════════════════════════════════════════════
# SHORTS
# ═══════════════════════════════════════════════════════════════════
SHORTS_PER_DAY = 3
SHORTS_HASHTAGS = [
    "#CivilizacionesOlvidadas", "#Historia", "#SabíasQue", "#Shorts",
    "#Curiosidades", "#Arqueología", "#Misterios", "#CivilizacionesPerdidas",
    "#Secretos", "#RuinasAntiguas",
]

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
THUMBNAIL_BORDER_WIDTH = 5
THUMBNAIL_FONT_FAMILY = "DejaVuSerif-Bold"
THUMBNAIL_BORDER_COLOR = "#D4A843"
THUMBNAIL_SHOW_4K_BADGE = False
THUMBNAIL_TEXT_STROKE_COLOR = "#1A0F08"
THUMBNAIL_VISUAL_STYLE = "ancient_mystery"
THUMBNAIL_MANUAL_STYLE = {
    "visual_style": "ancient_mystery",
    "color_palette": {"primary": "#6B4C3B", "accent": "#D4A843", "text": "#D4A843", "shadow": "#1A0F08"},
    "base_composition": "ruins_reveal",
    "effects": {"contrast_boost": 1.15, "saturation": 0.75, "vignette": 0.40},
    "text_style": {"uppercase": False, "max_words": 5},
    "pollo_prompt_suffix": (
        "ancient ruins, archaeological site, mysterious atmosphere, "
        "stone textures, golden hour lighting, dust motes in sunbeams, "
        "cinematic composition, 16:9 aspect ratio, warm earthy tones, "
        "professional archaeology photography"
    ),
}
THUMBNAIL_STYLE = {
    "layout": "image_full_background_text_overlay", "max_text_words": 4,
    "text_color": "warm_gold_on_dark_earth", "font_style": "bold_elegant_serif",
    "image_treatment": "ancient_ruins_golden_hour_mysterious",
    "background": "#1A0F08", "accent_color": "#D4A843",
    "face_policy": ("Ancient statues and faces from artifacts YES. Close-up of stone carvings, "
                    "golden masks, sculptures. AI-generated faces: NO. Siluetas de exploradores OK."),
    "number_preference": "odd_numbers_for_lists",
    "gold_accent_rule": ("Golden light ray or dust motes illuminating ruins boosts CTR. "
                         "NEVER use neon or modern styling."),
}
THUMBNAIL_TEMPLATES = {
    "the_ruins": {"description": "Wide shot of ancient ruins at golden hour", "text_position": "bottom_third_centered",
                  "text_words": "2-3", "accent": "golden_dust_light_ray",
                  "best_for": "Lost cities, temple discoveries, architectural marvels"},
    "the_artifact": {"description": "Close-up of mysterious artifact or golden mask", "text_position": "center_over_gradient",
                     "text_words": "3-4", "accent": "spotlight_on_artifact",
                     "best_for": "Mysterious objects, technological anomalies, forbidden artifacts"},
    "the_map": {"description": "Ancient map, parchment texture, compass rose, sepia tones", "text_position": "bottom_over_gradient",
                "text_words": "3-4", "accent": "magnifying_glass_or_compass",
                "best_for": "Lost cities, expeditions, geographic mysteries"},
}

# ═══════════════════════════════════════════════════════════════════
# VIDEO TIMING & MONETIZATION
# ═══════════════════════════════════════════════════════════════════
VIDEO_MIDROLL_STRATEGY = (
    "Colocar mid-rolls en pausas naturales entre capítulos de la "
    "expedición narrativa. NUNCA en medio del clímax arqueológico. "
    "Cada mid-roll debe preceder un mini-gancho."
)
MONETIZATION_TARGET_CPM = "$8–$18 USD"
MONETIZATION_VERTICALS = [
    "Educación y aprendizaje", "Viajes y turismo cultural",
    "Libros y audiolibros", "Tecnología", "Inversión y finanzas",
]

# ═══════════════════════════════════════════════════════════════════
# END SCREEN
# ═══════════════════════════════════════════════════════════════════
END_SCREEN_STRATEGY = {
    "left_card": {"type": "playlist", "content": "most_relevant_playlist",
                  "purpose": "Keep viewer exploring ancient worlds — archaeological rabbit hole"},
    "center": {"type": "subscribe", "purpose": "Convert explorer to subscriber"},
    "right_card": {"type": "video", "content": "most_recent_upload",
                   "purpose": "Push newest expedition to engaged viewers"},
    "spoken_cta": ("Si este descubrimiento te dejó sin palabras, el siguiente en la lista es "
                   "todavía más impactante. Te dejo el enlace en pantalla. Suscríbete si quieres "
                   "seguir explorando las civilizaciones que la historia olvidó."),
}

# ═══════════════════════════════════════════════════════════════════
# PLAYLISTS
# ═══════════════════════════════════════════════════════════════════
PLAYLISTS = [
    {"slug": "expediciones-completas", "name": "Expediciones Completas",
     "description": "Documentales en profundidad sobre las civilizaciones perdidas...", "type": "main"},
    {"slug": "lo-mas-impactante", "name": "Lo Más Impactante",
     "description": "Los 5 descubrimientos arqueológicos más asombrosos del canal.", "type": "onboarding"},
    {"slug": "civilizaciones-perdidas", "name": "Civilizaciones Perdidas",
     "description": "Sumerios, Mayas, Valle del Indo, Anasazi, Olmecas...", "type": "thematic"},
    {"slug": "misterios-arqueologicos", "name": "Misterios Arqueológicos",
     "description": "Los enigmas que la arqueología aún no ha podido resolver.", "type": "thematic"},
    {"slug": "maravillas-del-mundo", "name": "Maravillas del Mundo Antiguo",
     "description": "Petra, Angkor Wat, Machu Picchu, Stonehenge, Pirámides de Giza...", "type": "thematic"},
]

# ═══════════════════════════════════════════════════════════════════
# FIRST 48H / COMMUNITY TAB / CROSS-PLATFORM / COLLAB
# ═══════════════════════════════════════════════════════════════════
FIRST_48H_STRATEGY = {
    "pre_upload_24h": [
        "Community Tab poll: '¿Qué civilización antigua te fascina más?'",
        "YouTube Story: imagen de ruinas + 'Mañana. 9PM MX. Un secreto de hace 5.000 años.'",
    ],
    "hour_0": ["Publish at 9PM Mexico City time", "First comment: pregunta para debate histórico"],
    "hours_1_6": ["Reddit r/Archaeology + r/AlternativeHistory: TEXT post", "Facebook groups: Historia Antigua, Arqueología"],
    "hours_6_24": ["Reply to EVERY comment", "Twitter/X thread: 5-7 tweets, final tweet = YouTube link"],
    "hours_24_48": ["Analyze CTR and retention", "If CTR < 5%: swap thumbnail variant", "Second Community Tab"],
}
COMMUNITY_TAB_PLAN = {
    "frequency": "3x/week",
    "schedule": {
        "monday": {"type": "poll", "example": "¿Cuál de estos misterios arqueológicos te parece más fascinante?",
                   "options": ["Göbekli Tepe", "Líneas de Nazca", "Pirámides de Egipto", "Stonehenge"]},
        "wednesday": {"type": "image_fact", "example": "Fotografía de ruinas + dato asombroso"},
        "friday": {"type": "teaser", "example": "Este sábado: el descubrimiento que cambió todo."},
    },
}
CROSS_PLATFORM = {
    "tiktok": {"format": "60-90s vertical cut-downs", "style": "Same visual assets, cropped 9:16",
               "hook_template": "En {año}, un {persona} encontró {descubrimiento} que cambió la historia.",
               "structure": "Hook → El descubrimiento (20s) → El misterio (15s) → 'Video completo en YouTube' (10s)",
               "cadence": "3x/day (days 1, 3, 5)"},
    "youtube_shorts": {"format": "15-30s most incredible discovery moment", "end_cta": "'Video completo en el canal'"},
    "twitter_x": {"format": "Thread — 1 discovery = 1 thread per week",
                  "template": "'HOY en Civilizaciones Olvidadas: La civilización que...' + 5-7 tweets + link"},
    "spotify_podcast": {"format": "Audio-only export", "title_format": "Civilizaciones Olvidadas | {story_title}"},
}
COLLABORATION_TARGETS = {
    "tier_1_direct": [
        {"name": "Pero eso es otra Historia", "niche": "Historia y mitología, audiencia masiva"},
        {"name": "Descifrando la Historia", "niche": "Misterios históricos y conspiraciones"},
        {"name": "Agujeros de Guion", "niche": "Curiosidades históricas y cinematográficas"},
        {"name": "Bully Magnets", "niche": "Historia visual de alta calidad"},
    ],
    "tier_2_adjacent": [
        {"name": "Mundo Desconocido", "niche": "Misterios, civilizaciones — enorme audiencia LATAM"},
        {"name": "VM Granmisterio", "niche": "Conspiración y misterio, audiencia afín"},
    ],
    "collab_formats": ["React: 'Un historiador reacciona a Civilizaciones Olvidadas'", "Topic trade", "Mention strategy"],
}
TRENDING_TOPIC_HOOKS = {
    "type_a_news": {"trigger": "Nuevo descubrimiento arqueológico",
                    "pivot": "Esto que acaban de encontrar... la historia tiene docenas de hallazgos igual de inexplicables."},
    "type_b_anniversary": {"trigger": "Aniversario de grandes descubrimientos",
                           "calendar": {"july": "Machu Picchu (24 julio)", "november": "Tutankamón (4 nov)", "december": "Göbekli Tepe"}},
    "type_c_pop_culture": {"trigger": "Estreno de película/serie sobre civilizaciones antiguas",
                           "strategy": "'La historia REAL detrás de {show/movie}'"},
    "type_d_calendar": {"name": "Calendario de las Civilizaciones",
                        "months": {"january": "Civilizaciones del hielo", "march": "Equinoccio — templos alineados",
                                   "june": "Solsticio — Stonehenge", "september": "Equinoccio otoñal",
                                   "october": "Civilizaciones malditas (Halloween)", "december": "Solsticio — mitos del origen"}},
}
CONTENT_PILLARS = [
    {"name": "La Civilización", "ratio": 55, "desc": "Documental profundo de una civilización individual"},
    {"name": "Listas y Descubrimientos", "ratio": 30, "desc": "Compilación temática"},
    {"name": "El Enigma", "ratio": 15, "desc": "Video breve sobre un misterio arqueológico concreto"},
]
SEASON1_EPISODES = [
    {"ep": 1, "title": "Göbekli Tepe: El Templo que Reescribe la Historia", "civilization": "Cultura Neolítica Pre-Cerámica"},
    {"ep": 2, "title": "El Valle del Indo: La Civilización más Avanzada que Desapareció", "civilization": "Valle del Indo"},
    {"ep": 3, "title": "Los Anasazi: El Pueblo que Desapareció de los Acantilados", "civilization": "Anasazi"},
    {"ep": 4, "title": "Los Olmecas: Las Cabezas Colosales que Nadie Puede Explicar", "civilization": "Olmeca"},
    {"ep": 5, "title": "Angkor Wat: La Metrópolis que la Jungla Devoró", "civilization": "Imperio Jemer"},
    {"ep": 6, "title": "Petra: La Ciudad Esculpida en Piedra que el Mundo Olvidó", "civilization": "Nabateos"},
    {"ep": 7, "title": "Los Mayas: El Colapso que Nadie Puede Explicar", "civilization": "Maya"},
    {"ep": 8, "title": "La Isla de Pascua: El Misterio de los Moáis", "civilization": "Rapa Nui"},
    {"ep": 9, "title": "Las Líneas de Nazca: Mensajes para los Dioses", "civilization": "Nazca"},
    {"ep": 10, "title": "Los 5 Descubrimientos que Cambiaron la Historia", "civilization": "Compilación"},
]

# ═══════════════════════════════════════════════════════════════════
# MARATHON & VIRAL — channel-specific
# ═══════════════════════════════════════════════════════════════════
MARATHON_NARRATIVE_FORMAT = "historical_collapse"

MARATHON_TITLE_FORMULAS = [
    "{topic}: El Secreto Que La Arqueología No Explica",
    "Civilizaciones Perdidas: {topic} | Documental",
    "{topic} — Descubrimientos Que Desafían La Historia",
    "Lo Que Encontraron En {topic} Cambió La Arqueología",
    "Misterios Sin Resolver: {topic} | Documental HD",
]

MARATHON_HOOK_TYPES = [
    "misterio_sin_resolver",
    "secreto_ancestral",
    "revelacion_impactante",
]

NICHE_KEYWORDS_ENG = [
    "lost civilizations", "ancient mysteries", "forgotten civilizations",
    "ancient technology documentary", "archaeological discoveries",
    "ancient ruins unexplained", "lost cities found", "mysterious archaeological sites",
    "ancient artifacts unexplained", "hidden history documentary",
    "ancient civilizations documentary", "prehistoric discoveries",
]

VIRAL_PLAYLIST_KEYWORDS = {
    "expediciones-completas": ["lost civilizations full documentary", "ancient cities discovered", "archaeological expedition documentary", "forgotten history documentary"],
    "lo-mas-impactante": ["most amazing archaeological discoveries", "unbelievable ancient technology", "discoveries that changed history", "most mysterious ancient artifacts"],
    "civilizaciones-perdidas": ["vanished civilizations documentary", "sumerian mesopotamia documentary", "mysterious ancient civilizations", "advanced prehistoric civilizations"],
    "misterios-arqueologicos": ["archaeological mysteries unsolved", "ancient artifacts scientists can't explain", "impossible ancient structures", "out of place artifacts documentary"],
    "maravillas-del-mundo": ["ancient wonders of the world documentary", "greatest archaeological sites", "amazing ancient monuments", "forgotten temples documentary"],
}
