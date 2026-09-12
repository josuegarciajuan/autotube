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

from config.llm_helpers import _derive_hook_from_title
from pipeline.thumbnail_art_director import (
    TOPIC_FIRST_DIRECTIVE,
    recent_context_prompt,
)

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

    # ── v3 (tema-primero): sujeto + plan de proveedores + énfasis ──
    subject_type: str = ""         # person | place | object_artifact | concept | medical | event
    primary_subject: str = ""      # sintagma nominal concreto del tema
    provider_main: str = ""        # "ai" | "stock"
    provider_face: str = ""        # "stock" | "none"
    emphasis_word: str = ""        # palabra del overlay que se resalta
    color_intent: str = ""         # intención emocional de color (no hex de canal)

    # Psychology analysis (for debugging / logging)
    psych_hooks: list[str] = field(default_factory=list)
    psych_score: float = 0.0

    # Marketing analysis
    marketing_ctr_estimate: str = ""
    marketing_text_variants: list[str] = field(default_factory=list)


# ── LLM Prompts ─────────────────────────────────────────────────

# ── Default directive: sujeto-tema primero (v3, reemplaza el "MrBeast face") ──
# Se mantiene el nombre por compatibilidad con llamadas existentes.
DEFAULT_FACE_DIRECTIVE = TOPIC_FIRST_DIRECTIVE

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
3. Define el SUJETO PRINCIPAL CONCRETO del vídeo (el tema del título), no un fondo genérico.
4. Clasifica el vídeo en UN tipo de sujeto: "person" (trata de una persona concreta),
   "place" (lugar/expedición/civilización), "object_artifact" (documento/objeto/reliquia),
   "medical" (caso clínico/enfermedad), "event" (batalla/naufragio/catástrofe) o "concept".
5. Indica qué NO debe mostrar la miniatura.
6. IMPORTANTE: Evalúa el NIVEL DE IMPACTO VISUAL necesario (1-10) y sugiere técnicas
   específicas (iluminación, encuadre, contraste, composición).
7. Describe UNA ESCENA SECUNDARIA que complemente la imagen principal sin repetirla —
   será un recuadro pequeño (documento, fotografía antigua, objeto simbólico, silueta).

{recent_context}

Responde SOLO con JSON:
{{"emotion_target": "...", "curiosity_gap": "...", "visual_concept": "...", "primary_subject": "...", "subject_type": "person|place|object_artifact|concept|medical|event", "provider_main": "ai|stock", "provider_face": "stock|none", "avoid_showing": "...", "secondary_scene": "...", "psychological_hooks": ["...", "..."], "confidence_score": 0.0}}"""


MARKETING_AGENT_SYSTEM = """Eres un estratega de marketing digital especializado en YouTube. 
Has analizado miles de miniaturas virales y sabes exactamente qué combinación 
de imagen + texto maximiza el Click-Through Rate (CTR).

Tu expertise:
- Texto overlay de DOS LÍNEAS (NO una sola):
  • LÍNEA 1 (gancho principal, texto GRANDE): 1-2 palabras en MAYÚSCULAS. Máx 12 chars.
    **DEBE contener una palabra CLAVE del título del video. NUNCA uses frases genéricas.**
    Formatos: "SIN SANGRE", "FUE REAL", "3 MINUTOS", "5 MÉDICOS", "NADIE LO VIO"
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
1. Crea DOS líneas de texto overlay (deben funcionar CON el título, no repetirlo):
   - LÍNEA 1 (text_gancho): 1-3 palabras en MAYÚSCULAS. Máx 14 caracteres. Palabra-gancho
     concreta que DETIENE el scroll y aporta información (cifra, fecha, lugar, consecuencia).
   - LÍNEA 2 (text_complemento): 2-4 palabras. Máx 24 caracteres. Añade la intriga que el
     título no revela.
   - PROHIBIDO usar comodines vacíos: "OCULTO", "REAL", "IMPACTANTE", "INCREÍBLE",
     "PROHIBIDO", "NADIE LO VIO", "NADIE LO EXPLICÓ". El texto debe ser específico del tema.
   - **L1 DEBE contener una palabra clave concreta extraída del TÍTULO.**
