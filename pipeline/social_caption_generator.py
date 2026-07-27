"""AI-powered caption generator for social media platforms.

Generates platform-optimized captions with exact structures proven to
maximize reach and drive traffic to YouTube. Strategy per platform:

    TikTok:    Vertical clip (cliffhanger) + short caption, NO link.
               CTA = "Video completo en mi perfil".
    Twitter/X: 5-7 tweet thread. Self-contained value, link only in last tweet.
    Instagram: Same clip as TikTok. Caption with line breaks. Link in bio.
    Facebook:  Text post with bullets + DIRECT YouTube link (OG card).
    Reddit:    Long-form text post (standalone value). No link, subtle mention.

Usage:
    from pipeline.social_caption_generator import SocialCaptionGenerator

    gen = SocialCaptionGenerator()
    result = gen.generate("twitter", script_text, video_title, yt_url)
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class PlatformCaption:
    """Generated caption for a social media platform."""
    platform: str
    text: str                          # main post text
    thread_parts: list[str] = field(default_factory=list)
    hashtags: list[str] = field(default_factory=list)
    media_ready: bool = False          # whether a clip/image is expected


class SocialCaptionGenerator:
    """Generate platform-optimized social media captions using LLM."""

    # ── Character limits ───────────────────────────────────

    LIMITS = {
        "twitter_thread": 280,   # per tweet
        "twitter_hook": 240,     # first tweet (leaves room for 🧵)
        "tiktok": 150,           # max 150 chars for caption
        "instagram": 2200,       # caption + hidden hashtags
        "facebook": 63206,       # post text
        "reddit": 40000,         # post text
    }

    # ── Platform prompt templates (v2 — strategy-driven) ────

    PLATFORM_PROMPTS = {
        "twitter": (
            "Eres un creador de hilos virales en Twitter/X.\n"
            "REGLAS ESTRICTAS:\n"
            "- Genera EXACTAMENTE 5-7 tweets (no mas, no menos).\n"
            "- Tweet 1: HOOK. Dato increible o pregunta intrigante. Max 240 chars. "
            "NO incluyas el link aqui.\n"
            "- Tweets 2 al penultimo: Desarrolla la historia. Un dato o revelacion por tweet. "
            "Manten el suspense.\n"
            "- ULTIMO tweet: CTA + link al video. Ej: 'El analisis completo con todos "
            "los datos en YouTube: [LINK]'.\n"
            "- Maximo 1 hashtag en total (opcional).\n"
            "- Cada tweet debe ser autonomo y leerse bien por separado.\n"
            "- Sin emojis en el primer tweet. Max 1 emoji en los demas.\n"
            "- Tono: intrigante, objetivo, cero clickbait barato.\n\n"
        ),
        "tiktok": (
            "Eres un creador de contenido en TikTok.\n"
            "REGLAS ESTRICTAS:\n"
            "- Caption CORTO: max 150 caracteres (espacios incluidos).\n"
            "- PRIMERAS 3-5 PALABRAS = gancho (ej: 'Esto es real...', 'No sabias que...', "
            "'El experimento mas...').\n"
            "- NUNCA incluyas el link de YouTube. TikTok penaliza links externos.\n"
            "- CTA suave: 'Video completo en mi perfil' o 'Parte 2 en mi perfil 🔗'.\n"
            "- 3-5 hashtags de NICHO (no genericos como #fyp #viral).\n"
            "- Sin emojis en los hashtags. Max 1-2 emojis en total.\n\n"
        ),
        "instagram": (
            "Eres un creador de contenido en Instagram.\n"
            "REGLAS ESTRICTAS:\n"
            "- Empieza con una PREGUNTA intrigante o dato impactante.\n"
            "- Usa saltos de linea (linea en blanco cada 2-3 frases) para ritmo visual.\n"
            "- NUNCA incluyas el link de YouTube en el caption (no es cliqueable en IG).\n"
            "- CTA: 'Guarda este reel para verlo despues 🔖' y/o "
            "'Mira el video completo 👉 Link en bio'.\n"
            "- 6-8 hashtags: 3 de nicho especifico + 3 de alcance medio + 2 masivos.\n"
            "- Los hashtags separados del caption con puntos (.) para ocultarlos.\n\n"
        ),
        "facebook": (
            "Eres un creador de contenido en Facebook.\n"
            "REGLAS ESTRICTAS:\n"
            "- Empieza con: 'Sabias que...?' o dato curioso impactante.\n"
            "- Resume 3 puntos clave del video en formato lista (con emojis de bullet: 🔍 📂 ⚠️).\n"
            "- INCLUYE el link de YouTube. Facebook SI genera preview card con thumbnail.\n"
            "- CTA directo: 'Mira el video completo aqui 👇' y luego el link.\n"
            "- Tono conversacional, cercano, como si hablaras con un amigo.\n"
            "- CERO hashtags (Facebook los ignora).\n\n"
        ),
        "reddit": (
            "Eres un usuario de Reddit compartiendo una historia fascinante.\n"
            "REGLAS ESTRICTAS:\n"
            "- EMPIEZA con TL;DR: 1-2 lineas resumiendo lo mas impactante.\n"
            "- Desarrolla 3-5 parrafos sustanciales. Cada parrafo = una idea completa.\n"
            "- El post DEBE ser valioso por si mismo. Si alguien lee solo el post, "
            "debe sentir que aprendio algo.\n"
            "- NO incluyas el link de YouTube. NO hagas autopromocion.\n"
            "- Solo al final, una mencion SUTIL: 'Investigue este tema para un video. "
            "Si te interesa profundizar, el analisis completo esta en mi perfil.'\n"
            "- Tono objetivo, informativo, bien documentado.\n"
            "- CERO emojis. CERO hashtags. CERO mayusculas innecesarias.\n"
            "- NO uses 'mira mi video', 'suscribete', ni nada promocional.\n\n"
        ),
    }

    def __init__(self):
        pass

    # ── Main generation ────────────────────────────────────

    def generate(
        self,
        platform: str,
        script_text: str,
        video_title: str,
        yt_url: str,
        channel_niche: str = "",
        channel_tone: str = "",
    ) -> PlatformCaption:
        """Generate a platform-optimized caption."""
        platform = platform.lower()
        if platform not in self.PLATFORM_PROMPTS:
            logger.warning("Unknown platform '%s', using generic caption", platform)
            return self._generic_caption(platform, script_text, video_title, yt_url)

        try:
            result = self._llm_generate(
                platform, script_text, video_title, yt_url, channel_niche, channel_tone,
            )
            return result
        except Exception as exc:
            logger.warning("LLM caption generation failed for %s: %s — using heuristic", platform, exc)
            return self._heuristic_caption(platform, script_text, video_title, yt_url)

    def _llm_generate(
        self, platform: str, script_text: str, video_title: str,
        yt_url: str, niche: str, tone: str,
    ) -> PlatformCaption:
        """Use LLM to generate a platform-optimized caption."""
        from config.settings import AI_API_KEY, AI_BASE_URL, AI_MODEL
        import requests

        system_prompt = self.PLATFORM_PROMPTS.get(platform, "")
        if niche:
            system_prompt += f"\nEl nicho del canal es: {niche}."
        if tone:
            system_prompt += f"\nEl tono narrativo es: {tone}."

        user_prompt = (
            f"TITULO DEL VIDEO: {video_title}\n"
            f"ENLACE DE YOUTUBE: {yt_url}\n\n"
            f"GUION (primeros 2000 caracteres):\n{script_text[:2000]}\n\n"
        )

        # ── Platform-specific JSON schemas ─────────────────

        schemas = {
            "twitter": {
                "schema": (
                    'Genera un JSON EXACTO con este formato (sin texto fuera del JSON):\n'
                    '{"thread_parts": [\n'
                    '  "Tweet 1: HOOK. Max 240 chars. NO incluye link.",\n'
                    '  "Tweet 2: Desarrollo. Un dato intrigante.",\n'
                    '  "Tweet 3: Mas desarrollo o giro.",\n'
                    '  "Tweet 4: Plot twist o revelacion parcial.",\n'
                    '  "Tweet 5: Cliffhanger.",\n'
                    '  "Tweet 6: CTA + link al video completo."\n'
                    '], "hashtags": ["#UnSoloHashtag"]}'
                ),
                "parser": lambda p: PlatformCaption(
                    platform=platform,
                    text=p.get("thread_parts", [""])[0] if p.get("thread_parts") else "",
                    thread_parts=p.get("thread_parts", []),
                    hashtags=p.get("hashtags", [])[:1],
                ),
            },
            "tiktok": {
                "schema": (
                    'Genera un JSON EXACTO con este formato (sin texto fuera del JSON):\n'
                    '{"text": "Gancho en 3-5 palabras + 1 linea de contexto. '
                    'Max 150 chars TOTALES. NUNCA incluyas link.",\n'
                    '"hashtags": ["#nicho1", "#nicho2", "#nicho3"]}'
                ),
                "parser": lambda p: PlatformCaption(
                    platform=platform,
                    text=p.get("text", ""),
                    hashtags=p.get("hashtags", [])[:5],
                    media_ready=True,
                ),
            },
            "instagram": {
                "schema": (
                    'Genera un JSON EXACTO con este formato (sin texto fuera del JSON):\n'
                    '{"text": "Pregunta o dato intrigante.\\\\n\\\\n'
                    'Frase de contexto.\\\\n.\\\\n.\\\\n.\\\\n'
                    'CTA: Guarda este reel / Link en bio.\\\\n'
                    'LOS HASHTAGS VAN ABAJO, SEPARADOS POR PUNTOS",\n'
                    '"hashtags": ["#nicho1", "#nicho2", "#nicho3", '
                    '"#alcance1", "#alcance2", "#alcance3", "#masivo1", "#masivo2"]}'
                ),
                "parser": lambda p: PlatformCaption(
                    platform=platform,
                    text=p.get("text", ""),
                    hashtags=p.get("hashtags", [])[:8],
                    media_ready=True,
                ),
            },
            "facebook": {
                "schema": (
                    'Genera un JSON EXACTO con este formato (sin texto fuera del JSON):\n'
                    '{"text": "Sabias que...? dato impactante.\\\\n\\\\n'
                    'Acabo de publicar un video sobre [tema]. Esto descubri:\\\\n\\\\n'
                    '🔍 Punto 1\\\\n📂 Punto 2\\\\n⚠️ Punto 3\\\\n\\\\n'
                    'Mira el video completo aqui: [LINK]"}'
                ),
                "parser": lambda p: PlatformCaption(
                    platform=platform,
                    text=p.get("text", ""),
                    hashtags=[],
                ),
            },
            "reddit": {
                "schema": (
                    'Genera un JSON EXACTO con este formato (sin texto fuera del JSON):\n'
                    '{"title": "Titulo del post (dato intrigante, max 300 chars)",\n'
                    '"text": "**TL;DR:** [1-2 lineas].\\\\n\\\\n'
                    '[Parrafo 1: Contexto].\\\\n\\\\n'
                    '[Parrafo 2: Detalles impactantes].\\\\n\\\\n'
                    '[Parrafo 3: Implicaciones].\\\\n\\\\n'
                    '\\\\n---\\\\n'
                    'Fuentes:\\\\n- Fuente 1\\\\n- Fuente 2\\\\n'
                    'Investigue este tema para un video. Si quieres profundizar, '
                    'el analisis completo esta en mi perfil."}'
                ),
                "parser": lambda p: PlatformCaption(
                    platform=platform,
                    text=p.get("text", ""),
                    hashtags=[],
                ),
            },
        }

        schema_info = schemas.get(platform, {})
        if schema_info:
            user_prompt += schema_info["schema"]

        resp = requests.post(
            f"{AI_BASE_URL}/chat/completions",
            headers={
                "Authorization": f"Bearer {AI_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": AI_MODEL,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                "temperature": 0.7,
                "max_tokens": 800,
            },
            timeout=25,
        )
        data = resp.json()
        content = data["choices"][0]["message"]["content"]

        # Parse JSON from response
        json_match = re.search(r'\{.*\}', content, re.DOTALL)
        if not json_match:
            raise ValueError("No JSON found in LLM response")

        parsed = json.loads(json_match.group())

        if schema_info and "parser" in schema_info:
            return schema_info["parser"](parsed)

        # Generic fallback parser
        return PlatformCaption(
            platform=platform,
            text=parsed.get("text", ""),
            hashtags=parsed.get("hashtags", []),
        )

    # ── Heuristic fallback (strategy-aligned) ───────────────

    def _heuristic_caption(
        self, platform: str, script_text: str, video_title: str, yt_url: str,
    ) -> PlatformCaption:
        """Generate a caption without LLM, following the exact strategy."""
        sentences = re.split(r'[.!?]+', script_text[:2000])
        key_points = [s.strip() for s in sentences if len(s.strip()) > 20][:5]

        if platform == "twitter":
            thread = []
            # Tweet 1: hook
            hook = f"{key_points[0][:235]}..." if key_points else f"{video_title[:235]} 🧵"
            thread.append(hook)
            # Tweets 2-5: development
            for i, pt in enumerate(key_points[1:4]):
                tweet = f"{pt[:275]}..." if len(pt) > 275 else pt
                thread.append(tweet)
            # Last tweet: CTA + link
            thread.append(f"El analisis completo en YouTube:\n{yt_url}")
            return PlatformCaption(
                platform=platform,
                text=thread[0],
                thread_parts=thread,
                hashtags=[],
            )

        elif platform == "tiktok":
            hook = key_points[0][:100] if key_points else video_title[:100]
            text = f"{hook}... 🎬 Video completo en mi perfil"
            return PlatformCaption(
                platform=platform,
                text=text[:150],
                hashtags=["#misterio", "#curiosidades", "#datos"],
                media_ready=True,
            )

        elif platform == "instagram":
            hook = key_points[0][:120] if key_points else video_title[:120]
            text = (
                f"Sabias esto? 🤔\n\n"
                f"{hook}...\n.\n.\n.\n"
                f"Guarda este reel para verlo despues 🔖\n"
                f"Mira el video completo 👉 Link en bio"
            )
            return PlatformCaption(
                platform=platform,
                text=text,
                hashtags=["#misterio", "#historia", "#documental", "#curiosidades", "#datos", "#sabiasque"],
                media_ready=True,
            )

        elif platform == "facebook":
            bullets = "\n".join(
                ["🔍 " + key_points[i] for i in range(min(3, len(key_points)))]
            ) if key_points else "Descubrelo tu mismo"
            text = (
                f"Sabias que {key_points[0][:80].lower() if key_points else 'esto'}? 🤔\n\n"
                f"Acabo de publicar un video sobre {video_title}.\n"
                f"Esto fue lo que descubri:\n\n"
                f"{bullets}\n\n"
                f"Mira el video completo aqui 👇\n"
                f"{yt_url}"
            )
            return PlatformCaption(
                platform=platform, text=text, hashtags=[],
            )

        elif platform == "reddit":
            tl_dr = key_points[0][:200] if key_points else video_title[:200]
            body = "\n\n".join(key_points[:4]) if key_points else script_text[:1500]
            text = (
                f"**TL;DR:** {tl_dr}\n\n"
                f"{body}\n\n"
                f"---\n"
                f"Investigue este tema para un video. Si te interesa profundizar, "
                f"el analisis completo esta en mi perfil."
            )
            return PlatformCaption(
                platform=platform, text=text, hashtags=[],
            )

        return self._generic_caption(platform, script_text, video_title, yt_url)

    def _generic_caption(
        self, platform: str, script_text: str, video_title: str, yt_url: str,
    ) -> PlatformCaption:
        """Minimal generic caption."""
        return PlatformCaption(
            platform=platform,
            text=f"{video_title}\n\n{yt_url}",
            hashtags=[],
            media_ready=False,
        )
