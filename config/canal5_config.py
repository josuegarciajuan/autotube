"""Configuration for Canal 5: Anomalias Medicas.

Only CHANNEL-SPECIFIC parameters. Everything else inherits from config.defaults.py.
"""
from config.settings import DEFAULT_VIDEO_PROVIDERS, DEFAULT_VIDEO_FALLBACK_QUERIES

CANAL_NAME = "canal5"
CANAL_DISPLAY_NAME = "Anomalias Medicas"
CANAL_TAGLINE = "Casos clinicos reales que la ciencia aun no puede explicar"
CANAL_OUTRO_TAGLINE = "La medicina avanza cada dia. Pero este caso... la ciencia todavia no tiene respuesta."
YOUTUBE_HANDLE = "@AnomaliasMedicas"
YOUTUBE_CHANNEL_URL = "https://www.youtube.com/@AnomaliasMedicas"
CANAL_NARRATIVE_STYLE = "documental medico de asombro"
CANAL_STYLE_DESCRIPTION = (
    "Casos medicos reales que desafian la ciencia. Enfermedades que "
    "ningun medico ha podido explicar, sindromes tan raros que solo "
    "afectan a una persona en el mundo, recuperaciones que contradicen "
    "todo pronostico. El formato sin rostro permite que las imagenes "
    "clinicas, los diagramas y los testimonios hablen solos."
)
CHANNEL_ABOUT_SECTION = """Bienvenido a Anomalias Medicas.

Documentales sobre los casos clinicos mas inexplicables de la historia: enfermedades que ningun medico ha podido diagnosticar, sindromes tan raros que solo afectan a una persona en el mundo, y recuperaciones que desafian todo pronostico medico. Historias reales de pacientes que vivieron lo imposible.

🎬 Formato: video ensayos documentales (8-14 minutos)
🗓️ Nuevos casos: cada semana
🎙️ Narracion documental con fuentes medicas verificadas
📩 Contacto: {email}
🧬 Si te fascinan las enfermedades raras, los misterios del cuerpo humano, los casos medicos inexplicables y las historias que desafian a la ciencia... este canal es para ti.
Suscribete y activa la campana para no perderte ningun caso que la medicina no puede explicar."""
CHANNEL_KEYWORDS = [
    "enfermedades raras", "casos medicos inexplicables", "anomalias medicas",
    "sindromes extraños", "documental medico", "casos clinicos reales",
    "enfermedades misteriosas", "fenomenos medicos", "ciencia medica",
    "casos medicos sorprendentes", "historias medicas reales", "misterios del cuerpo humano",
    "enfermedades raras documental", "sindromes inexplicables", "casos medicos impactantes",
    "documental salud español", "medicina y misterio", "anomalias del cuerpo humano",
    "trastornos raros", "historias clinicas reales",
]
CANAL_INITIALS = "AM"
LOGO_SIZE = 180

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

CANAL_TONE = (
    "Preciso, clinico y profundamente humano. Narrativa documental que "
    "oscila entre el asombro cientifico y la empatia medica. Riguroso "
    "en los hechos, luminoso en la atmosfera, intimo en la narracion. "
    "Como un documental de National Geographic sobre misterios medicos."
)
TARGET_AUDIENCE = (
    "18-45 años (amplio), LATAM (MX 30%, CO 20%, AR 15%, PE 10%, ES 15%, otros 10%). "
    "Curiosos, interesados en medicina, ciencia, cuerpo humano y misterios. "
    "55% mujeres / 45% hombres. 60%+ mobile. Sesiones de 8-12 min."
)
TARGET_AUDIENCE_PSYCHOGRAPHIC = {
    "The Medical Curious": ("Fascinacion por el cuerpo humano y sus misterios. Busca entender lo inexplicable desde la ciencia."),
    "The Empathetic": ("Conecta con las historias humanas detras de cada caso."),
    "The Science Skeptic": ("Entra con dudas, se queda por el rigor de los hechos."),
    "The Student / Professional": ("Estudiantes de medicina, enfermeria, psicologia. Consumen como complemento."),
}

