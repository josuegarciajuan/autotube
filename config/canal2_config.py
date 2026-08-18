"""Configuration for Canal 2: Sincronías (Milagros y Casualidades).

Meta-niche: "historias reales de milagros, casualidades imposibles
y fenómenos que la ciencia aún no puede explicar"

Only CHANNEL-SPECIFIC parameters.  Everything else inherits from ``config.defaults.py``
via ``config_bridge.py``.
"""

from config.settings import DEFAULT_VIDEO_PROVIDERS, DEFAULT_VIDEO_FALLBACK_QUERIES

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

YOUTUBE_HANDLE = "@Sincronías-q1y"
YOUTUBE_CHANNEL_URL = "https://www.youtube.com/@Sincronías-q1y"

CANAL_NARRATIVE_STYLE = "documental de asombro"
CANAL_STYLE_DESCRIPTION = (
    "Historias extraordinarias que desafían toda explicación. "
    "Coincidencias imposibles, milagros documentados, fenómenos que "
    "la ciencia aún no entiende. El formato sin rostro permite que "
    "las imágenes hablen por sí solas — estas historias no necesitan presentador."
)

CHANNEL_ABOUT_SECTION = """Bienvenido a Sincronías.

Exploramos las casualidades más increíbles, los milagros mejor documentados y los fenómenos inexplicables que desafían toda lógica. Historias reales de personas que estuvieron en el lugar exacto en el momento exacto... y lo que la ciencia todavía intenta explicar.

🎬 Formato: video ensayos documentales (8-14 minutos)
🗓️ Nuevas historias: cada semana
🎙️ Narración documental con fuentes verificadas

📩 Contacto: {email}

✨ Si te fascinan las casualidades imposibles, los milagros modernos, los sucesos inexplicables y las historias que te dejan sin palabras... este canal es para ti.

Suscríbete y activa la campana para no perderte ninguna historia que desafía lo imposible."""

CHANNEL_KEYWORDS = [
    "casualidades imposibles", "milagros reales", "fenómenos inexplicables",
    "historias increíbles reales", "sincronías", "destino",
    "coincidencias sorprendentes", "historias que desafían la lógica",
    "milagros modernos", "sucesos paranormales", "predicciones cumplidas",
    "experiencias inexplicables", "documental misterio", "historias reales impactantes",
    "sucesos inexplicables", "casualidades del destino", "lo inexplicable",
    "misterios reales", "documental en español", "historias que inspiran",
]

CANAL_INITIALS = "SX"
LOGO_SIZE = 140

# ═══════════════════════════════════════════════════════════════════
# PRODUCTION TARGETS (channel-specific)
# ═══════════════════════════════════════════════════════════════════

PROD_SCRIPT_WORDS_MIN = 2000
PROD_SCRIPT_WORDS_MAX = 3500
PROD_SCRIPT_SCENES_MIN = 10
PROD_SCRIPT_SCENES_MAX = 18
PROD_SCRIPT_BLOCKS_MIN = 10
PROD_SCRIPT_BLOCKS_MAX = 18
PROD_VIDEO_DURATION_MIN = 10
PROD_VIDEO_DURATION_MAX = 14

VIDEO_AVERAGE_DURATION_MIN = 12
VIDEO_DURATION_DISCREPANCY_MIN = 2

MAX_CLIP_EXTEND_SEC = 16.0

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
    "La Casualidad que {action} y Nadie Puede Explicar",
    "El Milagro de {name}: {shocking_fact}",
    "Predijo {event} Años Antes de que Ocurriera",
    "¿{question}? La Respuesta Desafía Toda Lógica",
    "Cuando el Universo Conspiró: La Increíble Historia de {name}",
    "{number} Segundos que Desafiaron las Leyes de la Física",
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
    # Spiritual / Destiny
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

