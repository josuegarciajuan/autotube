"""Unit tests for expressive narration (prosodia por tono)."""
import pytest

from config.defaults import PROSODY_PROFILES, TONO_CATALOG
from config.voice_resolver import (
    normalize_tono,
    resolve_prosody,
    rate_to_speed,
    split_block_segments,
    infer_tono_from_text,
)


def _cfg(**over):
    base = {
        "EXPRESSIVE_NARRATION": True,
        "PROSODY_PROFILES": PROSODY_PROFILES,
        "TONO_CATALOG": TONO_CATALOG,
        "TONO_DEFAULT": "neutro",
        "TTS_STRATEGY": {},
        "VOICE_RATE": "+0%",
        "VOICE_PITCH": "+0Hz",
        "VOICE_VOLUME": "+0%",
    }
    base.update(over)
    return base


# ── normalize_tono ────────────────────────────────────────────

def test_normalize_tono_explicit_canonical():
    assert normalize_tono(_cfg(), tono="suspense") == "suspense"


def test_normalize_tono_accent_insensitive():
    assert normalize_tono(_cfg(), tono="tensión") == "tension"
    assert normalize_tono(_cfg(), tono="revelación") == "revelacion"


def test_normalize_tono_from_emotion_keyword():
    assert normalize_tono(_cfg(), emocion="misterio y tensión") == "misterio"
    assert normalize_tono(_cfg(), emocion="asombro absoluto") == "asombro"


def test_normalize_tono_from_tipo_fallback():
    assert normalize_tono(_cfg(), tipo="climax") == "tension"
    assert normalize_tono(_cfg(), tipo="cierre") == "cierre"


def test_normalize_tono_default_when_unknown():
    assert normalize_tono(_cfg(), tono="inventado", emocion="", tipo="") == "neutro"


# ── resolve_prosody ───────────────────────────────────────────

def test_resolve_prosody_expressive_uses_profile():
    pros = resolve_prosody(_cfg(), tono="revelacion")
    assert pros["tono"] == "revelacion"
    assert pros["rate"] == PROSODY_PROFILES["revelacion"]["rate"]
    assert pros["pause_after_ms"] == PROSODY_PROFILES["revelacion"]["pause_after_ms"]


def test_resolve_prosody_disabled_falls_back_to_strategy():
    cfg = _cfg(EXPRESSIVE_NARRATION=False, TTS_STRATEGY={"rate_climax": "-15%", "pitch_climax": "-4Hz"})
    pros = resolve_prosody(cfg, tipo="climax")
    assert pros["tono"] is None
    assert pros["rate"] == "-15%"
    assert pros["pitch"] == "-4Hz"
    assert pros["pause_after_ms"] == 0


# ── split_block_segments ──────────────────────────────────────

def test_split_block_segments_merges_consecutive_tone():
    bloque = {
        "tipo": "desarrollo",
        "texto": "A. B. C.",
        "segmentos": [
            {"texto": "A.", "tono": "suspense"},
            {"texto": "B.", "tono": "revelacion"},
            {"texto": "C.", "tono": "revelacion"},
        ],
    }
    segs = split_block_segments(bloque, _cfg(), max_segments=3, expressive=True)
    assert [s["tono"] for s in segs] == ["suspense", "revelacion"]
    assert segs[1]["texto"] == "B. C."


def test_split_block_segments_caps_max():
    bloque = {
        "tipo": "desarrollo",
        "texto": "A B C D",
        "segmentos": [
            {"texto": "A", "tono": "misterio"},
            {"texto": "B", "tono": "suspense"},
            {"texto": "C", "tono": "revelacion"},
            {"texto": "D", "tono": "tristeza"},
        ],
    }
    segs = split_block_segments(bloque, _cfg(), max_segments=2, expressive=True)
    assert len(segs) == 2
    # First tone kept, the rest merged into the last segment.
    assert segs[0]["texto"] == "A"
    assert segs[-1]["texto"] == "B C D"


def test_split_block_segments_no_segments_uses_block_tone():
    bloque = {"tipo": "desarrollo", "texto": "Solo un tono.", "tono": "misterio"}
    segs = split_block_segments(bloque, _cfg(), max_segments=3, expressive=True)
    assert len(segs) == 1
    assert segs[0]["tono"] == "misterio"
    assert segs[0]["texto"] == "Solo un tono."


