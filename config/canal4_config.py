"""Configuration for Canal 4: Expediciones sin retorno.

Only CHANNEL-SPECIFIC parameters. Everything else inherits from config.defaults.py.
"""
from config.settings import DEFAULT_VIDEO_PROVIDERS, DEFAULT_VIDEO_FALLBACK_QUERIES

CANAL_NAME = "canal4"
CANAL_DISPLAY_NAME = "Expediciones sin retorno"
CANAL_TAGLINE = ("Historias reales de expediciones que terminaron catastróficamente mal... "
                 "y los pocos que lograron volver.")
CANAL_OUTRO_TAGLINE = ("La historia de esta expedición es real. Los nombres, las fechas, "
                       "lo que encontraron... todo ocurrió.")
YOUTUBE_HANDLE = "@ExpedicionesSinRetorno"
YOUTUBE_CHANNEL_URL = "https://www.youtube.com/@ExpedicionesSinRetorno"
CANAL_NARRATIVE_STYLE = "documental de supervivencia"
CANAL_STYLE_DESCRIPTION = (
    "Expediciones reales llevadas al límite. Hielo, desierto, montaña, océano. "
    "Historias de personas que empujaron las fronteras humanas... y pagaron el precio."
)
CHANNEL_ABOUT_SECTION = """Bienvenido a Expediciones sin retorno.

Documentales sobre las exploraciones que terminaron en desastre: expediciones al Artico atrapadas en el hielo, naufragios en medio del océano, escaladas imposibles y travesias del desierto que nadie completo. Historias reales de personas que empujaron los limites humanos... y a veces no regresaron.

🎬 Formato: video ensayos documentales (8-14 minutos)
🗓️ Nuevas historias: cada semana
🎙️ Narracion documental con fuentes verificadas
📩 Contacto: {email}
⛵ Si te fascinan las historias de exploracion, supervivencia extrema y el drama humano en los entornos mas hostiles del planeta... este canal es para ti.
Suscribete y activa la campana para no perderte ninguna expedicion."""
CHANNEL_KEYWORDS = [
    "expediciones fallidas", "exploracion y supervivencia", "naufragios historicos",
    "tragedias en el everest", "documentales de supervivencia", "expediciones al artico",
    "explotadores desaparecidos", "historia real de exploracion", "accidentes de montaña",
    "supervivencia extrema", "naufragio real", "expediciones perdidas",
    "documental exploracion", "historias de supervivencia", "tragedias en expediciones",
    "exploracion polar", "expedicion franklin", "donner party",
    "documental en español", "historias increibles reales",
]
CANAL_INITIALS = "ESR"
LOGO_SIZE = 180

PROD_SCRIPT_WORDS_MIN = 2200
PROD_SCRIPT_WORDS_MAX = 3800
PROD_SCRIPT_SCENES_MIN = 10
PROD_SCRIPT_SCENES_MAX = 18
PROD_SCRIPT_BLOCKS_MIN = 10
PROD_SCRIPT_BLOCKS_MAX = 18
PROD_VIDEO_DURATION_MIN = 12
PROD_VIDEO_DURATION_MAX = 16
VIDEO_AVERAGE_DURATION_MIN = 14
VIDEO_DURATION_DISCREPANCY_MIN = 3

CANAL_TONE = (
    "Grave, tenso y profundamente humano. Narrativa documental que oscila "
    "entre el asombro y el horror. Riguroso en los hechos, implacable en la "
    "atmósfera, profundamente empático con las personas que vivieron la pesadilla."
)
TARGET_AUDIENCE = (
    "18-45 años (amplio), LATAM (MX 30%, CO 20%, AR 15%, PE 10%, ES 15%, otros 10%). "
    "Curiosos, amantes de la aventura, historia, documentales de naturaleza y supervivencia. "
    "60% hombres / 40% mujeres. 60%+ mobile. Sesiones de 8-12 min."
)
TARGET_AUDIENCE_PSYCHOGRAPHIC = {
    "The Armchair Explorer": ("Consume contenido de exploración desde el sillón. Le encanta el drama humano."),
    "The Survival Enthusiast": ("Ve estos documentales para aprender. Analiza cada decisión."),
    "The History Lover": ("Busca historias reales bien documentadas. Valora fuentes y contexto histórico."),
    "The Thrill Seeker": ("Adrenalina desde la pantalla. Quiere sentir la tensión y el peligro."),
}

