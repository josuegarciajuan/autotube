"""Portrait image fetching, video clip sourcing, and FFmpeg hybrid rendering for Shorts.

v2 rewrite (2026-07-28): exhaustive per-block asset search with cross-short
dedup, video/image mix strategy (~55% video), Ken Burns zoompan on still
images, and xfade transitions between mixed video and image scenes.

Backward compatibility:
  - fetch_portrait_images() is kept and marked deprecated.
  - render_slideshow_with_images() is kept for legacy slideshow mode.

Provides shared functions used by the scheduler (planning_service), the API
endpoint (api/routers/shorts.py), and the standalone NativeShortsPipeline.
"""

import hashlib
import logging
import re
import subprocess
from pathlib import Path
from typing import Any

import requests

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════════════════
# Module-level constants
# ═══════════════════════════════════════════════════════════════════════════

# Style words to strip from raw queries (same approach as media_fetcher._STYLE_WORDS)
_STYLE_WORDS: set[str] = {
    "cinematic", "photography", "dramatic", "lighting", "atmospheric",
    "portrait", "moody", "high", "contrast", "professional", "dark",
    "atmosphere", "style", "film", "stock", "vertical",
    "composition", "documentary", "historical", "depth", "field",
    "color", "grading", "grade", "beautiful", "amazing", "stunning",
}

# Fallback queries by block type when LLM search_query_en yields nothing
_FALLBACK_BY_TYPE: dict[str, str] = {
    "hook": "dramatic mystery intrigue cinematic",
    "desarrollo1": "documentary detail historical context",
    "desarrollo2": "documentary evidence discovery exploration",
    "desarrollo3": "documentary detail analysis investigation",
    "climax": "dramatic intense revelation climax",
    "cierre": "resolution conclusion hopeful cinematic",
}

# Cross-provider dedup state — reset per short in fetch_short_assets_exhaustive()
_DEDUP_STATE: dict[str, Any] = {
    "used_urls": set(),
    "used_img_ids": set(),         # "{provider}:{img_id}" strings
    "used_filenames": set(),
    "used_content_hashes": set(),
    "bad_urls": set(),             # URLs that returned non-image/video responses
    "cross_short_filenames": set(), # loaded from DB once per fetch
}


# ═══════════════════════════════════════════════════════════════════════════
# Module-level provider caches (singleton per process lifetime)
# ═══════════════════════════════════════════════════════════════════════════

_providers_cache: dict[str, Any] = {
    "unsplash": None,
    "pixabay": None,
    "pexels_video": None,
    "pixabay_video": None,
}


# ═══════════════════════════════════════════════════════════════════════════
# Provider accessors (deferred import to avoid circular deps at load time)
# ═══════════════════════════════════════════════════════════════════════════

def _get_image_providers():
    """Return (unsplash, pixabay) image provider instances, creating them once."""
    from config import settings

    unsplash = _providers_cache["unsplash"]
    pixabay = _providers_cache["pixabay"]

    if unsplash is None and settings.UNSPLASH_ACCESS_KEY:
        from pipeline.image_fetcher import UnsplashProvider
        unsplash = UnsplashProvider(settings.UNSPLASH_ACCESS_KEY)
        _providers_cache["unsplash"] = unsplash

    if pixabay is None and settings.PIXABAY_API_KEY:
        from pipeline.image_fetcher import PixabayImageProvider
        pixabay = PixabayImageProvider(settings.PIXABAY_API_KEY)
        _providers_cache["pixabay"] = pixabay

    return unsplash, pixabay


def _get_portrait_video_providers() -> list[Any]:
    """Return list of configured video providers, creating them once.

    Providers are returned in priority order: Pexels first, then Pixabay.
    Each implements BaseVideoProvider with search_page() and download().
    """
    from config import settings

    providers: list[Any] = []

    pexels = _providers_cache["pexels_video"]
    pixabay_vid = _providers_cache["pixabay_video"]

    if pexels is None and settings.PEXELS_API_KEY:
        from pipeline.providers.pexels import PexelsVideoProvider
        pexels = PexelsVideoProvider(settings.PEXELS_API_KEY)
        _providers_cache["pexels_video"] = pexels

    if pixabay_vid is None and settings.PIXABAY_API_KEY:
        from pipeline.providers.pixabay import PixabayVideoProvider
        pixabay_vid = PixabayVideoProvider(settings.PIXABAY_API_KEY)
        _providers_cache["pixabay_video"] = pixabay_vid

    if pexels:
        providers.append(pexels)
    if pixabay_vid:
        providers.append(pixabay_vid)

    return providers


# ═══════════════════════════════════════════════════════════════════════════
# FFmpeg escape helper
# ═══════════════════════════════════════════════════════════════════════════

def _esc_ffmpeg(t: str) -> str:
    """Escape single-quotes / colons / percent for FFmpeg drawtext."""
    return (
        t.replace("'", "'\\\\\\''")
        .replace(":", "\\\\:")
        .replace("%", "\\\\%")
    )


# ═══════════════════════════════════════════════════════════════════════════
# Query builder (v7 — narrative-first fusion, kept from v1)
# ═══════════════════════════════════════════════════════════════════════════

