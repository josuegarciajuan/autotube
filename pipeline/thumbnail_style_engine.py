"""Thumbnail Style Engine — auto-decides visual style per channel.

Analyses channel name, description, theme and keywords via LLM to determine
the optimal visual style for YouTube thumbnails.  The result is cached so it
runs once per channel and is reused for every video.
"""
import json
import logging
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)

# ── Style categories ────────────────────────────────────────────

STYLE_CATEGORIES = [
    "dark_cinematic",          # terror, misterio, psicología oscura
    "vintage_archive",         # historia, documentos clasificados
    "realistic_documentary",   # educativo, científico, divulgación
    "institutional_cold",      # médico, psiquiátrico, experimentos
    "dramatic_contrast",       # true crime, drama, impacto
    "moody_atmospheric",       # filosófico, reflexivo, arte
    "minimalist_clean",        # tecnología, ciencia, datos
    "vibrant_educational",     # educativo vibrante, Kurzgesagt-style
    "shock_documentary",       # documental de impacto, National Geographic-style
    "distress_signal",         # emergencia, rescate, expediciones perdidas
]

# ── Per-style defaults (fallback when LLM is unavailable) ───────

STYLE_DEFAULTS: dict[str, dict] = {
    "dark_cinematic": {
        "color_palette": {"primary": "#8B0000", "accent": "#DAA520", "text": "#F5F0E8", "shadow": "#0A0A0A"},
        "base_composition": "dark_reveal",
        "effects": {"contrast_boost": 1.3, "saturation": 0.85, "vignette": 0.45},
        "text_style": {"uppercase": True, "max_words": 4},
        "pollo_prompt_suffix": (
            "dark atmospheric cinematography, desaturated color palette, "
            "deep crimson and black tones, institutional cold lighting, "
            "film grain texture, documentary photography style, "
            "16:9 aspect ratio, high contrast, no text overlay"
        ),
    },
    "vintage_archive": {
        "color_palette": {"primary": "#8B7355", "accent": "#D4A853", "text": "#F5E6D0", "shadow": "#1A1410"},
        "base_composition": "classified_document",
        "effects": {"contrast_boost": 1.2, "saturation": 0.7, "vignette": 0.5},
        "text_style": {"uppercase": True, "max_words": 3},
        "pollo_prompt_suffix": (
            "vintage archive photography, sepia tones, aged paper texture, "
            "classified document aesthetic, old institutional building, "
            "16:9 aspect ratio, dim tungsten lighting, no modern elements"
        ),
    },
    "realistic_documentary": {
        "color_palette": {"primary": "#2C5F8A", "accent": "#E8A840", "text": "#F8F8F8", "shadow": "#0D1B2A"},
        "base_composition": "dark_reveal",
        "effects": {"contrast_boost": 1.15, "saturation": 0.9, "vignette": 0.3},
        "text_style": {"uppercase": False, "max_words": 5},
        "pollo_prompt_suffix": (
            "documentary photography style, natural lighting, realistic, "
            "educational scientific aesthetic, 16:9 aspect ratio, "
            "clean composition, professional quality"
        ),
    },
    "institutional_cold": {
        "color_palette": {"primary": "#3A5068", "accent": "#C0392B", "text": "#ECF0F1", "shadow": "#0D1B2A"},
        "base_composition": "classified_document",
        "effects": {"contrast_boost": 1.25, "saturation": 0.75, "vignette": 0.4},
        "text_style": {"uppercase": True, "max_words": 4},
        "pollo_prompt_suffix": (
            "institutional cold lighting, hospital laboratory aesthetic, "
            "sterile environment, clinical photography style, "
            "16:9 aspect ratio, fluorescent lighting, medical equipment"
        ),
    },
    "dramatic_contrast": {
        "color_palette": {"primary": "#C0392B", "accent": "#F39C12", "text": "#FFFFFF", "shadow": "#000000"},
        "base_composition": "shock_closeup",
        "effects": {"contrast_boost": 1.4, "saturation": 0.8, "vignette": 0.5},
        "text_style": {"uppercase": True, "max_words": 3},
        "pollo_prompt_suffix": (
            "dramatic high contrast photography, stark lighting, "
            "cinematic composition, intense atmosphere, "
            "16:9 aspect ratio, bold shadows, no text"
        ),
    },
    "moody_atmospheric": {
        "color_palette": {"primary": "#4A4E69", "accent": "#C9ADA7", "text": "#F2E9E4", "shadow": "#1A1A2E"},
        "base_composition": "dark_reveal",
        "effects": {"contrast_boost": 1.1, "saturation": 0.8, "vignette": 0.35},
        "text_style": {"uppercase": False, "max_words": 5},
        "pollo_prompt_suffix": (
            "moody atmospheric photography, soft lighting, contemplative, "
            "cinematic composition with negative space, 16:9 aspect ratio, "
            "subtle color grading, artistic photography"
        ),
    },
    "minimalist_clean": {
        "color_palette": {"primary": "#2C3E50", "accent": "#3498DB", "text": "#FFFFFF", "shadow": "#111111"},
        "base_composition": "dark_reveal",
        "effects": {"contrast_boost": 1.1, "saturation": 0.95, "vignette": 0.2},
        "text_style": {"uppercase": False, "max_words": 5},
        "pollo_prompt_suffix": (
            "minimalist clean photography, modern composition, "
            "professional studio lighting, 16:9 aspect ratio, "
            "sleek design aesthetic, high quality"
        ),
    },
    "vibrant_educational": {
        "visual_style": "vibrant_educational",
        "color_palette": {
            "primary": "#1a1a2e",
            "secondary": "#16213e",
            "accent": "#e94560",
            "text": "#FFFFFF",
            "background": "#0f3460",
        },
        "base_composition": "shock_closeup",
        "effects": {"contrast_boost": 1.35, "saturation": 1.15, "vignette": 0.25},
        "text_style": {"uppercase": True, "max_words": 4},
        "pollo_prompt_suffix": (
            "highly vibrant colors, educational documentary style, clean composition, "
            "bold graphics aesthetic, Kurzgesagt-inspired visual style, "
            "16:9 aspect ratio, dramatic lighting"
        ),
        "negative": "dull, muted, grayscale, boring, corporate",
    },
    "shock_documentary": {
        "visual_style": "shock_documentary",
        "color_palette": {
            "primary": "#0a0a0a",
            "secondary": "#1a0a0a",
            "accent": "#ff3333",
            "text": "#FFFFFF",
            "background": "#000000",
        },
        "base_composition": "shock_closeup",
        "effects": {"contrast_boost": 1.5, "saturation": 1.05, "vignette": 0.3},
        "text_style": {"uppercase": True, "max_words": 3},
        "pollo_prompt_suffix": (
            "extreme contrast, documentary photojournalism style, gritty texture, "
            "intense realism, dramatic shadows, National Geographic documentary aesthetic, "
            "16:9 aspect ratio"
        ),
        "negative": "soft, gentle, peaceful, calm, pastel, corporate",
    },
    "distress_signal": {
        "visual_style": "distress_signal",
        "color_palette": {
            "primary": "#0F2841",
            "secondary": "#060C18",
            "accent": "#FF5C00",
            "text": "#EBF0F5",
            "background": "#060C18",
        },
        "base_composition": "dark_reveal",
        "effects": {"contrast_boost": 1.25, "saturation": 0.70, "vignette": 0.40},
        "text_style": {"uppercase": True, "max_words": 4},
        "pollo_prompt_suffix": (
            "cold desaturated cinematography, arctic survival atmosphere, "
            "distress signal aesthetic, emergency orange accents, deep blue and snow white tones, "
            "dramatic documentary lighting, frozen wilderness landscape, 16:9 aspect ratio, "
            "high contrast, rescue beacon mood, no text overlay"
        ),
        "negative": "warm, cozy, tropical, sunny, cheerful, pastel, corporate, peaceful",
        "rescue_elements": {
            "mayday_banner": True,
            "coordinates": True,
            "sin_senal_stamp": True,
        },
    },
    "clinical_mystery": {
        "visual_style": "clinical_mystery",
        "color_palette": {
            "primary": "#E63946",
            "secondary": "#1A0A0F",
            "accent": "#00B4D8",
            "text": "#FFFFFF",
            "background": "#0A0A0F",
        },
        "base_composition": "dark_reveal",
        "effects": {"contrast_boost": 1.25, "saturation": 1.50, "vignette": 0.40},
        "text_style": {"uppercase": True, "max_words": 4},
        "pollo_prompt_suffix": (
            "dramatic medical imagery, human anatomy close-ups, DNA helix "
            "visualization, cellular structures under microscope, X-ray aesthetic, "
            "surgical lighting with dramatic shadows, heart monitor ECG waveforms, "
            "viral YouTube medical thumbnail aesthetic, high contrast, bold vivid "
            "colors, 16:9 aspect ratio, photorealistic, no text overlay, no gore, "
            "no explicit blood or open wounds, medical drama documentary style"
        ),
        "negative": "flat lighting, boring sterile, dull muted colors, generic, peaceful, calm, pastel",
        "medical_elements": {
            "ecg_waveform": True,
            "medical_cross": True,
            "diagnosis_badge": True,
        },
    },
}

