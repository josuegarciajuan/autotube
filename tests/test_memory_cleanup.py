"""Tests for memory cleanup: KokoroTTSEngine.unload().

Ensures that after job completion all heavy components are released
so that RAM returns to pre-pipeline levels (~1-2 GB instead of 5+ GB).

Run:  python3 -m pytest tests/test_memory_cleanup.py -v
"""

import sys
sys.path.insert(0, "/root/autotube")

import gc
import pytest
from unittest.mock import MagicMock, patch


# ── KokoroTTSEngine.unload() ──────────────────────────────────────────

class TestKokoroUnload:
    """Verify KokoroTTSEngine.unload() releases the KPipeline model."""

    def test_unload_releases_pipeline(self):
        """After unload(), _pipeline must be None and the pipeline property
        must re-create it on next access (lazy re-load)."""
        from pipeline.kokoro_tts import KokoroTTSEngine

        engine = KokoroTTSEngine()

        # The import inside the property is 'from kokoro import KPipeline',
        # so we patch at the import site.
        with patch("kokoro.KPipeline") as mock_kp:
            mock_instance = MagicMock()
            mock_kp.return_value = mock_instance

            # Access pipeline → loads mock
            _ = engine.pipeline
            assert engine._pipeline is mock_instance, "pipeline not cached"

            # Unload → drops reference
            engine.unload()
            assert engine._pipeline is None, "unload() did not set _pipeline to None"

    def test_unload_when_already_none_is_noop(self):
        """Calling unload() on an engine that never loaded should not crash."""
        from pipeline.kokoro_tts import KokoroTTSEngine
        engine = KokoroTTSEngine()
        # Should not raise
        engine.unload()
        assert engine._pipeline is None

    def test_unload_idempotent(self):
        """Calling unload() twice in a row should be safe."""
        from pipeline.kokoro_tts import KokoroTTSEngine
        engine = KokoroTTSEngine()
        with patch("kokoro.KPipeline") as mock_kp:
            mock_instance = MagicMock()
            mock_kp.return_value = mock_instance
            _ = engine.pipeline
            engine.unload()
            engine.unload()  # second call
            assert engine._pipeline is None

    def test_pipeline_reloads_after_unload(self):
        """After unload(), accessing pipeline again should trigger a fresh load."""
        from pipeline.kokoro_tts import KokoroTTSEngine
        engine = KokoroTTSEngine()
        with patch("kokoro.KPipeline") as mock_kp:
            first = MagicMock()
            second = MagicMock()
            mock_kp.side_effect = [first, second]

            p1 = engine.pipeline
            assert p1 is first
            engine.unload()
            assert engine._pipeline is None

            p2 = engine.pipeline
            assert p2 is second, "should have created a new pipeline instance"
            assert p2 is not first
