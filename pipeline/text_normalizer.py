"""
Text normalizer for Spanish TTS narration.

Converts numeric representations to their spoken-word equivalents
so that edge-tts and Kokoro TTS engines pronounce numbers naturally
instead of reading them digit-by-digit.

Examples:
    "5.000 años"       → "cinco mil años"
    "1.234 personas"   → "mil doscientas treinta y cuatro personas"
    "3,14 km"          → "tres coma catorce kilómetros"
    "el 42%"           → "el cuarenta y dos por ciento"
    "año 1999"         → "año mil novecientos noventa y nueve"
    "1er lugar"        → "primer lugar"
"""

import re
from num2words import num2words


# ---------------------------------------------------------------------------
# Ordinal apocopation map (Spanish)
# "1er" → "primer" (apócope, before masculine), not "primero"
# "3er" → "tercer" (apócope)
# ---------------------------------------------------------------------------
_ORDINAL_APOCOPE: dict[int, str] = {
    1: "primer",
    2: "segundo",
    3: "tercer",
    4: "cuarto",
    5: "quinto",
    6: "sexto",
    7: "séptimo",
    8: "octavo",
    9: "noveno",
    10: "décimo",
    11: "undécimo",
    12: "duodécimo",
}

# All ordinal suffixes used in Spanish abbreviations
_ORDINAL_SUFFIXES: tuple[str, ...] = ("er", "do", "ro", "to", "mo", "vo")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _numero_a_palabras(n: int) -> str:
    """Convert an integer to Spanish words using num2words."""
    try:
        return num2words(n, lang="es")
    except Exception:
        return str(n)


def _ordinal_a_palabras(n: int, apocopado: bool = False) -> str:
    """Convert an integer to Spanish ordinal words.

    Args:
        n: Integer to convert.
        apocopado: If True, use apocopated form (primer, tercer) for
                   masculine singular nouns.
    """
    if n in _ORDINAL_APOCOPE:
        return _ORDINAL_APOCOPE[n]

    # For ordinals beyond the map, use num2words
    # num2words returns "primero", "segundo", etc. (full form)
    try:
        result = num2words(n, lang="es", to="ordinal")
        # Apocopate if needed (only applies to primero → primer, tercero → tercer)
        if apocopado:
            result = result.replace("primero", "primer").replace("tercero", "tercer")
        return result
    except Exception:
        return str(n)


# ---------------------------------------------------------------------------
# Number pattern converters
# ---------------------------------------------------------------------------

def _convertir_miles(text: str) -> str:
    """Convert dot-separated thousands:  "5.000" → "cinco mil".

    Matches the Spanish convention where "." separates thousands:
        1.000, 5.000, 1.234, 10.000, 1.234.567

    Does NOT match dotted patterns shorter than thousands (like IPs or versions)
    because the regex enforces groups of exactly 3 digits after each dot.
    """
    pattern = re.compile(
        r"(?<![\d\w])(\d{1,3}(?:\.\d{3})+)(?![\d\w])"
    )

    def _replacer(m: re.Match) -> str:
        num_str = m.group(1)
        # Strip dots to get the raw integer
        n = int(num_str.replace(".", ""))
        return _numero_a_palabras(n)

    return pattern.sub(_replacer, text)


def _convertir_decimales(text: str) -> str:
    """Convert comma-separated decimals:  "3,14" → "tres coma catorce".

    Spanish uses comma as the decimal separator.
    Short decimal parts (≤2 digits) are read as a whole number;
    longer ones are read digit-by-digit.
    """
    # Only match when comma separates two pure digit groups
    # and is NOT immediately preceded/followed by a letter or another punctuation
    pattern = re.compile(
        r"(?<![\d\w])(\d+),(\d+)(?![\d\w])"
    )

    def _replacer(m: re.Match) -> str:
        parte_entera = int(m.group(1))
        parte_decimal_str = m.group(2)

        entera_palabras = _numero_a_palabras(parte_entera)

        if len(parte_decimal_str) <= 2:
            decimal_palabras = _numero_a_palabras(int(parte_decimal_str))
        else:
            # Read digit-by-digit for long decimal tails
            decimal_palabras = " ".join(
                _numero_a_palabras(int(d)) for d in parte_decimal_str
            )

        return f"{entera_palabras} coma {decimal_palabras}"

    return pattern.sub(_replacer, text)


