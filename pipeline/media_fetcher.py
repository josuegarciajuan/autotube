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
import re
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
from pipeline.providers.pollinations_provider import PollinationsProvider
from pipeline.providers.local_sd_provider import LocalSDProvider
from pipeline.visual_coherence import VisualCoherenceEngine

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
      1. Pixabay Photos API → download .jpg
      2. Unsplash Photos API → download .jpg
      3. Retry with simplified query (first 3 keywords only)
      4. Pollo AI image generation (rescue only when stock exhausted)
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

        # ── MEDIA_STRATEGY: deep-merge channel overrides onto defaults ──
        # Channel configs may define their own MEDIA_STRATEGY dict, which
        # replaces the entire defaults dict (shallow overwrite).  We deep-merge
        # so that new keys added to defaults auto-propagate to all channels
        # without requiring per-channel config updates.
        _channel_strategy = getattr(self._config, "MEDIA_STRATEGY", {}) or {}
        try:
            from config import defaults as _def
            _default_strategy = getattr(_def, "MEDIA_STRATEGY", {}) or {}
        except Exception:
            _default_strategy = {}
        if _default_strategy:
            self._media_strategy = dict(_default_strategy)
            self._media_strategy.update(_channel_strategy)  # channel wins per-key
        else:
            self._media_strategy = dict(_channel_strategy)

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

        # ── Image provider list (for urgent single-image fetches) ───
        self._image_providers = self._get_all_image_providers()

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

        # ── Video quality scoring (Phase 2) ────────────────────────
        self._video_quality_scores: list[float] = []

        # ── Pollo AI scene generator (lazy, avoids ~7 min per image unless absolutely needed) ─
        self._pollo_scene_gen = None
        self._ai_fallback_enabled = self._media_strategy.get("ai_image_fallback", False)

        # ── AI image providers (Phase 1: Pollinations + Local SD) ──────
        self._ai_image_primary = self._media_strategy.get("ai_image_primary", False)
        self._pollinations: PollinationsProvider | None = None
        self._local_sd: LocalSDProvider | None = None
        self._coherence_engine: VisualCoherenceEngine | None = None
        self._visual_bible: dict | None = None
        # Per-scene AI prompt log (scene_idx → full prompt) for the
        # verification report (scripts/test_visual_coherence.py).
        self._ai_prompt_log: dict[int, str] = {}

        # Build AI provider chain from config (always available — no auth needed)
        ai_cache_dir = str(settings.OUTPUT_DIR / "ai_cache" / "pollinations")
        try:
            # ── Upscale local post-generación (ESPCN_x2 + unsharp mask) ──
            # Pollinations devuelve 1024×576 aunque se pida 1920×1080; al
            # escalar en el render se ve borroso. Subimos a la resolución
            # mínima objetivo del canal (config AI_UPSCALE_*) y aplicamos
            # unsharp mask para nitidez percibida.
            if getattr(self._config, "AI_UPSCALE_ENABLED", True):
                upscale_min = (
                    getattr(self._config, "AI_UPSCALE_MIN_WIDTH", 1920),
                    getattr(self._config, "AI_UPSCALE_MIN_HEIGHT", 1080),
                )
            else:
                upscale_min = None
            _upscale_model_raw = getattr(self._config, "AI_UPSCALE_MODEL", None)
            upscale_model = (
                _upscale_model_raw if isinstance(_upscale_model_raw, str) else "espcn"
            )
            upscale_sharpen = bool(getattr(self._config, "AI_UPSCALE_SHARPEN_ENABLED", True))
            upscale_sharpen_amount = float(getattr(self._config, "AI_UPSCALE_SHARPEN_AMOUNT", 0.4))
            upscale_sharpen_sigma = float(getattr(self._config, "AI_UPSCALE_SHARPEN_SIGMA", 2.0))
            self._pollinations = PollinationsProvider(
                model=self._media_strategy.get("ai_pollinations_model") or "flux",
                width=1920,
                height=1080,
                cache_dir=ai_cache_dir,
                upscale_min=upscale_min,
                upscale_model=upscale_model,
                upscale_sharpen=upscale_sharpen,
                upscale_sharpen_amount=upscale_sharpen_amount,
                upscale_sharpen_sigma=upscale_sharpen_sigma,
            )
            logger.info("AI image provider registered: pollinations (free, no-auth)")
        except Exception as exc:
            logger.warning("Pollinations provider init failed: %s", exc)

        try:
            sd_steps = self._media_strategy.get("ai_local_sd_steps", 20)
            self._local_sd = LocalSDProvider(
                num_inference_steps=sd_steps,
                width=768,
                height=768,
                upscale_min=upscale_min if upscale_min else None,
                upscale_model=upscale_model if upscale_min else None,
                upscale_sharpen=upscale_sharpen if upscale_min else False,
                upscale_sharpen_amount=upscale_sharpen_amount,
                upscale_sharpen_sigma=upscale_sharpen_sigma,
            )
            logger.info("AI image provider registered: local_sd (free, CPU, ~3 min)")
        except Exception as exc:
            logger.warning("Local SD provider init failed: %s", exc)

    def set_visual_context(
        self,
        visual_bible: dict | None = None,
        coherence_engine: VisualCoherenceEngine | None = None,
    ) -> None:
        """Inject visual context into the media fetcher.

        Called by the orchestrator **before** ``fetch_for_script()`` so
        that all AI image generations within a single video share the
        same style prefix, colour arc, and visual bible.

        Parameters
        ----------
        visual_bible:
            Optional ``VisualBible`` dict (Phase 3). Not used in Phase 1.
        coherence_engine:
            ``VisualCoherenceEngine`` instance that provides the style
            prefix and colour-temperature arc. If not provided, one is
            auto-created from the channel config.
        """
        self._visual_bible = visual_bible

        if coherence_engine is not None:
            self._coherence_engine = coherence_engine
        elif self._coherence_engine is None:
            self._coherence_engine = VisualCoherenceEngine(self._config, visual_bible)

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

        # Build query: scene topic + video-level full theme context (v8), fitting Pixabay's 100-char limit
        ctx = theme_context or self._theme_context
        query = self._build_search_query(
            query=block_query,
            theme_keywords=ctx.theme_keywords_en if ctx else None,
            theme_ctx=ctx,  # v8: pass full ThemeContext for richer anchoring
            era_enabled=self._media_strategy.get("era_anchor_enabled", True),
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
        progress_cb: callable = None,
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

        # ── Phase 2: Classify scenes (video_priority vs stock_image vs ai_image) ──
        video_scenes, stock_image_scenes, scene_types = self._classify_scenes(scenes)

        # ── RAM governor: hard-cap video assets to prevent OOM ──
        MAX_ABSOLUTE_VIDEOS = 50
        try:
            from pipeline.ram_governor import available_mb
            free_ram_mb = available_mb()
            if free_ram_mb > 0 and free_ram_mb < 3000:
                MAX_ABSOLUTE_VIDEOS = min(MAX_ABSOLUTE_VIDEOS, 15)
                logger.warning(
                    "RAM governor: only %.1f GB free → capping videos at %d",
                    free_ram_mb / 1024, MAX_ABSOLUTE_VIDEOS,
                )
            elif free_ram_mb > 0 and free_ram_mb < 5000:
                MAX_ABSOLUTE_VIDEOS = min(MAX_ABSOLUTE_VIDEOS, 25)
                logger.warning(
                    "RAM governor: only %.1f GB free → capping videos at %d",
                    free_ram_mb / 1024, MAX_ABSOLUTE_VIDEOS,
                )
            elif free_ram_mb > 12000:
                MAX_ABSOLUTE_VIDEOS = 70
                logger.info(
                    "RAM governor: %.1f GB free → high-RAM mode: video cap raised to %d",
                    free_ram_mb / 1024, MAX_ABSOLUTE_VIDEOS,
                )
        except Exception:
            pass

        hard_cap = self._media_strategy.get("video_scene_hard_cap", 12)
        MAX_ABSOLUTE_VIDEOS = min(MAX_ABSOLUTE_VIDEOS, hard_cap)
        logger.info(
            "Fase 2 classification: %d video-priority / %d stock-image-priority / "
            "%d ai-image (%d scenes), hard cap %d videos, dynamic range %.0f-%.0f%%",
            len(video_scenes), len(stock_image_scenes),
            n_scenes - len(video_scenes) - len(stock_image_scenes), n_scenes,
            MAX_ABSOLUTE_VIDEOS,
            self._media_strategy.get("video_scene_pct_min", 20),
            self._media_strategy.get("video_scene_pct_max", 30),
        )

        # ── Phase 2: fetch per scene (tier-based) ──────────────
        results: list[dict] = [{} for _ in range(n_scenes)]
        video_ok = 0
        image_ok = 0
        placeholder = 0

        # Video quality scoring for 20-30% dynamic control
        self._video_quality_scores: list[float] = []
        _video_queries_tried: dict[int, int] = {}  # scene_idx → queries exhausted

        # Pollo AI counter
        ai_max = self._media_strategy.get("ai_max_per_video", 2)
        ai_used = 0
        ai_enabled = self._media_strategy.get("ai_image_fallback", False)

        # ── Phase timeout guard ───────────────────────────────────
        _cb_phase_start = time.time()
        # Scale timeout: 30s per stock scene + per-AI-scene seconds + 600s base
        # overhead. The per-AI-scene budget scales with the Local SD step count
        # (the slowest provider in the chain): ~25s/step + 45s overhead.
        # ago 2026: the fixed 180s/image assumed Local SD ~3 min, but at 20
        # steps it really takes ~15 min → false timeouts on long videos.
        n_ai = n_scenes - len(video_scenes)
        _sd_steps = int(self._media_strategy.get("ai_local_sd_steps", 8) or 8)
        _ai_scene_secs = max(180, 45 + _sd_steps * 25)  # 8 steps → 245s; 20 → 545s
        _cb_phase_timeout = max(3600, 600 + n_scenes * 30 + n_ai * _ai_scene_secs)

        # ── Reset video provider circuit breaker for this job ──
        self._vp_hard_fail.clear()
        self._vp_soft_fail_scenes.clear()
        self._vp_disabled.clear()
        self._scene_tried_providers.clear()

        # ── Reset enhanced dedup tracking ─────────────────────
        self._used_filenames.clear()
        self._used_img_ids.clear()
        self._used_content_hashes.clear()

        # ── Hard abort counter: consecutive placeholder scenes ──
        _consecutive_black = 0
        _MAX_CONSECUTIVE_BLACK = 3

        for i, scene in enumerate(scenes):
            scene_tipo = scene.get("tipo", "desarrollo")
            target_dur = scene.get("duration", 5)
            is_video_priority = i in video_scenes
            is_stock_image_priority = i in stock_image_scenes

            # ── RAM safety: hard-cap video assets ────────────────
            _force_images = False
            if video_ok >= MAX_ABSOLUTE_VIDEOS:
                is_video_priority = False
                _force_images = True
                if video_ok == MAX_ABSOLUTE_VIDEOS:
                    logger.warning(
                        "RAM safety cap reached (%d videos) — "
                        "forcing remaining %d scenes to image-only",
                        MAX_ABSOLUTE_VIDEOS, n_scenes - i,
                    )

            # ── Dynamic video stop: quality threshold not met ─────
            if is_video_priority and not self._should_continue_video_search(
                video_ok, n_scenes
            ):
                logger.info(
                    "Dynamic video stop at scene %d: %d videos found, "
                    "avg quality %.2f — reclassifying remaining as ai_image",
                    i + 1, video_ok,
                    sum(self._video_quality_scores) / max(len(self._video_quality_scores), 1),
                )
                is_video_priority = False

            logger.info(
                "Scene %d/%d [%s]: %s dur=%.1fs",
                i + 1, n_scenes, scene_tipo,
                ("video_priority" if is_video_priority else
                 "stock_image_priority" if is_stock_image_priority else "ai_image"),
                target_dur,
            )

            # ── Fetch via tier chain ──────────────────────────────
            asset, ai_used, quality_info = self._fetch_with_ai_tiers(
                scene=scene,
                scene_idx=i,
                total_scenes=n_scenes,
                is_video_priority=is_video_priority,
                target_dur=target_dur,
                ctx=ctx,
                ai_used=ai_used,
                ai_max=ai_max,
                ai_enabled=ai_enabled,
                force_images=_force_images,
                is_stock_image_priority=is_stock_image_priority,
            )

            # ── Score video quality for dynamic control ───────────
            if asset and asset.get("type") == "video":
                provider = asset.get("source", "unknown")
                resolution = asset.get("resolution", "unknown")
                actual_dur = asset.get("duration")
                score = self._score_video_quality(
                    provider_name=provider,
                    resolution=resolution,
                    target_dur=target_dur,
                    actual_dur=actual_dur,
                    queries_tried=_video_queries_tried.get(i, 1),
                )
                self._video_quality_scores.append(score)
                logger.info(
                    "Video quality score: %.2f (provider=%s, res=%s)",
                    score, provider, resolution,
                )

            # ── Placeholder (absolutely nothing found) ────────
            if asset is None:
                _consecutive_black += 1
                logger.warning(
                    "Scene %d [%s]: ALL tiers exhausted — "
                    "placeholder (%d/%d consecutive)",
                    i + 1, scene_tipo, _consecutive_black, _MAX_CONSECUTIVE_BLACK,
                )

                # ── HARD ABORT: 3+ consecutive black scenes ──
                if _consecutive_black >= _MAX_CONSECUTIVE_BLACK:
                    error_msg = (
                        f"CRITICAL: {_consecutive_black} consecutive scenes with NO media. "
                        f"All tiers, all providers, and all queries exhausted "
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
                _consecutive_black = 0
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

            # ── v14: progress callback (every ~10% of scenes) ──
            if progress_cb is not None and (i == 0 or i == n_scenes - 1
                    or (i + 1) % max(1, n_scenes // 10) == 0):
                try:
                    progress_cb(i + 1, n_scenes)
                except Exception:
                    pass

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
                # Original intent was video (from video_scenes classification)
                if i not in video_scenes:
                    continue

                target_dur = scene.get("duration", 5)
                # ── Respect hard cap even during rescue pass ──
                if video_ok >= MAX_ABSOLUTE_VIDEOS:
                    logger.warning(
                        "Video rescue stopped: hard cap reached (%d videos)",
                        MAX_ABSOLUTE_VIDEOS,
                    )
                    break
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
                        self._record_asset_for_history(retry)
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

        # A video-priority scene can ultimately receive an image.  Reconcile
        # after every rescue attempt so actual image ranges never exceed the
        # image cap.  New subscenes get separate, deduplicated fetch requests;
        # a failed/duplicate request becomes a placeholder rather than reusing
        # the first image or shifting any following timestamp.
        scenes, results = self._reconcile_actual_image_fallbacks(
            scenes, results, self._fetch_distinct_image_for_expanded_scene,
        )

        # ── Uniqueness gate: enough DISTINCT media for the timeline? ──
        # Mirrors the render-time "placeholder ratio" gate in video_editor:
        # if duplicate cache hits exhausted the unique image pool, the render
        # would reject the repeats, fall back to placeholders and abort after
        # ~1h of work (>30% black segments).  Fail fast HERE instead, while a
        # re-plan / re-fetch is still cheap and the operator gets a clear error.
        _unique_image_paths: set[str] = set()
        _dupe_images = 0
        _missing_files = 0
        for _a in results:
            if not isinstance(_a, dict) or not _a.get("path"):
                _missing_files += 1
                continue
            _sp = str(_a["path"])
            if _a.get("type") == "image":
                if _sp in _unique_image_paths:
                    _dupe_images += 1
                    logger.warning(
                        "Uniqueness gate: image %s already assigned to "
                        "another scene — render would reject it",
                        Path(_sp).name,
                    )
                _unique_image_paths.add(_sp)

        # NOTE: video files are legitimately reused across scenes via
        # offset-tracking (each scene takes a different segment), so only
        # IMAGE duplicates count as hard violations.  A scene with no file
        # at all (placeholder) is tolerated up to the same 30% limit the
        # render-time gate enforces.
        _placeholder_pct = _missing_files / max(n_scenes, 1) * 100
        if _dupe_images > 0 or _placeholder_pct > 30.0:
            if _dupe_images:
                _note = f" ({_dupe_images} duplicate image assignments)"
            else:
                _note = (
                    f" ({_missing_files}/{n_scenes} scenes without media "
                    f"— would exceed the 30%% placeholder limit at render)"
                )
            error_msg = (
                f"CRITICAL: media fetch produced insufficient DISTINCT media "
                f"for {n_scenes} scenes{_note}. Duplicate cache hits exhausted "
                f"the unique pool — aborting before render to prevent a "
                f"black-screen placeholder cascade."
            )
            logger.error(error_msg)
            raise RuntimeError(error_msg)
        return results

    def _reconcile_actual_image_fallbacks(
        self,
        scenes: list[dict],
        assets: list[dict],
        fetch_distinct_image: callable,
    ) -> tuple[list[dict], list[dict]]:
        """Split overlong *actual* image scenes and obtain unique assets.

        The function mutates ``scenes`` in place because callers retain that
        list as the renderer's timing contract.  It only partitions a scene's
        own [start, end] range, therefore subsequent timestamps are unchanged.
        """
        config = self._config
        if isinstance(config, dict):
            image_max = float(config.get("IMAGE_SCENE_DURATION_MAX", 6.0))
        else:
            image_max = float(getattr(config, "IMAGE_SCENE_DURATION_MAX", 6.0))

        expanded_scenes: list[dict] = []
        expanded_assets: list[dict] = []
        for scene, asset in zip(scenes, assets):
            actual_type = asset.get("type") if asset else None
            duration = float(scene.get("duration", 0))
            if actual_type != "image" or duration <= image_max:
                expanded_scenes.append(scene)
                expanded_assets.append(asset)
                continue

            count = max(2, int((duration + image_max - 1e-9) / image_max))
            sub_duration = duration / count
            parent_request = str(scene.get("media_request_id", "scene"))
            seen_identity_tokens = self._asset_identity_tokens(asset)
            for index in range(count):
                subscene = dict(scene)
                subscene["start"] = float(scene["start"]) + index * sub_duration
                subscene["end"] = float(scene["start"]) + (index + 1) * sub_duration
                subscene["duration"] = subscene["end"] - subscene["start"]
                subscene["media_tipo"] = "image"
                subscene["is_subscene"] = True
                subscene["media_request_id"] = f"{parent_request}:image:{index}"
                if index == 0:
                    subasset = asset
                else:
                    subasset = fetch_distinct_image(subscene)
                    identity_tokens = self._asset_identity_tokens(subasset)
                    if not subasset or subasset.get("type") != "image" or (
                        identity_tokens & seen_identity_tokens
                    ):
                        subasset = {
                            "path": None,
                            "type": "placeholder",
                            "duration": None,
                            "source": "placeholder",
                        }
                    else:
                        seen_identity_tokens.update(identity_tokens)
                expanded_scenes.append(subscene)
                expanded_assets.append(subasset)

        if len(expanded_scenes) != len(scenes):
            scenes[:] = expanded_scenes
            logger.info(
                "Expanded %d fetched scene(s) into %d actual-media ranges to enforce image timing",
                len(assets), len(expanded_assets),
            )
        return scenes, expanded_assets

    @staticmethod
    def _asset_identity_tokens(asset: dict | None) -> set[str]:
        """Return all stable identity tokens used to reject repeated images."""
        asset = asset or {}
        tokens: set[str] = set()
        for prefix, value in (
            ("path", asset.get("path")),
            ("url", asset.get("url") or asset.get("download_url")),
            ("content", asset.get("content_hash") or asset.get("id")),
        ):
            if value:
                tokens.add(f"{prefix}:{value}")
        return tokens

    def _fetch_distinct_image_for_expanded_scene(self, scene: dict) -> dict | None:
        """Fetch one additional image through normal per-video dedup tracking."""
        query_pool = self._build_query_pool(scene, self._theme_context)
        asset = self._fetch_asset_exhaustive(
            scene, query_pool, want_video=False,
            target_dur=scene["duration"], ctx=self._theme_context, force_images=True,
        )
        if asset and asset.get("type") == "image":
            self._record_asset_for_history(asset)
            return asset
        return None

    def fetch_single_image_urgent(self, query: str) -> dict | None:
        """Fetch ONE image urgently from any available provider.

        Used by the dynamic gap-fill system during video rendering when
        a scene runs out of pre-fetched assets. Checks against all
        dedup tracking sets (URLs, filenames, cross-video history)
        to prevent reusing assets already present in this or any
        previous video.

        Returns {type, path, source} dict or None if all providers fail.
        """
        import time
        # Defensive guard: _image_providers may be absent if __init__
        # was interrupted (e.g. race condition, hot-reload cascade).
        # Gracefully return None instead of crashing.
        _providers = getattr(self, '_image_providers', None)
        if not _providers:
            return None

        _urgent_set = getattr(self, '_urgent_used_urls', None)
        if _urgent_set is None:
            _urgent_set = set()
            self._urgent_used_urls = _urgent_set

        for provider in _providers:
            if not provider.available:
                continue
            try:
                candidates = provider.search(query, per_page=10)
                if not candidates:
                    continue
                for candidate in candidates:
                    url = getattr(candidate, 'url', '')
                    if not url:
                        continue
                    # --- Dedup: check all tracking sets ---
                    if url in _urgent_set:
                        continue
                    if url in self._used_image_urls:
                        continue
                    if url in self._used_asset_urls:
                        continue
                    img_id = str(getattr(candidate, 'id', ''))
                    source = getattr(provider, 'name', 'unknown')
                    if img_id:
                        predicted = f"output/images/{source}_{img_id}.jpg"
                        if predicted in self._used_filenames:
                            continue
                        # Also check cross-video dedup for this filename
                        if hasattr(self, '_cross_video_used_filenames') and predicted in self._cross_video_used_filenames:
                            continue

                    path = self._download_image(url, f"fill_{int(time.time())}.jpg")
                    if path and self._is_valid_image(path):
                        _urgent_set.add(url)
                        self._used_image_urls.add(url)
                        self._used_asset_urls.add(url)
                        # Record for cross-video dedup
                        self._record_asset_for_history({
                            "path": str(path),
                            "source": source,
                            "url": url,
                        })
                        return {
                            "type": "image",
                            "path": str(path),
                            "source": source,
                        }
            except Exception:
                continue
            time.sleep(0.1)  # rate-limit between providers
        return None

    # ── Pollo AI last resort ────────────────────────────────────

    def _try_pollo_scene(
        self,
        scene: dict | str,
        scene_idx: int = 0,
        total_scenes: int = 1,
        ctx=None,
    ) -> dict | None:
        """Invoke Pollo AI image generation as absolute last resort."""
        pollo = self._get_pollo_scene_gen()
        if pollo is None:
            return None

        # Accept the legacy query string for direct callers, but normalize it
        # into a scene so Pollo always uses the same concept/bible/style/seed
        # prompt assembly as Pollinations and Local SD.
        if isinstance(scene, str):
            scene = {"search_query_en": scene, "tipo": "desarrollo"}
        scene_tipo = scene.get("tipo", "desarrollo")
        if not scene.get("search_query_en") and not scene.get("texto"):
            # Build a minimal prompt from scene type if query is empty
            type_hints = {
                "hook": "dramatic cinematic opening scene dark atmosphere",
                "desarrollo": "atmospheric documentary b-roll storytelling",
                "climax": "intense dramatic peak moment tension climax",
                "reflexion": "contemplative peaceful atmospheric reflection",
                "cierre": "hopeful closing scene dawn light resolution",
            }
            scene = dict(scene)
            scene["search_query_en"] = type_hints.get(
                scene_tipo, "cinematic atmospheric scene 16:9"
            )

        prompt, _seed = self._build_ai_prompt(scene, scene_idx, total_scenes)
        self._ai_prompt_log[scene_idx] = prompt

        try:
            logger.info("Scene [%s]: invoking Pollo AI for %r", scene_tipo, prompt[:80])
            path = pollo.generate_scene_image(prompt, theme=ctx)
            if path and path.exists():
                # Same cache-duplicate guard as the AI image chain: the
                # scene generator caches by prompt hash and can return the
                # same file for repeated prompts.
                if self._is_asset_duplicate({"url": str(path)}):
                    logger.warning(
                        "Scene %d/%d: Pollo AI image %s already used in this "
                        "video (cache duplicate) — skipping",
                        scene_idx + 1, total_scenes, Path(path).name,
                    )
                    return None
                self._record_asset_used({"url": str(path)})
                return {
                    "path": path,
                    "type": "image",
                    "duration": None,
                    "source": "pollo_ai",
                }
        except Exception as exc:
            logger.warning("Pollo AI generation failed: %s", exc)

        return None

    # ── AI Image Chain (Phase 1) ─────────────────────────────────

    def _try_ai_image_chain(
        self,
        scene: dict,
        scene_idx: int,
        total_scenes: int,
    ) -> dict | None:
        """Try to generate an AI image for *scene* using the free providers.

        Provider order (from config ``ai_image_providers``):
          1. Pollinations.ai (fast, ~8 s, quality 7/10)
          2. Local SD 1.5   (slow, ~180 s, quality 6.5/10 — only if #1 fails)

        Each generation uses a coherent 4-layer prompt assembled by
        ``build_ai_prompt()``.  In Phase 1 only 3 layers are active
        (visual bible is ``None``).

        Returns an asset dict on success, or ``None`` if all providers
        (including Local SD) failed.
        """
        ai_providers_cfg = self._media_strategy.get("ai_image_providers", ["pollinations", "local_sd"])

        # Build shared prompt once per scene (it doesn't change per provider).
        prompt, seed = self._build_ai_prompt(scene, scene_idx, total_scenes)
        # Record the full prompt for the verification report.
        self._ai_prompt_log[scene_idx] = prompt
        # Negative prompt: global terms + forbidden_elements (anacronismos) del
        # theme extractor, que hoy solo se filtraban en queries de stock. La IA
        # necesita verlos como negative para no generar elementos fuera de época.
        negative = VisualCoherenceEngine.build_negative_prompt()
        tc = self._theme_context
        if tc is not None and getattr(tc, "forbidden_elements", None):
            forbidden = [f for f in tc.forbidden_elements if f]
            if forbidden:
                negative = f"{negative}, {', '.join(forbidden)}"
        output_dir = Path(settings.OUTPUT_DIR) / "ai_images" / scene.get("tipo", "escena")
        output_dir.mkdir(parents=True, exist_ok=True)
        output_path = output_dir / f"scene_{scene_idx:03d}_{hashlib.md5(prompt.encode()).hexdigest()[:10]}.jpg"

        for provider_key in ai_providers_cfg:
            provider = None
            provider_label = ""

            if provider_key == "pollinations" and self._pollinations is not None:
                provider = self._pollinations
                provider_label = "pollinations"

            elif provider_key == "local_sd" and self._local_sd is not None:
                provider = self._local_sd
                provider_label = "local_sd"

            if provider is None:
                continue

            try:
                logger.info(
                    "Scene %d/%d [%s]: AI image via %s — prompt: %s",
                    scene_idx + 1, total_scenes,
                    scene.get("tipo", "?"),
                    provider_label,
                    prompt,
                )
                result_path = provider.generate(
                    prompt=prompt,
                    output_path=output_path,
                    seed=seed,
                    negative_prompt=negative,
                )
                if result_path and result_path.exists():
                    # ── Strict intra-video dedup (cache hits!) ──────────
                    # The provider caches by prompt hash; identical or
                    # truncated prompts return the SAME cached file. If we
                    # assign it to a second scene, the render-time "never
                    # repeat an image" invariant rejects it later → fallback
                    # chain → placeholder. Reject it HERE so the next
                    # provider/tier is tried while we still have options.
                    if self._is_asset_duplicate({"url": str(result_path)}):
                        logger.warning(
                            "Scene %d/%d: AI image %s already used in this "
                            "video (cache duplicate) — trying next provider",
                            scene_idx + 1, total_scenes,
                            Path(result_path).name,
                        )
                        continue
                    # Validate minimum quality (lenient for AI — they may be smaller
                    # than stock photos due to efficient encoding).
                    if not self._is_valid_ai_image(result_path):
                        logger.warning(
                            "AI image from %s failed validation, trying next provider",
                            provider_label,
                        )
                        continue

                    self._record_asset_used({"url": str(result_path)})
                    self._record_asset_for_history({
                        "path": str(result_path),
                        "source": f"ai_{provider_label}",
                    })
                    return {
                        "path": result_path,
                        "type": "image",
                        "duration": None,
                        "source": f"ai_{provider_label}",
                    }

            except Exception as exc:
                logger.warning(
                    "AI provider %s failed for scene %d: %s",
                    provider_label, scene_idx + 1, exc,
                )
                continue  # next provider

        logger.warning(
            "Scene %d/%d: all AI providers exhausted — no image generated",
            scene_idx + 1, total_scenes,
        )
        return None

    def _build_ai_prompt(
        self,
        scene: dict,
        scene_idx: int,
        total_scenes: int,
    ) -> tuple[str, int | None]:
        """Assemble the AI image prompt for a scene.

        Returns (prompt, seed).

        Layers (Phase 1 — visual bible is ``None``):
          1. **Style prefix** — from ``VisualCoherenceEngine``, modulated
             by the scene's position in the colour-temperature arc.
          2. **Visual context** — (Phase 3 only; empty in Phase 1).
          3. **Scene concept** — query or description from the scene dict.
          4. **Technical suffix** — aspect ratio, quality, density hints.

        The prompt is truncated to ~1000 characters to avoid overwhelming
        the model with verbosity while preserving style consistency.
        """
        # ── Ensure coherence engine ──────────────────────────
        if self._coherence_engine is None:
            self._coherence_engine = VisualCoherenceEngine(self._config, self._visual_bible)

        # ── Layer 1: Style (channel + colour arc) ────────────
        style = self._coherence_engine.get_scene_style(scene_idx, total_scenes)

        # ── Layer 2: Visual context (Phase 3; placeholder) ───
        context_parts: list[str] = []
        if self._visual_bible:
            vu = self._visual_bible.get("visual_universe", "")
            if vu:
                context_parts.append(vu)
            entity = self._visual_bible.get("central_entity", {})
            if entity.get("type") != "none" and scene_idx in entity.get("appears_in_scenes", []):
                context_parts.append(entity.get("master_description", ""))
                variation = entity.get("variation_by_scene", {}).get(str(scene_idx), "")
                if variation:
                    context_parts.append(variation)
            for elem in self._visual_bible.get("recurring_elements", [])[:3]:
                if elem:
                    context_parts.append(elem)
        # Temporal anchor from the theme extractor (era coherence across scenes)
        tc = self._theme_context
        if tc is not None:
            era = getattr(tc, "era_decade", "") or getattr(tc, "era", "")
            if era and era not in ("atemporal", "presente"):
                context_parts.append(era)
        context = ", ".join(p for p in context_parts if p)

        # ── Layer 3: Scene concept ────────────────────────────
        # When visual bible is available, use the per-scene visual_concept
        # (a metaphoric visual description, NOT a literal illustration).
        # Fall back to search_query_en or generic type hints otherwise.
        concept = ""
        if self._visual_bible:
            scene_map = self._visual_bible.get("scene_visual_map", [])
            if scene_idx < len(scene_map):
                vb_scene = scene_map[scene_idx]
                concept = vb_scene.get("visual_concept", "")
                # Add visual bridge from the previous scene
                if scene_idx > 0:
                    bridge = vb_scene.get("bridge_from_prev", "")
                    if bridge:
                        concept = f"{concept}, visual bridge: {bridge}"

                # Override density from visual bible if available
                vb_density = vb_scene.get("visual_density", "")
                if vb_density in ("simple", "balanced", "rich"):
                    density = vb_density

        if not concept:
            concept = scene.get("search_query_en", "") or scene.get("texto", "") or ""
        if not concept:
            type_hints = {
                "hook": "dramatic cinematic opening scene",
                "desarrollo": "atmospheric documentary b-roll",
                "climax": "intense dramatic peak moment",
                "reflexion": "contemplative peaceful scene",
                "cierre": "hopeful closing scene resolution",
            }
            concept = type_hints.get(scene.get("tipo", ""), "cinematic atmospheric scene")

        # Ground literal labels and abstract metaphors in observable staging
        # before handing the concept to an image model.  The scene marker and
        # existing coherence/dedup layers remain unchanged.
        from pipeline.cinematic_staging import build_scene_brief
        grounded = build_scene_brief(scene.get("texto", ""), concept, self._theme_context)
        if grounded and grounded != concept.lower():
            concept = f"{concept}, {grounded}"

        # Ensure uniqueness: EVERY scene prompt carries a stable scene marker.
        # Even when a visual bible concept exists, two scenes can receive the
        # same concept (LLM repetition / padded entries / empty search_query),
        # and identical prompts → identical Pollinations cache key → the SAME
        # cached image for both scenes. The marker is PREPENDED so it survives
        # the truncation below (which cuts `concept` from the END).
        concept = f"scene {scene_idx + 1}/{total_scenes}: {concept}"

        # ── Layer 4: Technical suffix ────────────────────────
        # Density may have been overridden by the visual bible above
        if "density" not in dir() or density is None:
            palabras = len(scene.get("texto", "").split())
            duracion = scene.get("duration", 5) or 1
            pps = palabras / duracion
            density = VisualCoherenceEngine.get_visual_density(pps)
        tech = VisualCoherenceEngine.build_tech_suffix(density)

        # ── Assemble ─────────────────────────────────────────
        # Concept-first: the scene's subject leads the prompt so the
        # generated image tracks the narration. Style and visual context
        # are supporting layers appended AFTER the subject.
        parts = [concept]
        if context:
            parts.append(context)
        parts.append(style)
        parts.append(tech)
        prompt = ", ".join(p for p in parts if p)

        # Truncate to ~1000 chars while retaining the global impact treatment
        # and channel grade. They are the shared visual contract for every AI
        # provider; concept/context remain present but yield length first.
        # ⚠️ FIX: con el presupuesto de 500 chars, las configs de producción
        # (impact style largo + colour grade ≈ 460 + tech ≈ 190) dejaban
        # remaining=0 y DESCARTABAN el concepto de escena entero (prompt =
        # solo estilo+técnica) — las imágenes IA ignoraban la narración.
        # Subimos el tope a ~1000 (Pollo AI admite 2000; Pollinations acepta
        # prompts largos; Local SD corta por tokens CLIP igualmente) y
        # GARANTIZAMOS un suelo mínimo para el marcador + concepto, cortando
        # en límite de palabra limpio (nada de ", ," por cortes a mitad).
        if len(prompt) > 1000:
            protected_style = self._coherence_engine.impact_style_prefix
            overhead = 6 if context else 4  # ", " separators
            remaining = max(120, 997 - len(protected_style) - len(tech) - overhead)
            concept_budget = max(40, int(remaining * 0.65))
            context_budget = max(0, remaining - concept_budget)
            concept_lead = concept[:concept_budget]
            context_lead = context[:context_budget] if context else ""
            # Cut at clean word boundaries so "a, b, " never leaves "a, , c".
            if context_lead and context_budget < len(context) \
                    and context[context_budget] not in (" ", ","):
                context_lead = context_lead.rstrip().rsplit(" ", 1)[0]
            if concept_lead and concept_budget < len(concept) \
                    and concept[concept_budget] not in (" ", ","):
                concept_lead = concept_lead.rstrip().rsplit(" ", 1)[0]
            trim_parts = [concept_lead]
            if context_lead:
                trim_parts.append(context_lead)
            trim_parts.append(protected_style)
            trim_parts.append(tech)
            prompt = ", ".join(p for p in trim_parts if p)
            if len(prompt) > 1000:
                prompt = prompt[:997] + "..."

        # ── Seed (protagonist consistency — Phase 3) ─────────
        seed = None
        if self._visual_bible:
            entity = self._visual_bible.get("central_entity", {})
            if entity.get("type") != "none" and scene_idx in entity.get("appears_in_scenes", []):
                master = entity.get("master_description", "")
                video_id = getattr(self, "_video_id", 0)
                # Deterministic seed (hashlib, not Python's process-random
                # ``hash()``) so the same protagonist renders identically
                # across every scene where they appear.
                digest = hashlib.md5(
                    f"{master}|{video_id}".encode("utf-8")
                ).hexdigest()[:8]
                seed = int(digest, 16) % (2**31)

        return prompt, seed

    # ── Phase 2: Scene Classification & Tier System ───────────────

    def _classify_scenes(self, scenes: list[dict]) -> tuple[set[int], set[int], dict[int, str]]:
        """Classify every scene as ``video_priority``, ``stock_image_priority`` or ``ai_image``.

        **Video-priority criteria** (any one is sufficient):
          1. Scene type is ``hook`` or ``climax`` (always forced).
          2. Scene duration ≥ ``video_min_scene_duration`` AND position
             is within the first ``video_first_half_pct``% of runtime.

        Capped at ``video_scene_pct_max``% of total scenes.  Scenes
        beyond the cap are reclassified as ``ai_image`` even if they
        meet the criteria.

        **Stock-image priority** (only if ``stock_image_pct`` > 0):
          Up to ``stock_image_pct``% of the remaining (non-video,
          non-transition) scenes try real stock images (Pixabay/Unsplash)
          before AI generation.  ``hook``/``climax`` are excluded — visual
          coherence matters most there, so they stay AI-first.

        Returns
        -------
        video_scenes : set[int]
            Indices of scenes that should try stock video first.
        stock_image_scenes : set[int]
            Indices of scenes that should try stock images first.
        scene_types : dict[int, str]
            ``"video_priority"``, ``"stock_image_priority"`` or ``"ai_image"``
            for every scene index.
        """
        n_scenes = len(scenes)
        if n_scenes == 0:
            return set(), set(), {}

        total_duration = sum(s.get("duration", 5) for s in scenes) or 1.0
        min_dur = self._media_strategy.get("video_min_scene_duration", 10)
        first_half_pct = self._media_strategy.get("video_first_half_pct", 40) / 100.0
        max_pct = self._media_strategy.get("video_scene_pct_max", 30) / 100.0
        hard_cap = self._media_strategy.get("video_scene_hard_cap", 12)
        max_video = min(round(max_pct * n_scenes), hard_cap)

        # Build candidate list sorted by priority
        candidates: list[tuple[int, int]] = []  # (priority, idx) — higher = better
        cumulative = 0.0
        for idx, s in enumerate(scenes):
            dur = s.get("duration", 5)
            cumulative += dur
            pos_in_runtime = cumulative / total_duration
            tipo = s.get("tipo", "desarrollo")
            is_transition = s.get("is_transition", False)

            if is_transition:
                continue  # transitions never get video

            priority = 0

            # Hook / climax always candidates with top priority
            if tipo in ("hook", "climax"):
                priority = 100
            # Long scene in first half of runtime
            elif dur >= min_dur and pos_in_runtime <= first_half_pct:
                # Priority decays as we get further into the video
                pos_factor = 1.0 - (pos_in_runtime / first_half_pct)
                dur_factor = min(1.0, dur / max(min_dur * 2, 20))
                priority = int(50 * (pos_factor * 0.6 + dur_factor * 0.4))
                if tipo == "desarrollo":
                    priority += 5  # slight boost for desarrollo over otros

            if priority > 0:
                candidates.append((priority, idx))

        # Sort by priority descending, take top up to max_video
        candidates.sort(key=lambda x: x[0], reverse=True)
        video_scenes: set[int] = {idx for _, idx in candidates[:max_video]}

        # ── Stock-image priority: X% of non-video scenes try real stock first ──
        stock_image_pct = self._media_strategy.get("stock_image_pct", 0) / 100.0
        stock_image_scenes: set[int] = set()
        if stock_image_pct > 0:
            max_stock_image = round(stock_image_pct * n_scenes)
            si_candidates: list[tuple[int, int]] = []  # (priority, idx)
            for idx, s in enumerate(scenes):
                if idx in video_scenes:
                    continue  # already video-priority
                if s.get("is_transition", False):
                    continue
                # Hook/climax stay AI-first (coherence matters most there).
                if s.get("tipo", "desarrollo") in ("hook", "climax"):
                    continue
                dur = s.get("duration", 5)
                # Prefer longer scenes (real photos look better with more
                # screen time) and desarrollo/reflexion scenes.
                tipo = s.get("tipo", "desarrollo")
                tipo_bonus = 20 if tipo in ("desarrollo", "reflexion") else 0
                si_candidates.append((int(min(dur, 15) * 5) + tipo_bonus, idx))
            si_candidates.sort(key=lambda x: x[0], reverse=True)
            stock_image_scenes = {idx for _, idx in si_candidates[:max_stock_image]}

        # Build type map
        scene_types: dict[int, str] = {}
        for idx in range(n_scenes):
            if idx in video_scenes:
                scene_types[idx] = "video_priority"
            elif idx in stock_image_scenes:
                scene_types[idx] = "stock_image_priority"
            else:
                scene_types[idx] = "ai_image"

        logger.info(
            "Scene classification: %d video-priority / %d stock-image-priority / "
            "%d ai-image (%d scenes total, hook/climax: %d, max cap: %d)",
            len(video_scenes), len(stock_image_scenes),
            n_scenes - len(video_scenes) - len(stock_image_scenes), n_scenes,
            sum(1 for i in video_scenes if scenes[i].get("tipo") in ("hook", "climax")),
            max_video,
        )
        return video_scenes, stock_image_scenes, scene_types

    def _fetch_with_ai_tiers(
        self,
        scene: dict,
        scene_idx: int,
        total_scenes: int,
        is_video_priority: bool,
        target_dur: float,
        ctx,
        ai_used: int,
        ai_max: int,
        ai_enabled: bool,
        force_images: bool = False,
        is_stock_image_priority: bool = False,
    ) -> tuple[dict | None, int, dict]:
        """Fetch a media asset for *scene* using the 5-tier chain.

        **Video-priority chain:**
          TIER 1 → Stock Video (exhaustive)
          TIER 2 → AI Image (Pollinations → Local SD)
          TIER 3 → Stock Image (Pixabay → Unsplash)
          TIER 4 → Pollo AI (credits, last resort)
          TIER 5 → Placeholder

        **Stock-image chain** (``is_stock_image_priority``):
          TIER 1 → Stock Image (Pixabay → Unsplash)
          TIER 2 → AI Image (Pollinations → Local SD)
          TIER 3 → Stock Video (exhaustive)
          TIER 4 → Pollo AI (credits, last resort)
          TIER 5 → Placeholder

        **AI-image chain:**
          TIER 1 → AI Image (Pollinations → Local SD)  ← coherence first
          TIER 2 → Stock Image (Pixabay → Unsplash)
          TIER 3 → Stock Video (exhaustive)
          TIER 4 → Pollo AI (credits, last resort)
          TIER 5 → Placeholder

        Returns (asset, ai_used_updated, quality_info).

        *quality_info* is a dict with ``score``, ``provider``, ``resolution``,
        and ``query_attempt`` for ``_score_video_quality()``.  Empty dict
        if the asset is not a stock video.
        """
        quality_info: dict = {}
        query_pool = self._build_query_pool(scene, ctx)
        query_attempt = 0

        # ── Choose tiers based on scene type ─────────────────
        if is_video_priority and not force_images:
            tiers = ["stock_video", "ai_image", "stock_image", "pollo_ai", "placeholder"]
        elif is_stock_image_priority and not force_images:
            tiers = ["stock_image", "ai_image", "stock_video", "pollo_ai", "placeholder"]
        else:
            tiers = ["ai_image", "stock_image", "stock_video", "pollo_ai", "placeholder"]

        for tier in tiers:
            if tier == "stock_video":
                # Exhaustive search across all video providers
                asset = self._try_stock_video_tier(scene, query_pool, target_dur, ctx)
                if asset:
                    # Capture quality metadata for scoring
                    quality_info = {
                        "score": None,  # computed later by caller
                        "provider": asset.get("source", "unknown"),
                        "resolution": asset.get("resolution", "unknown"),
                        "query_attempt": 1,  # _try_stock_video_tier tracks internally
                    }
                    return asset, ai_used, quality_info

            elif tier == "ai_image" and self._ai_image_primary:
                asset = self._try_ai_image_chain(scene, scene_idx, total_scenes)
                if asset:
                    return asset, ai_used, quality_info

            elif tier == "stock_image":
                # Fall back to image-only providers (no video fallback)
                query_pool = self._build_query_pool(scene, ctx)
                asset = self._fetch_asset_exhaustive(
                    scene, query_pool, want_video=False,
                    target_dur=target_dur, ctx=ctx, force_images=True,
                )
                if asset:
                    return asset, ai_used, quality_info

            elif tier == "pollo_ai":
                if ai_enabled and ai_used < ai_max:
                    logger.info(
                        "Scene %d/%d: tier 4 — Pollo AI rescue (%d/%d)",
                        scene_idx + 1, total_scenes, ai_used + 1, ai_max,
                    )
                    pollo_asset = self._try_pollo_scene(
                        scene, scene_idx, total_scenes, ctx
                    )
                    if pollo_asset:
                        ai_used += 1
                        return pollo_asset, ai_used, quality_info

            elif tier == "placeholder":
                return None, ai_used, quality_info

        return None, ai_used, quality_info

    def _try_stock_video_tier(
        self,
        scene: dict,
        query_pool: list[str],
        target_dur: float,
        ctx,
    ) -> dict | None:
        """Exhaustive stock video search across all providers and queries.

        Uses the same `_fetch_asset_exhaustive` with video priority,
        but also captures resolution metadata for quality scoring.
        """
        return self._fetch_asset_exhaustive(
            scene, query_pool, want_video=True,
            target_dur=target_dur, ctx=ctx, force_images=False,
        )

    def _score_video_quality(
        self,
        provider_name: str,
        resolution: str,
        target_dur: float,
        actual_dur: float | None,
        queries_tried: int,
    ) -> float:
        """Score a video asset's quality from 0.0 to 1.0 using proxy signals.

        Components (weight):
          - **Provider tier** (0.30): pexels=0.30, pixabay=0.25, mixkit=0.20,
            coverr=0.15, youtube_cc=0.10.
          - **Resolution** (0.20): 2160p=0.20, 1080p=0.18, 720p=0.12, <720p=0.06.
          - **Duration match** (0.25): ratio in [0.8, 1.5]=0.25, [0.5, 2.0]=0.15, else=0.05.
          - **Query efficiency** (0.25): found in 1st query=0.25, 2nd-3rd=0.18, 4th+=0.10,
            fallback=0.05.

        Parameters
        ----------
        provider_name:
            Source string, e.g. ``"pexels"``, ``"pixabay"``, ``"youtube_cc"``.
        resolution:
            Resolution string like ``"1080p"``, ``"720p"``, ``"2160p"``.
        target_dur:
            Ideal duration in seconds (the scene's target).
        actual_dur:
            Actual duration of the downloaded clip, or ``None`` if unknown.
        queries_tried:
            How many query variations were exhausted before finding this asset
            (1 = first query worked, 2 = second query, etc.).
        """
        score = 0.0

        # ── Provider tier ─────────────────────────────────────
        provider_scores = {
            "pexels": 0.30, "pixabay": 0.25, "mixkit": 0.20,
            "coverr": 0.15, "youtube_cc": 0.10,
        }
        score += provider_scores.get(provider_name, 0.10)

        # ── Resolution ────────────────────────────────────────
        res = resolution.lower()
        if "2160" in res or "4k" in res:
            score += 0.20
        elif "1080" in res:
            score += 0.18
        elif "720" in res:
            score += 0.12
        elif "480" in res:
            score += 0.06
        else:
            score += 0.10  # unknown — be neutral

        # ── Duration match ────────────────────────────────────
        if actual_dur and target_dur > 0:
            ratio = actual_dur / target_dur
            if 0.8 <= ratio <= 1.5:
                score += 0.25
            elif 0.5 <= ratio <= 2.0:
                score += 0.15
            else:
                score += 0.05
        else:
            score += 0.10  # unknown duration

        # ── Query efficiency ──────────────────────────────────
        if queries_tried <= 1:
            score += 0.25
        elif queries_tried <= 3:
            score += 0.18
        elif queries_tried <= 6:
            score += 0.10
        else:
            score += 0.05

        return min(score, 1.0)

    def _should_continue_video_search(
        self, videos_found: int, n_scenes: int
    ) -> bool:
        """Decide whether to keep searching for stock videos.

        Rules:
          - videos_found < 20% minimum → always continue.
          - videos_found ≥ 30% maximum → always stop.
          - Between 20% and 30% → continue only if average quality ≥ threshold.
        """
        min_pct = self._media_strategy.get("video_scene_pct_min", 20) / 100.0
        max_pct = self._media_strategy.get("video_scene_pct_max", 30) / 100.0
        hard_cap = self._media_strategy.get("video_scene_hard_cap", 12)
        threshold = self._media_strategy.get("video_quality_threshold", 0.5)

        min_videos = max(1, round(min_pct * n_scenes))
        max_videos = min(round(max_pct * n_scenes), hard_cap)

        if videos_found < min_videos:
            return True   # haven't reached 20% minimum yet

        if videos_found >= max_videos:
            return False  # already at 30% / hard cap

        # Between 20% and 30% — check average quality
        if self._video_quality_scores:
            avg = sum(self._video_quality_scores) / len(self._video_quality_scores)
            return avg >= threshold

        return True  # no quality data yet → continue

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
                        "source": f"{provider.name}",
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
                        "source": f"{provider.name}",
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
                        "source": f"{provider.name}",
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
            # ── Duration validation: compare real vs claimed ────────────
            # Stock APIs (Pixabay, Pexels) often report inflated durations
            # (15s claimed → 6s real). Flag short-real videos so the renderer
            # knows to fill the gap instead of rejecting the scene entirely.
            try:
                import subprocess as _sp3
                result = _sp3.run(
                    ["ffprobe", "-v", "error", "-show_entries", "format=duration",
                     "-of", "csv=p=0", str(path)],
                    capture_output=True, text=True, timeout=10,
                )
                if result.returncode == 0 and result.stdout.strip():
                    real_dur = float(result.stdout.strip())
                    claimed_dur = getattr(asset, 'duration', 0) or 0
                    if claimed_dur > 0 and real_dur > 0 and real_dur < claimed_dur * 0.65:
                        logger.warning(
                            "Video duration mismatch: real %.1fs << claimed %.1fs "
                            "(%s). Accepting but renderer will fill gap.",
                            real_dur, claimed_dur, path.name,
                        )
            except Exception:
                pass  # non-critical
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
        # Also check Pixabay fallback (_fb) suffix — the same image
        # may have been saved with the fallback URL in a previous video.
        if predicted_fn and predicted_fn.endswith(".jpg"):
            fb_fn = predicted_fn[:-4] + "_fb.jpg"
            if fb_fn in self._cross_video_used_filenames:
                return True
            if fb_fn in self._used_filenames:
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

    @staticmethod
    def _theme_word_set(ctx) -> set[str]:
        """All theme-related words of a ThemeContext (keywords, motifs,
        primary subject, genre, era). Used to separate narrative keywords
        from theme anchoring in queries and relevance scoring."""
        theme_words: set[str] = set()
        if not ctx:
            return theme_words
        if getattr(ctx, "theme_keywords_en", None):
            for kw in ctx.theme_keywords_en:
                for w in str(kw).lower().split():
                    theme_words.add(w)
        if getattr(ctx, "key_motifs", None):
            for motif in ctx.key_motifs:
                for w in str(motif).lower().split():
                    theme_words.add(w)
        if getattr(ctx, "primary_subject", None):
            for w in ctx.primary_subject.lower().split():
                theme_words.add(w)
        if getattr(ctx, "genre", None) and ctx.genre != "documental":
            for w in ctx.genre.lower().replace("_", " ").split():
                theme_words.add(w)
        era_decade = getattr(ctx, "era_decade", "") or ""
        era = getattr(ctx, "era", "") or ""
        if era_decade.lower() not in ("atemporal", "presente", ""):
            theme_words.add(era_decade.lower())
        elif era.lower() not in ("atemporal", "presente", ""):
            for w in era.lower().replace("_", " ").split():
                theme_words.add(w)
        return theme_words

    @staticmethod
    def _extract_narrative_keywords(query: str, ctx) -> str:
        """Extract only the narrative keywords from a search query.

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

        # Gather all theme-related words to strip (shared helper so the
        # relevance scorer sees the exact same theme vocabulary)
        theme_words = MediaFetcher._theme_word_set(ctx)

        # Separate narrative words from theme/style words
        style_words = {
            "cinematic", "photography", "dramatic", "lighting", "atmospheric",
            "16:9", "moody", "high", "contrast", "professional", "dark",
            "atmosphere", "slow", "motion", "tracking", "shot", "aerial",
            "overhead", "style", "film", "video", "stock", "vertical",
            "establishing", "angle", "footage", "composition", "documentary",
            "depth", "field", "color", "grading", "grade",
            "wide", "close", "up", "detail", "low", "distant", "view",
            "alternative",
        }

        words = query.split()
        narrative_words = []
        theme_words_found = []

        for w in words:
            wl = w.lower().strip(",.!?;:")
            if wl in style_words:
                continue
            if wl in theme_words:
                theme_words_found.append(w)
            else:
                narrative_words.append(w)

        if not narrative_words:
            return ""  # query was entirely thematic — no narrative core to extract

        # Build: narrative keywords + at most 1 theme anchoring word
        result = " ".join(narrative_words[:6])

        # Pick the best single theme anchor from what was found
        if theme_words_found:
            anchor = theme_words_found[0]
            if len(result) + len(anchor) + 1 <= 100:
                result = f"{result} {anchor}"

        return result[:100]

    def _build_query_pool(self, scene: dict, ctx) -> list[str]:
        """Build an ordered list of query variations for a scene.

        Order (narrative priority): narrative-first queries try first,
        theme-anchored queries are fallbacks. This ensures that what you
        SEE matches what you HEAR, while maintaining visual context.

        Returns deduplicated list of non-empty queries (~11-13 variants).
        """
        base = scene.get("search_query_en", "")

        pool = []

        # 1. Base query (narrative + theme, roughly 60-70% / 30-40% ratio
        #    thanks to the improved LLM prompt in script_generator.py)
        if base and base.strip():
            pool.append(base.strip())

        scene_tipo = scene.get("tipo", "desarrollo")

        # 2. Narrative-heavy variant: strip most theme words, keep 1 anchor.
        #    This prioritizes narrative content with minimal theme anchoring.
        #    Comes BEFORE directional variations so narrative specificity
        #    wins over theme-heavy angle variations.
        narrative_heavy = self._extract_narrative_keywords(base, ctx) if base else ""
        if narrative_heavy and narrative_heavy != base and narrative_heavy.strip():
            pool.append(narrative_heavy.strip())

        # 2b. Era-anchored variant (HIGH priority for historical scenes).
        #     Guarantees the FIRST search of a historical scene carries the
        #     era anchor, even though the exhaustive path uses pool queries
        #     directly (they never pass through _build_search_query).
        era_phrase = None
        if ctx and self._media_strategy.get("era_anchor_enabled", True):
            from pipeline.era_terms import era_anchor
            try:
                era_phrase = era_anchor(ctx.era_decade, ctx.era)
            except Exception:
                era_phrase = None
        if era_phrase and base:
            v_narr = f"{narrative_heavy} {era_phrase}" if narrative_heavy else None
            v_base = f"{base} {era_phrase}"
            if v_narr and len(v_narr) <= 100 and v_narr not in pool:
                pool.append(v_narr)
            elif len(v_base) <= 100 and v_base not in pool:
                pool.append(v_base)

        # 3. Base + directional variations (reduced to 3 from 5 — keep the
        #    most distinct ones; wide/close/distant have most visual variety)
        if base:
            from pipeline.cinematic_staging import has_person_reference, sanitize_shot_direction
            has_person = has_person_reference(base)
            for suffix in [
                "wide shot establishing",
                sanitize_shot_direction("close-up detail", has_person=has_person),
                "distant view atmospheric",
            ]:
                v = f"{base} {suffix}"
                if len(v) <= 100:
                    pool.append(v)

        # 4. Simplified: just the key nouns (3-4 words, no style modifiers)
        simple = self._simplify_query(base) if base else ""
        if simple and simple != base and simple.strip():
            pool.append(simple)

        # 4b. Ultra-simplified: 2 core nouns. Drops overly-specific proper
        #     nouns (site names, etc.) that stock providers can't match, so
        #     the general subject still has a chance before generic fallbacks.
        ultra_simple = self._simplify_query(base, max_keywords=2) if base else ""
        if ultra_simple and ultra_simple not in pool and ultra_simple.strip():
            pool.append(ultra_simple)

        # 5. Without theme keywords — anti-poisoning fallback
        if ctx and ctx.theme_keywords_en and base:
            clean = _strip_theme_keywords(base, ctx.theme_keywords_en)
            if clean and clean != base and clean.strip():
                pool.append(clean)

        # 6. Themed fallback queries: built dynamically from ThemeContext.
        #    Topic-aware and moved BEFORE the generic type fallback so the
        #    video stays anchored to its actual subject.
        if ctx and hasattr(ctx, 'primary_subject') and ctx.primary_subject:
            from pipeline.cinematic_staging import build_contextual_fallback
            contextual_fb = build_contextual_fallback(scene_tipo, ctx)
            if contextual_fb and contextual_fb not in pool:
                pool.append(contextual_fb)
            themed_fb = self._build_themed_fallback(scene_tipo, ctx)
            if themed_fb and themed_fb not in pool:
                pool.append(themed_fb)

        # 7. Type-specific fallback queries (generic, near the end)
        type_fb = self._FALLBACK_BY_TYPE.get(scene_tipo, "")
        if type_fb and type_fb not in pool:
            pool.append(type_fb)

        # 8. Channel-configured fallback queries (absolute last resort)
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

    # ── Relevance & anachronism filtering (v9) ─────────────────

    def _candidate_text(self, candidate: dict) -> str:
        """Join candidate metadata (tags + title + page-url slug words) into
        lowercase text for relevance/anachronism matching. Empty when the
        provider exposed no metadata."""
        parts: list[str] = []
        tags = candidate.get("tags") or []
        if isinstance(tags, str):
            tags = [t.strip() for t in tags.split(",")]
        for t in tags:
            if t and str(t).strip():
                parts.append(str(t).strip())
        title = candidate.get("title") or ""
        if title and str(title).strip():
            parts.append(str(title).strip())
        page_url = candidate.get("page_url") or ""
        if page_url:
            # Pexels-style URLs embed a descriptive slug:
            # /video/aerial-view-of-city-123456/ → "aerial view city"
            m = re.search(r"/video/([^/?#]+)", str(page_url))
            if m:
                slug = re.sub(r"-\d+$", "", m.group(1))
                parts.extend(w for w in re.split(r"[-\s]+", slug.lower()) if w)
        return " ".join(parts).lower()

    def _scene_narrative_keywords(self, scene: dict, ctx) -> list[str]:
        """Narrative keywords of a scene: search_query_en minus style words
        and theme keywords (same vocabulary as _extract_narrative_keywords)."""
        query = scene.get("search_query_en", "") or ""
        if not query:
            return []
        theme_words = MediaFetcher._theme_word_set(ctx)
        kws: list[str] = []
        for w in query.split():
            wl = w.lower().strip(",.!?;:")
            if not wl:
                continue
            if wl in MediaFetcher._STYLE_WORDS:
                continue
            if wl in theme_words:
                continue
            kws.append(wl)
        return kws

    def _is_anachronistic(self, candidate: dict, ctx) -> bool:
        """True when the scene is historical (era anchor resolved) and the
        candidate metadata contains modern/anachronistic terms."""
        if not ctx:
            return False
        from pipeline.era_terms import anachronism_hits, era_anchor
        try:
            if era_anchor(ctx.era_decade, ctx.era) is None:
                return False  # not a historical scene → no anachronism check
        except Exception:
            return False
        text = self._candidate_text(candidate)
        if not text:
            return False
        return bool(anachronism_hits(text))

    def _relevance_score(self, candidate: dict, scene: dict, ctx) -> float:
        """Score how well a candidate matches the scene's narrative keywords.

        Base: +1 per narrative keyword found in candidate metadata (tags /
        title / page-url slug). Strong penalty: -3 per anachronistic term
        found in a historical scene. Returns a float >= 0; candidates with
        no metadata score 0 (caller treats that as "unknown", not "bad").
        """
        kws = self._scene_narrative_keywords(scene, ctx)
        if not kws:
            return 0.0
        text = self._candidate_text(candidate)
        if not text:
            return 0.0

        text_tokens = set(text.split())
        found = 0
        for kw in kws:
            if " " in kw:
                if kw in text:
                    found += 1
            elif kw in text_tokens:
                found += 1

        score = float(found)

        if found > 0 and self._is_anachronistic(candidate, ctx):
            from pipeline.era_terms import anachronism_hits
            try:
                penalty = 3.0 * len(anachronism_hits(text))
            except Exception:
                penalty = 3.0
            score -= penalty

        return max(0.0, score)

    def _llm_relevance_filter(self, candidates: list[dict], scene: dict) -> list[dict] | None:
        """Reorder up to 8 candidates with ONE LLM call so the best match
        comes first. Returns the reordered list, or None on any failure
        (caller keeps the original order — never blocks the pipeline)."""
        try:
            from config.llm_client import create_llm_client
            from config.llm_helpers import llm_json_call_or_fallback
            from config.settings import LLM_MODEL_CREATIVE
        except Exception:
            return None

        numbered = []
        for i, c in enumerate(candidates[:8]):
            tags = c.get("tags") or []
            if isinstance(tags, str):
                tags = [t.strip() for t in tags.split(",")]
            tags_txt = ", ".join(str(t) for t in tags[:8]) if tags else "(sin tags)"
            title = str(c.get("title") or "")
            page_url = str(c.get("page_url") or "")[:90]
            numbered.append(
                f"{i}. tags: {tags_txt} | título: {title} | url: {page_url}"
            )

        scene_text = (str(scene.get("texto", "")) or "")[:400]
        user_prompt = (
            "Escena de un documental (texto narrado):\n"
            f'"{scene_text}"\n\n'
            "Candidatos de stock (índice y metadatos):\n"
            + "\n".join(numbered)
            + "\n\nElige el índice del clip que MEJOR encaja visualmente con la "
              "escena, evitando anacronismos (material moderno en escenas "
              "históricas). "
              'Responde SOLO con JSON: {"best_index": N}'
        )

        try:
            client = create_llm_client(timeout=45.0, max_retries=1)
            data = llm_json_call_or_fallback(
                client,
                fallback={},
                max_retries=1,
                retry_delay=1.0,
                model=LLM_MODEL_CREATIVE,
                messages=[
                    {"role": "system", "content": (
                        "Eres un director de arte de stock footage. Eliges el clip "
                        "más coherente con la narración de la escena."
                    )},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=0.2,
                max_tokens=20,
                response_format={"type": "json_object"},
            )
        except Exception:
            return None

        best_index = None
        try:
            best_index = int(data.get("best_index"))
        except (AttributeError, TypeError, ValueError):
            return None

        if not (0 <= best_index < len(candidates)):
            return None

        ordered = [candidates[best_index]]
        ordered.extend(
            c for i, c in enumerate(candidates) if i != best_index
        )
        logger.info(
            "LLM relevance filter: best candidate %d/%d for scene %r",
            best_index, len(candidates), (scene.get("search_query_en", "") or "")[:60],
        )
        return ordered

    def _try_download_best_candidate(
        self, asset_candidates: list[dict], provider, scene: dict, ctx,
    ) -> dict | None:
        """Score one page of candidates by narrative relevance (era-aware),
        skip anachronistic ones, optionally refine with an LLM call, and
        download the best non-duplicate that succeeds.

        Defensive by design: candidates without metadata score 0 and are
        still downloadable (conservative fallback — never blocks a page).
        """
        scored: list[tuple[float, dict]] = []
        for candidate in asset_candidates:
            if self._is_asset_duplicate(candidate):
                continue
            if self._is_anachronistic(candidate, ctx):
                text = self._candidate_text(candidate)
                logger.info(
                    "Skipping anachronistic candidate for historical scene: %s",
                    text[:100] or candidate.get("url", "")[:60],
                )
                continue
            score = self._relevance_score(candidate, scene, ctx)
            scored.append((score, candidate))

        if not scored:
            return None

        scored.sort(key=lambda pair: pair[0], reverse=True)

        # Prefer candidates that clear the relevance threshold; if none do
        # (e.g. no metadata), fall back to the full page (current behavior).
        min_overlap = float(self._media_strategy.get("relevance_min_overlap", 1))
        if scored[0][0] >= min_overlap:
            ordered = [c for s, c in scored if s >= min_overlap]
        else:
            ordered = [c for _, c in scored]

        # Optional LLM refinement (opt-in per channel via media strategy)
        if (
            self._media_strategy.get("llm_relevance_filter", False)
            and len(ordered) >= 2
        ):
            reordered = self._llm_relevance_filter(ordered, scene)
            if reordered:
                ordered = reordered

        for candidate in ordered:
            downloaded = self._download_candidate(provider, candidate)
            if downloaded and downloaded.get("path"):
                self._record_asset_used(candidate)
                self._record_asset_for_history(downloaded)
                return downloaded
        return None

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
        providers, sparing_providers = self._interleaved_providers(want_video, scene, force_images=force_images)

        # ── First pass: exhaustive search across all primary providers ──
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

                    # Relevance-aware download: skips anachronistic candidates,
                    # prefers high-scoring ones, falls back conservatively when
                    # providers expose no metadata (v9).
                    downloaded = self._try_download_best_candidate(
                        asset_candidates, provider, scene, ctx,
                    )
                    if downloaded:
                        return downloaded

                    # Check if there are more pages
                    total = asset_candidates[0].get("_total_available", 0)
                    per_page = asset_candidates[0].get("_per_page", 20)
                    if total <= 0 or page * per_page >= total:
                        break
                    page += 1
                    time.sleep(0.15)

        # ── Second pass (last resort): sparing providers (Unsplash) across all queries ──
        # This only runs when primary providers (Pixabay + video) found nothing.
        # Preserves the Unsplash 45 req/hour budget for scenes that truly need it.
        if sparing_providers:
            logger.debug("Primary providers exhausted — falling back to sparing providers (Unsplash)")
            for query in query_pool:
                if not query or not query.strip():
                    continue
                for provider in sparing_providers:
                    page = 1
                    while True:
                        asset_candidates = self._search_provider_page(
                            provider, query, target_dur, page, want_video,
                        )
                        if not asset_candidates:
                            break
                        downloaded = self._try_download_best_candidate(
                            asset_candidates, provider, scene, ctx,
                        )
                        if downloaded:
                            return downloaded
                        total = asset_candidates[0].get("_total_available", 0)
                        per_page = asset_candidates[0].get("_per_page", 20)
                        if total <= 0 or page * per_page >= total:
                            break
                        page += 1
                        time.sleep(0.15)

        return None

    def _interleaved_providers(self, want_video: bool, scene: dict, force_images: bool = False) -> tuple[list, list]:
        """Return (primary_providers, sparing_providers) ordered by preference.

        Primary providers are searched exhaustively across all queries first.
        Sparing providers (rate-limited services like Unsplash) are only tried
        as a last resort after primary providers are exhausted across all queries.
        This preserves the Unsplash 45-req/hour budget for scenes that truly need it.

        When force_images is True (RAM safety cap), video providers are excluded
        entirely to prevent OOM from too many downloaded video assets.
        """
        is_transition = scene.get("is_transition", False)
        if is_transition:
            # Transitions always use images (Ken Burns)
            return (self._get_all_image_providers(), [])

        # Separate Pixabay (primary, high capacity) from Unsplash (sparing, 45 req/hr)
        pixabay_only = [self._pixabay_img] if self._pixabay_img else []
        unsplash_only = [self._unsplash] if self._unsplash else []

        if force_images:
            # RAM safety cap: only image providers, no video fallback
            return (pixabay_only, unsplash_only)

        video_providers_list = list(self.video_providers) if self.video_providers else []

        if want_video:
            # Video-first: exhaust all video providers, then Pixabay, Unsplash as last resort
            return (video_providers_list + pixabay_only, unsplash_only)
        else:
            # Image-first: exhaust Pixabay + video providers, Unsplash as last resort
            return (pixabay_only + video_providers_list, unsplash_only)

    def _get_all_image_providers(self) -> list:
        """Return all active image providers (Pixabay + Unsplash) as a list."""
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
                    "source": pname,
                    "provider": pname,
                    # Metadata for relevance/anachronism filtering (v9).
                    # Providers that don't expose it leave empty defaults.
                    "title": getattr(asset, "title", ""),
                    "tags": getattr(asset, "tags", []) or [],
                    "page_url": getattr(asset, "page_url", ""),
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

        max_retries = 3
        last_content_len = 0

        for attempt in range(max_retries):
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
                    logger.warning(
                        "Image download too small (%d bytes) — likely HTML/error page "
                        "(attempt %d/%d): %s",
                        content_len, attempt + 1, max_retries, url[:100],
                    )
                    last_content_len = content_len
                    if attempt < max_retries - 1:
                        time.sleep(2 ** attempt)
                        continue
                    self._bad_image_urls.add(url)
                    self._bad_image_urls_ts = _time.time()
                    return None

                # JPEG magic bytes check
                if content[:3] != b'\xff\xd8\xff':
                    logger.warning(
                        "Downloaded content is not JPEG (magic=%r) — retry or discard "
                        "(attempt %d/%d): %s",
                        content[:8], attempt + 1, max_retries, url[:100],
                    )
                    if attempt < max_retries - 1:
                        time.sleep(2 ** attempt)
                        continue
                    self._bad_image_urls.add(url)
                    self._bad_image_urls_ts = _time.time()
                    return None

                # ── Success path ─────────────────────────────────
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

            except requests.exceptions.Timeout:
                logger.warning(
                    "Image download timeout (attempt %d/%d): %s",
                    attempt + 1, max_retries, url[:80],
                )
            except requests.exceptions.ConnectionError as exc:
                logger.warning(
                    "Image download connection error (attempt %d/%d): %s",
                    attempt + 1, max_retries, exc,
                )
            except requests.exceptions.HTTPError as exc:
                status = exc.response.status_code if exc.response is not None else 0
                if status >= 500:
                    logger.warning(
                        "Image download server error %d (attempt %d/%d): %s",
                        status, attempt + 1, max_retries, url[:80],
                    )
                else:
                    # 4xx — don't retry, blacklist immediately
                    logger.error("Image download HTTP error %d: %s", status, url[:100])
                    self._bad_image_urls.add(url)
                    self._bad_image_urls_ts = _time.time()
                    return None
            except Exception as exc:
                logger.warning(
                    "Image download error (attempt %d/%d): %s",
                    attempt + 1, max_retries, exc,
                )

            if attempt < max_retries - 1:
                sleep_time = 2 ** attempt  # 1, 2, 4
                time.sleep(sleep_time)

        # Exhausted all retries — blacklist and return None
        logger.error("Image download failed after %d attempts: %s", max_retries, url[:100])
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

    @staticmethod
    def _is_valid_ai_image(filepath: Path) -> bool:
        """Lenient validation for AI-generated images.

        AI images (especially from free providers) may be smaller than
        stock photos due to simpler scenes or efficient encoding.  We
        only require a valid JPEG header and a minimum of 5 KB to
        filter out completely broken/garbage responses.
        """
        try:
            if not filepath.exists():
                return False
            size = filepath.stat().st_size
            if size < 5000:   # 5 KB minimum — anything smaller is likely broken
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

    # ── Internal: themed fallback query builder (v8) ────────────────

    @staticmethod
    def _build_themed_fallback(scene_tipo: str, ctx) -> str:
        """Build a fallback search query dynamically from the ThemeContext.

        When all specific queries fail, this provides a last-resort query
        that is at least anchored to the video's visual world (primary_subject,
        key_motifs, mood) rather than a completely generic fallback.

        Args:
            scene_tipo: The block type (hook, desarrollo, climax, reflexion, cierre)
            ctx: ThemeContext with primary_subject, key_motifs, mood, genre, etc.

        Returns:
            A themed fallback query string (in English), or empty string if no
            theme data is available.
        """
        if not ctx:
            return ""

        # Gather available thematic anchors
        anchors: list[str] = []

        # Primary subject (best anchor when available — English)
        if ctx.primary_subject:
            ps_words = ctx.primary_subject.split()[:4]
            anchors.extend(ps_words)

        # English theme keywords (built for stock search) — prefer these
        # over the Spanish key_motifs below.
        if ctx.theme_keywords_en:
            for kw in ctx.theme_keywords_en[:4]:
                anchors.extend(kw.split()[:2])

        # Key motifs (Spanish visual icons) — only used if no English
        # anchors were found above, since stock providers need English.
        if not anchors and ctx.key_motifs:
            for motif in ctx.key_motifs[:3]:
                motif_words = motif.split()[:2]
                anchors.extend(motif_words)

        # Mood → English mapping for stock query compatibility
        mood_map = {
            "misterioso": "mysterious", "épico": "epic", "ominoso": "ominous",
            "melancólico": "melancholic", "esperanzador": "hopeful",
            "sereno": "serene", "perturbador": "disturbing",
        }
        mood_word = mood_map.get(ctx.mood, "") if ctx.mood else ""

        if not anchors:
            return ""  # No thematic data to build from

        # Deduplicate while preserving order
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

        # Compose per-type fallback with mood suffix
        if mood_word:
            mood_suffix = f" {mood_word}" if len(base) + len(mood_word) + 2 < 100 else ""
        else:
            mood_suffix = ""

        by_type = {
            "hook": f"{base}{mood_suffix} dramatic atmosphere",
            "desarrollo": f"{base} documentary establishing shot",
            "climax": f"{base}{mood_suffix} dark tension shadow",
            "reflexion": f"{base}{mood_suffix} contemplative silence",
            "cierre": f"{base}{mood_suffix} resolution hope ending",
        }

        result = by_type.get(scene_tipo, f"{base}{mood_suffix} cinematic")
        # Truncate to 100 chars (Pixabay limit) at last complete word
        if len(result) > 100:
            result = result[:100].rsplit(" ", 1)[0]
        return result

    # ── Internal: query simplification ────────────────────────────

    @staticmethod
    def _simplify_query(query: str, max_keywords: int = 4) -> str:
        """Take first N keywords from a query (skip style modifiers).

        ``max_keywords=4`` returns a 3-4 word query; ``max_keywords=2``
        returns an ultra-simplified query that drops overly-specific
        proper nouns (e.g. site names) so stock providers have a chance
        of matching the general subject.
        """
        words = query.split()
        # Skip known style words
        style_words = {
            "cinematic", "photography", "dramatic", "lighting", "atmospheric",
            "16:9", "moody", "high", "contrast", "professional", "dark",
            "atmosphere", "slow", "motion", "tracking", "shot", "aerial",
            "overhead", "style", "film", "video", "stock",
        }
        keywords = [w for w in words if w.lower() not in style_words]
        return " ".join(keywords[:max_keywords])

    # ── Internal: smart query builder ────────────────────────────
    _STYLE_WORDS: set[str] = {
        "cinematic", "photography", "dramatic", "lighting", "atmospheric",
        "16:9", "moody", "high", "contrast", "professional", "dark",
        "atmosphere", "slow", "motion", "tracking", "shot", "aerial",
        "overhead", "style", "film", "video", "stock", "vertical",
        "establishing", "angle", "footage", "composition", "documentary",
        # NOTE: "historical" is intentionally NOT a style word anymore —
        # it is a real content signal (era anchor) that must survive
        # query building so historical scenes stay period-anchored.
        "depth", "field", "color", "grading", "grade",
    }

    @staticmethod
    def _build_search_query(
        query: str,
        variation: str | None = None,
        theme_keywords: list[str] | None = None,
        theme_ctx = None,  # ThemeContext object (v8 — full context)
        max_len: int = 100,
        era_enabled: bool = True,
    ) -> str:
        """Build a search query that fuses scene-specific narrative content with
        video-level theme context.

        Strategy (v9 — full ThemeContext fusion + era anchoring):
        1. Extract scene narrative keywords from the LLM query (primary subject)
        2. Resolve a HIGH-priority era anchor (e.g. "17th century wooden sailing
           ship") from ThemeContext.era_decade/era when the scene is historical.
           Its budget is reserved BEFORE the rest is split, so it is never the
           first thing trimmed.
        3. Add primary_subject / genre from ThemeContext as contextual anchors
        4. Add 1-2 theme keywords as additional anchors (dedup with anchors above)
        5. Filter out forbidden elements from the final query
        6. Fit all within ``max_len`` chars, trimming from lowest priority.

        The scene narrative content always leads the query so stock APIs weight it
        higher. Theme context fields serve as contextual anchors.
        """
        # Ground recurring labels (money, expedition, archive investigation)
        # in actions/period objects before allocating the strict stock budget.
        from pipeline.cinematic_staging import enrich_scene_query, sanitize_person_query
        query = enrich_scene_query(query, theme_ctx)
        query = sanitize_person_query(query)

        # 1. Extract scene topic keywords (strip style words, keep content nouns)
        words = query.split()
        scene_keywords = [w for w in words if w.lower() not in MediaFetcher._STYLE_WORDS]
        if not scene_keywords:
            scene_keywords = [w for w in words]  # fallback: all words

        # If both query and theme are empty, return the original query as-is
        has_theme_data = bool(theme_keywords or (theme_ctx and (
            theme_ctx.primary_subject or theme_ctx.era_decade or theme_ctx.genre
        )))
        if not scene_keywords and not has_theme_data:
            return query.strip()[:max_len]

        # 2. Era anchor — HIGH priority for historical scenes (v9).
        #    Resolved deterministically from the ThemeContext era fields
        #    (era_terms.era_anchor). Returns None for timeless/present/future
        #    eras, in which case behavior stays era-agnostic.
        era_phrase: str | None = None
        if era_enabled and theme_ctx:
            from pipeline.era_terms import era_anchor
            try:
                era_phrase = era_anchor(theme_ctx.era_decade, theme_ctx.era)
            except Exception:
                era_phrase = None

        # 2b. Gather contextual anchors from ThemeContext (primary_subject / genre).
        #     Era is NOT added here when era_phrase resolved (it is appended
        #     separately with reserved budget).
        ctx_anchors: list[str] = []
        if theme_ctx:
            # Primary subject keywords (e.g. "ancient Egyptian civilization")
            if theme_ctx.primary_subject:
                ps_words = [w for w in theme_ctx.primary_subject.split()
                           if w.lower() not in MediaFetcher._STYLE_WORDS]
                ctx_anchors.extend(ps_words[:3])
            # Era/decade as low-priority anchor ONLY when no era_phrase resolved
            if not era_phrase:
                if theme_ctx.era_decade and theme_ctx.era_decade.lower() not in ("atemporal", "presente", ""):
                    ctx_anchors.append(theme_ctx.era_decade)
                elif theme_ctx.era and theme_ctx.era.lower() not in ("atemporal", "presente", ""):
                    # era field might be like "siglo_XIII" — extract meaningful part
                    era_clean = theme_ctx.era.replace("_", " ").strip()
                    if era_clean and len(era_clean) <= 15:
                        ctx_anchors.extend(era_clean.split()[:2])
            # Genre as anchor (only if not generic "documental")
            if theme_ctx.genre and theme_ctx.genre.lower() not in ("documental", "documentary", ""):
                genre_clean = theme_ctx.genre.replace("_", " ").strip()
                ctx_anchors.extend(genre_clean.split()[:2])

        # Dedup ctx_anchors against scene part (to be done after scene_part is built)
        ctx_anchors = list(dict.fromkeys(ctx_anchors))  # preserve order, remove dups

        # 3. Allocate character budget — the era anchor gets a RESERVED slice
        #    first (enough for phrases like "17th century wooden sailing ship"
        #    up to min(35, max(20, 35% of max_len)) chars) so it always fits
        #    and is never trimmed before the scene content.
        era_budget = 0
        if era_phrase:
            era_budget = min(max_len, len(era_phrase) + 1)
            era_budget = min(era_budget, max(20, int(max_len * 0.35)))
        remaining = max_len - era_budget

        #    scene: ~60% of remaining (narrative subject — primary visual content)
        #    ctx_anchors: ~15% (primary_subject / genre — defines visual world)
        #    theme: ~15% (theme_keywords_en — complementary anchors)
        #    variation: ~10% (visual diversity — lowest priority, dropped first)
        variation_budget = min(12 if variation else 0, max(0, remaining))
        theme_budget = min(15, max(0, remaining - variation_budget))
        ctx_budget = min(15, max(0, remaining - variation_budget - theme_budget))
        scene_budget = max(0, remaining - variation_budget - ctx_budget - theme_budget)

        # 4. Build scene narrative part (fit within scene_budget chars)
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
            elif has_theme_data:
                # No scene keywords — build from context anchors + theme keywords instead
                remaining = max_len - era_budget - (len(variation) + 1 if variation else 0)
                all_anchors = ctx_anchors[:2] + (theme_keywords or [])
                for kw in all_anchors[:3]:
                    candidate = f"{scene_part} {kw}".strip()
                    if len(candidate) <= max(remaining, 10):
                        scene_part = candidate
                    else:
                        break
                ctx_anchors = []  # already consumed
                theme_keywords = None  # already consumed

        # 5. Build ctx_anchor part (primary_subject/genre — max 2 keywords, dedup)
        ctx_part = ""
        if ctx_anchors:
            scene_lower = scene_part.lower()
            fresh_anchors = [a for a in ctx_anchors[:2] if a.lower() not in scene_lower]
            remaining = max_len - len(scene_part) - era_budget - len(variation if variation else "") - 1
            for kw in fresh_anchors:
                candidate = f"{ctx_part} {kw}".strip()
                if len(candidate) <= max(remaining, 8):
                    ctx_part = candidate
                else:
                    break

        # 6. Build theme context part (theme_keywords — max 2 keywords, dedup with scene AND ctx)
        theme_part = ""
        if theme_keywords:
            scene_and_ctx = (scene_part + " " + ctx_part).lower()
            fresh_keywords = [
                kw for kw in theme_keywords[:2]
                if kw.lower() not in scene_and_ctx
            ]
            remaining = max_len - len(scene_part) - era_budget - len(ctx_part)
            if variation:
                remaining -= (len(variation) + 1)  # space + variation
            for kw in fresh_keywords:
                candidate = f"{theme_part} {kw}".strip()
                if len(candidate) <= max(remaining, 10):
                    theme_part = candidate
                else:
                    break

        # 7. Assemble: scene (primary) + era anchor (HIGH priority, after subject)
        #    + ctx_anchor (visual world) + variation (diversity) + theme (anchor)
        parts = [scene_part]
        era_used = False
        if era_phrase:
            # Avoid duplicating an era anchor the LLM query already carries
            # (e.g. "17th century wooden sailing ship arctic ice crew")
            anchor_prefix = " ".join(era_phrase.split()[:2])
            if anchor_prefix and anchor_prefix not in scene_part.lower():
                parts.append(era_phrase)
                era_used = True
        if ctx_part:
            parts.append(ctx_part)
        if variation:
            parts.append(variation)
        if theme_part:
            parts.append(theme_part)

        result = " ".join(parts)

        # 7b. Historical guard: append "historical" at the end for historical
        #     eras whose anchor does not already carry it, if the query has room.
        if era_used and "historical" not in era_phrase.lower():
            guard_candidate = f"{result} historical"
            if len(guard_candidate) <= max_len:
                result = guard_candidate

        # 8. Final safety: if still over budget, trim from right at last complete word
        #    (trims variation/theme first — the era anchor is protected by its
        #    reserved budget and sits before them).
        if len(result) > max_len:
            result = result[:max_len].rsplit(" ", 1)[0]

        # 9. Forbidden elements safety net (v8): strip any word from the
        #    forbidden_elements list that may have slipped into the query
        if theme_ctx and theme_ctx.forbidden_elements:
            result_lower = result.lower()
            for forbidden in theme_ctx.forbidden_elements:
                fb_lower = forbidden.lower().strip()
                if fb_lower and fb_lower in result_lower:
                    # Remove the forbidden word/phrase from the query
                    import re as _re
                    result = _re.sub(r'\b' + _re.escape(forbidden) + r'\b', '', result, flags=_re.IGNORECASE)
                    result = _re.sub(r'\s{2,}', ' ', result).strip()
                    logger.warning(
                        "Forbidden element '%s' removed from search query (query was: %r)",
                        forbidden, result or "(empty after removal)",
                    )
                    if not result or len(result) < 5:
                        # Query destroyed by forbidden removal — rebuild without the forbidden word's context
                        result = " ".join([p for p in parts if forbidden.lower() not in p.lower()])
                        if not result:
                            result = query.strip()[:max_len]  # fallback to original

        return result
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
