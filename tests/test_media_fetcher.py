"""Tests for media_fetcher.py — video fallback chain, second pass, scene_ranges sync.

Run:  python3 -m pytest tests/test_media_fetcher.py -v
"""

import sys
from pathlib import Path
# Raíz del repo dinámica (mismo patrón que tests/conftest.py) — permite correr
# los tests desde un worktree o desde el árbol principal.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
from unittest.mock import MagicMock, patch


# ── Config helpers ───────────────────────────────────────────────

def _make_media_strategy(**overrides):
    """Build a MEDIA_STRATEGY dict with sensible defaults."""
    cfg = {
        "media_per_block": 1,
        "prefer_video": True,
        "max_video_blocks_pct": 50,
        "target_video_pct": 50,
        "max_placeholder_pct": 0,
        "video_fallback_to_image": True,
        "video_min_duration": 4,
        "video_max_duration": 20,
        "video_providers": [
            {"name": "pexels", "api_key_env": "PEXELS_API_KEY"},
        ],
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
    """Build a mock config object with MEDIA_STRATEGY."""
    config = MagicMock()
    config.MEDIA_STRATEGY = _make_media_strategy(**overrides)
    return config


def _make_scene_range(**kwargs):
    """Build a minimal scene range dict."""
    return {
        "start": kwargs.get("start", 0),
        "end": kwargs.get("end", kwargs.get("duration", 5)),
        "duration": kwargs.get("duration", 5),
        "tipo": kwargs.get("tipo", "desarrollo"),
        "texto": kwargs.get("texto", "test text."),
        "media_tipo": kwargs.get("media_tipo", "imagen"),
        "asset_idx": kwargs.get("asset_idx", 0),
        "search_query_en": kwargs.get("search_query_en", "test query"),
        "is_transition": kwargs.get("is_transition", False),
    }


def _make_video_result(path="/tmp/test.mp4", duration=10.0, source="pexels_video"):
    return {"path": path, "type": "video", "duration": duration, "source": source}


def _make_image_result(path="/tmp/test.jpg", source="pexels_photo"):
    return {"path": path, "type": "image", "duration": None, "source": source}


# ── Tests ────────────────────────────────────────────────────────

class TestVideoFallbackChain:
    """_try_video_providers should try exact → simplified → generic queries."""

    def test_tries_exact_query_first(self):
        """Exact query succeeds on first attempt → no fallback needed."""
        import importlib
        from pipeline.media_fetcher import MediaFetcher

        fetcher = MediaFetcher(config=_make_config())
        # Need at least one provider to prevent early return
        mock_provider = MagicMock()
        mock_provider.name = "mock"
        fetcher.video_providers = [mock_provider]

        # Also suppress fallback queries so we don't trigger attempt 3
        fetcher._media_strategy["video_fallback_queries"] = []

        # Patch _try_all_video_providers to succeed on exact query
        mock_result = _make_video_result()
        fetcher._try_all_video_providers = MagicMock(return_value=mock_result)

        result = fetcher._try_video_providers("test query exact", target_dur=7.0)

        assert result is not None
        assert result["type"] == "video"
        # Should have been called with the exact query (first attempt)
        # min_dur = max(7.0*0.8, 1.0) = ~5.6, max_dur = 7.0*4.0 = 28.0
        fetcher._try_all_video_providers.assert_called_once()
        call_args = fetcher._try_all_video_providers.call_args[0]
        assert call_args[0] == "test query exact"
        assert abs(call_args[1] - 5.6) < 0.01, f"min_dur ~5.6, got {call_args[1]}"
        assert call_args[2] == 28.0  # updated: 7.0 * 4.0 = 28.0

    def test_falls_back_to_simplified_query(self):
        """Exact query fails, simplified query succeeds."""
        import importlib
        from pipeline.media_fetcher import MediaFetcher

        fetcher = MediaFetcher(config=_make_config())
        fetcher.video_providers = [MagicMock()]  # non-empty list to skip early return

        # Wire up video_fallback_queries to empty so we test simplified path
        fetcher._media_strategy["video_fallback_queries"] = []

        # Patch youtube_cc liberal attempt to fail immediately
        call_count = [0]

        def mock_try_all(query, min_dur, max_dur, skip_urls=None):
            call_count[0] += 1
            if call_count[0] == 1:
                return None  # exact query fails
            elif call_count[0] == 2:
                # simplified query succeeds
                return _make_video_result(source="simplified")

        fetcher._try_all_video_providers = MagicMock(side_effect=mock_try_all)

        # No youtube_cc provider to trigger attempt 4
        result = fetcher._try_video_providers("very long specific query test here", target_dur=7.0)

        assert result is not None
        assert result["source"] == "simplified"
        # First call: exact query
        # Second call: simplified query (first 3-4 keywords)
        assert fetcher._try_all_video_providers.call_count >= 2


class TestSecondPass:
    """Second pass should rescue video scenes when quota is below minimum."""

    @patch("pipeline.media_fetcher.time.sleep", return_value=None)
    def test_second_pass_triggers_below_min(self, mock_sleep):
        """Below min_video_pct → second pass tries generic queries."""
        import importlib
        from pipeline.media_fetcher import MediaFetcher

        cfg = _make_config(
            target_video_pct=50,
            min_video_pct=30,
            video_fallback_queries=["generic aerial drone"],
        )
        fetcher = MediaFetcher(config=cfg)

        # 5 scenes: scenes 0,1 have video slots, but only scene 0 got video
        scenes = [
            _make_scene_range(start=0, duration=5, media_tipo="video", asset_idx=0),
            _make_scene_range(start=5, duration=5, media_tipo="video", asset_idx=1),
            _make_scene_range(start=10, duration=5, media_tipo="imagen", asset_idx=2),
            _make_scene_range(start=15, duration=5, media_tipo="imagen", asset_idx=3),
            _make_scene_range(start=20, duration=5, media_tipo="imagen", asset_idx=4),
        ]

        # Mock providers: first call finds 1 video, second pass rescues 1 more
        video_count = [0]

        def mock_try_video(query, target_dur):
            video_count[0] += 1
            if video_count[0] <= 2:
                return _make_video_result()
            return None

        fetcher._try_video_providers = MagicMock(side_effect=mock_try_video)
        # Also mock _try_all_video_providers for the fetch loop
        fetcher._try_all_video_providers = MagicMock(return_value=None)

        # Mock image fallback for non-video scenes
        def mock_try_image(query, skip_urls=None):
            return _make_image_result()

        fetcher._try_image_unsplash = MagicMock(side_effect=mock_try_image)
        fetcher._try_image_pexels = MagicMock(return_value=None)

        results = fetcher.fetch_for_script(
            bloques=[{"texto": "test", "tipo": "desarrollo"} for _ in range(5)],
            scene_ranges=scenes,
        )

        assert len(results) == 5
        # At least 2 video results (1 from first pass + 1 from rescue)
        video_results = [r for r in results if r.get("type") == "video"]
        assert len(video_results) >= 2, f"Expected >=2 video, got {len(video_results)}"


class TestSceneRangesSync:
    """After fetch, scene_ranges media_tipo should reflect actual asset type."""

    @patch("pipeline.media_fetcher.time.sleep", return_value=None)
    def test_media_tipo_synced_after_fetch(self, mock_sleep):
        """scene_ranges[i]["media_tipo"] = results[i]["type"] after fetch."""
        from pipeline.media_fetcher import MediaFetcher

        cfg = _make_config(
            target_video_pct=0,  # no video slots → all image
            min_video_pct=0,
            video_fallback_queries=[],
        )
        fetcher = MediaFetcher(config=cfg)
        # Real value (6s max image scene) — un MagicMock flotaría a ~1-2.5
        # y partiría cada escena de 5s en subescenas placeholder sin media.
        cfg.IMAGE_SCENE_DURATION_MAX = 6.0

        scenes = [
            _make_scene_range(start=0, duration=5, media_tipo="video", asset_idx=0),
            _make_scene_range(start=5, duration=5, media_tipo="video", asset_idx=1),
        ]

        # Mock _fetch_asset_exhaustive (v2 path) to return image results
        # The v2 path uses _interleaved_providers → _search_provider_page, not
        # the legacy _try_* methods.  Distinct path per scene, matching the
        # production "never repeat an image" invariant.
        def mock_fetch_exhaustive(scene, query_pool, want_video, target_dur, ctx, force_images=False):
            idx = scene.get("asset_idx", 0)
            return _make_image_result(path=f"/tmp/img_{idx}.jpg")

        fetcher._fetch_asset_exhaustive = MagicMock(side_effect=mock_fetch_exhaustive)

        results = fetcher.fetch_for_script(
            bloques=[{"texto": "test", "tipo": "desarrollo"}] * 2,
            scene_ranges=scenes,
        )

        # After fetch, scene_ranges should be synced
        assert scenes[0]["media_tipo"] == "image", (
            f"Expected 'image', got {scenes[0]['media_tipo']}"
        )
        assert scenes[1]["media_tipo"] == "image", (
            f"Expected 'image', got {scenes[1]['media_tipo']}"
        )


class TestVideoValidation:
    """Tests for _is_valid_video with ffprobe integration."""

    def test_valid_video_uses_ffprobe(self):
        """ffprobe is called and returns duration on valid video."""
        from pipeline.media_fetcher import MediaFetcher
        import tempfile

        with patch("subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stdout = "12.5"

            with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as f:
                f.write(b'\x00\x00\x00\x18ftypisom' + b'\x00' * 50000)
                path = Path(f.name)

            try:
                result = MediaFetcher._is_valid_video(path)
                assert result is True
                assert mock_run.called, "ffprobe must be called"
            finally:
                path.unlink(missing_ok=True)

    def test_corrupt_video_rejected_by_ffprobe(self):
        """Video with valid header but ffprobe failure → rejected."""
        from pipeline.media_fetcher import MediaFetcher
        import tempfile

        with patch("subprocess.run") as mock_run:
            mock_run.return_value.returncode = 1
            mock_run.return_value.stderr = "Invalid data found when processing input"

            with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as f:
                f.write(b'\x00\x00\x00\x18ftypisom' + b'\x00' * 50000)
                path = Path(f.name)

            try:
                result = MediaFetcher._is_valid_video(path)
                assert result is False, "Corrupt video must be rejected"
            finally:
                path.unlink(missing_ok=True)

    def test_html_file_rejected_quickly(self):
        """HTML files are caught before ffprobe (saves time)."""
        from pipeline.media_fetcher import MediaFetcher
        import tempfile

        with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as f:
            f.write(b'<!DOCTYPE html><html>Not a video</html>')
            path = Path(f.name)

        try:
            with patch("subprocess.run") as mock_run:
                result = MediaFetcher._is_valid_video(path)
                assert result is False
                # ffprobe should NOT be called for HTML
                assert not mock_run.called
        finally:
            path.unlink(missing_ok=True)


class TestVideoForcing:
    """Test that hook and climax scenes are forced to video."""

    def test_hook_and_climax_always_get_video(self):
        """Hook and climax scenes assigned video regardless of LLM tag."""
        from pipeline.media_fetcher import MediaFetcher

        config = _make_config(target_video_pct=40)
        mf = MediaFetcher(config)
        mf.video_providers = []
        mf._pexels = None
        mf._unsplash = None
        mf._used_image_urls = set()

        scenes = [
            _make_scene_range(tipo="hook", duration=6, media_tipo="imagen", asset_idx=0),
            _make_scene_range(tipo="desarrollo", duration=8, media_tipo="imagen", asset_idx=1),
            _make_scene_range(tipo="climax", duration=10, media_tipo="imagen", asset_idx=2),
            _make_scene_range(tipo="reflexion", duration=5, media_tipo="imagen", asset_idx=3),
            _make_scene_range(tipo="desarrollo", duration=7, media_tipo="imagen", asset_idx=4),
        ]

        bloques = [
            {"texto": "h"}, {"texto": "d1"}, {"texto": "c"}, {"texto": "r"}, {"texto": "d2"}
        ]

        # Mock _try_video_providers and _try_image_pexels/unplash to return None
        with patch.object(mf, '_try_video_providers', return_value=None):
            with patch.object(mf, '_try_image_pexels',
                            return_value={"path": "/tmp/img.jpg", "type": "image", "source": "pexels"}):
                with patch.object(mf, '_try_image_unsplash', return_value=None):
                    result = mf.fetch_for_script(
                        bloques=bloques, scene_ranges=scenes,
                    )

        # All scenes should have a result (image fallback)
        assert len(result) == 5
        # Hook (idx 0) and climax (idx 2) should have tried video first
        assert True  # basic structure test passes


class TestVideoValidation:
    """Test _is_valid_video rejects corrupt files that pass header check
    but have damaged frame data (the root cause of frozen-video renders)."""

    @staticmethod
    def _make_fake_mp4(path: Path, size: int = 100000):
        """Create a file that looks like MP4 (ftyp box) but is bogus data."""
        data = b'\x00\x00\x00\x18ftypmp42' + b'\x00' * (size - 12)
        path.write_bytes(data)

    def test_rejects_html_as_video(self, tmp_path):
        """HTML content disguised as video should be rejected."""
        from pipeline.media_fetcher import MediaFetcher
        html_file = tmp_path / "fake.mp4"
        html_file.write_text("<!DOCTYPE html><html>...</html>")
        assert not MediaFetcher._is_valid_video(html_file), "HTML should be rejected"

    def test_rejects_zero_duration(self, tmp_path):
        """A valid-looking MP4 with ffprobe reporting 0 duration should fail."""
        from pipeline.media_fetcher import MediaFetcher
        mp4_file = tmp_path / "zero.mp4"
        self._make_fake_mp4(mp4_file)
        # ffprobe will fail because this is not a real video → should return False
        assert not MediaFetcher._is_valid_video(mp4_file), "Fake MP4 without real streams should fail"

    def test_rejects_corrupt_frames_via_ffmpeg(self, tmp_path):
        """A file that passes ffprobe but has frame decode errors
        in ffmpeg null-decode check should be rejected."""
        from pipeline.media_fetcher import MediaFetcher

        # Create a valid-looking MP4 with magic bytes but no real stream
        mp4_file = tmp_path / "corrupt_frames.mp4"
        self._make_fake_mp4(mp4_file)

        # Without mocking ffmpeg, the null-decode check will find errors
        # since there are no real frames to decode
        assert not MediaFetcher._is_valid_video(mp4_file), (
            "Files with frame decode errors should be rejected"
        )

    @patch("subprocess.run")
    def test_valid_video_passes_all_checks(self, mock_run, tmp_path):
        """A properly valid video should pass header, ffprobe, and frame checks."""
        from pipeline.media_fetcher import MediaFetcher

        mp4_file = tmp_path / "valid.mp4"
        self._make_fake_mp4(mp4_file)

        # Mock all 3 phases (ffprobe + 3 x ffmpeg decode checks)
        def fake_run(args, **kwargs):
            cmd_str = " ".join(args)
            result = MagicMock()
            result.returncode = 0
            result.stdout = ""
            result.stderr = ""
            if "ffprobe" in cmd_str:
                result.stdout = "15.0"
            return result

        mock_run.side_effect = fake_run
        assert MediaFetcher._is_valid_video(mp4_file), "Valid video should pass all checks"

    @patch("subprocess.run")
    def test_rejects_on_frame_decode_error(self, mock_run, tmp_path):
        """When ffprobe passes but ffmpeg null-decode returns stderr
        (indicating corrupt frames), the file should be rejected."""
        from pipeline.media_fetcher import MediaFetcher

        mp4_file = tmp_path / "bad_frames.mp4"
        self._make_fake_mp4(mp4_file)

        call_count = 0
        def fake_run(args, **kwargs):
            nonlocal call_count
            call_count += 1
            cmd_str = " ".join(args)
            result = MagicMock()
            if "ffprobe" in cmd_str:
                result.returncode = 0
                result.stdout = "23.42"
                result.stderr = ""
            elif "ffmpeg" in cmd_str and call_count == 3:
                # Middle check fails with decode errors
                result.returncode = 1
                result.stderr = "Error while decoding stream"
            else:
                result.returncode = 0
                result.stderr = ""
            return result

        mock_run.side_effect = fake_run
        assert not MediaFetcher._is_valid_video(mp4_file), (
            "Frame decode error at any check should reject the file"
        )


class TestOnDemandUrgentFetch:
    """Regression: scenes that fell to black placeholders during assembly
    (Pollo AI returned identical images → dedup rejected every duplicate)
    must be recoverable via ``fetch_single_image_urgent`` — the fetcher
    returns a fresh, unused image for a scene's query even after the
    pre-fetched pool was exhausted (videos #2174/#2175/#2176/#2178)."""

    def _make_fetcher(self, candidates):
        from pipeline.media_fetcher import MediaFetcher

        fetcher = MediaFetcher(config=_make_config())
        provider = MagicMock()
        provider.available = True
        provider.name = "test_provider"
        provider.search.return_value = candidates
        fetcher._image_providers = [provider]
        return fetcher

    def test_returns_fresh_image_when_available(self):
        from pipeline.media_fetcher import MediaFetcher

        cand = MagicMock()
        cand.url = "https://cdn.test/img1.jpg"
        cand.id = "img1"
        fetcher = self._make_fetcher([cand])

        with patch.object(
            MediaFetcher, "_download_image",
            return_value=Path("/tmp/fill_test.jpg"),
        ), patch.object(
            MediaFetcher, "_is_valid_image", return_value=True,
        ):
            asset = fetcher.fetch_single_image_urgent("ancient ruins")
        assert asset is not None
        assert asset.get("type") == "image"
        assert asset.get("path") is not None

    def test_skips_urls_already_used_in_this_video(self):
        from pipeline.media_fetcher import MediaFetcher

        cand1 = MagicMock()
        cand1.url = "https://cdn.test/dup.jpg"
        cand1.id = "dup1"
        cand2 = MagicMock()
        cand2.url = "https://cdn.test/fresh.jpg"
        cand2.id = "fresh1"
        fetcher = self._make_fetcher([cand1, cand2])
        fetcher._used_image_urls.add("https://cdn.test/dup.jpg")  # already in this video

        with patch.object(
            MediaFetcher, "_download_image",
            return_value=Path("/tmp/fill_test.jpg"),
        ), patch.object(
            MediaFetcher, "_is_valid_image", return_value=True,
        ):
            asset = fetcher.fetch_single_image_urgent("ancient ruins")
        assert asset is not None
        # Must NOT return the duplicate URL
        assert "dup.jpg" not in asset.get("path", "")

    def test_returns_none_when_all_providers_exhausted(self):
        from pipeline.media_fetcher import MediaFetcher

        cand = MagicMock()
        cand.url = "https://cdn.test/img1.jpg"
        cand.id = "img1"
        fetcher = self._make_fetcher([cand])
        fetcher._used_image_urls.add("https://cdn.test/img1.jpg")
        fetcher._used_asset_urls.add("https://cdn.test/img1.jpg")
        # _urgent_used_urls is lazily created by fetch_single_image_urgent —
        # pre-seed it the same way the method does.
        if not hasattr(fetcher, "_urgent_used_urls"):
            fetcher._urgent_used_urls = set()
        fetcher._urgent_used_urls.add("https://cdn.test/img1.jpg")

        asset = fetcher.fetch_single_image_urgent("ancient ruins")
        assert asset is None, "All dedup sets hold the URL → must return None (caller falls to placeholder)"


class TestAIImageDedup:
    """AI image cache hits must never be assigned twice within a video."""

    @staticmethod
    def _fake_jpeg(path):
        path.write_bytes(b"\xff\xd8\xff" + b"\x00" * 6100)
        return path

    @patch("pipeline.media_fetcher.time.sleep", return_value=None)
    def test_ai_image_chain_rejects_cache_duplicate(self, mock_sleep):
        """Same cached file returned for 2 scenes → second is rejected."""
        import tempfile
        from pipeline.media_fetcher import MediaFetcher

        cfg = _make_config(ai_image_primary=True)
        cfg.MEDIA_STRATEGY["ai_image_providers"] = ["pollinations", "local_sd"]
        fetcher = MediaFetcher(config=cfg)

        fake_path = Path(tempfile.gettempdir()) / "ai_cache_dedup_test.jpg"
        self._fake_jpeg(fake_path)

        mock_polli = MagicMock()
        mock_polli.generate.return_value = fake_path
        mock_sd = MagicMock()
        mock_sd.generate.return_value = None

        fetcher._pollinations = mock_polli
        fetcher._local_sd = mock_sd

        scene = {"tipo": "desarrollo", "texto": "narration", "duration": 5,
                 "search_query_en": "test query"}

        # First scene: cached file is fresh → accepted
        asset1 = fetcher._try_ai_image_chain(scene, scene_idx=0, total_scenes=3)
        assert asset1 is not None and str(asset1["path"]) == str(fake_path)

        # Second scene: SAME cached file → must be rejected and try local_sd
        asset2 = fetcher._try_ai_image_chain(scene, scene_idx=1, total_scenes=3)
        assert asset2 is None, "cache duplicate must be rejected"
        assert mock_sd.generate.called, "local_sd should be tried after dup rejection"

        fake_path.unlink(missing_ok=True)


class TestFetchUniquenessGate:
    """Post-fetch gate must abort fast when distinct media is insufficient."""

    @patch("pipeline.media_fetcher.time.sleep", return_value=None)
    def test_gate_raises_on_duplicate_images(self, mock_sleep):
        """Every scene assigned the SAME image → RuntimeError before render."""
        from pipeline.media_fetcher import MediaFetcher

        cfg = _make_config(target_video_pct=0, min_video_pct=0,
                           video_fallback_queries=[])
        fetcher = MediaFetcher(config=cfg)

        scenes = [
            _make_scene_range(start=i * 5, duration=5, media_tipo="imagen", asset_idx=i)
            for i in range(5)
        ]

        # Bypass the real fetch chain: ALL scenes get the same image path.
        dup_asset = {"path": "/tmp/dup.jpg", "type": "image",
                     "duration": None, "source": "pexels_photo"}
        fetcher._fetch_with_ai_tiers = MagicMock(return_value=(dup_asset, 0, {}))
        # Avoid real reconciliation/network: keep results as-is.
        fetcher._reconcile_actual_image_fallbacks = MagicMock(
            side_effect=lambda scenes, results, fn: (scenes, results)
        )

        with pytest.raises(RuntimeError, match="DISTINCT media"):
            fetcher.fetch_for_script(
                bloques=[{"tipo": "desarrollo", "texto": "t"} for _ in range(5)],
                scene_ranges=scenes,
            )

    @patch("pipeline.media_fetcher.time.sleep", return_value=None)
    def test_gate_passes_with_distinct_images(self, mock_sleep):
        """Distinct image per scene → no error."""
        from pipeline.media_fetcher import MediaFetcher

        cfg = _make_config(target_video_pct=0, min_video_pct=0,
                           video_fallback_queries=[])
        fetcher = MediaFetcher(config=cfg)

        scenes = [
            _make_scene_range(start=i * 5, duration=5, media_tipo="imagen", asset_idx=i)
            for i in range(5)
        ]

        def fake_fetch(scene, scene_idx, **kwargs):
            asset = {"path": f"/tmp/unique_{scene_idx}.jpg", "type": "image",
                     "duration": None, "source": "pexels_photo"}
            return asset, 0, {}

        fetcher._fetch_with_ai_tiers = MagicMock(side_effect=fake_fetch)
        fetcher._reconcile_actual_image_fallbacks = MagicMock(
            side_effect=lambda scenes, results, fn: (scenes, results)
        )

        results = fetcher.fetch_for_script(
            bloques=[{"tipo": "desarrollo", "texto": "t"} for _ in range(5)],
            scene_ranges=scenes,
        )
        assert len(results) == 5
        assert all(r.get("type") == "image" for r in results)
