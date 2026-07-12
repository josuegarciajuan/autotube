"""Viral Mirror — content cloning utilities.

Provides three cloning operations for the viral pipeline branch:
  1. clone_title_description() — translated + paraphrased metadata
  2. clone_thumbnail()       — Vision AI → Pollo AI replication with modifications
  3. build_viral_metadata()  — assemble full metadata dict for phase_metadata/video

All operations apply the "not a copy" strategy: translate, paraphrase ~30%,
adapt to channel style, and modify visuals.
"""

from __future__ import annotations

import json
import logging
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Optional
from types import SimpleNamespace

import requests

from config.config_bridge import get_channel_config

logger = logging.getLogger(__name__)


# ── Constants ─────────────────────────────────────────────────────────

_OUTPUT_DIR = Path(__file__).resolve().parent.parent / "output"
_THUMB_DIR = _OUTPUT_DIR / "thumbnails"


# ── LLM helpers ──────────────────────────────────────────────────────

def _get_llm_client(config: Optional[SimpleNamespace] = None):
    """Get an OpenAI-compatible client from config settings."""
    from config.settings import (
        DEEPSEEK_API_KEY, DEEPSEEK_BASE_URL, DEEPSEEK_MODEL,
        OPENAI_API_KEY, OPENAI_BASE_URL, OPENAI_MODEL,
    )
    from openai import OpenAI

    if DEEPSEEK_API_KEY:
        return OpenAI(
            api_key=DEEPSEEK_API_KEY,
            base_url=getattr(config, 'DEEPSEEK_BASE_URL', DEEPSEEK_BASE_URL) if config else DEEPSEEK_BASE_URL,
        ), getattr(config, 'DEEPSEEK_MODEL', DEEPSEEK_MODEL) if config else DEEPSEEK_MODEL
    elif OPENAI_API_KEY:
        return OpenAI(
            api_key=OPENAI_API_KEY,
            base_url=getattr(config, 'OPENAI_BASE_URL', OPENAI_BASE_URL) if config else OPENAI_BASE_URL,
        ), getattr(config, 'OPENAI_MODEL', OPENAI_MODEL) if config else OPENAI_MODEL
    return None, None


def _call_llm_json(config: Optional[SimpleNamespace], system: str, user: str, temp: float = 0.5) -> dict | None:
    """Call LLM and parse JSON response."""
    client, model = _get_llm_client(config)
    if not client:
        return None
    try:
        resp = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            temperature=temp,
            max_tokens=2048,
            response_format={"type": "json_object"},
        )
        content = resp.choices[0].message.content
        return json.loads(content) if content else None
    except Exception as e:
        logger.error("LLM call failed: %s", e)
        return None


# ── Title/Description Cloning ───────────────────────────────────────

def clone_title_description(
    viral_meta_json: str,
    channel_slug: str,
) -> dict:
    """Extract and enhance translated title/description from viral metadata.

    Returns:
        {
            "selected_title": str,
            "description": str,
            "tags": list[str],
            "titles": list[str],
            "thumbnail_text": str,
        }
    """
    logger.info("[%s] clone_title_description: START", channel_slug)
    channel_config = get_channel_config(channel_slug)
    channel_name = getattr(channel_config, "CANAL_DISPLAY_NAME", "")
    channel_tagline = getattr(channel_config, "CANAL_TAGLINE", "")
    channel_tone = getattr(channel_config, "CANAL_TONE", "")
    default_tags = getattr(channel_config, "YT_DEFAULT_TAGS", [])
    seo_hashtags = getattr(channel_config, "SEO_HASHTAGS", [])

    # Parse viral metadata
    try:
        viral_meta = json.loads(viral_meta_json) if isinstance(viral_meta_json, str) else viral_meta_json
    except (json.JSONDecodeError, TypeError):
        viral_meta = {}

    translated_title = viral_meta.get("translated_title", "")
    translated_desc = viral_meta.get("translated_description", "")
    block_texts = [b.get("text", "") for b in viral_meta.get("blocks", [])]

    # If no translated title, try to rebuild from the script text
    if not translated_title and block_texts:
        # Use first block's first sentence as title fallback
        first_sentence = block_texts[0].split(".")[0].strip()
        if len(first_sentence) > 20:
            translated_title = first_sentence

    # Build description from script blocks if no translated description
    if not translated_desc and block_texts:
        desc_lines = []
        for block in viral_meta.get("blocks", []):
            text = block.get("text", "")
            if text and len(text) > 30:
                # Take first 2 sentences of each block
                sentences = text.split(".")
                desc_lines.append(". ".join(sentences[:2]) + ".")
                if len(desc_lines) >= 4:
                    break
        translated_desc = "\n\n".join(desc_lines)

    # Generate tags from keyword extraction
    tags = _extract_tags_from_script(block_texts, channel_config)

    # Build thumbnail overlay text (short punchy phrase from title)
    thumbnail_text = translated_title[:40] if translated_title else channel_tagline[:40]

    # Generate alternative titles by paraphrasing
    alt_titles = _generate_alt_titles(translated_title, channel_config)

    result = {
        "selected_title": translated_title,
        "description": translated_desc,
        "tags": tags,
        "titles": alt_titles if alt_titles else [translated_title],
        "thumbnail_text": thumbnail_text,
    }
    logger.info("[%s] clone_title_description: DONE — title='%s', %d tags, desc=%d chars",
                channel_slug, translated_title[:60], len(tags), len(translated_desc))
    return result


