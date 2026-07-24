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
    # ── v2: multi-text overlay ──
    text_gancho: str = ""          # L1: palabra-gancho (max 12 chars, UPPERCASE)
    text_complemento: str = ""     # L2: complemento (max 24 chars)
    badge_text: str = ""           # L3: sello de confianza
    # ── v2: secondary inset scene ──
    secondary_scene: str = ""      # Description of secondary image for inset recuadro
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

# ── Default face directive (standard YouTube CTR-maximization strategy) ──
DEFAULT_FACE_DIRECTIVE = (
    "Facial expression psychology: the human face is the single highest-CTR element on "
    "YouTube thumbnails. A shocked/surprised/astonished facial expression triggers instant "
    "emotional contagion (mirror neurons) and dramatically increases click-through rate — "
    "this is the 'MrBeast face' principle. Siempre que el contenido lo permita, la emoción "
    "DEBE materializarse en un ROSTRO HUMANO en primer plano con expresión facial intensa "
    "(sorpresa, shock, asombro, miedo, incredulidad). Los rostros generan más clicks que "
    "cualquier escena abstracta."
)

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
- Visual impact analysis: a thumbnail MUST be visually striking to stop the scroll. High contrast, vibrant focal point, dramatic composition. Flat, dull, or monotonous images are FAILURES.
- Pattern interrupt techniques: unexpected colors, unusual angles, surreal elements that break the visual pattern of the YouTube feed.
- The golden rule of thumbnail design: the image should make someone who has NO IDEA what the video is about NEED to click.

Para cada video, analizas:
1. La emoción DOMINANTE que debe transmitir la miniatura
2. El curiosity gap específico que hará imposible NO hacer click
3. Los símbolos visuales con mayor carga psicológica para este contenido
4. Lo que NUNCA debe mostrar la miniatura (revelaría demasiado)

{face_directive}

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
1. Identifica la emoción dominante que maximizará el CTR.
   {face_directive}
2. Define un curiosity gap específico (algo que la imagen insinúe pero no revele).
3. Sugiere 1-2 símbolos/conceptos visuales con alta carga psicológica para la imagen PRINCIPAL.
4. Indica qué NO debe mostrar la miniatura.
5. IMPORTANTE: Evalúa el NIVEL DE IMPACTO VISUAL necesario (1-10). Para contenido de psicología/documental oscuro, el impacto debe ser 8+. Sugiere TÉCNICAS ESPECÍFICAS: iluminación dramática, primer plano extremo, contraste máximo, colores vibrantes contra fondos oscuros, composición asimétrica.
6. Describe UNA ESCENA SECUNDARIA que complemente la imagen principal sin repetirla — será un recuadro pequeño en la composición final (por ejemplo: un documento, una fotografía antigua, un objeto simbólico, una silueta).

Responde SOLO con JSON:
{{"emotion_target": "...", "curiosity_gap": "...", "visual_concept": "...", "avoid_showing": "...", "secondary_scene": "...", "psychological_hooks": ["...", "..."], "confidence_score": 0.0}}"""


MARKETING_AGENT_SYSTEM = """Eres un estratega de marketing digital especializado en YouTube. 
Has analizado miles de miniaturas virales y sabes exactamente qué combinación 
de imagen + texto maximiza el Click-Through Rate (CTR).

Tu expertise:
- Texto overlay de DOS LÍNEAS (NO una sola):
  • LÍNEA 1 (gancho principal, texto GRANDE): 1-2 palabras en MAYÚSCULAS. Máx 12 chars.
    Formatos: "¿QUÉ PASÓ?", "NADIE LO VIO", "PROHIBIDO", "3 MINUTOS", "5 MÉDICOS"
  • LÍNEA 2 (complemento, texto MEDIANO): 2-4 palabras. Máx 24 chars. Complementa L1 y título.
    Formatos: "Nadie lo explicó", "Lo que ocultaron", "El informe secreto"
- BADGE/SELLO en esquina superior: texto de confianza (DOCUMENTAL, CASO REAL, REAL, ARCHIVO, EXPEDIENTE). Máx 15 chars.
- Psicología del color en thumbnails
- Formatos de composición probados (split_face, dark_reveal, classified_document, shock_closeup)
- Reglas de oro: NUNCA repetir el título, SIEMPRE generar curiosidad — el texto de imagen añade la pieza de intriga que el título no revela
- Text readability: text must be readable at 100px wide on mobile. Use thick outlines (3-4px minimum), high contrast, and blocky fonts.
- Pattern interrupt: the thumbnail must VISUALLY INTERRUPT the viewer's scanning pattern.

{face_directive}

Responde SIEMPRE con JSON."""

MARKETING_AGENT_PROMPT = """Basado en el análisis psicológico y el contenido del video, diseña 
la estrategia de texto y composición para la miniatura.

