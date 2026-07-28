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
    max_len: int = 100,
) -> str:
    """Build a short-focused search query fusing scene narrative with theme context.

    Strategy (v7 — narrative-first fusion):
    1. Scene narrative keywords from ``search_query_en`` — primary subject (~75%)
    2. Theme keywords as era/style anchors (~20%)
    3. Channel style modifiers for aesthetic consistency (~15%)
    4. Fit within ``max_len`` chars (Pixabay limit: 100).

    This mirrors ``MediaFetcher._build_search_query()`` adapted for portrait shorts.
    """
    # 1. Strip style fluff from the LLM query
    words = search_query_en.split()
    scene_keywords = [w for w in words if w.lower() not in _STYLE_WORDS]
    if not scene_keywords:
        scene_keywords = [w for w in words]

    if not scene_keywords and not theme_keywords:
        return search_query_en.strip()[:max_len]

    # 2. Allocate character budget: scene ~75%, theme ~20%, style ~15%
    style_budget = min(len(style_modifiers) + 1, 14) if style_modifiers else 0
    theme_budget = min(20, max_len - style_budget)
    scene_budget = max_len - style_budget - theme_budget

    # 3. Build scene narrative part (primary subject)
    scene_part = ""
    for w in scene_keywords:
        candidate = f"{scene_part} {w}".strip()
        if len(candidate) <= scene_budget:
            scene_part = candidate
        else:
            break
    if not scene_part and scene_keywords:
        scene_part = scene_keywords[0][:scene_budget]
    elif not scene_part and theme_keywords:
        scene_budget = max_len - style_budget
        for kw in theme_keywords[:2]:
            candidate = f"{scene_part} {kw}".strip()
            if len(candidate) <= scene_budget:
                scene_part = candidate
            else:
                break
        theme_keywords = None

    # 4. Build theme context part (max 2 keywords, dedup with scene)
    theme_part = ""
    if theme_keywords:
        scene_lower = scene_part.lower()
        fresh_keywords = [
            kw for kw in theme_keywords[:2]
            if kw.lower() not in scene_lower
        ]
        remaining = max_len - len(scene_part) - style_budget
        for kw in fresh_keywords:
            candidate = f"{theme_part} {kw}".strip()
            if len(candidate) <= max(remaining, 10):
                theme_part = candidate
            else:
                break

    # 5. Add style modifiers
    style_part = style_modifiers if style_modifiers else ""

    # 6. Assemble: scene (primary) + style (diversity) + theme (anchor)
    parts = [scene_part]
    if style_part:
        parts.append(style_part)
    if theme_part:
        parts.append(theme_part)

    result = " ".join(parts)

    # 7. Final safety: truncate at last complete word
    if len(result) > max_len:
        result = result[:max_len].rsplit(" ", 1)[0]

    return result


# ═══════════════════════════════════════════════════════════════════════════
# v2: Query pool builder
# ═══════════════════════════════════════════════════════════════════════════