TITLE_FORMULAS = [
    "El Sindrome de {name}: {shocking_fact}",
    "La Unica Persona en el Mundo con {condition}",
    "El Caso de {name}: {number} Medicos y Ningun Diagnostico",
    "Le Dieron {number} Dias de Vida. {years} Años Despues Sigue Vivo.",
    "{condition}: La Enfermedad que la Ciencia No Puede Explicar",
    "Desperto del Coma Hablando un Idioma que Nunca Aprendio",
    "{number} Personas con Habilidades que la Ciencia No Entiende",
]
TITLE_POWER_WORDS = [
    "revelado", "filtrado", "censurado", "inédito", "clasificado",
    "confidencial", "prohibido", "archivado", "desclasificado",
    "silenciado", "ocultado", "suprimido", "enterrado",
    "escalofriante", "desgarrador", "inexplicable", "demoledor",
    "sobrecogedor", "estremecedor", "alucinante", "aterrador",
    "asombroso", "desconcertante", "fascinante", "impactante",
    "desgarrador", "conmovedor", "angustiante", "perturbador",
    "alarmante", "devastador", "sobrehumano", "insólito",
    "oculto", "secreto", "siniestro", "enigmático",
    "increíble", "misterioso", "único", "raro", "extraño",
    "indescifrable", "desconocido", "insospechado", "enigma",
    "síndrome", "enfermedad", "diagnóstico", "caso", "paciente",
    "curación", "tratamiento", "pronóstico", "patología",
    "condición", "trastorno", "mutación", "anomalía",
    "malformación", "resistencia", "inmunidad", "remisión",
    "milagro médico", "caso único",
    "documentado", "real", "verificado", "confirmado", "demostrado",
    "científico", "médico", "clínico", "probado", "estudiado",
    "publicado", "registrado",
    "rara", "única", "primera", "última", "ningún",
    "solo una", "la más rara", "extrema", "severa",
    "sobrevivió", "venció", "superó", "desafió", "contradijo",
    "imposible", "increíble", "milagrosa", "inexplicable",
    "contra todo pronóstico", "resucitó",
    "cuerpo", "cerebro", "dolor", "sufrimiento", "agonía",
    "transformación", "mutación", "deformidad",
]
TITLE_MAX_CHARS = 65