TITLE_FORMULAS = [
    "La Expedicion {name}: {number} Partieron, Solo {survivors} Regresaron",
    "Atrapados en {location}: {number} Dias Sin Comida a {temperature} Bajo Cero",
    "El Explorador que su Propia Tripulacion Abandono a la Deriva",
    "Buscaban {goal}. Encontraron la Muerte en {horror_detail}.",
    "Sobrevivio {number} Dias en {location}. Nadie Sabe Como.",
    "{number} Personas Entraron en {location}. Ninguna Salio.",
    "La Expedicion que Te Hara Preguntarte si Habrias Sobrevivido",
]
TITLE_POWER_WORDS = [
    "revelado", "filtrado", "censurado", "inédito", "clasificado",
    "confidencial", "prohibido", "archivado", "silenciado",
    "desclasificado", "ocultado", "suprimido", "enterrado",
    "escalofriante", "desgarrador", "inexplicable", "demoledor",
    "sobrecogedor", "estremecedor", "alucinante", "aterrador",
    "implacable", "extremo", "brutal", "salvaje", "inhóspito",
    "despiadado", "crucial", "angustiante", "desgarrador",
    "sobrecogedor", "desolador", "sombrío", "trágico",
    "oculto", "secreto", "perturbador", "siniestro", "enigmático",
    "impactante", "increíble", "insólito", "intrigante",
    "enigma", "misterio", "inquietante",
    "desapareció", "nunca regresó", "atrapados", "perdidos", "abandonados",
    "muertos", "congelados", "hundidos", "sepultados",
    "desvanecido", "devorados", "ahogados",
    "sobrevivió", "rescatado", "encontrado", "escapó", "volvió",
    "emergió", "resistió", "aguantó", "superó", "logró salir",
    "milagrosamente", "contra todo pronóstico",
    "hielo", "nieve", "tormenta", "océano", "desierto", "montaña",
    "selva", "abisal", "infierno", "gélido", "abrasador",
    "helado", "ardiente", "impenetrable", "remoto",
    "inaccesible", "aislado", "traidor", "mortal",
    "real", "documentado", "verificado", "demostrado", "confirmado",
    "registrado", "grabado", "filmado",
]
TITLE_MAX_CHARS = 65