SCRIPT_STRUCTURE = [
    {"step": "EL GANCHO", "time_pct": "0-10%",
     "description": (
         "El hecho más impactante en frío. Sin contexto. Imagen: la más "
         "evocadora de la historia. Cerrar con promesa: 'Al final de "
         "este video vas a entender por qué la ciencia sigue sin explicarlo.'"
     )},
    {"step": "EL CONTEXTO", "time_pct": "10-20%",
     "description": (
         "Lo que se creía imposible antes de este suceso. Construir "
         "anticipación: 'Las probabilidades de que esto ocurriera "
         "eran de una entre un millón. Pero ocurrió.'"
     )},
    {"step": "LOS PROTAGONISTAS", "time_pct": "20-30%",
     "description": (
         "Las personas reales detrás de la historia. Gente normal, "
         "vidas normales, hasta que ocurrió lo imposible. Humanizar "
         "para que el espectador se identifique."
     ),
     "retention_anchor": (
         "CLIFFHANGER al 25%: 'Pero lo que esta persona no sabía... "
         "es que en exactamente 72 horas, su vida cambiaría para siempre.'"
     )},
    {"step": "EL SUCESO", "time_pct": "30-55%",
     "description": (
         "El milagro, la coincidencia o el fenómeno paso a paso. "
         "Escalar el asombro. 'Y entonces ocurrió algo que desafía "
         "todo lo que creemos saber.'"
     ),
     "retention_anchor": (
         "CLIFFHANGER al 50%: Silencio 2s. Cambio de imagen. "
         "'Recapitulemos: [1 frase]. Ahora viene lo más increíble.'"
     )},
    {"step": "EL MOMENTO CUMBRE", "time_pct": "55-70%",
     "description": (
         "El instante exacto de lo inexplicable. Peak de asombro. "
         "Música crece. Luz. Emoción. El momento que deja sin palabras."
     )},
    {"step": "LAS CONSECUENCIAS", "time_pct": "70-85%",
     "description": (
         "Cómo cambió sus vidas. Qué dice la ciencia (si puede decir algo). "
         "Testimonios, documentos, evidencias. 'Nadie volvió a ser el mismo.'"
     ),
     "retention_anchor": (
         "EL ESPEJO al 70%: Dirigirse directamente al viewer. "
         "'Ahora piensa en tu vida. En esas coincidencias que has ignorado. "
         "En esa vez que algo te salvó sin explicación.' Hacerlo personal."
     )},
    {"step": "EL CIERRE", "time_pct": "85-100%",
     "description": (
         "Reflexión: qué nos dice esto sobre el universo, el destino, "
         "o simplemente sobre lo mucho que nos queda por entender. "
         "Pregunta al viewer. End hook + CTA."
     )},
]

SCRIPT_EMOTIONAL_ARC = {
    "0-10%": "asombro", "10-20%": "curiosidad", "20-30%": "empatía",
    "30-45%": "intriga", "45-55%": "anticipación", "55-65%": "estupefacción",
    "65-75%": "esperanza → inspiración", "75-85%": "reflexión",
    "85-95%": "gratitud", "95-100%": "maravilla",
}

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

VIRALITY_TRIGGERS = [
    {"name": "Awe & Wonder",
     "mechanism": (
         "'No vas a creer lo que pasó.' El asombro genuino es la emoción "
         "que más se comparte en redes sociales. La gente comparte para "
         "provocar la misma reacción en otros."
     )},
    {"name": "Emotional Uplift",
     "mechanism": (
         "Contenido que hace sentir bien, da esperanza, inspira. "
         "La gente guarda y reenvía lo que la eleva emocionalmente. "
         "Historias de milagros = altísima tasa de reenvío."
     )},
    {"name": "Conversation Starter",
     "mechanism": (
         "Cerrar con pregunta existencial: '¿Crees en las casualidades "
         "o en el destino?' Comentarios = señal de engagement = más "
         "impresiones = más shares. El debate crea comunidad."
     )},
    {"name": "Identity Signaling",
     "mechanism": (
         "Compartir esta historia dice: 'Yo tengo la mente abierta.' "
         "o 'Yo sé cosas que la ciencia aún no explica.' "
         "Enmarcar el contenido como conocimiento especial."
     )},
    {"name": "Hope Trigger",
     "mechanism": (
         "Cada guion debe incluir un momento de esperanza genuina. "
         "'Si esto pudo pasarle a esta persona, tal vez...' "
         "La esperanza es la emoción más compartible en WhatsApp."
     )},
]