def _extract_tags_from_script(block_texts: list[str], config: SimpleNamespace) -> list[str]:
    """Extract SEO tags from script content using channel config defaults."""
    all_text = " ".join(block_texts)

    # Start with channel default tags
    default_tags = getattr(config, "YT_DEFAULT_TAGS", [])
    hashtags = getattr(config, "SEO_HASHTAGS", [])

    tags = list(default_tags[:8])  # Copy first 8 defaults
    tags = [t.strip("#") for t in tags]

    # Add viral-specific tag
    tags.append("video viral")

    # Limit to 15 tags
    return tags[:15]


def _generate_alt_titles(title: str, config: SimpleNamespace) -> list[str]:
    """Generate 2-3 alternative titles by paraphrasing the main title."""
    if not title or len(title) < 10:
        return []

    title_formulas = getattr(config, "TITLE_FORMULAS", [])
    power_words = getattr(config, "TITLE_POWER_WORDS", [])

    alt_titles = [title]

    # Simple synonym-based alternation
    if "más" in title.lower():
        alt_titles.append(title.replace("más", "MAS").replace("más", "más"))
    if "que no" in title.lower() or "que la" in title.lower():
        alt = title.replace("que no", "que la ciencia no").replace("que la", "que la medicina no")
        alt_titles.append(alt)

    # Add formula-based variation if configured
    if title_formulas and len(alt_titles) < 3:
        for formula in title_formulas[:2]:
            # Simple placeholder replacement
            variant = formula.replace("{keyword}", title.split()[-1] if title.split() else "")
            if variant and len(variant) > 10 and variant != title:
                alt_titles.append(variant)
                break

    return alt_titles[:3]


# ── Thumbnail Cloning ───────────────────────────────────────────────

def clone_thumbnail(
    original_thumbnail_url: str,
    channel_slug: str,
    channel_display_name: str = "",
    channel_description: str = "",
    channel_theme: str = "",
    script_text: str = "",
    keywords: list[str] | None = None,
    video_id: int = 0,
) -> str | None:
    """Clone a viral thumbnail with modifications via Vision AI + Pollo AI.

    Flow:
      1. Download original thumbnail
      2. Vision AI analyzes composition, objects, colors, text
      3. LLM modifies: different color palette (channel), translated text, 2-3 visual differences
      4. Pollo AI generates new image from modified prompt
      5. Apply channel style composition (F4 from thumbnail_maker)

    Returns:
        Path to the generated thumbnail, or None on failure.
    """
    channel_config = get_channel_config(channel_slug)
    canal_slug = channel_slug

    logger.info("[%s] clone_thumbnail: START — original_url=%s", canal_slug, original_thumbnail_url[:100])

    # Step 1: Download original thumbnail
    t1 = time.time()
    original_img_path = _download_thumbnail(original_thumbnail_url, canal_slug)
    if not original_img_path:
        logger.warning("[%s] clone_thumbnail: Step 1 FAILED — could not download thumbnail, skipping clone", canal_slug)
        return None
    logger.info("[%s] clone_thumbnail: Step 1 (download) done in %.1fs → %s",
                canal_slug, time.time() - t1, original_img_path)

    # Step 2: Vision AI analysis
    t2 = time.time()
    logger.info("[%s] clone_thumbnail: Step 2 — Vision AI analyzing thumbnail composition...", canal_slug)
    vision_analysis = _analyze_thumbnail_vision(original_img_path, channel_config)
    if not vision_analysis:
        logger.warning("[%s] clone_thumbnail: Step 2 WARNING — Vision AI analysis failed, using simplified prompt", canal_slug)
        vision_analysis = {"description": "a dramatic documentary-style YouTube thumbnail",
                           "objects": [], "colors": [], "composition": "", "text_overlay": ""}
    else:
        logger.info("[%s] clone_thumbnail: Step 2 (vision) done in %.1fs — objects=%s, colors=%s",
                    canal_slug, time.time() - t2,
                    vision_analysis.get("objects", [])[:3],
                    vision_analysis.get("colors", [])[:3])

    # Step 3: Modify prompt for channel style
    modified_prompt = _modify_thumbnail_prompt(vision_analysis, channel_config, keywords or [])
    logger.info("[%s] clone_thumbnail: Step 3 (modify prompt) — prompt=%s...", canal_slug, modified_prompt[:120])

    # Step 4: Generate with Pollo AI
    t4 = time.time()
    logger.info("[%s] clone_thumbnail: Step 4 — Pollo AI generating image...", canal_slug)
    from pipeline.ai_image_generator import AIImageGenerator
    generator = AIImageGenerator()
    pollo_result = generator.generate(modified_prompt, f"/tmp/viral_thumb_{canal_slug}_{int(time.time())}.jpg")
    if not pollo_result:
        logger.warning("[%s] clone_thumbnail: Step 4 FAILED — Pollo AI generation failed, falling back to normal pipeline", canal_slug)
        return None
    logger.info("[%s] clone_thumbnail: Step 4 (Pollo) done in %.1fs → %s", canal_slug, time.time() - t4, pollo_result)

    # Step 5: Apply channel composition (F4 from thumbnail_maker)
    t5 = time.time()
    logger.info("[%s] clone_thumbnail: Step 5 — Applying channel composition (F4)...", canal_slug)
        base_image_path=str(pollo_result),
        channel_slug=canal_slug,
        channel_display_name=channel_display_name,
        channel_description=channel_description,
        channel_theme=channel_theme,
        script_text=script_text,
        keywords=keywords or [],
        video_id=video_id,
    )

    logger.info("[%s] clone_thumbnail: Step 5 (composition) done in %.1fs", canal_slug, time.time() - t5)
    logger.info("[%s] clone_thumbnail: DONE → %s", canal_slug, final_thumb)
    return final_thumb