TÍTULO DEL VIDEO: {title}
ANÁLISIS PSICOLÓGICO: {psych_analysis}
ESTILO VISUAL: {visual_style}
PALETA DE COLORES: {color_palette}

REQUISITOS:
1. Crea DOS líneas de texto overlay:
   - LÍNEA 1 (text_gancho): 1-2 palabras en MAYÚSCULAS. Máx 12 caracteres. Palabra-gancho que DETIENE el scroll.
   - LÍNEA 2 (text_complemento): 2-4 palabras. Máx 24 caracteres. Complementa L1 y título sin repetirlos.
   - Regla: L1 + L2 + título deben contar una mini-historia de 3 actos.
   - Formatos para L1: "¿QUÉ PASÓ?", "NADIE LO VIO", "PROHIBIDO", "3 MINUTOS", "5 MÉDICOS", "FUE REAL"
   - Formatos para L2: "Nadie lo explicó", "Lo que ocultaron", "El informe secreto"
2. Define el BADGE/SELLO de confianza (badge_text): DOCUMENTAL, CASO REAL, REAL, ARCHIVO, EXPEDIENTE, o vacío.
3. Define la composición (dónde va el texto, dónde el foco visual). {face_directive}
4. Sugiere qué layout usar: shock_closeup, dark_reveal, split_face, incomplete_puzzle
5. Crea 3 variantes de texto completas para A/B testing futuro (campo text_variants legacy)

