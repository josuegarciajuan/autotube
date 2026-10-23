"""Fase 4 — ranking multi-señal de temas (curiosidad oscura).

Fija las invariantes: orden por señales separadas, novedad frente a temas
recientes, penalización de repetición de tensión, fail-open, trazabilidad y
ausencia de ramas por slug.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from pipeline.editorial_policy import classify_tension, editorial_score
from pipeline.topic_ranking import (
    RankedTopic,
    is_disabled,
    pick_best,
    rank_candidates,
    source_quality,
    visual_potential,
)


def _cfg(**overrides):
    base = dict(
        NICHE_GUARD_ENABLED=False,  # niche_fit neutral y determinista
        EDITORIAL_CURIOSITY_ENABLED=True,
        EDITORIAL_CURIOSITY_PILOT_SHARE=0.5,
        EDITORIAL_CURIOSITY_WEIGHT=0.20,
        TOPIC_RANKING_WEIGHTS={
            "demand": 0.40,
            "niche": 0.20,
            "editorial": 0.20,
            "visual": 0.10,
            "novelty": 0.10,
            "source": 0.05,
        },
        TOPIC_RANKING_VISUAL_HINTS=["documento", "montana", "barco"],
        TOPIC_RANKING_RECENT_WINDOW=20,
        TOPIC_RANKING_REPEAT_PENALTY=0.20,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _item(title: str, **extra) -> dict:
    base = {"title": title, "text": title, "url": "", "source": "rss"}
    base.update(extra)
    return base


DARK = _item("La posada donde nadie salió con vida sin salida en la nieve")
NEUTRAL = _item("Receta de pan casero del domingo por la manana")


# ── Orden por señales ───────────────────────────────────────────────


def test_editorial_signal_orders_dark_topic_first():
    cfg = _cfg()
    ranked = rank_candidates(
        [NEUTRAL, DARK],
        cfg=cfg,
        demand_scores={DARK["title"][:120]: 0.5, NEUTRAL["title"][:120]: 0.5},
    )
    assert [r.source_item for r in ranked][0] is DARK
    assert ranked[0].signals["editorial"] > ranked[1].signals["editorial"]


def test_demand_can_outrank_editorial():
    cfg = _cfg(EDITORIAL_CURIOSITY_WEIGHT=0.05)
    ranked = rank_candidates(
        [DARK, NEUTRAL],
        cfg=cfg,
        demand_scores={DARK["title"][:120]: 0.0, NEUTRAL["title"][:120]: 1.0},
    )
    assert ranked[0].source_item is NEUTRAL


def test_equal_signals_keep_original_order():
    cfg = _cfg()
    a, b = _item("Tema uno del canal"), _item("Tema dos del canal")
    ranked = rank_candidates([a, b], cfg=cfg, demand_scores={})
    assert [r.source_item for r in ranked] == [a, b]


def test_editorial_weight_zero_neutralizes_editorial_gap():
    cfg = _cfg(EDITORIAL_CURIOSITY_WEIGHT=0.0)
    ranked = rank_candidates(
        [DARK, NEUTRAL],
        cfg=cfg,
        demand_scores={DARK["title"][:120]: 0.5, NEUTRAL["title"][:120]: 0.5},
    )
    dark = next(r for r in ranked if r.source_item is DARK)
    neutral = next(r for r in ranked if r.source_item is NEUTRAL)
    assert abs(dark.score - neutral.score) <= 0.03


# ── Novedad y diversidad ────────────────────────────────────────────


def test_novelty_penalizes_repeat_of_recent_topic():
    cfg = _cfg()
    recent = ["La posada donde nadie salió con vida sin salida en la nieve"]
    ranked = rank_candidates(
        [DARK, NEUTRAL],
        cfg=cfg,
        demand_scores={DARK["title"][:120]: 0.5, NEUTRAL["title"][:120]: 0.5},
        recent_texts=recent,
    )
    dark = next(r for r in ranked if r.source_item is DARK)
    neutral = next(r for r in ranked if r.source_item is NEUTRAL)
    assert dark.signals["novelty"] < 0.5
    assert dark.signals["similarity_max"] > 0.5
    assert ranked[0].source_item is NEUTRAL


def test_repeat_tension_is_penalized_not_vetoed():
    cfg = _cfg()
    recent = ["Nadie sobrevivió: muerte y víctimas en el barco"]  # tensión: muerte/supervivencia
    candidate = _item("Otro caso de muerte y víctimas en el barco")
    fresh = _item("Descubrimiento de un documento en la montaña")
    ranked = rank_candidates(
        [candidate, fresh],
        cfg=cfg,
        demand_scores={candidate["title"][:120]: 0.5, fresh["title"][:120]: 0.5},
        recent_texts=recent,
    )
    repeated = next(r for r in ranked if r.source_item is candidate)
    assert "repeticion" in repeated.reason
    # sigue presente (penalización, nunca veto)
    assert len(ranked) == 2


# ── Señales y trazabilidad ──────────────────────────────────────────


def test_signals_breakdown_and_reason_are_exposed():
    cfg = _cfg()
    ranked = rank_candidates([DARK], cfg=cfg, route="original")
    assert len(ranked) == 1
    item = ranked[0]
    assert isinstance(item, RankedTopic)
    for key in ("demand", "niche", "editorial", "visual", "novelty", "source"):
        assert key in item.signals
    assert item.reason.startswith("rank=")
    assert "ruta=original" in item.reason
    assert item.editorial_label.startswith("editorial:")


def test_pilot_group_is_assigned_and_deterministic():
    cfg = _cfg()
    first = rank_candidates(
        [DARK], cfg=cfg, channel_slug="canal4", day_key="2026-10-23"
    )[0]
    second = rank_candidates(
        [DARK], cfg=cfg, channel_slug="canal4", day_key="2026-10-23"
    )[0]
    assert first.pilot_group in {"pilot", "control"}
    assert first.pilot_group == second.pilot_group

    disabled = rank_candidates(
        [DARK],
        cfg=_cfg(EDITORIAL_CURIOSITY_ENABLED=False),
        channel_slug="canal4",
        day_key="2026-10-23",
    )[0]
    assert disabled.pilot_group == "disabled"


def test_visual_potential_rewards_showable_topics():
    cfg = _cfg()
    rich = "El documento de 1912 hallado en la montaña junto al barco"
    poor = "Una reflexión sobre el paso del tiempo"
    assert visual_potential(rich, cfg) > visual_potential(poor, cfg)


def test_source_quality_rewards_url_and_body():
    rich = {"title": "x", "text": "a" * 500, "url": "https://example.org/a"}
    thin = {"title": "x", "text": "", "url": ""}
    assert source_quality(rich) > source_quality(thin)


# ── Fail-open y kill-switch ─────────────────────────────────────────


def test_ranking_fail_open_with_corrupt_config():
    cfg = SimpleNamespace(
        TOPIC_RANKING_WEIGHTS="not-a-dict",
        EDITORIAL_CURIOSITY_WEIGHT="nan?",
        NICHE_GUARD_ENABLED=False,
    )
    ranked = rank_candidates([DARK, NEUTRAL], cfg=cfg, demand_scores={})
    assert len(ranked) == 2  # no lanza y devuelve todo


def test_pick_best_returns_none_for_empty():
    assert pick_best([], cfg=_cfg()) is None


class _FakeDB:
    def __init__(self, state=None):
        self._state = state

    def get_system_state(self, key):
        return self._state


def test_kill_switch_disables_ranking():
    cfg = _cfg()
    assert is_disabled(_FakeDB(), cfg) is False
    assert is_disabled(_FakeDB(state="true"), cfg) is True
    assert is_disabled(None, _cfg(TOPIC_RANKING_ENABLED=False)) is True


# ── Semillas editoriales en el seeding ──────────────────────────────


def test_topic_seeding_includes_editorial_seeds():
    from pipeline.topic_seeding import seeds_from_config

    cfg = SimpleNamespace(
        TOPIC_SEED_QUERIES=["consulta propia"],
        SEO_PRIMARY_KEYWORD="sincronicidad",
        EDITORIAL_TENSION_SEEDS=["coincidencias imposibles", "premoniciones"],
    )
    seeds = seeds_from_config(cfg)
    assert "coincidencias imposibles" in seeds
    assert "premoniciones" in seeds
    assert seeds[0] == "sincronicidad"


# ── Invariante de no-hardcoding ─────────────────────────────────────


def test_module_has_no_slug_branches():
    source = Path("pipeline/topic_ranking.py").read_text(encoding="utf-8")
    for needle in ("canal2", "canal3", "canal4", "canal5", "channel_id =="):
        assert needle not in source, f"rama hardcodeada detectada: {needle}"