2. Indica la PALABRA DE ÉNFASIS (emphasis_word): una sola palabra (1-2 máximo) de L1 o L2
   que se resaltará en la miniatura. En minúsculas, sin puntuación.
3. Define el BADGE/SELLO de confianza (badge_text): DOCUMENTAL, CASO REAL, ARCHIVO,
   EXPEDIENTE, o vacío.
4. Define la INTENCIÓN DE COLOR (color_intent) según la emoción y el contenido: p. ej.
   "frío peligro azul", "calor ámbar histórico", "verde tóxico médico". NO devuelvas hex.
5. Define la composición (dónde va el texto, dónde el foco visual). {face_directive}
6. Sugiere un layout de: topic_hero, subject_closeup, artifact_document, split_diagonal,
   negative_space_top, center_burst, portrait_hero, dark_reveal, shock_closeup.
7. Crea 3 variantes de texto completas (campo text_variants legacy).

{recent_context}

Responde SOLO con JSON:
{{"text_gancho": "...", "text_complemento": "...", "emphasis_word": "...", "badge_text": "...", "color_intent": "...", "best_text": "...", "text_variants": ["...", "...", "..."], "layout": "...", "composition_notes": "...", "ctr_estimate": "..."}}"""


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
        recent_context: dict | None = None,
    ) -> ThumbnailBrief:
        """Run both agents and merge into a complete design brief.

        Args:
            script_text: First ~1500 chars of the video script.
            title: YouTube video title.
            keywords: SEO keywords (used for context only).
            style_profile: Channel style profile from ThumbnailStyleEngine.
            channel_name: Channel display name.
            channel_theme: One-line theme summary.
            allow_faces: If False, faces are never the protagonist.
            concept_directive: Custom visual directive string. When non-empty,
                it is appended to the topic-first directive (never replaces the
                subject-first rule).
            recent_context: Summary of recent channel thumbnails (layouts,
                colours, subjects) so the agents explicitly avoid repeating them.

        Returns:
            ThumbnailBrief ready for image generation and composition.
        """
        style = style_profile or {}
        visual_style = style.get("visual_style", "dark_cinematic")
        color_palette = style.get("color_palette", {})

        # Topic-first is always the base; a channel directive is appended as an
        # extra constraint (it can no longer turn the thumbnail into a face-first
        # design).
        face_directive = DEFAULT_FACE_DIRECTIVE
        if concept_directive:
            face_directive = f"{DEFAULT_FACE_DIRECTIVE}\n{concept_directive}"
        recent_block = recent_context_prompt(recent_context)

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
                recent_context=recent_block,
            )

            marketing = self._run_marketing_agent(
                title=title,
                psych_analysis=json.dumps(psych, ensure_ascii=False),
                visual_style=visual_style,
                color_palette=color_palette,
                face_directive=face_directive,
                recent_context=recent_block,
            )

            brief = self._merge(psych, marketing, style, title=title)
            logger.info(
                "Thumbnail brief: emotion=%s text=%r layout=%s",
                brief.emotion_target, brief.text_overlay, brief.layout,
            )
            return brief

        except Exception as exc:
            logger.warning("Brainstorm failed: %s — using fallback brief", exc)
            return self._fallback_brief(title, style, allow_faces=allow_faces)

    def brainstorm_variants(
        self,
        script_text: str,
        title: str,
        keywords: list[str] | None = None,
        style_profile: dict | None = None,
        channel_name: str = "",
        channel_theme: str = "",
        allow_faces: bool = True,
        concept_directive: str = "",
        num_variants: int = 3,
        recent_context: dict | None = None,
    ) -> list[ThumbnailBrief]:
        """Generate N visually distinct thumbnail briefs for A/B testing.
        
        Each variant uses a different design strategy to maximize the
        chance that at least one achieves high CTR:
        
        Variant 1 (default): Emotional/intrigue — same as current brainstorm()
        Variant 2 (text-heavy): Text dominates 60%, curiosity question,  
                                secondary background image
        Variant 3 (image-heavy): Image dominates 80%, max 3 words,  
                                 strong emotional reaction (shock/awe/fear)
        
        Only variant 1 uses the full LLM pipeline (psychology + marketing agents).
        Variants 2 and 3 reuse the psychology analysis and adapt the marketing
        text overlay with directive-guided composition.

        Args:
            Same as brainstorm(), plus:
            num_variants: Number of variants to generate (2-3, default 3).

        Returns:
            List of ThumbnailBrief objects, one per variant.
        """
        if num_variants < 2:
            num_variants = 2
        
        variants: list[ThumbnailBrief] = []
        
        # ── Variant 1: Default (emotional / intrigue) ──────────
        try:
            brief_default = self.brainstorm(
                script_text=script_text,
                title=title,
                keywords=keywords,
                style_profile=style_profile,
                channel_name=channel_name,
                channel_theme=channel_theme,
                allow_faces=allow_faces,
                concept_directive=concept_directive,
                recent_context=recent_context,
            )
        except Exception as exc:
            logger.warning("Variant 1 brainstorm failed: %s — using fallback", exc)
            style = style_profile or {}
            brief_default = self._fallback_brief(title, style, allow_faces=allow_faces)
        variants.append(brief_default)
        
        if num_variants < 2:
            return variants
        
        # ── Variant 2: Text-heavy (curiosity question) ─────────
        # Reuses the same base image concept but changes composition
        brief_text = self._clone_brief_with_directive(
            brief_default,
            variant_directive=(
                "Crea un diseño donde el TEXTO domine el 60% de la miniatura. "
                "Usa una pregunta intrigante o dato impactante como texto principal "
                "en letras GRANDES que ocupan la mayor parte del espacio. "
                "La imagen de fondo debe ser secundaria, textura o patrón difuminado. "
                "El texto debe ser imposible de ignorar al hacer scroll."
            ),
            channel_name=channel_name,
        )
        variants.append(brief_text)
        
        if num_variants < 3:
            return variants
        
        # ── Variant 3: Image-heavy (emotional shock) ───────────
        brief_image = self._clone_brief_with_directive(
            brief_default,
            variant_directive=(
                "Crea un diseño donde la IMAGEN domine el 80% de la miniatura. "
                "Máximo 3 palabras de texto en total (una sola palabra grande o "
                "una frase cortísima de 2-3 palabras). La imagen debe provocar una "
                "reacción emocional fuerte: asombro, miedo, curiosidad intensa, shock. "
                "Fotorealista, 8K, iluminación dramática, alto contraste."
            ),
            channel_name=channel_name,
        )
        variants.append(brief_image)
        
        logger.info(
            "Brainstorm variants: %d briefs generated for '%s'",
            len(variants), title[:50],
        )
        return variants

    def _clone_brief_with_directive(
        self,
        base: ThumbnailBrief,
        variant_directive: str,
        channel_name: str = "",
    ) -> ThumbnailBrief:
        """Create a variant brief by cloning the base and adjusting 
        composition notes and text overlay for a different strategy.
        
        Uses a lightweight LLM call only for the text overlay — the
        image concept and visual focus are inherited from the base.
        """
        # Build a lightweight prompt for text overlay only
        from config.llm_client import create_llm_client
        from config.settings import LLM_MODEL_CREATIVE
        from config.llm_helpers import llm_json_call
        
        client = create_llm_client(enable_thinking=False, timeout=30.0, max_retries=1)
        
        system = """Eres un diseñador de miniaturas de YouTube.
