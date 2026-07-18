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

from config import canal2_config as _default_cfg
from config import settings
from pipeline.image_fetcher import UnsplashProvider, PexelsProvider
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
        self._config = config or _default_cfg
        self._media_strategy = getattr(self._config, "MEDIA_STRATEGY", {})

        # P3: Theme context for enriched search queries
        self._theme_context = None

        # ── Video provider chain ───────────────────────────────
        self.video_providers: list = []
        self._build_video_provider_chain()

        # ── Image providers (reuse from image_fetcher) ─────────
        self._unsplash: UnsplashProvider | None = None
        self._pexels: PexelsProvider | None = None
        self._unsplash_consecutive_empty = 0
        self._unsplash_disabled_until: float | None = None
        self._pexels_consecutive_empty = 0
        self._pexels_disabled_until: float | None = None

        if settings.UNSPLASH_ACCESS_KEY:
            self._unsplash = UnsplashProvider(settings.UNSPLASH_ACCESS_KEY)
        else:
            logger.warning("UNSPLASH_ACCESS_KEY not set — Unsplash disabled")

        if settings.PEXELS_API_KEY:
            self._pexels = PexelsProvider(settings.PEXELS_API_KEY)
        else:
            logger.warning("PEXELS_API_KEY not set — Pexels photos disabled")

        if self._unsplash is None and self._pexels is None and not self.video_providers:
            logger.error("No media providers configured! Set UNSPLASH_ACCESS_KEY or PEXELS_API_KEY")

        # ── Deduplication tracking ─────────────────────────────
        # Reset at the start of each fetch_for_script() call so the
        # same asset is never reused across multiple blocks within a
        # single video build.
        self._used_asset_urls: set[str] = set()
        # Image-specific dedup tracking (URLs + content hashes)
        self._used_image_urls: set[str] = set()

        # ── Pollo AI scene generator (lazy, avoids ~7 min per image unless absolutely needed) ─
        self._pollo_scene_gen = None
        self._ai_fallback_enabled = self._media_strategy.get("ai_image_fallback", False)

    # ── Pollo lazy init ──────────────────────────────────────────

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
        query = block.get("search_query_en", "")
        media_tipo = block.get("media_tipo", "imagen")
        target_dur = target_duration or block.get("media_duracion", 5)
        block_tipo = block.get("tipo", "desarrollo")

        # Enrich query with theme keywords if available (P3)
        ctx = theme_context or self._theme_context
        if ctx and ctx.theme_keywords_en:
            theme_kw = " ".join(ctx.theme_keywords_en[:3])
            enriched_query = f"{query} {theme_kw}"
            logger.info("Enriched query with theme: %r", enriched_query[:100])
            query = enriched_query

        prefer_video = self._media_strategy.get("prefer_video", True) and media_tipo == "video"

        logger.info("Fetching media for block: tipo=%s query=%r dur=%.1fs",
                     media_tipo, query[:80], target_dur)

        # ── Step 1: Try video providers (in priority order) ──
        if prefer_video and self.video_providers:
            result = self._try_video_providers(query, target_dur)
            if result:
                return result

        # ── Step 2: Try Pexels image (200 req/h — primary) ──
        result = self._try_image_pexels(query)
        if result:
            return result

        # ── Step 3: Try Unsplash image (50 req/h — fallback) ──
        result = self._try_image_unsplash(query)
        if result:
            return result

        # [POLLO_AI_HOOK] ─ Agent 2B: Insert Pollo AI image generation fallback here
        # result = self._try_pollo_ai(query)
        # if result: return result

        # ── Step 4: Retry with simplified query (Pexels + Unsplash) ──
        simple_query = self._simplify_query(query)
        if simple_query != query:
            logger.info("Retrying with simplified query: %r", simple_query)
            result = self._try_image_pexels(simple_query) or \
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

        # Reset provider cooldowns for a fresh script
        self._unsplash_consecutive_empty = 0
        self._unsplash_disabled_until = None
        self._pexels_consecutive_empty = 0
        self._pexels_disabled_until = None

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
        logger.info(
            "Ratio governor: %d scenes, target %d video (%.0f%%), "
            "max placeholder %.0f%%",
            n_scenes, target_video_count, target_video_pct,
            max_placeholder_pct,
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
        video_ok = 0
        image_ok = 0
        placeholder = 0

        # Pollo AI counter (capped at ai_max_per_video)
        ai_max = self._media_strategy.get("ai_max_per_video", 2)
        ai_used = 0
        ai_enabled = self._media_strategy.get("ai_image_fallback", False)

        # ── Circuit breaker: abort if all providers are exhausted ──
        # When both Pexels (429) and Unsplash (403) fail repeatedly,
        # abort the entire phase instead of retrying 38+ scenes.
        _cb_consecutive_all_provider_failures = 0
        _cb_max_consecutive_failures = 5  # abort after 5 consecutive all-provider failures
        _cb_phase_start = time.time()
        _cb_phase_timeout = 900  # 15 min hard timeout for the entire media phase

        # Track sub-scene sequence per asset_idx for query variation
        subscene_seq: dict[int, int] = {}

        for i, scene in enumerate(scenes):
            want_video = i in video_assigned
            target_dur = scene.get("duration", 5)
            query = scene.get("search_query_en", "")
            scene_tipo = scene.get("tipo", "desarrollo")
            is_hook = (scene_tipo == "hook")

            # ── Transition scenes always use image (Ken Burns) ─
            if scene.get("is_transition"):
                want_video = False

            # Sub-scene query variation: for split scenes, vary the search query
            # so each sub-scene gets a visually distinct result.
            asset_idx = scene.get("asset_idx", i)
            is_sub = scene.get("is_subscene", False)
            seq = subscene_seq.get(asset_idx, 0)
            subscene_seq[asset_idx] = seq + 1
            if is_sub and seq > 0:
                variations = [
                    "wide shot establishing",
                    "close-up detail",
                    "alternative angle composition",
                    "low angle dramatic",
                    "distant view atmospheric",
                ]
                var = variations[(seq - 1) % len(variations)]
                query = f"{query}, {var}" if query else var

            # Enrich query with theme keywords if available
            if ctx and ctx.theme_keywords_en:
                theme_kw = " ".join(ctx.theme_keywords_en[:3])
                query = f"{query} {theme_kw}"

            logger.info(
                "Scene %d/%d [%s]: want_video=%s query=%r dur=%.1fs",
                i + 1, n_scenes, scene_tipo, want_video, query[:80], target_dur,
            )

            asset = None

            # ── Pollo AI: hook siempre (si está activo y bajo cap) ─
            if is_hook and ai_enabled and ai_used < ai_max:
                logger.info("Scene %d [HOOK]: using Pollo AI (%d/%d)", i + 1, ai_used + 1, ai_max)
                asset = self._try_pollo_scene(query, scene_tipo, ctx)
                if asset:
                    ai_used += 1

            # ── Try video if assigned (skip if Pollo already gave us an asset) ─
            if asset is None and want_video and self.video_providers:
                asset = self._try_video_providers(query, target_dur)

            # ── Try image (dedup-aware) if no video yet ───────
            if asset is None:
                # Build skip_urls from already-used image URLs
                image_skip = self._used_image_urls.copy()
                # Pexels primary (200 req/h), Unsplash fallback (50 req/h)
                result = self._try_image_pexels(query, skip_urls=image_skip)
                if result:
                    asset = result
                else:
                    result = self._try_image_unsplash(query, skip_urls=image_skip)
                    if result:
                        asset = result

            # ── Fallback: simplified query (Pexels + Unsplash) ─
            if asset is None:
                # Simplified query retry
                simple_query = self._simplify_query(query)
                if simple_query != query:
                    logger.info("Scene %d: retrying with simplified query: %r", i, simple_query)
                    image_skip = self._used_image_urls.copy()
                    asset = self._try_image_pexels(simple_query, skip_urls=image_skip) or \
                             self._try_image_unsplash(simple_query, skip_urls=image_skip)

            # ── Hard fallback: generic queries that always find something ─
            if asset is None:
                generic_queries = self._media_strategy.get("fallback_queries", [
                    "historical documentary archival photography cinematic 16:9",
                    "ancient history artifacts museum exhibition documentary",
                    "nature landscape exploration discovery documentary cinematic",
                    "dramatic wilderness storm ocean survival documentary",
                    "old architecture cathedral historical building documentary",
                    "dark mystery abandoned exploration atmosphere cinematic",
                ])
                import random as _random
                fb_query = _random.choice(generic_queries)
                logger.info("Scene %d: trying hard fallback query: %r", i, fb_query)
                image_skip = self._used_image_urls.copy()
                asset = self._try_image_pexels(fb_query, skip_urls=image_skip) or \
                        self._try_image_unsplash(fb_query, skip_urls=image_skip)

            # ── Pollo AI: rescate si stock falló completamente y hay cupo ─
            if asset is None and ai_enabled and ai_used < ai_max:
                logger.info("Scene %d [%s]: stock exhausted — Pollo AI rescue (%d/%d)",
                            i + 1, scene_tipo, ai_used + 1, ai_max)
                asset = self._try_pollo_scene(query, scene_tipo, ctx)
                if asset:
                    ai_used += 1

            # ── Placeholder (absolutely nothing found) ────────
            if asset is None:
                logger.warning("Scene %d [%s]: ALL providers exhausted — placeholder", i, scene_tipo)
                asset = {
                    "path": None,
                    "type": "placeholder",
                    "duration": None,
                    "source": "placeholder",
                }

            # ── Count for stats ───────────────────────────────
            atype = asset.get("type", "?")
            if atype == "video":
                video_ok += 1
                _cb_consecutive_all_provider_failures = 0  # reset circuit breaker
            elif atype == "image":
                image_ok += 1
                _cb_consecutive_all_provider_failures = 0  # reset circuit breaker
            else:
                placeholder += 1
                _cb_consecutive_all_provider_failures += 1

            results[i] = asset

            # ── Circuit breaker: abort if ALL providers are exhausted ──
            if _cb_consecutive_all_provider_failures >= _cb_max_consecutive_failures:
                error_msg = (
                    f"CRITICAL: All media providers exhausted after {_cb_max_consecutive_failures} "
                    f"consecutive placeholder scenes. Pexels likely rate-limited (429), "
                    f"Unsplash likely down (403/429). Aborting media phase."
                )
                logger.error(error_msg)
                raise RuntimeError(error_msg)

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
            if getattr(provider, "name", "") == "youtube_cc":
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
                    logger.warning("youtube_cc liberal search failed: %s", exc)

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
        """
        if not query or not self.video_providers:
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

    # ── Internal: images ──────────────────────────────────────────

    def _try_image_unsplash(self, query: str, skip_urls: set[str] | None = None) -> dict | None:
        """Try Unsplash for an image, optionally skipping previously used URLs."""
        if not self._unsplash:
            return None
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

    def _try_image_pexels(self, query: str, skip_urls: set[str] | None = None) -> dict | None:
        """Try Pexels for an image, optionally skipping previously used URLs."""
        if not self._pexels:
            return None
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
        skip duplicates when *skip_urls* is provided."""
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

            if skip_urls:
                logger.warning("All %d %s results were duplicates — none fresh", n_request, source)
            return None
        except Exception as exc:
            logger.warning("%s image fetch failed: %s", source, exc)

        return None

    def _download_image(self, url: str, filename: str) -> Path | None:
        """Download image to IMAGES_DIR. Cached."""
        filepath = settings.IMAGES_DIR / filename
        if filepath.exists():
            logger.info("Image already cached: %s", filepath)
            return filepath

        try:
            resp = requests.get(url, timeout=30, stream=True)
            resp.raise_for_status()
            settings.IMAGES_DIR.mkdir(parents=True, exist_ok=True)
            filepath.write_bytes(resp.content)
            logger.info("Downloaded image: %s (%d bytes)", filepath, len(resp.content))
            return filepath
        except Exception as exc:
            logger.error("Image download failed %s: %s", url, exc)
            return None

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