SCRIPT_HOOK_RULE = (
    "ATENCION: La primera frase del guion DEBE ser el hecho mas "
    "impactante del caso, con un NUMERO y un HECHO CONCRETO. "
    "NUNCA empezar con contexto historico, definiciones, ni presentaciones. "
    "NUNCA 'Hola, bienvenidos a...' ni 'En este video vamos a hablar de...'.\n\n"
    "EJEMPLO CORRECTO: 'En 2009, una mujer de 28 años entro en el hospital "
    "con un dolor de cabeza. 72 horas despues, 14 medicos de 3 paises "
    "seguian sin saber que tenia.'\n"
    "EJEMPLO INCORRECTO: 'Las enfermedades raras afectan a millones de "
    "personas en todo el mundo y son un desafio para la medicina moderna...'."
)
SCRIPT_STRUCTURE = [
    {"step": "EL SINTOMA", "time_pct": "0-10%",
     "description": "El hecho mas impactante en frio. Sin contexto. Cerrar con promesa."},
    {"step": "EL HISTORIAL", "time_pct": "10-20%",
     "description": "Lo que se sabia antes de este caso. 'En los libros de medicina, esto no existia.'"},
    {"step": "EL PACIENTE", "time_pct": "20-30%",
     "description": "La persona real detras del caso clinico.",
     "retention_anchor": "CLIFFHANGER al 25%: 'Pero lo que este paciente no sabia... su caso apareceria en revistas medicas de todo el mundo.'"},
    {"step": "EL DIAGNOSTICO", "time_pct": "30-55%",
     "description": "Los sintomas, las pruebas, los medicos desconcertados.",
     "retention_anchor": "CLIFFHANGER al 50%: 'Recapitulemos: [1 frase]. Ahora viene lo mas increible.'"},
    {"step": "LA REVELACION", "time_pct": "55-70%",
     "description": "El momento clave. El hallazgo que cambio todo."},
    {"step": "LAS CONSECUENCIAS", "time_pct": "70-85%",
     "description": "Como cambio la vida del paciente. Que aprendio la medicina.",
     "retention_anchor": "EL ESPEJO al 70%: 'Tu cuerpo tambien esconde misterios que ni los medicos conocen.'"},
    {"step": "EL LEGADO", "time_pct": "85-100%",
     "description": "Que nos ensena este caso. Lo que la medicina aprendio."},
]
SCRIPT_END_HOOK = (
    "Y si este caso te parecio increible, espera a ver el de {next_case}. "
    "Porque lo que le ocurrio a {next_patient} es todavia mas "
    "inexplicable. Ese es el proximo video. Dale like, suscribete."
)
SCRIPT_EMOTIONAL_ARC = {
    "0-10%": "shock", "10-20%": "curiosidad cientifica", "20-30%": "empatia",
    "30-45%": "intriga clinica", "45-55%": "anticipacion", "55-65%": "asombro",
    "65-75%": "admiracion → comprension", "75-85%": "reflexion",
    "85-95%": "inspiracion", "95-100%": "fascinacion",
}
RETENTION_ANCHORS = {
    "at_25_pct": {"trigger": "cliffhanger_mid_video",
                  "action": "Insertar mini-cliffhanger: 'Pero lo que los analisis revelaron 3 dias despues cambio todo.'"},
    "at_50_pct": {"trigger": "the_reset",
                  "action": "Musica fuera 2s. 'Recapitulemos: [resumen]. Ahora viene lo que ningun medico esperaba.'"},
    "at_70_pct": {"trigger": "the_mirror",
                  "action": "Dirigirse al viewer: 'Tu cuerpo tambien esconde misterios que ni los medicos conocen.'"},
}
VIRALITY_TRIGGERS = [
    {"name": "Medical Awe", "mechanism": "'No vas a creer lo que el cuerpo humano puede hacer.' El asombro cientifico es altamente compartible."},
    {"name": "One in a Million", "mechanism": "La rareza extrema como gancho: 'Solo 12 personas en el mundo tienen esta enfermedad.'"},
    {"name": "Conversation Starter", "mechanism": "Cerrar con pregunta: '¿Conocias esta enfermedad?' Comentarios = engagement."},
    {"name": "Identity Signaling", "mechanism": "Compartir este contenido dice: 'Yo se cosas que la mayoria no sabe sobre el cuerpo humano.'"},
    {"name": "Hope Trigger", "mechanism": "Historias de pacientes que vencieron pronosticos imposibles. Altamente compartible."},
]

# Voice / TTS — edgetts
TTS_STRATEGY = {
    "voice_primary": "es-ES-AlvaroNeural", "voice_secondary": "es-MX-DaliaNeural",
    "rate_base": "-12%", "pitch_base": "+0Hz",
    "rate_hook": "-6%", "pitch_hook": "-2Hz",
    "rate_desarrollo": "-12%", "pitch_desarrollo": "+0Hz",
    "rate_climax": "-18%", "pitch_climax": "-6Hz",
    "rate_reflexion": "-14%", "pitch_reflexion": "-2Hz",
    "rate_cierre": "-8%", "pitch_cierre": "+2Hz",
}
VOICE_RATE = "-12%"
VOICE_PITCH = "+0Hz"
TTS_ENGINE = "edgetts"
KOKORO_VOICE = "em_santa"
KOKORO_BLOCK_SPEEDS = {"hook": 0.90, "desarrollo": 0.85, "climax": 0.78, "reflexion": 0.85, "cierre": 0.90}
KOKORO_PAUSE_BETWEEN_BLOCKS = 0.8
KOKORO_UNLOAD_EVERY_N_BLOCKS = 10

