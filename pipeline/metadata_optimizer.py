"""Metadata Optimizer — Re-optimize YouTube video title, description, and tags.

Uses LLM to regenerate metadata based on video performance analytics,
targeting improved CTR and search ranking.

Quota costs:
  - LLM call            → 0 (OpenAI/DeepSeek billing)
  - videos().update()   → 50 units
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Optional

from config.llm_client import create_llm_client
from googleapiclient.errors import HttpError

from config.settings import LLM_MODEL_CREATIVE, TOKENS_DIR
from pipeline.youtube_playlists import _load_credentials

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════════
# Prompt
# ═══════════════════════════════════════════════════════════════════

_OPTIMIZE_SYSTEM = """Eres un experto en YouTube SEO especializado en reoptimizar metadatos de videos con bajo rendimiento.

Tu trabajo es analizar el contenido de un video que está teniendo bajo CTR y generar NUEVOS metadatos que mejoren el rendimiento SIN cambiar el contenido real del video.

REGLAS:
1. El NUEVO título debe ser MÁS intrigante/clickbait ético que el actual — usar palabras de alto impacto emocional DIFERENTES a las actuales
2. Plantear una pregunta, misterio o cliffhanger que el video realmente responda
3. La NUEVA descripción debe enganchar en las 2 primeras líneas y usar emojis estratégicos
4. Los NUEVOS tags deben incluir keywords de alto volumen de búsqueda relacionadas
5. El título debe tener máximo 100 caracteres

Responde SIEMPRE en formato JSON con estas claves:
{
  "title": "NUEVO TÍTULO VIRAL (max 100 chars, distinto al actual)",
  "description": "nueva descripción con emojis y hashtags",
  "tags": ["tag1", "tag2", ...],
  "reason": "breve explicación de por qué estos cambios mejorarán el CTR"
}"""


# ═══════════════════════════════════════════════════════════════════
# MetadataOptimizer
# ═══════════════════════════════════════════════════════════════════

class MetadataOptimizer:
    """Re-optimize YouTube video metadata based on performance data."""

    def __init__(self, channel_slug: str):
        self.slug = channel_slug
        self._token_path = TOKENS_DIR / f"{channel_slug}.pickle"
        self._service: Any = None
        self._config: Any = None
        self._llm_client: OpenAI | None = None

    # ── Config ────────────────────────────────────────────────────

    @property
    def config(self):
        if self._config is None:
            from config.config_bridge import get_channel_config
            self._config = get_channel_config(self.slug)
        return self._config

    # ── Auth ───────────────────────────────────────────────────────

    def authenticate(self) -> bool:
        creds = _load_credentials(self._token_path)
        if creds is None:
            return False
        from googleapiclient.discovery import build
        self._service = build("youtube", "v3", credentials=creds, cache_discovery=False)
        return True

    def _ensure_auth(self):
        if self._service is None and not self.authenticate():
            raise RuntimeError(f"Cannot authenticate channel {self.slug}")

    # ── LLM ───────────────────────────────────────────────────────

    def _get_llm(self) -> OpenAI:
        if self._llm_client is None:
            self._llm_client = create_llm_client(
                enable_thinking=False,
                timeout=60.0,
                max_retries=2,
            )
        return self._llm_client

    # ── Metadata reoptimization ───────────────────────────────────

    def reoptimize(self, yt_video_id: str, script_text: str,
                    current_title: str, current_description: str = "",
                    analytics_data: dict = None) -> dict:
        """Generate optimized metadata via LLM based on current performance.

        Args:
            yt_video_id: YouTube video ID
            script_text: Original video script (for context)
            current_title: Current YouTube title
            current_description: Current YouTube description
            analytics_data: Optional dict with views, ctr, retention data

        Returns: {title, description, tags, reason}
        """
        analytics_str = ""
        if analytics_data:
            analytics_str = f"""
