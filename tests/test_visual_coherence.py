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


# ── Personas a distancia: regla universal de encuadre ────────────────

def test_style_prefix_keeps_people_at_a_distance():
    style = VisualCoherenceEngine(_config()).get_scene_style(scene_idx=2, total_scenes=5)

    assert "medium or wide shot" in style
    assert "never close-up" in style
    assert "people appear" in style


def test_tech_suffix_simple_no_shallow_dof():
    """Densidad 'simple' ya no empuja al sujeto al primer plano."""
    suffix = VisualCoherenceEngine.build_tech_suffix("simple")

    assert "shallow depth of field" not in suffix
    assert "blurred background" not in suffix
    assert "moderate depth of field" in suffix
    assert "subject at comfortable distance" in suffix


def test_style_prefix_uses_rule_of_thirds():
    style = VisualCoherenceEngine(_config()).base_style_prefix

    assert "rule of thirds" in style
    assert "off-center subject" in style


def test_negative_prompt_rejects_close_up_faces_and_bad_hands():
    negative = VisualCoherenceEngine.build_negative_prompt()

    assert "close-up face" in negative
    assert "extreme close-up portrait" in negative
    assert "foreground face" in negative
    assert "deformed hands" in negative
    assert "extra fingers" in negative


def test_theme_extractor_defaults_to_medium_and_wide_shots():
    from pipeline.theme_extractor import ThemeContext

    ctx = ThemeContext()
    assert "planos medios y generales" in ctx.composition


def test_pollo_prompt_keeps_people_at_a_distance():
    from pipeline.theme_extractor import ThemeContext

    ctx = ThemeContext(era_decade="1980s")
    prompt = ctx.to_pollo_prompt("city street")

    assert "medium or wide shot" in prompt
    assert "never close-up or foreground" in prompt


def test_build_ai_prompt_injects_era_decade():
    from types import SimpleNamespace

    fetcher = MediaFetcher(config=_config())
    fetcher.set_theme_context(SimpleNamespace(era_decade="1980s", era=""))

    prompt, _seed = fetcher._build_ai_prompt(
        {"tipo": "desarrollo", "texto": "narration", "duration": 5},
        scene_idx=0,
        total_scenes=1,
    )

    assert "1980s" in prompt


def test_ai_image_chain_adds_forbidden_elements_to_negative(tmp_path: Path):
    from types import SimpleNamespace

    fetcher = MediaFetcher(config=_config())
    fetcher.set_theme_context(
        SimpleNamespace(forbidden_elements=["smartphones", "modern buildings"])
    )
    pollinations = MagicMock()
    output = tmp_path / "forbidden.jpg"
    output.touch()
    pollinations.generate.return_value = output
    fetcher._pollinations = pollinations
    fetcher._is_valid_ai_image = MagicMock(return_value=True)

    fetcher._try_ai_image_chain(
        {
            "tipo": "desarrollo",
            "search_query_en": "medieval marketplace",
            "texto": "Merchants sell goods.",
            "duration": 5,
        },
        scene_idx=0,
        total_scenes=1,
    )

    negative = pollinations.generate.call_args.kwargs["negative_prompt"]
    assert "smartphones" in negative
    assert "modern buildings" in negative


def test_ai_image_chain_negative_without_theme_is_global_only(tmp_path: Path):
    fetcher = MediaFetcher(config=_config())
    pollinations = MagicMock()
    output = tmp_path / "plain.jpg"
    output.touch()
    pollinations.generate.return_value = output
    fetcher._pollinations = pollinations
    fetcher._is_valid_ai_image = MagicMock(return_value=True)

    fetcher._try_ai_image_chain(
        {
            "tipo": "desarrollo",
            "search_query_en": "empty desert",
            "texto": "No theme context.",
            "duration": 5,
        },
        scene_idx=0,
        total_scenes=1,
    )

    negative = pollinations.generate.call_args.kwargs["negative_prompt"]
    assert "close-up face" in negative  # global rule still present
    assert "smartphones" not in negative


# ── Vívido / contraste / bokeh: regresión para imágenes IA de escena ──

def test_tech_suffix_demands_bokeh_contrast_and_vibrancy():
    suffix = VisualCoherenceEngine.build_tech_suffix("balanced")

    assert "bokeh" in suffix
    assert "high contrast" in suffix
    assert "vibrant saturated colour" in suffix
    assert "crisp in-focus subject" in suffix
    assert "ultra sharp" in suffix
    # "no blur" desapareció — se sustituye por enfoque positivo + bokeh.
    assert "no blur" not in suffix


def test_negative_prompt_rejects_dark_and_flat_renders():
    negative = VisualCoherenceEngine.build_negative_prompt()

    assert "dark" in negative
    assert "underexposed" in negative
    assert "low contrast" in negative
    assert "flat" in negative
    assert "washed out" in negative
    assert "soft focus" in negative


def test_palette_hint_never_says_dark_moody_or_muted():
    hint = VisualCoherenceEngine._palette_to_hint(
        {"primary": (15, 40, 65)}   # el primary más oscuro de producción (canal4)
    )

    assert "dark moody" not in hint
    assert "muted" not in hint
    assert hint  # siempre devuelve un hint positivo


def test_defaults_impact_style_carries_bokeh_high_contrast_vivid():
    from config import defaults

    impact = defaults.AI_VISUAL_IMPACT_STYLE.lower()
    grading = defaults.AI_VISUAL_COLOR_GRADING.lower()

    assert "bokeh" in impact
    assert "high contrast" in impact
    assert "vivid" in impact
    assert "luminous" in grading
    assert "contrast" in grading


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

    assert len(prompt) <= 1000
    assert "hybrid documentary YouTube impact" in prompt
    assert "channel-specific vivid amber grade" in prompt
    # Regression: el marcador de escena NUNCA debe perderse en la truncación.
    assert "scene 1/1" in prompt


def test_long_prompt_keeps_scene_concept_under_production_style():
    """Las configs de producción (impact style largo) ya no descartan el concepto."""
    fetcher = MediaFetcher(config=_config(
        AI_VISUAL_IMPACT_STYLE=(
            "hybrid cinematic documentary photography with high-impact YouTube "
            "visual storytelling, immediate readable focal subject, vivid "
            "natural colour, strong subject-background separation, premium "
            "editorial detail"
        ),
        AI_VISUAL_COLOR_GRADING=(
            "vivid readable cinematic colour grade, natural skin tones, "
            "preserved highlight and shadow detail"
        ),
    ))

    scene = {
        "tipo": "desarrollo",
        "search_query_en": "merchant weighing gold scale ancient marketplace",
        "texto": "Los mercaderes pesaban el oro.", "duration": 5,
    }
    prompt, _ = fetcher._build_ai_prompt(scene, scene_idx=2, total_scenes=10)

    assert len(prompt) <= 1000
    assert "scene 3/10" in prompt, "marcador de escena debe sobrevivir"
    assert "merchant" in prompt, "concepto de escena debe sobrevivir"
    assert "medium or wide shot" in prompt


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
