"""edge-tts text-to-speech engine for the Autotube pipeline.

Converts script narration text into MP3 audio with word-level
timestamps, JSON metadata, and SRT subtitle files.

v2: Segmented synthesis — each narrative block gets its own voice
    settings (rate/pitch) based on emotional type, producing a
    dynamic, non-monotonous narration.
"""

import asyncio
import concurrent.futures
import json
import logging
import os
import re
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Optional

import edge_tts
from pydub import AudioSegment

from config.settings import AUDIO_DIR

logger = logging.getLogger(__name__)

MAX_RETRIES = 3
RETRY_DELAY_SECONDS = 2.0

# ── Async timeout helper ──────────────────────────────────────
_TTS_TIMEOUT_EXECUTOR = None  # lazy-init thread pool for TTS timeout


def _run_async_with_timeout(coro, timeout: float = 120.0):
    """Run an async coroutine with a timeout using a thread pool.
    
    Prevents the pipeline from hanging indefinitely if edge-tts
    becomes unresponsive.
    """
    global _TTS_TIMEOUT_EXECUTOR
    if _TTS_TIMEOUT_EXECUTOR is None:
        _TTS_TIMEOUT_EXECUTOR = concurrent.futures.ThreadPoolExecutor(
            max_workers=2, thread_name_prefix="tts-timeout"
        )

    def _runner():
        return asyncio.run(coro)

    future = _TTS_TIMEOUT_EXECUTOR.submit(_runner)
    try:
        return future.result(timeout=timeout)
    except concurrent.futures.TimeoutError:
        future.cancel()
        raise TimeoutError(f"TTS operation timed out after {timeout}s")

TICK_TO_MS = 10000  # 100-nanosecond ticks to milliseconds


