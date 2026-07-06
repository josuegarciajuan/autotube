"""Unit tests for config/voice_timing.py.

Run:  python3 -m pytest tests/test_voice_timing.py -v
"""

import sys
sys.path.insert(0, "/root/autotube")

import pytest
from config.voice_timing import (
    voice_speed_factor,
    words_per_minute_real,
    words_for_duration,
    duration_for_words,
    SPEED_COLCHON,
    BASE_WORDS_PER_MINUTE,
    UNKNOWN_RATE_FALLBACK,
)


# ── Mock configs ──────────────────────────────────────────────

class EdgeTTSConfig:
    """Simulates canal2: edge-tts with rate="-10%"."""
    TTS_STRATEGY = {"rate_base": "-10%"}
    VOICE_RATE = "-10%"


class EdgeTTSSlowConfig:
    """Slow edge-tts: rate="+5%"."""
    TTS_STRATEGY = {"rate_base": "+5%"}


class KokoroConfig:
    """Kokoro: float speed multiplier 0.85 (slower speech)."""
    TTS_STRATEGY = {"rate_base": 0.85}


class NeutralConfig:
    """Neutral / default rate."""
    TTS_STRATEGY = {"rate_base": "default"}


class NoConfig:
    """No rate configured at all — fallback to 1.0."""
    pass


# ── voice_speed_factor ────────────────────────────────────────

def test_edge_tts_negative_rate():
    """-10% rate → factor 1.10 (faster speech, more words/min)."""
    assert voice_speed_factor(EdgeTTSConfig) == pytest.approx(1.10)


def test_edge_tts_positive_rate():
    """+5% rate → factor 0.95 (slower speech, fewer words/min)."""
    assert voice_speed_factor(EdgeTTSSlowConfig) == pytest.approx(0.95)


def test_kokoro_rate():
    """Kokoro speed 0.85 → factor 0.85 (direct multiplier, 85% of neutral)."""
    assert voice_speed_factor(KokoroConfig) == pytest.approx(0.85)


def test_neutral_rate():
    assert voice_speed_factor(NeutralConfig) == 1.0


def test_no_config():
    assert voice_speed_factor(NoConfig) == UNKNOWN_RATE_FALLBACK


# ── words_per_minute_real ─────────────────────────────────────

def test_wpm_edge_tts_fast():
    """150 * 1.10 = 165 wpm."""
    assert words_per_minute_real(EdgeTTSConfig) == pytest.approx(165.0)


def test_wpm_edge_tts_slow():
    """150 * 0.95 = 142.5 wpm."""
    assert words_per_minute_real(EdgeTTSSlowConfig) == pytest.approx(142.5)


# ── words_for_duration ────────────────────────────────────────

def test_words_14min_edge_tts():
    """14 min * 165 wpm * 1.20 colchón = 2772."""
    assert words_for_duration(EdgeTTSConfig, 14.0) == 2772


def test_words_10min_edge_tts():
    """10 min * 150 * 1.10 * 1.20 = 1980."""
    assert words_for_duration(EdgeTTSConfig, 10.0) == 1980


def test_words_minimum():
    """Even 0.1 minutes should return at least 50 words."""
    assert words_for_duration(EdgeTTSConfig, 0.1) >= 50


# ── duration_for_words ────────────────────────────────────────

def test_duration_2772_words():
    """2772 / (165 * 1.20) = 14.0 minutes."""
    assert duration_for_words(EdgeTTSConfig, 2772) == 14.0


def test_duration_500_words():
    """500 / (165 * 1.20) ≈ 2.5 minutes."""
    dur = duration_for_words(EdgeTTSConfig, 500)
    assert 2.4 <= dur <= 2.6


def test_roundtrip():
    """words_for_duration(config, 10) → duration_for_words(config, result) ≈ 10."""
    words = words_for_duration(EdgeTTSConfig, 10.0)
    dur = duration_for_words(EdgeTTSConfig, words)
    assert abs(dur - 10.0) < 1.0  # within 1 minute of original


# ── Edge cases ────────────────────────────────────────────────

def test_zero_duration():
    assert words_for_duration(EdgeTTSConfig, 0.0) == 50


def test_zero_words():
    assert duration_for_words(EdgeTTSConfig, 0) == 0.0


def test_colchon_applied():
    """SPEED_COLCHON 1.20 means 20% extra words for safety."""
    raw_words = 14.0 * BASE_WORDS_PER_MINUTE * voice_speed_factor(EdgeTTSConfig)
    with_colchon = words_for_duration(EdgeTTSConfig, 14.0)
    assert with_colchon == int(raw_words * SPEED_COLCHON)

# ── Ampliación: más tasas y edge cases ──────────────────────

class EdgeTTS_8:
    """canal3: rate="-8%"."""
    TTS_STRATEGY = {"rate_base": "-8%"}

class EdgeTTSNoPct:
    TTS_STRATEGY = {"rate_base": "-10"}

class EdgeTTSSpaces:
    TTS_STRATEGY = {"rate_base": " -10 % "}

class EdgeTTSGarbage:
    TTS_STRATEGY = {"rate_base": "rápido"}

class EdgeTTSExtremeNeg:
    TTS_STRATEGY = {"rate_base": "-50%"}

class EdgeTTSExtremePos:
    TTS_STRATEGY = {"rate_base": "+30%"}

class KokoroSlow:
    TTS_STRATEGY = {"rate_base": 0.85}

class KokoroFast:
    TTS_STRATEGY = {"rate_base": 1.5}

class KokoroClampLow:
    TTS_STRATEGY = {"rate_base": 0.2}

class KokoroClampHigh:
    TTS_STRATEGY = {"rate_base": 3.0}

class LegacyVoiceRate:
    VOICE_RATE = "-10%"

class TTSNone:
    TTS_STRATEGY = None


def test_edge_tts_canal3_rate():
    assert voice_speed_factor(EdgeTTS_8) == pytest.approx(1.08)


def test_edge_tts_no_percent_sign():
    assert voice_speed_factor(EdgeTTSNoPct) == pytest.approx(1.10)

def test_edge_tts_spaces():
    assert voice_speed_factor(EdgeTTSSpaces) == pytest.approx(1.10)

def test_edge_tts_garbage():
    assert voice_speed_factor(EdgeTTSGarbage) == pytest.approx(1.0)

def test_edge_tts_extreme_neg():
    assert voice_speed_factor(EdgeTTSExtremeNeg) == pytest.approx(1.50)

def test_edge_tts_extreme_pos():
    assert voice_speed_factor(EdgeTTSExtremePos) == pytest.approx(0.70)

def test_kokoro_1_5_fast():
    """Kokoro speed 1.5 → factor 1.5 (direct multiplier, 150% of neutral)."""
    assert voice_speed_factor(KokoroFast) == pytest.approx(1.5)

def test_kokoro_clamped_low():
    """Kokoro speed 0.2 → clamped to min 0.5."""
    assert voice_speed_factor(KokoroClampLow) == pytest.approx(0.5)

def test_kokoro_clamped_high():
    """Kokoro speed 3.0 → clamped to max 2.0."""
    assert voice_speed_factor(KokoroClampHigh) == pytest.approx(2.0)

def test_legacy_voice_rate_fallback():
    assert voice_speed_factor(LegacyVoiceRate) == pytest.approx(1.10)

def test_tts_strategy_none():
    assert voice_speed_factor(TTSNone) == pytest.approx(1.0)

def test_wpm_canal3():
    assert words_per_minute_real(EdgeTTS_8) == pytest.approx(162.0)