# ── infer_tono_from_text (adaptación por contenido) ──────────

def test_infer_tono_from_text_revelacion():
    assert infer_tono_from_text("Y entonces lo descubrió: la verdad era otra.") == "revelacion"


def test_infer_tono_from_text_tension():
    assert infer_tono_from_text("De repente, algo se movió en la oscuridad.") == "tension"


def test_infer_tono_from_text_misterio():
    assert infer_tono_from_text("Un secreto oculto durante generaciones.") == "misterio"


def test_infer_tono_from_text_none_when_neutral():
    assert infer_tono_from_text("El río atraviesa la llanura.") is None


def test_normalize_prefers_explicit_tono_over_text():
    # El texto sugiere tensión, pero el tono explícito manda.
    assert normalize_tono(
        _cfg(), tono="cierre",
        texto="De repente, algo se movió en la oscuridad.",
    ) == "cierre"


def test_normalize_prefers_emocion_over_text():
    assert normalize_tono(
        _cfg(), emocion="asombro",
        texto="De repente, algo se movió en la oscuridad.",
    ) == "asombro"


def test_normalize_uses_text_when_no_tono_or_emocion():
    # Simula un bloque de guion antiguo sin tono ni emoción útil.
    assert normalize_tono(
        _cfg(), tipo="desarrollo",
        texto="Y entonces lo descubrió: la verdad era otra.",
    ) == "revelacion"


def test_resolve_prosody_adaptive_from_text():
    pros = resolve_prosody(_cfg(), tipo="desarrollo",
                           texto="De repente, un grito rompió el silencio.")
    assert pros["tono"] == "tension"
    assert pros["rate"] == PROSODY_PROFILES["tension"]["rate"]


def test_split_block_segments_adapts_old_block_by_text():
    # Bloque legacy: sin tono, sin emoción, sin segmentos.
    bloque = {
        "tipo": "desarrollo",
        "texto": "Y entonces lo descubrió: la verdad era otra.",
    }
    segs = split_block_segments(bloque, _cfg(), max_segments=3, expressive=True)
    assert len(segs) == 1
    assert segs[0]["tono"] == "revelacion"


# ── rate_to_speed ─────────────────────────────────────────────

def test_rate_to_speed_helpers():
    assert rate_to_speed("-18%") == pytest.approx(0.82)
    assert rate_to_speed("+5%") == pytest.approx(1.05)
    assert rate_to_speed(0.8) == pytest.approx(0.8)
    assert rate_to_speed(None, default=0.9) == pytest.approx(0.9)


# ── engines: segment resolution (no network) ──────────────────

def test_edge_engine_segments_expressive():
    from config.voice_resolver import build_tts_engine
    cfg = _cfg(TTS_ENGINE="edgetts", VOICE_ID="es-MX-DaliaNeural")
    engine = build_tts_engine(cfg)
    bloque = {
        "tipo": "climax",
        "texto": "Uno. Dos.",
        "segmentos": [
            {"texto": "Uno.", "tono": "suspense"},
            {"texto": "Dos.", "tono": "revelacion"},
        ],
    }
    segs = engine._segments_for_block(bloque)
    assert [s["tono"] for s in segs] == ["suspense", "revelacion"]


def test_kokoro_engine_segments_expressive():
    from config.voice_resolver import build_tts_engine
    cfg = _cfg(TTS_ENGINE="kokoro", KOKORO_VOICE="em_santa")
    engine = build_tts_engine(cfg)
    assert engine.expressive is True
    segs = engine._segments_for_block({"tipo": "hook", "texto": "Hola.", "tono": "suspense"})
    assert segs[0]["tono"] == "suspense"


# ── voice_timing: expressive averaging ────────────────────────

def test_voice_speed_factor_expressive_average():
    from config.voice_timing import voice_speed_factor
    cfg = _cfg()
    factor = voice_speed_factor(cfg)
    # All profiles are slower than neutral → WPM factor should be > 1.0
    assert factor > 1.0