def _build_portrait_query(
    search_query_en: str,
    theme_keywords: list[str] | None = None,
    style_modifiers: str = "",
    theme_ctx = None,  # v8: full ThemeContext for richer anchoring
    max_len: int = 100,
    scene_text: str = "",
) -> str:
    """Build a short-focused search query fusing scene narrative with theme context.

    Strategy (v8 — full ThemeContext fusion):
    1. Scene narrative keywords from ``search_query_en`` — primary subject (~60%)
    2. Context anchors from ThemeContext (primary_subject / era_decade / genre) (~15%)
    3. Theme keywords as era/style anchors (~15%)
    4. Channel style modifiers for aesthetic consistency (~10%)
    5. Fit within ``max_len`` chars (Pixabay limit: 100).
    6. Filter out forbidden elements from the final query.

    This mirrors ``MediaFetcher._build_search_query()`` adapted for portrait shorts.
    """
    # Ground literal labels in observable actions/objects while retaining the
    # original query for provider relevance scoring.
    from pipeline.cinematic_staging import enrich_scene_query, sanitize_person_query
    search_query_en = enrich_scene_query(search_query_en, theme_ctx, scene_text)
    search_query_en = sanitize_person_query(search_query_en)

    # 1. Strip style fluff from the LLM query
    words = search_query_en.split()
    scene_keywords = [w for w in words if w.lower() not in _STYLE_WORDS]
    if not scene_keywords:
        scene_keywords = [w for w in words]

    if not scene_keywords and not theme_keywords and not (theme_ctx and theme_ctx.primary_subject):
        return search_query_en.strip()[:max_len]

    # 2. Gather contextual anchors from ThemeContext (v8)
    ctx_anchors: list[str] = []
    if theme_ctx:
        if theme_ctx.primary_subject:
            ps_words = [w for w in theme_ctx.primary_subject.split()
                       if w.lower() not in _STYLE_WORDS]
            ctx_anchors.extend(ps_words[:3])
        if theme_ctx.era_decade and theme_ctx.era_decade.lower() not in ("atemporal", "presente", ""):
            ctx_anchors.append(theme_ctx.era_decade)
        elif theme_ctx.era and theme_ctx.era.lower() not in ("atemporal", "presente", ""):
            era_clean = theme_ctx.era.replace("_", " ").strip()
            if era_clean and len(era_clean) <= 15:
                ctx_anchors.extend(era_clean.split()[:2])
        if theme_ctx.genre and theme_ctx.genre.lower() not in ("documental", "documentary", ""):
            genre_clean = theme_ctx.genre.replace("_", " ").strip()
            ctx_anchors.extend(genre_clean.split()[:2])
    ctx_anchors = list(dict.fromkeys(ctx_anchors))

    # 3. Allocate character budget
    style_budget = min(len(style_modifiers) + 1, 12) if style_modifiers else 0
    theme_budget = min(15, max_len - style_budget)
    ctx_budget = min(15, max_len - style_budget - theme_budget)
    scene_budget = max_len - style_budget - ctx_budget - theme_budget

    # 4. Build scene narrative part (primary subject)
    scene_part = ""
    for w in scene_keywords:
        candidate = f"{scene_part} {w}".strip()
        if len(candidate) <= scene_budget:
            scene_part = candidate
        else:
            break
    if not scene_part and scene_keywords:
        scene_part = scene_keywords[0][:scene_budget]
    elif not scene_part and (ctx_anchors or theme_keywords):
        remaining = max_len - style_budget
        all_anchors = ctx_anchors[:2] + (theme_keywords or [])
        for kw in all_anchors[:3]:
            candidate = f"{scene_part} {kw}".strip()
            if len(candidate) <= max(remaining, 10):
                scene_part = candidate
            else:
                break
        ctx_anchors = []
        theme_keywords = None

    # 5. Build ctx_anchor part (primary_subject/era/genre — max 2 keywords, dedup)
    ctx_part = ""
    if ctx_anchors:
        scene_lower = scene_part.lower()
        fresh_anchors = [a for a in ctx_anchors[:2] if a.lower() not in scene_lower]
        remaining = max_len - len(scene_part) - style_budget
        for kw in fresh_anchors:
            candidate = f"{ctx_part} {kw}".strip()
            if len(candidate) <= max(remaining, 8):
                ctx_part = candidate
            else:
                break

    # 6. Build theme context part (max 2 keywords, dedup with scene AND ctx)
    theme_part = ""
    if theme_keywords:
        scene_and_ctx = (scene_part + " " + ctx_part).lower()
        fresh_keywords = [
            kw for kw in theme_keywords[:2]
            if kw.lower() not in scene_and_ctx
        ]
        remaining = max_len - len(scene_part) - len(ctx_part) - style_budget
        for kw in fresh_keywords:
            candidate = f"{theme_part} {kw}".strip()
            if len(candidate) <= max(remaining, 10):
                theme_part = candidate
            else:
                break

    # 7. Add style modifiers
    style_part = style_modifiers if style_modifiers else ""

    # 8. Assemble: scene (primary) + ctx_anchor + style (diversity) + theme (anchor)
    parts = [scene_part]
    if ctx_part:
        parts.append(ctx_part)
    if style_part:
        parts.append(style_part)
    if theme_part:
        parts.append(theme_part)

    result = " ".join(parts)

    # 9. Final safety: truncate at last complete word
    if len(result) > max_len:
        result = result[:max_len].rsplit(" ", 1)[0]

    # 10. Forbidden elements safety net (v8)
    if theme_ctx and theme_ctx.forbidden_elements:
        result_lower = result.lower()
        for forbidden in theme_ctx.forbidden_elements:
            fb_lower = forbidden.lower().strip()
            if fb_lower and fb_lower in result_lower:
                import re as _re
                result = _re.sub(r'\b' + _re.escape(forbidden) + r'\b', '', result, flags=_re.IGNORECASE)
                result = _re.sub(r'\s{2,}', ' ', result).strip()
                if not result or len(result) < 5:
                    result = search_query_en.strip()[:max_len]

    return result


# ═══════════════════════════════════════════════════════════════════════════
# v2: Query pool builder
# ═══════════════════════════════════════════════════════════════════════════

def _extract_narrative_keywords_short(query: str, theme_ctx) -> str:
    """Extract only the narrative keywords from a search query for shorts.

    Strips theme keywords (from ThemeContext.theme_keywords_en, key_motifs,
    and derived words) while preserving the narrative subject keywords
    that describe what is actually being narrated in this specific block.

    Then appends exactly ONE theme anchoring keyword to maintain visual
    context without letting the theme dominate the query.

    Returns a narrative-heavy query string, or empty string if no narrative
    keywords remain after stripping.
    """
    if not query:
        return ""

    # Gather all theme-related words to strip
    theme_words: set[str] = set()
    if theme_ctx:
        if theme_ctx.theme_keywords_en:
            for kw in theme_ctx.theme_keywords_en:
                for w in kw.lower().split():
                    theme_words.add(w)
        if theme_ctx.key_motifs:
            for motif in theme_ctx.key_motifs:
                for w in motif.lower().split():
                    theme_words.add(w)
        if theme_ctx.primary_subject:
            for w in theme_ctx.primary_subject.lower().split():
                theme_words.add(w)
        if theme_ctx.genre and theme_ctx.genre != "documental":
            for w in theme_ctx.genre.lower().replace("_", " ").split():
                theme_words.add(w)
        if theme_ctx.era_decade and theme_ctx.era_decade not in ("atemporal", "presente", ""):
            theme_words.add(theme_ctx.era_decade.lower())
        elif theme_ctx.era and theme_ctx.era not in ("atemporal", "presente", ""):
            for w in theme_ctx.era.lower().replace("_", " ").split():
                theme_words.add(w)

    # Separate narrative words from theme/style words
    words = query.split()
    narrative_words = []
    theme_words_found = []

    for w in words:
        wl = w.lower().strip(",.!?;:")
        if wl in _STYLE_WORDS:
            continue
        if wl in theme_words:
            theme_words_found.append(w)
        else:
            narrative_words.append(w)

    if not narrative_words:
        return ""  # query was entirely thematic

    # Build: narrative keywords + at most 1 theme anchoring word
    result = " ".join(narrative_words[:6])

    if theme_words_found:
        anchor = theme_words_found[0]
        if len(result) + len(anchor) + 1 <= 100:
            result = f"{result} {anchor}"

    return result[:100]