SCRIPT_HOOK_RULE = (
    "ATENCION: La primera frase del guion DEBE ser el hecho mas "
    "impactante de la expedicion, con un NUMERO y un HECHO CONCRETO. "
    "NUNCA empezar con contexto historico, definiciones, ni presentaciones. "
    "NUNCA 'Hola, bienvenidos a...' ni 'En este video vamos a hablar de...'.\n\n"
    "EJEMPLO CORRECTO: 'El 19 de mayo de 1845, 129 hombres zarparon de "
    "Inglaterra en dos barcos de ultima generacion. Ninguno volvio a ver "
    "a su familia jamas.'\nEJEMPLO INCORRECTO: 'Las expediciones polares del "
    "siglo XIX fueron un periodo de intensa exploracion artica que...'."
)
SCRIPT_STRUCTURE = [
    {"step": "EL FRIO", "time_pct": "0-10%",
     "description": "El hecho mas impactante en frio. Sin contexto."},
    {"step": "EL SUEÑO", "time_pct": "10-20%",
     "description": "Que buscaban, por que zarparon. 'Nada podia fallar.'"},
    {"step": "LOS PROTAGONISTAS", "time_pct": "20-30%",
     "description": "Las personas reales detras de la historia.",
     "retention_anchor": "CLIFFHANGER al 25%: 'Pero lo que esta persona no sabia...'"},
    {"step": "EL DESCENSO", "time_pct": "30-55%",
     "description": "Todo empieza a torcerse. El hielo que no cede, la tormenta inesperada.",
     "retention_anchor": "CLIFFHANGER al 50%: Silencio 2s. 'Ahora viene lo peor.'"},
    {"step": "EL COLAPSO", "time_pct": "55-70%",
     "description": "El momento exacto de la catastrofe. Peak de tension dramatica."},
    {"step": "LA SUPERVIVENCIA", "time_pct": "70-85%",
     "description": "Lo que hicieron para intentar sobrevivir.",
     "retention_anchor": "EL ESPEJO al 70%: 'Ahora piensa: ¿que habrias hecho tu?'"},
    {"step": "EL RESCATE / LEGADO", "time_pct": "85-100%",
     "description": "Como termino. Los que sobrevivieron, los que no."},
]
SCRIPT_END_HOOK = (
    "Y si crees que esta expedicion fue tragica, espera a ver {next_expedition}. "
    "Porque lo que le ocurrio a esa tripulacion fue todavia mas increible. "
    "Ese es el proximo video. Dale like, suscribete y activa la campana."
)
SCRIPT_EMOTIONAL_ARC = {
    "0-10%": "impacto", "10-20%": "anticipacion tragica", "20-30%": "empatia",
    "30-45%": "tension creciente", "45-55%": "angustia", "55-65%": "horror",
    "65-75%": "desesperacion → instinto", "75-85%": "alivio amargo / duelo",
    "85-95%": "reflexion", "95-100%": "respeto",
}
RETENTION_ANCHORS = {
    "at_25_pct": {"trigger": "cliffhanger_mid_video",
                  "action": "Insertar mini-cliffhanger: 'Pero lo que ocurrio 3 dias despues cambio todo.'"},
    "at_50_pct": {"trigger": "the_reset",
                  "action": "Musica fuera 2s. 'Recapitulemos: [resumen]. Ahora viene lo peor.'"},
    "at_70_pct": {"trigger": "the_mirror",
                  "action": "Dirigirse al viewer: 'Ahora piensa: ¿que habrias hecho tu con -40 grados y sin comida?'"},
}
VIRALITY_TRIGGERS = [
    {"name": "Survival Instinct", "mechanism": "'¿Habrias sobrevivido tu?' La pregunta personal activa el instinto del viewer."},
    {"name": "Awe of Nature", "mechanism": "La naturaleza como antagonista imponente."},
    {"name": "Moral Dilemma", "mechanism": "Cerrar con pregunta etica: '¿Abandonarias a tu compañero herido?'"},
    {"name": "Conversation Starter", "mechanism": "'No sabia que esto habia pasado.' La gente comparte para sorprender."},
    {"name": "Respect for the Fallen", "mechanism": "Tratar a los exploradores fallecidos con profundo respeto."},
]

# Voice / TTS — edgetts, specific rates
TTS_STRATEGY = {
    "voice_primary": "es-ES-AlvaroNeural", "voice_secondary": "es-MX-DaliaNeural",
    "rate_base": "+0%", "pitch_base": "+0Hz",
    "rate_hook": "-5%", "pitch_hook": "-2Hz",
    "rate_desarrollo": "-15%", "pitch_desarrollo": "+0Hz",
    "rate_climax": "-22%", "pitch_climax": "-8Hz",
    "rate_reflexion": "-18%", "pitch_reflexion": "-2Hz",
    "rate_cierre": "-10%", "pitch_cierre": "+2Hz",
}
VOICE_RATE = "+0%"
VOICE_PITCH = "+0Hz"
TTS_ENGINE = "edgetts"
KOKORO_VOICE = "em_alex"
KOKORO_BLOCK_SPEEDS = {"hook": 1.02, "desarrollo": 0.95, "climax": 0.82, "reflexion": 0.90, "cierre": 0.97}
KOKORO_PAUSE_BETWEEN_BLOCKS = 0.8