# Content Sources
REDDIT_SUBREDDITS = [
    "Radiology", "medizzy", "MedicalMysteries", "medlabprofessionals", "ems", "nursing",
    "Damnthatsinteresting", "interestingasfuck", "todayilearned",
    "UnresolvedMysteries", "TrueReddit", "HumanPorn", "HumansAreMetal",
    "science", "everythingScience",
]
WIKIPEDIA_CATEGORIES = [
    "Rare diseases", "Medical anomalies", "Unexplained medical conditions",
    "Genetic disorders", "Syndromes", "Medical mysteries", "Neurological disorders",
    "Congenital disorders", "Autoimmune diseases", "List of syndromes",
    "Medical curiosities", "People with rare diseases", "Undiagnosed diseases",
    "Spontaneous remission", "Medical miracles",
    "Enfermedades raras", "Sindromes", "Trastornos neurologicos",
    "Anomalias congenitas", "Casos medicos sin resolver",
]
SCRAPE_SOURCES = [
    {"plugin": "reddit", "priority": 1}, {"plugin": "wikipedia", "priority": 2},
    {"plugin": "atlas_obscura", "priority": 3}, {"plugin": "rss", "priority": 4},
    {"plugin": "google_news", "priority": 5},
]
ATLAS_OBSCURA_CATEGORIES = ["medical", "unique"]
RSS_FEEDS = []
GOOGLE_NEWS_QUERIES = [
    "enfermedad rara descubierta", "caso medico inexplicable",
    "sindrome extraño diagnosticado", "anomalia medica", "milagro medico",
]

# Visual Style
IMAGE_STYLE_MODIFIERS = (
    "medical documentary cinematography, clinical lighting, 16:9 aspect ratio, "
    "professional scientific photography, clean sterile aesthetic, "
    "cool teal and blue tones"
)
COLOR_PALETTE = {
    "primary": (0, 95, 115), "secondary": (8, 20, 38), "accent": (220, 130, 40),
    "text": (240, 245, 250), "text_shadow": (4, 6, 12), "tertiary": (30, 40, 50),
    "warning": (220, 130, 40),
}
FILM_GRAIN_OPACITY = 4
FILM_GRAIN_FRAMES = 6
KEN_BURNS_ZOOM_MIN = 4
KEN_BURNS_ZOOM_MAX = 10
VIDEO_RESOLUTION = (1280, 720)

MEDIA_STRATEGY = {
    "media_per_block": 1, "prefer_video": True,
    "max_video_blocks_pct": 80, "target_video_pct": 80, "max_placeholder_pct": 0,
    "video_fallback_to_image": True, "video_min_duration": 4, "video_max_duration": 20,
    "video_sources": ["pexels"], "video_providers": DEFAULT_VIDEO_PROVIDERS,
    "video_fallback_queries": DEFAULT_VIDEO_FALLBACK_QUERIES,
    "fallback_query": "medical hospital laboratory clinical scientific 16:9",
    "fallback_query_simple": "medical science hospital laboratory clinical",
    "ken_burns_zoom_min": 4, "ken_burns_zoom_max": 10,
    "crossfade_min": 0.3, "crossfade_max": 0.7,
    "ai_image_fallback": True, "ai_max_per_video": 5,
}

# Intro / Outro
INTRO_FONT_SIZE = 68
INTRO_BG_COLOR = (8, 20, 38)
OUTRO_FONT_SIZE = 52
OUTRO_BG_COLOR = (8, 20, 38)
OUTRO_TEXT = "Suscribete"
OUTRO_CTA_SUBSCRIBE = "❤️ Suscribete"
CTA_TEXT = ("Si has llegado hasta aqui y este caso te ha fascinado,\n"
            "suscribete y dale like\npara seguir revelando los misterios del cuerpo humano.")
CTA_TEXT_VARIANTS = [
    ("Si has llegado hasta aqui y este caso te ha fascinado,\n"
     "suscribete y dale like\npara seguir revelando los misterios del cuerpo humano."),
    ("Gracias por investigar este misterio medico con nosotros.\n"
     "Suscribete y dale like:\nel proximo caso es aun mas increible."),
    ("El cuerpo humano guarda secretos que desafian a la ciencia.\n"
     "Suscribete, dale like y comparte\npara que juntos sigamos desvelando estas anomalias."),
]
INTRO_VOICE_TEXT = "Bienvenidos a Anomalias Medicas, donde el cuerpo humano guarda secretos que la ciencia aun no comprende."
CTA_VOICE_TEXT = "Si has llegado hasta aqui y este caso te ha dejado sin palabras, suscribete y dale like. Sigamos investigando los misterios del cuerpo humano juntos."
OUTRO_VOICE_TEXT = "Gracias por acompanarnos. Hasta el proximo misterio medico."