def _build_query_pool(
    block: dict[str, Any],
    theme_keywords: list[str] | None = None,
    style_modifiers: str = "",
    theme_ctx = None,  # v8: full ThemeContext for richer anchoring
    fallback_queries: list[str] | None = None,
) -> list[str]:
    """Build ~7-9 ordered query variations for exhaustive search per block.

    Order (narrative priority): narrative-first queries try first,
    theme-anchored queries are fallbacks. This ensures that what you
    SEE matches what you HEAR in shorts, while maintaining visual context.

    Returns a deduplicated ordered list of non-empty queries, each ≤100 chars.
    """
    import re
    from config.settings import SHORTS_FALLBACK_QUERIES

    search_en = block.get("search_query_en", "")
    block_type = block.get("tipo", "desarrollo")

    pool: list[str] = []

    # 1. Primary query: scene + theme context + style (v8)
    #    With the improved LLM prompt, this should already be ~60-70% narrative.
    if search_en and search_en.strip():
        primary = _build_portrait_query(
            search_en, theme_keywords, style_modifiers, theme_ctx=theme_ctx,
            scene_text=block.get("texto", ""),
        )
        if primary.strip():
            pool.append(primary.strip()[:100])

    # 2. Narrative-heavy variant: strip most theme words, keep 1 anchor.
    #    Prioritizes narrative content with minimal theme anchoring.
    #    Comes BEFORE directional variations so narrative specificity wins.
    if search_en and search_en.strip():
        narrative_heavy = _extract_narrative_keywords_short(search_en, theme_ctx)
        if narrative_heavy and narrative_heavy.strip():
            # Also check it's different from the primary query
            primary_str = pool[0] if pool else ""
            nh_str = narrative_heavy.strip()
            if nh_str and nh_str not in pool:
                pool.append(nh_str[:100])

    # 3. Directional variations — reduced to 3 from 5 (most distinct angles)
    from pipeline.cinematic_staging import has_person_reference, sanitize_shot_direction
    has_person = has_person_reference(search_en)
    directional_modifiers = [
        "wide shot establishing",
        sanitize_shot_direction("close-up detail", has_person=has_person),
        "distant view atmospheric",
    ]
    if search_en and search_en.strip():
        base_words = search_en.split()
        base = " ".join(base_words[:6])
        for mod in directional_modifiers:
            cand = f"{base} {mod}"[:100]
            if cand not in pool:
                pool.append(cand)

    # 4. Simplified: first 4 content words (no style fluff)
    if search_en and search_en.strip():
        words = [w for w in search_en.split() if w.lower() not in _STYLE_WORDS]
        simple = " ".join(words[:4])
        if simple and simple not in pool:
            pool.append(simple[:100])

    # 5. Theme-clean: just the LLM query keywords, no channel style
    if search_en and search_en.strip():
        theme_clean = _build_portrait_query(search_en, theme_keywords, "")
        if theme_clean.strip() and theme_clean not in pool:
            pool.append(theme_clean.strip()[:100])

    # 6. Type-specific fallback (moved later — lower priority)
    type_fb = _FALLBACK_BY_TYPE.get(block_type)
    if type_fb is None:
        stem = re.sub(r"\d+$", "", block_type)
        type_fb = _FALLBACK_BY_TYPE.get(stem)
    if type_fb and type_fb not in pool:
        pool.append(type_fb[:100])

    # 7. Themed fallback (v8): built dynamically from ThemeContext
    if theme_ctx and theme_ctx.primary_subject:
        from pipeline.cinematic_staging import build_contextual_fallback, fit_query
        contextual_fb = fit_query(build_contextual_fallback(block_type, theme_ctx, portrait=True))
        if contextual_fb and contextual_fb not in pool:
            pool.append(contextual_fb)
        themed_fb = _build_themed_short_fallback(block_type, theme_ctx)
        if themed_fb and themed_fb not in pool:
            pool.append(themed_fb[:100])

    # 8. Generic fallbacks (absolute last resort)
    gen_fb = fallback_queries or SHORTS_FALLBACK_QUERIES
    for fb in gen_fb:
        if fb and fb not in pool:
            pool.append(fb[:100])

    # Filter empty and deduplicate preserving order
    seen: set[str] = set()
    result: list[str] = []
    for q in pool:
        q = q.strip()
        if not q:
            continue
        if q in seen:
            continue
        seen.add(q)
        result.append(q[:100])

    return result


def _build_themed_short_fallback(block_type: str, theme_ctx) -> str:
    """Build a themed fallback query for shorts using the ThemeContext.

    Similar to MediaFetcher._build_themed_fallback() but adapted for
    vertical portrait shorts with shorter queries.
    """
    if not theme_ctx or not theme_ctx.primary_subject:
        return ""

    # Gather thematic anchors
    anchors: list[str] = []
    if theme_ctx.primary_subject:
        ps_words = theme_ctx.primary_subject.split()[:4]
        anchors.extend(ps_words)
    if theme_ctx.key_motifs:
        for motif in theme_ctx.key_motifs[:2]:
            motif_words = motif.split()[:2]
            anchors.extend(motif_words)

    mood_map = {
        "misterioso": "mysterious", "épico": "epic", "ominoso": "ominous",
        "melancólico": "melancholic", "esperanzador": "hopeful",
        "sereno": "serene", "perturbador": "disturbing",
    }
    mood_word = mood_map.get(theme_ctx.mood, "") if theme_ctx.mood else ""

    if not anchors:
        return ""

    seen = set()
    unique_anchors = []
    for a in anchors:
        al = a.lower()
        if al not in seen:
            seen.add(al)
            unique_anchors.append(a)

    base = " ".join(unique_anchors[:5])
    if not base:
        return ""

    mood_suffix = f" {mood_word}" if mood_word and len(base) + len(mood_word) + 2 < 100 else ""

    by_type = {
        "hook": f"{base}{mood_suffix} dramatic atmosphere",
        "desarrollo": f"{base} documentary establishing shot",
        "climax": f"{base}{mood_suffix} dark tension shadow",
        "cierre": f"{base}{mood_suffix} resolution ending",
    }
    result = by_type.get(block_type, f"{base}{mood_suffix} cinematic")
    if len(result) > 100:
        result = result[:100].rsplit(" ", 1)[0]
    return result


# ═══════════════════════════════════════════════════════════════════════════
# v2: Dedup helpers
# ═══════════════════════════════════════════════════════════════════════════

def _predict_filename(asset_info: dict[str, Any]) -> str:
    """Predict the local filename for an asset before downloading it.

    Image asset:
        ``output/images/{source}_{img_id}.jpg``
    Video asset:
        ``output/video_clips/{source}_{url_hash}.mp4``

    This allows cross-short filename dedup without hitting the filesystem
    or downloading first.
    """
    from config import settings

    asset_type = asset_info.get("type", "image")
    source = asset_info.get("provider", "unknown")
    img_id = str(asset_info.get("img_id", ""))

    if asset_type == "video":
        url = asset_info.get("url", "")
        url_hash = hashlib.md5(url.encode()).hexdigest()[:12]
        filename = f"{source}_{url_hash}.mp4"
        # Use VIDEOS_DIR for clips; but for Shorts we use a clips subdir
        video_clips_dir = settings.VIDEOS_DIR / "shorts_clips"
        return str(video_clips_dir / filename)
    else:
        filename = f"{source}_{img_id}.jpg"
        return str(settings.IMAGES_DIR / filename)


def _is_asset_duplicate(asset_info: dict[str, Any]) -> bool:
    """Check whether an asset has already been used in this short or any prior short.

    Checks (in order of cheapest first):
      1. URL in used_urls.
      2. Bad URL in bad_urls — skip known-broken URLs.
      3. Predicted filename in used_filenames or cross_short_filenames.
      4. source:img_id in used_img_ids.
      5. content_hash in used_content_hashes (checked elsewhere after download).
    """
    url = asset_info.get("url", "")
    if url and url in _DEDUP_STATE["used_urls"]:
        return True
    if url and url in _DEDUP_STATE["bad_urls"]:
        return True

    predicted = _predict_filename(asset_info)
    if predicted and (
        predicted in _DEDUP_STATE["used_filenames"]
        or predicted in _DEDUP_STATE["cross_short_filenames"]
    ):
        return True

    source = asset_info.get("provider", "")
    img_id = str(asset_info.get("img_id", ""))
    if source and img_id:
        dedup_key = f"{source}:{img_id}"
        if dedup_key in _DEDUP_STATE["used_img_ids"]:
            return True

    return False


def _record_asset_used(asset_info: dict[str, Any]) -> None:
    """Record an asset as used in this short's dedup state.

    Called after a successful download. Records URL, img_id, and filename.
    Content hash is recorded separately after computing it.
    """
    url = asset_info.get("url", "")
    if url:
        _DEDUP_STATE["used_urls"].add(url)

    source = asset_info.get("provider", "")
    img_id = str(asset_info.get("img_id", ""))
    if source and img_id:
        _DEDUP_STATE["used_img_ids"].add(f"{source}:{img_id}")

    predicted = _predict_filename(asset_info)
    if predicted:
        _DEDUP_STATE["used_filenames"].add(predicted)

    content_hash = asset_info.get("content_hash", "")
    if content_hash:
        _DEDUP_STATE["used_content_hashes"].add(content_hash)


# ═══════════════════════════════════════════════════════════════════════════
# v2: Exhaustive single-asset fetcher
# ═══════════════════════════════════════════════════════════════════════════