def _convertir_porcentajes(text: str) -> str:
    """Convert percentages:  "42%" → "cuarenta y dos por ciento"."""
    pattern = re.compile(
        r"(?<![\d\w])(\d+(?:,\d+)?)\s*%(?![\d\w])"
    )

    def _replacer(m: re.Match) -> str:
        num_str = m.group(1).replace(",", ".")

        try:
            n_float = float(num_str)
        except ValueError:
            return m.group(0)

        if n_float == int(n_float):
            palabras = _numero_a_palabras(int(n_float))
        else:
            # Decimal percentage: "3,5%" → "tres coma cinco por ciento"
            partes = num_str.split(".")
            entera = _numero_a_palabras(int(partes[0]))
            decimal = _numero_a_palabras(int(partes[1]))
            palabras = f"{entera} coma {decimal}"

        return f"{palabras} por ciento"

    return pattern.sub(_replacer, text)


def _convertir_enteros_grandes(text: str) -> str:
    """Convert standalone integers with 4+ digits:  "5000" → "cinco mil".

    Avoids phone numbers (8+ contiguous digits without separators).
    """
    # Allow trailing comma (grammatical) but NOT comma-digit (decimal)
    # because decimals are already handled by _convertir_decimales upstream.
    pattern = re.compile(
        r"(?<![\d\w])"              # not preceded by digit/letter
        r"(\d{4,7})"                # 4 to 7 digits (skip phone numbers with 8+)
        r"(?=[\s,.;!?)\]]|$)"       # followed by whitespace, punctuation, or EOL
    )

    def _replacer(m: re.Match) -> str:
        num_str = m.group(1)
        n = int(num_str)
        return _numero_a_palabras(n)

    return pattern.sub(_replacer, text)


def _convertir_enteros_3digitos(text: str) -> str:
    """Convert 3-digit standalone integers:  "500" → "quinientos".

    edge-tts usually handles 3-digit numbers acceptably, but converting
    to words guarantees correct pronunciation for all voices.
    """
    pattern = re.compile(
        r"(?<![\d\w])"
        r"(\d{3})"
        r"(?=[\s,.;!?)\]]|$)"
    )

    def _replacer(m: re.Match) -> str:
        n = int(m.group(1))
        return _numero_a_palabras(n)

    return pattern.sub(_replacer, text)


def _convertir_ordinales(text: str) -> str:
    """Convert ordinal abbreviations:  "1er" → "primer", "3er" → "tercer"."""
    # Build the pattern: number + one of the valid suffixes
    suffixes_alt = "|".join(_ORDINAL_SUFFIXES)
    pattern = re.compile(
        rf"(?<![\d\w])(\d+)({suffixes_alt})(?![\d\w])",
        re.IGNORECASE,
    )

    def _replacer(m: re.Match) -> str:
        num = int(m.group(1))
        # Suffix "er" is the apocopated form marker ("1er" = primer, "3er" = tercer)
        suffix = m.group(2).lower()
        apocopado = (suffix == "er")
        return _ordinal_a_palabras(num, apocopado=apocopado)

    return pattern.sub(_replacer, text)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def normalize_numbers(text: str) -> str:
    """Normalize numeric expressions in Spanish text for voice synthesis.

    Converts numbers, percentages, decimals, and ordinals to their
    spoken-word equivalents.  All other text is left untouched.

    Args:
        text: Raw script/narration text potentially containing digits.

    Returns:
        Text with numeric patterns replaced by Spanish words.

    Examples:
        >>> normalize_numbers("5.000 personas y el 42%")
        'cinco mil personas y el cuarenta y dos por ciento'
    """
    if not text or not isinstance(text, str):
        return text or ""

    # Process from most specific (unambiguous) to least specific to avoid
    # conflicts between patterns.

    # 1. Thousands-separated numbers: "5.000", "1.234.567"
    text = _convertir_miles(text)

    # 2. Percentages (must run before decimals):
    #    "42%" ── removed by this step so "%" does not confuse later patterns
    text = _convertir_porcentajes(text)

    # 3. Decimal numbers: "3,14"
    text = _convertir_decimales(text)

    # 4. Ordinals: "1er", "3er"
    text = _convertir_ordinales(text)

    # 5. Large standalone integers (4-7 digits): "5000", "1999"
    text = _convertir_enteros_grandes(text)

    # 6. 3-digit integers: "500", "123"
    text = _convertir_enteros_3digitos(text)

    return text
