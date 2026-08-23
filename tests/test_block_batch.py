"""Tests for _generate_blocks_batch() — LLM call, normalization, errors.

Run:  python3 -m pytest tests/test_block_batch.py -v
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import json
import pytest
from unittest.mock import MagicMock, patch
from pipeline.script_generator import ScriptGenerator
from tests.conftest import MockDB, MockConfigCanal2, make_mock_openai_response


class TestBatchGeneration:
    """Test _generate_blocks_batch() with mocked LLM."""

    def _make_sg(self):
        sg = ScriptGenerator(MockDB(), MockConfigCanal2)
        sg._build_minimal_prompt = lambda *a, **kw: "test prompt"
        sg.client = MagicMock()
        return sg

    def test_returns_normalized_bloques(self):
        """LLM returns valid JSON with blocks → normalized list."""
        sg = self._make_sg()
        sg.client.chat.completions.create.return_value = \
            make_mock_openai_response({
                "bloques": [{"texto": "Hola"}, {"texto": "Mundo"}]
            })

        result = sg._generate_blocks_batch(
            {"id": 1, "title": "Test"}, None, 250, "source"
        )
        assert len(result) == 2
        assert result[0] == {"texto": "Hola"}
        assert result[1] == {"texto": "Mundo"}

    def test_filters_empty_text(self):
        """Blocks with empty texto are filtered out."""
        sg = self._make_sg()
        sg.client.chat.completions.create.return_value = \
            make_mock_openai_response({
                "bloques": [{"texto": ""}, {"texto": "Válido"}]
            })

        result = sg._generate_blocks_batch(
            {"id": 1, "title": "Test"}, None, 250, "source"
        )
        assert len(result) == 1
        assert result[0] == {"texto": "Válido"}

    def test_handles_missing_bloques_key(self):
        """Response without 'bloques' key → empty list."""
        sg = self._make_sg()
        sg.client.chat.completions.create.return_value = \
            make_mock_openai_response({"otro": "campo"})

        result = sg._generate_blocks_batch(
            {"id": 1, "title": "Test"}, None, 250, "source"
        )
        assert result == []

    def test_handles_llm_json_parse_error(self):
        """Non-JSON response → empty list (no crash)."""
        sg = self._make_sg()
        from types import SimpleNamespace
        sg.client.chat.completions.create.return_value = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="not json!!!"))],
            usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1)
        )

        result = sg._generate_blocks_batch(
            {"id": 1, "title": "Test"}, None, 250, "source"
        )
        assert result == []

    def test_handles_llm_api_error(self):
        """LLM API exception → empty list (no crash)."""
        sg = self._make_sg()
        sg.client.chat.completions.create.side_effect = Exception("API down")

        result = sg._generate_blocks_batch(
            {"id": 1, "title": "Test"}, None, 250, "source"
        )
        assert result == []

    def test_filters_non_dict_blocks(self):
        """Non-dict entries in bloques are ignored."""
        sg = self._make_sg()
        sg.client.chat.completions.create.return_value = \
            make_mock_openai_response({
                "bloques": ["not a dict", {"texto": "Válido"}, 123]
            })

        result = sg._generate_blocks_batch(
            {"id": 1, "title": "Test"}, None, 250, "source"
        )
        assert len(result) == 1
        assert result[0]["texto"] == "Válido"


class TestOutlinePassing:
    """Test that _generate_blocks_batch passes outline to prompt builder."""

    def _make_sg(self):
        sg = ScriptGenerator(MockDB(), MockConfigCanal2)
        sg._build_minimal_prompt = lambda *a, **kw: "test prompt"
        sg.client = MagicMock()
        return sg

    def test_passes_outline_to_prompt_builder(self):
        """verify outline and batch_num reach build_content_only_prompt."""
        sg = self._make_sg()
        sg.client.chat.completions.create.return_value = \
            make_mock_openai_response({"bloques": [{"texto": "Test block"}]})

        with patch("pipeline.script_generator.importlib.import_module") as mock_import:
            mock_prompts = MagicMock()
            mock_prompts.build_content_only_prompt.return_value = "prompt with outline"
            mock_import.return_value = mock_prompts

            outline = {"chapters": [{"titulo": "Cap 1"}]}
            sg._generate_blocks_batch(
                {"id": 1, "title": "Test"}, None, 250, "source",
                outline=outline, batch_num=2,
            )

            # Verify the mock was called with outline and batch_num
            assert mock_prompts.build_content_only_prompt.called
            call_kwargs = mock_prompts.build_content_only_prompt.call_args.kwargs
            assert "outline" in call_kwargs
            assert call_kwargs["outline"] == outline
            assert call_kwargs.get("batch_num") == 2

    def test_works_without_outline(self):
        """When outline is None, prompt builder should not fail."""
        sg = self._make_sg()
        sg.client.chat.completions.create.return_value = \
            make_mock_openai_response({"bloques": [{"texto": "OK"}]})

        with patch("pipeline.script_generator.importlib.import_module") as mock_import:
            mock_prompts = MagicMock()
            mock_prompts.build_content_only_prompt.return_value = "prompt"
            mock_import.return_value = mock_prompts

            result = sg._generate_blocks_batch(
                {"id": 1, "title": "Test"}, None, 250, "source",
            )
            assert len(result) == 1
            assert mock_prompts.build_content_only_prompt.called