def _download_image_asset(
    photo: dict[str, Any],
    image_dir: Path,
    provider_name: str,
) -> dict[str, Any] | None:
    """Download an image from a photo result dict to disk.

    Handles Unsplash (direct download_url) and Pixabay (with fallback).
    Retries with exponential backoff on timeouts and server errors.
    Returns asset_info dict with path, url, img_id, provider, content_hash,
    or None on failure.
    """
    download_url = photo.get("download_url", "")
    fallback_url = photo.get("fallback_download_url", "")

    # Try primary URL, then fallback URL
    for attempt_url in (download_url, fallback_url):
        if not attempt_url:
            continue
        if attempt_url in _DEDUP_STATE["bad_urls"]:
            continue

        # ── Retry loop with exponential backoff ────────────────
        resp = None
        max_retries = 3
        for retry_attempt in range(max_retries):
            try:
                resp = requests.get(attempt_url, timeout=30)
                resp.raise_for_status()
                break  # success
            except requests.exceptions.Timeout:
                logger.warning(
                    "Shorts image download timeout (attempt %d/%d): %s",
                    retry_attempt + 1, max_retries, attempt_url[:80],
                )
            except requests.exceptions.ConnectionError as exc:
                logger.warning(
                    "Shorts image download connection error (attempt %d/%d): %s",
                    retry_attempt + 1, max_retries, exc,
                )
            except requests.exceptions.HTTPError as exc:
                status = exc.response.status_code if exc.response is not None else 0
                if status >= 500:
                    logger.warning(
                        "Shorts image download server error %d (attempt %d/%d): %s",
                        status, retry_attempt + 1, max_retries, attempt_url[:80],
                    )
                else:
                    # 4xx — don't retry, mark bad and move on
                    _DEDUP_STATE["bad_urls"].add(attempt_url)
                    resp = None
                    break
            except requests.RequestException:
                # Generic request error — mark bad, don't retry
                _DEDUP_STATE["bad_urls"].add(attempt_url)
                resp = None
                break

            if retry_attempt < max_retries - 1:
                sleep_time = 2 ** retry_attempt  # 1, 2, 4
                time.sleep(sleep_time)

        # If all retries exhausted or 4xx → try next URL
        if resp is None:
            _DEDUP_STATE["bad_urls"].add(attempt_url)
            continue

        # Validate: must actually be image data
        content_type = resp.headers.get("Content-Type", "").lower()
        if "image" not in content_type and len(resp.content) < 1000:
            _DEDUP_STATE["bad_urls"].add(attempt_url)
            continue

        content_hash = hashlib.md5(resp.content).hexdigest()
        if content_hash in _DEDUP_STATE["used_content_hashes"]:
            logger.debug("Image content hash duplicate: %s", content_hash[:12])
            return None

        img_id = str(photo.get("id", ""))
        safe_id = re.sub(r"[^a-zA-Z0-9_\-]", "_", img_id)[:40]
        filename = f"{provider_name}_{safe_id}.jpg"
        filepath = image_dir / filename
        filepath.write_bytes(resp.content)

        return {
            "path": filepath,
            "type": "image",
            "url": attempt_url,
            "img_id": img_id,
            "provider": provider_name,
            "source": provider_name,
            "content_hash": content_hash,
            "download_url": attempt_url,
            "duration": 0.0,
        }

    return None


def _download_video_asset(
    video_asset: Any,
    video_dir: Path,
    provider_name: str,
    provider: Any,
) -> dict[str, Any] | None:
    """Download a video clip from a VideoAsset to disk via provider.download().

    Returns asset_info dict or None on failure / duplicate content hash.
    """
    if not video_asset or not video_asset.url:
        return None

    if video_asset.url in _DEDUP_STATE["bad_urls"]:
        return None

    try:
        filepath = provider.download(video_asset, video_dir)
    except Exception as exc:
        logger.debug("Video download failed for %s: %s", video_asset.url[:60], exc)
        _DEDUP_STATE["bad_urls"].add(video_asset.url)
        return None

    if not filepath or not filepath.exists():
        return None

    # Compute content hash
    try:
        content = filepath.read_bytes()
        content_hash = hashlib.md5(content).hexdigest()
    except Exception:
        return None

    if content_hash in _DEDUP_STATE["used_content_hashes"]:
        logger.debug("Video content hash duplicate, deleting: %s", filepath)
        try:
            filepath.unlink()
        except Exception:
            pass
        return None

    url_hash = hashlib.md5(video_asset.url.encode()).hexdigest()[:12]

    return {
        "path": filepath,
        "type": "video",
        "url": video_asset.url,
        "img_id": url_hash,  # videos use URL hash as id
        "provider": provider_name,
        "source": provider_name,
        "content_hash": content_hash,
        "duration": video_asset.duration,
    }


def _fetch_single_asset_exhaustive(
    block: dict[str, Any],
    query_pool: list[str],
    want_video: bool,
    theme_keywords: list[str] | None = None,
    style_modifiers: str = "",
) -> dict[str, Any] | None:
    """Exhaustive search for one non-duplicate asset per block.

    Iterates queries in the pool. For each query, tries interleaved providers:
      - If want_video: video-first (Pexels → Pixabay video), then image fallback.
      - If not want_video: image-first (Unsplash → Pixabay image), then video fallback.

    For each provider, paginates through ALL results (30 per page images,
    20 per page videos), checking _is_asset_duplicate() on every candidate.
    Downloads the first non-duplicate hit. Records in _DEDUP_STATE.

    Returns asset dict with:
      path, type, source, url, img_id, duration, content_hash
    or None if all providers/queries exhausted.
    """
    from config import settings

    block_type = block.get("tipo", "desarrollo")

    image_dir = settings.IMAGES_DIR
    image_dir.mkdir(parents=True, exist_ok=True)

    video_clips_dir = settings.VIDEOS_DIR / "shorts_clips"
    video_clips_dir.mkdir(parents=True, exist_ok=True)

    unsplash, pixabay_img = _get_image_providers()
    video_providers = _get_portrait_video_providers()

    # ── Build ordered provider lists ──────────────────────────
    img_providers: list[tuple[str, Any, bool]] = []
    if unsplash:
        img_providers.append(("unsplash", unsplash, True))
    if pixabay_img:
        img_providers.append(("pixabay", pixabay_img, True))

    vid_providers: list[tuple[str, Any, bool]] = []
    for vp in video_providers:
        vid_providers.append((vp.name, vp, False))

    # ── Interleaved provider order ────────────────────────────
    if want_video:
        # Video-first: try all video providers, then all image providers
        ordered_providers: list[tuple[str, Any, bool]] = vid_providers + img_providers
    else:
        # Image-first: try all image providers, then video fallback
        ordered_providers = img_providers + vid_providers

    # ── Search through query pool ─────────────────────────────
    for query in query_pool:
        if not query or not query.strip():
            continue

        for pname, provider, is_image in ordered_providers:
            if provider is None:
                continue

            if is_image:
                # ── Image search with pagination ──────────────
                page = 1
                max_pages = 10  # safety cap
                while page <= max_pages:
                    try:
                        results = provider.search(
                            query, n=30, style_modifiers="",
                            page=page, orientation="portrait",
                        )
                    except Exception as exc:
                        logger.debug("Image search error [%s, page %d]: %s",
                                     pname, page, exc)
                        break  # stop paginating this provider for this query

                    if not results:
                        break  # no more results

                    for photo in results:
                        img_id = str(photo.get("id", ""))
                        url = photo.get("download_url", "")
                        if not url:
                            continue

                        candidate = {
                            "url": url,
                            "provider": pname,
                            "img_id": img_id,
                            "type": "image",
                            "download_url": url,
                            "fallback_download_url": photo.get("fallback_download_url", ""),
                        }
                        if _is_asset_duplicate(candidate):
                            continue

                        # Try download
                        asset_info = _download_image_asset(photo, image_dir, pname)
                        if asset_info is not None:
                            asset_info["block_type"] = block_type
                            asset_info["block_text"] = block.get("texto", "")
                            _record_asset_used(asset_info)
                            logger.info(
                                "✓  [%s] image found via %s (query=%r, page=%d) → %s",
                                block_type, pname, query[:40], page,
                                asset_info["path"],
                            )
                            return asset_info

                    # Check if we likely exhausted this query for this provider
                    max_pages = settings.SHORTS_MAX_QUERY_PAGES if hasattr(settings, "SHORTS_MAX_QUERY_PAGES") else 5
                    if len(results) < 30:
                        break  # last page
                    page += 1

            else:
                # ── Video search with pagination ──────────────
                page = 1
                max_vid_pages = 10
                while page <= max_vid_pages:
                    try:
                        search_page = provider.search_page(
                            query=query,
                            min_duration=2.0,
                            max_duration=15.0,
                            resolution=(1080, 1920),
                            page=page,
                            per_page=20,
                            orientation="portrait",
                        )
                    except Exception as exc:
                        logger.debug("Video search_page error [%s, page %d]: %s",
                                     pname, page, exc)
                        break

                    if not search_page or not search_page.assets:
                        break

                    for vid_asset in search_page.assets:
                        vid_url = vid_asset.url if hasattr(vid_asset, "url") else ""
                        if not vid_url:
                            continue

                        candidate = {
                            "url": vid_url,
                            "provider": pname,
                            "img_id": hashlib.md5(vid_url.encode()).hexdigest()[:12],
                            "type": "video",
                        }
                        if _is_asset_duplicate(candidate):
                            continue

                        # Try download
                        asset_info = _download_video_asset(
                            vid_asset, video_clips_dir, pname, provider,
                        )
                        if asset_info is not None:
                            asset_info["block_type"] = block_type
                            asset_info["block_text"] = block.get("texto", "")
                            _record_asset_used(asset_info)
                            logger.info(
                                "▶  [%s] video found via %s (query=%r, page=%d) → %s",
                                block_type, pname, query[:40], page,
                                asset_info["path"],
                            )
                            return asset_info

                    if not search_page.has_more:
                        break
                    page += 1

    # ── Exhausted all options ─────────────────────────────────
    logger.warning("✗  [%s] exhausted all assets across %d queries",
                   block_type, len(query_pool))
    return None