def _download_thumbnail(url: str, canal_slug: str) -> str | None:
    """Download a thumbnail image from URL."""
    if not url:
        return None

    thumb_dir = _THUMB_DIR / canal_slug
    thumb_dir.mkdir(parents=True, exist_ok=True)

    # Use a unique filename based on URL hash
    import hashlib
    url_hash = hashlib.md5(url.encode()).hexdigest()[:12]
    output_path = thumb_dir / f"viral_original_{url_hash}.jpg"

    if output_path.exists():
        logger.info("[%s] Original thumbnail already downloaded: %s", canal_slug, output_path)
        return str(output_path)

    try:
        resp = requests.get(url, timeout=30, headers={
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
        })
        if resp.status_code == 200 and len(resp.content) > 1000:
            output_path.write_bytes(resp.content)
            logger.info("[%s] Thumbnail downloaded: %s (%d KB)", canal_slug, output_path.name, len(resp.content) // 1024)
            return str(output_path)
    except Exception as e:
        logger.error("[%s] Thumbnail download failed: %s", canal_slug, e)

    return None


def _analyze_thumbnail_vision(image_path: str, config: SimpleNamespace) -> dict | None:
    """Use vision-capable LLM to analyze thumbnail composition."""
    client, model = _get_llm_client(config)
    if not client:
        logger.warning("No LLM client for vision analysis")
        return None

    # Check if the model supports vision (try to use it)
    # Read image as base64
    import base64
    try:
        with open(image_path, "rb") as f:
            img_b64 = base64.b64encode(f.read()).decode()
    except Exception:
        return None

    # Try vision-capable call
    system = """You are a thumbnail design analyst. Analyze the YouTube thumbnail image.
Return a JSON object with:
{
  "description": "Detailed visual description in English (for image generation)",
  "objects": ["list", "of", "visible", "objects"],
  "colors": ["dominant", "color", "palette"],
  "composition": "description of layout and spatial arrangement",
  "text_overlay": "any visible text and its style (font, color, position)",
  "emotion": "emotional vibe of the thumbnail",
  "style": "overall style category"
}"""

    try:
        resp = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "Analyze this YouTube thumbnail in detail:"},
                        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{img_b64}"}},
                    ],
                },
            ],
            temperature=0.3,
            max_tokens=1500,
            response_format={"type": "json_object"},
        )
        content = resp.choices[0].message.content
        return json.loads(content) if content else None
    except Exception as e:
        logger.warning("Vision AI call failed (model may not support vision): %s", e)
        # Fallback: text-only description
        fallback = _call_llm_json(
            config,
            system,
            "Describe a YouTube thumbnail that would be highly clickable and viral. "
            "Return the same JSON format with a hypothetical description.",
            temp=0.7,
        )
        return fallback


