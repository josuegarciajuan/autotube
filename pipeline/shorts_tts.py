"""Segmented TTS synthesis for YouTube Shorts.

Synthesizes each narrative block separately with per-block voice
settings, then concatenates into a single audio file.  Ensures
the total duration fits within the 60-second Shorts limit and
that no block is silently truncated.

Replaces the monolithic ``edge-tts`` CLI call that caused stories
to be cut off mid-phrase when the text was too long.
"""

import asyncio
import logging
import os
import threading
import tempfile
from pathlib import Path
from typing import Optional

import edge_tts
from pydub import AudioSegment

logger = logging.getLogger(__name__)


def _run_async_in_thread(coro):
    """Run an async coroutine in a dedicated thread with its own event loop.

    This is safe to call from inside a running event loop (e.g. FastAPI/uvicorn)
    because it creates a fresh event loop in a separate OS thread.
    """
    result_container = {}
    error_container = {}

    def _target():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            result_container["value"] = loop.run_until_complete(coro)
        except Exception as e:
            error_container["error"] = e
        finally:
            loop.close()

    thread = threading.Thread(target=_target, daemon=True)
    thread.start()
    thread.join()

    if "error" in error_container:
        raise error_container["error"]
    return result_container["value"]

# ── constants ──────────────────────────────────────────────────

TICK_TO_MS = 10000                # 100-nanosecond ticks → milliseconds
SHORTS_MAX_DURATION_SEC = 55.0    # YouTube Shorts max = 60 s; leave 5 s buffer
MIN_WORD_COUNT = 35               # minimum words for a coherent Short (50→35: LLM struggles with 5-block format)
MAX_WORD_COUNT = 170              # hard cap to prevent over-long audio (≈55 s @ -10% rate)
BLOCK_PAUSE_MS = 350              # silence between narrative blocks (human rhythm)

# Fallback per-block voice settings when channel config has none
_DEFAULT_RATES: dict[str, str] = {
    "hook":        "-5%",
    "desarrollo1": "-10%",
    "desarrollo2": "-10%",
    "climax":      "-15%",
    "cierre":      "-5%",
}
_DEFAULT_PITCHES: dict[str, str] = {
    "climax": "+2Hz",
}


# ── helpers ────────────────────────────────────────────────────