Responde SOLO con JSON:
{{"text_gancho": "...", "text_complemento": "...", "badge_text": "...", "best_text": "...", "text_variants": ["...", "...", "..."], "text_color_hex": "#...", "layout": "...", "composition_notes": "...", "ctr_estimate": "..."}}"""


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
        allow_faces: bool = True,
        concept_directive: str = "",
    ) -> ThumbnailBrief:
        """Run both agents and merge into a complete design brief.

        Args:
            script_text: First ~1500 chars of the video script.
            title: YouTube video title.
            keywords: SEO keywords (used for context only).
            style_profile: Channel style profile from ThumbnailStyleEngine.
            channel_name: Channel display name.
            channel_theme: One-line theme summary.
            allow_faces: If False, use concept_directive instead of the default
                surprised-face strategy. Channels without faces (e.g. medical)
                set this to False.
            concept_directive: Custom visual directive string. When non-empty,
                replaces the default face-based directive in all agent prompts.

        Returns:
            ThumbnailBrief ready for image generation and composition.
        """
        style = style_profile or {}
        visual_style = style.get("visual_style", "dark_cinematic")
        color_palette = style.get("color_palette", {})

        # Build the face/concept directive for this channel
        # concept_directive (when non-empty) always takes precedence over default,
        # regardless of allow_faces. allow_faces only controls fallback brief logic.
        if concept_directive:
            face_directive = concept_directive
        else:
            face_directive = DEFAULT_FACE_DIRECTIVE

        script_snippet = script_text[:1500] if script_text else ""

        try:
            # Run both agents sequentially (they share context)
            psych = self._run_psychology_agent(
                script_snippet=script_snippet,
                title=title,
                channel_name=channel_name,
                channel_theme=channel_theme,
                visual_style=visual_style,
                face_directive=face_directive,
            )

            marketing = self._run_marketing_agent(
                title=title,
                psych_analysis=json.dumps(psych, ensure_ascii=False),
                visual_style=visual_style,
                color_palette=color_palette,
                face_directive=face_directive,
            )

            brief = self._merge(psych, marketing, style)
            logger.info(
                "Thumbnail brief: emotion=%s text=%r layout=%s",
                brief.emotion_target, brief.text_overlay, brief.layout,
            )
            return brief

        except Exception as exc:
            logger.warning("Brainstorm failed: %s — using fallback brief", exc)
            return self._fallback_brief(title, style, allow_faces=allow_faces)

    # ── Agent runners ────────────────────────────────────────

    def _run_psychology_agent(
        self,
        script_snippet: str,
        title: str,
        channel_name: str,
        channel_theme: str,
        visual_style: str,
        face_directive: str = "",
    ) -> dict:
        """Call the psychology LLM agent."""
        from config.llm_client import create_llm_client
        from config.settings import LLM_MODEL

        client = create_llm_client(timeout=60.0, max_retries=2)

        # Build system prompt with face directive
        system_prompt = PSYCHOLOGY_AGENT_SYSTEM.format(
            face_directive=face_directive or DEFAULT_FACE_DIRECTIVE,
        )

        user_prompt = PSYCHOLOGY_AGENT_PROMPT.format(
            channel_name=channel_name[:80],
            channel_theme=channel_theme[:200],
            title=title[:120],
            script_snippet=script_snippet[:1200],
            visual_style=visual_style,
            face_directive=face_directive or DEFAULT_FACE_DIRECTIVE,
        )

        response = client.chat.completions.create(
            model=LLM_MODEL,
            messages=[
                {"role": "system", "content": system_prompt},
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
        face_directive: str = "",
    ) -> dict:
        """Call the marketing LLM agent."""
        from config.llm_client import create_llm_client
        from config.settings import LLM_MODEL

        client = create_llm_client(timeout=60.0, max_retries=2)

        # Build system prompt with face directive
        system_prompt = MARKETING_AGENT_SYSTEM.format(
            face_directive=face_directive or DEFAULT_FACE_DIRECTIVE,
        )

        user_prompt = MARKETING_AGENT_PROMPT.format(
            title=title[:120],
            psych_analysis=psych_analysis[:600],
            visual_style=visual_style,
            color_palette=json.dumps(color_palette, ensure_ascii=False),
            face_directive=face_directive or DEFAULT_FACE_DIRECTIVE,
        )

        response = client.chat.completions.create(
            model=LLM_MODEL,
            messages=[
                {"role": "system", "content": system_prompt},
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
        # Best text for compatibility: L1 | L2 or single line
        best_text = marketing.get("best_text", "")
        if not best_text:
            gancho = marketing.get("text_gancho", "")
            complemento = marketing.get("text_complemento", "")
            best_text = f"{gancho} | {complemento}" if gancho and complemento else "¿QUÉ PASÓ?"
        
        return ThumbnailBrief(
            image_concept=psych.get("visual_concept", ""),
            visual_focus=psych.get("visual_concept", "")[:80],
            emotion_target=psych.get("emotion_target", "curiosidad"),
            curiosity_gap=psych.get("curiosity_gap", ""),
            text_overlay=best_text,
            text_gancho=marketing.get("text_gancho", ""),
            text_complemento=marketing.get("text_complemento", ""),
            badge_text=marketing.get("badge_text", ""),
            secondary_scene=psych.get("secondary_scene", ""),
            text_color_hex=marketing.get("text_color_hex", style.get("color_palette", {}).get("text", "#F5F0E8")),
            composition_notes=marketing.get("composition_notes", "texto abajo, imagen arriba"),
            layout=marketing.get("layout", style.get("base_composition", "dark_reveal")),
            psych_hooks=psych.get("psychological_hooks", []),
            psych_score=float(psych.get("confidence_score", 0.0)),
            marketing_ctr_estimate=marketing.get("ctr_estimate", ""),
            marketing_text_variants=marketing.get("text_variants", []),
        )

    def _fallback_brief(self, title: str, style: dict, allow_faces: bool = True) -> ThumbnailBrief:
        """Return a minimal brief when the LLM agents are unavailable."""
        if not allow_faces:
            # Clinical/medical fallback — no surprised faces
            return ThumbnailBrief(
                image_concept=(
                    "clinical medical imagery, dramatic anatomical close-up, "
                    "X-ray or MRI scan with high contrast lighting, DNA helix, "
                    "laboratory environment, photorealistic, 8K, no faces"
                ),
                visual_focus="anatomical or scientific detail with dramatic clinical lighting",
                emotion_target="curiosidad clínica",
                curiosity_gap="¿Qué anomalía se oculta en este diagnóstico?",
                text_overlay="¿QUÉ PASÓ? | El diagnóstico oculto",
                text_gancho="¿QUÉ PASÓ?",
                text_complemento="El diagnóstico oculto",
                badge_text="DOCUMENTAL",
                secondary_scene="medical report with redacted text, classified stamp",
                text_color_hex="#FFFFFF",
                composition_notes="texto GRANDE abajo con outline grueso, imagen clínica dramática arriba ocupando 70%",
                layout=style.get("base_composition", "dark_reveal"),
                psych_hooks=["morbid_curiosity", "scientific_awe", "diagnostic_urgency"],
                psych_score=0.7,
                marketing_ctr_estimate="high",
                marketing_text_variants=["DIAGNÓSTICO", "ANOMALÍA", "RARO"],
            )
        return ThumbnailBrief(
            image_concept="dramatic atmospheric scene with intense lighting, high contrast, photorealistic, 8K",
            visual_focus="central dramatic element with strong contrast",
            emotion_target="shock",
            curiosity_gap="¿Qué secreto se oculta?",
            text_overlay="¿QUÉ PASÓ? | La verdad oculta",
            text_gancho="¿QUÉ PASÓ?",
            text_complemento="La verdad oculta",
            badge_text="DOCUMENTAL",
            secondary_scene="classified document with redacted text",
            text_color_hex="#FFFFFF",
            composition_notes="texto GRANDE abajo con outline grueso, imagen dramática arriba ocupando 70%",
            layout=style.get("base_composition", "shock_closeup"),
            psych_hooks=["curiosity_gap", "emotional_arousal", "pattern_interrupt"],
            psych_score=0.7,
            marketing_ctr_estimate="high",
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
