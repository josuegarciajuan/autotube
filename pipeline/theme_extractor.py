"""Theme Extractor — analyzes content to extract visual context for the video."""

import json
import logging
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)


class NicheGuardrailError(Exception):
    """Raised when extracted theme keywords have zero overlap with the channel niche.

    This is a fatal error — the pipeline should abort rather than proceed with
    off-niche content that would produce irrelevant media queries and potentially
    black-screen videos.
    """
    pass


@dataclass
class ThemeContext:
    """Visual context extracted from the video's core argument."""
    genre: str = ""                      # e.g. "edad_media", "espacio", "psicología_moderna"
    era: str = ""                        # e.g. "siglo_XIII", "1960s", "presente"
    visual_style: str = ""               # e.g. "oscuro_documental", "futurista", "vintage"
    key_motifs: list[str] = field(default_factory=list)   # e.g. ["castillos","antorchas"]
    forbidden_elements: list[str] = field(default_factory=list)  # e.g. ["tecnología moderna"]
    theme_keywords_en: list[str] = field(default_factory=list)   # e.g. ["medieval","castle"]
    color_palette: dict = field(default_factory=dict)  # e.g. {"primary":"#3a1a0a","accent":"#c49a3c"}

    # ── NEW FIELDS (v2) ────────────────────────────────────
    primary_subject: str = ""            # e.g. "human mind", "ancient ruins"
    mood: str = "misterioso"             # "misterioso", "esperanzador", "perturbador"
    lighting: str = "claroscuro"         # "claroscuro", "luz cenital", "luz dorada", "neón frío"
    composition: str = "primeros planos" # "primeros planos", "planos generales", "simetría"
    era_decade: str = ""                 # Normalized: "1980s" instead of "los años ochenta"

    # ── Helper methods ─────────────────────────────────────

    def to_search_context(self) -> str:
        """Build a rich search context string for media queries."""
        parts = []
        if self.primary_subject:
            parts.append(self.primary_subject)
        if self.genre:
            parts.append(self.genre)
        if self.mood:
            parts.append(f"{self.mood} mood")
        if self.lighting:
            parts.append(f"{self.lighting} lighting")
        if self.visual_style:
            parts.append(self.visual_style)
        if self.era_decade:
            parts.append(self.era_decade)
        return ", ".join(parts)

    def to_pollo_prompt(self, base_description: str) -> str:
        """Build a detailed Pollo AI prompt for scene image generation."""
        parts = [base_description]
        if self.visual_style:
            parts.append(self.visual_style)
        if self.lighting:
            parts.append(f"{self.lighting} lighting")
        if self.mood:
            parts.append(f"{self.mood} atmosphere")
        if self.era_decade:
            parts.append(self.era_decade)
        if self.composition:
            parts.append(self.composition)
        parts.extend([
            "cinematic photography", "16:9 aspect ratio",
            "professional quality", "8K, high resolution, sharp focus, "
            "intricate detail", "no text", "no watermark",
        ])
        return ", ".join(parts)


THEME_EXTRACTOR_SYSTEM = """Eres un director de arte cinematográfico. Analizas el argumento de un video documental y extraes el contexto visual que debe unificar todas las escenas.

Tu tarea: identificar el GÉNERO, ÉPOCA, ESTILO VISUAL y MOTIVOS CLAVE que deben aparecer en TODAS las imágenes del video. También identificas qué elementos NUNCA deben aparecer (anacronismos, elementos fuera de contexto).

Responde SIEMPRE con JSON."""


