"""Unit tests for the per-scene visual context (coherence across asset types)."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.scene_context import SceneVisualContext, build_scene_context


def _bible():
    return {
        "visual_universe": "desert ruins, warm golden light, dust in the air",
        "central_entity": {
            "type": "person",
            "master_description": "a weathered explorer in a canvas coat",
            "appears_in_scenes": [1],
            "variation_by_scene": {"1": "medium shot, walking through colonnade, backlit"},
        },
        "recurring_elements": ["cracked marble columns", "incense smoke"],
        "scene_visual_map": [
            {"scene": 0, "visual_concept": "an empty desert road stretching to ruins",
             "bridge_from_prev": None},
            {"scene": 1, "visual_concept": "explorer silhouette against fire",
             "bridge_from_prev": "desert road"},
        ],
    }


class _Theme:
    era_decade = "19th century"
    era = "19th century"
    forbidden_elements = ["modern city", "cars"]
    theme_keywords_en = ["desert", "expedition"]
    primary_subject = "explorer"
    genre = "documental"
    key_motifs = ["desert dunes", "old maps"]
    mood = "épico"
    visual_style = "cine_dramático"
    lighting = "luz dorada"
    composition = "planos medios y generales"


def test_to_query_variant_uses_concept_and_era_and_is_english():
    ctx = SceneVisualContext(
        visual_concept="an empty desert road stretching to ancient ruins",
        era="19th century",
        recurring_elements=["cracked marble columns"],
    )
    v = ctx.to_query_variant()
    assert "an empty desert road" in v
    assert "19th century" in v
    assert len(v) <= 100


def test_to_query_variant_respects_max_len():
    ctx = SceneVisualContext(
        visual_concept=" ".join(["word"] * 50),
        era="19th century",
    )
    v = ctx.to_query_variant(max_len=50)
    assert len(v) <= 50


def test_to_rerank_brief_includes_phase_era_concept_forbidden():
    ctx = SceneVisualContext(
        fragment="El explorador cruzó el desierto.",
        phase_id="desarrollo",
        phase_label="EL DESCENSO",
        script_title="Expedición perdida",
        visual_concept="explorer silhouette against fire",
        era="19th century",
        forbidden_elements=["modern city", "cars"],
    )
    brief = ctx.to_rerank_brief()
    assert "EL DESCENSO" in brief
    assert "19th century" in brief
    assert "explorer silhouette" in brief
    assert "modern city" in brief


def test_build_scene_context_reads_bible_and_phase():
    scene = {"texto": "narración", "phase_id": "desarrollo", "media_tipo": "imagen"}
    structure = [{"id": "desarrollo", "step": "EL DESCENSO"}]
    ctx = build_scene_context(
        scene, scene_idx=1, theme_ctx=_Theme(), visual_bible=_bible(), structure=structure,
    )
    assert ctx.phase_id == "desarrollo"
    assert ctx.phase_label == "EL DESCENSO"
    assert ctx.era == "19th century"
    assert ctx.forbidden_elements == ["modern city", "cars"]
    # scene 1: central entity + bridge + concept
    assert "explorer silhouette" in ctx.visual_concept
    assert ctx.bridge_from_prev == "desert road"
    assert "medium shot, walking through colonnade" in ctx.central_entity


def test_build_scene_context_degrades_without_bible():
    scene = {"texto": "narración", "media_tipo": "video"}
    ctx = build_scene_context(scene, scene_idx=0, theme_ctx=_Theme(), visual_bible=None)
    assert ctx.visual_concept == ""
    assert ctx.central_entity == ""
    assert ctx.era == "19th century"  # theme still applies


def test_media_fetcher_query_pool_includes_bible_variant():
    """The stock query pool adds a bible-derived variant after the narrative-first query."""
    from pipeline.media_fetcher import MediaFetcher

    fetcher = object.__new__(MediaFetcher)
    fetcher._config = {"SCRIPT_STRUCTURE": [{"id": "desarrollo", "step": "D"}]}
    fetcher._theme_context = _Theme()
    fetcher._visual_bible = _bible()
    fetcher._media_strategy = {"era_anchor_enabled": True, "fallback_queries": []}
    scene = {
        "search_query_en": "explorer crossing desert at night",
        "texto": "El explorador cruzó el desierto de noche.",
        "tipo": "desarrollo",
        "media_tipo": "imagen",
    }
    pool = fetcher._build_query_pool(scene, fetcher._theme_context, scene_idx=1)
    # Narrative-first stays first (person sanitization appends "medium shot").
    assert pool[0].startswith("explorer crossing desert at night")
    # A bible-derived variant is present and English.
    assert any("explorer silhouette" in q for q in pool)
    assert any(len(q) <= 100 for q in pool)
