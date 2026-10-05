"""Fase 3 — per-scene filmable intent, temporal segments and anachronism exemption.

Run:  python3 -m pytest tests/test_scene_context_action.py -v
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.era_terms import is_anachronism_exempt
from pipeline.scene_context import (
    SceneVisualContext,
    build_scene_context,
    derive_depiction_mode,
)
from pipeline.theme_extractor import ThemeContext, era_for_scene


# ═══════════════════════════════════════════════════════════════════
# prev/next snippet resolution via the fetcher's current scene list
# ═══════════════════════════════════════════════════════════════════

def _fetcher(scenes, theme_ctx=None, visual_bible=None):
    from pipeline.media_fetcher import MediaFetcher

    f = object.__new__(MediaFetcher)
    f._config = {"SCRIPT_STRUCTURE": []}
    f._theme_context = theme_ctx
    f._visual_bible = visual_bible
    f._current_scenes = scenes
    f._current_scene_idx = -1
    return f


def test_scene_context_resolves_prev_next_from_scene_list():
    scenes = [
        {"fragment_text": "primera escena", "texto": "primera escena"},
        {"fragment_text": "escena central", "texto": "escena central"},
        {"fragment_text": "última escena", "texto": "última escena"},
    ]
    f = _fetcher(scenes)
    ctx = f._scene_context(scenes[1], scene_idx=1)
    assert ctx.prev_snippet == "primera escena"
    assert ctx.next_snippet == "última escena"


def test_scene_context_edges_have_empty_neighbours():
    scenes = [{"texto": "a"}, {"texto": "b"}]
    f = _fetcher(scenes)
    first = f._scene_context(scenes[0], scene_idx=0)
    last = f._scene_context(scenes[1], scene_idx=1)
    assert first.prev_snippet == ""
    assert first.next_snippet == "b"
    assert last.prev_snippet == "a"
    assert last.next_snippet == ""


def test_scene_context_without_scene_list_is_fail_open():
    f = _fetcher([])
    ctx = f._scene_context({"texto": "x"}, scene_idx=0)
    assert ctx.prev_snippet == ""
    assert ctx.next_snippet == ""


def test_scene_context_snippet_prefers_fragment_then_text():
    scenes = [{"texto": "texto largo"}, {"fragment_text": "fragmento"}]
    f = _fetcher(scenes)
    ctx = f._scene_context({"texto": "x"}, scene_idx=1)
    assert ctx.prev_snippet == "texto largo"


# ═══════════════════════════════════════════════════════════════════
# intent copied from the visual bible + query/brief integration
# ═══════════════════════════════════════════════════════════════════

def _intent_bible():
    return {
        "scene_visual_map": [{
            "scene": 0,
            "visual_concept": "explorer crossing dunes",
            "subject": "explorer",
            "action": "crossing dunes",
            "object": "compass",
            "setting": "Sahara desert",
            "must_show": ["dunes", "compass"],
            "must_avoid": ["smartphone"],
            "depiction_mode": "literal",
        }],
    }


def test_build_scene_context_copies_intent_from_bible():
    ctx = build_scene_context(
        {"texto": "El explorador cruza las dunas."},
        scene_idx=0,
        visual_bible=_intent_bible(),
    )
    assert ctx.subject == "explorer"
    assert ctx.action == "crossing dunes"
    assert ctx.object == "compass"
    assert ctx.setting == "Sahara desert"
    assert ctx.must_show == ["dunes", "compass"]
    assert "smartphone" in ctx.must_avoid
    assert ctx.depiction_mode == "literal"


def test_to_query_variant_prioritises_intent():
    ctx = SceneVisualContext(
        subject="explorer",
        action="crossing dunes",
        object="compass",
        setting="Sahara desert",
        visual_concept="ignored concept",
        era="19th century",
    )
    q = ctx.to_query_variant()
    assert q.startswith("crossing dunes explorer compass Sahara desert")
    assert "ignored concept" not in q
    assert len(q) <= 100


def test_to_query_variant_falls_back_to_concept_when_no_intent():
    ctx = SceneVisualContext(
        visual_concept="explorer silhouette against fire",
        era="19th century",
    )
    q = ctx.to_query_variant()
    assert "explorer silhouette" in q


def test_to_rerank_brief_lists_intent_and_must_avoid():
    ctx = SceneVisualContext(
        subject="explorer",
        action="crossing dunes",
        must_avoid=["smartphone", "car"],
        depiction_mode="literal",
    )
    brief = ctx.to_rerank_brief()
    assert "crossing dunes" in brief
    assert "smartphone" in brief
    assert "literal" in brief


# ═══════════════════════════════════════════════════════════════════
# derive_depiction_mode
# ═══════════════════════════════════════════════════════════════════

class TestDepictionMode:
    def test_literal_on_concrete_action(self):
        assert derive_depiction_mode(
            {"texto": "El explorador cruza el desierto a caballo"}
        ) == "literal"

    def test_literal_on_concrete_subject(self):
        assert derive_depiction_mode(
            {"texto": "Un antiguo castillo se alza sobre la colina"}
        ) == "literal"

    def test_symbolic_on_abstract(self):
        assert derive_depiction_mode(
            {"texto": "El tiempo y la memoria se disuelven en la nada"}
        ) == "symbolic"

    def test_documentary_fallback(self):
        assert derive_depiction_mode(
            {"texto": "En aquella época la situación cambió lentamente"}
        ) == "documentary"

    def test_empty_is_documentary(self):
        assert derive_depiction_mode({}) == "documentary"

    def test_never_raises_on_weird_input(self):
        assert derive_depiction_mode(None) == "documentary"
        assert derive_depiction_mode(12345) == "documentary"


# ═══════════════════════════════════════════════════════════════════
# temporal segments
# ═══════════════════════════════════════════════════════════════════

class _Theme:
    def __init__(self, era_decade="medieval", segments=None, forbidden=None):
        self.era_decade = era_decade
        self.era = era_decade
        self.temporal_segments = segments or []
        self.forbidden_elements = forbidden or []
        self.primary_subject = ""
        self.mood = ""
        self.lighting = ""
        self.composition = ""
        self.genre = ""
        self.visual_style = ""


def test_era_for_scene_uses_segment_override():
    t = _Theme(era_decade="medieval", segments=[
        {"from_scene": 5, "to_scene": 7, "era_decade": "actualidad", "note": "excavacion"},
    ])
    assert era_for_scene(t, 0) == "medieval"
    assert era_for_scene(t, 5) == "actualidad"
    assert era_for_scene(t, 6) == "actualidad"
    assert era_for_scene(t, 9) == "medieval"


def test_era_for_scene_fail_open():
    assert era_for_scene(None, 0) == ""
    t = _Theme(segments=[{"from_scene": "x"}])
    assert era_for_scene(t, 3) == "medieval"


def test_era_for_scene_default_falls_back_to_object_era():
    t = ThemeContext(era="siglo_XIII", era_decade="medieval")
    assert era_for_scene(t, 0) == "medieval"


def test_is_anachronism_exempt_modern_segment():
    t = _Theme(era_decade="medieval", segments=[
        {"from_scene": 2, "to_scene": 4, "era_decade": "actualidad", "note": "excavacion moderna"},
    ])
    assert is_anachronism_exempt({"scene_idx": 3}, t) is True
    assert is_anachronism_exempt({"scene_idx": 5}, t) is False


def test_is_anachronism_exempt_by_note_only():
    t = _Theme(era_decade="medieval", segments=[
        {"from_scene": 0, "to_scene": 1, "era_decade": "medieval", "note": "imagenes actuales de archivo"},
    ])
    assert is_anachronism_exempt({"scene_idx": 0}, t) is True


def test_is_anachronism_exempt_fail_open():
    assert is_anachronism_exempt({"scene_idx": 0}, None) is False
    assert is_anachronism_exempt({}, _Theme()) is False
    assert is_anachronism_exempt({"scene_idx": 0}, _Theme(segments=[{"nope": 1}])) is False


# ═══════════════════════════════════════════════════════════════════
# must_avoid anachronism gating in build_scene_context
# ═══════════════════════════════════════════════════════════════════

def test_build_scene_context_does_not_flag_anachronism_when_exempt():
    t = _Theme(era_decade="medieval", segments=[
        {"from_scene": 0, "to_scene": 0, "era_decade": "actualidad", "note": "excavacion"},
    ])
    scene = {"scene_idx": 0, "texto": "Los arqueólogos usan un dron sobre la ciudad moderna"}
    ctx = build_scene_context(scene, scene_idx=0, theme_ctx=t)
    assert "drone" not in ctx.must_avoid
    assert "city" not in ctx.must_avoid


def test_build_scene_context_flags_anachronism_in_historical_scene():
    t = _Theme(era_decade="17th century")
    # Loanwords present in Spanish narration are caught by anachronism_hits.
    scene = {"scene_idx": 0, "texto": "El barco navega mientras un drone graba la ciudad digital"}
    ctx = build_scene_context(scene, scene_idx=0, theme_ctx=t)
    assert "drone" in ctx.must_avoid
    assert "digital" in ctx.must_avoid


def test_temporal_overrides_can_be_disabled():
    t = _Theme(era_decade="medieval", segments=[
        {"from_scene": 0, "to_scene": 0, "era_decade": "actualidad", "note": "excavacion"},
    ])
    scene = {"scene_idx": 0, "texto": "Los arqueologos usan un drone en la excavacion"}

    ctx_on = build_scene_context(scene, scene_idx=0, theme_ctx=t)
    assert ctx_on.era == "actualidad"
    assert "drone" not in ctx_on.must_avoid  # exempt

    ctx_off = build_scene_context(
        scene, scene_idx=0, theme_ctx=t, temporal_overrides_enabled=False,
    )
    assert ctx_off.era == "medieval"
    assert "drone" in ctx_off.must_avoid  # not exempt when disabled


def test_fetcher_reads_temporal_overrides_flag():
    f = _fetcher([])
    f._config = {}  # default True
    assert f._temporal_overrides_enabled() is True
    f._config = {"THEME_TEMPORAL_OVERRIDES_ENABLED": False}
    assert f._temporal_overrides_enabled() is False
    f._config = {"THEME_TEMPORAL_OVERRIDES_ENABLED": "no"}  # truthy string
    assert f._temporal_overrides_enabled() is True
