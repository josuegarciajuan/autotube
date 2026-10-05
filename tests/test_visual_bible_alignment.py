"""Fase 3 — visual bible per-scene intent fields and strict alignment.

Run:  python3 -m pytest tests/test_visual_bible_alignment.py -v
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.visual_bible import VisualBible


def test_empty_scene_entry_has_new_intent_fields():
    entry = VisualBible._empty_scene_entry(3)
    assert entry["scene"] == 3
    assert entry["visual_concept"] == ""  # intentionally empty (cache safety)
    assert entry["subject"] == ""
    assert entry["action"] == ""
    assert entry["object"] == ""
    assert entry["setting"] == ""
    assert entry["must_show"] == []
    assert entry["must_avoid"] == []
    assert entry["depiction_mode"] == ""


def test_from_dict_preserves_provided_intent_fields():
    data = {
        "visual_universe": "u",
        "scene_visual_map": [{
            "scene": 0,
            "visual_concept": "explorer crossing dunes",
            "subject": "explorer",
            "action": "crossing dunes",
            "object": "compass",
            "setting": "Sahara desert",
            "must_show": ["dunes"],
            "must_avoid": ["smartphone"],
            "depiction_mode": "literal",
        }],
    }
    bible = VisualBible.from_dict(data, num_scenes=1)
    entry = bible.scene_visual_map[0]
    assert entry["subject"] == "explorer"
    assert entry["must_avoid"] == ["smartphone"]
    assert entry["depiction_mode"] == "literal"


def test_to_dict_preserves_intent_fields():
    data = {
        "scene_visual_map": [{
            "scene": 0, "visual_concept": "c",
            "subject": "explorer", "action": "walking",
            "must_show": ["map"], "must_avoid": ["car"],
            "depiction_mode": "literal",
        }],
    }
    out = VisualBible.from_dict(data, num_scenes=1).to_dict()
    entry = out["scene_visual_map"][0]
    assert entry["subject"] == "explorer"
    assert entry["must_show"] == ["map"]
    assert entry["depiction_mode"] == "literal"


def test_from_dict_padding_normalises_new_keys():
    bible = VisualBible.from_dict(
        {"scene_visual_map": [{"scene": 0, "visual_concept": "c0"}]},
        num_scenes=3,
    )
    assert len(bible.scene_visual_map) == 3
    for i, entry in enumerate(bible.scene_visual_map):
        assert entry["scene"] == i
        for key in ("subject", "action", "object", "setting", "depiction_mode"):
            assert key in entry
        assert "must_show" in entry and "must_avoid" in entry


def test_from_dict_alignment_derives_intent_from_scene_text():
    data = {
        "visual_universe": "u",
        "scene_visual_map": [{"scene": 0, "visual_concept": "c0"}],
    }
    scene_texts = [
        {"fragment_text": "Escena ya cubierta", "search_query_en": "covered scene"},
        {"fragment_text": "El explorador cruza el desierto", "search_query_en": "explorer crossing desert"},
        {"fragment_text": "El tiempo y la memoria se disuelven", "search_query_en": ""},
    ]
    bible = VisualBible.from_dict(data, num_scenes=3, scene_texts=scene_texts)
    assert len(bible.scene_visual_map) == 3

    # Padded entry 1: English query preferred for subject, action detected.
    e1 = bible.scene_visual_map[1]
    assert e1["visual_concept"] == ""  # left empty for cache-safety
    assert e1["subject"] == "explorer crossing desert"
    assert e1["action"] == "crossing desert"
    assert e1["depiction_mode"] == "literal"
    assert e1["must_show"]

    # Padded entry 2: purely abstract → symbolic, subject from Spanish text.
    e2 = bible.scene_visual_map[2]
    assert e2["subject"] == "El tiempo y la memoria se disuelven"
    assert e2["depiction_mode"] == "symbolic"


def test_from_dict_padding_without_scene_texts_is_fail_open():
    bible = VisualBible.from_dict(
        {"scene_visual_map": [{"scene": 0, "visual_concept": "c0"}]},
        num_scenes=2,
    )
    assert len(bible.scene_visual_map) == 2
    assert bible.scene_visual_map[1]["subject"] == ""
    assert bible.scene_visual_map[1]["depiction_mode"] == ""


def test_with_filled_concepts_preserves_intent_fields():
    bible = VisualBible.from_dict(
        {"scene_visual_map": [{"scene": 0, "visual_concept": "c0", "subject": "explorer"}]},
        num_scenes=1,
    )
    filled = bible.with_filled_concepts([
        {"scene": 0, "visual_concept": "new concept", "action": "walking"},
    ])
    entry = filled.scene_visual_map[0]
    assert entry["visual_concept"] == "new concept"
    assert entry["action"] == "walking"
    assert entry["subject"] == "explorer"  # preserved from the original


def test_from_dict_non_dict_is_fail_open():
    bible = VisualBible.from_dict("not a dict", num_scenes=2)
    assert len(bible.scene_visual_map) == 2
    assert bible.scene_visual_map[0]["depiction_mode"] == ""