# YouTube
YT_CATEGORY_ID = "27"
PUBLISH_MODE = "scheduled"
PUBLISH_WARMUP_MIN = 30
UPLOAD_WINDOWS = [{"start": 10, "end": 13}, {"start": 17, "end": 20}, {"start": 20, "end": 22}]
YT_DEFAULT_TAGS = [
    "enfermedades raras", "anomalias medicas", "casos medicos inexplicables",
    "documental medico", "misterios del cuerpo humano",
    "sindrome inexplicable", "enfermedad sin diagnostico", "caso clinico sorprendente",
    "anomalia genetica", "milagro medico real", "sindrome de capgras",
    "sindrome del acento extranjero", "insensibilidad congenita al dolor",
    "fibrodisplasia osificante progresiva", "sindrome de cotard",
    "enfermedades mas raras del mundo documental", "personas que no sienten dolor casos reales",
    "sindrome del cadaver ambulante explicacion", "gente que se convierte en piedra enfermedad",
    "que es el sindrome de capgras documental", "enfermedades que la medicina no puede curar",
    "trastornos psicologicos mas extraños del mundo", "mutaciones geneticas increibles en humanos",
    "casos clinicos que sorprendieron a los medicos",
    "enfermedades raras que solo tienen 50 personas",
    "pacientes con enfermedades unicas en el mundo",
    "priones enfermedad vacas locas en humanos", "sindrome del hombre arbol caso real",
    "niños que envejecen rapido progeria", "personas con superpoderes por enfermedades",
    "documental español medicina", "video ensayo medico",
    "historias medicas reales impactantes", "medicina y misterio documental", "ciencia medica explicada",
]

SEO_PRIMARY_KEYWORD = "enfermedades raras documental"
SEO_SECONDARY_KEYWORDS = [
    "casos medicos inexplicables", "anomalias medicas reales", "sindromes extraños documental",
    "enfermedades misteriosas", "fenomenos medicos inexplicables",
    "sindrome de capgras", "sindrome del acento extranjero", "insensibilidad al dolor congenita",
    "fibrodisplasia osificante progresiva", "sindrome de cotard", "progeria envejecimiento prematuro",
    "sindrome del hombre arbol", "enfermedad de las vacas locas humanos",
    "mutacion genetica alas de mariposa", "sindrome de parry romberg",
    "alergia al agua urticaria acuagenica",
    "enfermedades mas raras del mundo documental español",
    "personas que no pueden sentir dolor fisico", "sindrome del cadaver ambulante casos reales",
    "gente que se convierte literalmente en hueso", "que es realmente el sindrome de capgras",
    "enfermedades que los medicos no pueden explicar",
    "trastornos psicologicos mas raros del mundo", "mutaciones geneticas increibles documental",
    "casos medicos que desafiaron a la ciencia", "enfermedades raras que solo existen en un pais",
    "pacientes unicos en el mundo medicina", "priones enfermedad neurodegenerativa explicacion",
    "documental medico español", "video ensayo medicina",
    "historias medicas reales impactantes", "misterios del cuerpo humano documental",
    "datos curiosos medicina", "enfermedades mas raras del mundo",
    "historias clinicas sorprendentes", "lo que la medicina no explica",
    "descubrimientos medicos impactantes",
]
SEO_HASHTAGS = [
    "#AnomaliasMedicas", "#EnfermedadesRaras", "#Medicina", "#CasosReales",
    "#Documental", "#Ciencia", "#Misterio", "#Salud", "#Curiosidades",
    "#CuerpoHumano", "#SabiasQue", "#HistoriasReales", "#Sindromes",
    "#MedicinaMisteriosa", "#CienciaMedica",
]

