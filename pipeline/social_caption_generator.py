"""AI-powered caption generator for social media platforms.

Generates platform-optimized captions, threads, and hashtags for each
social media platform using the video's script and metadata.

Usage:
    from pipeline.social_caption_generator import SocialCaptionGenerator

    gen = SocialCaptionGenerator()
    result = gen.generate("twitter", script_text, video_title, yt_url)
    # result: {"text": "...", "thread_parts": [...], "hashtags": [...]}
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
        "twitter": 280,        # per tweet
        "tiktok": 2200,        # caption
        "instagram": 2200,     # caption
        "facebook": 63206,     # post text
        "reddit": 40000,       # post text
    }

    # ── Platform prompt templates ──────────────────────────

    PLATFORM_PROMPTS = {
        "twitter": (
            "Eres un creador de contenido viral en Twitter/X. "
            "Crea un HILO de 4-6 tweets usando este guion de video de YouTube. "
            "El primer tweet debe ser un gancho IMPACTANTE (max 280 chars). "
            "Los siguientes tweets desarrollan los puntos clave. "
            "El ultimo tweet incluye un CTA con el enlace al video completo. "
            "Usa emojis relevantes. NO uses hashtags excesivos (max 2). "
            "El tono debe ser intrigante y misterioso.\n\n"
        ),
        "tiktok": (
            "Eres un creador de contenido en TikTok. "
            "Crea un caption corto y ENGANCHE para un clip de 60 segundos de un video mas largo. "
            "Las primeras 3 palabras son CRITICAS para la retencion (usa 'No creeras...', "
            "'Esto es real...', 'El secreto de...', etc.). "
            "Incluye 3-5 hashtags relevantes y populares. "
            "Termina con un CTA tipo 'Mira el video completo en mi perfil'. "
            "Max 150 palabras.\n\n"
        ),
        "instagram": (
            "Eres un creador de contenido en Instagram. "
            "Crea un caption para un Reel de 60 segundos. "
            "Empieza con una pregunta intrigante o dato impactante. "
            "Usa saltos de linea para crear ritmo visual. "
            "Incluye 5-8 hashtags al final (los 3 primeros de nicho, "
            "los siguientes de alcance medio, y los ultimos de alto volumen). "
            "CTA: 'Guarda este reel para verlo despues' o 'Comparte con alguien que deba ver esto'. "
            "Max 200 palabras.\n\n"
        ),
        "facebook": (
            "Eres un creador de contenido en Facebook. "
            "Crea un post de texto para compartir un video de YouTube. "
            "Empieza con una pregunta o dato curioso. "
            "Resume los 3 puntos clave del video en forma de lista. "
            "Incluye el enlace al video completo. "
            "Tono conversacional, cercano. No uses hashtags.\n\n"
        ),
        "reddit": (
            "Eres un usuario de Reddit compartiendo contenido de valor. "
            "Crea un post de texto para un subreddit de [misterio/historia/ciencia]. "
            "Empieza con un TL;DR de 1-2 lineas. "
            "Luego desarrolla el contenido con 3-4 parrafos sustanciales. "
            "Incluye un enlace al video de YouTube como fuente. "
            "Tono objetivo, informativo, sin autopromocion obvia. "
            "NO uses emojis. NO uses hashtags.\n\n"
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
        """Generate a platform-optimized caption.

        Args:
            platform: One of 'twitter', 'tiktok', 'instagram', 'facebook', 'reddit'.
            script_text: The full video script text.
            video_title: Final YouTube video title.
            yt_url: YouTube video URL.
            channel_niche: Channel niche for context (e.g., 'misterio', 'historia').
            channel_tone: Channel narrative tone.

        Returns:
            PlatformCaption with text, thread_parts, hashtags, and media_ready flag.
        """
        platform = platform.lower()
        if platform not in self.PLATFORM_PROMPTS:
            logger.warning("Unknown platform '%s', using generic caption", platform)
            return self._generic_caption(platform, script_text, video_title, yt_url)

        try:
            # Try LLM generation
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
            f"ENLACE: {yt_url}\n\n"
            f"GUION (primeros 1500 caracteres):\n{script_text[:1500]}\n\n"
        )

        if platform == "twitter":
            user_prompt += (
                "Genera un objeto JSON con este formato EXACTO:\n"
                '{"thread_parts": ["tweet1", "tweet2", ...], "hashtags": ["#tag1", "#tag2"]}\n'
                "Cada tweet max 280 caracteres."
            )
        else:
            user_prompt += (
                "Genera un objeto JSON con este formato EXACTO:\n"
                '{"text": "caption completo", "hashtags": ["#tag1", "#tag2", ...]}\n'
            )

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
            timeout=20,
        )
        data = resp.json()
        content = data["choices"][0]["message"]["content"]

        # Parse JSON from response
        json_match = re.search(r'\{.*\}', content, re.DOTALL)
        if not json_match:
            raise ValueError("No JSON found in LLM response")

        parsed = json.loads(json_match.group())

        if platform == "twitter":
            thread_parts = parsed.get("thread_parts", [])
            return PlatformCaption(
                platform=platform,
                text=thread_parts[0] if thread_parts else "",
                thread_parts=thread_parts,
                hashtags=parsed.get("hashtags", []),
                media_ready=False,
            )
        else:
            return PlatformCaption(
                platform=platform,
                text=parsed.get("text", ""),
                hashtags=parsed.get("hashtags", []),
                media_ready=platform in ("tiktok", "instagram"),
            )

    # ── Heuristic fallback ─────────────────────────────────

    def _heuristic_caption(
        self, platform: str, script_text: str, video_title: str, yt_url: str,
    ) -> PlatformCaption:
        """Generate a caption without LLM, using the script text directly."""
        # Extract first meaningful sentences
        sentences = re.split(r'[.!?]+', script_text[:2000])
        key_points = [s.strip() for s in sentences if len(s.strip()) > 20][:3]

        if platform == "twitter":
            # Create a mini-thread from key points
            thread = [f"🔥 {video_title}\n"]
            for i, point in enumerate(key_points[:4]):
                tweet = f"{i+1}/ {point}..."
                if len(tweet) > 280:
                    tweet = tweet[:277] + "..."
                thread.append(tweet)
            thread.append(f"🎬 Video completo: {yt_url}")
            return PlatformCaption(
                platform=platform,
                text=thread[0],
                thread_parts=thread,
                hashtags=["#YouTube"],
                media_ready=False,
            )

        elif platform in ("tiktok", "instagram"):
            hook_phrases = [
                "No creeras lo que descubri 🔥",
                "Esto cambia todo lo que sabias 😱",
                "El secreto que nadie te cuenta 👀",
                "Mira esto antes de que lo borren ⚡",
            ]
            hook = hook_phrases[hash(video_title) % len(hook_phrases)]
            text = f"{hook}\n\n{video_title}\n\n"
            if key_points:
                text += f"{key_points[0]}...\n\n"
            text += f"🎬 Video completo en mi perfil — Link en bio"
            return PlatformCaption(
                platform=platform,
                text=text,
                hashtags=["#viral", "#curiosidades", "#datoscuriosos"],
                media_ready=True,
            )

        elif platform == "facebook":
            text = f"🤔 {video_title}\n\n"
            for i, point in enumerate(key_points[:3]):
                text += f"{i+1}. {point}\n"
            text += f"\n📺 Mira el video completo aqui: {yt_url}"
            return PlatformCaption(
                platform=platform,
                text=text,
                hashtags=[],
                media_ready=False,
            )

        elif platform == "reddit":
            text = f"**TL;DR:** {key_points[0] if key_points else video_title}\n\n"
            text += f"{script_text[:1500]}...\n\n"
            text += f"[Video completo en YouTube]({yt_url})"
            return PlatformCaption(
                platform=platform,
                text=text,
                hashtags=[],
                media_ready=False,
            )

        # Generic fallback
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