# ── LLM prompt for style decision ────────────────────────────────

STYLE_DECISION_PROMPT = """Analiza este canal de YouTube y decide el ESTILO VISUAL óptimo para sus miniaturas.

DATOS DEL CANAL:
- Nombre: {channel_name}
- Descripción: {description}
- Temática: {theme}
- Palabras clave: {keywords}

CATEGORÍAS DE ESTILO DISPONIBLES:
1. dark_cinematic — Terror, misterio, psicología oscura. Oscuro, carmesí, sombras profundas.
2. vintage_archive — Historia, documentos antiguos, archivos. Sepia, dorado envejecido.
3. realistic_documentary — Educativo, científico, divulgación. Natural, limpio, profesional.
4. institutional_cold — Médico, psiquiátrico, experimentos. Frío, clínico, fluorescente.
5. dramatic_contrast — True crime, drama, impacto. Alto contraste, colores intensos.
6. moody_atmospheric — Filosófico, reflexivo, arte. Suave, contemplativo, artístico.
7. minimalist_clean — Tecnología, ciencia, datos. Limpio, moderno, elegante.

INSTRUCCIONES:
- Elige 1 categoría principal que mejor represente al canal.
- Define la paleta de colores (primary, accent, text, shadow en hex).
- Define las reglas de texto (uppercase si/no, max_words).
- Escribe un pollo_prompt_suffix que capture la esencia visual del canal.
- Sé coherente: todas las miniaturas del canal deben tener el MISMO estilo.

Responde SOLO con JSON:
{{"visual_style": "dark_cinematic", "reasoning": "...", "color_palette": {{...}}, "base_composition": "...", "text_style": {{...}}, "pollo_prompt_suffix": "..."}}"""