# ═══════════════════════════════════════════════════════════════════
# VOICE (TTS) — Channel-specific
# ═══════════════════════════════════════════════════════════════════

TTS_STRATEGY = {
    "voice_primary": "es-ES-AlvaroNeural",
    "voice_secondary": "es-MX-DaliaNeural",
    "rate_base": "-10%",
    "pitch_base": "+2Hz",
    "rate_hook": "-5%",
    "pitch_hook": "+0Hz",
    "rate_desarrollo": "-8%",
    "pitch_desarrollo": "+2Hz",
    "rate_climax": "-15%",
    "pitch_climax": "-4Hz",
    "rate_reflexion": "-10%",
    "pitch_reflexion": "+2Hz",
    "rate_cierre": "-5%",
    "pitch_cierre": "+4Hz",
}

VOICE_RATE = "-10%"
VOICE_PITCH = "+2Hz"

TTS_ENGINE = "kokoro"
KOKORO_VOICE = "em_santa"
KOKORO_BLOCK_SPEEDS = {
    "hook": 0.85, "desarrollo": 0.80, "climax": 0.75,
    "reflexion": 0.78, "cierre": 0.85,
}
KOKORO_PAUSE_BETWEEN_BLOCKS = 0.9

# ═══════════════════════════════════════════════════════════════════
# MARATHON CONFIG
# ═══════════════════════════════════════════════════════════════════

MARATHON_NARRATIVE_FORMAT = "miracles_and_coincidences"

MARATHON_TITLE_FORMULAS = [
    "{topic}: El Documental Que Cambiará Tu Visión De La Realidad",
    "Sincronías Imposibles: {topic} | Documental Completo",
    "{topic} — Casualidades Que La Ciencia No Puede Explicar",
    "Milagros Modernos: {topic} — Historias Reales",
    "El Misterio De {topic}: Sincronías Que Desafían La Lógica",
    "{topic}: Cuando El Universo Conspira | Documental HD",
    "Señales Del Destino: {topic} — Casos Documentados",
    "Lo Increíble De {topic}: Pruebas De Que Nada Es Casualidad",
]

MARATHON_HOOK_TYPES = [
    "misterio_sin_resolver",
    "revelacion_impactante",
    "asombro_cientifico",
    "conocimiento_exclusivo",
]

# ═══════════════════════════════════════════════════════════════════
# CONTENT SOURCES
# ═══════════════════════════════════════════════════════════════════

REDDIT_SUBREDDITS = [
    "Glitch_in_the_Matrix", "nevertellmetheodds", "HighStrangeness",
    "Thetruthishere", "Synchronicities", "paranormal", "Unexplained",
    "HumanPorn", "Damnthatsinteresting", "interestingasfuck",
    "todayilearned", "Coincidence", "Dreams", "NDE", "precognition",
    "TrueReddit", "AskReddit",
]

WIKIPEDIA_CATEGORIES = [
    "Coincidences", "Miracle", "Parapsychology", "Synchronicity",
    "Prophecy", "List of people who disappeared mysteriously",
    "Unexplained phenomena", "Forteana", "Unusual articles",
    "Paranormal terminology", "Spiritualism", "Near-death experiences",
    "Survival", "Luck", "Precognition", "Guardian angels",
    "Fenómenos paranormales", "Milagros", "Coincidencias",
    "Profecías", "Sincronicidad",
]

SCRAPE_SOURCES = [
    {"plugin": "reddit", "priority": 1},
    {"plugin": "wikipedia", "priority": 2},
    {"plugin": "atlas_obscura", "priority": 3},
    {"plugin": "rss", "priority": 4},
    {"plugin": "google_news", "priority": 5},
]

