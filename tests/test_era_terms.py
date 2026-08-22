"""Tests for pipeline/era_terms.py — era anchoring and anachronism detection.

Run:  python3 -m pytest tests/test_era_terms.py -v
"""

import sys
from pathlib import Path
# Raíz del repo dinámica (mismo patrón que tests/conftest.py)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.era_terms import (
    ANACHRONISM_WORDS,
    anachronism_hits,
    era_anchor,
    normalize_era,
)


class TestEraAnchor:
    """era_anchor(era_decade, era) → stock phrase or None."""

    def test_siglo_xvii_contains_17th_century(self):
        assert "17th century" in era_anchor("siglo_xvii", "siglo_XVII")

    def test_1611_year_maps_to_17th_century(self):
        assert "17th century" in era_anchor("", "1611")

    def test_17th_century_english(self):
        assert "17th century" in era_anchor("17th century", "")

    def test_seventeenth_word(self):
        assert "17th century" in era_anchor("", "seventeenth")

    def test_medieval_and_siglo_xiii(self):
        assert "medieval" in era_anchor("", "medieval")
        assert "medieval" in era_anchor("", "siglo_xiii")
        assert "medieval" in era_anchor("", "siglo_xv")

    def test_centuries_16_18_19(self):
        assert "16th century" in era_anchor("", "siglo_xvi")
        assert "18th century" in era_anchor("", "siglo_xviii")
        assert "19th century" in era_anchor("", "siglo_xix")

    def test_ancient_world(self):
        for era in ("ancient", "antigua", "antigüedad", "roma", "egipto", "grecia"):
            assert "ancient" in era_anchor("", era), era

    def test_decades(self):
        assert "1980s" in era_anchor("1980s", "")
        assert "1960s" in era_anchor("1960s", "")
        assert "1980s" in era_anchor("", "los años ochenta")

    def test_present_future_is_none(self):
        assert era_anchor("", "presente") is None
        assert era_anchor("", "actualidad") is None
        assert era_anchor("", "futuro") is None
        assert era_anchor("atemporal", "") is None

    def test_unknown_era_is_none(self):
        assert era_anchor("", "concepto_abstracto_xyz") is None
        assert era_anchor("", "") is None


class TestNormalizeEra:
    """normalize_era(text) → canonical key, original if unrecognized."""

    def test_siglo_roman(self):
        assert normalize_era("siglo_XVII") == "17th century"
        assert normalize_era("siglo xiii") == "13th century"

    def test_years_and_decades(self):
        assert normalize_era("1611") == "17th century"
        assert normalize_era("años 80") == "1980s"
        assert normalize_era("los años ochenta") == "1980s"
        assert normalize_era("1980s") == "1980s"

    def test_unrecognized_unchanged(self):
        assert normalize_era("") == ""
        assert normalize_era("presente") == "presente"


class TestAnachronismWords:
    """anachronism_hits / ANACHRONISM_WORDS sanity."""

    def test_hits_modern_terms(self):
        hits = anachronism_hits("modern city canal traffic drone")
        assert "city" in hits
        assert "canal" in hits
        assert "modern" in hits

    def test_word_boundary_no_false_positive(self):
        # "car" must not match "cart"; nautical words are NOT anachronisms
        assert anachronism_hits("horse cart wooden ship ocean sea") == []

    def test_nautical_words_not_flagged(self):
        for word in ("ship", "boat", "water", "ocean", "sea", "sailing"):
            assert word not in ANACHRONISM_WORDS, word
