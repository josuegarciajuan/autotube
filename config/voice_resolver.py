"""Voice resolver — single source of truth for channel TTS voice and engine.

Replicates the panel's voice selection logic (VoiceSelector.tsx:120-130):
  - engine = TTS_ENGINE (default "edgetts")
  - kokoro → KOKORO_VOICE
  - edgetts → VOICE_ID (user-selected), with TTS_STRATEGY.voice_primary as fallback

Provides a factory ``build_tts_engine(config)`` that returns the correct
engine instance for any given channel config (module, dict, or SimpleNamespace).
"""

import logging
from typing import Optional, Union, Any

logger = logging.getLogger(__name__)

# ── Helper: normalise any config object ──────────────────────

def _get(cfg: Any, key: str, default=None) -> Any:
    """Extract a value from a dict, SimpleNamespace, or module."""
    if isinstance(cfg, dict):
        return cfg.get(key, default)
    return getattr(cfg, key, default)


# ── Public API ───────────────────────────────────────────────

def resolve_channel_voice(config: Any) -> dict:
    """Resolve the actual voice settings for a channel.

    Returns a dict with:
      - engine: "kokoro" or "edgetts"
      - voice: voice ID string (edge voice id or kokoro voice name)
      - rate: base rate string (edge) or None (kokoro uses block_speeds)
      - pitch: base pitch string (edge) or None
      - volume: volume string
      - block_speeds: dict (kokoro) or None (edge)
      - tts_strategy: full TTS_STRATEGY dict for per-block rate/pitch
    """
    engine = _get(config, "TTS_ENGINE", "edgetts")
    tts_strategy = _get(config, "TTS_STRATEGY", {}) or {}

    if engine == "kokoro":
        voice = _get(config, "KOKORO_VOICE", "em_santa")
        # Build block_speeds from TTS_STRATEGY rate values
        block_speeds = _kokoro_block_speeds(tts_strategy)
        return {
            "engine": "kokoro",
            "voice": voice,
            "rate": None,
            "pitch": None,
            "volume": _get(config, "VOICE_VOLUME", "+0%"),
            "block_speeds": block_speeds,
            "tts_strategy": tts_strategy,
        }
    else:
        # edge-tts: use VOICE_ID (panel selection) with voice_primary as fallback
        voice = _get(config, "VOICE_ID")
        if not voice:
            voice = tts_strategy.get("voice_primary", "es-ES-AlvaroNeural")
        rate = tts_strategy.get("rate_base", _get(config, "VOICE_RATE", "+5%"))
        pitch = tts_strategy.get("pitch_base", _get(config, "VOICE_PITCH", "+0Hz"))
        volume = _get(config, "VOICE_VOLUME", "+0%")
        return {
            "engine": "edgetts",
            "voice": voice,
            "rate": rate,
            "pitch": pitch,
            "volume": volume,
            "block_speeds": None,
            "tts_strategy": tts_strategy,
        }


def build_tts_engine(config: Any) -> Any:
    """Build the TTS engine (KokoroTTSEngine or TTSEngine) for a channel config.

    Uses resolve_channel_voice internally and returns a fully configured engine.
    """
    resolved = resolve_channel_voice(config)
    engine_type = resolved["engine"]

    if engine_type == "kokoro":
        from pipeline.kokoro_tts import KokoroTTSEngine
        # Resolve pause_between_blocks — try top-level key first,
        # then fall back to TTS_STRATEGY dict.
        pause_between = _get(config, "KOKORO_PAUSE_BETWEEN_BLOCKS", None)
        if pause_between is None:
            tts_strategy = _get(config, "TTS_STRATEGY", {})
            if isinstance(tts_strategy, dict):
                pause_between = tts_strategy.get("pause_between_blocks", 0.7)
            else:
                pause_between = 0.7
        voice_config = {
            "kokoro_voice": resolved["voice"],
            "block_speeds": resolved["block_speeds"],
            "pause_between_blocks": pause_between,
            # Batch unload: reload Kokoro every N blocks to keep RAM low.
            # 0 = disabled (legacy: model stays loaded for all blocks).
            "unload_every_n_blocks": _get(config, "KOKORO_UNLOAD_EVERY_N_BLOCKS", 0),
        }
        logger.info("🔊 TTS engine: Kokoro (voice=%s)", resolved["voice"])
        return KokoroTTSEngine(voice_config)
    else:
        from pipeline.tts_engine import TTSEngine
        voice_config = {
            "voice": resolved["voice"],
            "rate": resolved["rate"],
            "pitch": resolved["pitch"],
            "volume": resolved["volume"],
            "tts_strategy": resolved["tts_strategy"],
        }
        logger.info("🔊 TTS engine: edge-tts (voice=%s, rate=%s)", resolved["voice"], resolved["rate"])
        return TTSEngine(voice_config)


# ── Kokoro block_speeds from edge-tts rate strings ───────────

def _kokoro_block_speeds(tts_strategy: dict) -> dict:
    """Convert edge-tts rate percentages to Kokoro float speed multipliers.

    edge-tts "-10%" → slower speech (165 wpm)
    Kokoro  0.90  → slower speech (90% of neutral)

    Approximation: speed = 1.0 + (rate_pct / 100.0)
    -10% → 0.90,  -5% → 0.95,  +0% → 1.0
    """
    import re
    speeds: dict[str, float] = {}
    for key, val in tts_strategy.items():
        if key.startswith("rate_") and isinstance(val, str):
            block_type = key[5:]  # strip "rate_" prefix
            match = re.match(r'([+-]?\d+)%?', val)
            if match:
                pct = float(match.group(1))
                speeds[block_type] = max(0.5, min(2.0, 1.0 + pct / 100.0))
    # Ensure base speed exists
    if "base" not in speeds:
        speeds["base"] = 0.90  # slightly slower than neutral
    return speeds
