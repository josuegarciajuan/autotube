"""Estructuras compartidas de los 4 canales nuevos 2026.

Los canales se crean primero en local (active=0, egress_managed=0). El egress
se enganchará después al nodo encuestaCitas. Este módulo solo contiene datos
editoriales/estratégicos; los defaults universales viven en config/defaults.py.
"""

SCRIPT_HOOK_RULE = (
    "La primera frase debe ser un hecho concreto, una cifra o una pregunta que "
    "genere curiosidad inmediata. NUNCA empezar con 'Hola, bienvenidos' ni con "
    "contexto histórico. El hook debe prometer una revelación concreta y "
    "verificable, sin clickbait vacío."
)

SCRIPT_STRUCTURE = [
    {"id": "gancho", "step": "EL GANCHO", "time_pct": "0-10%",
     "description": "Hecho, cifra o pregunta que impacta. Promesa concreta."},
    {"id": "contexto", "step": "EL CONTEXTO", "time_pct": "10-25%",
     "description": "Qué se sabe, quién, cuándo y por qué importa."},
    {"id": "desarrollo", "step": "EL CASO", "time_pct": "25-50%",
     "description": "Desarrollo con detalles verificables y ejemplos."},
    {"id": "giro", "step": "EL GIRO", "time_pct": "50-65%",
     "description": "El dato que cambia la lectura. Mini-cliffhanger al 50%."},
    {"id": "revelacion", "step": "LA REVELACIÓN", "time_pct": "65-85%",
     "description": "La explicación o el desenlace. Evidencia y matices."},
    {"id": "cierre", "step": "EL CIERRE", "time_pct": "85-100%",
     "description": "Conclusión, pregunta abierta y CTA."},
]

SCRIPT_END_HOOK = (
    "Cerrar con una pregunta que invite a comentar y una promesa concreta para "
    "el siguiente vídeo. Nunca cerrar con un resumen plano."
)

SCRIPT_EMOTIONAL_ARC = {
    "0-10%": "curiosidad/shock",
    "10-25%": "contexto y empatía",
    "25-50%": "intriga",
    "50-65%": "anticipación",
    "65-85%": "asombro/comprensión",
    "85-100%": "reflexión y conversación",
}

RETENTION_ANCHORS = {
    "at_25_pct": {"trigger": "mini_cliffhanger",
                  "action": "Adelantar que hay un dato que casi nadie conoce."},
    "at_50_pct": {"trigger": "the_reset",
                  "action": "Recapitulemos en una frase y anunciar el giro."},
    "at_70_pct": {"trigger": "the_mirror",
                  "action": "Conectar el caso con la experiencia del espectador."},
}

VIRALITY_TRIGGERS = [
    {"name": "Curiosity Gap", "mechanism": "Prometer una respuesta concreta a una pregunta que el espectador no sabía que tenía."},
    {"name": "Shareable Fact", "mechanism": "Un dato que se pueda repetir en una conversación o en redes."},
    {"name": "Identity Signal", "mechanism": "Contenido que dice algo sobre quien lo comparte: 'yo sé esto'."},
    {"name": "Conversation Bait", "mechanism": "Pregunta final que invita a posicionarse o contar una experiencia."},
]

CTA_TEXT = (
    "Si te ha parecido interesante, suscríbete y activa la campana.\n"
    "Déjanos tu opinión en los comentarios: ¿ya lo sabías?"
)
CTA_TEXT_VARIANTS = [
    "Suscríbete para más historias como esta.",
    "¿Tú qué opinas? Te leemos en comentarios.",
    "Comparte este vídeo con alguien que necesite verlo.",
]
INTRO_VOICE_TEXT = "Bienvenidos al canal."
CTA_VOICE_TEXT = "Si te ha gustado, suscríbete y déjanos tu opinión en los comentarios."
OUTRO_VOICE_TEXT = "Gracias por acompañarnos. Hasta la próxima."

FIRST_48H_STRATEGY = {
    "pre_upload_24h": [
        "Community Tab: encuesta o teaser con la pregunta del vídeo.",
        "Story: imagen del tema + 'Mañana'.",
    ],
    "hour_0": [
        "Publicar en la franja programada.",
        "Primer comentario con una pregunta abierta.",
    ],
    "hours_1_6": [
        "Compartir en 2 comunidades relevantes (sin spam).",
        "Responder a todos los comentarios.",
    ],
    "hours_6_24": [
        "Revisar CTR y retención.",
        "Si CTR < 4%, probar variante de miniatura.",
    ],
    "hours_24_48": [
        "Fijar comentario con el siguiente vídeo.",
        "Analizar qué minuto pierde más retención.",
    ],
}

COMMUNITY_TAB_PLAN = {
    "frequency": "2-3x/semana",
    "schedule": {
        "monday": {"type": "poll", "example": "¿Qué tema quieres ver esta semana?"},
        "wednesday": {"type": "image_fact", "example": "Dato curioso + pregunta."},
        "friday": {"type": "teaser", "example": "Avance del siguiente vídeo."},
    },
}

CROSS_PLATFORM = {
    "youtube_shorts": {"format": "30-55s vertical", "end_cta": "Vídeo completo en el canal"},
    "tiktok": {"format": "30-60s vertical", "cadence": "1-2/día"},
    "twitter_x": {"format": "Hilo de 4-6 posts por vídeo"},
}

COLLABORATION_TARGETS = {
    "tier_1_direct": [],
    "tier_2_adjacent": [],
    "collab_formats": ["Reacción conjunta", "Intercambio de tema", "Directo conjunto"],
}

TRENDING_TOPIC_HOOKS = [
    "Convertir una noticia del día en una pregunta verificable.",
    "Aprovechar un aniversario o efeméride.",
    "Relacionar el tema con una película/serie reciente.",
]

# Canales nuevos en modo local: sin maratones ni ráfagas hasta el egress.
MARATHON_ENABLED = False
MARATHON_NARRATIVE_FORMAT = "top_cases"
MARATHON_TITLE_FORMAT = "{topic}: la historia completa"

TITLE_POWER_WORDS = [
    "increíble", "asombroso", "oculto", "prohibido", "desconocido",
    "misterioso", "real", "verificado", "inaudito", "insólito",
    "nadie", "primera vez", "última", "secreto", "revelado",
    "engañó", "cambió", "desapareció", "sin explicación", "lo que no te contaron",
]

VIRAL_PLAYLIST_KEYWORDS = [
    "historias reales", "misterio", "curiosidades", "documental",
    "datos impactantes", "casos inexplicables",
]
