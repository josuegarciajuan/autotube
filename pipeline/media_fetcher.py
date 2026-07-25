"""Hybrid media fetcher: multi-provider video with fallback chain to images.

Replaces the old Pexels-only video fetching with a provider chain
(Pexels → Pixabay → Mixkit → YouTube CC). Fetches ONE media asset per
block, preferring video where configured, falling back through a chain
of image providers (Unsplash, Pexels Photos).

v2 (Jun 2026): fetches per enforceable scene (not raw LLM block), with a
ratio governor that targets a configurable video/image mix, image-URL
deduplication, and Pollo AI as an absolute last resort.
"""

import hashlib
import logging
import os
import random
import time
from pathlib import Path

import requests

from config import settings
from pipeline.image_fetcher import UnsplashProvider, PexelsProvider, PixabayImageProvider
from pipeline.providers.pexels import PexelsVideoProvider
from pipeline.providers.pixabay import PixabayVideoProvider
from pipeline.providers.mixkit import MixkitVideoProvider
from pipeline.providers.coverr import CoverrVideoProvider
from pipeline.providers.youtube_cc import YouTubeCCProvider
from pipeline.providers.base import VideoAsset

logger = logging.getLogger(__name__)

# New output dir for video clips
VIDEO_CLIPS_DIR = settings.OUTPUT_DIR / "video_clips"
VIDEO_CLIPS_DIR.mkdir(parents=True, exist_ok=True)

# ── Provider class registry ───────────────────────────────────────
_PROVIDER_CLASSES: dict[str, type] = {
    "pexels": PexelsVideoProvider,
    "pixabay": PixabayVideoProvider,
    "mixkit": MixkitVideoProvider,
    "coverr": CoverrVideoProvider,
    "youtube_cc": YouTubeCCProvider,
}

# Providers that work without an API key
_NO_KEY_PROVIDERS: set[str] = {"mixkit", "coverr", "youtube_cc"}