def _modify_thumbnail_prompt(vision_analysis: dict, config: SimpleNamespace, keywords: list[str]) -> str:
    """Take vision analysis and modify for channel style + slight differences."""
    channel_name = getattr(config, "CANAL_DISPLAY_NAME", "")
    channel_tagline = getattr(config, "CANAL_TAGLINE", "")
    visual_style = getattr(config, "THUMBNAIL_VISUAL_STYLE", "realistic_documentary")
    color_palette = getattr(config, "COLOR_PALETTE", {})

    # Build style suffix from channel config
    style_suffix_map = {
        "dark_cinematic": "cinematic lighting, deep shadows, film grain, high contrast, dramatic atmosphere",
        "vintage_archive": "aged photograph look, sepia tones, archival texture, vintage feel",
        "realistic_documentary": "documentary style, natural lighting, realistic, photojournalism quality",
        "institutional_cold": "clinical white lighting, sterile environment, medical precision",
        "dramatic_contrast": "extreme contrast, dramatic shadows, spotlight lighting",
        "moody_atmospheric": "atmospheric fog, moody lighting, mysterious ambiance, desaturated colors",
        "minimalist_clean": "clean composition, minimal elements, professional presentation",
        "vibrant_educational": "bright saturated colors, educational diagram style, clear visuals",
        "shock_documentary": "raw documentary style, unsettling realism, gritty texture",
        "distress_signal": "distress visual language, emergency tones, urgency feeling",
    }
    style_suffix = style_suffix_map.get(visual_style, "realistic documentary style")

    # Extract key elements from vision analysis
    objects = vision_analysis.get("objects", [])
    main_objects = ", ".join(objects[:4]) if objects else "dramatic scene"
    composition = vision_analysis.get("composition", "")
    description = vision_analysis.get("description", "")

    # Build modified prompt for Pollo AI
    prompt = (
        f"A YouTube thumbnail for the channel '{channel_name}'. "
        f"Style: {style_suffix}. "
        f"Main elements: {main_objects}. "
        f"Composition: {composition[:200]}. "
        f"Visual reference: {description[:300]}. "
        f"Channel theme: {channel_tagline[:100]}. "
        f"IMPORTANT: make it look DIFFERENT from the reference — change the color palette, "
        f"shift the layout slightly, use different background elements. "
        f"High quality 16:9 ratio, no text overlay on the image itself."
    )

    # Replace colors with channel palette if defined
    if color_palette:
        palette_desc = ", ".join(f"{k}: {v}" for k, v in list(color_palette.items())[:3])
        prompt += f" Use this color scheme: {palette_desc}."

    return prompt


def _apply_channel_composition(
    base_image_path: str,
    channel_slug: str,
    channel_display_name: str = "",
    channel_description: str = "",
    channel_theme: str = "",
    script_text: str = "",
    keywords: list[str] | None = None,
    video_id: int = 0,
) -> str | None:
    """Apply F4 composition (channel-specific overlays) to the generated image.

    Reuses the existing thumbnail_maker._compose_final() logic.
    """
    try:
        from pipeline.thumbnail_maker import ThumbnailMaker
        from types import SimpleNamespace

        # Create a minimal config for ThumbnailMaker
        maker_config = SimpleNamespace(
            THUMBNAIL_WIDTH=1280,
            THUMBNAIL_HEIGHT=720,
            THUMBNAIL_FONT_SIZE=56,
            THUMBNAIL_BORDER_WIDTH=5,
            THUMBNAIL_RESCUE_MAYDAY=True,
            THUMBNAIL_MEDICAL_ECG=True,
        )

        maker = ThumbnailMaker(config=maker_config)
        keywords = keywords or []

        result = maker.make_viral_thumbnail(
            title=channel_theme[:80] if channel_theme else channel_display_name,
            overlay_text="",
            keywords=keywords,
            scene_images=None,
            script_text=script_text[:1500],
            canal_slug=channel_slug,
            channel_display_name=channel_display_name,
            channel_description=channel_description,
            channel_theme=channel_theme,
            base_image_path=Path(base_image_path) if base_image_path else None,
            video_id=video_id,
        )

        return str(result) if result else None
    except Exception as e:
        logger.error("[%s] Channel composition failed: %s — returning base image", channel_slug, e)
        return base_image_path


# ── Build viral metadata ────────────────────────────────────────────

def build_viral_metadata(
    viral_meta_json: str,
    channel_slug: str,
) -> dict:
    """Build the complete metadata dict for the viral pipeline branch.

    This replaces phase_metadata's AI generation with pre-cloned metadata.

    Returns:
        metadata dict: {titles, selected_title, description, tags, thumbnail_text}
    """
    return clone_title_description(viral_meta_json, channel_slug)