def _build_query_pool(
    block: dict[str, Any],
    theme_keywords: list[str] | None = None,
    style_modifiers: str = "",
    fallback_queries: list[str] | None = None,
) -> list[str]:
    """Build ~6-8 ordered query variations for exhaustive search per block.

    Strategy:
      1. Primary: LLM's ``search_query_en`` via ``_build_portrait_query``
      2. Directional variations: wide shot, close-up, alternative angle,
         low angle dramatic, distant view atmospheric — each built by
         appending one modifier word to the primary query.
      3. Simplified: first 4 content words of the query (stripped).
      4. Theme-clean: LLM query without style modifiers (raw keywords).
      5. Type-specific fallback from _FALLBACK_BY_TYPE.
      6. Generic fallbacks from SHORTS_FALLBACK_QUERIES.

    Returns a deduplicated ordered list of non-empty queries, each ≤100 chars.
    """
    from config.settings import SHORTS_FALLBACK_QUERIES

    search_en = block.get("search_query_en", "")
    block_type = block.get("tipo", "desarrollo")

    pool: list[str] = []

    # 1. Primary query: scene + theme + style
    if search_en and search_en.strip():
        primary = _build_portrait_query(search_en, theme_keywords, style_modifiers)
        if primary.strip():
            pool.append(primary.strip()[:100])

    # 2. Directional variations — add one modifier word at a time
    directional_modifiers = [
        "wide shot establishing",
        "close-up detail",
        "alternative angle composition",
        "low angle dramatic",
        "distant view atmospheric",
    ]
    if search_en and search_en.strip():
        base_words = search_en.split()
        # take up to 6 content words as base
        base = " ".join(base_words[:6])
        for mod in directional_modifiers:
            cand = f"{base} {mod}"[:100]
            if cand not in pool:
                pool.append(cand)

    # 3. Simplified: first 4 content words (no style fluff)
    if search_en and search_en.strip():
        words = [w for w in search_en.split() if w.lower() not in _STYLE_WORDS]
        simple = " ".join(words[:4])
        if simple and simple not in pool:
            pool.append(simple[:100])

    # 4. Theme-clean: just the LLM query keywords, no channel style
    if search_en and search_en.strip():
        theme_clean = _build_portrait_query(search_en, theme_keywords, "")
        if theme_clean.strip() and theme_clean not in pool:
            pool.append(theme_clean.strip()[:100])

    # 5. Type-specific fallback
    # Try exact type match first, then strip trailing digits (desarrollo2 → desarrollo)
    type_fb = _FALLBACK_BY_TYPE.get(block_type)
    if type_fb is None:
        stem = re.sub(r"\d+$", "", block_type)
        type_fb = _FALLBACK_BY_TYPE.get(stem)
    if type_fb and type_fb not in pool:
        pool.append(type_fb[:100])

    # 6. Generic fallbacks
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
    Returns asset_info dict with path, url, img_id, provider, content_hash,
    or None on failure.
    """
    download_url = photo.get("download_url", "")
    fallback_url = photo.get("fallback_download_url", "")

    # Try primary URL
    for attempt_url in (download_url, fallback_url):
        if not attempt_url:
            continue
        if attempt_url in _DEDUP_STATE["bad_urls"]:
            continue
        try:
            resp = requests.get(attempt_url, timeout=30)
            resp.raise_for_status()
        except requests.RequestException:
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
    channel_id: int = 0,
    video_ratio: float | None = None,
    channel_slug: str = "",
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
    from database.db_extended import DatabaseExtended

    # ── Reset module-level dedup state ────────────────────────
    _DEDUP_STATE["used_urls"] = set()
    _DEDUP_STATE["used_img_ids"] = set()
    _DEDUP_STATE["used_filenames"] = set()
    _DEDUP_STATE["used_content_hashes"] = set()
    _DEDUP_STATE["bad_urls"] = set()
    _DEDUP_STATE["cross_short_filenames"] = set()

    # Load cross-short filenames from DB
    try:
        db = DatabaseExtended(settings.DATABASE_PATH)
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

def render_short_hybrid(
    asset_items: list[dict[str, Any] | None],
    audio_path: Path,
    output_path: Path,
    audio_duration: float | None = None,
    bg_color_hex: str = "0a0a1a",
    crossfade_dur: float = 1.0,
    srt_path: Path | None = None,
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

    Args:
        asset_items: List of asset dicts from fetch_short_assets_exhaustive().
                     None entries are skipped (solid-bg filler).
        audio_path: Path to MP3 audio file (TTS output).
        output_path: Where to write the output MP4.
        audio_duration: Duration in seconds (auto-detected if None).
        bg_color_hex: Fallback background color (hex without #).
        crossfade_dur: Crossfade duration between scenes in seconds.
        srt_path: Optional SRT/VTT subtitle file path.

    Returns:
        The output Path on success.

    Raises:
        RuntimeError: If FFmpeg render fails.
    """
    from config import settings

    # Filter to valid assets (non-None, with existing path)
    valid_assets: list[dict[str, Any]] = []
    for a in asset_items:
        if a is None:
            continue
        p = a.get("path")
        if p is None:
            continue
        if isinstance(p, str):
            p = Path(p)
        if not p.exists():
            logger.warning("Asset path does not exist, skipping: %s", p)
            continue
        valid_assets.append(a)

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

    # ── No valid assets → solid colour background fallback ────
    if not valid_assets:
        filter_str = _build_solid_bg_filter(bg_color_hex, srt_path)
        cmd = [
            "ffmpeg", "-y",
            "-f", "lavfi", "-i",
            f"color=c=0x{bg_color_hex}:s=1080x1920:d={audio_duration}:r=30",
            "-i", str(audio_path) if audio_path.exists() else "-f", "lavfi",
        ]
        if not audio_path.exists():
            cmd.extend(["-i", f"anullsrc=r=44100:cl=stereo:d={audio_duration}"])
        cmd.extend([
            "-filter_complex", filter_str,
            "-map", "[v]", "-map", f"{'1' if audio_path.exists() else '2'}:a",
            "-c:v", "libx264", "-preset", "fast", "-crf", "22",
            "-pix_fmt", "yuv420p", "-r", "30",
            "-c:a", "aac", "-b:a", "128k",
            "-shortest", "-movflags", "+faststart",
            str(output_path),
        ])
        subprocess.run(cmd, capture_output=True, timeout=180)
        return output_path

    # ── Hybrid render: mixed video + image scenes ─────────────
    n_assets = len(valid_assets)
    fade = crossfade_dur
    # Each segment duration: total = n * seg_dur - (n-1) * fade
    # → seg_dur = (total + (n-1) * fade) / n
    segment_dur = (audio_duration + fade * (n_assets - 1)) / max(n_assets, 1)

    inputs: list[str] = []
    filter_parts: list[str] = []
    img_counter = 0  # separate counter for image zoompans

    for i, asset in enumerate(valid_assets):
        asset_type = asset.get("type", "image")
        asset_path = str(asset["path"])

        if asset_type == "video":
            # Video: use as-is (trimmed to segment_dur later in concat)
            inputs.extend(["-i", asset_path])
            # Scale + crop to 9:16
            filter_parts.append(
                f"[{i}:v]scale=1080:1920:force_original_aspect_ratio=increase,"
                f"crop=1080:1920,setsar=1,fps=30,"
                f"trim=duration={segment_dur+fade:.3f},setpts=PTS-STARTPTS[v{i}]"
            )
        else:
            # Image: loop + Ken Burns zoompan
            # Use a dedicated counter for the image-to-zoompan label chain
            inputs.extend(["-loop", "1", "-t",
                          f"{segment_dur + fade:.2f}", "-i", asset_path])
            frames = int((segment_dur + fade) * 30)
            zoompan = (
                f"zoompan=z='min(zoom+0.0015,1.12)':"
                f"d={frames}:s=1080x1920:"
                f"x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':fps=30"
            )
            # First scale/crop, then apply Ken Burns zoompan
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
            offset = segment_dur - fade
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

    logger.info("FFmpeg hybrid render: %d assets (%d video, %d image)",
                n_assets,
                sum(1 for a in valid_assets if a.get("type") == "video"),
                sum(1 for a in valid_assets if a.get("type") == "image"))

    result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    if result.returncode != 0:
        err = result.stderr[-600:] if result.stderr else "(no stderr)"
        logger.error("FFmpeg hybrid render failed: %s", err)
        # Fallback: solid background render
        logger.warning("Falling back to solid-colour background render")
        return render_slideshow_with_images(
            image_paths=[],
            audio_path=audio_path,
            hook_text="",
            output_path=output_path,
            audio_duration=audio_duration,
            bg_color_hex=bg_color_hex,
            crossfade_dur=fade,
            srt_path=srt_path,
        )

    logger.info("Hybrid short rendered: %s (%.1f MB)",
                output_path, output_path.stat().st_size / 1024 / 1024)
    return output_path


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
    from database.db_extended import DatabaseExtended

    db = DatabaseExtended(settings.DATABASE_PATH)
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