# ── In-memory cache ─────────────────────────────────────────────

_style_cache: dict[str, dict] = {}


def get_channel_style(
    channel_name: str = "",
    description: str = "",
    theme: str = "",
    keywords: list[str] | None = None,
    channel_slug: str = "",
    force_reload: bool = False,
) -> dict:
    """Return the thumbnail style profile for a channel.

    Uses cache keyed by channel_slug.  Calls the LLM on first access.
    Falls back to ``dark_cinematic`` defaults if the LLM is unavailable.

    Args:
        channel_name: Display name (e.g. "Psicología Oculta").
        description: Channel about section or description text.
        theme: One-line theme summary.
        keywords: List of SEO keywords.
        channel_slug: Unique slug for cache key (e.g. "canal2").
        force_reload: Bypass cache and re-run LLM analysis.

    Returns:
        Style profile dict with keys: visual_style, color_palette,
        base_composition, effects, text_style, pollo_prompt_suffix.
    """
    cache_key = channel_slug or channel_name
    if not force_reload and cache_key in _style_cache:
        logger.debug("Style profile cache hit: %s", cache_key)
        return _style_cache[cache_key]

    try:
        style = _decide_style_via_llm(
            channel_name=channel_name,
            description=description,
            theme=theme,
            keywords=keywords or [],
        )
    except Exception as exc:
        logger.warning(
            "LLM style decision failed for %s: %s — using dark_cinematic defaults",
            cache_key, exc,
        )
        style = dict(STYLE_DEFAULTS["dark_cinematic"])
        style["visual_style"] = "dark_cinematic"

    _style_cache[cache_key] = style
    logger.info(
        "Style profile for %s: %s (composition=%s)",
        cache_key, style.get("visual_style"), style.get("base_composition"),
    )
    return style


