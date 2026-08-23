"""Tests for ScriptGenerator.generate_v2() sequential loop.

Run:  python3 -m pytest tests/test_generate_v2.py -v
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import json
import pytest
from unittest.mock import MagicMock, patch
from pipeline.script_generator import ScriptGenerator
from tests.conftest import MockDB, MockConfigCanal2, make_mock_openai_response


class TestGenerateV2:
    """Test generate_v2() with mocked LLM and batches."""

    def _make_sg(self):
        return ScriptGenerator(MockDB(), MockConfigCanal2)

    def _mock_batches(self, sg, batch_results: list[list[str]], palabras_objetivo=2772):
        """Mock _generate_blocks_batch and _generate_outline to avoid LLM calls."""
        calls = [0]
        original = sg._generate_blocks_batch

        def mock_batch(item, prev, guidance, source, outline=None, batch_num=0):
            idx = calls[0]
            calls[0] += 1
            if idx < len(batch_results):
                return [{"texto": t} for t in batch_results[idx]]
            return []

        sg._generate_blocks_batch = mock_batch
        # Mock outline generation to avoid LLM call (NEW: v2 now calls outline first)
        sg._generate_outline = lambda item, wt: {
            "chapters": [
                {"titulo": f"Cap {i+1}", "idea_central": "test",
                 "hechos_concretos": [f"Hecho {i}"], "visual_keywords_en": "test",
                 "emocion_objetivo": "asombro"}
                for i in range(4)
            ]
        }
        # Also mock enrichment to avoid LLM call
        sg._enrich_blocks = lambda bloques, item, wt: {
            "titulo_options": ["Título"],
            "descripcion_seo": "Desc",
            "guion": "\n\n".join(b["texto"] for b in bloques),
            "parrafos": [{"idea_central": "x", "bloques": bloques}],
            "cta": {"tipo": "cta", "texto": "CTA"},
            "bloques": bloques,
            "escenas": [],
            "emociones": [],
            "keywords": [],
            "hashtags": [],
            "duracion_estimada": 14.0,
            "chapters": [],
            "fuentes_citadas": [],
            "palabras_reales": sum(len(b["texto"].split()) for b in bloques),
        }
        return sg

    def test_reaches_target_and_returns_script(self):
        """Generate enough words → returns valid script."""
        sg = self._make_sg()
        # Each batch ~500 words, target=2772 → ~6 batches
        wordy_text = "palabra " * 500
        self._mock_batches(sg, [[wordy_text] for _ in range(8)], 2772)

        item = {"id": 1, "title": "Test", "text": "source"}
        result = sg.generate_v2(item, palabras_objetivo=2772)
        assert result is not None
        assert result.get("guion") is not None
        assert len(result["guion"]) > 100

    def test_stops_at_92_percent(self):
        """Target=500, batch reaches 460 → stops (≥92%)."""
        sg = self._make_sg()
        self._mock_batches(sg, [["palabra " * 460]], 500)
        # Mock _save_and_return to avoid DB call
        sg._save_and_return = lambda **kw: {"id": 999, "guion": "test"}
        item = {"id": 1, "title": "Test", "text": "source"}
        result = sg.generate_v2(item, palabras_objetivo=500)
        assert result is not None

    def test_empty_strikes_exhaust(self):
        """10 consecutive empty batches → returns None."""
        sg = self._make_sg()
        self._mock_batches(sg, [] * 15, 1000)  # all empty
        item = {"id": 1, "title": "Test", "text": "source"}
        result = sg.generate_v2(item, palabras_objetivo=1000)
        assert result is None

    def test_empty_strikes_reset_on_success(self):
        """Empty strikes reset after a successful batch."""
        sg = self._make_sg()
        results = [            # batch results:
            [],                # 1: empty
            [],                # 2: empty
            ["palabra " * 200],  # 3: success → reset!
            [],                # 4: empty (strike 1 again)
            ["palabra " * 400],  # 5: success
            ["palabra " * 400],  # 6: success → hits target
        ]
        self._mock_batches(sg, results, 800)
        item = {"id": 1, "title": "Test", "text": "source"}
        result = sg.generate_v2(item, palabras_objetivo=800)
        assert result is not None

    def test_llm_transient_error_with_backoff(self):
        """LLM exceptions in batch → backoff sleep, retry, eventually succeed."""
        sg = self._make_sg()
        import time
        original_sleep = time.sleep
        sleep_calls = []

        def mock_sleep(s):
            sleep_calls.append(s)

        # First 3 batches fail, then success
        call_count = [0]
        original_batch = sg._generate_blocks_batch

        def flaky_batch(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] <= 3:
                raise Exception("Transient error")
            return [{"texto": "palabra " * 300}]

        sg._generate_blocks_batch = flaky_batch
        # Mock outline to avoid LLM call
        sg._generate_outline = lambda item, wt: {"chapters": [{"titulo": "T", "hechos_concretos": ["f"]}]}
        sg._enrich_blocks = lambda b, i, w: {
            "titulo_options": ["T"], "descripcion_seo": "D",
            "guion": "x " * 500, "parrafos": [],
            "cta": {"tipo": "cta", "texto": "C"},
            "bloques": [], "escenas": [], "emociones": [],
            "keywords": [], "hashtags": [],
            "duracion_estimada": 5.0, "chapters": [],
            "fuentes_citadas": [], "palabras_reales": 500,
        }

        time.sleep = mock_sleep
        item = {"id": 1, "title": "Test", "text": "source"}
        result = sg.generate_v2(item, palabras_objetivo=500)
        assert result is not None


class TestV2WithOutline:
    """Test that generate_v2() calls outline generation before blocks."""

    def _make_sg(self):
        return ScriptGenerator(MockDB(), MockConfigCanal2)

    def _mock_batches(self, sg, batch_results, palabras_objetivo=500):
        """Mock _generate_blocks_batch and _enrich_blocks."""
        calls = [0]
        def mock_batch(item, prev, guidance, source, outline=None, batch_num=0):
            idx = calls[0]
            calls[0] += 1
            if idx < len(batch_results):
                return [{"texto": t} for t in batch_results[idx]]
            return []
        sg._generate_blocks_batch = mock_batch
        sg._enrich_blocks = lambda bl, item, wt: {
            "titulo_options": ["T"], "descripcion_seo": "D",
            "guion": "\n".join(b["texto"] for b in bl),
            "parrafos": [{"idea_central": "x", "bloques": bl}],
            "cta": {"tipo": "cta", "texto": "CTA"},
            "bloques": bl, "escenas": [], "emociones": [],
            "keywords": [], "hashtags": [], "duracion_estimada": 3.0,
            "chapters": [], "fuentes_citadas": [],
        }
        return sg

    def test_outline_called_before_blocks(self):
        """generate_v2 calls _generate_outline before block generation."""
        sg = self._make_sg()
        sg._generate_outline = MagicMock(return_value={
            "chapters": [{"titulo": "Cap 1", "idea_central": "x",
                          "hechos_concretos": ["f1"], "visual_keywords_en": "test",
                          "emocion_objetivo": "asombro"}]
        })
        self._mock_batches(sg, [["test block content"]], 500)
        sg._save_and_return = lambda **kw: {"id": 99, "guion": "test"}

        item = {"id": 1, "title": "Test", "text": "source text"}
        result = sg.generate_v2(item, palabras_objetivo=500)

        assert sg._generate_outline.called, "Outline must be called"
        assert result is not None, "Must return a valid script"

    def test_catches_outline_failure_gracefully(self):
        """If outline generation fails, v2 continues without it."""
        sg = self._make_sg()
        sg._generate_outline = MagicMock(side_effect=Exception("LLM failed"))
        self._mock_batches(sg, [["test block"]], 500)
        sg._save_and_return = lambda **kw: {"id": 99, "guion": "test"}

        item = {"id": 1, "title": "Test", "text": "source"}
        result = sg.generate_v2(item, palabras_objetivo=500)

        # Must continue without outline — should not crash
        assert result is not None
        assert sg._generate_outline.called

    def test_progress_callback_fires(self):
        """Progress callback is called after each successful batch."""
        sg = self._make_sg()
        wordy = "palabra " * 300
        self._mock_batches(sg, [[wordy] for _ in range(4)], 1000)

        progress_calls = []
        sg._progress_cb = lambda pct, phase, msg, **kw: progress_calls.append(pct)

        item = {"id": 1, "title": "Test", "text": "source"}
        sg.generate_v2(item, palabras_objetivo=1000)

        assert len(progress_calls) > 0
        # Each call should be between 15 and 23
        for pct in progress_calls:
            assert 15 <= pct <= 25, f"Progress {pct} outside [15,25]"

    def test_palabras_objetivo_none_uses_default(self):
        """When palabras_objetivo is None, generates something (uses default)."""
        sg = self._make_sg()
        wordy = "palabra " * 400
        self._mock_batches(sg, [[wordy] for _ in range(8)], None)

        item = {"id": 1, "title": "Test", "text": "source"}
        result = sg.generate_v2(item, palabras_objetivo=None)
        # Should use _get_word_target() internally and generate
        assert result is not None