THEME_EXTRACTOR_PROMPT = """Analiza el siguiente contenido y extrae el contexto visual unificado para el video documental:

CONTENIDO:
{content_text}

TÍTULO CANAL: {channel_name}
TEMA CANAL: {channel_theme}

Determina:
1. genre: el género o ambientación principal (ej: "medieval", "espacial", "psicológico_moderno", "histórico_siglo_XX")
2. era: la época temporal concreta (ej: "siglo_XIII", "años_1960", "actualidad", "futuro_cercano")
3. visual_style: estilo visual predominante (ej: "oscuro_documental", "futurista_limpio", "vintage_granulado", "institucional_frío")
4. key_motifs: 3-6 elementos visuales icónicos de esta ambientación (ej: ["antorchas","pergaminos","castillos","armaduras"])
5. forbidden_elements: 2-4 elementos que NUNCA deben aparecer porque romperían la inmersión (ej: ["smartphones","edificios modernos","pantallas digitales"])
6. theme_keywords_en: 5-8 keywords en INGLÉS para búsqueda de imágenes/videos de stock (ej: ["medieval","castle","ancient","historical","torchlight","monastery"])
7. color_palette: paleta de colores sugerida con primary, secondary, accent en hex
8. primary_subject: el sujeto o tema visual principal del video (ej: "human mind", "ancient ruins", "cosmic void")
9. mood: el estado de ánimo predominante — uno de: "misterioso", "esperanzador", "perturbador", "melancólico", "épico", "sereno", "ominoso"
10. lighting: estilo de iluminación — uno de: "claroscuro", "luz cenital", "luz dorada", "neón frío", "luz natural difusa", "contraluz", "penumbra"
11. composition: tipo de encuadre preferido — uno de: "primeros planos", "planos generales", "simetría", "regla de tercios", "ángulo holandés", "plano detalle"
12. era_decade: década normalizada (ej: "1980s", "1940s", "medieval", "futuro cercano") — NUNCA uses frases como "los años ochenta", siempre en formato "1980s"

Responde SOLO con JSON:
{{"genre":"...","era":"...","visual_style":"...","key_motifs":[...],"forbidden_elements":[...],"theme_keywords_en":[...],"color_palette":{{"primary":"#...","secondary":"#...","accent":"#..."}},"primary_subject":"...","mood":"...","lighting":"...","composition":"...","era_decade":"..."}}"""