# ═══════════════════════════════════════════════════════════════════════════
# v2: Main entry point — fetch short assets with video/image mix
# ═══════════════════════════════════════════════════════════════════════════

def fetch_short_assets_exhaustive(
    blocks: list[dict[str, Any]],
    ch_config: Any,
    theme_keywords: list[str] | None = None,
    theme_ctx = None,  # v8: full ThemeContext for richer anchoring
    channel_id: int = 0,
    video_ratio: float | None = None,
    channel_slug: str = "",
    progress_cb: callable = None,
) -> list[dict[str, Any]]:
    """Fetch ONE asset per block with video/image mix strategy.

    Strategy:
      - ~55% video ratio by default (configurable via settings.SHORTS_VIDEO_PCT).
      - Hook + climax blocks ALWAYS get video (forced).
      - Remaining video slots assigned to the longest-duration blocks.
      - Each block gets exactly one asset via exhaustive per-block search.
      - Cross-short dedup: loads all previously-used filenames from DB.

    Args:
        blocks: List of block dicts with keys: tipo, texto, search_query_en, duracion_sec.
        ch_config: Channel config (for IMAGE_STYLE_MODIFIERS etc.).
        theme_keywords: Global theme keywords for query building.
        channel_id: Channel DB id (for flushing asset history).
        video_ratio: Override fraction of blocks that should get video (default 0.55).
        channel_slug: Channel slug for avatar fallback (subscribe CTA blocks).

    Returns:
        List of asset dicts, one per block (1:1 mapping). Blocks that couldn't
        find any asset get a ``None`` entry.
        Example::
            [{"path": Path(...), "type": "video", "source": "pexels",
              "url": "...", "img_id": "...", "duration": 12.3,
              "block_type": "hook", "block_text": "..."},
             {"path": Path(...), "type": "image", "source": "pixabay", ...},
             None,  # block 3 failed to find any asset
             ...]
    """
    from config import settings
    from database.db_extended import ExtendedDatabase

    # ── Reset module-level dedup state ────────────────────────
    _DEDUP_STATE["used_urls"] = set()
    _DEDUP_STATE["used_img_ids"] = set()
    _DEDUP_STATE["used_filenames"] = set()
    _DEDUP_STATE["used_content_hashes"] = set()
    _DEDUP_STATE["bad_urls"] = set()
    _DEDUP_STATE["cross_short_filenames"] = set()

    # Load cross-short filenames from DB
    try:
        db = ExtendedDatabase(settings.DATABASE_PATH)
        _DEDUP_STATE["cross_short_filenames"] = db.get_all_used_filenames()
        logger.info("Loaded %d cross-short filenames for dedup",
                    len(_DEDUP_STATE["cross_short_filenames"]))
    except Exception as exc:
        logger.warning("Could not load cross-short filenames: %s", exc)

    style_mod = getattr(ch_config, "IMAGE_STYLE_MODIFIERS", "")
    ratio = video_ratio if video_ratio is not None else settings.SHORTS_VIDEO_PCT
    n_blocks = len(blocks)

    if n_blocks == 0:
        return []

    # ── Decide which blocks get video ─────────────────────────
    video_count = max(1, int(round(n_blocks * ratio)))
    want_video_flags: list[bool] = [False] * n_blocks

    # Forced video blocks: hook (index 0) and climax (last or tipo="climax")
    forced_video_indices: set[int] = set()
    for i, b in enumerate(blocks):
        bt = b.get("tipo", "")
        # Hook is always index 0, or explicitly typed as "hook"
        if i == 0 or bt == "hook":
            forced_video_indices.add(i)
        # Climax
        if bt == "climax" or bt.startswith("climax"):
            forced_video_indices.add(i)

    # Assign video to forced blocks first
    remaining_video = video_count
    for idx in sorted(forced_video_indices):
        if remaining_video > 0:
            want_video_flags[idx] = True
            remaining_video -= 1

    # Assign remaining video slots to longest-duration blocks (not already video)
    if remaining_video > 0:
        # Sort non-video blocks by duration descending
        candidates = [
            (i, b.get("duracion_sec", 0))
            for i, b in enumerate(blocks)
            if not want_video_flags[i]
        ]
        candidates.sort(key=lambda x: -x[1])
        for idx, _ in candidates[:remaining_video]:
            want_video_flags[idx] = True

    logger.info(
        "Short asset strategy: %d/%d blocks video (~%.0f%%), %d forced (hook+climax)",
        sum(want_video_flags), n_blocks,
        sum(want_video_flags) / max(n_blocks, 1) * 100,
        min(len(forced_video_indices), video_count),
    )

    # ── Fetch one asset per block ─────────────────────────────
    assets: list[dict[str, Any] | None] = [None] * n_blocks

    for i, block in enumerate(blocks):
        block_type = block.get("tipo", f"desarrollo{i+1}")

        # Build query pool for this block
        query_pool = _build_query_pool(
            block, theme_keywords, style_mod,
            theme_ctx=theme_ctx,  # v8: pass full ThemeContext
            fallback_queries=settings.SHORTS_FALLBACK_QUERIES if hasattr(settings, "SHORTS_FALLBACK_QUERIES") else None,
        )
        logger.info(
            "[%d/%d] %s (%s): %d queries, want_video=%s",
            i + 1, n_blocks, block_type,
            "video" if want_video_flags[i] else "image",
            len(query_pool), want_video_flags[i],
        )

        asset = _fetch_single_asset_exhaustive(
            block, query_pool,
            want_video=want_video_flags[i],
            theme_keywords=theme_keywords,
            style_modifiers=style_mod,
        )
        assets[i] = asset

        # ── v3: progress callback every ~1/3 of blocks ──
        if progress_cb is not None and (i == 0 or i == n_blocks - 1
                or (i + 1) % max(1, n_blocks // 3) == 0):
            try:
                progress_cb(i + 1, n_blocks)
            except Exception:
                pass

    # ── Subscribe CTA fallback: use channel avatar if no stock asset found ──
    for i, block in enumerate(blocks):
        if block.get("tipo") == "subscribe_cta" and assets[i] is None and channel_slug:
            avatar_path = Path("output") / "thumbnails" / channel_slug / "avatar.jpg"
            if avatar_path.exists():
                assets[i] = {
                    "path": str(avatar_path),
                    "type": "image",
                    "source": "local_avatar",
                    "url": "",
                    "img_id": f"avatar_{channel_slug}",
                    "duration": block.get("duracion_sec", 4),
                    "block_type": "subscribe_cta",
                    "block_text": block.get("texto", []),
                }
                logger.info("  [%d/%d] subscribe_cta → channel avatar fallback: %s",
                           i + 1, n_blocks, avatar_path)
            else:
                logger.warning("  [%d/%d] subscribe_cta → no avatar found at %s (will use solid bg)",
                             i + 1, n_blocks, avatar_path)

    # ── Summary ───────────────────────────────────────────────
    success_count = sum(1 for a in assets if a is not None)
    video_count_final = sum(1 for a in assets if a is not None and a.get("type") == "video")
    image_count_final = sum(1 for a in assets if a is not None and a.get("type") == "image")
    logger.info(
        "Short assets fetched: %d/%d (video=%d, image=%d, failed=%d)",
        success_count, n_blocks, video_count_final, image_count_final,
        n_blocks - success_count,
    )

    return assets


# ═══════════════════════════════════════════════════════════════════════════
# v2: Hybrid render (video + Ken Burns images + xfade)
# ═══════════════════════════════════════════════════════════════════════════

def has_sufficient_visual_assets(asset_items, min_ratio: float = 0.5) -> bool:
    """Reject solid-bg or mostly-filler renders before ffmpeg/upload."""
    positions = len(asset_items or [])
    valid = sum(1 for asset in (asset_items or []) if asset is not None)
    return positions > 0 and valid > 0 and (valid / positions) >= float(min_ratio)


def render_short_hybrid(
    asset_items: list[dict[str, Any] | None],
    audio_path: Path,
    output_path: Path,
    audio_duration: float | None = None,
    bg_color_hex: str = "0a0a1a",
    crossfade_dur: float = 1.0,
    srt_path: Path | None = None,
    scene_ranges: list[dict] | None = None,
    progress_cb: callable = None,
) -> Path:
    """FFmpeg hybrid render: mix video clips and Ken Burns still images.

    Resolution: 1080x1920, 30fps, crf=22, libx264, yuv420p.

    For video assets:
      - scale+crop to 9:16, trim to their scene duration.
      - duration comes from asset's «duration» field clipped to block's
        duracion_sec, or evenly divided from total audio_duration.

    For image assets:
      - scale+crop to 9:16 + Ken Burns zoompan effect.
      - zoompan: ``zoompan=z='min(zoom+0.0015,1.12)':d={frames}:s=1080x1920:...``

    All clips are sequenced with xfade transitions. SRT subtitles are burned
    in if provided. Falls back to solid-colour background if no viable assets.

    If ``scene_ranges`` is provided (must have same length as valid assets),
    each segment is trimmed to match its actual narration duration instead of
    a uniform split. This enables sub-scene splitting: a 20s block that gets
    split into two 10s sub-scenes will render two distinct assets of ~10s each
    instead of one stretched 20s asset.

    Args:
        asset_items: List of asset dicts from fetch_short_assets_exhaustive().
                     None entries are skipped (solid-bg filler).
        audio_path: Path to MP3 audio file (TTS output).
        output_path: Where to write the output MP4.
        audio_duration: Duration in seconds (auto-detected if None).
        bg_color_hex: Fallback background color (hex without #).
        crossfade_dur: Crossfade duration between scenes in seconds.
        srt_path: Optional SRT/VTT subtitle file path.
        scene_ranges: Optional list of scene range dicts (from
                      VideoEditor._compute_block_ranges) with "duration" key.
                      When provided and length matches valid assets, each
                      asset's visual duration matches its narration duration.

    Returns:
        The output Path on success.

    Raises:
        RuntimeError: If FFmpeg render fails.
    """
    from config import settings

    render_timeout = render_timeout_seconds(audio_duration, len(asset_items))

    # ── Audio duration ────────────────────────────────────────
    if audio_duration is None and audio_path.exists():
        dur_out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "csv=p=0", str(audio_path)],
            capture_output=True, text=True, timeout=10,
        )
        try:
            audio_duration = float(dur_out.stdout.strip()) + 1.5
        except (ValueError, AttributeError):
            audio_duration = 20.0
    elif audio_duration is None:
        audio_duration = 20.0

    # ── v3: progress — audio analyzed ──
    if progress_cb is not None:
        progress_cb(55, "render", "Analizando audio del short...")

    # ── Utility: check if an asset is renderable ──────────────
    def _asset_valid(a: dict[str, Any] | None) -> bool:
        if a is None:
            return False
        p = a.get("path")
        if p is None:
            return False
        if isinstance(p, str):
            p = Path(p)
        if not p.exists():
            logger.warning("Asset path does not exist, skipping: %s", p)
            return False
        return True

    # Build list of booleans matching asset_items length
    asset_valid_flags = [_asset_valid(a) for a in asset_items]
    # Count valid assets for logging / stats
    valid_assets: list[dict[str, Any]] = [
        a for a, ok in zip(asset_items, asset_valid_flags) if ok
    ]
    has_any_valid = any(asset_valid_flags)

    # ── No renderable assets is a hard rejection ─────────────────
    if not has_any_valid:
        raise RuntimeError("degraded render rejected: no valid visual assets")

    # ── Hybrid render: mixed video + image scenes ─────────────
    n_assets = len(asset_items)  # total positions including None fillers
    fade = crossfade_dur

    # Determine whether to use variable scene durations from scene_ranges
    # (enables sub-scene splitting: each split sub-scene gets its own distinct
    #  visual asset with its actual narration duration)
    use_variable_dur = (
        scene_ranges is not None
        and len(scene_ranges) == n_assets
    )

    # Pre-compute per-segment durations and xfade offsets
    #   - Each segment gets visual_dur = narration_dur + fade
    #   - xfade offsets MUST be CUMULATIVE for chained FFmpeg xfade:
    #     offset[i] = sum of narration durations 0..i (the time in the
    #     output stream when the transition to scene i+1 should begin).
    per_seg_dur: list[float] = []       # visual clip duration per segment
    per_xfade_offset: list[float] = []   # xfade offset per transition step

    if use_variable_dur:
        cumulative = 0.0
        for i, sr in enumerate(scene_ranges):
            narration_dur = float(sr.get("duration", 5.0))
            per_seg_dur.append(narration_dur + fade)
            if i < n_assets - 1:
                cumulative += narration_dur
                per_xfade_offset.append(cumulative)
        logger.info(
            "FFmpeg variable scene durations: %d assets, durations=%s, offsets=%s",
            n_assets,
            [f"{d:.1f}s" for d in per_seg_dur],
            [f"{o:.1f}s" for o in per_xfade_offset],
        )
    else:
        # Uniform split (legacy behavior) — offsets are also cumulative
        segment_dur = (audio_duration + fade * (n_assets - 1)) / max(n_assets, 1)
        per_seg_dur = [segment_dur + fade] * n_assets
        for i in range(n_assets - 1):
            per_xfade_offset.append((i + 1) * segment_dur - fade)
        logger.info(
            "FFmpeg uniform scene durations: %d assets, each %.1fs, offsets=%s",
            n_assets, segment_dur,
            [f"{o:.1f}s" for o in per_xfade_offset],
        )

    # ── Build inputs: valid assets use their file; None/invalid → solid-bg ──
    inputs: list[str] = []
    filter_parts: list[str] = []

    for i, (asset, ok) in enumerate(zip(asset_items, asset_valid_flags)):
        visual_dur = per_seg_dur[i]

        if not ok:
            # Solid-bg placeholder — preserves timeline position
            inputs.extend(["-f", "lavfi", "-i",
                f"color=c=0x{bg_color_hex}:s=1080x1920:d={visual_dur:.3f}:r=30"])
            filter_parts.append(f"[{i}:v]null[v{i}]")
            continue

        asset_type = asset.get("type", "image")
        asset_path = str(asset["path"])

        if asset_type == "video":
            # Video: scale + crop to 9:16, trim to visual_dur
            inputs.extend(["-i", asset_path])
            filter_parts.append(
                f"[{i}:v]scale=1080:1920:force_original_aspect_ratio=increase,"
                f"crop=1080:1920,setsar=1,fps=30,"
                f"trim=duration={visual_dur:.3f},setpts=PTS-STARTPTS[v{i}]"
            )
        else:
            # Image: loop + Ken Burns zoompan
            inputs.extend(["-loop", "1", "-t",
                          f"{visual_dur:.2f}", "-i", asset_path])
            frames = int(visual_dur * 30)
            zoompan = (
                f"zoompan=z='min(zoom+0.0015,1.12)':"
                f"d={frames}:s=1080x1920:"
                f"x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':fps=30"
            )
            filter_parts.append(
                f"[{i}:v]scale=1080:1920:force_original_aspect_ratio=increase,"
                f"crop=1080:1920,setsar=1,"
                f"{zoompan}[v{i}]"
            )

    # Add audio as final input
    inputs.extend(["-i", str(audio_path)])
    audio_idx = n_assets

    # ── Crossfade chain ────────────────────────────────────────
    if n_assets == 1:
        final_label = "[v0]"
    else:
        for i in range(1, n_assets):
            offset = per_xfade_offset[i - 1]
            if i == 1:
                filter_parts.append(
                    f"[v0][v1]xfade=transition=fade:duration={fade}:offset={offset:.3f}[vf1]"
                )
            else:
                filter_parts.append(
                    f"[vf{i-1}][v{i}]xfade=transition=fade:"
                    f"duration={fade}:offset={offset:.3f}[vf{i}]"
                )
        final_label = f"[vf{n_assets - 1}]"

    # ── Burn SRT subtitles ─────────────────────────────────────
    if srt_path and srt_path.exists():
        filter_parts.append(
            f"{final_label}subtitles='{_esc_ffmpeg(str(srt_path))}':"
            f"force_style='FontSize=18,Alignment=2,"
            f"PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,"
            f"BorderStyle=1,Outline=2'[v]"
        )
    else:
        filter_parts.append(f"{final_label}null[v]")

    filter_graph = ";".join(filter_parts)

    cmd = [
        "ffmpeg", "-y",
        *inputs,
        "-filter_complex", filter_graph,
        "-map", "[v]", "-map", f"{audio_idx}:a",
        "-c:v", "libx264", "-preset", "fast", "-crf", "22",
        "-pix_fmt", "yuv420p", "-r", "30",
        "-c:a", "aac", "-b:a", "128k",
        "-shortest", "-movflags", "+faststart",
        str(output_path),
    ]

    logger.info("FFmpeg hybrid render: %d segments (%d valid, %d video, %d image, %d filler)",
                n_assets,
                sum(asset_valid_flags),
                sum(1 for a in valid_assets if a.get("type") == "video"),
                sum(1 for a in valid_assets if a.get("type") == "image"),
                n_assets - sum(asset_valid_flags))

    # ── v3: progress — rendering with ffmpeg ──
    if progress_cb is not None:
        progress_cb(65, "render", "Renderizando short (ffmpeg xfade + subtitulos)...")

    last_error = ""
    for attempt in range(2):
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=render_timeout
            )
        except subprocess.TimeoutExpired as exc:
            last_error = f"timeout after {render_timeout}s"
            logger.warning("FFmpeg hybrid render timeout (attempt %d/2)", attempt + 1)
            if attempt == 1:
                raise RuntimeError(last_error) from exc
            continue
        if result.returncode == 0:
            break
        last_error = result.stderr[-600:] if result.stderr else "(no stderr)"
        logger.warning("FFmpeg hybrid render failed (attempt %d/2): %s", attempt + 1, last_error)
        if attempt == 1:
            raise RuntimeError(f"ffmpeg failed: {last_error}")

    # ── v3: progress — render complete ──
    if progress_cb is not None:
        progress_cb(72, "render", "Short renderizado, verificando output...")

    logger.info("Hybrid short rendered: %s (%.1f MB)",
                output_path, output_path.stat().st_size / 1024 / 1024)
    return output_path


