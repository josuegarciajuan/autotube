"""Tests for the v49 title engine and shared title tokens.

No LLM is called: the rubric / caps / token tests are pure functions, and the
TitleEngine integration test uses ``use_llm=False``.
"""

import os
import sys
from types import SimpleNamespace

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(TESTS_DIR)
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from pipeline.title_tokens import (  # noqa: E402
    contains_banned_token,
    has_dangling_tail,
    normalize,
    uppercase_words,
)
from pipeline.title_engine import (  # noqa: E402
    TitleEngine,
    apply_caps_policy,
    score_title,
)
from pipeline.title_enricher import enforce_power_words  # noqa: E402
from api.services.packaging_policy import validate_title  # noqa: E402


def _cfg(**overrides):
    base = dict(
        TITLE_MIN_CHARS=28,
        TITLE_MAX_CHARS=65,
        TITLE_TARGET_MIN_CHARS=45,
        TITLE_TARGET_MAX_CHARS=70,
        TITLE_CAPS_POLICY="sentence",
        TITLE_ENGINE_ENABLED=True,
        TITLE_CANDIDATE_COUNT=5,
        TITLE_FORMULAS=[],
        TITLE_POWER_WORDS=[],
        TITLE_BANNED_PATTERNS=[],
        TITLE_REQUIRED_SPECIFICITY=[],
        SEO_PRIMARY_KEYWORD="",
    )
    base.update(overrides)
    return SimpleNamespace(**base)


# ── (a) tokens ───────────────────────────────────────────────────────

def test_normalize_strips_accents_and_case():
    assert normalize("Increíble") == "increible"
    assert normalize("Extraño") == "extrano"


def test_contains_banned_token_clickbait_markers():
    assert contains_banned_token("El rodaje maldito de Saigón")
    assert contains_banned_token("Una historia increíble")
    assert contains_banned_token("Algo IMPOSIBLE de explicar")
    assert contains_banned_token("Licencia: el loophole que NADIE te contó")
    assert not contains_banned_token("Un caso real sin misterio")


def test_contains_banned_token_broken_injected_tails():
    assert contains_banned_token("Un caso | La Verdad Ocultado")
    assert contains_banned_token("El Secreto Primera")
    assert contains_banned_token("Extraño para Siempre")


def test_has_dangling_tail_detects_connectors_and_punctuation():
    assert has_dangling_tail("La historia que nadie contó sobre")
    assert has_dangling_tail("El caso de")
    assert has_dangling_tail("Un título roto |")
    assert has_dangling_tail("Otro título cortado (")
    assert not has_dangling_tail("El caso de 1987 en Madrid")
    assert not has_dangling_tail("¿Qué ocurrió realmente?")
    assert not has_dangling_tail("")


def test_uppercase_words_excludes_acronyms():
    words = uppercase_words("La NASA y el FBI NO explican el caso OVNI")
    assert "NASA" not in words
    assert "FBI" not in words
    assert "OVNI" not in words
    assert "NO" in words


# ── (b) rubric penalties ─────────────────────────────────────────────

def test_rubric_penalizes_banned_token():
    cfg = _cfg()
    _score, breakdown = score_title(
        "El caso maldito ocurrió en 1987 en la ciudad de Madrid", {}, cfg
    )
    assert "banned_token" in breakdown["penalties"]


def test_rubric_penalizes_dangling_tail():
    cfg = _cfg()
    _score, breakdown = score_title(
        "La historia que cambió el caso y nadie supo nunca por qué sobre", {}, cfg
    )
    assert "dangling_tail" in breakdown["penalties"]


def test_rubric_penalizes_length_out_of_band():
    cfg = _cfg()
    _score, breakdown = score_title("Caso corto", {}, cfg)
    assert "length" in breakdown["penalties"]
    _score2, breakdown2 = score_title(
        "Un título larguísimo que se pasa muchísimo de la banda objetivo permitida", {}, cfg
    )
    assert "length" in breakdown2["penalties"]


def test_rubric_penalizes_excessive_and_all_caps():
    cfg = _cfg()
    _score, breakdown = score_title(
        "La verdad NO ES CLARA y el caso de 1987 en Madrid", {}, cfg
    )
    assert "excessive_caps" in breakdown["penalties"]

    _score2, breakdown2 = score_title("TODO ESTO ES MUY EXTRAÑO Y RARO", {}, cfg)
    assert "all_caps" in breakdown2["penalties"]


def test_rubric_penalizes_injected_suffix_and_clickbait():
    cfg = _cfg()
    _score, breakdown = score_title(
        "El caso de 1987 en Madrid (REAL)", {}, cfg
    )
    assert "clickbait_suffix" in breakdown["penalties"]

    _score2, breakdown2 = score_title(
        "El caso de 1987 en Madrid | cierre", {}, cfg
    )
    assert "injected_suffix" in breakdown2["penalties"]


def test_rubric_clean_title_beats_broken_title():
    cfg = _cfg()
    good, _ = score_title(
        "Jung y Pauli: la sincronicidad que cambió la física en 1952", {}, cfg
    )
    bad, _ = score_title(
        "Cosa increíble | La Verdad Ocultado", {}, cfg
    )
    assert good > bad


