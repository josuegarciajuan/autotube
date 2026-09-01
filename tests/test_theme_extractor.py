"""Tests for ThemeContext re-anchoring from the final script."""

import sys
from pathlib import Path

import pytest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.theme_extractor import ThemeContext, reanchor_from_script


def _ctx():
    return ThemeContext(
        genre="documental",
        era="siglo_XIII",
        era_decade="medieval",
        primary_subject="ancient ruins",
        key_motifs=["castles", "torches"],
        forbidden_elements=["modern city"],
        theme_keywords_en=["medieval", "castle"],
        visual_style="cine_dramático",
        mood="misterioso",
    )


def test_reanchor_updates_era_subject_and_motifs():
    data = {
        "era": "años_1960",
        "era_decade": "1960s",
        "primary_subject": "cold war bunker",
        "key_motifs": ["bunker", "tunnels"],
        "forbidden_elements": ["medieval armor"],
    }
    with patch("config.llm_client.create_llm_client"), \
         patch("config.llm_helpers.llm_json_call_or_fallback", return_value=data), \
         patch("config.settings.LLM_MODEL_CREATIVE", "mock"):
        out = reanchor_from_script(_ctx(), "El guion final narra la guerra fría. " * 10)
    assert out is not None
    assert out.era == "años_1960"
    assert out.era_decade == "1960s"
    assert out.primary_subject == "cold war bunker"
    assert out.key_motifs == ["bunker", "tunnels"]
    # Unchanged fields are preserved.
    assert out.genre == "documental"
    assert out.theme_keywords_en == ["medieval", "castle"]


def test_reanchor_fail_open_on_llm_error():
    ctx = _ctx()
    with patch("config.llm_client.create_llm_client"), \
         patch("config.llm_helpers.llm_json_call_or_fallback",
               side_effect=RuntimeError("llm down")):
        out = reanchor_from_script(ctx, "guion " * 20)
    assert out is ctx  # original object returned unchanged


def test_reanchor_returns_original_when_script_too_short():
    ctx = _ctx()
    out = reanchor_from_script(ctx, "corto")
    assert out is ctx


def test_reanchor_returns_original_when_empty_result():
    ctx = _ctx()
    with patch("config.llm_client.create_llm_client"), \
         patch("config.llm_helpers.llm_json_call_or_fallback", return_value={}):
        out = reanchor_from_script(ctx, "guion " * 20)
    assert out is ctx