ATLAS_OBSCURA_CATEGORIES = ["wonders", "history", "unique"]
RSS_FEEDS = []
GOOGLE_NEWS_QUERIES = [
    "sincronicidades historias reales", "milagros inexplicables",
    "casualidades increíbles", "coincidencias misteriosas",
]

# ═══════════════════════════════════════════════════════════════════
# VISUAL STYLE
# ═══════════════════════════════════════════════════════════════════

IMAGE_STYLE_MODIFIERS = (
    "warm cinematic photography, golden hour lighting, atmospheric, 16:9, "
    "professional photography, hopeful luminous mood, ethereal light rays, "
    "soft focus, inspiring"
)
AI_VISUAL_COLOR_GRADING = (
    "luminous gold-and-indigo colour grade, warm amber highlights, rich navy "
    "shadows, vivid readable ethereal glow"
)

COLOR_PALETTE = {
    "primary": (212, 175, 55), "secondary": (15, 32, 62),
    "accent": (200, 120, 80), "text": (245, 240, 230),
    "text_shadow": (8, 6, 4), "tertiary": (40, 35, 30),
    "warning": (230, 180, 30),
}

FILM_GRAIN_OPACITY = 5
FILM_GRAIN_FRAMES = 8
KEN_BURNS_ZOOM_MIN = 4
KEN_BURNS_ZOOM_MAX = 12

# ═══════════════════════════════════════════════════════════════════
# MEDIA STRATEGY (channel-specific overrides)
# ═══════════════════════════════════════════════════════════════════

MEDIA_STRATEGY = {
    "media_per_block": 1, "prefer_video": True,
    "max_video_blocks_pct": 80, "target_video_pct": 80,
    "max_placeholder_pct": 0, "video_fallback_to_image": True,
    "video_min_duration": 4, "video_max_duration": 20,
    "video_sources": ["pexels"],
    "video_providers": DEFAULT_VIDEO_PROVIDERS,
    "video_fallback_queries": DEFAULT_VIDEO_FALLBACK_QUERIES,
    "fallback_query": "warm golden hour cinematic atmosphere light rays 16:9",
    "fallback_query_simple": "warm atmospheric hopeful cinematic",
    "ken_burns_zoom_min": 4, "ken_burns_zoom_max": 12,
    "crossfade_min": 0.3, "crossfade_max": 0.7,
    "ai_image_fallback": True, "ai_max_per_video": 5,
}

# ═══════════════════════════════════════════════════════════════════
# INTRO / OUTRO — channel-specific
# ═══════════════════════════════════════════════════════════════════

INTRO_FONT_SIZE = 72
INTRO_BG_COLOR = (15, 32, 62)
OUTRO_FONT_SIZE = 60
OUTRO_BG_COLOR = (15, 32, 62)
OUTRO_TEXT = "Suscríbete"
OUTRO_CTA_SUBSCRIBE = "❤️ Suscríbete"

CTA_TEXT = (
    "Si has llegado hasta aquí y esta historia te ha resonado,\n"
    "suscríbete y dale like\n"
    "para que más almas encuentren las señales del universo."
)
CTA_TEXT_VARIANTS = [
    ("Si has llegado hasta aquí y esta historia te ha resonado,\n"
     "suscríbete y dale like\n"
     "para que más almas encuentren las señales del universo."),
    ("Gracias por acompañarnos hasta el final de esta sincronía.\n"
     "Suscríbete y dale like,\n"
     "que la próxima señal ya está vibrando en el éter."),
    ("El universo ha hablado y tú has escuchado.\n"
     "Suscríbete, dale like y comparte:\n"
     "estas historias necesitan llegar a más almas."),
]

INTRO_VOICE_TEXT = "Bienvenidos a Sincronías, donde nada es casualidad."
CTA_VOICE_TEXT = "Si has llegado hasta aquí, esta historia era para ti. Suscríbete y dale like: la próxima señal puede cambiarlo todo."
OUTRO_VOICE_TEXT = "Gracias por acompañarnos. Hasta la próxima sincronía."

# ═══════════════════════════════════════════════════════════════════
# YOUTUBE METADATA
# ═══════════════════════════════════════════════════════════════════

