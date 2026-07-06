"""Voice-aware duration ↔ word-count conversion.

Eliminates the hardcoded "150 words per minute" assumption by reading
the actual voice rate configured for each channel.  Because edge-tts
uses percentage strings (e.g. "-8%") while Kokoro uses float multipliers
(e.g. 0.85), this module normalises everything into a single speed factor.

Usage
-----
    from config.voice_timing import words_for_duration, duration_for_words

    target = words_for_duration(canal_config, duration_minutes=12)
    # → ~1950 palabras para canal2 (-10% rate + 20% colchón)

    dur   = duration_for_words(canal_config, word_count=1950)
    # → ~12.0 minutos
"""

from __future__ import annotations

import math
from typing import Any

# ── Constants ──────────────────────────────────────────────────
BASE_WORDS_PER_MINUTE = 150.0   # neutral-paced Spanish narration
SPEED_COLCHON = 1.05            # 5 % cushion — tight enough to avoid overshoot
UNKNOWN_RATE_FALLBACK = 1.0     # assume neutral pace if rate is unreadable


# ── Public API ─────────────────────────────────────────────────

def voice_speed_factor(canal_config: Any) -> float:
    """Return the speed multiplier for the channel's configured voice.

    * edge-tts stores rate as strings like ``"-10%"`` or ``"+5%"``.
    * Kokoro stores rate as a float speed multiplier (e.g. 0.85 is slower).
    * A factor > 1.0 means the voice speaks *faster* than neutral.
    """
    # Try TTS_STRATEGY dict first (per-channel config bridge format)
    tts = getattr(canal_config, "TTS_STRATEGY", None)
    if isinstance(tts, dict):
        rate = tts.get("rate_base") or tts.get("rate_primary")
    else:
        rate = None

    # Fall back to legacy VOICE_RATE attribute
    if rate is None:
        rate = getattr(canal_config, "VOICE_RATE", None)

    if rate is None:
        return UNKNOWN_RATE_FALLBACK

    # Kokoro-style float multiplier
    # speed=0.94 means 94% of normal → fewer WPM → factor=0.94
    # speed=1.06 means 106% of normal → more WPM → factor=1.06
    if isinstance(rate, (int, float)):
        clamped = max(0.5, min(2.0, float(rate)))
        return clamped

    # edge-tts percentage string: "-10%", "+5%", "default"
    rate_str = str(rate).strip()
    if rate_str.lower() == "default" or rate_str == "0%":
        return 1.0

    # Strip the percent sign and parse
    try:
        pct = float(rate_str.replace("%", "").strip())
    except (ValueError, TypeError):
        return UNKNOWN_RATE_FALLBACK

    # Negative percentage → faster speech (more words / minute)
    #   -10 % → 1.10 × words
    #   +5  % → 0.95 × words
    return 1.0 - (pct / 100.0)


def words_per_minute_real(canal_config: Any) -> float:
    """Real words-per-minute for this channel's voice.

    Example
    -------
    canal2: rate = "-10%" → factor = 1.10 → wpm = 150 × 1.10 = 165
    canal3: rate = "-8%"  → factor = 1.08 → wpm = 150 × 1.08 = 162
    """
    return BASE_WORDS_PER_MINUTE * voice_speed_factor(canal_config)


def words_for_duration(canal_config: Any, duration_minutes: float) -> int:
    """How many words to generate to fill *duration_minutes*.

    Includes a 20 % cushion so that the final video is more likely
    to be *longer* rather than shorter than the target.

    Example
    -------
    canal2, 14 minutes → 14 × 165 × 1.20 = 2772 palabras
    """
    real_wpm = words_per_minute_real(canal_config)
    raw = duration_minutes * real_wpm * SPEED_COLCHON
    return max(50, int(math.ceil(raw)))


def duration_for_words(canal_config: Any, word_count: int) -> float:
    """Estimated video narration duration (minutes) for *word_count*.

    Does NOT include pausas, intro or outro — those are added by the
    video editor.

    Example
    -------
    canal2, 2772 palabras → 2772 / (165 × 1.20) = 14.0 minutos
    """
    real_wpm = words_per_minute_real(canal_config)
    if real_wpm <= 0:
        return 0.0
    raw = word_count / (real_wpm * SPEED_COLCHON)
    return round(raw, 1)
