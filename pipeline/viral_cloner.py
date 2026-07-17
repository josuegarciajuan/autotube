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
    """Get an OpenAI-compatible client from config settings (text-only LLM)."""
    from config.settings import (
        LLM_API_KEY, LLM_BASE_URL, LLM_MODEL,
        OPENAI_API_KEY, OPENAI_MODEL,
    )
    from openai import OpenAI

    api_key = LLM_API_KEY or OPENAI_API_KEY
    base_url = LLM_BASE_URL
    model = LLM_MODEL or OPENAI_MODEL

    if api_key:
        return OpenAI(api_key=api_key, base_url=base_url), model
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

# Patterns that indicate a host/narrator/channel name in a title
# These are removed because they reference the original creator, not subject matter
_HOST_NAME_PATTERNS = [
    # "with Name" / "con Nombre" patterns
    (r'\|\s*[Ss]ecretos al descubierto con\s+[A-ZÁÉÍÓÚÑ][a-záéíóúñ]+\s*[A-ZÁÉÍÓÚÑ][a-záéíóúñ]+(?:\s*[A-ZÁÉÍÓÚÑ][a-záéíóúñ]+)?', ''),
    (r'\|\s*[A-Za-z\s]+ with\s+[A-Z][a-z]+\s*[A-Z][a-z]+(?:\s*[A-Z][a-z]+)?', ''),
    (r'with\s+[A-Z][a-z]+\s*[A-Z][a-z]+(?:\s*[A-Z][a-z]+)?\s*$', ''),
    (r'con\s+[A-ZÁÉÍÓÚÑ][a-záéíóúñ]+\s*[A-ZÁÉÍÓÚÑ][a-záéíóúñ]+(?:\s*[A-ZÁÉÍÓÚÑ][a-záéíóúñ]+)?\s*$', ''),
    # "| Show Name" patterns (remove network/channel show names after pipe)
    (r'\|\s*(?:Mysteries\s*Unearthed|Unsolved\s*Mysteries|History\s*Channel\s*Presents|Discovery\s*Channel|National\s*Geographic)\s*.*$', ''),
    # Clean up dangling pipes
    (r'\s*\|\s*$', ''),
    # Clean up double pipes
    (r'\|\s*\|', '|'),
]


def _strip_host_names(title: str, original_channel: str = "") -> str:
    """Remove host/narrator/creator names from a viral title.
    
    The original video's host or channel is NOT part of our content —
    we only want the subject matter (the mystery, event, or topic).
    
    Also strips show names from networks/channels.
    """
    import re
    
    original = title
    cleaned = title.strip()
    
    # Apply regex patterns for common host-name constructions
    for pattern, replacement in _HOST_NAME_PATTERNS:
        cleaned = re.sub(pattern, replacement, cleaned)
    
    # If we know the original channel name, remove it from title
    if original_channel and original_channel.lower() in cleaned.lower():
        # Remove channel name patterns like "| OriginalChannel", "- OriginalChannel", "by OriginalChannel"
        cleaned = re.sub(
            rf'[\|\-–—]\s*{re.escape(original_channel)}.*$',
            '',
            cleaned,
            flags=re.IGNORECASE,
        )
        cleaned = re.sub(
            rf'by\s+{re.escape(original_channel)}\s*$',
            '',
            cleaned,
            flags=re.IGNORECASE,
        )
    
    # Clean up any remaining artifacts
    cleaned = re.sub(r'\s*\|\s*$', '', cleaned)  # trailing pipe
    cleaned = re.sub(r'\s*-\s*$', '', cleaned)   # trailing dash
    cleaned = re.sub(r'\s{2,}', ' ', cleaned)     # double spaces
    cleaned = cleaned.strip()
    
    # Ensure the title is not completely stripped
    if not cleaned or len(cleaned) < 10:
        logger.warning("_strip_host_names: title stripped too aggressively, keeping original: '%s'", original)
        return original
    
    if cleaned != original:
        logger.info("_strip_host_names: stripped host name — '%s' → '%s'", original[:80], cleaned[:80])
    
    return cleaned


