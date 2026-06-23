"""Thumbnail psychology — CTR maximization principles for YouTube thumbnails.

Based on research from digital marketing studies, YouTube analytics data,
and behavioral psychology patterns that drive click-through behavior.

Refs:
- Curiosity Gap Theory (Loewenstein, 1994)
- Von Restorff Effect (isolation effect in visual attention)
- Emotional Arousal & CTR correlation (Berger & Milkman, 2012)
- Color psychology in marketing (Labrecque & Milne, 2012)
"""

# ── CTR Psychology Rules ────────────────────────────────────────

PSYCHOLOGY_RULES = """
═══ REGLAS DE MINIATURA VIRAL ═══

🔴 COLORES QUE DISPARAN EL CLICK:
- Usa fondo oscuro + elemento brillante en contraste (rojo neón, amarillo dorado)
- Los thumbnails con alto contraste tienen 23% más CTR
- Evita fondos blancos o grises — se camuflan con el fondo de YouTube
- El color rojo en elementos clave aumenta CTR entre 8-12%
- Paleta recomendada: fondo negro/casi-negro, acento carmesí/rojo, texto blanco

😱 ROSTROS HUMANOS:
- Los thumbnails con rostros tienen 38% más CTR
- Expresiones extremas: shock, miedo, asombro, confusión
- El contacto visual directo con la cámara es crítico
- Si no hay rostro real, usa siluetas humanas en sombra (funciona en nicho terror/misterio)

📐 COMPOSICIÓN:
- Regla de tercios estricta
- El punto focal debe ocupar al menos 40% del frame
- Usa patrones visuales de "puzle": mostrar algo incompleto o fragmentado genera curiosidad
- Flechas o círculos rojos sutiles dirigen la mirada pero sin parecer clickbait barato
- Deja espacio negativo para texto (generalmente tercio inferior o superior)

✍️ TEXTO EN MINIATURA:
- MÁXIMO 3-4 palabras (nunca más de 15 caracteres)
- Tamaño de fuente: 25-35% del alto de la imagen
- Contraste máximo: texto blanco con sombra negra gruesa (offset 4-6px)
- Tipografía bold sans-serif, SIN SERIFAS
- El texto NUNCA debe repetir las primeras 3 palabras del título
- Posición: tercio inferior (no tapa el elemento visual principal)

🔮 CURIOSITY GAP:
- Muestra algo INCOMPLETO: una puerta entreabierta, un documento a medias, una silueta
- El cerebro humano NO soporta la información incompleta (efecto Zeigarnik)
- Sugiere pero NO reveles: "el experimento que..." → la miniatura insinúa sin mostrar
- Usa elipsis visual: objetos cortados por el borde del frame

⚡ URGENCIA VISUAL:
- Elementos que sugieran peligro, advertencia o secreto
- Estética de "documento clasificado": sellos, texto tachado, marca de agua
- Efecto "prohibido": barra censuradora parcial, pixelado sutil, código de barras

🧩 NICHO: PSICOLOGÍA OSCURA / MISTERIO:
- Laboratorios oscuros, pasillos institucionales, archivos antiguos
- Siluetas humanas, sombras alargadas, espejos
- Texturas: papel envejecido, metal frío, cristal roto
- Símbolos: jaulas, cronómetros, electrodos, documentos clasificados
- Paleta: negro, carmesí, dorado envejecido, gris institucional
"""

# ── Thumbnail Layout Templates ─────────────────────────────────

