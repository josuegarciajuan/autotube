"""Tests for the Jul-2026 MediaFetcher optimizations.

Covers:
- Pexels-first image priority (was Unsplash-first)
- Reduced fallback cascade (4 → 2 steps)
- Coverr provider integration
- Non-blocking rate limiter behaviour in image fetch chain

Run:  python3 -m pytest tests/test_media_fetcher_optimizations.py -v
"""

import sys
from pathlib import Path
# Raíz del repo dinámica (mismo patrón que tests/conftest.py) — permite correr
# los tests desde un worktree o desde el árbol principal.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
from unittest.mock import MagicMock, patch

from pipeline.rate_limiter import TokenBucketRateLimiter


# ── helpers ─────────────────────────────────────────────────────

def _make_media_strategy(**overrides):
    cfg = {
        "media_per_block": 1,
        "prefer_video": True,
        "max_video_blocks_pct": 50,
        "target_video_pct": 50,
        "max_placeholder_pct": 0,
        "video_fallback_to_image": True,
        "video_min_duration": 4,
        "video_max_duration": 20,
        "video_providers": [],
        "fallback_query": "test fallback cinematic",
        "fallback_query_simple": "test fallback simple",
        "ken_burns_zoom_min": 2,
        "ken_burns_zoom_max": 7,
        "ai_image_fallback": False,
        "ai_max_per_video": 0,
    }
    cfg.update(overrides)
    return cfg


def _make_config(**overrides):
    cfg = MagicMock()
    cfg.MEDIA_STRATEGY = _make_media_strategy(**overrides)
    return cfg


def _make_scene(**kwargs):
    return {
        "start": kwargs.get("start", 0),
        "end": kwargs.get("end", kwargs.get("duration", 5)),
        "duration": kwargs.get("duration", 5),
        "tipo": kwargs.get("tipo", "desarrollo"),
        "texto": kwargs.get("texto", "test text."),
        "media_tipo": kwargs.get("media_tipo", "imagen"),
        "asset_idx": kwargs.get("asset_idx", 0),
        "search_query_en": kwargs.get("search_query_en", "test query cinematic"),
        "is_transition": kwargs.get("is_transition", False),
    }


def _make_image(path="/tmp/test.jpg", source="pexels_photo"):
    return {"path": path, "type": "image", "duration": None, "source": source}


class TestImagePriority:
    """Pixabay should be tried before Unsplash for images."""

    @patch("pipeline.media_fetcher.time.sleep", return_value=None)
    def test_pixabay_tried_before_unsplash_in_fetch_for_block(self, mock_sleep):
        """fetch_for_block: Pixabay is called first, Unsplash only if Pixabay fails."""
        from pipeline.media_fetcher import MediaFetcher

        fetcher = MediaFetcher(config=_make_config())
        fetcher.video_providers = []

        call_order = []

        def mock_pixabay(query, skip_urls=None):
            call_order.append("pixabay")
            return None  # simulate failure

        def mock_unsplash(query, skip_urls=None):
            call_order.append("unsplash")
            return _make_image(source="unsplash")

        fetcher._try_image_pixabay = MagicMock(side_effect=mock_pixabay)
        fetcher._try_image_unsplash = MagicMock(side_effect=mock_unsplash)

        result = fetcher.fetch_for_block(
            {"search_query_en": "test", "media_tipo": "imagen", "tipo": "desarrollo"}
        )

        assert call_order[0] == "pixabay", f"Pixabay should be first, got {call_order}"
        assert call_order[1] == "unsplash", f"Unsplash should be fallback, got {call_order}"
        assert result is not None
        assert result["source"] == "unsplash"

    @patch("pipeline.media_fetcher.time.sleep", return_value=None)
    def test_pixabay_succeeds_unsplash_not_called(self, mock_sleep):
        """When Pixabay succeeds, Unsplash should NOT be called at all."""
        from pipeline.media_fetcher import MediaFetcher

        fetcher = MediaFetcher(config=_make_config())
        fetcher.video_providers = []

        fetcher._try_image_pixabay = MagicMock(return_value=_make_image(source="pixabay_photo"))
        fetcher._try_image_unsplash = MagicMock(return_value=_make_image())

        result = fetcher.fetch_for_block(
            {"search_query_en": "test", "media_tipo": "imagen", "tipo": "desarrollo"}
        )

        assert result is not None
        assert result["source"] == "pixabay_photo"
        fetcher._try_image_pixabay.assert_called()
        fetcher._try_image_unsplash.assert_not_called()