def render_timeout_seconds(audio_duration: float | None, asset_count: int) -> int:
    """Return a bounded timeout scaled to the actual short render workload.

    Cap raised from 900s to 1500s (2026-08-31): under concurrent system load
    (other projects sharing the machine), ffmpeg renders can legitimately take
    longer than 15 min; the previous cap caused avoidable "render produced no
    output file" failures and slot retries.
    """
    duration = max(float(audio_duration or 20.0), 1.0)
    assets = max(int(asset_count or 1), 1)
    return min(1500, max(180, int(120 + duration * 7 + assets * 12)))


# ═══════════════════════════════════════════════════════════════════════════
# v2: Asset history flush
# ═══════════════════════════════════════════════════════════════════════════

def flush_short_asset_history(
    short_id: int,
    channel_id: int,
    asset_items: list[dict[str, Any] | None],
) -> int:
    """Record all used assets to ``short_asset_history`` table for cross-short dedup.

    Args:
        short_id: The short's DB id.
        channel_id: Channel DB id.
        asset_items: List of asset dicts from fetch_short_assets_exhaustive().

    Returns:
        Number of assets successfully recorded.
    """
    from config import settings
    from database.db_extended import ExtendedDatabase

    db = ExtendedDatabase(settings.DATABASE_PATH)
    recorded = 0

    for asset in asset_items:
        if asset is None:
            continue
        path = asset.get("path")
        if path is None:
            continue
        file_path = str(path)
        source = asset.get("source", asset.get("provider", "unknown"))
        url = asset.get("url", "")
        try:
            db.insert_short_asset_history(short_id, channel_id, file_path, source, url)
            recorded += 1
        except Exception as exc:
            logger.warning("Failed to record short asset history for %s: %s",
                         file_path, exc)

    logger.info("Flushed %d short asset history rows (short_id=%d)", recorded, short_id)
    return recorded