SHORTS_PER_DAY = 2
SHORTS_HASHTAGS = [
    "#AnomaliasMedicas", "#EnfermedadesRaras", "#Shorts", "#SabiasQue",
    "#AprendeEnYouTube", "#Medicina", "#CuerpoHumano", "#Curiosidades",
    "#Documental", "#Ciencia",
]

DESCRIPTION_TEMPLATE = """🧬 {titulo}
———

{descripcion_seo}

🔬 EN ESTE VIDEO
- La historia real detras de este caso medico inexplicable
- Quien era el paciente y como empezo todo
- Por que los medicos no podian encontrar un diagnostico
- Lo que la ciencia aprendio de este caso

⏱️ CAPITULOS
{chapters}

———

🎙️ Bienvenido a **Anomalias Medicas** — el canal donde documentamos los casos clinicos mas inexplicables de la historia.

📚 Fuentes: Wikipedia, articulos cientificos, revistas medicas, hilos de Reddit (r/Radiology, r/medizzy, r/MedicalMysteries) y archivos clinicos verificados.
⚠️ Todo el contenido tiene fines educativos y de divulgacion cientifica.

🔔 Suscribete y activa la campana para descubrir mas misterios del cuerpo humano que la ciencia aun no puede explicar.

💬 ¿Conocias este caso? Dejalo en los comentarios.

#AnomaliasMedicas #EnfermedadesRaras #CasosReales"""

# Thumbnail
THUMBNAIL_BORDER_WIDTH = 5
THUMBNAIL_FONT_FAMILY = "DejaVuSans-Bold"
THUMBNAIL_BORDER_COLOR = "#FF2D55"
THUMBNAIL_SHOW_4K_BADGE = True
THUMBNAIL_TEXT_STROKE_COLOR = "#000000"
THUMBNAIL_VISUAL_STYLE = "clinical_mystery"
THUMBNAIL_MEDICAL_ECG = True
THUMBNAIL_MEDICAL_CROSS = False
THUMBNAIL_MEDICAL_DIAGNOSIS = False
THUMBNAIL_ALLOW_FACES = True
THUMBNAIL_CONCEPT_DIRECTIVE = (
    "Canal medico-cientifico. El rostro humano PUEDE aparecer con expresion de "
    "preocupacion/asombro PROFESIONAL. Priorizar imagenes clinicas y cientificas "
    "de alto impacto: radiografias, resonancias magneticas, helices de ADN, "
    "estructuras celulares al microscopio, monitores cardiacos ECG. SIN sangre visible."
)
THUMBNAIL_MANUAL_STYLE = {
    "visual_style": "clinical_mystery",
    "color_palette": {"primary": "#E63946", "accent": "#00B4D8", "text": "#FFFFFF", "shadow": "#0A0A0F"},
    "base_composition": "dark_reveal",
    "effects": {"contrast_boost": 1.15, "saturation": 0.95, "vignette": 0.35},
    "text_style": {"uppercase": True, "max_words": 4},
    "pollo_prompt_suffix": (
        "professional medical photography, human anatomy close-ups, DNA helix visualization, "
        "cellular structures under microscope, X-ray aesthetic, clinical lighting with soft shadows, "
        "heart monitor ECG waveforms, scientific documentary style, 16:9, photorealistic, "
        "no text overlay, no gore, no explicit blood or open wounds, medical journal quality"
    ),
}
THUMBNAIL_STYLE = {
    "layout": "image_full_background_text_overlay", "max_text_words": 4,
    "text_color": "clinical_white_on_teal_dark", "font_style": "bold_sans_serif_clean",
    "image_treatment": "clinical_cool_contrast_documentary",
    "background": "#081426", "accent_color": "#DC8228",
    "face_policy": "Real faces YES — patient portraits with dignified expressions. AI-generated faces: NO.",
    "number_preference": "odd_numbers_for_lists",
    "medical_viral_rule": "Red/cyan medical alert accents on key elements boost CTR dramatically.",
}
THUMBNAIL_TEMPLATES = {
    "the_scan": {"description": "Radiografia o resonancia magnetica", "text_position": "bottom_third",
                 "text_words": "2-3", "accent": "amber_highlight_on_anomaly",
                 "best_for": "Casos con evidencia radiologica"},
    "the_patient": {"description": "Retrato o silueta del paciente, iluminacion clinica", "text_position": "bottom_over_dark_gradient",
                    "text_words": "3-4", "accent": "amber_light_on_face",
                    "best_for": "Historias de supervivientes"},
    "the_lab": {"description": "Microscopio, tubos de ensayo, laboratorio", "text_position": "center_or_bottom",
                "text_words": "2-3", "accent": "amber_accent_on_equipment",
                "best_for": "Casos sobre investigacion medica"},
}