# ── Duration claim patterns ──────────────────────────────────────
# These are time claims that appear in viral video titles.
# Our generated videos are 8-20 min — claiming hours of content
# hurts viewer trust and CTR. Strip or replace these claims.
_DURATION_CLAIM_PATTERNS = [
    # English patterns
    (r'\d+\+?\s*HOURS?\s*(?:OF|of)\s+', '', re.IGNORECASE),    # "4+ HOURS of "
    (r'\d+\+?\s*[Hh]ours?\s*(?:of|of)\s+', '', 0),               # "4+ hours of "
    (r'\d+\s*[Hh][rR]\s*(?:of|of)\s+', '', 0),                   # "4hr of "
    # Spanish patterns
    (r'\d+\+?\s*HORAS?\s*(?:DE|de)\s+', '', 0),                  # "4+ HORAS de "
    (r'\d+\+?\s*[Hh]oras?\s*(?:DE|de)\s+', '', 0),               # "4+ horas de "
    (r'\d+\s*[Hh]\s*(?:DE|de)\s+', '', 0),                       # "4h de "
    # More Spanish variants
    (r'\d+[+\s]*[Hh]oras?\s*(?:y\s+media)?\s*$', '', 0),        # "2 horas" at end
    (r'\d+[+\s]*HORAS?\s*(?:y\s+media)?\s*$', '', 0),            # "2 HORAS" at end
]

_DURATION_CLAIM_MID_TITLE = [
    # " - 4+ hours of content" type patterns
    (r'\s*[-–—]\s*\d+\+?\s*[Hh]ours?\s*(?:of|de|del?)?\s*.*$', '', re.IGNORECASE),
    (r'\s*[-–—]\s*\d+\+?\s*HORAS?\s*(?:of|de|del?)?\s*.*$', '', 0),
    (r'\s*\|\s*\d+\+?\s*[Hh]ours?\s*(?:of|de|del?)?\s*.*$', '', re.IGNORECASE),
    (r'\s*\|\s*\d+\+?\s*HORAS?\s*(?:of|de|del?)?\s*.*$', '', 0),
]

# Replacement words when a duration claim is removed and the title
# becomes too bare (e.g., just a topic noun).
_DURATION_REPLACEMENTS = [
    "Documental de ",
    "Historia completa de ",
    "Todo sobre ",
    "Lo que debes saber de ",
]