def _decide_style_via_llm(
    channel_name: str,
    description: str,
    theme: str,
    keywords: list[str],
) -> dict:
    """Call the LLM to decide the best visual style."""
    from config.llm_client import create_llm_client
    from config.settings import LLM_MODEL

    client = create_llm_client(enable_thinking=True, timeout=60.0, max_retries=2)

    prompt = STYLE_DECISION_PROMPT.format(
        channel_name=channel_name[:100],
        description=description[:500],
        theme=theme[:200],
        keywords=", ".join(keywords[:15]),
    )

    response = client.chat.completions.create(
        model=LLM_MODEL,
        messages=[
            {
                "role": "system",
                "content": (
                    "Eres un director de arte especializado en miniaturas virales "
                    "de YouTube. Analizas canales y decides su identidad visual. "
                    "Respondes exclusivamente con JSON válido."
                ),
            },
            {"role": "user", "content": prompt},
        ],
        temperature=0.7,
        max_tokens=600,
    )

    content = response.choices[0].message.content.strip()
    if content.startswith("```"):
        lines = content.split("\n")
        content = "\n".join(lines[1:-1])
        content = content.replace("```json", "").replace("```", "").strip()

    style = json.loads(content)

    # Merge with defaults for any missing keys
    visual_style = style.get("visual_style", "dark_cinematic")
    defaults = STYLE_DEFAULTS.get(visual_style, STYLE_DEFAULTS["dark_cinematic"])
    merged = dict(defaults)
    merged.update({k: v for k, v in style.items() if v})
    merged["visual_style"] = visual_style

    return merged


def build_pollo_prompt(image_concept: str, style_profile: dict) -> str:
    """Combine an image concept with the channel style profile into a Pollo AI prompt.

    Structure: [focal subject] + [composition (rule of thirds, lower-third empty)]
    + [lighting/lens] + [mood/atmosphere] + [style suffix] + [quality booster] + [negative prompt].

    Args:
        image_concept: Description of what the thumbnail should show
            (e.g. "abandoned hospital corridor with a half-open door at the end").
        style_profile: Dict from ``get_channel_style()``.

    Returns:
        A complete prompt string ready for Pollo AI.
    """
    suffix = style_profile.get("pollo_prompt_suffix", "")

    quality_suffix = (
        "high contrast, cinematic lighting, photorealistic, vivid colors, "
        "viral YouTube thumbnail, 8K, dramatic composition, professional"
    )

    negative_prompt = (
        "flat lighting, dull colors, boring, generic, low contrast, washed out, "
        "mundane, ordinary, amateur, blurry, pixelated, overexposed, underexposed, "
        "text, watermark, logo, signature"
    )

    return (
        f"{image_concept}. Rule of thirds composition, single dominant focal point, "
        f"negative space in lower third reserved for text, cinematic depth of field. "
        f"{suffix}. {quality_suffix}. --no {negative_prompt}"
    )


def invalidate_cache(channel_slug: str = "") -> None:
    """Clear the style cache for *channel_slug*, or all channels if empty."""
    if channel_slug:
        _style_cache.pop(channel_slug, None)
    else:
        _style_cache.clear()