# Content Sources
REDDIT_SUBREDDITS = [
    "Survival", "expedition", "Mountaineering", "shipwrecks", "Maritime",
    "WildernessBackpacking", "History", "TrueReddit", "todayilearned",
    "AskHistorians", "HistoryAnecdotes", "Damnthatsinteresting", "interestingasfuck",
    "AbandonedPorn", "natureismetal", "HumanPorn", "CatastrophicFailure",
    "AskReddit", "UnsolvedMysteries", "HighStrangeness",
]
WIKIPEDIA_CATEGORIES = [
    "Exploration disasters", "Shipwrecks", "Maritime disasters", "Mountaineering deaths",
    "Explorers lost at sea", "Lost explorers", "Arctic expeditions", "Antarctic expeditions",
    "Survival", "People lost at sea", "Shipwrecks in the Arctic Ocean",
    "Disasters in Antarctica", "Missing aviators", "Sole survivors", "Cannibalism",
    "Desert survival", "Mountain disasters", "Failed expeditions",
    "Naufragios", "Expediciones al Artico", "Expediciones a la Antartida",
    "Exploradores de España", "Accidentes de montaña", "Desastres maritimos",
    "Naufragios en el Atlantico",
]
SCRAPE_SOURCES = [
    {"plugin": "reddit", "priority": 1}, {"plugin": "wikipedia", "priority": 2},
    {"plugin": "atlas_obscura", "priority": 3}, {"plugin": "rss", "priority": 4},
    {"plugin": "google_news", "priority": 5},
]
ATLAS_OBSCURA_CATEGORIES = ["abandoned", "natural-wonders", "unique", "maritime"]
RSS_FEEDS = []
GOOGLE_NEWS_QUERIES = [
    "expedicion desaparecida", "rescate montaña", "naufragio historico",
    "explorador perdido", "supervivencia extrema",
]

# Visual Style
IMAGE_STYLE_MODIFIERS = (
    "cinematic documentary photography, natural lighting, wide shot, 16:9, "
    "professional photography, dramatic atmosphere, expedition landscape"
)
COLOR_PALETTE = {
    "primary": (15, 40, 65), "secondary": (18, 28, 50), "accent": (255, 92, 0),
    "text": (235, 240, 245), "text_shadow": (4, 6, 12), "tertiary": (35, 45, 55),
    "warning": (255, 92, 0),
}
IMAGE_TINT_COLOR = (40, 42, 48)
FILM_GRAIN_OPACITY = 5
FILM_GRAIN_FRAMES = 10
KEN_BURNS_ZOOM_MIN = 10
KEN_BURNS_ZOOM_MAX = 18
VIDEO_RESOLUTION = (1280, 720)

MEDIA_STRATEGY = {
    "media_per_block": 1, "prefer_video": True,
    "max_video_blocks_pct": 80, "target_video_pct": 80, "max_placeholder_pct": 0,
    "video_fallback_to_image": True, "video_min_duration": 4, "video_max_duration": 20,
    "video_sources": ["pexels"], "video_providers": DEFAULT_VIDEO_PROVIDERS,
    "video_fallback_queries": DEFAULT_VIDEO_FALLBACK_QUERIES,
    "fallback_query": "dramatic expedition landscape cinematic 16:9",
    "fallback_query_simple": "expedition wilderness dramatic nature",
    "ken_burns_zoom_min": 10, "ken_burns_zoom_max": 18,
    "crossfade_min": 0.3, "crossfade_max": 0.7,
    "ai_image_fallback": True, "ai_max_per_video": 5,
}

# Intro / Outro
INTRO_FONT_SIZE = 68
INTRO_BG_COLOR = (6, 12, 24)
OUTRO_DURATION_SEC = 6.0
OUTRO_FONT_SIZE = 52
OUTRO_BG_COLOR = (6, 12, 24)
OUTRO_TEXT = "Suscribete"
OUTRO_CTA_SUBSCRIBE = "❤️ Suscribete"
CTA_TEXT = ("Si has llegado hasta aqui y te ha atrapado esta aventura,\n"
            "suscribete y dale like\npara seguir explorando juntos las expediciones mas extremas.")