Genera el TEXTO SUPERPUESTO para una miniatura basado en una directiva de diseño.
Devuelve JSON con: text_gancho (L1, max 12 chars, UPPERCASE), text_complemento (L2, max 24 chars), badge_text (L3, corto), composition_notes (breve).
El texto_gancho y texto_complemento DEBEN ser en español."""
        
        prompt = f"""TÍTULO DEL VIDEO: {base.text_overlay or 'Sin título'}
CONCEPTO VISUAL: {base.image_concept[:200]}
EMOCIÓN OBJETIVO: {base.emotion_target}
CANAL: {channel_name}

DIRECTIVA DE DISEÑO: {variant_directive}

Genera el texto superpuesto para esta variante."""
        
        try:
            result = llm_json_call(
                client,
                model=LLM_MODEL_CREATIVE,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.85,
                max_tokens=300,
                max_retries=1,
                retry_delay=0.5,
            )
            
            if result and isinstance(result, dict):
                return ThumbnailBrief(
                    image_concept=base.image_concept,
                    visual_focus=base.visual_focus,
                    emotion_target=base.emotion_target,
                    curiosity_gap=base.curiosity_gap,
                    text_overlay=result.get("text_gancho", "") + " " + result.get("text_complemento", ""),
                    text_gancho=result.get("text_gancho", base.text_gancho),
                    text_complemento=result.get("text_complemento", ""),
                    badge_text=result.get("badge_text", base.badge_text),
                    secondary_scene=base.secondary_scene,
                    text_color_hex=base.text_color_hex,
                    composition_notes=result.get("composition_notes", variant_directive[:200]),
                    layout=base.layout,
                    subject_type=base.subject_type,
                    primary_subject=base.primary_subject,
                    provider_main=base.provider_main,
                    provider_face=base.provider_face,
                    emphasis_word=result.get("emphasis_word", base.emphasis_word),
                    color_intent=base.color_intent,
                    psych_hooks=list(base.psych_hooks),
                    psych_score=base.psych_score,
                    marketing_ctr_estimate=base.marketing_ctr_estimate,
                    marketing_text_variants=[
                        result.get("text_gancho", ""),
                        result.get("text_complemento", ""),
                        result.get("badge_text", ""),
                    ],
                )
        except Exception as exc:
            logger.debug("Variant brief generation failed: %s — cloning base with directive", exc)
        
        # Fallback: clone base with directive as composition_notes
        return ThumbnailBrief(
            image_concept=base.image_concept,
            visual_focus=base.visual_focus,
            emotion_target=base.emotion_target,
            curiosity_gap=base.curiosity_gap,
            text_overlay=base.text_overlay,
            text_gancho=base.text_gancho,
            text_complemento=base.text_complemento,
            badge_text=base.badge_text,
            secondary_scene=base.secondary_scene,
            text_color_hex=base.text_color_hex,
            composition_notes=variant_directive[:200],
            layout=base.layout,
            subject_type=base.subject_type,
            primary_subject=base.primary_subject,
            provider_main=base.provider_main,
            provider_face=base.provider_face,
            emphasis_word=base.emphasis_word,
            color_intent=base.color_intent,
            psych_hooks=list(base.psych_hooks),
            psych_score=base.psych_score,
            marketing_ctr_estimate=base.marketing_ctr_estimate,
            marketing_text_variants=list(base.marketing_text_variants),
        )

    # ── Agent runners ────────────────────────────────────────

    def _run_psychology_agent(
        self,
        script_snippet: str,
        title: str,
        channel_name: str,
        channel_theme: str,
        visual_style: str,
        face_directive: str = "",
        recent_context: str = "",
    ) -> dict:
        """Call the psychology LLM agent."""
        from config.llm_client import create_llm_client
        from config.settings import LLM_MODEL_CREATIVE
        from config.llm_helpers import llm_json_call

        client = create_llm_client(enable_thinking=False, timeout=60.0, max_retries=2)

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
            recent_context=recent_context,
        )

        try:
            return llm_json_call(
                client,
                max_retries=3,
                retry_delay=2.0,
                model=LLM_MODEL_CREATIVE,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=0.8,
                max_tokens=500,
            )
        except Exception:
            logger.debug("Psychology agent failed after retries — returning empty dict")
            return {}

    def _run_marketing_agent(
        self,
        title: str,
        psych_analysis: str,
        visual_style: str,
        color_palette: dict,
        face_directive: str = "",
        recent_context: str = "",
    ) -> dict:
        """Call the marketing LLM agent."""
        from config.llm_client import create_llm_client
        from config.settings import LLM_MODEL_CREATIVE
        from config.llm_helpers import llm_json_call

        client = create_llm_client(enable_thinking=False, timeout=60.0, max_retries=2)

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
            recent_context=recent_context,
        )

        try:
            return llm_json_call(
                client,
                max_retries=3,
                retry_delay=2.0,
                model=LLM_MODEL_CREATIVE,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=0.9,
                max_tokens=500,
            )
        except Exception:
            logger.debug("Marketing agent failed after retries — returning empty dict")
            return {}

    # ── Merge ─────────────────────────────────────────────────

    def _merge(self, psych: dict, marketing: dict, style: dict, title: str = "") -> ThumbnailBrief:
        """Merge psychology + marketing agent outputs into a ThumbnailBrief."""
        # Best text for compatibility: L1 | L2 or single line
        best_text = marketing.get("best_text", "")
        if not best_text:
            gancho = marketing.get("text_gancho", "")
            complemento = marketing.get("text_complemento", "")
            if gancho and complemento:
                best_text = f"{gancho} | {complemento}"
            else:
                best_text = _derive_hook_from_title(title) if title else "FUE REAL | La verdad oculta"
        
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
            layout=marketing.get("layout", style.get("base_composition", "topic_hero")),
            # ── v3 tema-primero ──
            subject_type=psych.get("subject_type", ""),
            primary_subject=psych.get("primary_subject", psych.get("visual_concept", ""))[:160],
            provider_main=psych.get("provider_main", ""),
            provider_face=psych.get("provider_face", ""),
            emphasis_word=marketing.get("emphasis_word", ""),
            color_intent=marketing.get("color_intent", ""),
            psych_hooks=psych.get("psychological_hooks", []),
            psych_score=float(psych.get("confidence_score", 0.0)),
            marketing_ctr_estimate=marketing.get("ctr_estimate", ""),
            marketing_text_variants=marketing.get("text_variants", []),
        )

    def _fallback_brief(self, title: str, style: dict, allow_faces: bool = True) -> ThumbnailBrief:
        """Return a minimal brief when the LLM agents are unavailable."""
        hook_text = _derive_hook_from_title(title)
        hook_parts = hook_text.split(" | ")
        l1 = hook_parts[0] if len(hook_parts) >= 1 else "FUE REAL"
        l2 = hook_parts[1] if len(hook_parts) >= 2 else "La verdad oculta"

        if not allow_faces:
            # Clinical/medical fallback — no faces at all
            return ThumbnailBrief(
                image_concept=(
                    "clinical medical imagery, dramatic anatomical close-up, "
                    "X-ray or MRI scan with high contrast lighting, DNA helix, "
                    "laboratory environment, photorealistic, 8K, no faces"
                ),
                visual_focus="anatomical or scientific detail with dramatic clinical lighting",
                emotion_target="curiosidad clínica",
                curiosity_gap="¿Qué anomalía se oculta en este diagnóstico?",
                text_overlay=hook_text,
                text_gancho=l1,
                text_complemento=l2,
                badge_text="DOCUMENTAL",
                secondary_scene="medical report with redacted text, classified stamp",
                text_color_hex="#FFFFFF",
                composition_notes="texto GRANDE abajo con outline grueso, imagen clínica dramática arriba ocupando 70%",
                layout=style.get("base_composition", "topic_hero"),
                subject_type="medical",
                primary_subject=hook_text,
                provider_main="ai",
                provider_face="none",
                emphasis_word=l1.split()[0] if l1 else "",
                color_intent="frío clínico cian",
                psych_hooks=["morbid_curiosity", "scientific_awe", "diagnostic_urgency"],
                psych_score=0.7,
                marketing_ctr_estimate="high",
                marketing_text_variants=["DIAGNÓSTICO", "ANOMALÍA", "RARO"],
            )
        return ThumbnailBrief(
            image_concept=(
                "topic-first cinematic scene representing the video subject, "
                "single dominant focal point, dramatic lighting, no unrelated background"
            ),
            visual_focus="central topic element with strong contrast",
            emotion_target="curiosidad",
            curiosity_gap="¿Qué secreto se oculta?",
            text_overlay=hook_text,
            text_gancho=l1,
            text_complemento=l2,
            badge_text="DOCUMENTAL",
            secondary_scene="complementary document or symbol from the story",
            text_color_hex="#FFFFFF",
            composition_notes="texto GRANDE abajo con outline grueso, sujeto del tema arriba ocupando 70%",
            layout=style.get("base_composition", "topic_hero"),
            subject_type="concept",
            primary_subject=hook_text,
            provider_main="ai",
            provider_face="none",
            emphasis_word=l1.split()[0] if l1 else "",
            color_intent="atmósfera del tema",
            psych_hooks=["curiosity_gap", "emotional_arousal", "pattern_interrupt"],
            psych_score=0.7,
            marketing_ctr_estimate="high",
            marketing_text_variants=["DESCUBIERTO", "EL DATO", "LA CLAVE"],
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
