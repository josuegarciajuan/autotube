"""Playlist Generator — LLM-based subniche discovery and SEO playlist creation.

Generates 10 thematic playlists for a YouTube channel by:
1. Analysing the channel's niche and keywords via LLM
2. Producing 10 distinct sub-niches (subtemas)
3. For each sub-niche, generating an SEO-optimised name and description
4. Auto-generating URL-safe slugs

Used by:
- ``create_playlists_for_channel()`` in ``pipeline/youtube_playlists.py``
- ``scripts/create_all_playlists.py`` for batch channel processing
"""

import json
import logging
import re
from typing import Optional

logger = logging.getLogger(__name__)


def _slugify(text: str, max_len: int = 50) -> str:
    """Convert a Spanish/English playlist name to a URL-safe kebab-case slug."""
    # Lowercase, replace spaces/punctuation with hyphens
    slug = text.lower().strip()
    slug = re.sub(r'[^a-z0-9\s-]', '', slug)
    slug = re.sub(r'[\s_]+', '-', slug)
    slug = re.sub(r'-+', '-', slug)
    slug = slug.strip('-')[:max_len].rstrip('-')
    return slug or "playlist"


def generate_playlists_for_channel(
    channel_name: str,
    niche_keywords: list[str] | None = None,
    niche_description: str = "",
    language: str = "es",
    count: int = 10,
    llm_model: str | None = None,
    llm_api_key: str | None = None,
    llm_base_url: str | None = None,
) -> list[dict]:
    """Generate ``count`` thematic playlist configs for a channel via LLM.

    Args:
        channel_name: Human-readable channel name (e.g. "Sincronías")
        niche_keywords: List of niche-defining keywords (e.g. ["misterios", "sincronicidades"])
        niche_description: Optional longer description of the channel's theme
        language: "es" for Spanish, "en" for English (affects LLM output language)
        count: Number of playlists to generate (default 10)
        llm_model: Override LLM_MODEL from settings
        llm_api_key: Override LLM_API_KEY
        llm_base_url: Override LLM_BASE_URL

    Returns:
        List of playlist config dicts, each with keys:
        ``slug``, ``name``, ``description``, ``type`` (always "thematic"),
        ``keywords`` (English keywords for scraping)
    """
    from config.llm_client import create_llm_client
    from config.settings import LLM_MODEL

    model = llm_model or LLM_MODEL
    api_key = llm_api_key or None  # handled by create_llm_client defaults
    base_url = llm_base_url or None

    client = create_llm_client(enable_thinking=True, api_key=api_key, base_url=base_url)

    keywords_text = ", ".join(niche_keywords) if niche_keywords else ""
    niche_text = f"{channel_name}"
    if keywords_text:
        niche_text += f" (keywords: {keywords_text})"
    if niche_description:
        niche_text += f" — {niche_description}"

    lang_instruction = "en español" if language == "es" else "in English"

    system_prompt = (
        f"Eres un experto en YouTube y SEO. Tu tarea es analizar el nicho de un canal "
        f"y proponer {count} subnichos distintos (subtemas) para crear listas de reproducción temáticas. "
        f"Cada subnicho debe ser un tema específico dentro del nicho general, "
        f"lo suficientemente diferente de los otros para que cada lista tenga contenido único. "
        f"Para cada subnicho, debes generar:\n"
        f"1. Un nombre de playlist optimizado para SEO {lang_instruction} (50-60 caracteres máx)\n"
        f"2. Una descripción SEO {lang_instruction} (150-200 caracteres máx) que incluya keywords y un CTA suave\n"
        f"3. 3-5 keywords en inglés (para búsqueda de contenido)\n\n"
        f"Responde SOLO con un JSON array de {count} objetos con este formato:\n"
        f'[{{"name": "...", "description": "...", "keywords_en": ["kw1", "kw2", "kw3"]}}]'
    )

    user_prompt = (
        f"NICHO DEL CANAL: {niche_text}\n\n"
        f"Genera exactamente {count} subnichos temáticos distintos para este canal. "
        f"Asegúrate de que los nombres y descripciones estén optimizados para SEO "
        f"y que cada subnicho sea claramente diferente de los demás."
    )

    try:
        from config.llm_helpers import llm_json_call

        raw = llm_json_call(
            client,
            max_retries=3,
            retry_delay=2.0,
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.8,
            max_tokens=4000,
        )

        if not isinstance(raw, list):
            raise ValueError(f"Expected JSON array, got {type(raw).__name__}")

        # Build playlist configs with auto-generated slugs
        playlists = []
        used_slugs = set()

        for item in raw[:count]:
            name = str(item.get("name", "")).strip()
            description = str(item.get("description", "")).strip()
            keywords_en = list(item.get("keywords_en", [])) if isinstance(item.get("keywords_en"), list) else []

            if not name:
                continue

            # Generate slug — ensure uniqueness
            base_slug = _slugify(name)
            slug = base_slug
            counter = 1
            while slug in used_slugs:
                slug = f"{base_slug}-{counter}"
                counter += 1
            used_slugs.add(slug)

            playlists.append({
                "slug": slug,
                "name": name[:150],
                "description": description[:5000],
                "type": "thematic",
                "keywords_en": keywords_en[:5],
            })

        logger.info(
            "Generated %d playlist configs for channel '%s' (requested %d)",
            len(playlists), channel_name, count,
        )
        return playlists

    except json.JSONDecodeError as e:
        logger.error("Failed to parse LLM response as JSON: %s\nRaw: %s", e, content[:500])
        raise RuntimeError(f"Playlist generation failed: invalid JSON from LLM") from e
    except Exception as e:
        logger.error("Playlist generation error for '%s': %s", channel_name, e)
        raise
