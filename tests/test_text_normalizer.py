"""
Tests for pipeline/text_normalizer.py — Spanish number → words conversion.

Verifies that numbers in narration scripts are correctly converted
to their spoken equivalents so TTS engines pronounce them naturally.
"""

import pytest
from pipeline.text_normalizer import normalize_numbers


# ── Thousands-separated numbers (Spanish dot convention) ──────

def test_miles_simple():
    assert normalize_numbers("5.000") == "cinco mil"


def test_miles_with_text():
    assert normalize_numbers("5.000 personas") == "cinco mil personas"


def test_miles_multiple_groups():
    assert normalize_numbers("1.234.567") == (
        "un millón doscientos treinta y cuatro mil quinientos sesenta y siete"
    )


def test_miles_10k():
    assert normalize_numbers("10.000") == "diez mil"


def test_miles_100k():
    assert normalize_numbers("100.000") == "cien mil"


def test_miles_1million():
    assert normalize_numbers("1.000.000") == "un millón"


def test_miles_in_sentence():
    texto = "Más de 50.000 personas asistieron al evento"
    esperado = "Más de cincuenta mil personas asistieron al evento"
    assert normalize_numbers(texto) == esperado


def test_miles_beginning_of_sentence():
    assert normalize_numbers("2.000 años después") == "dos mil años después"


# ── Decimal numbers (Spanish comma convention) ─────────────────

def test_decimal_pi():
    assert normalize_numbers("3,14") == "tres coma catorce"


def test_decimal_small():
    assert normalize_numbers("0,5") == "cero coma cinco"


def test_decimal_in_text():
    texto = "un radio de 2,5 kilómetros"
    esperado = "un radio de dos coma cinco kilómetros"
    assert normalize_numbers(texto) == esperado


def test_decimal_long():
    # Long decimal tail → read digit by digit
    assert normalize_numbers("3,14159") == "tres coma uno cuatro uno cinco nueve"


# ── Percentages ────────────────────────────────────────────────

def test_porcentaje_simple():
    assert normalize_numbers("42%") == "cuarenta y dos por ciento"


def test_porcentaje_100():
    assert normalize_numbers("100%") == "cien por ciento"


def test_porcentaje_in_text():
    texto = "el 75% de la población"
    esperado = "el setenta y cinco por ciento de la población"
    assert normalize_numbers(texto) == esperado


def test_porcentaje_decimal():
    assert normalize_numbers("3,5%") == "tres coma cinco por ciento"


# ── Large standalone integers (4-7 digits) ─────────────────────

def test_entero_4digitos():
    assert normalize_numbers("5000") == "cinco mil"


def test_entero_year():
    assert normalize_numbers("1999") == "mil novecientos noventa y nueve"


def test_entero_year_2024():
    assert normalize_numbers("2024") == "dos mil veinticuatro"


def test_entero_in_text():
    texto = "en el año 1492"
    esperado = "en el año mil cuatrocientos noventa y dos"
    assert normalize_numbers(texto) == esperado


def test_entero_1500():
    assert normalize_numbers("1500") == "mil quinientos"


def test_entero_1100():
    assert normalize_numbers("1100") == "mil cien"


# ── 3-digit integers ──────────────────────────────────────────

def test_3digitos():
    assert normalize_numbers("500") == "quinientos"


def test_3digitos_100():
    assert normalize_numbers("100") == "cien"


def test_3digitos_with_text():
    texto = "a 300 metros"
    esperado = "a trescientos metros"
    assert normalize_numbers(texto) == esperado


# ── Ordinals ──────────────────────────────────────────────────

def test_ordinal_1er():
    assert normalize_numbers("1er") == "primer"


def test_ordinal_3er():
    assert normalize_numbers("3er") == "tercer"


def test_ordinal_2do():
    assert normalize_numbers("2do") == "segundo"


def test_ordinal_4to():
    assert normalize_numbers("4to") == "cuarto"


def test_ordinal_5to():
    assert normalize_numbers("5to") == "quinto"


def test_ordinal_in_text():
    texto = "el 1er lugar"
    esperado = "el primer lugar"
    assert normalize_numbers(texto) == esperado


# ── Combined patterns in realistic text ────────────────────────

def test_realistic_paragraph():
    texto = (
        "En el año 1969, más de 500.000 personas vieron cómo el Apollo 11 "
        "llegaba a la Luna a 384.400 kilómetros de distancia. Solo el 12% "
        "de la población mundial tenía televisión. Los 3 astronautas "
        "viajaron durante 3 días a 40.000 km/h."
    )
    resultado = normalize_numbers(texto)

    # Key assertions
    assert "mil novecientos sesenta y nueve" in resultado     # 1969
    assert "quinientos mil personas" in resultado             # 500.000
    assert "doce por ciento" in resultado                     # 12%
    assert "Apollo 11" in resultado                           # 11 stays (2 digits, fine)
    assert "tres astronautas" not in resultado                # "3" stays digit (1 digit)
    assert "tres días" not in resultado                       # "3" stays digit
    assert "cuarenta mil" in resultado                        # 40.000


def test_multiple_numbers():
    texto = "1er video: 5.000 vistas, 2do video: 10.000 vistas"
    resultado = normalize_numbers(texto)
    assert "primer" in resultado
    assert "cinco mil" in resultado
    assert "segundo" in resultado
    assert "diez mil" in resultado


# ── Edge cases — things that should NOT be converted ────────────

def test_no_convert_short_numbers_in_context():
    """2-digit numbers embedded in text should be left alone
    (edge-tts handles them fine)."""
    # "12" is only 2 digits, our patterns only target 3+ digits
    assert "12" in normalize_numbers("12 meses")


def test_no_convert_phone_like():
    """8+ contiguous digits should be left alone (likely phone numbers)."""
    texto = "llama al 12345678"
    assert normalize_numbers(texto) == texto


def test_no_convert_version():
    """Version strings should remain untouched."""
    assert "v2.0" in normalize_numbers("versión v2.0 del sistema")


def test_no_convert_time():
    """HH:MM time notation should remain untouched."""
    texto = "a las 10:30 de la mañana"
    assert normalize_numbers(texto) == texto


def test_3digits_in_time_context():
    """3 digits that look like part of time should be preserved."""
    # "10:30" – the : prevents matching for the 3-digit pattern
    texto = "10:30"
    assert normalize_numbers(texto) == texto


def test_empty_and_non_string():
    assert normalize_numbers("") == ""
    assert normalize_numbers(None) == ""


# ── Smoke test: large script text ──────────────────────────────

def test_large_text_does_not_crash():
    """A realistic block of Spanish narration text should not raise."""
    texto = (
        "Hace aproximadamente 13.800 millones de años, el universo tal como "
        "lo conocemos comenzó con el Big Bang. En los primeros 3 minutos, "
        "la temperatura era de 1.000 millones de grados. Hoy, el 68% del "
        "universo es energía oscura. Se estima que hay 100.000 millones "
        "de galaxias, cada una con 100.000 millones de estrellas."
    )
    try:
        resultado = normalize_numbers(texto)
        assert isinstance(resultado, str)
        assert len(resultado) > len(texto)  # words are longer than digits
    except Exception as e:
        pytest.fail(f"normalize_numbers crashed on large text: {e}")