class MediaFetcher:
    """Orchestrates hybrid media fetching with multi-provider fallback chain.

    Video chain (when MEDIA_STRATEGY["prefer_video"] is True):
      1. Pexels Videos API → download .mp4
      2. Pixabay Videos API → download .mp4
      3. Mixkit (web scrape) → download .mp4
      4. YouTube Creative Commons (yt-dlp) → download .mp4

    Image fallback chain (when video fails or media_tipo is "imagen"):
      1. Unsplash Photos API → download .jpg
      2. Pexels Photos API → download .jpg
      3. [POLLO_AI_HOOK] → Pollo AI image generation (Agent 2B)
      4. Retry with simplified query (first 3 keywords only)
      5. Type-specific pre-baked fallback query
      6. Generic fallback query
      7. Return placeholder info

    Only ONE asset is downloaded per block — no waste.
    """

    # Pre-baked fallback queries by block type (guaranteed to find results)
    _FALLBACK_BY_TYPE: dict[str, str] = {
        "hook": "dark dramatic mystery atmosphere cinematic",
        "desarrollo": "empty institutional building documentary style",
        "climax": "dark shadow tension dramatic lighting",
        "reflexion": "contemplative silence empty space window light",
        "cierre": "light hope dawn breaking darkness",
    }

    def __init__(self, config=None) -> None:
        if config is None:
            from config.config_bridge import get_channel_config
            config = get_channel_config(settings.ACTIVE_CHANNELS[0])
        self._config = config
        self._media_strategy = getattr(self._config, "MEDIA_STRATEGY", {})

        # P3: Theme context for enriched search queries
        self._theme_context = None

        # ── Video provider chain ───────────────────────────────
        self.video_providers: list = []
        self._build_video_provider_chain()

        # ── Image providers (reuse from image_fetcher) ─────────
        self._unsplash: UnsplashProvider | None = None
        self._pixabay_img: PixabayImageProvider | None = None
        self._pexels: PexelsProvider | None = None  # legacy — Pexels API is dead, kept for graceful fallback
        self._unsplash_consecutive_empty = 0
        self._unsplash_disabled_until: float | None = None
        self._pixabay_img_consecutive_empty = 0
        self._pixabay_img_disabled_until: float | None = None

        # ── Video provider circuit breaker (2-tier) ─────────────
        # Tier 1 — Hard failures: network errors, 404, DNS failures
        #   → provider disabled IMMEDIATELY for entire phase (no retries).
        # Tier 2 — Soft failures: search() returns None (no results)
        #   → per-scene: don't retry provider within same scene
        #   → global: after 5 distinct-scene failures, disable for phase.
        self._vp_hard_fail: set[str] = set()          # network/404 → instant disable
        self._vp_soft_fail_scenes: dict[str, int] = {}  # distinct scenes failed
        self._vp_disabled: set[str] = set()            # union of hard_fail + soft threshold
        self._scene_tried_providers: set[str] = set()   # reset per scene

        if settings.UNSPLASH_ACCESS_KEY:
            self._unsplash = UnsplashProvider(settings.UNSPLASH_ACCESS_KEY)
        else:
            logger.warning("UNSPLASH_ACCESS_KEY not set — Unsplash disabled")

        if settings.PIXABAY_API_KEY:
            self._pixabay_img = PixabayImageProvider(settings.PIXABAY_API_KEY)
        else:
            logger.warning("PIXABAY_API_KEY not set — Pixabay images disabled")

        if self._unsplash is None and self._pixabay_img is None and not self.video_providers:
            logger.error("No media providers configured! Set UNSPLASH_ACCESS_KEY or PIXABAY_API_KEY")

        # ── Deduplication tracking ─────────────────────────────
        # Reset at the start of each fetch_for_script() call so the
        # same asset is never reused across multiple blocks within a
        # single video build.
        self._used_asset_urls: set[str] = set()
        # Image-specific dedup tracking (URLs + content hashes)
        self._used_image_urls: set[str] = set()
        # Enhanced dedup (v10): track by filename (e.g. pixabay_photo_6841384.jpg)
        # and by img_id (the provider's native ID). This catches duplicates
        # that have different CDN URLs but refer to the same image.
        self._used_filenames: set[str] = set()
        self._used_img_ids: set[str] = set()
        # Content hashes for true image dedup (bit-exact duplicate detection)
        self._used_content_hashes: set[str] = set()

        # ── Cross-video dedup (v9): filenames used in ANY previous video ─
        # Loaded at the start of each fetch_for_script() call from
        # the video_asset_history DB table. Empty at init time.
        self._cross_video_used_filenames: set[str] = set()

        # ── Pending asset records for flush_asset_history() ────────
        # Accumulated during fetch_for_script(); flushed by the
        # orchestrator/service after video_id is assigned.
        self._pending_asset_records: list[dict] = []

        # ── Bad URL cache (prevent retry storms on broken Pixabay CDN URLs) ──
        # URLs that returned error/HTML/non-JPEG are remembered for this
        # session to avoid retrying them hundreds of times. Reset on
        # fetch_for_script() boundary or after TTL expiry.
        self._bad_image_urls: set[str] = set()
        self._bad_image_urls_ts: float = 0.0

        # ── Pollo AI scene generator (lazy, avoids ~7 min per image unless absolutely needed) ─
        self._pollo_scene_gen = None
        self._ai_fallback_enabled = self._media_strategy.get("ai_image_fallback", False)

    def _load_cross_video_filenames(self) -> None:
        """Refresh the cross-video dedup set from the DB history table.

        Called at the start of each fetch_for_script() to ensure the set
        includes all assets from recently completed videos.
        Non-blocking: degrades gracefully to intra-video-only dedup.
        """
        try:
            from database.db_extended import ExtendedDatabase
            db = ExtendedDatabase()
            self._cross_video_used_filenames = db.get_all_used_filenames()
            if self._cross_video_used_filenames:
                logger.debug(
                    "Loaded %d cross-video used filenames for dedup",
                    len(self._cross_video_used_filenames),
                )
        except Exception:
            self._cross_video_used_filenames = set()

    @staticmethod
    def _query_variation(query: str, attempt: int = 0) -> str:
        """Modify a search query slightly to find different assets.

        Used when all assets returned by a provider are already used
        (cross-video dedup) — avoids placeholder fallbacks by trying
        different angles on the same topic.
        """
        variations = [
            "cinematic dramatic",
            "atmospheric detailed",
            "historic ancient",
            "wide establishing shot",
            "mysterious dark mood",
            "epic grand scale",
            "documentary archival",
        ]
        suffix = variations[attempt % len(variations)]
        return f"{query} {suffix}"

    def _record_asset_for_history(self, asset: dict) -> None:
        """Accumulate asset info for later flush to video_asset_history.

        Called automatically after every successful media download in
        fetch_for_script(). The caller (orchestrator) must invoke
        flush_asset_history() after video_id is assigned.
        """
        if asset and asset.get("path"):
            self._pending_asset_records.append({
                "path": asset.get("path", ""),
                "source": asset.get("source", ""),
                "url": asset.get("url", "") or asset.get("download_url", ""),
            })

    def flush_asset_history(self, db, video_id: int) -> int:
        """Record all pending assets for this video in the history table.

        Must be called after video_id is assigned (post phase_video).

        Returns number of records inserted.
        """
        count = 0
        try:
            from pipeline.cleanup_utils import record_asset_in_history
            for rec in self._pending_asset_records:
                record_asset_in_history(db, video_id, rec)
                count += 1
        except Exception as exc:
            logger.warning("flush_asset_history: error for video %d: %s", video_id, exc)
        finally:
            self._pending_asset_records.clear()
        if count:
            logger.info("Recorded %d assets for cross-video dedup (video %d)", count, video_id)
        return count

    def _get_pollo_scene_gen(self):
        """Return a SceneImageGenerator, creating it only on first access."""
        if self._pollo_scene_gen is None:
            try:
                from pipeline.ai_image_generator import SceneImageGenerator
                self._pollo_scene_gen = SceneImageGenerator()
                logger.info("Pollo AI scene generator initialized (last-resort fallback)")
            except Exception as exc:
                logger.warning("Pollo AI scene generator not available: %s", exc)
                self._pollo_scene_gen = False  # sentinel — don't retry
        return self._pollo_scene_gen if self._pollo_scene_gen is not False else None

    # ── Provider chain builder ────────────────────────────────────

    def _build_video_provider_chain(self) -> None:
        """Populate self.video_providers from MEDIA_STRATEGY["video_providers"].

        Supports the new multi-provider config key. Falls back to a
        single Pexels provider for backward compatibility when the
        ``video_providers`` key is absent.
        """
        provider_configs = self._media_strategy.get("video_providers")
        if provider_configs:
            # New multi-provider format (Oleada 3)
            for pcfg in provider_configs:
                name = pcfg.get("name", "")
                provider_cls = _PROVIDER_CLASSES.get(name)
                if provider_cls is None:
                    logger.warning("Unknown video provider %r — skipping", name)
                    continue

                api_key = None
                api_key_env = pcfg.get("api_key_env", "")
                if api_key_env:
                    api_key = os.getenv(api_key_env, "")

                # Skip API-key providers when the key is missing
                if not api_key and name not in _NO_KEY_PROVIDERS:
                    logger.warning(
                        "%s API key not set (%s) — skipping provider",
                        name, api_key_env,
                    )
                    continue

                try:
                    provider = provider_cls(api_key=api_key) if api_key else provider_cls()
                    self.video_providers.append(provider)
                    logger.info("Video provider registered: %s", name)
                except Exception as exc:
                    logger.warning("Failed to initialise provider %s: %s", name, exc)
            return

        # ── Backward-compatible fallback: single Pexels provider ─
        if settings.PEXELS_API_KEY:
            try:
                self.video_providers.append(PexelsVideoProvider(settings.PEXELS_API_KEY))
                logger.info("Video provider registered: pexels (legacy fallback)")
            except Exception as exc:
                logger.warning("Failed to initialise Pexels provider: %s", exc)
        else:
            logger.warning("PEXELS_API_KEY not set — video fetching disabled")

    def set_theme_context(self, ctx):
        """Set visual theme context for enriched search queries."""
        self._theme_context = ctx

    # ── Public API ────────────────────────────────────────────────

    def fetch_for_block(
        self,
        block: dict,
        theme_context=None,
        target_duration: float | None = None,
    ) -> dict:
        """Fetch ONE media asset for a block. Returns asset info dict.

        Args:
            block: Block dict from LLM output (tipo, texto, media_tipo, …).
            theme_context: Optional ThemeContext for enriched queries.
            target_duration: Desired scene duration in seconds. When provided
                the video search uses ``target_duration * 0.8`` as minimum
                and ``target_duration * 2.5`` as maximum so the clip is
                guaranteed to be long enough to fill the scene.

        Returns:
            Asset info dict: {path, type, duration, source}.
        """
        block_query = block.get("search_query_en", "")
        media_tipo = block.get("media_tipo", "imagen")
        target_dur = target_duration or block.get("media_duracion", 5)
        block_tipo = block.get("tipo", "desarrollo")

        # Build query: scene topic + video-level theme context, fitting Pixabay's 100-char limit
        ctx = theme_context or self._theme_context
        query = self._build_search_query(
            query=block_query,
            theme_keywords=ctx.theme_keywords_en if ctx else None,
        )
        logger.info("Built search query: %r (%d chars)", query, len(query))

        prefer_video = self._media_strategy.get("prefer_video", True) and media_tipo == "video"

        logger.info("Fetching media for block: tipo=%s query=%r dur=%.1fs",
                     media_tipo, query[:80], target_dur)

        # ── Step 1: Try video providers (in priority order) ──
        if prefer_video and self.video_providers:
            result = self._try_video_providers(query, target_dur)
            if result:
                return result

        # ── Step 2: Try Pixabay image (100 req/min — primary) ──
        result = self._try_image_pixabay(query)
        if result:
            return result

        # ── Step 3: Try Unsplash image (50 req/h — fallback) ──
        result = self._try_image_unsplash(query)
        if result:
            return result

        # [POLLO_AI_HOOK] ─ Agent 2B: Insert Pollo AI image generation fallback here
        # result = self._try_pollo_ai(query)
        # if result: return result

        # ── Step 4: Retry with simplified query (Pixabay + Unsplash) ──
        simple_query = self._simplify_query(query)
        if simple_query != query:
            logger.info("Retrying with simplified query: %r", simple_query)
            result = self._try_image_pixabay(simple_query) or \
                     self._try_image_unsplash(simple_query)
            if result:
                return result

        # ── Step 6.5: Pollo AI image generation (when enabled) ──
        if self._ai_fallback_enabled:
            logger.info("All stock providers exhausted — trying Pollo AI generation for [%s]", block_tipo)
            result = self._try_pollo_ai(query)
            if result:
                return result

        # ── Step 7: Placeholder ──────────────────────────────
        logger.warning("All providers exhausted for block [%s] — using placeholder", block_tipo)
        return {
            "path": None,
            "type": "placeholder",
            "duration": None,
            "source": "placeholder",
        }

    def fetch_for_script(
        self,
        bloques: list[dict] = None,
        theme_context=None,
        scene_ranges: list[dict] | None = None,
    ) -> list[dict]:
        """Fetch media for every scene in a script.

        **v2 (Jun 2026)**: when *scene_ranges* is provided (enforceable scene
        boundaries computed by ``video_editor._compute_block_ranges``), fetches
        ONE distinct asset per enforceable scene and applies the ratio governor.
        Falls back to the old per-block fetch for backward compatibility.

        Args:
            bloques: List of raw block dicts from LLM output (used when
                     *scene_ranges* is None — legacy path).
            theme_context: Optional ThemeContext for enriched queries.
            scene_ranges: List of enforceable scene range dicts (post merge/split)
                          from ``video_editor._compute_block_ranges``.

        Returns:
            List of asset info dicts (same order as scene_ranges).
        """
        ctx = theme_context or self._theme_context

        # Reset dedup tracking for this script
        self._used_asset_urls = set()
        self._used_image_urls = set()

        # Refresh cross-video dedup set from DB (includes assets from
        # recently completed/uploaded videos)
        self._load_cross_video_filenames()

        # Reset provider cooldowns for a fresh script
        self._unsplash_consecutive_empty = 0
        self._unsplash_disabled_until = None
        self._pixabay_img_consecutive_empty = 0
        self._pixabay_img_disabled_until = None

        # Reset bad URL cache for fresh script
        self._bad_image_urls.clear()
        self._bad_image_urls_ts = 0.0

        if not bloques:
            return []

        # Build scene_ranges from bloques if not provided (fallback for legacy callers)
        if not scene_ranges:
            scene_ranges = []
            for i, b in enumerate(bloques):
                scene_ranges.append({
                    "start": 0,
                    "end": b.get("media_duracion", 5),
                    "duration": b.get("media_duracion", 5),
                    "tipo": b.get("tipo", "desarrollo"),
                    "texto": b.get("texto", ""),
                    "media_tipo": b.get("media_tipo", "imagen"),
                    "media_duracion": b.get("media_duracion", 5),
                    "search_query_en": b.get("search_query_en", ""),
                    "asset_idx": i,
                })

        # ── v2: per-enforceable-scene fetch with ratio governor ──
        scenes = scene_ranges
        n_scenes = len(scenes)

        # ── Phase 0: compute video target ─────────────────────
        target_video_pct = self._media_strategy.get("target_video_pct", 50)
        max_video_pct = self._media_strategy.get("max_video_blocks_pct", 50)
        max_placeholder_pct = self._media_strategy.get("max_placeholder_pct", 0)
        target_video_count = max(1, round(target_video_pct / 100.0 * n_scenes))
        target_video_count = min(target_video_count, round(max_video_pct / 100.0 * n_scenes))
        target_video_count = min(target_video_count, n_scenes)
        # ── Hard cap: never exceed 50 video assets to prevent RAM exhaustion ──
        # Each downloaded video clip is ~10-50 MB. 50 videos ≈ 1-2 GB in-memory
        # before rendering. Beyond this, ffmpeg decoders risk OOM kills.
        MAX_ABSOLUTE_VIDEOS = 50
        video_ok = 0  # declared early for hard-cap check in the fetch loop
        logger.info(
            "Ratio governor: %d scenes, target %d video (%.0f%%), "
            "max placeholder %.0f%%, hard cap %d videos",
            n_scenes, target_video_count, target_video_pct,
            max_placeholder_pct, MAX_ABSOLUTE_VIDEOS,
        )

        # ── Phase 1: build priority list for video assignment ─
        # Priority:
        #   1) LLM tagged "video"
        #   2) hook/climax types (forced video for high-impact scenes)
        #   3) 1 in 3 desarrollo scenes (to ensure video variety, not all images)
        #   4) longest scenes benefit most from video
        scene_indices = list(range(n_scenes))
        
        # Force-assign video to ALL hook and climax scenes (structural guarantee)
        forced_video: set[int] = set()
        for idx in scene_indices:
            s = scenes[idx]
            if s.get("tipo") in ("hook", "climax") and not s.get("is_transition"):
                forced_video.add(idx)
            # Also force 1 in 3 desarrollo scenes
            elif s.get("tipo") == "desarrollo" and idx in scene_indices:
                # Force video for every 3rd desarrollo (positions 2, 5, 8... among desarrollo)
                des_idx = sum(1 for j in scene_indices if j <= idx and scenes[j].get("tipo") == "desarrollo")
                if des_idx % 3 == 0:
                    forced_video.add(idx)
        
        remaining_slots = max(0, target_video_count - len(forced_video))
        
        def _video_priority(idx: int) -> tuple[int, int, float]:
            s = scenes[idx]
            llm_video = 1 if s.get("media_tipo") == "video" else 0
            high_impact = 1 if s.get("tipo") in ("hook", "climax") else 0
            duration = s.get("duration", 5)
            return (-llm_video, -high_impact, -duration)

        # Fill remaining slots from priority queue (excluding already forced)
        remaining_queue = sorted(
            [idx for idx in scene_indices if idx not in forced_video],
            key=_video_priority,
        )
        additional_video = set(remaining_queue[:remaining_slots])
        video_assigned: set[int] = forced_video | additional_video
        
        logger.info(
            "Video slots assigned: forced=%d (+additional=%d) = %d total / %d scenes (LLM-video: %d, hook/climax forced: %d)",
            len(forced_video), len(additional_video), len(video_assigned),
            n_scenes,
            sum(1 for i in video_assigned if scenes[i].get("media_tipo") == "video"),
            sum(1 for i in forced_video if scenes[i].get("tipo") in ("hook", "climax")),
        )

        # ── Phase 2: fetch per scene ──────────────────────────
        results: list[dict] = [{} for _ in range(n_scenes)]
        # video_ok declared earlier (Phase 0) for hard-cap check
        image_ok = 0
        placeholder = 0

        # Pollo AI counter (capped at ai_max_per_video)
        ai_max = self._media_strategy.get("ai_max_per_video", 2)
        ai_used = 0
        ai_enabled = self._media_strategy.get("ai_image_fallback", False)

        # ── Phase timeout guard ───────────────────────────────────
        _cb_phase_start = time.time()
        _cb_phase_timeout = 1200  # 20 min hard timeout for the entire media phase

        # ── Reset video provider circuit breaker for this job ──
        self._vp_hard_fail.clear()
        self._vp_soft_fail_scenes.clear()
        self._vp_disabled.clear()
        self._scene_tried_providers.clear()

        # Track sub-scene sequence per asset_idx (preserved for backward compat)
        subscene_seq: dict[int, int] = {}

        # ── v10: reset enhanced dedup tracking ─────────────────
        self._used_filenames.clear()
        self._used_img_ids.clear()
        self._used_content_hashes.clear()

        # ── Hard abort counter: consecutive black/placeholder scenes ──
        _consecutive_black = 0
        _MAX_CONSECUTIVE_BLACK = 3

        for i, scene in enumerate(scenes):
            want_video = i in video_assigned
            target_dur = scene.get("duration", 5)
            scene_tipo = scene.get("tipo", "desarrollo")
            is_hook = (scene_tipo == "hook")

            # ── Transition scenes always use image (Ken Burns) ─
            if scene.get("is_transition"):
                want_video = False

            # ── RAM safety: hard-cap video assets to prevent OOM ──
            # When we hit the absolute video limit, force remaining scenes
            # to use only images (no video fallback in interleaved providers).
            _force_images = False
            if video_ok >= MAX_ABSOLUTE_VIDEOS:
                want_video = False
                _force_images = True

            logger.info(
                "Scene %d/%d [%s]: want_video=%s dur=%.1fs",
                i + 1, n_scenes, scene_tipo, want_video, target_dur,
            )

            asset = None

            # ── Pollo AI: hook siempre (si está activo y bajo cap) ─
            if is_hook and ai_enabled and ai_used < ai_max:
                query = scene.get("search_query_en", "")
                logger.info("Scene %d [HOOK]: using Pollo AI (%d/%d)", i + 1, ai_used + 1, ai_max)
                asset = self._try_pollo_scene(query, scene_tipo, ctx)
                if asset:
                    ai_used += 1

            # ── v10: Exhaustive search with cross-rotation + pagination ─
            if asset is None:
                query_pool = self._build_query_pool(scene, ctx)
                logger.info(
                    "Scene %d [%s]: %d query variations → exhaustive search",
                    i + 1, scene_tipo, len(query_pool),
                )
                asset = self._fetch_asset_exhaustive(
                    scene, query_pool, want_video, target_dur, ctx,
                    force_images=_force_images,
                )

            # ── Pollo AI: rescate si el stock falló y hay cupo ─
            if asset is None and ai_enabled and ai_used < ai_max:
                rescue_query = scene.get("search_query_en", "") or scene_tipo
                logger.info("Scene %d [%s]: stock exhausted — Pollo AI rescue (%d/%d)",
                            i + 1, scene_tipo, ai_used + 1, ai_max)
                asset = self._try_pollo_scene(rescue_query, scene_tipo, ctx)
                if asset:
                    ai_used += 1

            # ── Placeholder (absolutely nothing found) ────────
            if asset is None:
                _consecutive_black += 1
                logger.warning(
                    "Scene %d [%s]: ALL providers + all pages + all queries exhausted — "
                    "placeholder (%d/%d consecutive)",
                    i, scene_tipo, _consecutive_black, _MAX_CONSECUTIVE_BLACK,
                )

                # ── HARD ABORT: if 3+ consecutive black scenes, the video is invalid ──
                if _consecutive_black >= _MAX_CONSECUTIVE_BLACK:
                    error_msg = (
                        f"CRITICAL: {_consecutive_black} consecutive scenes with NO media. "
                        f"All providers, all pages, and all query variations exhausted "
                        f"at scene {i+1}/{n_scenes}. Aborting to prevent black/blank video."
                    )
                    logger.error(error_msg)
                    raise RuntimeError(error_msg)

                asset = {
                    "path": None,
                    "type": "placeholder",
                    "duration": None,
                    "source": "placeholder",
                }
            else:
                _consecutive_black = 0  # reset counter on success
                # Record for cross-video dedup
                self._record_asset_for_history(asset)

            # ── Count for stats ───────────────────────────────
            atype = asset.get("type", "?")
            if atype == "video":
                video_ok += 1
            elif atype == "image":
                image_ok += 1
            else:
                placeholder += 1

            results[i] = asset

            # ── Global timeout: abort if media phase takes too long ──
            elapsed = time.time() - _cb_phase_start
            if elapsed > _cb_phase_timeout:
                error_msg = (
                    f"CRITICAL: Media phase timeout after {elapsed:.0f}s "
                    f"(limit={_cb_phase_timeout}s). Processed {i+1}/{n_scenes} scenes. "
                    f"Aborting to prevent infinite retry loop."
                )
                logger.error(error_msg)
                raise RuntimeError(error_msg)

            # Rate limiting
            if i < n_scenes - 1:
                time.sleep(0.3)

        video_pct = (video_ok / max(n_scenes, 1)) * 100
        logger.info(
            "Scene fetch complete: %d video, %d image, %d placeholder, %d pollo_ai "
            "(%.0f%% video)",
            video_ok, image_ok, placeholder, ai_used, video_pct,
        )

        # ── Phase 3: sync scene_ranges media_tipo with actual results ──
        for i in range(min(len(scenes), len(results))):
            actual_type = results[i].get("type", scenes[i].get("media_tipo", "imagen"))
            scenes[i]["media_tipo"] = actual_type
        logger.info("Scene ranges media_tipo synced with fetched assets")

        # ── Phase 4: second pass if video quota below minimum ─────
        min_video_pct = self._media_strategy.get("min_video_pct", 0)
        if min_video_pct > 0 and video_pct < min_video_pct:
            needed = max(0, round(min_video_pct / 100.0 * n_scenes) - video_ok)
            logger.info(
                "Video quota below minimum (%.0f%% < %.0f%%) — "
                "second pass targeting %d more video scenes",
                video_pct, min_video_pct, needed,
            )

            # Re-try image-assigned scenes that had video slots originally
            # (but failed), using generic fallback queries. Skip placeholder
            # and transition scenes.
            rescued = 0
            generic_queries = self._media_strategy.get("video_fallback_queries", [])
            for i, scene in enumerate(scenes):
                if rescued >= needed:
                    break
                if scene.get("is_transition"):
                    continue
                if results[i].get("type") != "image":
                    continue
                # Original intent was video (from video_assigned)
                if i not in video_assigned:
                    continue

                target_dur = scene.get("duration", 5)
                # Try each generic query in order
                for fbq in generic_queries:
                    if rescued >= needed:
                        break
                    retry = self._try_video_providers(fbq, target_dur)
                    if retry:
                        results[i] = retry
                        scenes[i]["media_tipo"] = "video"
                        video_ok += 1
                        image_ok -= 1
                        rescued += 1
                        logger.info(
                            "Video rescue scene %d: %r → %s",
                            i + 1, fbq[:50], retry.get("source", "?"),
                        )
                        time.sleep(0.15)
                        break

            logger.info(
                "Second pass rescued %d video scenes (total video now: %d, %.0f%%)",
                rescued, video_ok, (video_ok / max(n_scenes, 1)) * 100,
            )

        return results

    # ── Pollo AI last resort ────────────────────────────────────

    def _try_pollo_scene(self, query: str, scene_tipo: str, ctx) -> dict | None:
        """Invoke Pollo AI image generation as absolute last resort."""
        pollo = self._get_pollo_scene_gen()
        if pollo is None:
            return None

        prompt = query
        if not prompt:
            # Build a minimal prompt from scene type if query is empty
            type_hints = {
                "hook": "dramatic cinematic opening scene dark atmosphere",
                "desarrollo": "atmospheric documentary b-roll storytelling",
                "climax": "intense dramatic peak moment tension climax",
                "reflexion": "contemplative peaceful atmospheric reflection",
                "cierre": "hopeful closing scene dawn light resolution",
            }
            prompt = type_hints.get(scene_tipo, "cinematic atmospheric scene 16:9")

        try:
            logger.info("Scene [%s]: invoking Pollo AI for %r", scene_tipo, prompt[:80])
            path = pollo.generate_scene_image(prompt, theme=ctx)
            if path and path.exists():
                return {
                    "path": path,
                    "type": "image",
                    "duration": None,
                    "source": "pollo_ai",
                }
        except Exception as exc:
            logger.warning("Pollo AI generation failed: %s", exc)

        return None

    # ── Internal: multi-provider video chain ──────────────────────

    def _try_video_providers(
        self,
        query: str,
        target_dur: float,
        skip_urls: set[str] | None = None,
    ) -> dict | None:
        """Try each video provider in priority order with a fallback chain.

        Chain:
        1. Exact query → all providers
        2. Simplified query (first 3-4 keywords) → retry all providers  
        3. Generic fallback queries (from MEDIA_STRATEGY or type-based) → retry
        4. YouTube CC with liberal license (no CC filter) as absolute last resort

        Args:
            skip_urls: Set of asset URLs to skip for deduplication.
                       Pass ``None`` to disable dedup (sub-scene fallback).
                       Default is ``self._used_asset_urls`` (full dedup).

        Returns:
            Asset info dict on success, or None if every attempt fails.
        """
        if not self.video_providers:
            return None

        # Use global dedup set if no explicit skip_urls provided
        if skip_urls is None:
            skip_urls = self._used_asset_urls

        # Scene-appropriate duration window
        min_dur = max(target_dur * 0.8, 1.0)   # at least 1s (safety floor)
        max_dur = target_dur * 4.0              # wider window for Mixkit compatibility

        # ── Attempt 1: exact query ───────────────────────────
        result = self._try_all_video_providers(query, min_dur, max_dur, skip_urls=skip_urls)
        if result:
            return result

        # ── v9: retry with query variation if all videos were deduped ─
        for var_attempt in range(2):
            var_query = self._query_variation(query, var_attempt)
            if var_query == query:
                continue
            logger.info("Video fallback: dedup exhausted — varied query %r", var_query[:80])
            result = self._try_all_video_providers(var_query, min_dur, max_dur, skip_urls=skip_urls)
            if result:
                return result

        # ── Attempt 2: simplified query ──────────────────────
        simple_query = self._simplify_query(query)
        if simple_query and simple_query != query:
            logger.info("Video fallback: trying simplified query %r", simple_query)
            result = self._try_all_video_providers(simple_query, min_dur, max_dur, skip_urls=skip_urls)
            if result:
                return result

        # ── Attempt 3: generic fallback queries ──────────────
        fallback_queries = self._media_strategy.get("video_fallback_queries", [])
        for fb_query in fallback_queries:
            logger.info("Video fallback: trying generic query %r", fb_query[:60])
            result = self._try_all_video_providers(fb_query, min_dur, max_dur, skip_urls=skip_urls)
            if result:
                return result

        # ── Attempt 4: YouTube CC with liberal license ───────
        # As absolute last resort, relax the CC requirement and
        # search YouTube broadly (no "creative commons" filter).
        for provider in self.video_providers:
            pname = getattr(provider, "name", "")
            if pname != "youtube_cc":
                continue
            if pname in self._vp_disabled or pname in self._scene_tried_providers:
                continue
            try:
                asset = provider.search(query, min_dur, max_dur, liberal_license=True)
                if asset is None:
                    continue
                if skip_urls is not None and asset.url in skip_urls:
                    continue
                if skip_urls is not None:
                    skip_urls.add(asset.url)
                self._used_asset_urls.add(asset.url)
                logger.info("Video found via youtube_cc (liberal): dur=%.1fs", asset.duration)
                path = self._download_video_asset(asset, provider)
                if path:
                    return {
                        "path": path,
                        "type": "video",
                        "duration": asset.duration,
                        "source": "youtube_cc_liberal",
                    }
            except Exception as exc:
                self._vp_hard_fail.add(pname)
                self._vp_disabled.add(pname)
                logger.warning(
                    "Circuit breaker [hard]: youtube_cc disabled — %s", exc)

        return None

    def _try_all_video_providers(
        self,
        query: str,
        min_dur: float,
        max_dur: float,
        skip_urls: set[str] | None = None,
    ) -> dict | None:
        """Try every video provider in priority order with the given query.

        Args:
            skip_urls: Set of asset URLs to skip for deduplication.
                       Pass ``None`` to disable dedup entirely (sub-scene fallback).

        Circuit breaker (2-tier):
            Tier 1 — Hard failures (network errors, 404, DNS failures): provider
                     disabled IMMEDIATELY for entire phase with no retries.
            Tier 2 — Soft failures (search returns None / no results): provider
                     skipped for THIS scene only (won't retry on subsequent
                     query levels). After 5 distinct-scene soft failures,
                     provider gets globally disabled for the phase.
        """
        if not query or not self.video_providers:
            return None

        for provider in self.video_providers:
            pname = getattr(provider, "name", str(provider))

            # ── Skip if disabled or already tried for this scene ──
            if pname in self._vp_disabled:
                continue
            if pname in self._scene_tried_providers:
                continue

            try:
                asset = provider.search(query, min_dur, max_dur)
                if asset is None:
                    # ── Soft failure: no results ──────────────
                    logger.debug("Provider %s: no results for query %r", pname, query[:60])
                    self._scene_tried_providers.add(pname)
                    continue

                # ── Deduplication check ─────────────────────
                if skip_urls is not None and asset.url in skip_urls:
                    logger.debug("Asset URL already used, skipping: %s", asset.url[:80])
                    self._scene_tried_providers.add(pname)
                    continue

                # ── Cross-video dedup check (v9) ────────────
                # Compute the filename this asset would produce and
                # skip if it was used by ANY previous video.
                url_hash = hashlib.md5(asset.url.encode()).hexdigest()[:12]
                predicted_filename = f"output/video_clips/{pname}_{url_hash}.mp4"
                if predicted_filename in self._cross_video_used_filenames:
                    logger.info(
                        "Video %s_%s: already used in previous video — skipping",
                        pname, url_hash,
                    )
                    self._scene_tried_providers.add(pname)
                    continue

                if skip_urls is not None:
                    skip_urls.add(asset.url)
                self._used_asset_urls.add(asset.url)

                logger.info("Video found via %s: dur=%.1fs url=%s",
                             provider.name, asset.duration, asset.url[:80])

                # ── Download the video clip ──────────────────
                path = self._download_video_asset(asset, provider)
                if path:
                    return {
                        "path": path,
                        "type": "video",
                        "duration": asset.duration,
                        "source": f"{provider.name}_video",
                    }

                # Download failed: treat as soft failure for this scene
                logger.warning("Provider %s: found video but download failed", provider.name)
                self._scene_tried_providers.add(pname)

            except Exception as exc:
                # ── Hard failure: network/404/DNS → instant global disable ──
                self._vp_hard_fail.add(pname)
                self._vp_disabled.add(pname)
                logger.warning(
                    "Circuit breaker [hard]: %s disabled for phase — %s: %s",
                    pname, type(exc).__name__, exc)

        # ── After loop: escalate soft failures to global if threshold hit ──
        # Track distinct-scene soft failures for each provider that was tried
        # and failed this round (added to _scene_tried_providers).
        # This runs once per _try_all_video_providers call, not per provider,
        # to avoid over-counting from the same scene.
        newly_failed = self._scene_tried_providers - self._vp_disabled
        for pname in newly_failed:
            count = self._vp_soft_fail_scenes.get(pname, 0) + 1
            self._vp_soft_fail_scenes[pname] = count
            if count >= 5:
                self._vp_disabled.add(pname)
                logger.warning(
                    "Circuit breaker [soft]: %s failed 5 distinct scenes — "
                    "disabled for remaining phase", pname)

        return None

        all_used = True  # Track if every provider returned an already-used asset
        
        for provider in self.video_providers:
            pname = getattr(provider, "name", str(provider))
            
            # ── Circuit breaker: skip disabled providers ──
            if pname in self._vp_disabled:
                continue
                
            try:
                # Use strict CC for normal searches (not liberal)
                asset = provider.search(query, min_dur, max_dur)
                if asset is None:
                    self._vp_fail_streak[pname] = self._vp_fail_streak.get(pname, 0) + 1
                    if self._vp_fail_streak[pname] >= 3:
                        self._vp_disabled.add(pname)
                        logger.warning(
                            "Circuit breaker: %s failed 3 times — disabled for this phase", pname)
                    continue
                
                # Reset streak on any successful find
                self._vp_fail_streak[pname] = 0

                # ── Deduplication check ─────────────────────────
                if skip_urls is not None and asset.url in skip_urls:
                    logger.info("Asset URL already used this script, skipping: %s", asset.url[:80])
                    all_used = True  # At least one provider found a result (even if duplicate)
                    continue
                if skip_urls is not None:
                    skip_urls.add(asset.url)
                self._used_asset_urls.add(asset.url)

                logger.info("Video found via %s: dur=%.1fs url=%s",
                             provider.name, asset.duration, asset.url[:80])

                # ── Download the video clip ──────────────────────
                path = self._download_video_asset(asset, provider)
                if path:
                    return {
                        "path": path,
                        "type": "video",
                        "duration": asset.duration,
                        "source": f"{provider.name}_video",
                    }

                logger.warning("Provider %s: found video but download failed", provider.name)

            except Exception as exc:
                self._vp_fail_streak[pname] = self._vp_fail_streak.get(pname, 0) + 1
                logger.warning("Provider %s failed: %s", provider.name, exc)
                if self._vp_fail_streak[pname] >= 3:
                    self._vp_disabled.add(pname)
                    logger.warning(
                        "Circuit breaker: %s failed 3 times — disabled for this phase", pname)
                continue

        return None

        for provider in self.video_providers:
            try:
                # Use strict CC for normal searches (not liberal)
                asset = provider.search(query, min_dur, max_dur)
                if asset is None:
                    continue

                # ── Deduplication check ─────────────────────────
                if skip_urls is not None and asset.url in skip_urls:
                    logger.info("Asset URL already used this script, skipping: %s", asset.url[:80])
                    continue
                if skip_urls is not None:
                    skip_urls.add(asset.url)
                self._used_asset_urls.add(asset.url)

                logger.info("Video found via %s: dur=%.1fs url=%s",
                             provider.name, asset.duration, asset.url[:80])

                # ── Download the video clip ──────────────────────
                path = self._download_video_asset(asset, provider)
                if path:
                    return {
                        "path": path,
                        "type": "video",
                        "duration": asset.duration,
                        "source": f"{provider.name}_video",
                    }

                logger.warning("Provider %s: found video but download failed", provider.name)

            except Exception as exc:
                logger.warning("Provider %s failed: %s", provider.name, exc)
                continue

        return None

    def _download_video_asset(self, asset: VideoAsset, provider) -> Path | None:
        """Download a VideoAsset using the provider's download method.

        Falls back to direct HTTP download if the provider download raises
        an exception.  Validates the downloaded file to avoid caching HTML
        pages as video files (e.g. when a provider sets ``url`` to a page
        URL instead of a CDN link).
        """
        url_hash = hashlib.md5(asset.url.encode()).hexdigest()[:12]

        # ── Phase 1: direct HTTP download (fast, handles most CDNs) ──
        filename = f"{provider.name}_{url_hash}.mp4"
        path = self._download_video(asset.url, filename)
        if path and self._is_valid_video(path):
            # ── Pre-transcode: downscale >1080p videos at download time ──
            # This prevents MoviePy from ever decoding 4K (4096x2160) or
            # 2.7K (2732x1440) raw RGB frames (~25 MB each), which would
            # cause ffmpeg decoder RSS to spike to 1.2 GB+. By transcoding
            # at download time, all source clips are ≤1080p → ~300 MB max
            # per decoder, safe for 2-3 simultaneous renders.
            path = self._pre_transcode_if_needed(path)
            return path
        if path:
            # Looks bogus — delete and don't cache HTML as video
            logger.warning("Downloaded file does not look like video, discarding: %s", path)
            path.unlink(missing_ok=True)

        # ── Phase 2: provider-specific download (handles yt-dlp, etc.) ──
        try:
            return provider.download(asset, VIDEO_CLIPS_DIR)
        except Exception as exc:
            logger.warning("%s provider download failed: %s", provider.name, exc)
            return None

    @staticmethod
    def _is_valid_video(path: Path) -> bool:
        """Quick check: does the file look like a video, not an HTML page?
        
        Also runs ffprobe to verify the file is not corrupted internally
        (header may look fine but the stream could be damaged).
        
        Additionally decodes sample frames from start, middle, and end
        of the video to detect partially corrupt downloads (valid header
        but damaged frame data — common with Pexels CDN).
        """
        try:
            import subprocess as _sp
            
            header = path.read_bytes()[:200]
            # Common video container signatures
            if header[:4] == b'\x00\x00\x00\x18':  # MP4 (ftyp box)
                pass
            elif header[:3] == b'\x1a\x45\xdf':      # WebM / Matroska
                pass
            elif header[:4] == b'RIFF':                # AVI
                pass
            else:
                # Looks like HTML or JSON — definitely not a video
                if header.startswith(b'<!') or header.startswith(b'<html') or header.startswith(b'{'):
                    return False
                # Unknown header — be conservative and accept if it's large enough
                if path.stat().st_size <= 50000:
                    return False
            
            # ── Deep validation with ffprobe ────────────────────
            # Header may be valid MP4 but the file could be truncated/corrupt.
            # ffprobe actually decodes the stream to verify integrity.
            result = _sp.run(
                ["ffprobe", "-v", "error", "-show_entries", "format=duration",
                 "-of", "csv=p=0", str(path)],
                capture_output=True, text=True, timeout=15,
            )
            if result.returncode != 0 or not result.stdout.strip():
                logger.warning(
                    "ffprobe validation failed for %s (code=%d): %s",
                    path.name, result.returncode, result.stderr[:200],
                )
                return False
            
            duration = float(result.stdout.strip())
            if duration <= 0:
                logger.warning("Video %s has zero duration — corrupt", path.name)
                return False
            
            # ── Frame-level decode validation ─────────────────────
            # ffprobe check above only validates the container header.
            # Corrupt frames (partially downloaded streams, CDN glitches)
            # cause MoviePy to silently freeze on the last valid frame
            # or crash the render entirely with BrokenPipeError.
            # We decode 3 short segments (start / middle / end) to
            # catch both early-onset and tail corruption.
            if duration > 6.0:
                mid_point = duration / 2.0
                check_points = [
                    ("start", 0.0),
                    ("middle", mid_point),
                    ("end", max(0, duration - 3.0)),
                ]
            elif duration > 3.0:
                check_points = [("start", 0.0), ("end", max(0, duration - 2.0))]
            else:
                check_points = [("start", 0.0)]
            
            for label, ss in check_points:
                result = _sp.run(
                    ["ffmpeg", "-v", "error", "-ss", str(ss), "-i", str(path),
                     "-t", "2", "-f", "null", "-"],
                    capture_output=True, text=True, timeout=30,
                )
                if result.returncode != 0 or result.stderr.strip():
                    logger.warning(
                        "Frame decode error at %s (%.1fs) for %s: %s",
                        label, ss, path.name,
                        result.stderr[:200].strip() if result.stderr else f"exit code {result.returncode}",
                    )
                    return False
            
            return True
        except Exception as e:
            logger.warning("Video validation exception for %s: %s", path.name, e)
            return False

    @staticmethod
    def _pre_transcode_if_needed(path: Path) -> Path:
        """Downscale videos exceeding 1080p at download time.

        Returns the original path if already ≤1080p or if transcoding fails.
        On success, replaces the file in-place with a 1080p version using
        ultrafast preset and CRF 23 to preserve quality while reducing
        decoding memory from ~1.2 GB (4K) to ~300 MB (1080p) per source.
        """
        try:
            import subprocess as _sp
            result = _sp.run(
                ["ffprobe", "-v", "error", "-select_streams", "v:0",
                 "-show_entries", "stream=width,height",
                 "-of", "csv=p=0", str(path)],
                capture_output=True, text=True, timeout=10,
            )
            if result.returncode != 0 or not result.stdout.strip():
                return path
            parts = result.stdout.strip().split(",")
            w, h = int(parts[0]), int(parts[1])
            if w <= 1920 and h <= 1080:
                return path  # already 1080p or lower

            # Transcode to 1080p
            tmp = path.with_suffix(".transcode_tmp.mp4")
            rc = _sp.run(
                ["ffmpeg", "-y", "-i", str(path),
                 "-vf", "scale=1920:1080:force_original_aspect_ratio=decrease,pad=1920:1080:(ow-iw)/2:(oh-ih)/2",
                 "-c:v", "libx264", "-preset", "ultrafast", "-crf", "23",
                 "-c:a", "aac", "-b:a", "128k",
                 "-movflags", "+faststart",
                 str(tmp)],
                capture_output=True, text=True, timeout=120,
            )
            if rc.returncode == 0 and tmp.exists() and tmp.stat().st_size > 1000:
                orig_size_mb = path.stat().st_size / 1024 / 1024
                new_size_mb = tmp.stat().st_size / 1024 / 1024
                tmp.replace(path)  # atomic on same filesystem
                logger.info(
                    "Pre-transcoded %s: %dx%d → 1920x1080 (%.1f MB → %.1f MB)",
                    path.name, w, h, orig_size_mb, new_size_mb,
                )
                return path
            else:
                tmp.unlink(missing_ok=True)
                logger.warning("Pre-transcode failed for %s, using original", path.name)
                return path
        except Exception:
            logger.exception("Pre-transcode check failed for %s", path.name)
            return path

    def _download_video(self, url: str, filename: str) -> Path | None:
        """Download a video clip to VIDEO_CLIPS_DIR. Cached."""
        filepath = VIDEO_CLIPS_DIR / filename
        if filepath.exists():
            logger.info("Video already cached: %s", filepath)
            return filepath

        try:
            resp = requests.get(url, timeout=60, stream=True)
            resp.raise_for_status()
            filepath.write_bytes(resp.content)
            logger.info("Downloaded video: %s (%.1f MB)",
                         filepath, len(resp.content) / 1024 / 1024)
            return filepath
        except Exception as exc:
            logger.error("Video download failed %s: %s", url, exc)
            return None

    # ── Enhanced dedup & exhaustive search (v10) ─────────────────

    def _is_asset_duplicate(self, asset_info: dict) -> bool:
        """Check if an asset (image or video) is a duplicate.

        Checks by: download URL, predicted filename, img_id, and content hash.
        Returns True if this asset has already been used in the current video.
        """
        url = asset_info.get("url", "") or asset_info.get("download_url", "")
        if url and url in self._used_asset_urls:
            return True
        if url and url in self._used_image_urls:
            return True

        img_id = str(asset_info.get("id", ""))
        source = asset_info.get("source", "") or asset_info.get("provider", "")
        if img_id and f"{source}_{img_id}" in self._used_img_ids:
            return True

        # Predict filename as it would be saved on disk
        predicted_fn = self._predict_filename(asset_info)
        if predicted_fn and predicted_fn in self._used_filenames:
            return True
        if predicted_fn and predicted_fn in self._cross_video_used_filenames:
            return True

        content_hash = asset_info.get("content_hash", "")
        if content_hash and content_hash in self._used_content_hashes:
            return True

        return False

    def _record_asset_used(self, asset_info: dict) -> None:
        """Record an asset in all dedup tracking sets."""
        url = asset_info.get("url", "") or asset_info.get("download_url", "")
        if url:
            self._used_asset_urls.add(url)
            self._used_image_urls.add(url)

        img_id = str(asset_info.get("id", ""))
        source = asset_info.get("source", "") or asset_info.get("provider", "")
        if img_id:
            self._used_img_ids.add(f"{source}_{img_id}")

        predicted_fn = self._predict_filename(asset_info)
        if predicted_fn:
            self._used_filenames.add(predicted_fn)

        content_hash = asset_info.get("content_hash", "")
        if content_hash:
            self._used_content_hashes.add(content_hash)

    def _predict_filename(self, asset_info: dict) -> str:
        """Predict the filename this asset would have on disk after download."""
        url = asset_info.get("url", "") or asset_info.get("download_url", "")
        source = asset_info.get("source", "") or asset_info.get("provider", "")
        img_id = str(asset_info.get("id", ""))
        atype = asset_info.get("type", "image")

        if atype == "video":
            url_hash = hashlib.md5(url.encode()).hexdigest()[:12] if url else ""
            return f"output/video_clips/{source}_{url_hash}.mp4" if url_hash else ""
        else:
            # image
            if img_id:
                return f"output/images/{source}_{img_id}.jpg"
            elif url:
                url_hash = hashlib.md5(url.encode()).hexdigest()[:12]
                return f"output/images/{source}_{url_hash}.jpg"
        return ""

    # ── Query pool builder ──────────────────────────────────────

    def _build_query_pool(self, scene: dict, ctx) -> list[str]:
        """Build an ordered list of query variations for a scene.

        Starts with the most specific and ends with generic fallbacks.
        Each variation is a fresh attempt — if the first queries return
        only duplicates, later queries with different wording should
        produce different results from the providers.

        Returns deduplicated list of non-empty queries (~11 variants).
        """
        base = scene.get("search_query_en", "")

        pool = []
        if base and base.strip():
            pool.append(base.strip())

        scene_tipo = scene.get("tipo", "desarrollo")

        # 3 directional variations for the same topic
        if base:
            for suffix in [
                "wide shot establishing",
                "close-up detail",
                "alternative angle composition",
                "low angle dramatic",
                "distant view atmospheric",
            ]:
                v = f"{base} {suffix}"
                if len(v) <= 100:  # Pixabay 100-char limit
                    pool.append(v)

        # Simplified: just the key nouns (3-4 words, no style modifiers)
        simple = self._simplify_query(base) if base else ""
        if simple and simple != base and simple.strip():
            pool.append(simple)

        # Without theme keywords (v6) — theme injection can poison queries
        if ctx and ctx.theme_keywords_en and base:
            clean = _strip_theme_keywords(base, ctx.theme_keywords_en)
            if clean and clean != base and clean.strip():
                pool.append(clean)

        # Type-specific fallback queries
        type_fb = self._FALLBACK_BY_TYPE.get(scene_tipo, "")
        if type_fb and type_fb not in pool:
            pool.append(type_fb)

        # Channel-configured fallback queries
        fallbacks = self._media_strategy.get("fallback_queries", [
            "historical documentary archival photography cinematic 16:9",
            "ancient history artifacts museum exhibition documentary",
            "nature landscape exploration discovery documentary cinematic",
            "dramatic wilderness storm ocean survival documentary",
            "old architecture cathedral historical building documentary",
            "dark mystery abandoned exploration atmosphere cinematic",
        ])
        for fb in fallbacks:
            if fb and fb.strip() and fb not in pool:
                pool.append(fb.strip())

        # Remove empty/whitespace and deduplicate while preserving order
        seen = set()
        result = []
        for q in pool:
            qs = q.strip()
            if qs and qs not in seen:
                seen.add(qs)
                result.append(qs)

        return result

    # ── Exhaustive asset search with cross-rotation + pagination ─

    def _fetch_asset_exhaustive(
        self, scene: dict, query_pool: list[str],
        want_video: bool, target_dur: float, ctx,
        force_images: bool = False,
    ) -> dict | None:
        """Search exhaustively for a non-duplicate asset.

        Algorithm:
          1. For each query in the pool (specific → generic):
             a. Interleave providers (image+video or video+image based on want_video)
             b. For each provider, paginate through ALL available pages
             c. For each result, check dedup → first fresh one wins
             d. If all pages exhausted → next provider
             e. If all providers exhausted → next query
          2. If absolutely nothing found after all queries → return None

        This guarantees that we only give up after exhausting:
          ~11 queries × ~6 providers × ~10 pages each ≈ 660 candidate evaluations
        """
        providers = self._interleaved_providers(want_video, scene, force_images=force_images)

        for query in query_pool:
            if not query or not query.strip():
                continue

            for provider in providers:
                page = 1
                while True:
                    asset_candidates = self._search_provider_page(
                        provider, query, target_dur, page, want_video,
                    )

                    if not asset_candidates:
                        break  # no more results from this provider

                    for candidate in asset_candidates:
                        if not self._is_asset_duplicate(candidate):
                            # Download the asset
                            downloaded = self._download_candidate(provider, candidate)
                            if downloaded and downloaded.get("path"):
                                self._record_asset_used(candidate)
                                self._record_asset_for_history(downloaded)
                                return downloaded

                    # Check if there are more pages
                    total = asset_candidates[0].get("_total_available", 0)
                    per_page = asset_candidates[0].get("_per_page", 20)
                    if total <= 0 or page * per_page >= total:
                        break
                    page += 1
                    time.sleep(0.15)

        return None

    def _interleaved_providers(self, want_video: bool, scene: dict, force_images: bool = False) -> list:
        """Return providers in interleaved order: preferred type first, then the other.

        This ensures that even when a scene wants an image, video providers are
        tried after image providers are exhausted (cross-rotation).

        When force_images is True (RAM safety cap), video providers are excluded
        entirely to prevent OOM from too many downloaded video assets.
        """
        is_transition = scene.get("is_transition", False)
        if is_transition:
            # Transitions always use images (Ken Burns)
            return self._get_all_image_providers()

        image_providers_list = self._get_all_image_providers()

        if force_images:
            # RAM safety cap: only image providers, no video fallback
            return image_providers_list

        video_providers_list = list(self.video_providers) if self.video_providers else []

        if want_video:
            return video_providers_list + image_providers_list
        else:
            return image_providers_list + video_providers_list

    def _get_all_image_providers(self) -> list:
        """Return all active image providers as a list for iteration."""
        providers = []
        if self._pixabay_img:
            providers.append(self._pixabay_img)
        if self._unsplash:
            providers.append(self._unsplash)
        return providers

    def _search_provider_page(
        self, provider, query: str, target_dur: float,
        page: int, want_video: bool,
    ) -> list[dict]:
        """Search a single provider on a single page.

        Returns a list of candidate asset dicts with dedup metadata.
        Each dict includes: _total_available, _per_page for pagination control.
        """
        candidates: list[dict] = []

        # Detect provider type
        from pipeline.providers.base import BaseVideoProvider
        from pipeline.image_fetcher import ImageProvider

        if isinstance(provider, BaseVideoProvider):
            candidates = self._search_video_provider_page(
                provider, query, target_dur, page,
            )
        elif isinstance(provider, ImageProvider):
            candidates = self._search_image_provider_page(
                provider, query, page,
            )

        return candidates

    def _search_video_provider_page(
        self, provider, query: str, target_dur: float, page: int,
    ) -> list[dict]:
        """Search a video provider on one page using search_page()."""
        pname = getattr(provider, "name", str(provider))

        # Skip disabled providers
        if pname in self._vp_disabled:
            return []

        min_dur = max(target_dur * 0.8, 1.0)
        max_dur = target_dur * 4.0

        try:
            sp = provider.search_page(query, min_dur, max_dur, page=page, per_page=50)
            if not sp or not sp.assets:
                return []

            candidates = []
            for asset in sp.assets:
                candidates.append({
                    "id": "",  # video providers use URL as key
                    "url": asset.url,
                    "type": "video",
                    "duration": asset.duration,
                    "source": f"{pname}_video",
                    "provider": pname,
                    "_total_available": sp.total_available,
                    "_per_page": sp.per_page,
                })
            return candidates
        except Exception as exc:
            logger.debug("Video provider %s page %d failed: %s", pname, page, exc)
            return []

    def _search_image_provider_page(
        self, provider, query: str, page: int,
    ) -> list[dict]:
        """Search an image provider on one page using search_paginated()."""
        pname = provider.__class__.__name__.lower()

        try:
            results, total_available = provider.search_paginated(
                query, n=50, page=page,
            )
            if not results:
                return []

            candidates = []
            for img in results:
                candidates.append({
                    "id": str(img.get("id", "")),
                    "url": img.get("download_url", ""),
                    "type": "image",
                    "source": "pixabay_photo" if "pixabay" in pname else
                             "unsplash" if "unsplash" in pname else
                             "pexels_photo",
                    "provider": pname,
                    "download_url": img.get("download_url", ""),
                    "fallback_download_url": img.get("fallback_download_url"),
                    "_total_available": total_available,
                    "_per_page": 50,
                })
            return candidates
        except Exception as exc:
            logger.debug("Image provider %s page %d failed: %s", pname, page, exc)
            return []

    def _download_candidate(self, provider, candidate: dict) -> dict | None:
        """Download a candidate asset and return the asset dict with path."""
        atype = candidate.get("type", "image")
        source = candidate.get("source", "")

        if atype == "video":
            url = candidate.get("url", "")
            if not url:
                return None
            url_hash = hashlib.md5(url.encode()).hexdigest()[:12]
            filename = f"{source}_{url_hash}.mp4"
            from pipeline.providers.base import VideoAsset
            asset = VideoAsset(
                url=url, file_path=Path(), duration=candidate.get("duration", 5),
                resolution=(1920, 1080), provider=source,
            )
            path = self._download_video_asset(asset, provider)
            if path:
                return {
                    "path": str(path), "type": "video",
                    "duration": candidate.get("duration"),
                    "source": source,
                }

        elif atype == "image":
            download_url = candidate.get("download_url", "") or candidate.get("url", "")
            if not download_url:
                return None
            img_id = candidate.get("id", "")
            if img_id:
                filename = f"{source}_{img_id}.jpg"
            else:
                url_hash = hashlib.md5(download_url.encode()).hexdigest()[:12]
                filename = f"{source}_{url_hash}.jpg"

            path = self._download_image(download_url, filename)
            if path:
                return {
                    "path": str(path), "type": "image",
                    "duration": None, "source": source,
                }
            # Pixabay fallback: retry with webformatURL
            fb_url = candidate.get("fallback_download_url")
            if fb_url and fb_url != download_url:
                fb_filename = f"{source}_{img_id}_fb.jpg" if img_id else filename
                path = self._download_image(fb_url, fb_filename)
                if path:
                    return {
                        "path": str(path), "type": "image",
                        "duration": None, "source": source,
                    }

        return None

    # ── Internal: images ──────────────────────────────────────────

    def _try_image_unsplash(self, query: str, skip_urls: set[str] | None = None) -> dict | None:
        """Try Unsplash for an image, optionally skipping previously used URLs."""
        if not self._unsplash:
            return None
        if not query or not query.strip():
            return None  # empty query → no point searching
        if self._unsplash_disabled_until and time.time() < self._unsplash_disabled_until:
            return None  # provider is under cooldown
        result = self._fetch_image(self._unsplash, query, "unsplash", skip_urls=skip_urls)
        if result is None:
            self._unsplash_consecutive_empty += 1
            if self._unsplash_consecutive_empty >= 3:
                # After 3 consecutive empty results (likely 403/429), disable
                # Unsplash for 30 minutes to stop wasting HTTP calls
                self._unsplash_disabled_until = time.time() + 1800
                logger.error(
                    "Unsplash: %d consecutive empty results — provider DISABLED "
                    "for 30 min (likely API key revoked or rate limited)",
                    self._unsplash_consecutive_empty,
                )
        else:
            self._unsplash_consecutive_empty = 0
            self._unsplash_disabled_until = None
        return result

    def _try_image_pixabay(self, query: str, skip_urls: set[str] | None = None) -> dict | None:
        """Try Pixabay Photos for an image, optionally skipping previously used URLs."""
        if not self._pixabay_img:
            return None
        if not query or not query.strip():
            return None
        if self._pixabay_img_disabled_until and time.time() < self._pixabay_img_disabled_until:
            return None  # provider is under cooldown
        result = self._fetch_image(self._pixabay_img, query, "pixabay_photo", skip_urls=skip_urls)
        if result is None:
            self._pixabay_img_consecutive_empty += 1
            if self._pixabay_img_consecutive_empty >= 10:
                # Pixabay has 100 req/min — 10 consecutive empties = likely API issue
                self._pixabay_img_disabled_until = time.time() + 600
                logger.error(
                    "Pixabay images: %d consecutive empty results — provider DISABLED "
                    "for 10 min",
                    self._pixabay_img_consecutive_empty,
                )
        else:
            self._pixabay_img_consecutive_empty = 0
            self._pixabay_img_disabled_until = None
        return result

    def _try_image_pexels(self, query: str, skip_urls: set[str] | None = None) -> dict | None:
        """Try Pexels for an image, optionally skipping previously used URLs."""
        if not self._pexels:
            return None
        if not query or not query.strip():
            return None  # empty query → Pexels returns 400, skip early
        if self._pexels_disabled_until and time.time() < self._pexels_disabled_until:
            return None  # provider is under cooldown
        result = self._fetch_image(self._pexels, query, "pexels_photo", skip_urls=skip_urls)
        if result is None:
            self._pexels_consecutive_empty += 1
            if self._pexels_consecutive_empty >= 5:
                # After 5 consecutive empty results (likely 429 rate limit),
                # disable Pexels for 10 min
                self._pexels_disabled_until = time.time() + 600
                logger.error(
                    "Pexels: %d consecutive empty results — provider DISABLED "
                    "for 10 min (likely rate limited 429)",
                    self._pexels_consecutive_empty,
                )
        else:
            self._pexels_consecutive_empty = 0
            self._pexels_disabled_until = None
        return result

    def _fetch_image(self, provider, query: str, source: str,
                     skip_urls: set[str] | None = None) -> dict | None:
        """Fetch one image from a provider, requesting extra results to
        skip duplicates when *skip_urls* is provided.

        For Pixabay images, if the primary download_url (largeImageURL) fails,
        retries automatically with fallback_download_url (webformatURL).
        """
        if not query or not query.strip():
            return None
        try:
            n_request = 15 if skip_urls else 1
            results = provider.search(query, n=n_request)
            if not results:
                return None

            # When dedup is active, skip already-used URLs until we find a fresh one
            for img in results:
                download_url = img.get("download_url", "")
                if not download_url:
                    continue
                if skip_urls and download_url in skip_urls:
                    img_id = img.get("id", "?")
                    logger.info("Skipping duplicate %s image id=%s", source, img_id)
                    continue

                img_id = str(img.get("id", hashlib.md5(download_url.encode()).hexdigest()[:12]))

                # ── Cross-video dedup check (v9) ────────────
                predicted_filename = f"output/images/{source}_{img_id}.jpg"
                if predicted_filename in self._cross_video_used_filenames:
                    logger.info(
                        "Image %s: already used in previous video — skipping %s",
                        source, img_id,
                    )
                    continue

                path = self._download_image(download_url, f"{source}_{img_id}.jpg")
                if path:
                    if skip_urls is not None:
                        skip_urls.add(download_url)
                        self._used_image_urls.add(download_url)
                    return {
                        "path": path,
                        "type": "image",
                        "duration": None,
                        "source": source,
                    }

                # ── Pixabay fallback: retry with webformatURL if largeImageURL failed ──
                fallback_url = img.get("fallback_download_url")
                if fallback_url and fallback_url != download_url:
                    logger.info(
                        "Pixabay largeImageURL failed — retrying with webformatURL "
                        "(id=%s)", img_id,
                    )
                    fb_path = self._download_image(fallback_url, f"{source}_{img_id}_fb.jpg")
                    if fb_path:
                        if skip_urls is not None:
                            skip_urls.add(fallback_url)
                            self._used_image_urls.add(fallback_url)
                        return {
                            "path": fb_path,
                            "type": "image",
                            "duration": None,
                            "source": source,
                        }

            if skip_urls:
                logger.warning("All %d %s results were duplicates — none fresh", n_request, source)
            return None
        except Exception as exc:
            logger.warning("%s image fetch failed: %s", source, exc)

        return None

    def _download_image(self, url: str, filename: str) -> Path | None:
        """Download image to IMAGES_DIR. Cached. Validates JPEG integrity.

        For Pixabay domains, adds a Referer header (largeImageURL CDN
        may reject requests without it). Downloads are validated:
        - Must be 50+ KB (reject HTML/error pages)
        - Must have JPEG magic bytes (ff d8 ff)
        - Must have at least 800px on the shorter axis (Ken Burns minimum)
        """
        # ── Bad URL cache: skip URLs that already failed this session ──
        import time as _time
        if url in self._bad_image_urls:
            # TTL check: expire cache after 10 min to allow recovery
            if _time.time() - self._bad_image_urls_ts < 600:
                # Skip silently — caller treats None as "no valid asset" and moves on
                return None
            else:
                # TTL expired — clear cache and retry
                self._bad_image_urls.clear()
                self._bad_image_urls_ts = _time.time()

        filepath = settings.IMAGES_DIR / filename
        if filepath.exists():
            # Re-validate cached images on each hit (catches corrupt cache)
            if self._is_valid_image(filepath):
                logger.info("Image already cached: %s", filepath)
                return filepath
            else:
                logger.warning("Cached image %s is invalid — re-downloading", filepath)
                filepath.unlink(missing_ok=True)

        try:
            headers = {}
            # Pixabay CDN often requires a Referer for largeImageURL downloads
            if "pixabay" in url.lower():
                headers["Referer"] = "https://pixabay.com/"

            resp = requests.get(url, timeout=30, stream=True, headers=headers)
            resp.raise_for_status()

            # Quick size check before writing
            content = resp.content
            content_len = len(content)
            if content_len < 50000:
                logger.error(
                    "Image download too small (%d bytes) — likely HTML/error page: %s",
                    content_len, url[:100],
                )
                self._bad_image_urls.add(url)
                self._bad_image_urls_ts = _time.time()
                return None

            # JPEG magic bytes check
            if content[:3] != b'\xff\xd8\xff':
                logger.error(
                    "Downloaded content is not JPEG (magic=%r) — discarding: %s",
                    content[:8], url[:100],
                )
                self._bad_image_urls.add(url)
                self._bad_image_urls_ts = _time.time()
                return None

            settings.IMAGES_DIR.mkdir(parents=True, exist_ok=True)
            filepath.write_bytes(content)

            # Dimension check for Ken Burns viability
            try:
                from PIL import Image
                from io import BytesIO
                pil_img = Image.open(BytesIO(content))
                w, h = pil_img.size
                min_dim = min(w, h)
                if min_dim < 800:
                    logger.warning(
                        "Image %s is %dx%d — below 800px minimum for Ken Burns zoom",
                        filename, w, h,
                    )
                    # Don't reject — still usable if closest match — but warn
                pil_img.close()
            except Exception:
                logger.warning("Could not verify image dimensions for %s", filename)

            logger.info("Downloaded image: %s (%d bytes)", filepath, content_len)
            return filepath
        except Exception as exc:
            logger.error("Image download failed %s: %s", url, exc)
            self._bad_image_urls.add(url)
            self._bad_image_urls_ts = _time.time()
            return None

    @staticmethod
    def _is_valid_image(filepath: Path) -> bool:
        """Quick validation: is the cached file a real JPEG with adequate size?"""
        try:
            if not filepath.exists():
                return False
            size = filepath.stat().st_size
            if size < 50000:
                return False
            header = filepath.read_bytes()[:4]
            return header[:3] == b'\xff\xd8\xff'
        except Exception:
            return False

    # ── Internal: Pollo AI image generation ────────────────────────

    def _try_pollo_ai(self, query: str) -> dict | None:
        """Generate an image via Pollo AI as a last-resort fallback.

        Only invoked when *all* stock providers have been exhausted and
        ``ai_image_fallback`` is enabled in MEDIA_STRATEGY.

        Uses the existing ``_get_pollo_scene_gen()`` lazy singleton for
        cookie resolution, caching, and worker subprocess management.
        """
        gen = self._get_pollo_scene_gen()
        if gen is None:
            self._ai_fallback_enabled = False  # don't retry
            return None

        try:
            logger.info("Generating AI image via Pollo for: %r", query[:120])
            path = gen.generate_scene_image(query, theme=self._theme_context)
            if path and path.exists():
                logger.info("Pollo AI generated: %s (%.1f KB)", path,
                            path.stat().st_size / 1024)
                return {
                    "path": str(path),
                    "type": "image",
                    "duration": None,
                    "source": "pollo_ai",
                }
            logger.warning("Pollo AI returned no image for: %r", query[:80])
        except Exception as exc:
            logger.error("Pollo AI generation failed: %s", exc)

        return None

    # ── Internal: query simplification ────────────────────────────

    @staticmethod
    def _simplify_query(query: str) -> str:
        """Take first 3-4 keywords from a query (skip style modifiers)."""
        words = query.split()
        # Skip known style words
        style_words = {
            "cinematic", "photography", "dramatic", "lighting", "atmospheric",
            "16:9", "moody", "high", "contrast", "professional", "dark",
            "atmosphere", "slow", "motion", "tracking", "shot", "aerial",
            "overhead", "style", "film", "video", "stock",
        }
        keywords = [w for w in words if w.lower() not in style_words]
        return " ".join(keywords[:4])

    # ── Internal: smart query builder ────────────────────────────
    _STYLE_WORDS: set[str] = {
        "cinematic", "photography", "dramatic", "lighting", "atmospheric",
        "16:9", "moody", "high", "contrast", "professional", "dark",
        "atmosphere", "slow", "motion", "tracking", "shot", "aerial",
        "overhead", "style", "film", "video", "stock", "vertical",
        "establishing", "angle", "footage", "composition", "documentary",
        "historical", "depth", "field", "color", "grading", "grade",
    }

    @staticmethod
    def _build_search_query(
        query: str,
        variation: str | None = None,
        theme_keywords: list[str] | None = None,
        max_len: int = 100,
    ) -> str:
        """Build a search query that fuses scene-specific narrative content with
        video-level theme context.

        Strategy (v7 — narrative-first fusion):
        1. Extract scene narrative keywords from the LLM query (primary subject)
        2. Add sub-scene variation for visual diversity (lowest priority)
        3. Add 1-2 theme keywords as era/style anchors (not duplicate subjects)
        4. Fit all within ``max_len`` chars, trimming variation first, then theme, then scene.

        The scene narrative content always leads the query so stock APIs weight it
        higher. Theme keywords serve as contextual anchors appended at the end.
        """
        # 1. Extract scene topic keywords (strip style words, keep content nouns)
        words = query.split()
        scene_keywords = [w for w in words if w.lower() not in MediaFetcher._STYLE_WORDS]
        if not scene_keywords:
            scene_keywords = [w for w in words]  # fallback: all words

        # If both query and theme are empty, return the original query as-is
        if not scene_keywords and not theme_keywords:
            return query.strip()[:max_len]

        # 2. Allocate character budget: scene leads, theme anchors, variation optional
        #    scene: ~75% (narrative subject — the scene's primary visual content)
        #    theme: ~20% (era/style anchor — max 1-2 keywords appended at end)
        #    variation: ~15% (visual diversity — lowest priority, dropped first)
        variation_budget = 14 if variation else 0
        theme_budget = min(20, max_len - variation_budget)
        scene_budget = max_len - variation_budget - theme_budget

        # 3. Build scene narrative part (fit within scene_budget chars)
        scene_part = ""
        for w in scene_keywords:
            candidate = f"{scene_part} {w}".strip()
            if len(candidate) <= scene_budget:
                scene_part = candidate
            else:
                break  # budget exhausted
        if not scene_part:
            if scene_keywords:
                scene_part = scene_keywords[0][:scene_budget]  # first word, truncated
            elif theme_keywords:
                # No scene keywords — build from theme keywords instead
                scene_budget = max_len - (len(variation) + 1 if variation else 0)
                for kw in theme_keywords[:2]:
                    candidate = f"{scene_part} {kw}".strip()
                    if len(candidate) <= scene_budget:
                        scene_part = candidate
                    else:
                        break
                theme_keywords = None  # already consumed, skip step 4

        # 4. Build theme context part (era/style anchor at end, max 2 keywords, dedup)
        theme_part = ""
        if theme_keywords:
            scene_lower = scene_part.lower()
            # Only add theme keywords NOT already present in scene part
            fresh_keywords = [
                kw for kw in theme_keywords[:2]
                if kw.lower() not in scene_lower
            ]
            remaining = max_len - len(scene_part)
            if variation:
                remaining -= (len(variation) + 1)  # space + variation
            for kw in fresh_keywords:
                candidate = f"{theme_part} {kw}".strip()
                if len(candidate) <= max(remaining, 10):
                    theme_part = candidate
                else:
                    break

        # 5. Assemble: scene (primary) + variation (diversity) + theme (anchor)
        parts = [scene_part]
        if variation:
            parts.append(variation)
        if theme_part:
            parts.append(theme_part)

        result = " ".join(parts)

        # 6. Final safety: if still over budget, trim from right at last complete word
        if len(result) > max_len:
            result = result[:max_len].rsplit(" ", 1)[0]

        return result

    # ── Internal: Pixabay safety truncation ─────────────────────
    @staticmethod
    def _sanitize_for_pixabay(query: str, max_len: int = 100) -> str:
        """Truncate a query to Pixabay's 100-char limit at the last complete word.
        
        This is a safety net — ``_build_search_query`` should normally produce
        queries that fit, but this catches any edge cases.
        """
        if len(query) <= max_len:
            return query
        return query[:max_len].rsplit(" ", 1)[0]


def _strip_theme_keywords(query: str, theme_keywords: list[str]) -> str:
    """Remove theme keywords from a search query.

    Used as a retry fallback when theme-injected queries return no results.
    The original LLM-generated query keywords are preserved; only the theme
    keywords that were injected by ``_build_search_query`` are stripped.

    Args:
        query: The full query (potentially including theme keywords).
        theme_keywords: The theme keywords to remove.

    Returns:
        A cleaned query with theme keywords removed.
    """
    if not theme_keywords or not query:
        return query

    theme_lower = set(kw.lower().strip() for kw in theme_keywords if kw.strip())
    words = query.split()
    cleaned = [w for w in words if w.lower().strip() not in theme_lower]
    return " ".join(cleaned) if cleaned else query
