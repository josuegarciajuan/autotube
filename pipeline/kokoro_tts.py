"""Kokoro-82M TTS engine for the Autotube pipeline.

Provides the same interface as TTSEngine (edge-tts) but uses the
local, Apache-2.0-licensed Kokoro-82M model. Supports segmented
synthesis with per-block speed control for dynamic narration.

Spanish voices available:
  - ef_dora  (female)
  - em_alex  (male)
  - em_santa (male)
"""

import json
import logging
import os
import re
import tempfile
import time
from pathlib import Path
from typing import Optional

import numpy as np
import soundfile as sf
from pydub import AudioSegment

from config.settings import AUDIO_DIR

logger = logging.getLogger(__name__)

# ── SRT helpers (same as tts_engine.py) ──────────────────────

def _ms_to_srt_time(ms: float) -> str:
    hours = int(ms // 3_600_000)
    minutes = int((ms % 3_600_000) // 60_000)
    seconds = int((ms % 60_000) // 1000)
    millis = int(ms % 1000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{millis:03d}"


# ── Default block speed profiles (can be overridden per channel) ──

DEFAULT_BLOCK_SPEEDS: dict[str, float] = {
    "hook": 1.06,
    "desarrollo": 0.94,
    "climax": 0.87,
    "reflexion": 0.92,
    "cierre": 0.98,
}

AVAILABLE_VOICES = ["ef_dora", "em_alex", "em_santa"]


class KokoroTTSEngine:
    """Synthesize speech using Kokoro-82M with segmented narration.

    Each narrative block gets a custom speed to create emotional
    dynamics: faster for hooks/intrigue, slower for climax, etc.
    """

    def __init__(self, voice_config: Optional[dict] = None):
        if voice_config is None:
            voice_config = {}

        self.kokoro_voice = voice_config.get("kokoro_voice", "em_santa")
        self.block_speeds = voice_config.get(
            "block_speeds", DEFAULT_BLOCK_SPEEDS
        )
        self.pause_between = voice_config.get("pause_between_blocks", 0.7)
        self.sample_rate = 24000
        self._pipeline = None  # lazy init

    @property
    def pipeline(self):
        """Lazy-load Kokoro pipeline (heavy model, loaded once)."""
        if self._pipeline is None:
            from kokoro import KPipeline
            logger.info("Loading Kokoro-82M pipeline (lang=es)...")
            self._pipeline = KPipeline(lang_code='e')
        return self._pipeline

    def unload(self):
        """Release the Kokoro pipeline model to free ~500 MB RAM.

        After TTS phase completes, the KPipeline (KModel + voice packs + torch
        tensors) is no longer needed.  Calling this drops the reference so the
        garbage collector can reclaim the memory.
        """
        if self._pipeline is not None:
            import gc
            logger.info("Unloading Kokoro pipeline to free memory...")
            del self._pipeline
            self._pipeline = None
            gc.collect()
            logger.info("Kokoro pipeline unloaded")

    def _speed_for_tipo(self, tipo: str) -> float:
        """Get speed multiplier for a block type."""
        return self.block_speeds.get(tipo, 1.0)

    # ── Text cleaning (shared with edge-tts engine) ──────────

    @staticmethod
    def _remove_bracketed_marker(text: str, marker: str) -> str:
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
        cleaned = self._remove_bracketed_marker(text, "ESCENA")
        cleaned = self._remove_bracketed_marker(cleaned, "PAUSA")
        cleaned = re.sub(r'\bNarrador\s*:\s*', '', cleaned, flags=re.IGNORECASE)
        cleaned = cleaned.replace('[', ' ').replace(']', ' ')
        cleaned = re.sub(r'\bESCENA\b', '', cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r'\bPAUSA\b', '', cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r'\s+', ' ', cleaned).strip()
        return cleaned

    # ── Timestamp estimation ─────────────────────────────────

    @staticmethod
    def _estimate_timestamps(words: list[str], total_duration_s: float) -> list[dict]:
        """Estimate word-level timestamps by distributing duration
        proportional to character count of each word.

        Args:
            words: List of words in order.
            total_duration_s: Total audio duration in seconds.

        Returns:
            List of dicts with word, start_ms, end_ms, duration_ms.
        """
        if not words or total_duration_s <= 0:
            return []

        total_chars = sum(len(w) for w in words)
        if total_chars == 0:
            return []

        timestamps = []
        current_ms = 0.0
        total_ms = total_duration_s * 1000.0

        for word in words:
            word_ms = (len(word) / total_chars) * total_ms
            timestamps.append({
                "word": word,
                "start_ms": round(current_ms, 1),
                "end_ms": round(current_ms + word_ms, 1),
                "duration_ms": round(word_ms, 1),
            })
            current_ms += word_ms

        return timestamps

    # ── SRT generation ───────────────────────────────────────

    @staticmethod
    def _timestamps_to_srt(timestamps: list[dict]) -> str:
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

    # ── Main API ─────────────────────────────────────────────

    def generate(
        self,
        text: str,
        output_path: Optional[str] = None,
    ) -> tuple[str, list[dict]]:
        """Generate TTS from a single block of text (legacy fallback).

        Returns (mp3_path, timestamps_list).
        """
        clean = self._clean_guion(text)
        if not clean:
            raise ValueError("Empty text after cleaning")

        if output_path is None:
            base = str(AUDIO_DIR / f"narration_kokoro_{int(time.time())}")
        else:
            base = os.path.splitext(output_path)[0]

        os.makedirs(os.path.dirname(base) or ".", exist_ok=True)

        audio_path = f"{base}.mp3"
        json_path = f"{base}_timestamps.json"
        srt_path = f"{base}_subtitles.srt"

        # Generate
        generator = self.pipeline(clean, voice=self.kokoro_voice, speed=1.0)
        audio_chunks = []
        for _gs, _ps, arr in generator:
            audio_chunks.append(arr if isinstance(arr, np.ndarray) else arr.numpy())

        if len(audio_chunks) > 1:
            audio = np.concatenate(audio_chunks)
        else:
            audio = audio_chunks[0]

        duration_s = len(audio) / self.sample_rate

        # Estimate timestamps
        words = clean.split()
        timestamps = self._estimate_timestamps(words, duration_s)

        # Save as MP3 via pydub
        tmp_wav = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
        sf.write(tmp_wav.name, audio, self.sample_rate)
        AudioSegment.from_wav(tmp_wav.name).export(audio_path, format="mp3", bitrate="192k")
        os.unlink(tmp_wav.name)

        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(timestamps, f, ensure_ascii=False, indent=2)

        srt = self._timestamps_to_srt(timestamps)
        with open(srt_path, "w", encoding="utf-8") as f:
            f.write(srt)

        logger.info("Kokoro TTS: %s (%.1fs, %d words)", audio_path, duration_s, len(words))
        return audio_path, timestamps

    def generate_segmented(
        self,
        bloques: list[dict],
        output_path: Optional[str] = None,
        progress_cb: Optional[callable] = None,
    ) -> tuple[str, list[dict]]:
        """Generate TTS with per-block speed control and paragraph pauses.

        Each block is synthesized with its own speed based on its
        ``tipo`` (hook, desarrollo, climax, reflexion, cierre).

        Pauses are only inserted at paragraph boundaries (when the
        previous block has ``is_last_in_paragraph=True``), not between
        every block. This prevents "microcortes" — annoying 1-2 second
        silences within paragraphs.

        Args:
            bloques: List of block dicts with ``tipo`` and ``texto`` keys.
            output_path: Optional base path for output files.

        Returns:
            Tuple of (audio_mp3_path, timestamps_list).
        """
        if not bloques:
            raise ValueError("bloques list is empty")

        if output_path is None:
            base = str(AUDIO_DIR / f"narration_kokoro_{int(time.time())}")
        else:
            base = os.path.splitext(output_path)[0]

        os.makedirs(os.path.dirname(base) or ".", exist_ok=True)

        audio_path = f"{base}.mp3"
        json_path = f"{base}_timestamps.json"
        srt_path = f"{base}_subtitles.srt"

        logger.info(
            "🎙️ Kokoro segmented TTS: %d blocks → %s (voice=%s)",
            len(bloques), audio_path, self.kokoro_voice
        )

        all_audio: list[np.ndarray] = []
        all_timestamps: list[dict] = []
        cumulative_offset_ms: float = 0.0

        for i, bloque in enumerate(bloques):
            tipo = bloque.get("tipo", "desarrollo")
            texto = bloque.get("texto", "")
            if not texto.strip():
                logger.warning("Block %d has empty text — skipping", i)
                continue

            clean = self._clean_guion(texto)
            if not clean:
                continue

            speed = self._speed_for_tipo(tipo)
            logger.info(
                "  Block %d/%d [%s] speed=%.2f text=%d chars",
                i + 1, len(bloques), tipo, speed, len(clean)
            )

            # Generate audio for this block
            t0 = time.time()
            try:
                generator = self.pipeline(clean, voice=self.kokoro_voice, speed=speed)
                audio_chunks = []
                for _gs, _ps, arr in generator:
                    audio_chunks.append(arr if isinstance(arr, np.ndarray) else arr.numpy())
            except Exception as exc:
                logger.error("Kokoro block %d failed: %s — skipping", i, exc)
                continue

            if not audio_chunks:
                logger.warning("Block %d produced no audio", i)
                continue

            block_audio = (
                np.concatenate(audio_chunks) if len(audio_chunks) > 1
                else audio_chunks[0]
            )
            block_dur = len(block_audio) / self.sample_rate
            elapsed = time.time() - t0
            logger.info("    ✅ %.1fs audio in %.1fs (RTF %.1fx)", block_dur, elapsed, elapsed / block_dur)

            # Pause only at paragraph boundaries (when previous block
            # was last in its paragraph). Inter-paragraph transitions
            # with background music are handled separately by the video
            # editor's transition system (_insert_transitions).
            if i > 0 and self.pause_between > 0:
                prev_block = bloques[i - 1]
                if prev_block.get("is_last_in_paragraph", False):
                    pause_samples = int(self.pause_between * self.sample_rate)
                    all_audio.append(np.zeros(pause_samples, dtype=np.float32))
                    cumulative_offset_ms += self.pause_between * 1000.0
                    logger.debug(
                        "    ⏸️ %.1fs pause at paragraph boundary (block %d → %d)",
                        self.pause_between, i - 1, i,
                    )

            all_audio.append(block_audio)

            # Estimate word-level timestamps for this block
            words = clean.split()
            block_ts = self._estimate_timestamps(words, block_dur)

            # Adjust with cumulative offset
            for ts in block_ts:
                ts["start_ms"] = round(ts["start_ms"] + cumulative_offset_ms, 1)
                ts["end_ms"] = round(ts["end_ms"] + cumulative_offset_ms, 1)
            all_timestamps.extend(block_ts)

            cumulative_offset_ms += block_dur * 1000.0

            # Progress callback (if provided)
            if progress_cb:
                try:
                    progress_cb(i + 1, len(bloques))
                except Exception:
                    pass

        if not all_audio:
            raise RuntimeError("No blocks produced audio")

        # Concatenate all blocks
        final_audio = np.concatenate(all_audio)
        total_dur = len(final_audio) / self.sample_rate

        # Save as MP3 via pydub
        logger.info("Exporting %.1fs audio to MP3…", total_dur)
        tmp_wav = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
        sf.write(tmp_wav.name, final_audio, self.sample_rate)
        AudioSegment.from_wav(tmp_wav.name).export(audio_path, format="mp3", bitrate="192k")
        os.unlink(tmp_wav.name)

        # Save timestamps and SRT
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(all_timestamps, f, ensure_ascii=False, indent=2)

        srt = self._timestamps_to_srt(all_timestamps)
        with open(srt_path, "w", encoding="utf-8") as f:
            f.write(srt)

        logger.info(
            "✅ Kokoro segmented done: %d words, %.1fs, %d blocks",
            len(all_timestamps), total_dur, len(all_audio)
        )
        return audio_path, all_timestamps

    # ── Scene parsing (legacy, shared from EdgeTTSEngine) ──────

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

        return scenes
