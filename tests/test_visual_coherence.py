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
