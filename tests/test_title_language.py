from __future__ import annotations
import os, sys
from types import SimpleNamespace
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from pipeline.title_language import detect_language, is_title_language_ok

CFG = SimpleNamespace(TITLE_LANGUAGE_GUARD_ENABLED=True, TITLE_ALLOWED_LANGUAGES=("es",))


def test_detects_english_title():
    assert detect_language("Dreamt the earthquake before it happened: the case that keeps geologists awake") == "en"
    assert not is_title_language_ok(
        "Dreamt the earthquake before it happened", CFG
    )


def test_accepts_spanish_title():
    text = "El sueño que predijo el terremoto antes de que ocurriera"
    assert detect_language(text) == "es"
    assert is_title_language_ok(text, CFG)


def test_short_or_proper_noun_title_is_fail_open():
    assert detect_language("Göbekli Tepe") == "unknown"
    assert is_title_language_ok("Göbekli Tepe", CFG)