def _ms_to_srt_time(ms: float) -> str:
    """Convert milliseconds to SRT timestamp format HH:MM:SS,mmm."""
    hours = int(ms // 3_600_000)
    minutes = int((ms % 3_600_000) // 60_000)
    seconds = int((ms % 60_000) // 1000)
    millis = int(ms % 1000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{millis:03d}"


class TTSEngine:
    """Synthesize speech from text using Microsoft Edge TTS via edge-tts.

    Supports word-level alignment timestamps, SRT subtitle generation,
    scene-aware parsing, and segmented synthesis with per-block
    voice settings for dynamic narration.
    """

    def __init__(self, voice_config: Optional[dict] = None):
        """Initialize the TTS engine with voice parameters.

        Args:
            voice_config: Dict with optional keys:
                voice (str): Voice ID, e.g. "es-ES-AlvaroNeural".
                rate (str): Default speaking rate, e.g. "+5%".
                pitch (str): Default pitch adjustment, e.g. "+0Hz".
                volume (str): Volume adjustment, e.g. "+0%".
                tts_strategy (dict): Per-block-type voice config
                    with keys like rate_hook, pitch_hook, etc.
        """
        if voice_config is None:
            voice_config = {}
        self.voice = voice_config.get("voice", "es-ES-AlvaroNeural")
        self.rate = voice_config.get("rate", "+0%")
        self.pitch = voice_config.get("pitch", "+0Hz")
        self.volume = voice_config.get("volume", "+0%")
        self.tts_strategy = voice_config.get("tts_strategy", {})

    def _voice_for_tipo(self, tipo: str) -> tuple[str, str]:
        """Get (rate, pitch) for a block type from TTS_STRATEGY.

        Falls back to self.rate / self.pitch if no strategy defined.
        """
        if not self.tts_strategy:
            return self.rate, self.pitch

        rate_key = f"rate_{tipo}"
        pitch_key = f"pitch_{tipo}"

        rate = self.tts_strategy.get(rate_key)
        if rate is None:
            rate = self.tts_strategy.get("rate_base", self.rate)

        pitch = self.tts_strategy.get(pitch_key)
        if pitch is None:
            pitch = self.tts_strategy.get("pitch_base", self.pitch)

        return rate, pitch

    def _build_communicate(
        self,
        text: str,
        rate: Optional[str] = None,
        pitch: Optional[str] = None,
    ) -> edge_tts.Communicate:
        """Create an edge_tts Communicate instance with voice settings.

        Args:
            text: Text to synthesize.
            rate: Optional rate override for this segment.
            pitch: Optional pitch override for this segment.

        Returns:
            Configured edge_tts.Communicate instance.
        """
        return edge_tts.Communicate(
            text=text,
            voice=self.voice,
            rate=rate if rate is not None else self.rate,
            pitch=pitch if pitch is not None else self.pitch,
            volume=self.volume,
            boundary="WordBoundary",
        )

    @staticmethod
    def _remove_bracketed_marker(text: str, marker: str) -> str:
        """Remove all ``[MARKER: ...]`` sections with proper bracket matching."""
        result: list[str] = []
        i = 0
        start_tag = f"[{marker}:"
        tag_lower = start_tag.lower()
        while i < len(text):
            idx = text.lower().find(tag_lower, i)
            if idx == -1:
                result.append(text[i:])
                break
            result.append(text[i:idx])
            depth = 1
            j = idx + len(start_tag)
            while j < len(text) and depth > 0:
                ch = text[j]
                if ch == '[':
                    depth += 1
                elif ch == ']':
                    depth -= 1
                j += 1
            i = j
        return ''.join(result)

    def _clean_guion(self, text: str) -> str:
        """Strip structural markers from guion text and normalize numbers."""
        cleaned = self._remove_bracketed_marker(text, "ESCENA")
        cleaned = self._remove_bracketed_marker(cleaned, "PAUSA")
        cleaned = re.sub(r'\bNarrador\s*:\s*', '', cleaned, flags=re.IGNORECASE)
        cleaned = cleaned.replace('[', ' ').replace(']', ' ')
        cleaned = re.sub(r'\bESCENA\b', '', cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r'\bPAUSA\b', '', cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r'\s+', ' ', cleaned).strip()
        # Normalize numbers to spoken words for natural TTS pronunciation
        # (e.g. "5.000" → "cinco mil", "42%" → "cuarenta y dos por ciento")
        from pipeline.text_normalizer import normalize_numbers
        cleaned = normalize_numbers(cleaned)
        return cleaned

    # ── Legacy: single-segment synthesis ────────────────────

    def generate(
        self,
        text: str,
        output_path: Optional[str] = None,
    ) -> tuple[str, list[dict]]:
        """Generate TTS audio from text with word-level timestamps (single segment).

        Args:
            text: Narration text to synthesize.
            output_path: Optional base path for output files.

        Returns:
            Tuple of (audio_mp3_path, timestamps_list).
        """
        text = self._clean_guion(text)

        if output_path is None:
            timestamp = int(time.time())
            base_path = str(AUDIO_DIR / f"narration_{timestamp}")
        else:
            base_path = os.path.splitext(output_path)[0]

        audio_path = f"{base_path}.mp3"
        json_path = f"{base_path}_timestamps.json"
        srt_path = f"{base_path}_subtitles.srt"

        os.makedirs(os.path.dirname(base_path) or ".", exist_ok=True)

        last_error = None
        tts_timeout = int(os.environ.get("TTS_BLOCK_TIMEOUT", "120"))
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                audio_data, timestamps = _run_async_with_timeout(
                    self._stream_sync(text), timeout=tts_timeout
                )
                if not audio_data:
                    raise edge_tts.exceptions.NoAudioReceived(
                        "No audio chunks received from the server"
                    )

                with open(audio_path, "wb") as f:
                    f.write(b"".join(audio_data))

                with open(json_path, "w", encoding="utf-8") as f:
                    json.dump(timestamps, f, ensure_ascii=False, indent=2)

                srt_content = self._timestamps_to_srt(timestamps)
                with open(srt_path, "w", encoding="utf-8") as f:
                    f.write(srt_content)

                logger.info(
                    "TTS generated: audio=%s words=%d duration=%.1fs",
                    audio_path, len(timestamps),
                    timestamps[-1]["end_ms"] / 1000 if timestamps else 0,
                )
                return audio_path, timestamps

            except edge_tts.exceptions.NoAudioReceived as exc:
                last_error = exc
                logger.warning("TTS attempt %d/%d failed with NoAudioReceived: %s",
                               attempt, MAX_RETRIES, exc)
                if attempt < MAX_RETRIES:
                    time.sleep(RETRY_DELAY_SECONDS * attempt)
            except Exception as exc:
                last_error = exc
                logger.error("TTS attempt %d/%d failed: %s", attempt, MAX_RETRIES, exc)
                if attempt < MAX_RETRIES:
                    time.sleep(RETRY_DELAY_SECONDS * attempt)

        raise RuntimeError(
            f"TTS generation failed after {MAX_RETRIES} attempts: {last_error}"
        )

    # ── v2: Segmented synthesis per block ───────────────────

    def generate_segmented(
        self,
        bloques: list[dict],
        output_path: Optional[str] = None,
        progress_cb: Optional[callable] = None,
    ) -> tuple[str, list[dict]]:
        """Generate TTS audio segment-by-segment with per-block voice settings.

        Each block is synthesized separately with its own rate/pitch
        derived from its ``tipo`` (hook, desarrollo, climax, reflexion, cierre).
        Segments are concatenated with pydub and timestamps are merged
        with cumulative offsets.

        Args:
            bloques: List of block dicts from LLM output. Each must have:
                - tipo (str): "hook", "desarrollo", "climax", "reflexion", "cierre"
                - texto (str): The exact narration text for this block.
            output_path: Optional base path for output files.

        Returns:
            Tuple of (audio_mp3_path, timestamps_list).
            timestamps_list: word-level with cumulative time offsets.
        """
        if not bloques:
            raise ValueError("bloques list is empty")

        if output_path is None:
            timestamp = int(time.time())
            base_path = str(AUDIO_DIR / f"narration_{timestamp}")
        else:
            base_path = os.path.splitext(output_path)[0]

        audio_path = f"{base_path}.mp3"
        json_path = f"{base_path}_timestamps.json"
        srt_path = f"{base_path}_subtitles.srt"

        os.makedirs(os.path.dirname(base_path) or ".", exist_ok=True)

        logger.info("🎙️ Segmented TTS: %d blocks → %s", len(bloques), audio_path)

        # Synthesize each block to a temp MP3 file
        temp_files: list[str] = []
        all_timestamps: list[dict] = []
        cumulative_offset_ms: float = 0.0

        # Default TTS timeout per block (seconds)
        tts_timeout = int(os.environ.get("TTS_BLOCK_TIMEOUT", "120"))

        for i, bloque in enumerate(bloques):
            tipo = bloque.get("tipo", "desarrollo")
            texto = bloque.get("texto", "")
            if not texto.strip():
                logger.warning("Block %d has empty text — skipping", i)
                continue

            rate, pitch = self._voice_for_tipo(tipo)
            logger.info("  Block %d/%d [%s]: rate=%s pitch=%s text=%d chars",
                         i + 1, len(bloques), tipo, rate, pitch, len(texto))

            # Clean text for this block
            clean_text = self._clean_guion(texto)
            if not clean_text:
                continue

            # Synthesize with timeout to prevent indefinite hangs
            try:
                audio_data, word_ts = _run_async_with_timeout(
                    self._stream_sync(clean_text, rate=rate, pitch=pitch),
                    timeout=tts_timeout,
                )
            except TimeoutError:
                logger.error("Block %d TTS timed out after %ds — retrying with defaults", i, tts_timeout)
                try:
                    audio_data, word_ts = _run_async_with_timeout(
                        self._stream_sync(clean_text),
                        timeout=tts_timeout,
                    )
                except TimeoutError:
                    logger.error("Block %d retry also timed out — skipping", i)
                    continue
                except Exception as exc2:
                    logger.error("Block %d retry also failed: %s — skipping", i, exc2)
                    continue
            except Exception as exc:
                logger.error("Block %d synthesis failed: %s — retrying with defaults", i, exc)
                try:
                    audio_data, word_ts = _run_async_with_timeout(
                        self._stream_sync(clean_text),
                        timeout=tts_timeout,
                    )
                except TimeoutError:
                    logger.error("Block %d retry timed out — skipping", i)
                    continue
                except Exception as exc2:
                    logger.error("Block %d retry also failed: %s — skipping", i, exc2)
                    continue

            if not audio_data:
                logger.warning("Block %d produced no audio — skipping", i)
                continue

            # Save to temp file
            tmp = tempfile.NamedTemporaryFile(suffix=".mp3", delete=False)
            tmp.write(b"".join(audio_data))
            tmp.close()
            temp_files.append(tmp.name)

            # Adjust timestamps with cumulative offset
            for ts in word_ts:
                ts["start_ms"] = round(ts["start_ms"] + cumulative_offset_ms, 1)
                ts["end_ms"] = round(ts["end_ms"] + cumulative_offset_ms, 1)
            all_timestamps.extend(word_ts)

            # Update cumulative offset
            if word_ts:
                cumulative_offset_ms = word_ts[-1]["end_ms"]

            # Progress callback (if provided)
            if progress_cb:
                try:
                    progress_cb(i + 1, len(bloques))
                except Exception:
                    pass  # never let progress crash the synthesis

        if not temp_files:
            raise RuntimeError("No blocks produced audio — cannot create narration")

        # ── Concatenate all temp MP3s with pydub ────────────
        try:
            logger.info("Concatenating %d audio segments…", len(temp_files))
            combined = AudioSegment.empty()
            for tmp_path in temp_files:
                seg = AudioSegment.from_mp3(tmp_path)
                combined += seg

            combined.export(audio_path, format="mp3", bitrate="192k")
            logger.info("Exported combined audio: %s (%.1fs)", audio_path, len(combined) / 1000.0)
        finally:
            # Always clean up temp files, even if concatenation/export fails
            for tmp_path in temp_files:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass

        # ── Save timestamps and SRT ─────────────────────────
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(all_timestamps, f, ensure_ascii=False, indent=2)

        srt_content = self._timestamps_to_srt(all_timestamps)
        with open(srt_path, "w", encoding="utf-8") as f:
            f.write(srt_content)

        total_dur = all_timestamps[-1]["end_ms"] / 1000 if all_timestamps else 0
        logger.info("✅ Segmented TTS done: %d words, %.1fs, %d blocks",
                     len(all_timestamps), total_dur, len(temp_files))

        return audio_path, all_timestamps

    # ── Async streaming ─────────────────────────────────────

    async def _stream_sync(
        self,
        text: str,
        rate: Optional[str] = None,
        pitch: Optional[str] = None,
    ) -> tuple[list[bytes], list[dict]]:
        """Asynchronously stream TTS audio with word boundary detection.

        Args:
            text: Text to synthesize.
            rate: Optional rate override.
            pitch: Optional pitch override.

        Returns:
            Tuple of (audio_chunks, timestamps_list).
        """
        communicate = self._build_communicate(text, rate=rate, pitch=pitch)
        audio_chunks: list[bytes] = []
        timestamps: list[dict] = []

        async for chunk in communicate.stream():
            chunk_type = chunk.get("type", "")

            if chunk_type == "audio":
                audio_chunks.append(chunk["data"])

            elif chunk_type == "WordBoundary":
                offset_tick = chunk.get("offset", 0)
                duration_tick = chunk.get("duration", 0)
                word_text = str(chunk.get("text", ""))

                start_ms = offset_tick / TICK_TO_MS
                end_ms = (offset_tick + duration_tick) / TICK_TO_MS
                duration_ms = duration_tick / TICK_TO_MS

                timestamps.append({
                    "word": word_text,
                    "start_ms": round(start_ms, 1),
                    "end_ms": round(end_ms, 1),
                    "duration_ms": round(duration_ms, 1),
                })

        return audio_chunks, timestamps

    # ── SRT generation ──────────────────────────────────────

    def _timestamps_to_srt(self, timestamps: list[dict]) -> str:
        """Convert word-level timestamps into SRT subtitle format."""
        if not timestamps:
            return ""

        blocks = []
        current_words = [timestamps[0]["word"]]
        current_start = timestamps[0]["start_ms"]
        current_end = timestamps[0]["end_ms"]

        for ts in timestamps[1:]:
            gap = ts["start_ms"] - current_end
            if gap < 200 and len(" ".join(current_words)) < 42:
                current_words.append(ts["word"])
                current_end = ts["end_ms"]
            else:
                blocks.append({
                    "start_ms": current_start,
                    "end_ms": current_end,
                    "text": " ".join(current_words).strip(),
                })
                current_words = [ts["word"]]
                current_start = ts["start_ms"]
                current_end = ts["end_ms"]

        if current_words:
            blocks.append({
                "start_ms": current_start,
                "end_ms": current_end,
                "text": " ".join(current_words).strip(),
            })

        lines = []
        for i, block in enumerate(blocks, 1):
            lines.append(str(i))
            lines.append(
                f"{_ms_to_srt_time(block['start_ms'])} --> "
                f"{_ms_to_srt_time(block['end_ms'])}"
            )
            lines.append(block["text"])
            lines.append("")

        return "\n".join(lines)

    # ── Scene parsing (legacy) ──────────────────────────────

    def parse_scenes(self, guion_text: str) -> list[dict]:
        """Parse a script with [ESCENA: ...] markers into scene segments.

        (Legacy — prefer using bloques from the LLM output directly.)
        """
        if not guion_text or not guion_text.strip():
            raise ValueError("guion_text is empty")

        pattern = r'\[ESCENA:\s*([^\]]*)\]\s*'
        parts = re.split(pattern, guion_text, flags=re.IGNORECASE)

        scenes = []
        start = 1 if parts[0].strip() == "" else 0

        i = start
        while i + 1 < len(parts):
            description = parts[i].strip()
            scene_text = parts[i + 1].strip()

            if scene_text:
                scenes.append({
                    "text": scene_text,
                    "description": description,
                    "emotion": self._infer_emotion(scene_text, description),
                })

            i += 2

        if not scenes:
            raise ValueError("No [ESCENA: ...] markers found in guion_text")

        return scenes

    _EMOTION_KEYWORDS = {
        "tensión": [
            "miedo", "terror", "angustia", "peligro", "amenaza",
            "oscuro", "sombra", "grito", "corazón latía",
        ],
        "tristeza": [
            "llorar", "lágrima", "dolor", "pérdida", "murió",
            "falleció", "adiós", "soledad", "ausencia",
        ],
        "intriga": [
            "misterio", "secreto", "extraño", "inexplicable",
            "desconocido", "enigma", "descubrió", "sospecha",
        ],
        "esperanza": [
            "luz", "renacer", "oportunidad", "futuro", "sueño",
            "salvación", "milagro", "segunda oportunidad",
        ],
        "asombro": [
            "increíble", "sorprendente", "impresionante",
            "inimaginable", "nunca visto", "alucinante",
        ],
        "reflexión": [
            "aprendió", "lección", "moraleja", "comprendió",
            "entendió", "reflexionar", "pensar", "conclusión",
        ],
    }

    def _infer_emotion(self, text: str, description: str) -> str:
        """Infer the emotional tone from scene text and description."""
        combined = (text + " " + description).lower()
        for emotion, keywords in self._EMOTION_KEYWORDS.items():
            if any(kw in combined for kw in keywords):
                return emotion
        return "neutral"
