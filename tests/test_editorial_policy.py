"""Fase 3 — modelo editorial «curiosidad oscura» (config-driven).

Estos tests fijan las invariantes del modelo: resolución de defaults,
overrides por canal vía config_bridge, clasificación determinista, exclusiones
sensibles, asignación piloto/control estable y ausencia de ramas por slug.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from config.config_bridge import get_channel_config
from pipeline.editorial_policy import (
    EditorialPolicy,
    build_policy,
    classify_tension,
    dominant_tension,
    editorial_label,
    editorial_score,
    is_eligible,
    is_enabled,
    is_sensitive,
    pilot_decision,
)

DARK_TOPIC = "La posada donde nadie salió con vida: el hielo los atrapó sin salida"
NEUTRAL_TOPIC = "Receta de pan casero para el desayuno del domingo"


class _FakeDB:
    def __init__(self, state=None):
        self._state = state

    def get_system_state(self, key):
        return self._state


# ── Resolución de config ─────────────────────────────────────────────


def test_defaults_policy_when_config_is_empty():
    policy = build_policy(SimpleNamespace())
    assert isinstance(policy, EditorialPolicy)
    assert policy.enabled is True
    assert policy.pilot_share == 0.5
    assert policy.weight == 0.25
    assert len(policy.tension_types) == 6
    assert set(policy.tension_weights) == set(policy.tension_types)
    assert policy.tension_lexicon  # léxico por defecto no vacío
    assert policy.excluded_categories
    assert policy.require_factual_support is True
    assert policy.diversity_min == 2


def test_channel_overrides_are_read_via_config_bridge():
    """Cada canal ajusta sus pesos; el bridge es la única vía de lectura."""
    canal2 = build_policy(get_channel_config("canal2", force_reload=True))
    canal3 = build_policy(get_channel_config("canal3", force_reload=True))
    canal4 = build_policy(get_channel_config("canal4", force_reload=True))
    canal5 = build_policy(get_channel_config("canal5", force_reload=True))

    assert canal2.tension_weights["anomalia"] == 1.0
    assert canal3.tension_weights["colapso"] == 1.0
    assert canal4.tension_weights["peligro"] == 1.0
    assert canal4.tension_weights["supervivencia"] == 1.0
    assert canal5.tension_weights["anomalia"] == 1.0
    # Todas las piezas del piloto arrancan al 50 %.
    assert {p.pilot_share for p in (canal2, canal3, canal4, canal5)} == {0.5}


def test_channel_seeds_are_declared_in_config_not_code():
    for slug in ("canal2", "canal3", "canal4", "canal5"):
        cfg = get_channel_config(slug, force_reload=True)
        seeds = getattr(cfg, "EDITORIAL_TENSION_SEEDS", None)
        assert seeds and len(seeds) >= 4, slug


# ── Clasificación de tensión ─────────────────────────────────────────


def test_classify_detects_dark_case():
    cfg = get_channel_config("canal4", force_reload=True)
    scores = classify_tension(DARK_TOPIC, cfg)
    assert "muerte" in scores  # "nadie salió", "con vida"
    assert "peligro" in scores  # "atrapó", "sin salida"
    assert dominant_tension(DARK_TOPIC, cfg) in {"peligro", "muerte"}
    assert editorial_score(DARK_TOPIC, cfg) >= 0.9


def test_classify_neutral_topic_has_no_signal():
    cfg = SimpleNamespace()
    assert classify_tension(NEUTRAL_TOPIC, cfg) == {}
    assert editorial_score(NEUTRAL_TOPIC, cfg) == 0.0
    assert dominant_tension(NEUTRAL_TOPIC, cfg) == ""


def test_weight_zero_vetoes_a_tension_type():
    cfg = SimpleNamespace(
        EDITORIAL_TENSION_WEIGHTS={
            "peligro": 0.0,
            "muerte": 1.0,
        },
        EDITORIAL_TENSION_TYPES=["peligro", "muerte"],
    )
    scores = classify_tension(DARK_TOPIC, cfg)
    assert "peligro" not in scores
    assert scores.get("muerte", 0) > 0


def test_editorial_score_is_bounded():
    cfg = get_channel_config("canal3", force_reload=True)
    for text in (DARK_TOPIC, NEUTRAL_TOPIC, "", "colapso y ruina y abandono"):
        score = editorial_score(text, cfg)
        assert 0.0 <= score <= 1.0


# ── Exclusiones sensibles y elegibilidad ─────────────────────────────


def test_sensitive_categories_are_excluded():
    cfg = SimpleNamespace()
    assert is_sensitive("un caso de suicidio entre adolescentes", cfg) is True
    assert is_sensitive("menores desaparecidos en el monte", cfg) is True
    assert is_sensitive("la cura milagrosa del cancer", cfg) is True
    assert is_sensitive("una masacre sangrienta", cfg) is True
    assert is_sensitive(NEUTRAL_TOPIC, cfg) is False


def test_eligibility_requires_tension_and_not_sensitive():
    cfg = SimpleNamespace()
    assert is_eligible(DARK_TOPIC, cfg) is True
    assert is_eligible(NEUTRAL_TOPIC, cfg) is False
    assert is_eligible("nadie sobrevivió: caso de suicidio", cfg) is False


def test_eligibility_honours_factual_support_gate():
    cfg = SimpleNamespace(EDITORIAL_REQUIRE_FACTUAL_SUPPORT=True)
    assert is_eligible(DARK_TOPIC, cfg, has_factual_support=False) is False
    assert is_eligible(DARK_TOPIC, cfg, has_factual_support=True) is True
    # "Desconocido" no veta: la Fase 4 pasa la señal cuando la tiene.
    assert is_eligible(DARK_TOPIC, cfg, has_factual_support=None) is True

    relaxed = SimpleNamespace(EDITORIAL_REQUIRE_FACTUAL_SUPPORT=False)
    assert is_eligible(DARK_TOPIC, relaxed, has_factual_support=False) is True


def test_kill_switch_disables_policy():
    cfg = SimpleNamespace()
    assert is_enabled(_FakeDB(), cfg) is True
    assert is_enabled(_FakeDB(state="true"), cfg) is False
    assert is_enabled(_FakeDB(state="0"), cfg) is True
    assert is_enabled(_FakeDB(state=None), cfg) is True


# ── Asignación piloto/control ────────────────────────────────────────


def test_pilot_decision_is_deterministic():
    cfg = SimpleNamespace()
    first = pilot_decision("canal4", "2026-10-23", cfg)
    assert first in {"pilot", "control"}
    assert first == pilot_decision("canal4", "2026-10-23", cfg)
    assert first == pilot_decision("canal4", "2026-10-23", SimpleNamespace())


def test_pilot_share_extremes():
    assert pilot_decision("canal2", "2026-01-01", SimpleNamespace(EDITORIAL_CURIOSITY_PILOT_SHARE=0.0)) == "control"
    assert pilot_decision("canal2", "2026-01-01", SimpleNamespace(EDITORIAL_CURIOSITY_PILOT_SHARE=1.0)) == "pilot"
    assert pilot_decision("canal2", "2026-01-01", SimpleNamespace(EDITORIAL_CURIOSITY_ENABLED=False)) == "disabled"


def test_pilot_share_is_roughly_half_over_time():
    cfg = SimpleNamespace(EDITORIAL_CURIOSITY_PILOT_SHARE=0.5)
    results = [
        pilot_decision("canal3", f"2026-{month:02d}-{day:02d}", cfg)
        for month in range(1, 7)
        for day in range(1, 29)
    ]
    pilot_ratio = results.count("pilot") / len(results)
    assert 0.3 <= pilot_ratio <= 0.7, pilot_ratio


# ── Etiqueta e invariante de no-hardcoding ───────────────────────────


def test_editorial_label_is_stable_and_informative():
    cfg = get_channel_config("canal4", force_reload=True)
    assert editorial_label(DARK_TOPIC, cfg).startswith("editorial:")
    assert editorial_label(DARK_TOPIC, cfg) != "editorial:none"
    assert editorial_label(NEUTRAL_TOPIC, cfg) == "editorial:none"


def test_module_has_no_slug_or_channel_id_branches():
    source = Path("pipeline/editorial_policy.py").read_text(encoding="utf-8")
    for needle in ("canal2", "canal3", "canal4", "canal5", "channel_id =="):
        assert needle not in source, f"rama hardcodeada detectada: {needle}"
