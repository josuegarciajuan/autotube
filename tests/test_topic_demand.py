"""Tests de T1.3: scoring de demanda de temas (autocomplete, fail-open)."""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from pipeline import topic_demand as td  # noqa: E402


def test_parse_suggestions_json():
    raw = '["gobekli tepe", [["gobekli tepe misterio", 0], ["gobekli tepe templo", 0]]]'
    assert td._parse_suggestions(raw) == ["gobekli tepe misterio", "gobekli tepe templo"]


def test_parse_suggestions_jsonp():
    raw = 'window.google.ac.h(["q", [["a b c", 0]]])'
    assert td._parse_suggestions(raw) == ["a b c"]


def test_parse_suggestions_garbage():
    assert td._parse_suggestions("") == []
    assert td._parse_suggestions("<html>error</html>") == []


def test_score_demand_with_hits():
    topic = "El enigma de Göbekli Tepe"
    suggestions = [
        "gobekli tepe enigma historia",
        "gobekli tepe que es",
        "otra cosa distinta",
    ]
    score = td.score_demand(topic, suggestions)
    assert score is not None and score > 0.0


def test_score_demand_no_hits_is_zero():
    score = td.score_demand("Tema raro sin busquedas", ["otra cosa distinta"])
    assert score == 0.0


def test_rank_labels_sorts_by_score(monkeypatch):
    db = {
        "tema popular": ["tema popular documental", "tema popular historia"],
        "tema raro": ["nada relacionado"],
    }
    monkeypatch.setattr(td, "fetch_suggestions", lambda q, timeout=4.0: db.get(q, []))
    td._cache.clear()
    # Simula que las llamadas tuvieron éxito (cache poblada).
    for k, v in db.items():
        td._cache[k] = (1e12, v)

    ranked = td.rank_labels(["tema raro", "tema popular"])
    assert ranked[0][0] == "tema popular"
    assert ranked[0][1] > ranked[1][1]


def test_rank_labels_fail_open_preserves_order(monkeypatch):
    monkeypatch.setattr(td, "fetch_suggestions", lambda q, timeout=4.0: [])
    td._cache.clear()
    ranked = td.rank_labels(["uno", "dos", "tres"])
    # Sin red, todos neutrales → orden original estable.
    assert [r[0] for r in ranked] == ["uno", "dos", "tres"]
    assert all(r[1] == td.NEUTRAL_SCORE for r in ranked)