LAYOUT_TEMPLATES = [
    {
        "name": "split_face",
        "description": "Mitad rostro/expresión + mitad texto oscuro. Alto impacto emocional.",
        "zones": {
            "visual": {"x": 0, "y": 0, "w": 0.6, "h": 1.0},
            "text": {"x": 0.6, "y": 0.55, "w": 0.4, "h": 0.45},
        }
    },
    {
        "name": "classified_document",
        "description": "Estética de archivo secreto/desclasificado. Texto tachado, sellos.",
        "zones": {
            "visual": {"x": 0, "y": 0, "w": 1.0, "h": 0.65},
            "text": {"x": 0.05, "y": 0.68, "w": 0.9, "h": 0.3},
        }
    },
    {
        "name": "dark_reveal",
        "description": "Fondo casi negro con una revelación luminosa central. Misterio.",
        "zones": {
            "visual": {"x": 0.15, "y": 0.05, "w": 0.7, "h": 0.6},
            "text": {"x": 0.05, "y": 0.68, "w": 0.9, "h": 0.28},
        }
    },
    {
        "name": "shock_closeup",
        "description": "Primer plano extremo de objeto simbólico. Texto overlay inferior.",
        "zones": {
            "visual": {"x": 0, "y": 0, "w": 1.0, "h": 0.72},
            "text": {"x": 0.05, "y": 0.75, "w": 0.9, "h": 0.22},
        }
    },
    {
        "name": "incomplete_puzzle",
        "description": "Imagen fragmentada con vacíos. Genera curiosity gap extremo.",
        "zones": {
            "visual": {"x": 0.05, "y": 0.05, "w": 0.9, "h": 0.55},
            "text": {"x": 0.05, "y": 0.62, "w": 0.9, "h": 0.35},
        }
    },
]

# ── Color Palettes for Thumbnail Text/Overlays ─────────────────

THUMBNAIL_PALETTES = {
    "dark_mystery": {
        "background": (10, 10, 15),
        "text_primary": (240, 240, 245),
        "text_shadow": (5, 5, 10),
        "accent": (220, 40, 40),       # Crimson red
        "accent_glow": (255, 80, 60),   # Lighter red for glow
        "overlay_dark": (0, 0, 0),
        "overlay_gradient_start": (0, 0, 0, 0),
        "overlay_gradient_end": (0, 0, 0, 220),
        "border": (180, 40, 40),
    },
    "cold_institutional": {
        "background": (20, 25, 35),
        "text_primary": (230, 240, 250),
        "text_shadow": (5, 8, 15),
        "accent": (40, 140, 200),       # Cold blue
        "accent_glow": (60, 180, 240),
        "overlay_dark": (5, 10, 20),
        "overlay_gradient_start": (0, 0, 0, 0),
        "overlay_gradient_end": (5, 10, 20, 220),
        "border": (40, 120, 180),
    },
}

# ── Keyword-to-Query Mappings for Thumbnail Search ─────────────

THUMBNAIL_QUERY_TEMPLATES = [
    "dark cinematic photography dramatic lighting 16:9",
    "psychological horror atmosphere dark corridor",
    "abandoned laboratory institutional cold lighting",
    "classified document archive secret file",
    "human silhouette shadow mystery dark room",
    "vintage medical equipment dark atmosphere",
    "broken mirror shattered glass dark reflection",
    "empty prison cell cold institutional",
    "fear expression portrait dramatic lighting",
    "creepy hallway long shadow perspective",
]

# ── Thumbnail Concept Builder (LLM Prompt Fragment) ────────────

# ── Per-style psychology rules ──────────────────────────────────

