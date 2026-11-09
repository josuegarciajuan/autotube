from __future__ import annotations
import os, sys
from types import SimpleNamespace
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from pipeline.topic_ranking import rank_candidates


def _cfg():
    return SimpleNamespace(
        CONTENT_PILLARS=[
            {"name": "Milagros", "ratio": 50, "desc": "curaciones inexplicables evidencia medica"},
            {"name": "Ovnis", "ratio": 50, "desc": "naves extraterrestres avistamientos"},
        ],
        TOPIC_RANKING_WEIGHTS={"demand": 1.0, "niche": 1.0, "editorial": 0.2,
                               "visual": 0.2, "novelty": 0.2, "source": 0.2},
    )


def test_overused_pillar_is_penalized():
    items = [
        {"title": "Curaciones inexplicables con evidencia medica"},
        {"title": "Avistamientos de naves extraterrestres"},
    ]
    base = rank_candidates(items, cfg=_cfg(), route="test")
    usage = {"Milagros": 0.9, "Ovnis": 0.1}
    biased = rank_candidates(items, cfg=_cfg(), route="test",
                             pillars=base[0].signals and [
                                 {"name": "Milagros", "ratio": 50,
                                  "desc": "curaciones inexplicables evidencia medica"},
                                 {"name": "Ovnis", "ratio": 50,
                                  "desc": "naves extraterrestres avistamientos"},
                             ], pillar_usage=usage)
    # The under-used pillar must move to the top when the bias is applied.
    assert biased[0].pillar == "Ovnis"
    assert all("pillar" in r.signals for r in biased)