YT_CATEGORY_ID = "24"  # Entertainment
PUBLISH_MODE = "scheduled"
PUBLISH_WARMUP_MIN = 30

UPLOAD_WINDOWS = [
    {"start": 10, "end": 13},
    {"start": 16, "end": 19},
    {"start": 20, "end": 22},
]

YT_DEFAULT_TAGS = [
    "casualidades imposibles", "milagros reales", "fenómenos inexplicables",
    "sincronías", "historias increíbles",
    "coincidencias sorprendentes", "predicciones cumplidas",
    "experiencias inexplicables reales", "milagros modernos documentados",
    "sueños premonitorios", "edgar mitchell astronauta misterio",
    "bligh motin bounty supervivencia", "mardani khel arte marcial india",
    "cern aceleracion del tiempo", "angeles reales grabados en video",
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
    "documental misterio español", "video ensayo inexplicable",
    "historias que desafían la lógica", "misterios reales documental",
    "historias para reflexionar",
]

# ═══════════════════════════════════════════════════════════════════
# SEO
# ═══════════════════════════════════════════════════════════════════

SEO_PRIMARY_KEYWORD = "milagros reales documentados"
SEO_SECONDARY_KEYWORDS = [
    "casualidades imposibles", "coincidencias inexplicables",
    "sincronías del universo", "fenómenos inexplicables reales",
    "milagros modernos", "predicciones cumplidas reales",
    "sueños premonitorios reales", "experiencias inexplicables documentadas",
    "casualidades sorprendentes historia", "sucesos paranormales reales",
    "glitches en la matrix casos reales", "sincronicidades jung ejemplos reales",
    "edgar mitchell telepatia espacio", "motin de la bounty supervivencia real",
    "colisionador de hadrones percepcion tiempo", "angeles grabados en video real",
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
    "documental misterio español", "video ensayo inexplicable",
    "historias que desafían la lógica", "misterios reales documental",
    "historias para reflexionar", "datos curiosos inexplicables",
    "historias reales que inspiran", "fenómenos que la ciencia no explica",
]

SEO_HASHTAGS = [
    "#Sincronías", "#MilagrosReales", "#Casualidades",
    "#Destino", "#HistoriasReales", "#LoInexplicable",
    "#Misterio", "#Documental", "#Fenómenos", "#Curiosidades",
    "#Inspiración", "#Universo", "#SabíasQue", "#Coincidencias",
    "#HistoriasQueInspiran",
]

# ═══════════════════════════════════════════════════════════════════
# SHORTS — channel-specific
# ═══════════════════════════════════════════════════════════════════

SHORTS_HASHTAGS = [
    "#Sincronías", "#MilagrosReales", "#SabíasQue", "#Shorts",
    "#Curiosidades", "#Destino", "#Misterio", "#Casualidades",
    "#LoInexplicable", "#HistoriasReales",
]
SHORTS_SUBSCRIBE_CTA_VARIANTS = [
    "Suscríbete para más historias como esta",
    "Dale like y suscríbete si quieres más",
    "En nuestro canal hay muchas más como esta",
    "Suscríbete y activa la campana para no perderte nada",
    "Si te gustó, suscríbete. Publicamos a diario.",
    "Únete al canal, cada día hay algo nuevo",
]

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
# THUMBNAIL — channel-specific
# ═══════════════════════════════════════════════════════════════════

THUMBNAIL_BORDER_WIDTH = 5
THUMBNAIL_FONT_FAMILY = "DejaVuSans-Bold"
THUMBNAIL_BORDER_COLOR = "#CC0000"
THUMBNAIL_SHOW_4K_BADGE = True
THUMBNAIL_TEXT_STROKE_COLOR = "#000000"

THUMBNAIL_VISUAL_STYLE = "moody_atmospheric"