CTA_TEXT_VARIANTS = [
    ("Si has llegado hasta aqui y te ha atrapado esta aventura,\n"
     "suscribete y dale like\npara seguir explorando juntos las expediciones mas extremas."),
    ("Gracias por sobrevivir a esta expedicion con nosotros.\n"
     "Suscribete y dale like:\nla proxima aventura te dejara sin aliento."),
    ("Has llegado al final de esta travesia, pero aun queda mucho por explorar.\n"
     "Suscribete, dale like y comparte\npara que mas gente descubra lo que hay mas alla."),
]
INTRO_VOICE_TEXT = "Bienvenidos a Expediciones sin retorno, donde no todos vuelven para contarlo."
CTA_VOICE_TEXT = "Si has llegado hasta aqui, ya eres parte de la expedicion. Suscribete y dale like para no perderte la proxima aventura."
OUTRO_VOICE_TEXT = "Gracias por ver. Nos vemos en la proxima expedicion."

# YouTube
YT_CATEGORY_ID = "27"
PUBLISH_MODE = "scheduled"
PUBLISH_WARMUP_MIN = 30
UPLOAD_WINDOWS = [{"start": 10, "end": 13}, {"start": 14, "end": 17}, {"start": 20, "end": 22}]
YT_DEFAULT_TAGS = [
    "expediciones fallidas", "exploraciones que salieron mal", "tragedias en expediciones",
    "supervivencia extrema documental", "naufragios historicos",
    "expedicion franklin", "donner party historia real", "naufragio endurance",
    "everest desastre 1996", "exploracion artica tragedias",
    "tragedia batalla badr", "desaparicion vasco de ataide",
    "naufragio nao portuguesa", "red october conspiración filipinas",
    "fabrica abandonada virginia 1912", "jerome park misterio Nueva York",
    "que paso en el everest 1996 documental", "historia real donner party canibalismo",
    "como sobrevivio shackleton en la antartida", "naufragios famosos que nunca se encontraron",
    "expediciones que terminaron en tragedia", "historias reales de supervivencia en el mar",
    "exploradores que desaparecieron sin dejar rastro", "accidentes en el monte everest documental",
    "exploracion del artico siglo xix", "tragedias en alta mar documental español",
    "supervivientes de naufragios historias reales", "expediciones perdidas en la selva",
    "montañeros que murieron en el everest", "desastres navales peores de la historia",
    "misterios sin resolver expediciones", "documental español expediciones",
    "historia real documental exploracion", "video ensayo tragedias historicas",
    "historias increibles documental", "mejores documentales de supervivencia",
    "descubrimientos arqueologicos en el hielo", "barcos hundidos encontrados recientemente",
    "tragedias en montaña documental", "exploradores españoles olvidados",
    "rutas de exploracion peligrosas historia", "desaparecidos en expediciones famosos",
    "misterios maritimos sin resolver", "leyendas de naufragios documental",
    "aventuras extremas que salieron mal", "lugares mas peligrosos del mundo exploracion",
]

# SEO
SEO_PRIMARY_KEYWORD = "expediciones fallidas reales"
SEO_SECONDARY_KEYWORDS = [
    "exploraciones que salieron mal", "tragedias en expediciones", "supervivencia extrema documental",
    "naufragios historicos documental", "expediciones fallidas reales",
    "expedicion franklin documental", "donner party historia real", "naufragio endurance shackleton",
    "everest desastre 1996 documental", "exploracion artica tragedias",
    "tragedia batalla de badr", "desaparicion vasco de ataide naufragio",
    "naufragio nao portuguesa documental", "red october filipinas golpe de estado",
    "fabrica abandonada virginia can company", "jerome park nueva york leyenda urbana",
    "juncus articulatus planta misteriosa", "shayba ibn rabi duelo badr",
    "que paso realmente en el everest 1996", "historia real de la donner party canibalismo",
    "como sobrevivio shackleton en la antartida", "peores naufragios de la historia",
    "expediciones que terminaron en tragedia documental", "historias reales de supervivencia en el mar",
    "exploradores que desaparecieron misteriosamente", "accidentes en el everest documental español",
    "tragedias en alta mar historias reales", "supervivientes de naufragios famosos",
    "expediciones perdidas en la amazonia", "montañeros que murieron en el himalaya",
    "desastres navales mas grandes de la historia", "misterios sin resolver de expediciones",
    "documental español exploracion supervivencia", "video ensayo tragedias historicas",
    "mejores historias de supervivencia documental",
    "barcos hundidos encontrados recientemente", "misterios maritimos sin resolver documental",
    "lugares mas peligrosos del mundo exploracion", "exploradores españoles olvidados historia",
    "desaparecidos en montaña casos reales", "leyendas de naufragios historias reales",
    "aventuras extremas que salieron mal documental", "lugares abandonados con historia documental",
]
SEO_HASHTAGS = [
    "#ExpedicionesSinRetorno", "#Supervivencia", "#Documental", "#Naufragios",
    "#Historia", "#Exploracion", "#Aventura", "#Naturaleza", "#Curiosidades",
    "#HistoriasReales", "#AprendeEnYouTube", "#Montaña", "#Oceano", "#Desierto", "#Exploradores",
]