# ═══════════════════════════════════════════════════════════════════════════
# ─── Legacy functions (kept for backward compatibility) ─────────────────
# ═══════════════════════════════════════════════════════════════════════════

def fetch_portrait_images(
    queries: list[str],
    ch_config: Any,
    count: int = 4,
) -> list[Path]:
    """Fetch portrait (vertical) images from Unsplash / Pixabay for a Short.

    .. deprecated::
        Use ``fetch_short_assets_exhaustive()`` instead for per-block
        video/image mix with cross-short dedup.

    Args:
        queries: Search queries — should be English keywords (5-8 words) for
                 stock API compatibility. NOT raw Spanish narration text.
        ch_config: Channel config module (has IMAGE_STYLE_MODIFIERS etc.).
        count: Max number of images to download.

    Returns:
        List of local Paths to downloaded images (may be shorter than count
        on errors).
    """
    from config import settings

    unsplash, pixabay = _get_image_providers()
    style_mod = getattr(ch_config, "IMAGE_STYLE_MODIFIERS", "")

    if unsplash is None and pixabay is None:
        logger.warning("No image providers configured — Short will have solid background")
        return []

    images_dir = settings.IMAGES_DIR
    images_dir.mkdir(parents=True, exist_ok=True)

    downloaded: list[Path] = []
    _used_urls: set[str] = set()  # dedup across all queries

    for query in queries[:count + 2]:  # slight overfetch
        if len(downloaded) >= count:
            break
        if not query or not query.strip():
            continue

        results = []
        # Try with full query first
        for provider in (unsplash, pixabay):
            if provider is None:
                continue
            try:
                results = provider.search(query, n=2, style_modifiers="", orientation="portrait")
            except Exception as exc:
                logger.debug("Provider search failed for %r: %s", query[:40], exc)
            if results:
                break

        # Fallback: simplified query (first 3-4 keywords only)
        if not results and len(query.split()) > 3:
            simple_query = " ".join(query.split()[:4])
            if simple_query != query:
                logger.debug("Retrying with simplified query: %r → %r", query[:40], simple_query)
                for provider in (unsplash, pixabay):
                    if provider is None:
                        continue
                    try:
                        results = provider.search(simple_query, n=2, style_modifiers="", orientation="portrait")
                    except Exception as exc:
                        logger.debug("Simplified retry failed: %s", exc)
                    if results:
                        break

        if not results:
            continue

        for photo in results:
            download_url = photo.get("download_url", "")
            if not download_url:
                continue
            # Dedup: skip URLs already used in this short
            if download_url in _used_urls:
                logger.debug("Skipping duplicate image URL: %s", download_url[:60])
                continue
            try:
                resp = requests.get(download_url, timeout=30)
                resp.raise_for_status()
            except requests.RequestException as exc:
                logger.debug("Download failed: %s", exc)
                continue

            _used_urls.add(download_url)
            url_hash = hashlib.md5(download_url.encode()).hexdigest()[:12]
            safe_query = re.sub(r"[^a-zA-Z0-9_]", "_", query[:30])
            filename = f"short_portrait_{safe_query}_{url_hash}.jpg"
            filepath = images_dir / filename
            filepath.write_bytes(resp.content)
            downloaded.append(filepath)
            break

    logger.info("Fetched %d portrait images for Short (wanted %d)", len(downloaded), count)
    return downloaded


