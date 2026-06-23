"""Thumbnail Brainstorming — psychology + marketing agents for CTR maximization.

Runs two LLM "agents" in parallel:
1. Psychology Agent — analyses emotional triggers and curiosity gaps.
2. Marketing Agent — designs the text overlay and composition strategy.

The merged output is a ``ThumbnailBrief`` that feeds the image generator
and final composition.
"""
import json
import logging
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class ThumbnailBrief:
    """Complete thumbnail design brief produced by the brainstorming agents."""

    image_concept: str = ""
    visual_focus: str = ""
    emotion_target: str = "curiosidad"
    curiosity_gap: str = ""
    text_overlay: str = ""
    text_color_hex: str = "#F5F0E8"
    composition_notes: str = "texto abajo, imagen arriba"
    layout: str = "dark_reveal"

    # Psychology analysis (for debugging / logging)
    psych_hooks: list[str] = field(default_factory=list)
    psych_score: float = 0.0

    # Marketing analysis
    marketing_ctr_estimate: str = ""
    marketing_text_variants: list[str] = field(default_factory=list)


# ── LLM Prompts ─────────────────────────────────────────────────

PSYCHOLOGY_AGENT_SYSTEM = """Eres un psicólogo especializado en comportamiento de consumo digital y 
marketing de atención. Tu especialidad es analizar contenido de video y determinar 
qué disparadores emocionales y psicológicos harán que una persona haga CLICK 
en una miniatura de YouTube.

Conoces en profundidad:
- Curiosity Gap Theory (Loewenstein, 1994)
- Von Restorff Effect (aislamiento visual)
- Emotional Arousal & sharing (Berger & Milkman, 2012)
- Zeigarnik Effect (información incompleta genera ansiedad)
- Fear appeals en marketing digital
- Color psychology (rojo = urgencia/peligro, azul = confianza, negro = misterio)

Para cada video, analizas:
1. La emoción DOMINANTE que debe transmitir la miniatura
2. El curiosity gap específico que hará imposible NO hacer click
3. Los símbolos visuales con mayor carga psicológica para este contenido
4. Lo que NUNCA debe mostrar la miniatura (revelaría demasiado)

Responde SIEMPRE con JSON."""

PSYCHOLOGY_AGENT_PROMPT = """Analiza el siguiente contenido de video y determina la estrategia 
psicológica óptima para su miniatura de YouTube.

CANAL: {channel_name}
TEMA: {channel_theme}
TÍTULO DEL VIDEO: {title}

CONTENIDO (guion resumido):
{script_snippet}

ESTILO VISUAL DEL CANAL: {visual_style}

INSTRUCCIONES:
1. Identifica la emoción dominante que maximizará el CTR
2. Define un curiosity gap específico (algo que la imagen insinúe pero no revele)
3. Sugiere 1-2 símbolos/conceptos visuales con alta carga psicológica
4. Indica qué NO debe mostrar la miniatura

Responde SOLO con JSON:
{{"emotion_target": "...", "curiosity_gap": "...", "visual_concept": "...", "avoid_showing": "...", "psychological_hooks": ["...", "..."], "confidence_score": 0.0}}"""


MARKETING_AGENT_SYSTEM = """Eres un estratega de marketing digital especializado en YouTube. 
Has analizado miles de miniaturas virales y sabes exactamente qué combinación 
de imagen + texto maximiza el Click-Through Rate (CTR).

Tu expertise:
- Texto overlay: 2-4 palabras, UPPERCASE, alto contraste, bajo de la imagen
- Psicología del color en thumbnails
- Formatos de composición probados (split_face, dark_reveal, classified_document, etc.)
- Reglas de oro: nunca repetir el título, siempre generar curiosidad
- Badges y sellos que aumentan CTR: "4K", "NUEVO", "EXCLUSIVO", "CLASIFICADO"

Responde SIEMPRE con JSON."""