SHORTS_PER_DAY = 2
SHORTS_HASHTAGS = [
    "#ExpedicionesSinRetorno", "#Supervivencia", "#Shorts", "#SabiasQue",
    "#AprendeEnYouTube", "#Historia", "#Naufragio", "#Curiosidades", "#Documental", "#Aventura",
]

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

🎙️ Bienvenido a **Expediciones sin retorno** — el canal donde documentamos las exploraciones que terminaron en desastre.

📚 Fuentes: Wikipedia, archivos historicos, hilos de Reddit (r/History, r/Survival, r/shipwrecks) y documentos de exploracion verificados.
⚠️ Todo el contenido tiene fines educativos y de divulgacion historica.

🔔 Suscribete y activa la campana para descubrir mas expediciones que desafiaron a la naturaleza... y perdieron.

💬 ¿Crees que habrias sobrevivido en esta expedicion? Dejalo en los comentarios.

———

🎬 VIDEOS RELACIONADOS DE NUESTRO CANAL
{related_videos}

#ExpedicionesSinRetorno #Supervivencia #HistoriasReales"""

# Thumbnail
THUMBNAIL_BORDER_WIDTH = 7
THUMBNAIL_FONT_FAMILY = "DejaVuSans-Bold"
THUMBNAIL_BORDER_COLOR = "#FF5C00"
THUMBNAIL_SHOW_4K_BADGE = True
THUMBNAIL_TEXT_STROKE_COLOR = "#000000"
THUMBNAIL_VISUAL_STYLE = "distress_signal"
THUMBNAIL_RESCUE_MAYDAY = False
THUMBNAIL_RESCUE_COORDINATES = True
THUMBNAIL_RESCUE_SIN_SENAL = True
THUMBNAIL_ALLOW_FACES = True
THUMBNAIL_CONCEPT_DIRECTIVE = (
    "El rostro humano con expresion de sorpresa/impacto PUEDE aparecer pero "
    "PEQUEÑO (max ~25-30% del encuadre), integrado en el entorno de la expedicion. "
    "El PAISAJE/ENTORNO de la expedicion es el protagonista."
)
THUMBNAIL_MANUAL_STYLE = {
    "visual_style": "distress_signal",
    "color_palette": {"primary": "#0F2841", "accent": "#FF5C00", "text": "#FFFFFF", "shadow": "#060C18"},
    "base_composition": "dark_reveal",
    "effects": {"contrast_boost": 1.20, "saturation": 0.80, "vignette": 0.35, "brightness_boost": 1.25},
    "text_style": {"uppercase": True, "max_words": 4},
    "pollo_prompt_suffix": (
        "cold desaturated cinematography, arctic survival atmosphere, emergency orange accents, "
        "dramatic natural lighting, photorealistic, 16:9, National Geographic documentary style"
    ),
}
THUMBNAIL_STYLE = {
    "layout": "image_full_background_text_overlay", "max_text_words": 4,
    "text_color": "frost_white_on_arctic_blue", "font_style": "bold_sans_serif_condensed",
    "image_treatment": "cold_desaturated_dramatic_contrast_documentary",
    "background": "#060C18", "accent_color": "#FF5C00",
    "face_policy": "Real faces YES — weathered, cold, expeditionary expressions. AI-generated faces: NO.",
    "number_preference": "odd_numbers_for_lists",
    "rescue_orange_accent_rule": "Orange distress accents on danger elements boost CTR.",
}
THUMBNAIL_TEMPLATES = {
    "the_ship": {"description": "Ship trapped in ice, cold blue tones", "text_position": "bottom_third",
                 "text_words": "2-3", "accent": "red_highlight_on_ice",
                 "best_for": "Naufragios y expediciones maritimas"},
    "the_mountain": {"description": "Summit or cliff face, figure for scale, storm clouds", "text_position": "upper_or_center_third",
                     "text_words": "2-3", "accent": "red_line_on_route",
                     "best_for": "Expediciones de montaña"},
    "the_survivor": {"description": "Close-up portrait or silhouette, weather-beaten", "text_position": "bottom_over_dark_gradient",
                     "text_words": "3-4", "accent": "red_arrow_on_eyes",
                     "best_for": "Historias de supervivientes"},
}