STYLE_PSYCHOLOGY_RULES = {
    "dark_cinematic": {
        "color_triggers": "rojo carmesí + negro = peligro, urgencia, conocimiento prohibido",
        "composition_rule": "revelación parcial — siluetas, sombras largas, puertas entreabiertas",
        "emotional_hooks": ["curiosity_gap", "fear_of_missing_out", "forbidden_knowledge", "moral_outrage"],
        "text_strategy": "PALABRAS CORTAS Y GOLPEANTES: PROHIBIDO, OCULTO, SECRETO, REAL",
        "avoid": "rostros sonrientes, colores brillantes, fondos blancos, composiciones simétricas",
    },
    "vintage_archive": {
        "color_triggers": "sepia + dorado envejecido = autoridad, autenticidad, verdad oculta",
        "composition_rule": "documento central con sello — texto tachado, marcas de agua, bordes quemados",
        "emotional_hooks": ["exclusivity", "authority_questioning", "hidden_truth"],
        "text_strategy": "DESCLASIFICADO, CONFIDENCIAL, ARCHIVO SECRETO, PROHIBIDO PUBLICAR",
        "avoid": "elementos modernos, tipografía digital, colores neón, selfies",
    },
    "realistic_documentary": {
        "color_triggers": "azul profundo + dorado = confianza, conocimiento, seriedad",
        "composition_rule": "imagen realista con texto limpio — regla de tercios estricta",
        "emotional_hooks": ["curiosity_gap", "intellectual_arousal", "surprise"],
        "text_strategy": "frase intrigante pero creíble: DESCUBRIMIENTO, REVELADO, EXPLICADO",
        "avoid": "exageración visual, filtros extremos, clickbait obvio",
    },
    "institutional_cold": {
        "color_triggers": "gris clínico + rojo alerta = experimento, control, vigilancia",
        "composition_rule": "ambiente estéril con elemento perturbador — contraste frío/amenaza",
        "emotional_hooks": ["fear", "uncanny_valley", "loss_of_control", "surveillance_anxiety"],
        "text_strategy": "términos clínicos con carga emocional: EXPERIMENTO, SUJETO #, DOSIS LETAL",
        "avoid": "calidez, naturaleza, decoración hogareña, niños",
    },
    "dramatic_contrast": {
        "color_triggers": "rojo sangre + negro absoluto = shock, urgencia, peligro inminente",
        "composition_rule": "primer plano extremo o contraste violento — luz/sombra radical",
        "emotional_hooks": ["shock", "moral_outrage", "fear", "urgency"],
        "text_strategy": "UNA o DOS palabras máximo: IMPACTANTE, PROHIBIDO, CENSURADO",
        "avoid": "espacios vacíos, colores pastel, composiciones relajadas",
    },
    "moody_atmospheric": {
        "color_triggers": "tonos tierra + acento cálido = reflexión, profundidad, melancolía",
        "composition_rule": "espacio negativo amplio con punto focal sutil — invita a mirar",
        "emotional_hooks": ["introspection", "nostalgia", "wonder", "melancholy"],
        "text_strategy": "frase poética o filosófica: REFLEXIÓN, VERDAD, ALMA, MENTE",
        "avoid": "ruido visual, colores chillones, agresividad gráfica, prisas",
    },
    "minimalist_clean": {
        "color_triggers": "blanco/negro + acento cromático único = precisión, tecnología, futuro",
        "composition_rule": "elemento único centrado con amplio espacio negativo — sin distracciones",
        "emotional_hooks": ["intellectual_arousal", "curiosity_gap", "awe"],
        "text_strategy": "máximo 3 palabras, fuente sans-serif limpia: DESCUBRE, DATOS, CIENCIA",
        "avoid": "decoración excesiva, texturas rugosas, caos visual, vintage",
    },
}

# ── Thumbnail Concept Builder (LLM Prompt Fragment) ────────────

THUMBNAIL_CONCEPT_PROMPT = """Genera un CONCEPTO DE MINIATURA VIRAL para YouTube basado en el contenido del video.

REQUISITOS:
1. Describe UNA imagen principal que genere CURIOSIDAD EXTREMA
2. La imagen debe representar un MOMENTO o SÍMBOLO clave del video, no una escena cualquiera
3. Usa disparadores psicológicos: curiosity gap, miedo, shock, exclusividad
4. La imagen debe ser conceptual/simbólica — no literal
5. Incluye sugerencia de composición (dónde va el elemento principal, dónde el texto)
6. Describe el ESTADO EMOCIONAL que debe transmitir la imagen

Responde SOLO con JSON:
{
  "concept": "Descripción detallada de la imagen conceptual para la miniatura (1-2 frases)",
  "visual_element": "El elemento visual PRINCIPAL que ocupará el 60% del frame",
  "emotion": "La emoción dominante que debe transmitir (shock, miedo, curiosidad, asombro)",
  "layout": "split_face | classified_document | dark_reveal | shock_closeup | incomplete_puzzle",
  "search_query": "5-8 palabras en inglés para buscar esta imagen en Unsplash"
}"""
