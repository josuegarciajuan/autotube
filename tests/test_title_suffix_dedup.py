"""Tests for title power-word duplication fixes.

Covered:
- metadata_generator._append_title_suffix: case-insensitive suffix dedup
  (fixes "(Impactante) (IMPACTANTE)" when the LLM fills title_suffix).
- title_enricher._collapse_trailing_parentheticals + enforce_power_words:
  belt-and-suspenders collapse of duplicate trailing parentheticals.
"""

import os
import sys

# Resolve imports against the repo this test lives in (works from a git
# worktree copy, not only from /root/autotube — conftest hardcodes the latter).
TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(TESTS_DIR)
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from pipeline.title_enricher import (
    enforce_power_words,
    _collapse_trailing_parentheticals,
)
from pipeline.metadata_generator import (
    _append_title_suffix,
    _normalize_title_suffix,
)


# ── metadata_generator._append_title_suffix ──────────────────────────

def test_append_suffix_dedups_case_insensitive():
    # LLM puso el sufijo en el título (capitalizado) y también en title_suffix
    assert _append_title_suffix("Título (Impactante)", "Impactante") == "Título (Impactante)"


def test_append_suffix_dedups_exact_uppercase():
    assert _append_title_suffix("Título (IMPACTANTE)", "impactante") == "Título (IMPACTANTE)"


def test_append_suffix_appends_when_missing():
    assert _append_title_suffix("Título", "Documental") == "Título (DOCUMENTAL)"


def test_append_suffix_empty_is_noop():
    assert _append_title_suffix("Título", "") == "Título"
    assert _append_title_suffix("Título", None) == "Título"


def test_append_suffix_strips_wrapper_symbols():
    assert _append_title_suffix("Título", "(REAL)") == "Título (REAL)"


def test_normalize_title_suffix_uppercases_and_strips():
    assert _normalize_title_suffix("(impactante)") == "IMPACTANTE"
    assert _normalize_title_suffix("") == ""


# ── title_enricher._collapse_trailing_parentheticals ─────────────────

def test_collapse_duplicate_trailing_parentheticals():
    assert _collapse_trailing_parentheticals("Título (Impactante) (IMPACTANTE)") == "Título (Impactante)"


def test_collapse_triple_duplicate():
    assert _collapse_trailing_parentheticals("Título (Real) (REAL) (real)") == "Título (Real)"


def test_collapse_keeps_single_parenthetical():
    assert _collapse_trailing_parentheticals("Título (Impactante)") == "Título (Impactante)"


def test_collapse_keeps_different_parentheticals():
    assert _collapse_trailing_parentheticals("Título (Real) (Documental)") == "Título (Real) (Documental)"


def test_collapse_no_parenthetical():
    assert _collapse_trailing_parentheticals("Título normal") == "Título normal"


# ── enforce_power_words integration ──────────────────────────────────

def test_enforce_power_words_collapses_before_present_check():
    # Ya contiene "impactante" → no inyecta nada y deja un solo paréntesis
    assert enforce_power_words("Título (Impactante) (IMPACTANTE)", ["impactante"]) == "Título (Impactante)"


def test_enforce_power_words_collapses_even_without_power_words():
    assert enforce_power_words("Título (Impactante) (IMPACTANTE)", []) == "Título (Impactante)"


def test_enforce_power_words_still_injects_when_missing():
    # Sin power word: la red de seguridad sigue inyectando (aquí se garantiza
    # que el resultado contiene la palabra y no excede 100 chars)
    result = enforce_power_words("Un título sin gancho", ["impactante"], max_chars=65)
    assert "impactante" in result.lower()
    assert len(result) <= 65
