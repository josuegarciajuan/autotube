"""Tests for _enrich_blocks() — duration recalculation, field correctness.

Run:  python3 -m pytest tests/test_enrich_blocks.py -v
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import json
import pytest
from unittest.mock import MagicMock, patch
from pipeline.script_generator import ScriptGenerator
from tests.conftest import MockDB, MockConfigCanal2, make_mock_openai_response, make_mock_llm_response


class TestEnrichBlocks:
    """Test _enrich_blocks() always recalculates duration."""

    def _make_sg(self):
        sg = ScriptGenerator(MockDB(), MockConfigCanal2)
        sg._build_system_prompt = lambda cfg, **kw: "test system prompt"
        sg._format_user_prompt = lambda item: "test user prompt"
        sg.client = MagicMock()
        return sg

    def test_palabras_reales_present(self):
        """Enriched data includes palabras_reales field."""
        sg = self._make_sg()
        sg.client.chat.completions.create.return_value = \
            make_mock_openai_response(make_mock_llm_response([
                "Párrafo uno con palabras.", "Párrafo dos más contenido.", "Tercer párrafo."]))

        bloques = [{"texto": "Texto uno de prueba."}, {"texto": "Texto dos."}]
        word_target = {"words_min": 100, "words_max": 500, "duration_target": 3.0,
                        "blocks_min": 3, "blocks_max": 10}
        enriched = sg._enrich_blocks(bloques, {"id": 1, "title": "T"}, word_target)

        assert enriched is not None
        assert "palabras_reales" in enriched
        assert enriched["palabras_reales"] > 0

    def test_duracion_not_from_llm(self):
        """LLM hallucinated duracion_estimada=99.9 → overridden."""
        sg = self._make_sg()
        data = make_mock_llm_response(["Párrafo único de test."])
        data["duracion_estimada"] = 99.9
        sg.client.chat.completions.create.return_value = \
            make_mock_openai_response(data)

        bloques = [{"texto": "Párrafo único de test."}]
        enriched = sg._enrich_blocks(bloques, {"id": 1, "title": "T"},
            {"words_min": 10, "words_max": 100, "duration_target": 2.0, "blocks_min": 2, "blocks_max": 5})

        assert enriched["duracion_estimada"] != 99.9
        assert enriched["palabras_reales"] == 4

    def test_guion_reverts_if_too_short(self):
        """If enrichment truncates guion > 30%, restore original."""
        sg = self._make_sg()
        original_text = "texto " * 50
        data = make_mock_llm_response(["solo tres palabras"])
        sg.client.chat.completions.create.return_value = \
            make_mock_openai_response(data)

        bloques = [{"texto": original_text}]
        enriched = sg._enrich_blocks(bloques, {"id": 1, "title": "T"},
            {"words_min": 10, "words_max": 100, "duration_target": 2.0, "blocks_min": 2, "blocks_max": 5})

        assert enriched is not None
        assert len(enriched["guion"].split()) >= 35

    def test_returns_none_for_empty_bloques(self):
        """Empty bloques list → None."""
        sg = self._make_sg()
        result = sg._enrich_blocks([], {"id": 1, "title": "T"},
            {"words_min": 10, "words_max": 100, "duration_target": 2.0, "blocks_min": 2, "blocks_max": 5})
        assert result is None

    def test_fallback_on_llm_failure(self):
        """When enrichment LLM fails, returns minimal valid structure."""
        sg = self._make_sg()
        sg.client.chat.completions.create.side_effect = Exception("LLM down")

        bloques = [{"texto": "Párrafo de prueba con cinco palabras."}]
        enriched = sg._enrich_blocks(bloques, {"id": 1, "title": "T"},
            {"words_min": 10, "words_max": 100, "duration_target": 2.0, "blocks_min": 2, "blocks_max": 5})

        assert enriched is not None
        assert "guion" in enriched
        assert "duracion_estimada" in enriched