VIDEO_MIDROLL_STRATEGY = (
    "Colocar mid-rolls en pausas naturales entre capitulos narrativos. "
    "NUNCA en medio de una frase ni durante el clímax."
)
MONETIZATION_TARGET_CPM = "$5–$12 USD"
MONETIZATION_VERTICALS = [
    "Aventura y outdoor", "Viajes y experiencias", "Libros / Audiolibros",
    "Educacion online", "Documentales y streaming", "Equipamiento de supervivencia",
]

END_SCREEN_STRATEGY = {
    "left_card": {"type": "playlist", "content": "most_relevant_playlist",
                  "purpose": "Keep viewer in a survival session — thematic rabbit hole"},
    "center": {"type": "subscribe", "purpose": "Convert viewer to subscriber"},
    "right_card": {"type": "video", "content": "most_recent_upload",
                   "purpose": "Push newest content to engaged viewers"},
    "spoken_cta": ("Si esta expedicion te parecio tragica, la siguiente en la lista "
                   "lo es todavia mas. Suscribete si quieres descubrir mas historias de exploracion."),
}

PLAYLISTS = [
    {"slug": "tragedias-polares", "name": "Tragedias Polares",
     "description": "Expediciones articas y antarticas: Franklin, Shackleton, Scott...", "type": "thematic"},
    {"slug": "naufragios-historicos", "name": "Naufragios Historicos",
     "description": "Barcos que nunca llegaron a puerto.", "type": "thematic"},
    {"slug": "montanas-mortales", "name": "Montañas Mortales",
     "description": "Everest, K2, Annapurna, los Andes.", "type": "thematic"},
    {"slug": "desiertos-y-selvas", "name": "Desiertos y Selvas",
     "description": "Travesias terrestres extremas.", "type": "thematic"},
    {"slug": "lo-mas-impactante", "name": "Lo Mas Impactante",
     "description": "Las 5 expediciones mas tragicas del canal.", "type": "onboarding"},
]