VIDEO_MIDROLL_STRATEGY = (
    "Colocar mid-rolls en pausas naturales entre capitulos narrativos. "
    "NUNCA en medio de una frase ni durante la revelacion del diagnostico."
)
MONETIZATION_TARGET_CPM = "$8–$15 USD"
MONETIZATION_VERTICALS = [
    "Salud y bienestar", "Farmaceutica y medicina", "Seguros de salud",
    "Educacion online", "Libros / Audiolibros", "Tecnologia medica",
]

END_SCREEN_STRATEGY = {
    "left_card": {"type": "playlist", "content": "most_relevant_playlist",
                  "purpose": "Keep viewer in a medical mystery session"},
    "center": {"type": "subscribe", "purpose": "Convert viewer to subscriber"},
    "right_card": {"type": "video", "content": "most_recent_upload",
                   "purpose": "Push newest content to engaged viewers"},
    "spoken_cta": ("Si este caso te parecio increible, el siguiente en la lista "
                   "es todavia mas inexplicable. Suscribete si quieres descubrir mas misterios del cuerpo humano."),
}

PLAYLISTS = [
    {"slug": "casos-completos", "name": "Casos Completos",
     "description": "Documentales en profundidad sobre los casos medicos mas inexplicables.", "type": "main"},
    {"slug": "lo-mas-increible", "name": "Lo Mas Increible",
     "description": "Los 5 casos medicos mas asombrosos del canal.", "type": "onboarding"},
    {"slug": "enfermedades-raras", "name": "Enfermedades Raras",
     "description": "Patologias que afectan a 1 de cada millon de personas.", "type": "thematic"},
    {"slug": "sindromes-inexplicables", "name": "Sindromes Inexplicables",
     "description": "Sindromes que la ciencia no ha podido explicar.", "type": "thematic"},
    {"slug": "milagros-medicos", "name": "Milagros Medicos",
     "description": "Recuperaciones que desafiaron todo pronostico.", "type": "thematic"},
]