THUMBNAIL_MANUAL_STYLE = {
    "visual_style": "moody_atmospheric",
    "color_palette": {
        "primary": "#1A1A3E", "accent": "#D4AF37",
        "text": "#FFD700", "shadow": "#0A0A0F",
    },
    "base_composition": "dark_reveal",
    "effects": {
        "contrast_boost": 1.15, "saturation": 0.85, "vignette": 0.35,
    },
    "text_style": {"uppercase": False, "max_words": 5},
    "pollo_prompt_suffix": (
        "soft cinematic lighting, mysterious atmosphere, warm golden accents, "
        "contemplative mood, golden hour glow, subtle color grading, artistic "
        "photography, 16:9 aspect ratio, photorealistic, professional documentary "
        "photography, rich textures, atmospheric depth"
    ),
}

THUMBNAIL_STYLE = {
    "layout": "image_full_background_text_overlay",
    "max_text_words": 4,
    "text_color": "warm_gold_on_deep_indigo",
    "font_style": "bold_elegant_sans_or_serif",
    "image_treatment": "warm_luminous_golden_hour_ethereal_glow",
    "background": "#0F203E", "accent_color": "#D4AF37",
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
        "text_position": "bottom_third_centered", "text_words": "2-3",
        "accent": "golden_light_ray_or_glow",
        "best_for": "Survivor stories, miracle interventions, NDE accounts",
    },
    "the_coincidence": {
        "description": "Split composition: two related elements connected by visual thread/line",
        "text_position": "center_bridging", "text_words": "3-4",
        "accent": "golden_connecting_line",
        "best_for": "Coincidence stories, synchronicities, parallel lives",
    },
    "the_evidence": {
        "description": "Vintage document, newspaper clipping, or photograph with warm sepia treatment",
        "text_position": "bottom_over_gradient", "text_words": "3-4",
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
    "Bienestar y espiritualidad", "Libros / Audiolibros",
    "Viajes y experiencias", "Tecnología", "Salud y bienestar mental",
]

# ═══════════════════════════════════════════════════════════════════
# END SCREEN
# ═══════════════════════════════════════════════════════════════════

END_SCREEN_STRATEGY = {
    "left_card": {
        "type": "playlist", "content": "most_relevant_playlist",
        "purpose": "Keep viewer in a session of wonder — thematic rabbit hole",
    },
    "center": {"type": "subscribe", "purpose": "Convert viewer to subscriber"},
    "right_card": {
        "type": "video", "content": "most_recent_upload",
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
        "slug": "historias-completas", "name": "Historias Completas",
        "description": (
            "Documentales en profundidad sobre los milagros, casualidades y "
            "fenómenos inexplicables más sorprendentes de la historia. "
            "Cada video: contexto, protagonistas, el suceso y las consecuencias."
        ),
        "type": "main",
    },
    {
        "slug": "lo-mas-increible", "name": "Lo Más Increíble",
        "description": (
            "Las 5 historias más asombrosas del canal. Si eres nuevo aquí, "
            "empieza por esta lista. Bienvenido a Sincronías."
        ),
        "type": "onboarding",
    },
    {
        "slug": "milagros-modernos", "name": "Milagros Modernos",
        "description": (
            "Milagros documentados del siglo XX y XXI. Evidencias, "
            "testimonios y el contexto científico de cada caso."
        ),
        "type": "thematic",
    },
    {
        "slug": "casualidades-imposibles", "name": "Casualidades Imposibles",
        "description": (
            "Las coincidencias más alucinantes de la historia. Probabilidades "
            "de una entre millones que ocurrieron. El universo conspirando."
        ),
        "type": "thematic",
    },
    {
        "slug": "predicciones-que-se-cumplieron", "name": "Predicciones que se Cumplieron",
        "description": (
            "Sueños premonitorios, profecías acertadas y personas que vieron "
            "el futuro antes de que ocurriera. Documentado y verificado."
        ),
        "type": "thematic",
    },
]

# ═══════════════════════════════════════════════════════════════════
# VIRAL PLAYLIST KEYWORDS (English mirror)
# DEPRECATED: no se lee en runtime (el query builder usa
# PLAYLISTS_GENERATED/keywords_en). Se conserva como referencia.
# ═══════════════════════════════════════════════════════════════════