def _correct_duration_claims(title: str) -> str:
    """Remove or replace duration claims in cloned video titles.

    Original viral videos often claim "4+ HOURS of content" but our
    generated videos are 8-20 minutes. Stripping these false claims
    prevents viewer disappointment and improves CTR accuracy.
    """
    import re

    original = title
    cleaned = title.strip()

    # Step 1: Apply mid-title duration patterns (remove suffix after separator)
    for pattern, replacement, flags in _DURATION_CLAIM_MID_TITLE:
        cleaned = re.sub(pattern, replacement, cleaned, flags=flags)

    # Step 2: Apply duration claim patterns (remove duration prefix)
    for pattern, replacement, flags in _DURATION_CLAIM_PATTERNS:
        cleaned = re.sub(pattern, replacement, cleaned, flags=flags)

    # Step 3: If we removed a duration prefix and the title is now too short,
    # prepend a natural replacement phrase
    if cleaned != original and len(cleaned) < 20:
        # Pick a replacement that fits the channel tone
        cleaned = _DURATION_REPLACEMENTS[0] + cleaned[0].upper() + cleaned[1:]

    # Step 4: Clean up artifacts
    cleaned = re.sub(r'\s{2,}', ' ', cleaned)     # double spaces
    cleaned = cleaned.strip()

    if cleaned != original and len(cleaned) > 0:
        logger.info(
            "_correct_duration_claims: '%s...' → '%s...'",
            original[:60], cleaned[:60],
        )

    return cleaned or original  # never return empty


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
    original_channel = viral_meta.get("original_channel", "")
    block_texts = [b.get("text", "") for b in viral_meta.get("blocks", [])]

    # ── Strip duration claims from title ─────────────────────
    # Original viral videos are often 1-4h long, but our generated
    # videos are 8-20min. A title claiming "4+ HORAS" is false advertising
    # and hurts viewer trust + CTR. Strip or replace duration claims.
    if translated_title:
        translated_title = _correct_duration_claims(translated_title)

    # ── Strip host/creator names from title ──────────────────
    if translated_title:
        translated_title = _strip_host_names(translated_title, original_channel)

    # If no translated title, try to rebuild from the script text
    if not translated_title and block_texts:
        # Use first block's first sentence as title fallback
        first_sentence = block_texts[0].split(".")[0].strip()
        if len(first_sentence) > 20:
            translated_title = first_sentence

    # ── Fix description: detect and replace English fragments ──
    # The scraper may fall back to the original English description.
    # We detect English content and rebuild from translated script blocks.
    _ENGLISH_MARKERS = [
        "from ", "into ", "through ", "with ", "the ", "and ", "that ",
        "this ", "have ", "been ", "will ", "would ", "could ", "should ",
        "discover", "uncover", "reveal", "ancient", "history",
    ]
    desc_is_english = (
        translated_desc
        and any(
            translated_desc.lower().startswith(m) or m in translated_desc.lower()[:200]
            for m in _ENGLISH_MARKERS[:5]
        )
    )

    if (not translated_desc or desc_is_english) and block_texts:
        if desc_is_english:
            logger.warning("[%s] clone_title_description: description appears to be in English — rebuilding from script blocks", channel_slug)
        desc_lines = []
        for block in viral_meta.get("blocks", []):
            text = block.get("text", "") if isinstance(block, dict) else block
            if text and len(text) > 30:
                # Take first 2 sentences of each block
                sentences = text.split(".")
                desc_lines.append(". ".join(sentences[:2]) + ".")
                if len(desc_lines) >= 4:
                    break
        if desc_lines:
            translated_desc = "\n\n".join(desc_lines)
            logger.info("[%s] Rebuilt description from %d blocks (%d chars)", channel_slug, len(desc_lines), len(translated_desc))

    # Generate tags from keyword extraction
    tags = _extract_tags_from_script(block_texts, channel_config)

    # Build thumbnail overlay text (short punchy phrase from title, without names)
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
    """Extract SEO tags using channel config defaults plus viral marker."""
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

    # Variant 1: emphasis on 'más' → replace with uppercase
    if "más" in title.lower() and len(alt_titles) < 3:
        emphatic = title.replace("más", "MÁS")
        if emphatic != title:
            alt_titles.append(emphatic)

    # Variant 2: common viral pattern — "lo que no sabías sobre..."
    if len(alt_titles) < 3:
        # Extract a subject from the end of the title
        words = title.split()
        subject = " ".join(words[-3:]) if len(words) >= 3 else title
        alt_titles.append(f"Lo que no sabías sobre {subject.lower()}")

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

    # Save raw base (pre-F4) copy for later recomposition with overlay text.
    # phase_metadata() needs the raw Pollo image to re-compose F4 with the
    # final SEO text overlay, without regenerating via Pollo AI.
    raw_base_path = _THUMB_DIR / canal_slug / f"viral_raw_{video_id}.jpg" if video_id else None
    if raw_base_path:
        import shutil
        shutil.copy2(pollo_result, raw_base_path)
        logger.info("[%s] clone_thumbnail: Raw base saved → %s", canal_slug, raw_base_path)

    # Step 5: Apply channel composition (F4 from thumbnail_maker)
    t5 = time.time()
    logger.info("[%s] clone_thumbnail: Step 5 — Applying channel composition (F4)...", canal_slug)
    final_thumb = _apply_channel_composition(
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
    """Use vision-capable LLM (gpt-4o-mini) to analyze thumbnail composition."""
    from config.settings import VISION_MODEL, VISION_API_KEY, VISION_BASE_URL
    from openai import OpenAI
    import base64

    if not VISION_API_KEY:
        logger.warning("No VISION_API_KEY configured — cannot analyze thumbnail")
        return None

    # Read image as base64
    try:
        with open(image_path, "rb") as f:
            img_b64 = base64.b64encode(f.read()).decode()
    except Exception as e:
        logger.warning("Failed to read image for vision analysis: %s", e)
        return None

    # Vision-capable model (OpenAI gpt-4o-mini supports multimodal)
    vision_client = OpenAI(api_key=VISION_API_KEY, base_url=VISION_BASE_URL)
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
        resp = vision_client.chat.completions.create(
            model=VISION_MODEL,
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
        logger.warning("Vision AI call failed: %s", e)
        return None


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
    Loads the actual channel config so overlays match the channel style.
    """
    try:
        from pipeline.thumbnail_maker import ThumbnailMaker
        from config.config_bridge import get_channel_config

        # Load REAL channel config so overlays match the channel style.
        # Previously hardcoded RESCUE_MAYDAY=True and MEDICAL_ECG=True
        # which added irrelevant banners to non-rescue/non-medical channels.
        channel_config = get_channel_config(channel_slug)
        
        # Build a config namespace that preserves the channel's overlay settings
        # while adding any required defaults
        from types import SimpleNamespace
        maker_config = SimpleNamespace(
            THUMBNAIL_WIDTH=getattr(channel_config, "THUMBNAIL_WIDTH", 1280),
            THUMBNAIL_HEIGHT=getattr(channel_config, "THUMBNAIL_HEIGHT", 720),
            THUMBNAIL_FONT_SIZE=getattr(channel_config, "THUMBNAIL_FONT_SIZE", 56),
            THUMBNAIL_BORDER_WIDTH=getattr(channel_config, "THUMBNAIL_BORDER_WIDTH", 5),
            THUMBNAIL_FONT_FAMILY=getattr(channel_config, "THUMBNAIL_FONT_FAMILY", "DejaVuSans-Bold"),
            THUMBNAIL_BORDER_COLOR=getattr(channel_config, "THUMBNAIL_BORDER_COLOR", "#CC0000"),
            THUMBNAIL_SHOW_4K_BADGE=getattr(channel_config, "THUMBNAIL_SHOW_4K_BADGE", True),
            THUMBNAIL_TEXT_STROKE_WIDTH=getattr(channel_config, "THUMBNAIL_TEXT_STROKE_WIDTH", 0),
            THUMBNAIL_TEXT_STROKE_COLOR=getattr(channel_config, "THUMBNAIL_TEXT_STROKE_COLOR", "#000000"),
            THUMBNAIL_VISUAL_STYLE=getattr(channel_config, "THUMBNAIL_VISUAL_STYLE", "dark_cinematic"),
            THUMBNAIL_MANUAL_STYLE=getattr(channel_config, "THUMBNAIL_MANUAL_STYLE", None),
            COLOR_PALETTE=getattr(channel_config, "COLOR_PALETTE", {}),
            CANAL_DISPLAY_NAME=getattr(channel_config, "CANAL_DISPLAY_NAME", ""),
            THUMBNAILS_DIR=getattr(channel_config, "THUMBNAILS_DIR", "output/thumbnails"),
            # ── Overlays: use channel config, NEVER hardcode to True ──
            THUMBNAIL_RESCUE_MAYDAY=getattr(channel_config, "THUMBNAIL_RESCUE_MAYDAY", False),
            THUMBNAIL_RESCUE_COORDINATES=getattr(channel_config, "THUMBNAIL_RESCUE_COORDINATES", False),
            THUMBNAIL_RESCUE_SIN_SENAL=getattr(channel_config, "THUMBNAIL_RESCUE_SIN_SENAL", False),
            THUMBNAIL_MEDICAL_ECG=getattr(channel_config, "THUMBNAIL_MEDICAL_ECG", False),
            THUMBNAIL_MEDICAL_CROSS=getattr(channel_config, "THUMBNAIL_MEDICAL_CROSS", False),
            THUMBNAIL_MEDICAL_DIAGNOSIS=getattr(channel_config, "THUMBNAIL_MEDICAL_DIAGNOSIS", False),
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