class ThemeExtractor:
    """Extracts visual theme context from video argument before script generation."""

    def __init__(self, config=None):
        self._config = config

    def extract(
        self,
        content_text: str,
        channel_name: str = "",
        channel_theme: str = "",
        niche_keywords: list[str] | None = None,
    ) -> ThemeContext:
        """Analyze content and return a ThemeContext for visual coherence.

        Args:
            content_text: Scraped content to extract visual theme from.
            channel_name: Display name of the channel (for prompt context).
            channel_theme: Channel tagline (for prompt context).
            niche_keywords: Optional list of channel-niche keywords (English).
                            Used as a guardrail — if the LLM returns theme keywords
                            with zero overlap against these, the theme is rejected
                            to prevent off-niche content from poisoning media queries.
        """
        from config.llm_client import create_llm_client
        from config.settings import LLM_MODEL_CREATIVE
        from config.llm_helpers import llm_json_call_or_fallback

        _DEFAULT_FALLBACK_DATA = {
            "genre": "documental",
            "era": "atemporal",
            "visual_style": "oscuro_documental",
            "key_motifs": [],
            "forbidden_elements": [],
            "theme_keywords_en": [],
            "primary_subject": "",
            "mood": "misterioso",
            "lighting": "claroscuro",
            "composition": "primeros planos",
            "era_decade": "",
        }

        client = create_llm_client(timeout=60.0, max_retries=2)

        # ── Build niche guardrail context for the prompt ──────────
        niche_banner = ""
        if niche_keywords:
            niche_banner = (
                f"\nNICHO DEL CANAL: {channel_name}\n"
                f"Keywords del nicho (EN INGLÉS): {', '.join(niche_keywords[:8])}\n"
                f"⚠️ CRÍTICO: Las theme_keywords_en DEBEN pertenecer al nicho del canal. "
                f"Si el contenido scrapeado NO coincide con el nicho (ej: contenido sobre "
                f"internet, tecnología moderna, psicología o una época no relacionada), "
                f"IGNORA el contenido scrapeado y genera keywords GENÉRICAS del nicho."
            )

        user_prompt = THEME_EXTRACTOR_PROMPT.format(
            content_text=content_text[:3000],
            channel_name=channel_name[:80],
            channel_theme=channel_theme[:200],
        )
        if niche_banner:
            user_prompt += niche_banner

        try:
            data = llm_json_call_or_fallback(
                client,
                fallback=_DEFAULT_FALLBACK_DATA,
                max_retries=3,
                retry_delay=2.0,
                model=LLM_MODEL_CREATIVE,
                messages=[
                    {"role": "system", "content": THEME_EXTRACTOR_SYSTEM},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=0.6,
                max_tokens=350,
                response_format={"type": "json_object"},
            )

            ctx = ThemeContext(
                genre=data.get("genre", ""),
                era=data.get("era", ""),
                visual_style=data.get("visual_style", ""),
                key_motifs=data.get("key_motifs", []),
                forbidden_elements=data.get("forbidden_elements", []),
                theme_keywords_en=data.get("theme_keywords_en", []),
                color_palette=data.get("color_palette", {}),
                # v2 fields
                primary_subject=data.get("primary_subject", ""),
                mood=data.get("mood", "misterioso"),
                lighting=data.get("lighting", "claroscuro"),
                composition=data.get("composition", "primeros planos"),
                era_decade=data.get("era_decade", ""),
            )

            # ── Niche guardrail validation ────────────────────
            if niche_keywords and ctx.theme_keywords_en:
                ok = _validate_theme_against_niche(ctx.theme_keywords_en, niche_keywords)
                if not ok:
                    logger.warning(
                        "Theme keywords %r have ZERO overlap with niche keywords "
                        "%r — rejecting theme (off-niche content)",
                        ctx.theme_keywords_en,
                        niche_keywords[:8],
                    )
                    raise NicheGuardrailError(
                        f"Theme keywords {ctx.theme_keywords_en} have zero overlap "
                        f"with niche keywords {niche_keywords[:8]} — pipeline aborted "
                        f"to prevent off-niche content generation"
                    )

            logger.info(
                "Theme extracted: genre=%s era=%s motifs=%s mood=%s lighting=%s",
                ctx.genre, ctx.era, ctx.key_motifs, ctx.mood, ctx.lighting,
            )
            return ctx

        except Exception as exc:
            logger.warning("Theme extraction failed: %s — using default context", exc)
            return ThemeContext(
                genre="documental",
                era="atemporal",
                visual_style="oscuro_documental",
                key_motifs=["archivos", "documentos", "sombras"],
                theme_keywords_en=["dark", "documentary", "atmosphere", "cinematic"],
                primary_subject="archivos",
                mood="misterioso",
                lighting="claroscuro",
                composition="primeros planos",
                era_decade="",
            )


def _validate_theme_against_niche(
    theme_keywords: list[str],
    niche_keywords: list[str],
    min_matches: int = 1,
) -> bool:
    """Check whether extracted theme keywords overlap with the channel's niche.

    For each extracted theme keyword, checks if any niche keyword phrase
    contains it (word-level match). This prevents off-niche scraped content
    from poisoning media queries with irrelevant keywords.

    Example:
        theme_keywords = ["1960s", "retro", "celebration", "internet", "digital"]
        niche_keywords = ["lost civilizations", "ancient ruins", "archaeology"]
        → returns False (zero overlap — theme is off-niche)

        theme_keywords = ["ancient", "temple", "ruins", "mysterious"]
        niche_keywords = ["lost civilizations", "ancient ruins", "archaeology"]
        → returns True (multiple matches)

    Args:
        theme_keywords: Keywords extracted by the LLM from scraped content.
        niche_keywords: Channel-niche keyword phrases (e.g. from NICHE_KEYWORDS_ENG).
        min_matches: Minimum number of matching keywords required (default 1).

    Returns:
        True if at least min_matches keywords overlap, False otherwise.
    """
    if not theme_keywords or not niche_keywords:
        return True  # Can't validate without data — let it through

    # Normalize: lowercase both sides
    tkw = set(kw.lower().strip() for kw in theme_keywords if kw.strip())
    nkw_lower = [kw.lower().strip() for kw in niche_keywords if kw.strip()]

    matches = 0
    for tw in tkw:
        # Check if this theme keyword appears as a word or substring
        # in any niche keyword phrase
        for nw in nkw_lower:
            if tw in nw or nw in tw:
                matches += 1
                break
            # Also check individual words within the niche phrase
            nw_words = set(nw.split())
            if tw in nw_words:
                matches += 1
                break

    if matches < min_matches:
        logger.warning(
            "Niche guardrail: theme keywords %r matched only %d niche keywords "
            "(needed %d) — rejecting theme",
            theme_keywords, matches, min_matches,
        )
        return False

    logger.debug(
        "Niche guardrail: theme keywords %r matched %d niche keywords — OK",
        theme_keywords, matches,
    )
    return True