def _build_solid_bg_filter(
    bg_color_hex: str,
    srt_path: Path | None = None,
) -> str:
    """Build filter_complex for solid-colour background (no images).

    If an SRT subtitle file is available it is burned in via the
    ``subtitles`` filter; otherwise the background is passed through as-is.
    """
    if srt_path and srt_path.exists():
        return (
            f"[0:v]subtitles='{_esc_ffmpeg(str(srt_path))}':"
            f"force_style='FontSize=18,Alignment=2,"
            f"PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,"
            f"BorderStyle=1,Outline=2'[v]"
        )
    return "[0:v]null[v]"


def render_slideshow_with_images(
    image_paths: list[Path],
    audio_path: Path,
    hook_text: str,
    output_path: Path,
    audio_duration: float | None = None,
    bg_color_hex: str = "0a0a1a",
    crossfade_dur: float = 1.0,
    srt_path: Path | None = None,
) -> Path:
    """Render a vertical Short video as a slideshow of portrait images.

    Each image is scaled+cropped to 1080x1920, sequenced with crossfade
    transitions.  Optional SRT subtitle file is burned in via the
    ``subtitles`` filter, and TTS audio is added.

    Falls back to solid-colour background if no images are provided.

    Args:
        image_paths: Local paths to portrait images (empty = solid bg).
        audio_path: Path to MP3 audio file (TTS output).
        hook_text: Hook text to burn at the bottom.
        output_path: Where to write the output MP4.
        audio_duration: Duration of the audio in seconds (auto-detected).
        bg_color_hex: Fallback background color (hex without #).
        crossfade_dur: Crossfade duration between images in seconds.
        srt_path: Optional SRT/VTT subtitle file path to burn with
            FFmpeg's ``subtitles`` filter.

    Returns:
        The output Path on success.
    """
    # Get audio duration if not provided
    if audio_duration is None:
        dur_out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "csv=p=0", str(audio_path)],
            capture_output=True, text=True, timeout=10,
        )
        try:
            audio_duration = float(dur_out.stdout.strip()) + 1.5
        except (ValueError, AttributeError):
            audio_duration = 20.0

    # ── No images → solid background fallback ────────────────
    if not image_paths:
        filter_str = _build_solid_bg_filter(bg_color_hex, srt_path)
        cmd = [
            "ffmpeg", "-y",
            "-f", "lavfi", "-i",
            f"color=c=0x{bg_color_hex}:s=1080x1920:d={audio_duration}:r=30",
            "-i", str(audio_path),
            "-filter_complex", filter_str,
            "-map", "[v]", "-map", "1:a",
            "-c:v", "libx264", "-preset", "fast", "-crf", "22",
            "-pix_fmt", "yuv420p", "-r", "30",
            "-c:a", "aac", "-b:a", "128k",
            "-shortest", "-movflags", "+faststart",
            str(output_path),
        ]
        subprocess.run(cmd, capture_output=True, timeout=180)
        return output_path

    # ── Images → slideshow with crossfade ────────────────────
    n_images = len(image_paths)
    fade = crossfade_dur
    segment_dur = (audio_duration + fade * (n_images - 1)) / n_images

    inputs = []
    filter_parts = []

    # Input each image as a loop
    for img in image_paths:
        inputs.extend(["-loop", "1", "-t", f"{segment_dur + fade:.2f}", "-i", str(img)])

    # Add audio last
    inputs.extend(["-i", str(audio_path)])
    audio_idx = n_images

    # Scale + crop each image to 9:16
    for i in range(n_images):
        filter_parts.append(
            f"[{i}:v]scale=1080:1920:force_original_aspect_ratio=increase,"
            f"crop=1080:1920,setsar=1,fps=30[v{i}]"
        )

    # Crossfade chain
    if n_images == 1:
        final_label = "[v0]"
    else:
        for i in range(1, n_images):
            offset = segment_dur - fade
            if i == 1:
                filter_parts.append(
                    f"[v0][v1]xfade=transition=fade:duration={fade}:offset={offset:.2f}[vf1]"
                )
            else:
                filter_parts.append(
                    f"[vf{i-1}][v{i}]xfade=transition=fade:"
                    f"duration={fade}:offset={offset:.2f}[vf{i}]"
                )
        final_label = f"[vf{n_images - 1}]"

    # Burn SRT subtitles if available; otherwise passthrough video as-is
    if srt_path and srt_path.exists():
        filter_parts.append(
            f"{final_label}subtitles='{_esc_ffmpeg(str(srt_path))}':"
            f"force_style='FontSize=18,Alignment=2,"
            f"PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,"
            f"BorderStyle=1,Outline=2'[v]"
        )
    else:
        filter_parts.append(f"{final_label}null[v]")

    filter_graph = ";".join(filter_parts)
    cmd = [
        "ffmpeg", "-y",
        *inputs,
        "-filter_complex", filter_graph,
        "-map", "[v]", "-map", f"{audio_idx}:a",
        "-c:v", "libx264", "-preset", "fast", "-crf", "22",
        "-pix_fmt", "yuv420p", "-r", "30",
        "-c:a", "aac", "-b:a", "128k",
        "-shortest", "-movflags", "+faststart",
        str(output_path),
    ]

    result = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    if result.returncode != 0:
        logger.error("FFmpeg slideshow failed: %s", result.stderr[-400:])
        raise RuntimeError(f"FFmpeg slideshow render failed: {result.stderr[-300:]}")
    return output_path
