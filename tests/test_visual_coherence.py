"""Focused contracts for AI visual-impact prompt composition."""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from pipeline.media_fetcher import MediaFetcher
from pipeline.visual_coherence import VisualCoherenceEngine


def _config(**overrides):
    values = {
        "IMAGE_STYLE_MODIFIERS": "channel documentary photography",
        "COLOR_PALETTE": {"primary": (194, 154, 75)},
        "CANAL_NARRATIVE_STYLE": "historical documentary",
        "AI_VISUAL_IMPACT_STYLE": "hybrid documentary YouTube impact",
        "AI_VISUAL_COLOR_GRADING": "channel-specific vivid amber grade",
        "MEDIA_STRATEGY": {
            "video_providers": [],
            "ai_image_primary": True,
            "ai_image_fallback": True,
            "ai_max_per_video": 1,
        },
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_scene_style_combines_global_impact_and_channel_colour_grade():
    engine = VisualCoherenceEngine(_config())

    style = engine.get_scene_style(scene_idx=2, total_scenes=5)

    assert "hybrid documentary YouTube impact" in style
    assert "channel-specific vivid amber grade" in style
    assert "channel documentary photography" in style


def test_colour_arc_keeps_mid_video_colour_vivid_and_readable():
    style = VisualCoherenceEngine(_config()).get_scene_style(scene_idx=3, total_scenes=5)

    assert "vivid" in style.lower()
    assert "readable" in style.lower()
    assert "desaturat" not in style.lower()


def test_pollo_fallback_receives_same_coherent_prompt(tmp_path: Path):
    fetcher = MediaFetcher(config=_config())
    pollo = MagicMock()
    output = tmp_path / "pollo.jpg"
    output.touch()
    pollo.generate_scene_image.return_value = output
    fetcher._pollo_scene_gen = pollo

    scene = {
        "tipo": "desarrollo",
        "search_query_en": "lost city beneath jungle canopy",
        "texto": "The expedition enters the jungle.",
        "duration": 5,
    }
    asset = fetcher._try_pollo_scene(scene, scene_idx=1, total_scenes=4, ctx=None)

    assert asset["source"] == "pollo_ai"
    prompt = pollo.generate_scene_image.call_args.args[0]
    assert "lost city beneath jungle canopy" in prompt
    assert "hybrid documentary YouTube impact" in prompt
    assert "channel-specific vivid amber grade" in prompt


def test_pollinations_and_local_sd_share_the_same_coherent_prompt(tmp_path: Path):
    fetcher = MediaFetcher(config=_config())
    pollinations = MagicMock()
    local_sd = MagicMock()
    output = tmp_path / "local.jpg"
    output.touch()
    pollinations.generate.side_effect = RuntimeError("provider unavailable")
    local_sd.generate.return_value = output
    fetcher._pollinations = pollinations
    fetcher._local_sd = local_sd
    fetcher._is_valid_ai_image = MagicMock(return_value=True)

    fetcher._try_ai_image_chain(
        {
            "tipo": "desarrollo",
            "search_query_en": "lost city beneath jungle canopy",
            "texto": "The expedition enters the jungle.",
            "duration": 5,
        },
        scene_idx=1,
        total_scenes=4,
    )

    primary_prompt = pollinations.generate.call_args.kwargs["prompt"]
    fallback_prompt = local_sd.generate.call_args.kwargs["prompt"]
    assert primary_prompt == fallback_prompt
    assert "hybrid documentary YouTube impact" in fallback_prompt
    assert "channel-specific vivid amber grade" in fallback_prompt


def test_long_scene_prompt_retains_global_impact_and_channel_grade():
    bible = {
        "visual_universe": "ancient expedition " * 80,
        "central_entity": {"type": "none"},
        "recurring_elements": [],
        "scene_visual_map": [{"visual_concept": "lost city " * 100}],
    }
    fetcher = MediaFetcher(config=_config())
    fetcher.set_visual_context(visual_bible=bible)

    prompt, _seed = fetcher._build_ai_prompt(
        {"tipo": "desarrollo", "texto": "narration", "duration": 5},
        scene_idx=0,
        total_scenes=1,
    )

    assert len(prompt) <= 500
    assert "hybrid documentary YouTube impact" in prompt
    assert "channel-specific vivid amber grade" in prompt


# ── Regression: duplicate AI prompts from incomplete visual bibles ──────────
# Videos #2174/#2175/#2176/#2178 aborted with >30% placeholder ratio because
# Pollo AI/Pollinations returned identical images for different scenes: the
# visual bible's scene_visual_map was SHORTER than the scene count (e.g. 11
# entries for 120 scenes), so every scene past the map fell back to the same
# generic concept → identical prompt → same cached image → dedup rejected it.

def test_visual_bible_from_dict_pads_missing_scenes():
    """from_dict must pad a short scene_visual_map up to num_scenes."""
    from pipeline.visual_bible import VisualBible

    bible = VisualBible.from_dict(
        {"scene_visual_map": [{"visual_concept": "real concept"}]},
        num_scenes=10,
    )
    assert len(bible.scene_visual_map) == 10
    # The original entry is preserved.
    assert bible.scene_visual_map[0]["visual_concept"] == "real concept"
    # Padded entries have EMPTY concepts (fetcher appends scene variation).
    assert all(not bible.scene_visual_map[i]["visual_concept"]
               for i in range(1, 10))


def test_visual_bible_fallback_has_empty_concepts():
    """_fallback must not reuse one literal concept for every scene."""
    from pipeline.visual_bible import VisualBible

    bible = VisualBible._fallback(5)
    assert len(bible.scene_visual_map) == 5
    assert all(not entry["visual_concept"]
               for entry in bible.scene_visual_map)


def test_prompts_unique_when_bible_incomplete():
    """Scenes past a short scene_visual_map must produce DIFFERENT prompts."""
    bible = {
        "visual_universe": "shared universe",
        "central_entity": {"type": "none"},
        "recurring_elements": [],
        "scene_visual_map": [
            {"visual_concept": "concept for scene zero"},
            {"visual_concept": "concept for scene one"},
            {"visual_concept": "concept for scene two"},
        ],
    }
    fetcher = MediaFetcher(config=_config())
    fetcher.set_visual_context(visual_bible=bible)

    scene = {"tipo": "desarrollo", "texto": "generic narration",
             "search_query_en": "", "duration": 5}

    prompt_a, _ = fetcher._build_ai_prompt(scene, scene_idx=5, total_scenes=10)
    prompt_b, _ = fetcher._build_ai_prompt(scene, scene_idx=6, total_scenes=10)

    assert prompt_a != prompt_b, "Scenes past the vb map must not share a prompt"
    assert "scene 6/10" in prompt_a
    assert "scene 7/10" in prompt_b


def test_prompt_keeps_vb_concept_when_present():
    """A real vb concept must be preserved AND still get the scene marker.

    The scene marker is now prepended unconditionally: it guarantees prompt
    uniqueness even when the LLM repeats the same visual_concept across
    scenes (observed in production), while the vb concept itself remains
    untouched inside the prompt.
    """
    bible = {
        "visual_universe": "shared universe",
        "central_entity": {"type": "none"},
        "recurring_elements": [],
        "scene_visual_map": [
            {"visual_concept": "sunken temple beneath green water",
             "bridge_from_prev": None, "visual_density": "balanced"},
        ],
    }
    fetcher = MediaFetcher(config=_config())
    fetcher.set_visual_context(visual_bible=bible)

    prompt, _ = fetcher._build_ai_prompt(
        {"tipo": "desarrollo", "texto": "narration", "duration": 5},
        scene_idx=0,
        total_scenes=10,
    )

    assert "sunken temple beneath green water" in prompt
    assert "scene 1/10" in prompt, (
        "Scene marker always present (uniqueness); vb concept preserved"
    )