FIRST_48H_STRATEGY = {
    "pre_upload_24h": ["Community Tab poll: '¿Conocias esta enfermedad?'", "YouTube Story: imagen clinica + 'Mañana. 9PM MX.'"],
    "hour_0": ["Publish at 9PM Mexico City time", "First comment: pregunta sobre el caso"],
    "hours_1_6": ["Reddit r/medizzy: TEXT post", "Facebook groups: Medicina, Curiosidades Cientificas, Salud"],
    "hours_6_24": ["Reply to EVERY comment", "Twitter/X thread: 5-7 tweets + YouTube link"],
    "hours_24_48": ["Analyze CTR and retention", "If CTR < 5%: swap thumbnail variant"],
}
COMMUNITY_TAB_PLAN = {
    "frequency": "3x/week",
    "schedule": {
        "monday": {"type": "poll", "example": "¿Cual de estas enfermedades raras te parece mas increible?",
                   "options": ["Insensibilidad al dolor", "Fibrodisplasia osificante", "Sindrome del acento extranjero", "Alergia al agua"]},
        "wednesday": {"type": "image_fact", "example": "Radiografia o imagen medica + dato sobre enfermedad rara"},
        "friday": {"type": "teaser", "example": "Este sabado: el caso medico que dejo sin palabras a 14 especialistas."},
    },
}
CROSS_PLATFORM = {
    "tiktok": {"format": "60-90s vertical", "hook_template": "Esta enfermedad solo la tienen {number} personas en el mundo.",
               "cadence": "2x/day (days 1, 3, 5)"},
    "youtube_shorts": {"format": "15-30s most shocking medical moment", "end_cta": "'Video completo en el canal'"},
    "twitter_x": {"format": "Thread — 1 case = 1 thread/week",
                  "template": "'HOY en Anomalias Medicas: el caso de...'"},
    "spotify_podcast": {"format": "Audio-only export", "title_format": "Anomalias Medicas | {case_title}"},
}
COLLABORATION_TARGETS = {
    "tier_1_direct": [{"name": "La Hiperactina", "niche": "Divulgacion medica"}, {"name": "Dr. Borja Bandera", "niche": "Medico divulgador"}, {"name": "Medicina Clara", "niche": "Divulgacion medica"}],
    "tier_2_adjacent": [{"name": "QuantumFracture", "niche": "Ciencia"}, {"name": "CdeCiencia", "niche": "Divulgacion cientifica"}],
    "collab_formats": ["React: 'Un medico reacciona a Anomalias Medicas'", "Topic trade"],
}
TRENDING_TOPIC_HOOKS = {
    "type_a_news": {"trigger": "Noticia de enfermedad rara o diagnostico sorprendente",
                    "pivot": "Esto que acaba de pasar en {country}... la medicina tiene docenas de casos igual de inexplicables."},
    "type_b_anniversary": {"trigger": "Aniversario de descubrimientos medicos",
                           "calendar": {"february": "Dia Mundial Enfermedades Raras (28 feb)", "april": "Dia Mundial de la Salud", "october": "Mes sindromes geneticos"}},
    "type_c_pop_culture": {"trigger": "Estreno de pelicula/serie sobre medicina", "strategy": "'La historia REAL detras de {show/movie}'"},
    "type_d_calendar": {"name": "Calendario de Misterios Medicos",
                        "months": {"january": "Casos medicos inicio de año", "february": "Enfermedades raras", "april": "Salud y bienestar", "october": "Sindromes geneticos", "december": "Milagros medicos navideños"}},
}
CONTENT_PILLARS = [
    {"name": "El Caso", "ratio": 55, "desc": "Documental profundo de un caso medico inexplicable"},
    {"name": "Recopilaciones", "ratio": 25, "desc": "Compilacion tematica: '5 enfermedades mas raras del mundo'"},
    {"name": "El Analisis", "ratio": 20, "desc": "Video mas corto analizando el caso desde la perspectiva cientifica"},
]

NICHE_KEYWORDS_ENG = [
    "medical anomalies", "rare medical cases", "mysterious diseases",
    "unexplained medical conditions", "medical mysteries", "rare diseases explained",
    "medical phenomena science can't explain", "bizarre medical conditions",
    "most shocking medical cases", "doctors couldn't explain this",
    "rarest diseases in the world", "medical cases that changed science",
    "patients who baffled doctors", "unexplained recoveries medical",
    "medical miracles true stories", "strangest syndromes",
    "rare genetic disorders", "weird medical conditions",
    "undiagnosed diseases documentary", "medical documentary rare cases",
]

MARATHON_NARRATIVE_FORMAT = "top_cases"

MARATHON_TITLE_FORMULAS = [
    "{topic}: Casos Que La Medicina No Puede Explicar",
    "Anomalías Médicas: {topic} | Documental",
    "{topic} — Los Pacientes Que Desafiaron La Ciencia",
    "Misterios Del Cuerpo Humano: {topic}",
]

MARATHON_HOOK_TYPES = [
    "asombro_cientifico",
    "misterio_sin_resolver",
    "conocimiento_exclusivo",
]

VIRAL_PLAYLIST_KEYWORDS = {
    "casos-completos": ["mysterious medical conditions documentary", "patients who baffled every doctor", "undiagnosed diseases full documentary", "medical mystery diagnosis explained"],
    "lo-mas-increible": ["most bizarre medical cases ever", "unbelievable medical anomalies", "top rarest diseases in the world", "shocking medical mysteries compilation"],
    "enfermedades-raras": ["rarest genetic disorders documentary", "one in a million medical conditions", "rare disease patient stories", "medical oddities explained"],
}
