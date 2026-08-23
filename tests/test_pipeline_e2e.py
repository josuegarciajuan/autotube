"""End-to-end test: full word target pipeline integration.

Run:  python3 -m pytest tests/test_pipeline_e2e.py -v
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
from unittest.mock import MagicMock, patch
from pipeline.script_generator import ScriptGenerator
from tests.conftest import MockDB, MockConfigCanal2, make_mock_openai_response, make_mock_llm_response
from config.voice_timing import words_for_duration, duration_for_words


class TestPipelineE2E:
    """Full integration: voice_timing → target → generate → enrich."""

    def test_full_word_target_pipeline(self):
        """words → target → generate_v2 (mocked) → enrich → verify."""
        from unittest.mock import MagicMock

        # Step 1: Calculate words via voice_timing
        target_words = words_for_duration(MockConfigCanal2, 14.0)
        assert target_words == 2772

        # Step 2: Create ScriptGenerator and word target
        sg = ScriptGenerator(MockDB(), MockConfigCanal2)
        sg._build_system_prompt = lambda cfg, **kw: "test prompt"
        sg.client = MagicMock()
        word_target = sg._compute_word_target(14.0)
        assert word_target["palabras_objetivo"] == target_words

        # Step 3: Mock the LLM for enrichment
        content_words = ["palabra " * 100 for _ in range(28)]
        enrichment_data = make_mock_llm_response(content_words)
        sg.client.chat.completions.create.return_value = \
            make_mock_openai_response(enrichment_data)

        # Step 4: Run enrichment directly
        enriched = sg._enrich_blocks(
            [{"texto": t} for t in content_words],
            {"id": 1, "title": "Test"},
            word_target,
        )

        # Step 5: Verify
        assert enriched is not None
        assert "palabras_reales" in enriched
        real_words = enriched["palabras_reales"]
        # Should be within 15% of target
        assert abs(real_words - target_words) / target_words < 0.15

        # Duration should match real words
        expected_dur = duration_for_words(MockConfigCanal2, real_words)
        assert enriched["duracion_estimada"] == expected_dur
