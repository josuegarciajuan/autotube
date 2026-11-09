"""Audio integrity helpers shared by TTS engines and renderers.

The historical bug: edge-tts appends ~1 s of silence/encoder padding to every
synthesised block.  Shorts used the last WordBoundary as the cumulative offset
(ignoring that padding), which made the SRT/scene timeline up to 6 s shorter
than the real audio and let ``-shortest`` cut the final narration.  Long-form
concatenated the padding + the expressive pause, producing 1-2 s gaps.

Everything here is defensive: a failure to measure/trim returns the original
audio and only logs at debug level.  Never drop audio to "fix" a duration.
"""

from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path
from typing import Optional

from pydub import AudioSegment
from pydub.silence import detect_nonsilent

logger = logging.getLogger(__name__)


# ── Settings (env > config.settings > default) ───────────────────

def _bool_env(name: str, default: bool = True) -> bool:
    raw = os.getenv(name)
    if raw is None:
        try:
            from config import settings as _settings
            return bool(getattr(_settings, name, default))
        except Exception:  # noqa: BLE001 — never break audio over config
            return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def trim_enabled() -> bool:
    return _bool_env("TTS_TRIM_TRAILING_SILENCE", True)


# ── Duration ─────────────────────────────────────────────────────

def media_duration(path) -> Optional[float]:
    """Return the container duration in seconds (ffprobe, fallback pydub)."""
    if not path:
        return None
    p = Path(str(path))
    if not p.exists():
        return None
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(p)],
            capture_output=True, text=True, timeout=15,
        ).stdout.strip()
        if out:
            return float(out)
    except Exception as exc:  # noqa: BLE001
        logger.debug("ffprobe duration failed for %s: %s", p, exc)
    try:
        return len(AudioSegment.from_file(str(p))) / 1000.0
    except Exception as exc:  # noqa: BLE001
        logger.debug("pydub duration failed for %s: %s", p, exc)
        return None


# ── Trailing-silence trimming ────────────────────────────────────

def trim_trailing_silence(
    audio: AudioSegment,
    *,
    keep_ms: int = 180,
    thresh_offset_db: float = 20.0,
    min_silence_ms: int = 400,
) -> AudioSegment:
    """Trim trailing silence/encoder padding, keeping ``keep_ms`` of tail.

    Conservative by design:
    - If trimming is disabled, the clip is shorter than 300 ms, or no speech
      range is detected, the original clip is returned unchanged.
    - Only trims when it saves more than 100 ms (avoids needless re-encoding).
    """
    if not trim_enabled() or audio is None or len(audio) <= 300:
        return audio
    try:
        ranges = detect_nonsilent(
            audio,
            min_silence_len=max(200, int(min_silence_ms)),
            silence_thresh=audio.dBFS - float(thresh_offset_db),
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("detect_nonsilent failed: %s", exc)
        return audio

    if not ranges:
        return audio
    last_end = int(ranges[-1][1])
    target = min(len(audio), last_end + max(0, int(keep_ms)))
    if target < len(audio) - 100:
        return audio[:target]
    return audio


def load_trimmed_mp3(path, **kwargs) -> AudioSegment:
    """Load an MP3 and trim its trailing silence (best effort)."""
    return trim_trailing_silence(AudioSegment.from_mp3(str(path)), **kwargs)