RENDIMIENTO ACTUAL:
- Visualizaciones: {analytics_data.get('viewCount', 'N/A')}
- Likes: {analytics_data.get('likeCount', 'N/A')}
- Comentarios: {analytics_data.get('commentCount', 'N/A')}
"""
        else:
            analytics_str = "\nRENDIMIENTO ACTUAL: CTR bajo (necesita mejora)\n"

        user_prompt = f"""DATOS DEL VIDEO:
Título actual: {current_title}
Descripción actual: {current_description[:500] if current_description else '(sin descripción)'}
{analytics_str}
CONTENIDO DEL VIDEO (guion):
{script_text[:2500]}

Genera NUEVOS metadatos que mejoren el rendimiento."""

        try:
            from config.llm_helpers import llm_json_call
            client = self._get_llm()
            result = llm_json_call(
                client,
                max_retries=3,
                retry_delay=2.0,
                model=LLM_MODEL_CREATIVE,
                messages=[
                    {"role": "system", "content": _OPTIMIZE_SYSTEM},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=0.9,
                max_tokens=2000,
            )

            title = result.get("title", current_title)[:100]
            description = result.get("description", "")
            tags = result.get("tags", [])[:60]  # YouTube max 60 tags
            reason = result.get("reason", "Reoptimización automática por bajo CTR")

            logger.info("[%s] Metadata reoptimized for %s: %s", self.slug, yt_video_id, title[:80])

            return {
                "title": title,
                "description": description,
                "tags": tags,
                "reason": reason,
            }

        except json.JSONDecodeError:
            logger.warning("[%s] LLM returned invalid JSON for metadata reoptimization", self.slug)
            return None
        except Exception as exc:
            logger.error("[%s] Metadata reoptimization failed after retries: %s", self.slug, exc)
            return None

    def apply_optimization(self, yt_video_id: str, new_title: str,
                            new_description: str = None,
                            new_tags: list[str] = None,
                            category_id: str = None) -> dict:
        """Apply new metadata to a YouTube video via videos().update().

        Quota: 50 units.

        Returns {updated: True, yt_video_id}.
        """
        self._ensure_auth()

        snippet = {"title": new_title[:100]}

        if new_description is not None:
            snippet["description"] = new_description[:5000]
        if new_tags is not None:
            # Sanitize tags
            sanitized = []
            for tag in new_tags:
                clean = str(tag).replace('"', '').replace("'", '').replace('\n', '').strip()
                if clean and 0 < len(clean) <= 30:
                    sanitized.append(clean)
            snippet["tags"] = sanitized[:60]
        if category_id is not None:
            snippet["categoryId"] = category_id
        elif self.config:
            snippet["categoryId"] = getattr(self.config, "YT_CATEGORY_ID", "22")

        body = {
            "id": yt_video_id,
            "snippet": snippet,
        }

        self._service.videos().update(
            part="snippet",
            body=body,
        ).execute()

        logger.info("[%s] Metadata updated for video %s: %s", self.slug, yt_video_id, new_title[:80])
        return {"updated": True, "yt_video_id": yt_video_id}

    def run_full_optimization(self, yt_video_id: str, script_text: str,
                               current_title: str, current_description: str = "",
                               analytics_data: dict = None) -> dict:
        """Full cycle: reoptimize + apply. Returns result or None on failure."""
        new_meta = self.reoptimize(
            yt_video_id, script_text,
            current_title, current_description,
            analytics_data,
        )
        if new_meta is None:
            return None

        try:
            self.apply_optimization(
                yt_video_id,
                new_meta["title"],
                new_meta["description"],
                new_meta["tags"],
            )
            return {
                "old_title": current_title,
                "new_title": new_meta["title"],
                "reason": new_meta["reason"],
            }
        except Exception as exc:
            logger.error("[%s] Failed to apply metadata optimization for %s: %s",
                         self.slug, yt_video_id, exc)
            return {"error": str(exc), "optimized_metadata": new_meta}
