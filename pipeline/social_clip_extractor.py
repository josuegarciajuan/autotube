"""AI-powered social media clip extractor.

Analyzes a generated video script to find the most engaging 45-90 second
segment, then extracts it from the video and converts to vertical 9:16 format
with burnt-in subtitles.

Strategy: find the PRE-CLIMAX — the moment of maximum suspense just BEFORE
the resolution. This creates a cliffhanger that drives viewers to YouTube
for the full story. Never include the full explanation or conclusion.
"""
from __future__ import annotations

import json
import logging
import os
import random
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass
class SegmentScore:
    """Scored segment from the video script."""
    block_index: int
    start_time: float       # seconds into the video
    duration: float
    text: str                # the script text for this segment
    viral_score: float       # 0-1, how engaging this segment is
    reason: str              # why it scored high


class SocialClipExtractor:
    """Extract the best viral clip from a generated video.

    Uses LLM-based scoring of script blocks to identify the most
    engaging segment (hook, climax, plot twist), then uses ffmpeg
    to extract and reformat that segment for social media.
    """

    # Scoring weights for segment selection (v2 — pre-climax strategy)
    VIRAL_INDICATORS = {
        "hook": 0.35,          # opening hook or attention grabber (MAX weight)
        "cliffhanger": 0.25,   # suspense-building, unresolved tension
        "climax": 0.20,        # dramatic peak (we include start but exclude end)
        "emotional": 0.10,     # emotional high point
        "visual": 0.10,        # visually described scene
    }

    # Pre-climax strategy: start BEFORE the climax and end JUST AFTER,
    # never including the resolution/explanation.
    PRE_CLIMAX_LEAD_SECONDS = 20   # seconds of buildup before climax
    PRE_CLIMAX_TAIL_SECONDS = 10   # seconds after climax peak (no resolution)
    MIN_CLIP_DURATION = 45         # seconds
    MAX_CLIP_DURATION = 65         # seconds
    TARGET_DURATION = 55           # seconds (preferred)

    def __init__(self, config=None):
        self.config = config or {}

    # ── AI-based segment selection ─────────────────────────

    def score_segments(self, script_blocks: list[dict], total_duration: float) -> list[SegmentScore]:
        """Score each script block for viral potential.

        Uses LLM to evaluate each block against viral indicators.
        Falls back to heuristic scoring if LLM is unavailable.
        """
        scores = []

        for i, block in enumerate(script_blocks):
            text = block.get("text", block.get("narration", ""))
            if not text or len(text) < 30:
                continue

            # Estimate timing from block metadata
            start_time = block.get("start_time", 0)
            duration = block.get("duration", 0)
            if duration <= 0:
                # Heuristic: ~150 words per minute = 2.5 words per second
                word_count = len(text.split())
                duration = max(5, word_count / 2.5)
                # Clamp to valid range
                duration = min(duration, self.MAX_CLIP_DURATION)
                duration = max(duration, self.MIN_CLIP_DURATION)

            if not start_time:
                start_time = sum(
                    b.get("duration", 5) for b in script_blocks[:i]
                )

            # Try LLM scoring
            score, reason = self._llm_score_block(text, i, len(script_blocks))

            scores.append(SegmentScore(
                block_index=i,
                start_time=start_time,
                duration=duration,
                text=text,
                viral_score=score,
                reason=reason,
            ))

        # Sort by viral score descending
        scores.sort(key=lambda s: s.viral_score, reverse=True)
        return scores

    def find_best_segment(self, script_blocks: list[dict], total_duration: float) -> SegmentScore | None:
        """Find the best PRE-CLIMAX segment for a social clip.

        Strategy: identify the climax block, then select a window that starts
        BEFORE it (buildup) and ends JUST AFTER it (before resolution).
        This creates a cliffhanger that drives YouTube clicks.
        """
        scores = self.score_segments(script_blocks, total_duration)
        if not scores:
            return None

        # Find the climax block: high score + late position but not last
        # Sort by viral_score * position_penalty (favor middle-late blocks)
        n = len(scores)
        for s in scores:
            # Position bonus: middle-late is ideal for climax (not very end)
            position_ratio = s.block_index / max(n - 1, 1)
            if 0.5 <= position_ratio <= 0.85:
                s.viral_score += 0.10
            elif position_ratio > 0.85:
                s.viral_score -= 0.15  # Penalize very late (probably resolution)
            elif position_ratio < 0.2:
                s.viral_score -= 0.05  # Slight penalty for too early

        scores.sort(key=lambda s: s.viral_score, reverse=True)
        climax = scores[0]

        # Build pre-climax window: start = (climax_start - LEAD), end = (climax_start + TAIL)
        pre_start = max(0, climax.start_time - self.PRE_CLIMAX_LEAD_SECONDS)
        post_end = climax.start_time + climax.duration + self.PRE_CLIMAX_TAIL_SECONDS

        # Clamp to min/max duration
        window_duration = post_end - pre_start
        if window_duration < self.MIN_CLIP_DURATION:
            # Extend forward to reach minimum
            post_end = pre_start + self.MIN_CLIP_DURATION
            window_duration = self.MIN_CLIP_DURATION
        elif window_duration > self.MAX_CLIP_DURATION:
            # Tighten: reduce lead time
            pre_start = post_end - self.MAX_CLIP_DURATION
            window_duration = self.MAX_CLIP_DURATION

        best = SegmentScore(
            block_index=climax.block_index,
            start_time=pre_start,
            duration=window_duration,
            text=climax.text,
            viral_score=climax.viral_score,
            reason=f"Pre-climax window around block {climax.block_index}: {climax.reason}",
        )

        logger.info(
            "Best clip segment (pre-climax): block %d, score=%.2f, start=%.1fs, dur=%.1fs: %s",
            best.block_index, best.viral_score, best.start_time, best.duration, best.reason,
        )
        return best

    def _llm_score_block(self, text: str, block_index: int, total_blocks: int) -> tuple[float, str]:
        """Use LLM to score a block for viral potential (pre-climax strategy).

        Rewards: hook quality, unresolved suspense, emotional intensity, visual imagery.
        Penalizes: resolution, explanation, summary, conclusions.
        """
        try:
            from config.settings import AI_API_KEY, AI_BASE_URL, AI_MODEL
            import requests

            prompt = (
                "Evalua este fragmento de guion para un clip de TikTok/Reels (0-1).\n"
                "BUSCAMOS EL PRE-CLIMAX: maximo suspense ANTES de la resolucion.\n"
                "Puntua ALTO si: hook fuerte, suspense sin resolver, revelacion parcial, "
                "emocion intensa.\n"
                "Puntua BAJO si: explicacion, conclusion, resumen, desenlace.\n\n"
                f"Fragmento #{block_index + 1}/{total_blocks}:\n{text[:500]}\n\n"
                "Responde SOLO en formato JSON: {\"score\": 0.XX, \"reason\": \"breve explicacion\"}"
            )

            resp = requests.post(
                f"{AI_BASE_URL}/chat/completions",
                headers={
                    "Authorization": f"Bearer {AI_API_KEY}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": AI_MODEL,
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": 0.3,
                    "max_tokens": 150,
                },
                timeout=15,
            )
            data = resp.json()
            content = data["choices"][0]["message"]["content"]
            result = json.loads(re.search(r'\{.*\}', content, re.DOTALL).group())
            return float(result.get("score", 0.5)), str(result.get("reason", "AI scored"))
        except Exception as exc:
            logger.debug("LLM viral score failed, using heuristic: %s", exc)
            return self._heuristic_score(text, block_index, total_blocks)

    def _heuristic_score(self, text: str, block_index: int, total_blocks: int) -> tuple[float, str]:
        """Heuristic fallback for pre-climax scoring.

        Rewards:
        - Middle-late blocks (climax zone) but NOT last blocks (resolution)
        - Suspense language (questions, cliffhangers, "pero", "sin embargo")
        - Emotional intensity indicators
        - Visual descriptions

        Penalizes:
        - Conclusion words ("en conclusion", "finalmente", "asi que")
        - Last blocks (usually resolution)
        """
        score = 0.3
        reasons = []

        # Position: pre-climax zone is 0.4-0.8
        position_ratio = block_index / max(total_blocks - 1, 1)
        if 0.4 <= position_ratio <= 0.75:
            score += 0.25
            reasons.append("zona pre-climax")
        elif position_ratio > 0.85:
            score -= 0.20
            reasons.append("posicion: probable resolucion")
        elif position_ratio < 0.15:
            score += 0.05
            reasons.append("posicion: posible hook")

        # Penalize resolution/conclusion words
        text_lower = text.lower()
        conclusion_words = [
            "en conclusion", "finalmente", "asi que", "por eso", "por lo tanto",
            "en resumen", "para concluir", "la moraleja", "en definitiva",
            "por consiguiente", "como resultado", "esto demuestra",
        ]
        penalty = sum(0.08 for w in conclusion_words if w in text_lower)
        score -= min(0.30, penalty)
        if penalty > 0:
            reasons.append(f"penalizacion conclusion (-{min(0.30, penalty):.2f})")

        # Reward suspense/cliffhanger indicators
        suspense_words = [
            "pero", "sin embargo", "lo que paso", "lo que encontro", "descubrio",
            "nunca", "jamas", "inesperado", "sorprendente", "aterrador",
            "misterio", "secreto", "oculto", "prohibido", "desconocido",
            "lo que nadie", "repentinamente", "de pronto", "entonces",
        ]
        for word in suspense_words:
            if word in text_lower:
                score += 0.03
                if score > 0.95:
                    break
        reasons.append("indicadores de suspense")

        # Emotional intensity: exclamations + questions
        exclamations = text.count("!") + text.count("¡")
        questions = text.count("?") + text.count("¿")
        score += min(0.10, (exclamations + questions) * 0.02)

        # Visual descriptions (good for TikTok/Reels)
        visual_words = [
            "imagen", "video", "grabacion", "foto", "vista", "oscuro",
            "luz", "sombras", "gigante", "enorme", "profundo",
        ]
        visual_count = sum(1 for w in visual_words if w in text_lower)
        score += min(0.10, visual_count * 0.02)

        if not reasons:
            reasons.append("analisis heuristico")

        return min(score, 1.0), "; ".join(reasons)

    # ── ffmpeg clip extraction ─────────────────────────────

    def extract_clip(
        self, video_path: str, start_time: float, duration: float,
        output_path: str, subtitle_text: str = None,
    ) -> str:
        """Extract a clip from the video, convert to vertical 9:16.

        Args:
            video_path: Path to the source video file.
            start_time: Start time in seconds.
            duration: Duration of the clip in seconds.
            output_path: Path for the output clip (.mp4).
            subtitle_text: Optional subtitle text to burn into the clip.

        Returns:
            Path to the generated clip.
        """
        os.makedirs(os.path.dirname(output_path), exist_ok=True)

        # Build ffmpeg filter chain for vertical crop + subtitles
        vf_filters = []

        # Crop center to 9:16 vertical
        # Formula: crop width = height * 9/16, centered horizontally
        vf_filters.append(
            "crop=ih*9/16:ih:(iw-ih*9/16)/2:0,scale=1080:1920"
        )

        # Add burnt-in subtitles if provided
        if subtitle_text:
            subtitle_file = output_path.replace(".mp4", ".ass")
            self._write_subtitle_file(subtitle_text, subtitle_file, duration)
            vf_filters.append(f"ass={subtitle_file}")

        vf_chain = ",".join(vf_filters)

        cmd = [
            "ffmpeg",
            "-ss", str(start_time),
            "-i", video_path,
            "-t", str(duration),
            "-vf", vf_chain,
            "-c:v", "libx264",
            "-crf", "23",
            "-preset", "fast",
            "-c:a", "aac",
            "-b:a", "128k",
            "-movflags", "+faststart",
            "-y",
            output_path,
        ]

        logger.info("Extracting clip: start=%.1f dur=%.1f → %s", start_time, duration, output_path)
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
            if result.returncode != 0:
                logger.error("ffmpeg clip extraction failed: %s", result.stderr[-500:])
                raise RuntimeError(f"ffmpeg failed: {result.stderr[-200:]}")
        except subprocess.TimeoutExpired:
            raise RuntimeError("ffmpeg clip extraction timed out")

        # Clean up subtitle temp file
        if subtitle_text:
            try:
                os.unlink(output_path.replace(".mp4", ".ass"))
            except OSError:
                pass

        logger.info("Clip extracted: %s", output_path)
        return output_path

    def _write_subtitle_file(self, text: str, output_path: str, duration: float):
        """Write a simple ASS subtitle file with large, centered text."""
        words = text.split()
        lines = []
        current_line = []
        current_len = 0
        max_chars_per_line = 35

        for word in words:
            if current_len + len(word) + 1 > max_chars_per_line:
                lines.append(" ".join(current_line))
                current_line = [word]
                current_len = len(word)
            else:
                current_line.append(word)
                current_len += len(word) + 1
        if current_line:
            lines.append(" ".join(current_line))

        ass_content = f"""[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,Arial,48,&H00FFFFFF,&H000000FF,&H00000000,&H80000000,1,0,0,0,100,100,0,0,1,3,0,2,60,60,120,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
        line_duration = duration / max(len(lines), 1)
        for idx, line in enumerate(lines):
            start = idx * line_duration
            end = min(start + line_duration + 0.5, duration)
            ass_content += (
                f"Dialogue: 0,{self._format_ass_time(start)},{self._format_ass_time(end)},"
                f"Default,,0,0,0,,{line}\n"
            )

        with open(output_path, "w", encoding="utf-8") as f:
            f.write(ass_content)

    @staticmethod
    def _format_ass_time(seconds: float) -> str:
        """Format seconds as ASS time (H:MM:SS.cc)."""
        h = int(seconds // 3600)
        m = int((seconds % 3600) // 60)
        s = seconds % 60
        return f"{h}:{m:02d}:{s:05.2f}"