MARKETING_AGENT_PROMPT = """Basado en el análisis psicológico y el contenido del video, diseña 
la estrategia de texto y composición para la miniatura.

TÍTULO DEL VIDEO: {title}
ANÁLISIS PSICOLÓGICO: {psych_analysis}
ESTILO VISUAL: {visual_style}
PALETA DE COLORES: {color_palette}

REQUISITOS:
1. Crea 3 variantes de texto overlay (2-4 palabras cada una, max 15 caracteres)
2. Elige la MEJOR (la que genere más curiosidad sin ser clickbait barato)
3. Define la composición (dónde va el texto, dónde el foco visual)
4. Sugiere qué layout usar: split_face, dark_reveal, classified_document, shock_closeup, incomplete_puzzle
5. El texto NO debe repetir las primeras 3 palabras del título

Responde SOLO con JSON:
{{"text_variants": ["...", "...", "..."], "best_text": "...", "text_color_hex": "#...", "layout": "...", "composition_notes": "...", "ctr_estimate": "..."}}"""


class ThumbnailBrainstorm:
    """Orchestrates psychology + marketing agents for thumbnail design."""

    def brainstorm(
        self,
        script_text: str,
        title: str,
        keywords: list[str] | None = None,
        style_profile: dict | None = None,
        channel_name: str = "",
        channel_theme: str = "",
    ) -> ThumbnailBrief:
        """Run both agents and merge into a complete design brief.

        Args:
            script_text: First ~1500 chars of the video script.
            title: YouTube video title.
            keywords: SEO keywords (used for context only).
            style_profile: Channel style profile from ThumbnailStyleEngine.
            channel_name: Channel display name.
            channel_theme: One-line theme summary.

        Returns:
            ThumbnailBrief ready for image generation and composition.
        """
        style = style_profile or {}
        visual_style = style.get("visual_style", "dark_cinematic")
        color_palette = style.get("color_palette", {})

        script_snippet = script_text[:1500] if script_text else ""

        try:
            # Run both agents sequentially (they share context)
            psych = self._run_psychology_agent(
                script_snippet=script_snippet,
                title=title,
                channel_name=channel_name,
                channel_theme=channel_theme,
                visual_style=visual_style,
            )

            marketing = self._run_marketing_agent(
                title=title,
                psych_analysis=json.dumps(psych, ensure_ascii=False),
                visual_style=visual_style,
                color_palette=color_palette,
            )

            brief = self._merge(psych, marketing, style)
            logger.info(
                "Thumbnail brief: emotion=%s text=%r layout=%s",
                brief.emotion_target, brief.text_overlay, brief.layout,
            )
            return brief

        except Exception as exc:
            logger.warning("Brainstorm failed: %s — using fallback brief", exc)
            return self._fallback_brief(title, style)

    # ── Agent runners ────────────────────────────────────────

    def _run_psychology_agent(
        self,
        script_snippet: str,
        title: str,
        channel_name: str,
        channel_theme: str,
        visual_style: str,
    ) -> dict:
        """Call the psychology LLM agent."""
        from openai import OpenAI
        from config.settings import LLM_API_KEY, LLM_BASE_URL, LLM_MODEL

        client = OpenAI(api_key=LLM_API_KEY, base_url=LLM_BASE_URL)

        user_prompt = PSYCHOLOGY_AGENT_PROMPT.format(
            channel_name=channel_name[:80],
            channel_theme=channel_theme[:200],
            title=title[:120],
            script_snippet=script_snippet[:1200],
            visual_style=visual_style,
        )

        response = client.chat.completions.create(
            model=LLM_MODEL,
            messages=[
                {"role": "system", "content": PSYCHOLOGY_AGENT_SYSTEM},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.8,
            max_tokens=400,
        )

        content = self._extract_json(response.choices[0].message.content)
        logger.debug("Psychology agent: %s", json.dumps(content, ensure_ascii=False)[:200])
        return content

    def _run_marketing_agent(
        self,
        title: str,
        psych_analysis: str,
        visual_style: str,
        color_palette: dict,
    ) -> dict:
        """Call the marketing LLM agent."""
        from openai import OpenAI
        from config.settings import LLM_API_KEY, LLM_BASE_URL, LLM_MODEL

        client = OpenAI(api_key=LLM_API_KEY, base_url=LLM_BASE_URL)

        user_prompt = MARKETING_AGENT_PROMPT.format(
            title=title[:120],
            psych_analysis=psych_analysis[:600],
            visual_style=visual_style,
            color_palette=json.dumps(color_palette, ensure_ascii=False),
        )

        response = client.chat.completions.create(
            model=LLM_MODEL,
            messages=[
                {"role": "system", "content": MARKETING_AGENT_SYSTEM},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.9,
            max_tokens=500,
        )

        content = self._extract_json(response.choices[0].message.content)
        logger.debug("Marketing agent: %s", json.dumps(content, ensure_ascii=False)[:200])
        return content

    # ── Merge ─────────────────────────────────────────────────

    def _merge(self, psych: dict, marketing: dict, style: dict) -> ThumbnailBrief:
        """Merge psychology + marketing agent outputs into a ThumbnailBrief."""
        return ThumbnailBrief(
            image_concept=psych.get("visual_concept", ""),
            visual_focus=psych.get("visual_concept", "")[:80],
            emotion_target=psych.get("emotion_target", "curiosidad"),
            curiosity_gap=psych.get("curiosity_gap", ""),
            text_overlay=marketing.get("best_text", marketing.get("text_variants", [""])[0] if marketing.get("text_variants") else ""),
            text_color_hex=marketing.get("text_color_hex", style.get("color_palette", {}).get("text", "#F5F0E8")),
            composition_notes=marketing.get("composition_notes", "texto abajo, imagen arriba"),
            layout=marketing.get("layout", style.get("base_composition", "dark_reveal")),
            psych_hooks=psych.get("psychological_hooks", []),
            psych_score=float(psych.get("confidence_score", 0.0)),
            marketing_ctr_estimate=marketing.get("ctr_estimate", ""),
            marketing_text_variants=marketing.get("text_variants", []),
        )

    def _fallback_brief(self, title: str, style: dict) -> ThumbnailBrief:
        """Return a minimal brief when the LLM agents are unavailable."""
        visual_style = style.get("visual_style", "dark_cinematic")
        color_palette = style.get("color_palette", {})
        text_color = color_palette.get("text", "#F5F0E8")

        return ThumbnailBrief(
            image_concept="dark atmospheric mystery scene with dramatic lighting",
            visual_focus="central dramatic element",
            emotion_target="curiosidad",
            curiosity_gap="¿Qué secreto se oculta?",
            text_overlay="IMPACTANTE",
            text_color_hex=text_color,
            composition_notes="texto grande abajo, imagen oscura arriba",
            layout=style.get("base_composition", "dark_reveal"),
            psych_hooks=["curiosity_gap", "emotional_arousal"],
            psych_score=0.5,
            marketing_ctr_estimate="medium",
            marketing_text_variants=["IMPACTANTE", "INCREÍBLE", "SECRETO"],
        )

    # ── Helpers ───────────────────────────────────────────────

    @staticmethod
    def _extract_json(text: str) -> dict:
        """Extract JSON object from LLM response (handles markdown fences)."""
        text = text.strip()
        if text.startswith("```"):
            lines = text.split("\n")
            text = "\n".join(lines[1:-1]) if len(lines) > 2 else text
            text = text.replace("```json", "").replace("```", "").strip()
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            # Try to find a JSON object in the text
            import re
            match = re.search(r'\{.*\}', text, re.DOTALL)
            if match:
                return json.loads(match.group())
            logger.warning("Could not parse JSON from: %s", text[:200])
            return {}
