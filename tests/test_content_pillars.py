from __future__ import annotations
import os, sys
from types import SimpleNamespace
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from config.content_pillars import (
    match_pillar, normalize_pillars, overuse_penalty, pillar_usage, resolve_pillars,
)


def test_normalize_both_schemas():
    cfg = {"CONTENT_PILLARS": [
        {"pillar": "Uno", "pct": 60, "description": "aaa"},
        {"name": "Dos", "ratio": 40, "desc": "bbb"},
    ]}
    out = normalize_pillars(cfg)
    assert out == [
        {"name": "Uno", "ratio": 60.0, "desc": "aaa", "keywords": []},
        {"name": "Dos", "ratio": 40.0, "desc": "bbb", "keywords": []},
    ]


def test_resolve_normalizes_ratios():
    cfg = {"CONTENT_PILLARS": [{"name": "A", "ratio": 2}, {"name": "B", "ratio": 2}]}
    out = resolve_pillars(cfg)
    assert [p["ratio"] for p in out] == [50.0, 50.0]


def test_match_pillar_by_description():
    pillars = resolve_pillars({"CONTENT_PILLARS": [
        {"name": "Milagros", "ratio": 50, "desc": "curaciones inexplicables evidencia medica"},
        {"name": "Ovnis", "ratio": 50, "desc": "naves extraterrestres avistamientos"},
    ]})
    name, score = match_pillar("Curaciones inexplicables con evidencia medica real", pillars)
    assert name == "Milagros"
    assert score > 0


def test_usage_and_overuse_penalty():
    pillars = resolve_pillars({"CONTENT_PILLARS": [
        {"name": "A", "ratio": 50}, {"name": "B", "ratio": 50},
    ]})
    records = [{"signals_json": '{"pillar":"A"}'} for _ in range(9)] + [
        {"signals_json": '{"pillar":"B"}'}
    ]
    usage = pillar_usage(records)
    assert usage["A"] == 0.9
    assert overuse_penalty("A", usage, pillars) > 0
    assert overuse_penalty("B", usage, pillars) == 0


def test_phase_error_rates():
    from api.services.channel_analyzer import _phase_error_rows
    rows = [
        {"phase": "video", "status": "success", "count": 95},
        {"phase": "video", "status": "error", "count": 5},
        {"phase": "tts", "status": "success", "count": 10},
    ]
    out = {r["phase"]: r for r in _phase_error_rows(rows)}
    assert out["video"]["runs"] == 100
    assert out["video"]["error_rate_pct"] == 5.0
    assert out["tts"]["error_rate_pct"] == 0.0
