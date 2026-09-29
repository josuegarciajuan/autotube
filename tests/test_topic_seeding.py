"""Tests de la Fase 2 (search-first): seeding de temas por demanda.

Sin red: se monkeypatchea el autocompletado (``pipeline.topic_demand``) para
verificar scoring, prioridad de semillas, kill-switch y persistencia. Todo con
DB falsa.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from pipeline import topic_demand
from pipeline import topic_seeding as ts


def _cfg(**overrides):
    base = dict(
        TOPIC_SEEDING_ENABLED=True,
        TOPIC_SEED_MAX_QUERIES=12,
        TOPIC_SEEDING_MIN_SCORE=0.0,
        TOPIC_SEED_QUERIES=[],
        SEO_PRIMARY_KEYWORD="civilizaciones perdidas",
        SEO_SECONDARY_KEYWORDS=["tribus aisladas", "ruinas antiguas"],
        NICHE_KEYWORDS_ENG=["lost civilizations"],
    )
    base.update(overrides)
    return SimpleNamespace(**base)


class FakeDB:
    def __init__(self):
        self.state = {}
        self.saved = []

    def get_system_state(self, key):
        return self.state.get(key)

    def set_system_state(self, key, value):
        self.state[key] = value

    def save_topic_demand_candidates(self, channel_id, candidates, source="autocomplete"):
        self.saved.extend(candidates)
        return len(candidates)


# ─────────────────────────────────────────────────────────────────────
# Semillas
# ─────────────────────────────────────────────────────────────────────

def test_seeds_from_config_priority_and_dedupe():
    cfg = _cfg(TOPIC_SEED_QUERIES=["civilizaciones perdidas", "otra cosa"])
    seeds = ts.seeds_from_config(cfg)
    assert seeds[0] == "civilizaciones perdidas"  # primary insertado al frente
    # sin duplicado del primary que ya venía en TOPIC_SEED_QUERIES
    assert seeds.count("civilizaciones perdidas") == 1
    assert "tribus aisladas" in seeds


def test_seeds_fall_back_to_niche_when_no_seo():
    cfg = _cfg(SEO_PRIMARY_KEYWORD="", SEO_SECONDARY_KEYWORDS=[],
               NICHE_KEYWORDS_ENG=["medicina rara", "casos clinicos"])
    seeds = ts.seeds_from_config(cfg)
    assert seeds == ["medicina rara", "casos clinicos"]


# ─────────────────────────────────────────────────────────────────────
# Kill-switch
# ─────────────────────────────────────────────────────────────────────

def test_disabled_by_config():
    assert ts.is_disabled(FakeDB(), _cfg(TOPIC_SEEDING_ENABLED=False)) is True


def test_disabled_by_system_state():
    db = FakeDB()
    db.state[ts.STATE_DISABLED_KEY] = "true"
    assert ts.is_disabled(db, _cfg()) is True


# ─────────────────────────────────────────────────────────────────────
# Recolección y scoring
# ─────────────────────────────────────────────────────────────────────

def test_collect_candidates_scores_and_orders(monkeypatch):
    def fake_suggest(query, timeout=4.0):
        return {
            "civilizaciones perdidas": [
                "civilizaciones perdidas documental",
                "civilizaciones perdidas en la selva",
            ],
            "tribus aisladas": ["tribus aisladas del amazonas"],
        }.get(query, [])

    monkeypatch.setattr(topic_demand, "fetch_suggestions", fake_suggest)
    monkeypatch.setattr(topic_demand, "score_demand",
                        lambda topic, suggestions=None: 0.9 if suggestions else None)

    cfg = _cfg(TOPIC_SEED_MAX_QUERIES=10)
    cands = ts.collect_candidates(cfg)
    queries = [c["query"] for c in cands]
    assert "civilizaciones perdidas documental" in queries
    assert all(c["source"] == "autocomplete" for c in cands)
    # ordenados por score descendente
    scores = [c["demand_score"] for c in cands]
    assert scores == sorted(scores, reverse=True)


def test_collect_candidates_fail_open(monkeypatch):
    monkeypatch.setattr(topic_demand, "fetch_suggestions", lambda q, timeout=4.0: [])
    monkeypatch.setattr(topic_demand, "score_demand",
                        lambda topic, suggestions=None: None)
    assert ts.collect_candidates(_cfg()) == []


def test_collect_candidates_respects_max(monkeypatch):
    monkeypatch.setattr(topic_demand, "fetch_suggestions",
                        lambda q, timeout=4.0: [f"{q} {i}" for i in range(20)])
    monkeypatch.setattr(topic_demand, "score_demand",
                        lambda topic, suggestions=None: 0.5)
    cands = ts.collect_candidates(_cfg(TOPIC_SEED_MAX_QUERIES=5))
    assert len(cands) == 5


# ─────────────────────────────────────────────────────────────────────
# Persistencia
# ─────────────────────────────────────────────────────────────────────

def test_seed_channel_persists(monkeypatch):
    monkeypatch.setattr(topic_demand, "fetch_suggestions",
                        lambda q, timeout=4.0: [f"{q} documental"])
    monkeypatch.setattr(topic_demand, "score_demand",
                        lambda topic, suggestions=None: 0.7)
    db = FakeDB()
    out = ts.seed_channel(db, 1, _cfg())
    assert out and db.saved
    assert all("query" in c for c in db.saved)


def test_seed_channel_disabled_does_not_persist(monkeypatch):
    db = FakeDB()
    out = ts.seed_channel(db, 1, _cfg(TOPIC_SEEDING_ENABLED=False))
    assert out == []
    assert db.saved == []