FIRST_48H_STRATEGY = {
    "pre_upload_24h": ["Community Tab poll: '¿En que entorno extremo sobrevivirias menos?'", "YouTube Story: imagen dramatica"],
    "hour_0": ["Publish at 9PM Mexico City time", "First comment: dilema de supervivencia"],
    "hours_1_6": ["Reddit r/History: TEXT post", "Facebook groups: Historia, Documentales, Aventura"],
    "hours_6_24": ["Reply to EVERY comment", "Twitter/X thread: 5-7 tweets + YouTube link"],
    "hours_24_48": ["Analyze CTR", "If CTR < 5%: swap thumbnail"],
}
COMMUNITY_TAB_PLAN = {
    "frequency": "3x/week",
    "schedule": {
        "monday": {"type": "poll", "example": "¿Que expedicion tragica te parece mas increible?",
                   "options": ["Franklin (Artico)", "Donner Party", "Everest 1996", "Barco Endurance"]},
        "wednesday": {"type": "image_fact", "example": "Fotografia de la expedicion + dato tragico"},
        "friday": {"type": "teaser", "example": "Este sabado: la expedicion que nadie habia intentado."},
    },
}
CROSS_PLATFORM = {
    "tiktok": {"format": "60-90s vertical", "hook_template": "{number} personas entraron en {location}. Solo {survivors} salieron.",
               "cadence": "3x/day (days 1, 3, 5)"},
    "youtube_shorts": {"format": "15-30s most dramatic moment", "end_cta": "'Video completo en el canal'"},
    "twitter_x": {"format": "Thread — 1 expedition/week",
                  "template": "'HOY en Expediciones sin retorno: {expedition_name}...'"},
    "spotify_podcast": {"format": "Audio-only export", "title_format": "Expediciones sin retorno | {expedition_name}"},
}
COLLABORATION_TARGETS = {
    "tier_1_direct": [{"name": "Ciencia de Sofa", "niche": "Divulgacion"}, {"name": "El Robot de Platon", "niche": "Historia y ciencia"}, {"name": "Antroporama", "niche": "Antropologia"}],
    "tier_2_adjacent": [{"name": "Mundo Desconocido", "niche": "Misterio y fenomenos"}],
    "collab_formats": ["React: 'Un superviviente reacciona a Expediciones sin retorno'", "Topic trade"],
}
TRENDING_TOPIC_HOOKS = {
    "type_a_news": {"trigger": "Rescate de montañeros, naufragio en noticias",
                    "pivot": "Esto que acaba de pasar... ya ocurrio antes. Y fue mucho peor."},
    "type_b_anniversary": {"trigger": "Aniversario de expediciones famosas",
                           "calendar": {"may": "Everest", "december": "Tragedias navideñas", "april": "Titanic"}},
    "type_c_pop_culture": {"trigger": "Estreno de pelicula/serie de expediciones",
                           "strategy": "'La historia REAL detras de {show/movie}'"},
    "type_d_calendar": {"name": "Calendario de Expediciones Tragicas",
                        "months": {"january": "Shackleton", "march": "Expediciones polares", "may": "Everest", "september": "Huracanes", "december": "Tragedias navideñas"}},
}
CONTENT_PILLARS = [
    {"name": "La Expedicion", "ratio": 55, "desc": "Documental profundo de una expedicion tragica individual"},
    {"name": "Recopilaciones", "ratio": 25, "desc": "Compilacion tematica"},
    {"name": "El Analisis", "ratio": 20, "desc": "Video mas corto analizando que salio mal"},
]

NICHE_KEYWORDS_ENG = [
    "survival stories", "expeditions gone wrong", "unexplained disappearances",
    "survival documentary", "lost in the wilderness", "expedition mysteries",
    "true survival stories", "missing explorers", "wilderness survival documentary",
    "deadliest expeditions", "survival against all odds", "mysterious disappearances documentary",
]

MARATHON_NARRATIVE_FORMAT = "tragic_expeditions"

MARATHON_TITLE_FORMULAS = [
    "{topic}: La Expedición Que Nadie Debió Intentar",
    "Expediciones Mortales: {topic} | Documental Completo",
    "{topic} — La Verdadera Historia De Supervivencia",
    "Lo Que REALMENTE Pasó En {topic}",
    "Tragedias Reales: {topic} | Documental HD",
]

MARATHON_HOOK_TYPES = [
    "amenaza_inminente",
    "revelacion_impactante",
    "misterio_sin_resolver",
]

VIRAL_PLAYLIST_KEYWORDS = {
    "tragedias-polares": ["arctic expedition disaster documentary", "antarctic survival stories", "polar exploration tragedies", "franklin expedition documentary"],
    "naufragios-historicos": ["shipwreck survival stories", "famous maritime disasters", "lost ships found documentary", "ocean survival true stories"],
    "montanas-mortales": ["mount everest disaster documentary", "deadliest mountain expeditions", "k2 climbing tragedy stories", "high altitude survival stories"],
}