def _ms_to_srt_time(ms: float) -> str:
    hours   = int(ms // 3_600_000)
    minutes = int((ms % 3_600_000) // 60_000)
    seconds = int((ms % 60_000) // 1000)
    millis  = int(ms % 1000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{millis:03d}"


def _timestamps_to_srt(timestamps: list[dict]) -> str:
    """Convert word-level timestamps into SRT subtitle format."""
    if not timestamps:
        return ""

    blocks: list[dict] = []
    current_words   = [timestamps[0]["word"]]
    current_start   = timestamps[0]["start_ms"]
    current_end     = timestamps[0]["end_ms"]

    for ts in timestamps[1:]:
        gap = ts["start_ms"] - current_end
        if gap < 200 and len(" ".join(current_words)) < 42:
            current_words.append(ts["word"])
            current_end = ts["end_ms"]
        else:
            blocks.append({
                "start_ms": current_start,
                "end_ms":   current_end,
                "text":     " ".join(current_words).strip(),
            })
            current_words = [ts["word"]]
            current_start = ts["start_ms"]
            current_end   = ts["end_ms"]

    if current_words:
        blocks.append({
            "start_ms": current_start,
            "end_ms":   current_end,
            "text":     " ".join(current_words).strip(),
        })

    lines: list[str] = []
    for i, b in enumerate(blocks, 1):
        lines.append(str(i))
        lines.append(f"{_ms_to_srt_time(b['start_ms'])} --> "
                      f"{_ms_to_srt_time(b['end_ms'])}")
        lines.append(b["text"])
        lines.append("")

    return "\n".join(lines)


def _block_voice_params(
    block_type: str,
    ch_config,
) -> tuple[str, str]:
    """Return (rate, pitch) for a block type from TTS_STRATEGY config.

    Priority:
    1. ``TTS_STRATEGY.rate_{tipo}`` / ``pitch_{tipo}``
    2. ``TTS_STRATEGY.rate_base`` / ``pitch_base``
    3. ``_DEFAULT_RATES`` / ``_DEFAULT_PITCHES``
    """
    import re
    strategy = getattr(ch_config, "TTS_STRATEGY", None) or {}

    # Rate
    rate = strategy.get(f"rate_{block_type}")
    if rate is None:
        # Try stem (strip trailing digits, e.g. desarrollo1 → desarrollo)
        stem = re.sub(r'\d+$', '', block_type)
        if stem != block_type:
            rate = strategy.get(f"rate_{stem}")
    if rate is None:
        rate = strategy.get("rate_base")
    if rate is None:
        rate = _DEFAULT_RATES.get(block_type, "-10%")

    # Pitch
    pitch = strategy.get(f"pitch_{block_type}")
    if pitch is None:
        stem = re.sub(r'\d+$', '', block_type)
        if stem != block_type:
            pitch = strategy.get(f"pitch_{stem}")
    if pitch is None:
        pitch = strategy.get("pitch_base")
    if pitch is None:
        pitch = _DEFAULT_PITCHES.get(block_type, "+0Hz")

    return rate, pitch


async def _synthesize_block(
    text: str,
    voice: str,
    rate: str,
    pitch: str,
) -> tuple[list[bytes], list[dict]]:
    """Synthesise a single block via edge-tts WebSocket.

    Returns:
        (audio_chunks, word_timestamps) where timestamps are
        relative to the start of *this* block.
    """
    communicate = edge_tts.Communicate(
        text=text,
        voice=voice,
        rate=rate,
        pitch=pitch,
        boundary="WordBoundary",
    )

    audio_chunks: list[bytes] = []
    timestamps: list[dict]   = []

    async for chunk in communicate.stream():
        chunk_type = chunk.get("type", "")

        if chunk_type == "audio":
            audio_chunks.append(chunk["data"])

        elif chunk_type == "WordBoundary":
            offset_tick   = chunk.get("offset", 0)
            duration_tick = chunk.get("duration", 0)
            word_text     = str(chunk.get("text", ""))

            start_ms = offset_tick / TICK_TO_MS
            end_ms   = (offset_tick + duration_tick) / TICK_TO_MS
            dur_ms   = duration_tick / TICK_TO_MS

            timestamps.append({
                "word":        word_text,
                "start_ms":    round(start_ms, 1),
                "end_ms":      round(end_ms, 1),
                "duration_ms": round(dur_ms, 1),
            })

    return audio_chunks, timestamps


# ── public API ─────────────────────────────────────────────────


def validate_short_script(script: dict) -> list[str]:
    """Verify that a Short script JSON is structurally sound.

    Returns a list of error messages (empty = valid).
    """
    errors: list[str] = []
    bloques = script.get("bloques", [])

    if not bloques:
        errors.append("No blocks in script")
        return errors

    expected = ["hook", "desarrollo1", "desarrollo2", "climax", "cierre"]
    found    = [b.get("tipo") for b in bloques]

    for t in expected:
        if t not in found:
            errors.append(f"Missing block: '{t}'")

    if found and found[-1] != "cierre":
        errors.append(f"Last block must be 'cierre', got '{found[-1]}'")

    for b in bloques:
        tipo  = b.get("tipo", "?")
        texto = b.get("texto", "")
        if not texto.strip():
            errors.append(f"Block '{tipo}' has empty text")

    total_words = len(
        " ".join(b.get("texto", "") for b in bloques).split()
    )
    if total_words < MIN_WORD_COUNT:
        errors.append(f"Script too short: {total_words} words (min {MIN_WORD_COUNT})")
    if total_words > MAX_WORD_COUNT:
        errors.append(f"Script too long: {total_words} words (max {MAX_WORD_COUNT})")

    return errors


def synthesize_shorts_blocks(
    bloques: list[dict],
    ch_config,
    output_audio_path: Path,
    output_srt_path: Path,
    voice: Optional[str] = None,
    max_duration_sec: float = SHORTS_MAX_DURATION_SEC,
) -> dict:
    """Synthesise TTS for a Short block-by-block.

    Each block gets its own ``rate`` and ``pitch`` from the
    channel's ``TTS_STRATEGY`` so the hook sounds energetic,
    the climax is dramatic, and the cierre brings closure.

    Args:
        bloques:             [{"tipo": "hook", "texto": "..."}, ...]
        ch_config:           Channel config module / namespace.
        output_audio_path:   Where to write the concatenated MP3.
        output_srt_path:     Where to write the merged SRT.
        voice:               edge-tts voice ID. If None, read from channel TTS config.
        max_duration_sec:    Hard limit on total audio length.

    Returns:
        ``{"audio_path", "srt_path", "duration_sec", "timestamps",
           "word_count"}``

    Raises:
        RuntimeError: If no blocks produced audio OR total duration
                       exceeds ``max_duration_sec``.
    """
    output_audio_path = Path(output_audio_path)
    output_srt_path   = Path(output_srt_path)
    output_audio_path.parent.mkdir(parents=True, exist_ok=True)

    # ── Dispatch: Kokoro vs edge-tts based on channel config ──
    from config.voice_resolver import resolve_channel_voice, build_tts_engine
    resolved = resolve_channel_voice(ch_config)

    if resolved["engine"] == "kokoro":
        # Use Kokoro segmented synthesis (same API as body narration)
        from pipeline.kokoro_tts import KokoroTTSEngine
        engine = build_tts_engine(ch_config)
        logger.info("🎙️ Kokoro Short TTS: %d blocks, voice=%s", len(bloques), resolved["voice"])
        audio_path_str, all_timestamps = engine.generate_segmented(bloques, output_path=str(output_audio_path))
        import shutil
        shutil.move(audio_path_str, str(output_audio_path))
        srt_content = _timestamps_to_srt(all_timestamps)
        output_srt_path.write_text(srt_content, encoding="utf-8")
        # Get duration from audio file
        dur = len(AudioSegment.from_mp3(str(output_audio_path))) / 1000.0
        total_words = len(all_timestamps)
        if dur > max_duration_sec:
            # Unload model before raising to prevent memory leak on error path
            engine.unload()
            raise RuntimeError(
                f"Short audio too long: {dur:.1f}s exceeds maximum {max_duration_sec}s."
            )
        logger.info("✅ Kokoro Short TTS done: %d words, %.1fs, %d blocks", total_words, dur, len(bloques))
        # CRITICAL: Unload the Kokoro pipeline to prevent ~1-2 GB RAM leak per Short.
        # Without this, repeated Short generations accumulate model weights in memory.
        engine.unload()
        return {
            "audio_path": str(output_audio_path),
            "srt_path": str(output_srt_path),
            "duration_sec": dur,
            "timestamps": all_timestamps,
            "word_count": total_words,
        }

    # ── Edge-tts path (existing) ──────────────────────────────

    # Use resolved voice (honours panel selection)
    if voice is None:
        voice = resolved["voice"]

    # Filter out empty blocks
    valid = [b for b in bloques if b.get("texto", "").strip()]
    if not valid:
        raise RuntimeError("No blocks with text to synthesise")

    temp_files: list[str]       = []
    all_timestamps: list[dict]  = []
    cumulative_offset_ms: float = 0.0

    logger.info("🎙️ Segmented Short TTS: %d blocks → %s",
                len(valid), output_audio_path)

    for i, block in enumerate(valid):
        block_type = block.get("tipo", "desarrollo")
        block_text = block.get("texto", "").strip()
        # Normalize numbers for natural TTS pronunciation
        from pipeline.text_normalizer import normalize_numbers
        block_text = normalize_numbers(block_text)
        rate, pitch = _block_voice_params(block_type, ch_config)

        logger.info("  [%d/%d] %s | rate=%s pitch=%s | %d chars",
                     i + 1, len(valid), block_type, rate, pitch,
                     len(block_text))

        # --- attempt synthesis with per-block params ---
        audio_chunks: list[bytes] = []
        word_ts:     list[dict]   = []

        try:
            audio_chunks, word_ts = _run_async_in_thread(
                _synthesize_block(block_text, voice, rate, pitch)
            )
        except Exception as exc:
            logger.warning(
                "Block [%s] failed with custom params: %s — "
                "retrying with defaults", block_type, exc
            )
            try:
                audio_chunks, word_ts = _run_async_in_thread(
                    _synthesize_block(block_text, voice, "-10%", "+0Hz")
                )
            except Exception as exc2:
                logger.error(
                    "Block [%s] retry also failed: %s — skipping",
                    block_type, exc2
                )
                continue

        if not audio_chunks:
            logger.warning("Block [%s] produced no audio — skipping", block_type)
            continue

        # Save block audio to temp file
        tmp = tempfile.NamedTemporaryFile(suffix=".mp3", delete=False)
        tmp.write(b"".join(audio_chunks))
        tmp.close()
        temp_files.append(tmp.name)

        # Offset timestamps and accumulate
        for ts in word_ts:
            ts["start_ms"] = round(ts["start_ms"] + cumulative_offset_ms, 1)
            ts["end_ms"]   = round(ts["end_ms"]   + cumulative_offset_ms, 1)
        all_timestamps.extend(word_ts)

        if word_ts:
            cumulative_offset_ms = word_ts[-1]["end_ms"]
            # Inter-block pause: offset subsequent blocks' timestamps
            # so SRT subtitles reflect the silence between narrative blocks
            if i < len(valid) - 1:
                cumulative_offset_ms += BLOCK_PAUSE_MS

    if not temp_files:
        raise RuntimeError("No blocks produced audio — cannot create Short")

    # ── Concatenate all MP3 segments ──────────────────────────
    logger.info("Concatenating %d audio segments with %dms inter-block pauses…",
                len(temp_files), BLOCK_PAUSE_MS)
    combined = AudioSegment.empty()
    for i, tmp_path in enumerate(temp_files):
        combined += AudioSegment.from_mp3(tmp_path)
        # Insert silence between blocks (not after the last one)
        if i < len(temp_files) - 1:
            combined += AudioSegment.silent(duration=BLOCK_PAUSE_MS)

    duration_sec = len(combined) / 1000.0
    combined.export(str(output_audio_path), format="mp3", bitrate="192k")
    logger.info("Exported: %s (%.1f s)", output_audio_path, duration_sec)

    # Clean temp files
    for tmp_path in temp_files:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass

    # ── Guard: max duration ───────────────────────────────────
    if duration_sec > max_duration_sec:
        raise RuntimeError(
            f"Short audio too long: {duration_sec:.1f}s exceeds "
            f"maximum {max_duration_sec}s. Regenerate with shorter script."
        )

    # ── Write SRT ─────────────────────────────────────────────
    srt_content = _timestamps_to_srt(all_timestamps)
    output_srt_path.write_text(srt_content, encoding="utf-8")

    total_words = len(all_timestamps)
    logger.info("✅ Short TTS done: %d words, %.1f s, %d blocks",
                total_words, duration_sec, len(temp_files))

    return {
        "audio_path":   str(output_audio_path),
        "srt_path":     str(output_srt_path),
        "duration_sec": duration_sec,
        "timestamps":   all_timestamps,
        "word_count":   total_words,
    }