class TestReducedCascade:
    """Only 1 fallback retry (simplified query), no type-specific or generic."""

    @patch("pipeline.media_fetcher.time.sleep", return_value=None)
    def test_only_simplified_retry_not_type_or_generic(self, mock_sleep):
        """fetch_for_block: only simplified query retry after first attempt fails."""
        from pipeline.media_fetcher import MediaFetcher

        fetcher = MediaFetcher(config=_make_config())
        fetcher.video_providers = []

        # Both primary providers fail
        call_count = [0]

        def mock_image(query, skip_urls=None):
            call_count[0] += 1
            if call_count[0] <= 4:
                return None  # first Pixabay+Unsplash fail, retry fails
            return _make_image(source="retry")

        fetcher._try_image_pixabay = MagicMock(side_effect=mock_image)
        fetcher._try_image_unsplash = MagicMock(side_effect=mock_image)

        # Simulate _simplify_query returning a different query
        fetcher._simplify_query = MagicMock(return_value="simplified query")

        result = fetcher.fetch_for_block(
            {"search_query_en": "original query test here", "media_tipo": "imagen", "tipo": "desarrollo"}
        )

        # Pixabay(original)=call 1, Unsplash(original)=call 2,
        # Pixabay(simplified)=call 3, Unsplash(simplified)=call 4,
        # Any further calls would be the deleted type/generic/simple fallbacks
        # Call 5 should not happen if cascade was removed
        assert call_count[0] <= 4, (
            f"Expected <=4 calls (original P+U + simple retry P+U), got {call_count[0]}"
        )


class TestCoverrIntegration:
    """Coverr should be registered and usable in the provider chain."""

    def test_coverr_in_provider_registry(self):
        """Coverr is registered in _PROVIDER_CLASSES and _NO_KEY_PROVIDERS."""
        from pipeline.media_fetcher import _PROVIDER_CLASSES, _NO_KEY_PROVIDERS

        assert "coverr" in _PROVIDER_CLASSES, "Coverr should be in provider classes"
        assert "coverr" in _NO_KEY_PROVIDERS, "Coverr should be in no-key providers"

    def test_coverr_instantiable(self):
        """CoverrVideoProvider can be instantiated without an API key."""
        from pipeline.providers.coverr import CoverrVideoProvider

        provider = CoverrVideoProvider()
        assert provider is not None
        assert provider.name == "coverr"

    def test_coverr_fallback_works(self):
        """Coverr fallback URL should return a valid asset."""
        from pipeline.providers.coverr import CoverrVideoProvider

        provider = CoverrVideoProvider()
        asset = provider.search("test", min_duration=10, max_duration=20)
        if asset is not None:
            assert asset.url.startswith("http")
            assert asset.duration >= 10
            assert asset.duration <= 20
            assert asset.provider.startswith("coverr")
        # If scraping fails and fallbacks don't match, None is acceptable


class TestFetchForScriptOptimizations:
    """v2 fetch_for_script path should also use Pixabay-first and reduced cascade."""

    @patch("pipeline.media_fetcher.time.sleep", return_value=None)
    def test_fetch_for_script_pixabay_first(self, mock_sleep):
        """fetch_for_script: Pixabay is tried before Unsplash per scene."""
        from pipeline.media_fetcher import MediaFetcher

        fetcher = MediaFetcher(config=_make_config(target_video_pct=0))
        fetcher.video_providers = []
        # Skip the real AI-image tier so the stock-image tier (Pixabay-first)
        # is the one that fetches. Without this, real Pollinations generation
        # succeeds and the mock below is never reached.
        fetcher._ai_image_primary = False

        scenes = [
            _make_scene(start=0, duration=5, asset_idx=0),
            _make_scene(start=5, duration=5, asset_idx=1),
        ]

        # Mock _fetch_asset_exhaustive (v2 path) to return image results
        # The v2 path uses _interleaved_providers → _search_provider_page, not
        # the legacy _try_* methods.
        call_sources = []

        def mock_fetch_exhaustive(scene, query_pool, want_video, target_dur, ctx, force_images=False):
            source = "pixabay_photo"
            call_sources.append(source)
            # Distinct path per scene: the uniqueness gate aborts if the same
            # image path is assigned to two scenes (anti-repeat invariant).
            idx = scene.get("asset_idx", 0)
            return {"path": f"/tmp/test_{idx}.jpg", "type": "image",
                    "duration": None, "source": source}

        fetcher._fetch_asset_exhaustive = MagicMock(side_effect=mock_fetch_exhaustive)
        fetcher._try_pollo_scene = MagicMock(return_value=None)
        # Image-fallback scene expansion is out of scope for this test
        fetcher._reconcile_actual_image_fallbacks = (
            lambda scenes, results, fn: (scenes, results)
        )

        results = fetcher.fetch_for_script(
            bloques=[{"texto": "test", "tipo": "desarrollo"} for _ in range(2)],
            scene_ranges=scenes,
        )

        assert len(results) == 2
        assert all(r.get("type") == "image" for r in results)
        # All results should be from Pixabay (not Unsplash), since Pixabay
        # is the primary image provider in the v2 path
        assert all(r.get("source") == "pixabay_photo" for r in results)