VIRAL_PLAYLIST_KEYWORDS = {
    "historias-completas": [
        "unexplained miracles documentary",
        "real life miracles caught on camera",
        "incredible true stories documentary",
        "synchronicity explained documentary",
    ],
    "lo-mas-increible": [
        "most incredible coincidences in history",
        "unbelievable true stories",
        "mind blowing coincidences",
        "things science cannot explain",
    ],
    "milagros-modernos": [
        "modern day miracles documentary",
        "medical miracles unexplained",
        "real miracles documentary 2025",
        "miraculous events caught on camera",
    ],
    "casualidades-imposibles": [
        "impossible coincidences documentary",
        "twin strangers documentary",
        "synchronicity real cases",
        "meaningful coincidences documentary",
    ],
    "predicciones-que-se-cumplieron": [
        "predictions that came true documentary",
        "prophecies fulfilled throughout history",
        "nostradamus predictions that came true",
        "future predictions that were right",
    ],
}

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
# CROSS-PLATFORM
# ═══════════════════════════════════════════════════════════════════

CROSS_PLATFORM = {
    "tiktok": {
        "enabled": True,
        "posts_per_day": 2,
        "format": "clip_vertical_60s",
        "hook_template": "¿Sabías que {hook_frase}? #sincronias #misterio #documental",
        "cadence": "morning_evening",
    },
    "youtube_shorts": {
        "enabled": True,
        "posts_per_day": 3,
        "format": "shorts_vertical_60s",
        "hook_template": "{hook_frase} #Shorts",
    },
    "twitter_x": {
        "enabled": True,
        "posts_per_day": 3,
        "format": "text_thread",
        "hook_template": "{titulo_video}\n\n{hook_frase}\n\n🧵 Abro hilo...",
    },
    "spotify_podcast": {
        "enabled": True,
        "format": "audio_only",
        "cadence": "per_video",
    },
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
                "persona sobreviviera eran de 1 entre 10 millones. Mañana "
                "te cuento cómo ocurrió.'"
            ),
        },
        "friday": {
            "type": "question",
            "example": "¿Cuál es la casualidad más increíble que has vivido? Compártela abajo.",
        },
    },
}

# ═══════════════════════════════════════════════════════════════════
# CONTENT PILLARS
# ═══════════════════════════════════════════════════════════════════

CONTENT_PILLARS = [
    {
        "pillar": "Milagros Modernos",
        "pct": 30,
        "description": (
            "Historias reales de milagros documentados en los últimos 100 años. "
            "Curaciones inexplicables, intervenciones divinas, eventos "
            "imposibles con evidencia médica y testimonial."
        ),
    },
    {
        "pillar": "Casualidades Imposibles",
        "pct": 30,
        "description": (
            "Coincidencias que desafían toda probabilidad estadística. "
            "Historias de personas que estuvieron en el lugar y momento "
            "exactos para que ocurriera algo extraordinario."
        ),
    },
    {
        "pillar": "Fenómenos que la Ciencia No Explica",
        "pct": 25,
        "description": (
            "Fenómenos paranormales, científicos o naturales documentados "
            "que la comunidad científica aún no ha logrado explicar."
        ),
    },
    {
        "pillar": "Predicciones y Premoniciones",
        "pct": 15,
        "description": (
            "Personas que predijeron eventos con precisión asombrosa. "
            "Sueños premonitorios, profecías verificadas, visiones que "
            "se hicieron realidad."
        ),
    },
]

# ═══════════════════════════════════════════════════════════════════
# VIRAL KEYWORDS (English — for YouTube search abroad)
# ═══════════════════════════════════════════════════════════════════

NICHE_KEYWORDS_ENG = [
    "unexplained mysteries", "real miracles", "impossible coincidences",
    "synchronicity stories", "real life miracles", "unexplained phenomena",
    "miracle stories true", "incredible coincidences",
    "paranormal true stories", "things science can't explain",
    "destiny or coincidence", "real angel encounters",
]