# ── (c) caps policy ──────────────────────────────────────────────────

def test_apply_caps_policy_sentence_preserves_proper_nouns_and_acronyms():
    cfg = _cfg(TITLE_CAPS_POLICY="sentence")
    out = apply_caps_policy(
        "Jung y Pauli: LA SINCRONICIDAD que cambió la física", cfg
    )
    assert out == "Jung y Pauli: la sincronicidad que cambió la física"
    out2 = apply_caps_policy("El informe de la NASA sobre el ADN", cfg)
    assert "NASA" in out2 and "ADN" in out2


def test_apply_caps_policy_title_case():
    cfg = _cfg(TITLE_CAPS_POLICY="title_case")
    out = apply_caps_policy("la guerra que césar no podía evitar", cfg)
    assert out == "La Guerra que César no Podía Evitar"


def test_apply_caps_policy_one_word_caps():
    cfg = _cfg(TITLE_CAPS_POLICY="one_word_caps")
    out = apply_caps_policy("La guerra que César NO podía evitar", cfg)
    assert out == "La guerra que César NO podía evitar"
    # Only one emphasised word.
    assert sum(1 for w in out.split() if w.isupper()) == 1


def test_apply_caps_policy_never_cuts_mid_word():
    cfg = _cfg(TITLE_MAX_CHARS=20)
    out = apply_caps_policy("Narración extraordinaria sobre el caso", cfg)
    assert len(out) <= 20
    assert "extraordina" not in out
    assert out == "Narración"

    # A single token longer than the budget is returned intact (never split).
    cfg_tiny = _cfg(TITLE_MAX_CHARS=5)
    assert apply_caps_policy("Extraordinario", cfg_tiny) == "Extraordinario"


def test_apply_caps_policy_removes_injected_markers():
    cfg = _cfg()
    out = apply_caps_policy("El caso de 1987 en Madrid | cierre", cfg)
    assert "|" not in out and "[" not in out and "]" not in out


# ── (d) enricher no longer mutates ───────────────────────────────────

def test_enforce_power_words_returns_title_intact_without_power_word():
    title = "La historia que cambió el caso en 1987"
    assert enforce_power_words(title, ["impactante"], max_chars=100) == title


def test_enforce_power_words_returns_title_intact_with_power_word():
    title = "Un final impactante en 1987"
    assert enforce_power_words(title, ["impactante"], max_chars=100) == title


def test_enforce_power_words_never_appends_broken_suffix():
    title = "Un título sin gancho"
    out = enforce_power_words(title, ["impactante", "increíble"], max_chars=65)
    assert out == title
    assert "|" not in out and "—" not in out


def test_enforce_power_words_ignores_banned_power_words():
    title = "Un título limpio de 1987"
    out = enforce_power_words(title, ["increíble", "imposible"], max_chars=65)
    assert out == title


# ── (e) packaging validate_title ─────────────────────────────────────

def test_validate_title_rejects_injected_suffix():
    cfg = _cfg(TITLE_MIN_CHARS=1, TITLE_MAX_CHARS=100)
    result = validate_title("El caso de 1987 en Madrid [REAL]", cfg)
    assert not result.valid
    assert "injected_suffix" in result.reasons


def test_validate_title_rejects_incomplete_phrase():
    cfg = _cfg(TITLE_MIN_CHARS=1, TITLE_MAX_CHARS=100)
    result = validate_title("La historia que nadie contó sobre", cfg)
    assert not result.valid
    assert "incomplete_phrase" in result.reasons


def test_validate_title_rejects_banned_token():
    cfg = _cfg(TITLE_MIN_CHARS=1, TITLE_MAX_CHARS=100)
    result = validate_title("Un caso maldito", cfg)
    assert not result.valid
    assert "banned_token" in result.reasons


def test_validate_title_accepts_clean_title():
    cfg = _cfg(TITLE_MIN_CHARS=1, TITLE_MAX_CHARS=100)
    assert validate_title("Jung y Pauli: la sincronicidad de 1952", cfg).valid


# ── TitleEngine deterministic integration (no LLM) ───────────────────

def test_title_engine_generate_uses_fallback_without_llm():
    cfg = _cfg(SEO_PRIMARY_KEYWORD="sincronicidad")
    script = {
        "titulo_options": '["Jung y Pauli: la sincronicidad que cambió la física"]',
        "keywords": '["sincronicidad"]',
    }
    result = TitleEngine(cfg).generate(script, use_llm=False)
    assert result["selected_title"]
    assert "|" not in result["selected_title"]
    assert result["candidates"]
    assert isinstance(result["rationale"], str)
    # Deterministic fallback is always a complete, grammatical title.
    assert not has_dangling_tail(result["selected_title"])
    assert not contains_banned_token(result["selected_title"])


def test_title_engine_generate_falls_back_when_llm_fails(monkeypatch):
    cfg = _cfg()
    engine = TitleEngine(cfg)
    monkeypatch.setattr(engine, "_llm_generate_candidates", lambda *a, **k: [])
    result = engine.generate({"titulo_options": "[]", "keywords": "[]"})
    assert result["selected_title"]
    assert result["candidates"][0]["source"] == "deterministic"
