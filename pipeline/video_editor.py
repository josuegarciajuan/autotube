"""Video editor: assembles images + audio + subtitles + effects into final MP4."""

import os
import re
import random
import logging
import time
from pathlib import Path
from typing import Callable, Optional

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

from config.settings import (
    VIDEOS_DIR,
    AUDIO_DIR,
    IMAGES_DIR,
    ASSETS_DIR,
    OUTPUT_DIR,
    VIDEO_FPS,
    VIDEO_RESOLUTION,
    VIDEO_BITRATE,
    VIDEO_CODEC,
)
from config.canal2_config import (
    CANAL_NAME,
    CANAL_DISPLAY_NAME,
    COLOR_PALETTE,
    FILM_GRAIN_OPACITY,
    KEN_BURNS_ZOOM_MIN,
    KEN_BURNS_ZOOM_MAX,
)

try:
    from moviepy import (
        VideoClip,
        VideoFileClip,
        ImageClip,
        AudioFileClip,
        AudioClip,
        TextClip,
        CompositeVideoClip,
        CompositeAudioClip,
        concatenate_videoclips,
        concatenate_audioclips,
        vfx,
        afx,
    )

    MOVIEPY_V2 = True
except ImportError:
    from moviepy.editor import (
        VideoClip,
        VideoFileClip,
        ImageClip,
        AudioFileClip,
        AudioClip,
        TextClip,
        CompositeVideoClip,
        CompositeAudioClip,
        concatenate_videoclips,
        vfx,
        afx,
    )

    MOVIEPY_V2 = False

    # concatenate_audioclips not available in moviepy v1 — provide a fallback
    def concatenate_audioclips(clips) -> CompositeAudioClip:
        return CompositeAudioClip(clips)


def _db_to_linear(db: float) -> float:
    """Convert dB relative to full scale to linear amplitude factor."""
    return 10 ** (db / 20)


def _dynamic_volume(clip, factor):
    """MoviePy v2: apply a dynamic or scalar volume factor to an audio clip.

    Unlike ``afx.MultiplyVolume`` (which only supports scalar factors),
    this helper accepts a **callable** ``factor(t) -> float`` so volume
    can vary over time (e.g. ducking).  Scalars are still supported.

    Uses ``clip.transform(..., keep_duration=True)`` — the same internal
    mechanism as ``MultiplyVolume`` — but evaluates ``factor(t)`` per frame
    when it is callable.
    """
    if callable(factor):
        def _frame_fn(get_frame, t):
            fv = np.asarray(factor(t), dtype=float)
            frame = get_frame(t)
            # Ensure factor shapes broadcast with multi-channel audio.
            # factor(t) may return (n_samples,) while frame is (n_samples, nchannels).
            if frame.ndim > fv.ndim:
                fv = fv[..., np.newaxis]
            return fv * frame
        return clip.transform(_frame_fn, keep_duration=True)
    return clip.transform(
        lambda get_frame, t: factor * get_frame(t),
        keep_duration=True,
    )


class VideoEditor:
    """MoviePy-based video assembler with Ken Burns, subtitles, ducking, and effects.

    Handles both MoviePy v1 and v2 APIs through compatibility branches.
    All visual effects degrade gracefully — a single effect failure does not
    abort the entire render.
    """

    TRIGGER_WORDS: set[str] = {
        "puerta",
        "pasos",
        "viento",
        "grito",
        "trueno",
        "silencio",
        "susurro",
        "explosión",
        "disparo",
        "latido",
        "campana",
        "sirena",
        "vidrio",
        "lluvia",
        "fuego",
        "eco",
        "golpe",
        "risa",
        "llanto",
        "aplausos",
    }

    INTRO_DURATION: float = 3.0
    OUTRO_DURATION: float = 5.0
    SCENE_DURATION_MIN: float = 5.0   # Enforced — scenes shorter than this are merged
    SCENE_DURATION_MAX: float = 20.0  # Enforced — scenes longer than this are split (matches stock video max)
    # Legacy aliases kept for backward compatibility
    MIN_SCENE_DURATION: float = SCENE_DURATION_MIN
    MAX_SCENE_DURATION: float = SCENE_DURATION_MAX

    DEFAULT_FONT: str = "DejaVu-Sans"

    def __init__(self, canal_config = None) -> None:
        """Initialise the video editor with optional canal configuration.

        Args:
            canal_config: Dict or module with canal-specific overrides (COLOR_PALETTE,
                FILM_GRAIN_OPACITY, KEN_BURNS_ZOOM_MIN, KEN_BURNS_ZOOM_MAX, etc.).
        """
        # Convert module to dict if needed
        if canal_config is not None and not isinstance(canal_config, dict):
            import inspect
            canal_config = {
                k: v for k, v in inspect.getmembers(canal_config)
                if not k.startswith('_') and not inspect.ismodule(v) and not inspect.isfunction(v)
            }
        self.canal = canal_config or {}
        self.logger = logging.getLogger(self.__class__.__name__)
        self.video_size: tuple[int, int] = VIDEO_RESOLUTION
        self.fps: int = VIDEO_FPS

        # P3: Per-video uniform color grade (consistent across all clips)
        self._video_color_grade = None

        # P4: Asset deduplication tracking — prevents the same file from
        # being used for multiple scenes within one video build.
        # Images: full dedup (Ken Burns on same image is repetitive).
        # Videos: offset tracking — each scene gets a different segment.
        self._used_asset_paths: set[str] = set()
        self._video_offset_tracker: dict[str, float] = {}  # path → next_start_offset

    # ── Public API ──────────────────────────────────────────────

    def build_video(
        self,
        bloques: list[dict] = None,
        media_assets: list[dict] = None,
        scenes: list[dict] = None,
        image_paths: list = None,
        audio_path: str = None,
        timestamps: list[dict] = None,
        output_path: str | None = None,
        music_volume: float = -20.0,
        cta_audio_path: str | None = None,
        scene_ranges: list[dict] | None = None,     # v2: precomputed enforceable ranges
    ) -> Path:
        """Assemble the complete video from blocks, media, audio, and effects.

        v2 API (preferred):
            bloques: List of block dicts from LLM (tipo, texto, media_tipo, ...)
            media_assets: List of asset dicts from MediaFetcher (path, type, duration)
            audio_path: Path to TTS voice-over MP3
            timestamps: Word-level timestamps
            cta_audio_path: Optional voice-over for the CTA segment

        v1 API (legacy fallback):
            scenes: List of scene dicts from parse_scenes()
            image_paths: List[list[Path]] per-scene image paths

        v2 Pipeline stages:
            1. Compute block time ranges from timestamps
            2. Create clips: VideoFileClip (video) or Ken Burns (image)
            3. Composite with crossfade transitions
            4. Add subtitles (if SUBTITLES_ENABLED)
            5. Build CTA clip with optional voice
            6. Assemble final sequence:
               INTRO (3s) → BODY (exact narration length) → CTA (2.5s) → OUTRO (5s)
            7. Render MP4
        """
        if output_path is None:
            safe_stem = Path(audio_path).stem.replace(" ", "_")
            output_path = str(VIDEOS_DIR / f"{safe_stem}.mp4")
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)

        timestamps = self._normalize_timestamps(timestamps)

        # ── Determine API mode ─────────────────────────────────
        use_blocks = bloques is not None and media_assets is not None
        if not use_blocks:
            raise ValueError("bloques + media_assets must be provided (legacy scenes path removed)")

        self.logger.info("🎬 Building video (v2 blocks) → %s", output_path)

        # ── Reset per-build state ──────────────────────────────
        self._used_asset_paths.clear()
        self._video_offset_tracker.clear()
        self._video_color_grade = None

        # ── Compute block time ranges ──────────────────────────
        if scene_ranges:
            # Pre-computed ranges from the orchestrator (1:1 with media_assets).
            # Skip _compute_block_ranges to avoid re-computation skew.
            block_ranges = scene_ranges
            self.logger.info("Step 1/6: Using %d pre-computed scene ranges (1:1 with media_assets)", len(block_ranges))
        else:
            block_ranges = self._compute_block_ranges(bloques, timestamps)
            if not block_ranges:
                raise RuntimeError("No block ranges computed — cannot build video.")
            self.logger.info("Step 1/6: %d blocks with time ranges computed", len(block_ranges))

        # ── Create clips per block/range ───────────────────────
        block_clips: list[VideoClip] = []
        tts_duration = timestamps[-1].get("end", 0) if timestamps else 0

        # Collect the paths of all real (non-placeholder) assets as a
        # fallback pool so we never render the blue-text placeholder.
        _real_image_paths: list[Path] = []
        if media_assets:
            for a in media_assets:
                ap = a.get("path")
                if ap and a.get("type") in ("image", "video") and Path(ap).exists():
                    _real_image_paths.append(Path(ap))

        for i, br in enumerate(block_ranges):
            # When scene_ranges are pre-computed, media_assets align 1:1 by index.
            # Otherwise fall back to asset_idx mapping.
            asset = None
            if scene_ranges:
                asset = media_assets[i] if i < len(media_assets) else None
            else:
                asset_idx = br.get("asset_idx", i)
                asset = media_assets[asset_idx] if asset_idx < len(media_assets) else None

            if asset is None:
                asset = {"type": "placeholder", "path": None}

            clip = self._create_block_clip(br, asset, fallback_pool=_real_image_paths)

            if clip is None:
                # No real media for this scene — extend the PREVIOUS clip
                # instead of rendering the blue-text placeholder.
                if block_clips:
                    prev = block_clips[-1]
                    # Extend duration: keep the same clip, just longer.
                    try:
                        prev_dur = prev.duration() if callable(prev.duration) else prev.duration
                    except Exception:
                        prev_dur = br.get("start", 0)  # best guess
                    block_clips[-1] = prev.with_duration(prev_dur + br["duration"])
                    self.logger.info(
                        "  Scene %d [%s] merged with previous — no media available",
                        i + 1, br.get("tipo", "?"),
                    )
                else:
                    # First scene has no media at all — use a black placeholder
                    # (extreme edge case; should never happen with the fallback pool).
                    self.logger.warning(
                        "  Scene 1 has no media and nothing to merge with — using black clip"
                    )
                    clip = self._placeholder_clip(br["duration"])
                    clip = clip.with_start(br["start"])
                    block_clips.append(clip)
                continue

            clip = clip.with_start(br["start"])
            block_clips.append(clip)

            media_type = asset.get("type", "?")
            self.logger.info("  Scene %d [%s]: %.1f-%.1fs (%.1fs) media=%s",
                             i + 1, br.get("tipo", "?"), br["start"], br["end"],
                             br["duration"], media_type)

        # The body video duration may exceed the TTS narration when
        # inter-paragraph transition scenes are present. The last clip
        # covers the full body including transitions.
        if not block_clips:
            raise RuntimeError("No clips created — cannot build video.")

        # ── Composite with crossfades ──────────────────────────
        self.logger.info("Step 2/6: Compositing %d clips with crossfades…", len(block_clips))
        body_video = self._composite_scenes(block_clips)

        # ── Subtitles (toggleable) ─────────────────────────────
        subtitles_enabled = self.canal.get("SUBTITLES_ENABLED", True)
        if subtitles_enabled:
            self.logger.info("Step 3/6: Adding animated subtitles…")
            subtitle_clips = self._build_subtitles(timestamps, self.video_size)
            body_video = CompositeVideoClip(
                [body_video] + subtitle_clips, size=self.video_size,
            )
        else:
            self.logger.info("Step 3/6: Subtitles disabled (SUBTITLES_ENABLED=False)")

        # ── CTA audio: load first to determine duration ────────
        # The CTA voice-over can be 10+ seconds (full SCRIPT_END_HOOK),
        # so we measure it before building the visual clip. This prevents:
        #   - CTA visual ending mid-audio (black screen while narrator talks)
        #   - CTA audio bleeding into the OUTRO (outro visuals with CTA voice)
        cta_audio_clip = None
        cta_dur = 2.5  # fallback — silent CTA placeholder
        if cta_audio_path and Path(cta_audio_path).exists():
            try:
                cta_audio_clip = AudioFileClip(cta_audio_path)
                cta_dur = self._dur(cta_audio_clip)
                self.logger.info("Step 4/6: CTA voice loaded from %s (%.1fs)",
                                 cta_audio_path, cta_dur)
            except Exception as exc:
                self.logger.warning("Failed to load CTA audio %s: %s — using %ss silence",
                                    cta_audio_path, exc, cta_dur)
                cta_audio_clip = None

        # ── CTA clip (between body and outro) ──────────────────
        self.logger.info("Step 4/6: Building CTA clip (%.1fs)…", cta_dur)
        cta_clip = self._build_cta(cta_dur, cta_audio_path)

        # ── Intro + Outro ──────────────────────────────────────
        self.logger.info("Step 5/6: Adding intro + outro…")
        intro_dur = self.canal.get("INTRO_DURATION_SEC", self.INTRO_DURATION)
        outro_dur = self.canal.get("OUTRO_DURATION_SEC", self.OUTRO_DURATION)
        intro = self._build_intro(intro_dur)
        outro = self._build_outro(outro_dur)

        # ── Assemble final video sequence ──────────────────────
        # Sequence: INTRO → BODY (exact TTS length) → CTA → OUTRO
        video_parts: list[VideoClip] = []
        if intro is not None:
            intro = intro.with_audio(self._silent_audio(intro_dur))
            video_parts.append(intro)
        video_parts.append(body_video)
        if cta_clip is not None:
            video_parts.append(cta_clip)
        if outro is not None:
            outro = outro.with_audio(self._silent_audio(outro_dur))
            video_parts.append(outro)

        final_video = concatenate_videoclips(video_parts)

        # ── Assemble final audio sequence ──────────────────────
        # v2 with transitions: body audio = narration segments + transition silences + optional music
        # Sequence: intro_silence + BODY (narration + transitions) + CTA_audio + outro_silence
        audio_parts: list[AudioClip] = []
        # Intro silence (visual intro has no narration)
        audio_parts.append(self._silent_audio(intro_dur))

        # ── Build body audio: narration segments with transition silences ──
        tts_audio = AudioFileClip(audio_path)
        body_narr_parts: list[AudioClip] = []
        pos: float = 0.0  # cursor within the TTS audio (seconds)
        for br in block_ranges:
            if br.get("is_transition"):
                body_narr_parts.append(self._silent_audio(br["duration"]))
            else:
                seg_dur = br["duration"]
                available = max(0.0, tts_duration - pos)
                seg_dur = min(seg_dur, available)
                if seg_dur > 0.01:
                    body_narr_parts.append(tts_audio.subclipped(pos, pos + seg_dur))
                    pos += seg_dur
        body_narration = concatenate_audioclips(body_narr_parts)
        body_dur = self._dur(body_narration)
        self.logger.info("Body audio: %.1fs (narration %.1fs + transitions %.1fs)",
                         body_dur, tts_duration, body_dur - tts_duration)

        # ── Background music — generated in-situ, deleted after render ──
        # Applies ducking: music is louder during transitions/silence
        # and softer during narration, making transitions feel intentional.
        music_enabled = self.canal.get("BACKGROUND_MUSIC_ENABLED", False)
        _music_temp_path: Optional[Path] = None
        if music_enabled:
            try:
                _music_temp_path = self._generate_ambient_music(body_dur)
                music = AudioFileClip(str(_music_temp_path))
                music = music.with_duration(body_dur)

                music_vol_db = self.canal.get("BACKGROUND_MUSIC_VOLUME", -22.0)
                duck_vol_db = self.canal.get("BACKGROUND_MUSIC_DUCK_VOLUME", -28.0)

                # Build voice-activity slots from word timestamps (10ms granularity)
                # During transitions/silence → music_vol_db (louder)
                # During narration → duck_vol_db (softer)
                if timestamps:
                    try:
                        voice_slots = self._voice_active_slots(
                            timestamps, int(body_dur * 100)
                        )
                        music_factor = _db_to_linear(music_vol_db)
                        duck_factor = _db_to_linear(duck_vol_db)

                        def _duck_fn(t: float) -> float:
                            idx = np.clip(
                                (np.asarray(t) * 100).astype(int),
                                0, len(voice_slots) - 1,
                            )
                            result = np.where(voice_slots[idx], duck_factor, music_factor)
                            return result.item() if np.ndim(result) == 0 else result

                        if MOVIEPY_V2:
                            music = _dynamic_volume(music, _duck_fn)
                        else:
                            music = music.volumex(lambda t: _duck_fn(t))
                        self.logger.info(
                            "Ambient music + ducking: %.1f dB (narration) / %.1f dB (transitions)",
                            duck_vol_db, music_vol_db,
                        )
                    except Exception as duck_err:
                        self.logger.warning(
                            "Ducking automation failed (%s) — using flat %.1f dB",
                            duck_err, music_vol_db,
                        )
                        if MOVIEPY_V2:
                            music = music.with_effects(
                                [afx.MultiplyVolume(_db_to_linear(music_vol_db))]
                            )
                        else:
                            music = music.volumex(_db_to_linear(music_vol_db))
                else:
                    # No timestamps — flat volume
                    if MOVIEPY_V2:
                        music = music.with_effects(
                            [afx.MultiplyVolume(_db_to_linear(music_vol_db))]
                        )
                    else:
                        music = music.volumex(_db_to_linear(music_vol_db))

                body_audio = CompositeAudioClip([body_narration, music])
                self.logger.info("Ambient music generated (%.1fs)", body_dur)
            except Exception as exc:
                self.logger.warning("Ambient music generation failed (%s) — falling through without music", exc)
                body_audio = body_narration
        else:
            body_audio = body_narration

        audio_parts.append(body_audio)
        # CTA voice (if loaded) or silence matching cta_dur
        if cta_audio_clip is not None:
            audio_parts.append(cta_audio_clip)
        else:
            audio_parts.append(self._silent_audio(cta_dur))
        # Outro silence
        audio_parts.append(self._silent_audio(outro_dur))

        final_audio = concatenate_audioclips(audio_parts)
        final_video = final_video.with_audio(final_audio)

        # ── Render ─────────────────────────────────────────────
        self.logger.info("Step 6/6: Rendering MP4 (%dx%d, %d fps)…",
                         self.video_size[0], self.video_size[1], self.fps)
        render_ok = False
        _render_timeout = None  # infinite — no ceiling
        try:
            import concurrent.futures
            _ffmpeg_pids_before = _find_ffmpeg_pids()
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as _render_exec:
                _future = _render_exec.submit(
                    final_video.write_videofile,
                    str(output_path),
                    fps=self.fps,
                    codec=VIDEO_CODEC,
                    bitrate=VIDEO_BITRATE,
                    preset="medium",
                    audio_codec="aac",
                    threads=min(os.cpu_count() or 4, 8),  # cap to avoid memory exhaustion
                    ffmpeg_params=["-movflags", "+faststart"],
                )
                try:
                    _future.result(timeout=_render_timeout)
                    render_ok = True
                except concurrent.futures.TimeoutError:
                    self.logger.error(
                        "❌ Video rendering timed out after %ds — killing ffmpeg", _render_timeout
                    )
                    _kill_new_ffmpeg(_ffmpeg_pids_before)
        except Exception as exc:
            self.logger.error("❌ MoviePy render crashed: %s", exc)
            import traceback
            crash_log = output_path.with_suffix(".crash.log")
            crash_log.write_text(traceback.format_exc())
            self.logger.error("   Full traceback saved to %s", crash_log)
        finally:
            final_video.close()
            final_audio.close()
            # Clean up temp music file (generated in-situ)
            if _music_temp_path and _music_temp_path.exists():
                try:
                    _music_temp_path.unlink()
                    self.logger.info("🧹 Temp music deleted: %s", _music_temp_path.name)
                except Exception:
                    pass

        if render_ok and output_path.exists() and output_path.stat().st_size > 0:
            self.logger.info("✅ Video saved → %s (%.1f MB)",
                             output_path, output_path.stat().st_size / 1024 / 1024)
        else:
            self.logger.error("❌ Video file missing or empty: %s", output_path)
            raise RuntimeError(f"Video rendering failed: no output file at {output_path}")

        return output_path

    # ── v2: Block-based clip creation ──────────────────────────

    def _compute_block_ranges(
        self, bloques: list[dict], timestamps: list[dict]
    ) -> list[dict]:
        """Partition word-level timestamps into per-block time ranges.

        Matches each block's word count against the sequential timestamps.
        Falls back to proportional allocation on mismatch.

        After initial ranges are computed, enforces SCENE_DURATION_MIN and
        SCENE_DURATION_MAX: short scenes are merged with neighbours and
        long scenes are split into sub-scenes.

        Timestamps may arrive in either format (start_ms/end_ms in milliseconds
        or start/end in seconds).  This method normalises both to seconds before
        computing ranges so that scene durations always match the actual
        narration timing.
        """
        # ── Normalise timestamps to seconds ────────────────────
        # The TTS engines (edge-tts, Kokoro) return start_ms / end_ms
        # in milliseconds, but the rest of the pipeline expects
        # start / end in seconds.  This block must be called *before*
        # any code that reads the ``start`` / ``end`` keys.
        if timestamps:
            sample = timestamps[0]
            if "start_ms" in sample or "end_ms" in sample:
                norm: list[dict] = []
                for ts in timestamps:
                    nt = dict(ts)
                    nt["start"] = nt.pop("start_ms", 0.0) / 1000.0
                    nt["end"] = nt.pop("end_ms", 0.0) / 1000.0
                    if "duration_ms" in nt:
                        nt["duration"] = nt.pop("duration_ms") / 1000.0
                    norm.append(nt)
                timestamps = norm

        if not bloques or not timestamps:
            return []

        # Clean each block's text and count words
        block_word_counts = []
        for b in bloques:
            clean = re.sub(r'\s+', ' ', b.get("texto", "")).strip()
            block_word_counts.append(len(clean.split()) if clean else 0)

        total_block_words = sum(block_word_counts)
        total_ts_words = len(timestamps)

        # If word counts roughly match (within 20%), use word counting
        if total_block_words > 0 and abs(total_block_words - total_ts_words) / max(total_ts_words, 1) < 0.2:
            ranges = []
            word_idx = 0
            total_audio_end = timestamps[-1].get("end", 0)
            for i, (bloque, n_words) in enumerate(zip(bloques, block_word_counts)):
                if n_words == 0:
                    continue
                start_time = timestamps[min(word_idx, total_ts_words - 1)].get("start", 0)
                end_idx = min(word_idx + n_words - 1, total_ts_words - 1)
                end_time = timestamps[end_idx].get("end", start_time + 1)

                # Force last block to cover full remaining audio
                is_last = (i == len(bloques) - 1)
                if is_last and total_audio_end > end_time:
                    end_time = total_audio_end

                ranges.append({
                    "start": start_time,
                    "end": end_time,
                    "duration": end_time - start_time,
                    "tipo": bloque.get("tipo", "desarrollo"),
                    "texto": bloque.get("texto", ""),
                    "media_tipo": bloque.get("media_tipo", "imagen"),
                    "media_duracion": bloque.get("media_duracion", 5),
                    "search_query_en": bloque.get("search_query_en", ""),
                    "asset_idx": i,  # track original asset index for sub-scene reuse
                })
                word_idx += n_words

            # Safety: extend last range to audio end
            if ranges and total_audio_end > 0:
                ranges[-1]["end"] = max(ranges[-1]["end"], total_audio_end)
                ranges[-1]["duration"] = ranges[-1]["end"] - ranges[-1]["start"]
            return self._enforce_scene_durations(ranges)

        # Fallback: proportional allocation based on text length
        total_audio = timestamps[-1].get("end", 60) if timestamps else 60
        lengths = [max(len(b.get("texto", "")), 1) for b in bloques]
        total_len = sum(lengths)

        ranges = []
        cumulative = 0.0
        for i, bloque in enumerate(bloques):
            dur = (lengths[i] / total_len) * total_audio
            ranges.append({
                "start": cumulative,
                "end": cumulative + dur,
                "duration": dur,
                "tipo": bloque.get("tipo", "desarrollo"),
                "texto": bloque.get("texto", ""),
                "media_tipo": bloque.get("media_tipo", "imagen"),
                "media_duracion": bloque.get("media_duracion", 5),
                "search_query_en": bloque.get("search_query_en", ""),
                "asset_idx": i,
            })
            cumulative += dur
        return self._enforce_scene_durations(ranges)

    # ── Scene duration enforcement ──────────────────────────────

    def _enforce_scene_durations(self, block_ranges: list[dict]) -> list[dict]:
        """Enforce SCENE_DURATION_MIN / SCENE_DURATION_MAX on block ranges.

        - Scenes shorter than SCENE_DURATION_MIN are merged with the next scene.
        - Scenes longer than SCENE_DURATION_MAX are split into sub-scenes.
        - Merged scenes inherit the first scene's asset_idx and tipo; the second
          scene's asset is skipped (its media will not be used in this build).
        - Split sub-scenes inherit the parent's asset_idx and tipo.
        """
        min_dur = self.canal.get("SCENE_DURATION_MIN", self.SCENE_DURATION_MIN)
        max_dur = self.canal.get("SCENE_DURATION_MAX", self.SCENE_DURATION_MAX)

        # ---- Phase 1: Merge short scenes (repeat until stable) ----
        # Repeatedly scan the list and merge any scene whose duration
        # falls below min_dur with the next scene.  The outer loop runs
        # at most len(block_ranges) times (each iteration reduces the
        # list by at least one element when a merge happens).
        merged = list(block_ranges)  # work on a mutable copy
        changed = True
        safety = 0
        while changed and safety < len(block_ranges) + 2:
            changed = False
            safety += 1
            new_list: list[dict] = []
            i = 0
            while i < len(merged):
                br = dict(merged[i])
                if br["duration"] < min_dur and i + 1 < len(merged):
                    # Merge this short scene with the next one
                    nxt = merged[i + 1]
                    br["end"] = nxt["end"]
                    br["duration"] = br["end"] - br["start"]
                    br["texto"] = br.get("texto", "") + " " + nxt.get("texto", "")
                    self.logger.info(
                        "Merged short scene (%.1fs < %.1fs) with next → %.1fs",
                        merged[i]["duration"], min_dur, br["duration"],
                    )
                    i += 2
                    changed = True
                else:
                    i += 1
                new_list.append(br)
            merged = new_list

        # ---- Phase 1b: Backward-merge last scene ------------------
        # The forward loop above can only merge scene[i] with scene[i+1],
        # so the last scene never gets a chance to be swallowed when it
        # is the one that is too short.  Here we merge the last scene
        # backward into the previous one when it still falls below the
        # minimum duration.
        if (len(merged) >= 2
                and merged[-1]["duration"] < min_dur
                and not any(r.get("is_subscene") for r in merged[-2:])):
            prev = merged[-2]
            last = merged.pop()
            prev["end"] = last["end"]
            prev["duration"] = prev["end"] - prev["start"]
            prev["texto"] = prev.get("texto", "") + " " + last.get("texto", "")
            self.logger.info(
                "Merged last short scene (%.1fs) backward into previous → %.1fs",
                last["duration"], prev["duration"],
            )

        # ---- Phase 2: Split long scenes ----
        new_ranges: list[dict] = []
        for br in merged:
            if br["duration"] > max_dur:
                num_subscenes = int(br["duration"] / max_dur) + 1
                sub_dur = br["duration"] / num_subscenes
                self.logger.info(
                    "Splitting long scene (%.1fs > %.1fs) into %d sub-scenes of %.1fs each",
                    br["duration"], max_dur, num_subscenes, sub_dur,
                )
                for j in range(num_subscenes):
                    sub = dict(br)
                    sub["start"] = br["start"] + j * sub_dur
                    sub["end"] = br["start"] + (j + 1) * sub_dur
                    sub["duration"] = sub["end"] - sub["start"]
                    sub["is_subscene"] = True
                    sub["parent_tipo"] = br["tipo"]
                    # Preserve parent asset_idx so sub-scenes reuse the same asset
                    new_ranges.append(sub)
            else:
                br = dict(br)
                br["is_subscene"] = False
                new_ranges.append(br)

        return new_ranges

    def _create_block_clip(self, block_range: dict, asset: dict,
                           fallback_pool: list = None) -> Optional[VideoClip]:
        """Create a clip for one block: video, image, or None (no real media).

        When no real media is available (placeholder / duplicate / missing path),
        returns ``None`` so the caller can merge the scene with its neighbour
        instead of rendering the blue-text placeholder.

        Deduplication:
        - Images: full dedup — same image used twice is visually repetitive.
        - Videos: offset tracking — different scenes get different segments
          of the same source video file (no more frozen/looped frames).
        """
        media_type = asset.get("type", "placeholder")
        block_dur = block_range["duration"]
        asset_path = str(asset.get("path", "")) if asset.get("path") else ""
        content_hash = asset.get("content_hash", "")

        # ---- Image deduplication check ----
        if media_type == "image" and asset_path and asset_path in self._used_asset_paths:
            self.logger.warning(
                "Image %s already used — returning None (caller will merge)",
                asset_path,
            )
            return None
        if media_type == "image" and content_hash and content_hash in self._used_asset_paths:
            self.logger.warning(
                "Image content hash %s already used — returning None",
                content_hash[:12],
            )
            return None

        # Initialize per-video color grade on first non-placeholder clip (P3)
        if self._video_color_grade is None and media_type not in ("placeholder", "duplicate"):
            self._video_color_grade = {
                "contrast": random.uniform(1.05, 1.15),
                "brightness": random.uniform(0.95, 1.05),
                "saturation": random.uniform(0.90, 1.10),
            }
            self.logger.info("Per-video color grade set: %s", self._video_color_grade)

        clip: Optional[VideoClip] = None
        if media_type == "video" and asset_path and Path(asset_path).exists():
            # ── Video: offset tracking ──────────────────────
            # Instead of dedup (which froze/looped after 1 scene), each scene
            # takes the next available segment from the source video.
            offset = self._video_offset_tracker.get(asset_path, 0.0)
            clip = self._video_clip_for_block(Path(asset_path), block_dur, start_offset=offset)
            if clip is None:
                # Source exhausted or too short — fall back to image merge
                self.logger.info(
                    "Video %s exhausted at offset %.1fs — falling back to image",
                    Path(asset_path).name, offset,
                )
                return None
            else:
                # Advance the offset so the next scene (even with same file)
                # starts where this one ended.
                self._video_offset_tracker[asset_path] = offset + block_dur
                self._used_asset_paths.add(asset_path)
                if content_hash:
                    self._used_asset_paths.add(content_hash)
        if clip is None and media_type == "image" and asset_path and Path(asset_path).exists():
            clip = self._image_clip_for_block(Path(asset_path), block_dur)
            self._used_asset_paths.add(asset_path)
            if content_hash:
                self._used_asset_paths.add(content_hash)

        if clip is None:
            # No real media for this scene — return None so the caller
            # merges this scene with the previous one instead of showing
            # the blue-text placeholder. The caller has a fallback_pool
            # of real image paths from other scenes.
            self.logger.info("Scene has no real media — caller will extend previous clip")
            return None

        # Apply uniform color grading for visual coherence (P3)
        if self._video_color_grade and media_type not in ("placeholder", "duplicate"):
            try:
                import numpy as np
                cg = self._video_color_grade
                if MOVIEPY_V2:
                    clip = clip.with_effects([
                        vfx.ColorCorrection(
                            contrast=cg["contrast"],
                            brightness=cg["brightness"],
                            saturation=cg["saturation"],
                        )
                    ])
                else:
                    clip = clip.fx(vfx.ColorCorrection,
                                   contrast=cg["contrast"],
                                   brightness=cg["brightness"],
                                   saturation=cg["saturation"])
            except Exception as exc:
                self.logger.debug("Color grading not applied (effect unavailable): %s", exc)

        return clip

    def _video_clip_for_block(self, video_path: Path, block_dur: float,
                               start_offset: float = 0.0) -> Optional[VideoClip]:
        """Load a video clip and trim to match block duration from a given offset.

        **No looping**: if the source video runs out before reaching
        ``start_offset + block_dur`` the method returns ``None`` so the
        caller can fall back to a static image.  Each scene in a split
        block advances ``start_offset`` so successive scenes play different
        segments of the same source file.

        Args:
            video_path: Absolute path to the source video file.
            block_dur:  Desired scene duration in seconds.
            start_offset:  Offset in seconds into the source where
                           this scene should begin.
        """
        try:
            clip = VideoFileClip(str(video_path))
            clip_dur = clip.duration() if callable(clip.duration) else clip.duration

            if clip_dur >= start_offset + block_dur:
                # Enough source material — extract the requested segment
                clip = clip.subclipped(start_offset, start_offset + block_dur)
            else:
                # Source exhausted for this scene
                self.logger.warning(
                    "Video %s exhausted (%.1fs < offset %.1f + %.1fs) — falling back to image",
                    video_path, clip_dur, start_offset, block_dur,
                )
                clip.close()
                return None

            # Resize to video dimensions
            clip = clip.resized(self.video_size)
            return clip
        except Exception as exc:
            self.logger.exception("VideoFileClip failed for %s, falling back to placeholder", video_path)
            return self._placeholder_clip(block_dur)

    def _image_clip_for_block(self, image_path: Path, block_dur: float) -> VideoClip:
        """Create a Ken Burns image clip for the given duration."""
        zoom = random.uniform(
            self.canal.get("KEN_BURNS_ZOOM_MIN", KEN_BURNS_ZOOM_MIN),
            self.canal.get("KEN_BURNS_ZOOM_MAX", KEN_BURNS_ZOOM_MAX),
        )
        return self._single_ken_burns_clip(image_path, block_dur, zoom)

    def _placeholder_clip_with_text(self, block_range: dict, block_dur: float) -> VideoClip:
        """Solid color placeholder with the first sentence of the block text overlaid."""
        try:
            texto = block_range.get("texto", "")[:120]
            if not texto:
                return self._placeholder_clip(block_dur)

            font = self._resolve_font()
            color_pal = self.canal.get("COLOR_PALETTE", COLOR_PALETTE)
            text_color = tuple(color_pal.get("text", (225, 220, 215)))
            bg_color = tuple(color_pal.get("secondary", (12, 10, 10)))

            img = Image.new("RGB", self.video_size, bg_color)
            draw = ImageDraw.Draw(img)
            try:
                pil_font = ImageFont.truetype(font, 36)
            except Exception:
                pil_font = ImageFont.load_default()

            # Word wrap
            lines = []
            words = texto.split()
            current = ""
            for w in words:
                test = f"{current} {w}".strip()
                bbox = pil_font.getbbox(test)
                if bbox[2] - bbox[0] < self.video_size[0] * 0.8:
                    current = test
                else:
                    lines.append(current)
                    current = w
            if current:
                lines.append(current)

            y = self.video_size[1] // 2 - len(lines) * 20
            for line in lines:
                bbox = pil_font.getbbox(line)
                x = (self.video_size[0] - (bbox[2] - bbox[0])) // 2
                draw.text((x, y), line, font=pil_font, fill=text_color)
                y += 42

            arr = np.array(img)

            def make_frame(t: float) -> np.ndarray:
                return arr

            return VideoClip(make_frame, duration=block_dur)
        except Exception:
            return self._placeholder_clip(block_dur)

    def _normalize_timestamps(self, timestamps: list[dict]) -> list[dict]:
        """Normalize timestamps to use 'start'/'end' keys in seconds.

        Handles both millisecond-based (start_ms, end_ms) and second-based
        (start, end) formats, converting everything to seconds.
        """
        if not timestamps:
            return []

        # Detect if timestamps are in ms (have start_ms/end_ms keys)
        sample = timestamps[0]
        if "start_ms" in sample or "end_ms" in sample:
            norm = []
            for ts in timestamps:
                nt = dict(ts)
                nt["start"] = nt.pop("start_ms", 0) / 1000.0
                nt["end"] = nt.pop("end_ms", 0) / 1000.0
                if "duration_ms" in nt:
                    nt["duration"] = nt.pop("duration_ms") / 1000.0
                norm.append(nt)
            return norm

        # Already in seconds or unknown format — return as-is
        return timestamps

    def _single_ken_burns_clip(
        self, image_path: Path, duration: float, zoom_percent: float
    ) -> VideoClip:
        """Create one ImageClip with Ken Burns zoom/pan baked in via a custom
        ``make_frame`` function.

        The frame function progressively zooms into the image over the clip
        lifetime and applies a gentle pan chosen at random.
        """
        try:
            pil_img = Image.open(image_path).convert("RGB")
        except Exception:
            self.logger.exception("Failed to open %s", image_path)
            return self._placeholder_clip(duration)

        target_w, target_h = self.video_size
        src_w, src_h = pil_img.size

        if src_w < target_w or src_h < target_h:
            ratio = max(target_w / src_w, target_h / src_h)
            new_w, new_h = int(src_w * ratio), int(src_h * ratio)
            pil_img = pil_img.resize((new_w, new_h), Image.LANCZOS)
            src_w, src_h = new_w, new_h

        zoom_factor = 1.0 + zoom_percent / 100.0

        pan_dir_x = random.choice([-1, 0, 1])
        pan_dir_y = random.choice([-1, 0, 1]) if pan_dir_x == 0 else 0

        def make_frame(t: float) -> np.ndarray:
            progress = t / duration if duration > 0 else 0
            z = 1.0 + (zoom_factor - 1.0) * progress

            new_w = int(src_w * z)
            new_h = int(src_h * z)
            scaled = pil_img.resize((new_w, new_h), Image.LANCZOS)

            max_ox = max(0, (new_w - target_w) / 2)
            max_oy = max(0, (new_h - target_h) / 2)

            ox = max_ox + max_ox * pan_dir_x * progress * 0.5
            oy = max_oy + max_oy * pan_dir_y * progress * 0.5

            left = int((new_w - target_w) / 2 + ox)
            top = int((new_h - target_h) / 2 + oy)
            left = max(0, min(left, new_w - target_w))
            top = max(0, min(top, new_h - target_h))

            cropped = scaled.crop((left, top, left + target_w, top + target_h))
            return np.array(cropped)

        return VideoClip(make_frame, duration=duration)

    def _placeholder_clip(self, duration: float) -> VideoClip:
        """Return a plain black ImageClip for missing images."""
        img = Image.new("RGB", self.video_size, (0, 0, 0))
        arr = np.array(img)

        def make_frame(t: float) -> np.ndarray:
            return arr

        return VideoClip(make_frame, duration=duration)

    # ── Scene compositing ───────────────────────────────────────

    def _composite_scenes(self, clips: list[VideoClip]) -> CompositeVideoClip:
        """Layer clips in a ``CompositeVideoClip`` with random crossfade durations.

        Each clip starts *x* seconds before the previous one ends,
        where *x* is a random crossfade between 0.3–0.7 s.

        After compositing, the returned clip is guaranteed to span exactly
        the total duration of all input clips (the narration audio length).
        Any remaining gap at the end is filled with a transparent placeholder
        so the composite stays locked to the TTS timeline.
        """

        crossfade_duration = random.uniform(
            self.canal.get("CROSSFADE_MIN", 0.3),
            self.canal.get("CROSSFADE_MAX", 0.7),
        )

        # Total span that the composite MUST cover (sum of all scene durations).
        # This is the length of the TTS narration that this composite backs.
        total_scenes_dur = sum(self._dur(c) for c in clips)

        positioned: list[VideoClip] = []
        cursor = 0.0

        for i, clip in enumerate(clips):
            start = max(0.0, cursor - (crossfade_duration if i > 0 else 0.0))
            dur = self._dur(clip)

            if i > 0:
                try:
                    clip = self._apply_crossfade_in(clip, crossfade_duration)
                except Exception:
                    self.logger.exception("Crossfade-in failed for scene %d", i)

                try:
                    prev = positioned[-1]
                    positioned[-1] = self._apply_crossfade_out(prev, crossfade_duration)
                except Exception:
                    self.logger.exception("Crossfade-out failed for scene %d", i - 1)

            clip = clip.with_start(start)
            positioned.append(clip)
            cursor = start + dur

        # ── Pad to match total scenes duration ──────────────────
        # Because of crossfade overlaps the composite ends at
        #   cursor = sum(durs) - (N-1) * crossfade
        # which is shorter than the total narration audio.  Extend the
        # last clip so the body_video timeline stays perfectly aligned
        # with the TTS audio.
        composite_end = cursor
        if composite_end < total_scenes_dur and positioned:
            pad_dur = total_scenes_dur - composite_end
            last_clip = positioned[-1]
            try:
                last_dur = self._dur(last_clip)
            except Exception:
                last_dur = 0.0
            positioned[-1] = last_clip.with_duration(last_dur + pad_dur)
            self.logger.debug(
                "Composite padded by %.2fs to match narration length (crossfade=%.2fs × %d scenes)",
                pad_dur, crossfade_duration, len(clips),
            )

        return CompositeVideoClip(positioned, size=self.video_size)

    def _apply_crossfade_in(self, clip: VideoClip, duration: float) -> VideoClip:
        if MOVIEPY_V2:
            return clip.with_effects([vfx.CrossFadeIn(duration)])
        else:
            return clip.crossfadein(duration)

    def _apply_crossfade_out(self, clip: VideoClip, duration: float) -> VideoClip:
        if MOVIEPY_V2:
            return clip.with_effects([vfx.CrossFadeOut(duration)])
        else:
            return clip.crossfadeout(duration)

    # ── Audio ───────────────────────────────────────────────────

    @staticmethod
    def _dur(clip) -> float:
        """Get clip duration, compatible with MoviePy v1 (property) and v2 (method)."""
        d = clip.duration
        return d() if callable(d) else d

    def _build_audio(
        self,
        audio_path: str,
        timestamps: list[dict],
        total_duration: float,
        music_volume: float,
    ) -> CompositeAudioClip:
        """Create voice + background music audio track with ducking.

        Ducking strategy:
            - Music plays at ``music_volume`` dB in silent gaps.
            - Music ducks to -25 dB during voice narration.
            - Voice track is passed through at unity gain.

        When no background music files are found the voice track is returned
        as-is.
        """
        try:
            voice = AudioFileClip(audio_path)
        except Exception:
            self.logger.exception("Cannot load voice audio %s", audio_path)
            return self._silent_audio(total_duration)

        music_paths = self._find_music_files()
        if not music_paths:
            self.logger.warning("No background music files found — skipping music.")
            return voice

        try:
            music = AudioFileClip(random.choice(music_paths))
            target_dur = max(self._dur(voice), total_duration)
            music = music.with_duration(target_dur)
            if self._dur(music) < target_dur:
                music = music.with_effects([afx.AudioLoop(duration=target_dur)])
        except Exception:
            self.logger.exception("Cannot load background music — skipping.")
            return voice

        voice_dur = self._dur(voice)
        effective_dur = max(voice_dur, total_duration)
        voice = voice.with_duration(effective_dur)

        voice_active_slots = self._voice_active_slots(timestamps, int(effective_dur * 100))

        music_db = music_volume
        duck_db = -25.0

        music_factor = _db_to_linear(music_db)
        duck_factor = _db_to_linear(duck_db)

        def volume_fn(t: float) -> float:
            idx = np.clip(
                (np.asarray(t) * 100).astype(int),
                0, len(voice_active_slots) - 1,
            )
            result = np.where(voice_active_slots[idx], duck_factor, music_factor)
            return result.item() if np.ndim(result) == 0 else result

        try:
            if MOVIEPY_V2:
                music = _dynamic_volume(music, volume_fn)
            else:
                music = music.volumex(lambda t: volume_fn(t))
        except Exception:
            self.logger.exception("Volume automation failed — using flat music level.")
            music = music.with_effects([afx.MultiplyVolume(_db_to_linear(music_volume))]) if MOVIEPY_V2 else music.volumex(_db_to_linear(music_volume))

        return CompositeAudioClip([voice, music])

    @staticmethod
    def _voice_active_slots(
        timestamps: list[dict], num_slots: int
    ) -> np.ndarray:
        """Build a boolean array indicating voice activity every 10 ms.

        A slot is ``True`` whenever any word timestamp covers it.
        """
        active = np.zeros(num_slots, dtype=bool)
        for ts in timestamps:
            start = int(ts.get("start", 0) * 100)
            end = int(ts.get("end", 0) * 100)
            start = max(0, start)
            end = min(num_slots, end)
            if end > start:
                active[start:end] = True
        return active

    @staticmethod
    def _find_music_files() -> list[Path]:
        """Return sorted list of audio files in ``ASSETS_DIR/music``.
        
        .. deprecated:: kept for backward compatibility with legacy path.
        """
        music_dir = ASSETS_DIR / "music"
        if not music_dir.is_dir():
            return []
        return sorted(
            p
            for p in music_dir.iterdir()
            if p.suffix.lower() in (".mp3", ".wav", ".ogg", ".m4a", ".aac", ".flac")
        )

    def _generate_ambient_music(self, duration_sec: float) -> Path:
        """Generate a subtle ambient drone track via numpy + pydub.

        Creates a low-frequency ambient bed (55-65 Hz fundamental with soft
        harmonics) that evolves slowly via amplitude modulation (~0.3 Hz).
        No external API or network access required — the entire waveform is
        synthesised in-process.

        The file is written to a temp location and the caller is responsible
        for deleting it after the video render completes.
        """
        import numpy as np
        from pydub import AudioSegment

        sample_rate = 44100
        n_samples = int(sample_rate * duration_sec)

        # Time array
        t = np.linspace(0, duration_sec, n_samples, endpoint=False, dtype=np.float32)

        # ── Ambient drone: fundamental + soft harmonics ──────────
        # Stereo: left and right channels with slight phase difference
        fund_l = np.sin(2 * np.pi * 58.0 * t, dtype=np.float32)
        fund_r = np.sin(2 * np.pi * 58.0 * t + 0.15, dtype=np.float32)

        harm_l = 0.35 * np.sin(2 * np.pi * 87.0 * t, dtype=np.float32)
        harm_r = 0.35 * np.sin(2 * np.pi * 87.0 * t + 0.35, dtype=np.float32)

        octave_l = 0.15 * np.sin(2 * np.pi * 116.0 * t, dtype=np.float32)
        octave_r = 0.15 * np.sin(2 * np.pi * 116.0 * t + 0.25, dtype=np.float32)

        # Slow amplitude swell (0.25-0.35 Hz) for organic feel
        swell = 0.55 + 0.45 * np.sin(
            2 * np.pi * 0.28 * t + np.sin(2 * np.pi * 0.09 * t) * 1.5,
            dtype=np.float32,
        )

        left = (fund_l + harm_l + octave_l) * swell
        right = (fund_r + harm_r + octave_r) * swell

        # Fade in/out to avoid clicks
        fade_samples = int(sample_rate * 0.5)  # 0.5s fade
        if fade_samples < n_samples:
            ramp = np.linspace(0, 1, fade_samples, dtype=np.float32)
            left[:fade_samples] *= ramp
            right[:fade_samples] *= ramp
            left[-fade_samples:] *= ramp[::-1]
            right[-fade_samples:] *= ramp[::-1]

        # Normalise and convert to 16-bit PCM
        peak = max(np.max(np.abs(left)), np.max(np.abs(right)), 1e-12)
        left = (left / peak * 32767 * 0.7).astype(np.int16)
        right = (right / peak * 32767 * 0.7).astype(np.int16)

        # Interleave stereo
        stereo = np.empty((n_samples, 2), dtype=np.int16)
        stereo[:, 0] = left
        stereo[:, 1] = right

        # Export via pydub
        audio = AudioSegment(
            stereo.tobytes(),
            frame_rate=sample_rate,
            sample_width=2,
            channels=2,
        )

        out_path = OUTPUT_DIR / "temp" / f"ambient_{int(time.time())}.mp3"
        out_path.parent.mkdir(parents=True, exist_ok=True)
        audio.export(str(out_path), format="mp3", bitrate="128k")
        self.logger.info(
            "Ambient music generated: %s (%.1fs, %.0f KB)",
            out_path.name, duration_sec, out_path.stat().st_size / 1024,
        )
        return out_path

    def _silent_audio(self, duration: float) -> AudioClip:
        """Create a silent AudioClip of *duration* seconds."""
        if MOVIEPY_V2:
            return AudioClip(
                lambda t: np.zeros((2,)),
                duration=duration,
                fps=self.fps,
            )
        else:
            frame_fn = lambda t: np.zeros((2,))
            return AudioClip(make_frame=frame_fn, duration=duration, fps=self.fps)

    # ── Subtitles ───────────────────────────────────────────────

    PHRASE_GAP_THRESHOLD: float = 0.4  # seconds of silence to start a new phrase
    MAX_PHRASE_CHARS: int = 50         # flush phrase when text exceeds this width

    def _extract_phrases(self, timestamps: list[dict]) -> list[dict]:
        """Group word timestamps into phrases for subtitle/image pacing.

        Returns list of dicts: {"start": float, "end": float, "text": str}
        Used by both subtitle rendering and image-clip scheduling.
        """
        phrases: list[dict] = []
        buf_words: list[str] = []
        buf_start: float = 0.0
        buf_end: float = 0.0

        for i, ts in enumerate(timestamps):
            word = str(ts.get("word", ""))
            w_start = float(ts.get("start", 0))
            w_end = float(ts.get("end", 0))
            if not word or w_end <= w_start:
                continue
            if word.upper() in ("PAUSA", "PAUSE", "SILENCIO"):
                if buf_words:
                    phrases.append({"start": buf_start, "end": buf_end, "text": " ".join(buf_words)})
                    buf_words, buf_start, buf_end = [], 0.0, 0.0
                continue

            if not buf_words:
                buf_start = w_start
            buf_words.append(word.strip(".,!?;:¡¿"))
            buf_end = w_end

            if i < len(timestamps) - 1:
                next_start = float(timestamps[i + 1].get("start", 0))
                gap = next_start - w_end
                tentative = " ".join(buf_words + [str(timestamps[i + 1].get("word", "")).strip(".,!?;:¡¿")])
                if gap > self.canal.get("SUBTITLE_PHRASE_GAP", self.PHRASE_GAP_THRESHOLD) or len(tentative) > self.canal.get("SUBTITLE_MAX_CHARS", self.MAX_PHRASE_CHARS):
                    phrases.append({"start": buf_start, "end": buf_end, "text": " ".join(buf_words)})
                    buf_words, buf_start, buf_end = [], 0.0, 0.0

        if buf_words:
            phrases.append({"start": buf_start, "end": buf_end, "text": " ".join(buf_words)})
        return phrases

    def _build_subtitles(
        self, timestamps: list[dict], video_size: tuple[int, int]
    ) -> list[VideoClip]:
        """Create phrase-by-phrase animated subtitle clips.

        Uses ``_extract_phrases`` for phrase grouping, then creates a
        single PIL-rendered TextClip per phrase with pop-in animation.
        """
        font = self._resolve_font()
        text_color = tuple(
            self.canal.get("COLOR_PALETTE", COLOR_PALETTE).get("text", (230, 230, 230))
        )
        shadow_color = tuple(
            self.canal.get("COLOR_PALETTE", COLOR_PALETTE).get("text_shadow", (10, 10, 10))
        )
        subtitle_clips: list[VideoClip] = []

        for phrase in self._extract_phrases(timestamps):
            phrase_dur = phrase["end"] - phrase["start"]
            if phrase_dur <= 0:
                continue
            try:
                txt = self._make_text_clip(
                    phrase["text"], font, text_color, shadow_color, video_size
                )
                txt = txt.with_start(phrase["start"]).with_duration(phrase_dur)
                txt = self._apply_pop_in(txt, phrase_dur)
                subtitle_clips.append(txt)
            except Exception:
                self.logger.exception("Skipping subtitle phrase at %.1f s", phrase["start"])

        return subtitle_clips

    def _make_text_clip(
        self,
        text: str,
        font: str,
        color: tuple[int, int, int],
        shadow_color: tuple[int, int, int],
        video_size: tuple[int, int],
    ) -> VideoClip:
        """Build a subtitle TextClip using PIL for precise wrapping + centering.

        Draws text onto a full-frame transparent RGBA canvas.  Shadow is
        rendered by drawing dark text offset 3 px behind the main text.
        The text block is anchored near the bottom of the frame so
        multiline phrases never get clipped at the top.
        """
        font_size = self.canal.get("SUBTITLE_FONT_SIZE", 52)
        max_width = int(video_size[0] * 0.88)  # 88% of 1920 ≈ 1690 px
        vw, vh = video_size
        # Vertical anchor: bottom of text block at 92% of frame height
        bottom_y = int(vh * 0.92)
        padding_bottom = 30

        try:
            pil_font = ImageFont.truetype(font, font_size)
        except Exception:
            pil_font = ImageFont.load_default()

        # Wrap text to fit max_width using PIL's getbbox
        lines = []
        for paragraph in text.split("\n"):
            words = paragraph.split()
            current_line = ""
            for word in words:
                test_line = f"{current_line} {word}".strip()
                bbox = pil_font.getbbox(test_line)
                line_w = bbox[2] - bbox[0]
                if line_w <= max_width:
                    current_line = test_line
                else:
                    if current_line:
                        lines.append(current_line)
                    current_line = word
            if current_line:
                lines.append(current_line)

        if not lines:
            lines = [text]

        # Measure total text block height
        line_height = font_size + 8
        total_text_h = line_height * len(lines)
        start_y = bottom_y - total_text_h

        # Render onto full-frame transparent RGBA canvas
        canvas = Image.new("RGBA", (vw, vh), (0, 0, 0, 0))
        draw = ImageDraw.Draw(canvas)
        shadow_off = max(2, font_size // 20)

        for i, line in enumerate(lines):
            bbox = pil_font.getbbox(line)
            line_w = bbox[2] - bbox[0]
            x = (vw - line_w) // 2
            y = start_y + i * line_height

            # Shadow (offset dark text behind)
            draw.text((x + shadow_off, y + shadow_off), line, font=pil_font, fill=shadow_color + (255,))
            draw.text((x - 1, y - 1), line, font=pil_font, fill=shadow_color + (200,))
            # Main text
            draw.text((x, y), line, font=pil_font, fill=color + (255,))

        frame = np.array(canvas)

        def make_frame(t: float) -> np.ndarray:
            return frame

        return VideoClip(make_frame, duration=1.0)  # actual duration set by caller

    def _apply_pop_in(self, clip: VideoClip, duration: float) -> VideoClip:
        """Scale animation: pop_start → pop_end over phrase duration."""
        pop_start = self.canal.get("SUBTITLE_POP_START", 0.95)
        pop_end = self.canal.get("SUBTITLE_POP_END", 1.05)
        try:
            if MOVIEPY_V2:
                return clip.with_effects([
                    vfx.Resize(lambda t: pop_start + (pop_end - pop_start) * (t / duration) if duration > 0 else 1.0)
                ])
            else:
                return clip.resize(
                    lambda t: pop_start + (pop_end - pop_start) * (t / duration) if duration > 0 else 1.0
                )
        except Exception:
            return clip

    @staticmethod
    def _resolve_font() -> str:
        """Return the first available font from a preferred list."""
        candidates = [
            "DejaVu-Sans",
            "DejaVu Sans",
            "Arial",
            "Helvetica",
            "Liberation-Sans",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
        ]
        for name in candidates:
            if os.path.exists(name):
                return name
            if any(Path("/usr/share/fonts").rglob(f"*{name}*")):
                return name
        return "DejaVu-Sans"

    # ── Film grain ──────────────────────────────────────────────

    def _create_grain_clip(
        self, duration: float, size: tuple[int, int]
    ) -> VideoClip:
        """Generate a looping film-grain overlay from procedural numpy noise.

        Pre-generates noise frames then loops them to avoid computing
        noise on every rendered frame.  Returns RGBA (4-channel) arrays
        so MoviePy v2 composites the grain as a semi-transparent overlay.
        """
        grain_opacity = self.canal.get("FILM_GRAIN_OPACITY", FILM_GRAIN_OPACITY) / 100.0
        grain_frames = self.canal.get("FILM_GRAIN_FRAMES", 12)
        alpha_val = int(grain_opacity * 255)

        frames: list[np.ndarray] = []
        rng = np.random.RandomState(42)
        for _ in range(grain_frames):
            noise = rng.randint(0, 256, (*size[::-1], 3), dtype=np.uint8)
            alpha = np.full((*size[::-1], 1), alpha_val, dtype=np.uint8)
            rgba = np.concatenate([noise, alpha], axis=2)
            frames.append(rgba)

        def make_frame(t: float) -> np.ndarray:
            idx = int(t * self.fps) % grain_frames
            return frames[idx]

        return VideoClip(make_frame, duration=duration)

    # ── Vignette ────────────────────────────────────────────────

    def _create_vignette_clip(
        self, duration: float, size: tuple[int, int]
    ) -> VideoClip:
        """Return a static dark radial-gradient vignette overlay.

        Returns RGBA (4-channel) arrays so MoviePy v2 composites the
        vignette correctly as a semi-transparent overlay on top of
        the image clips.

        Uses ``VIGNETTE_RADIUS_FACTOR`` and ``COLOR_PALETTE.secondary``
        from config for channel-specific styling.
        """
        vw, vh = size
        cx, cy = vw / 2, vh / 2
        max_r = np.sqrt(cx ** 2 + cy ** 2)
        radius_factor = self.canal.get("VIGNETTE_RADIUS_FACTOR", 0.65)
        vignette_intensity = self.canal.get("VIGNETTE_INTENSITY", 10)
        # Use color_palette secondary for vignette tint, fallback to near-black
        color_pal = self.canal.get("COLOR_PALETTE", COLOR_PALETTE)
        vignette_rgb = color_pal.get("secondary", (vignette_intensity,) * 3)
        if isinstance(vignette_rgb, int):
            vignette_rgb = (vignette_rgb,) * 3

        yy, xx = np.mgrid[0:vh, 0:vw]
        dist = np.sqrt((xx - cx) ** 2 + (yy - cy) ** 2)
        alpha = np.clip(dist / (max_r * radius_factor), 0.0, 1.0)

        # Pre-built RGBA frame: tinted overlay with radial alpha gradient
        dark_rgb = np.full((vh, vw, 3), vignette_rgb, dtype=np.uint8)
        alpha_ch = (alpha * 255).astype(np.uint8)[..., np.newaxis]
        vignette = np.concatenate([dark_rgb, alpha_ch], axis=2)

        def make_frame(t: float) -> np.ndarray:
            return vignette

        return VideoClip(make_frame, duration=duration)

    # ── Intro / Outro ───────────────────────────────────────────

    def _get_template_path(self, segment_type: str) -> Optional[Path]:
        """Get cached template path if it exists."""
        canal_name = self.canal.get("CANAL_NAME", self.canal.get("slug", "canal2"))
        template_dir = Path("output/templates") / canal_name
        path = template_dir / f"{segment_type}.mp4"
        if path.exists():
            return path
        return None

    def _build_intro(self, duration: float = None) -> VideoClip | None:
        """Intro: load cached template MP4 or fallback to programmatic generation."""
        if duration is None:
            duration = self.canal.get("INTRO_DURATION_SEC", self.INTRO_DURATION)

        # Try loading cached template
        template_path = self._get_template_path("intro")
        if template_path:
            try:
                clip = VideoFileClip(str(template_path))
                if clip.duration > duration:
                    clip = clip.subclipped(0, duration)
                self.logger.info("Using cached intro template: %s", template_path)
                return clip
            except Exception as e:
                self.logger.warning("Failed to load intro template: %s — falling back to programmatic", e)

        # Fallback: original programmatic intro
        canal_display = self.canal.get("CANAL_DISPLAY_NAME", CANAL_DISPLAY_NAME)
        subtitle_text = self.canal.get("INTRO_SUBTITLE", "")
        color_pal = self.canal.get("COLOR_PALETTE", COLOR_PALETTE)
        text_color = color_pal.get("text", (230, 230, 230))
        accent_color = color_pal.get("accent", (200, 160, 40))
        bg_color = self.canal.get("INTRO_BG_COLOR", (5, 5, 5))
        font_size = self.canal.get("INTRO_FONT_SIZE", 68)
        sub_font_size = self.canal.get("INTRO_SUBTITLE_FONT_SIZE", 28)

        try:
            font = self._resolve_font()
            vw, vh = self.video_size

            # Background: radial gradient from center
            bg_frame = self._gradient_bg(vw, vh, bg_color, color_pal.get("secondary", (12, 10, 10)))

            def make_bg(t: float) -> np.ndarray:
                return bg_frame
            bg = VideoClip(make_bg, duration=duration)

            # Channel name
            max_width = int(vw * 0.80)
            label = TextClip(
                text=canal_display,
                font=font,
                font_size=font_size,
                color=_rgb_to_hex(accent_color),
                method="pillow",
                size=(max_width, None),
            ).with_position(("center", int(vh * 0.48))).with_duration(duration)
            label = _fade_in_out(label, duration, hold_ratio=0.4)

            clips = [bg, label]

            # Subtitle below channel name
            if subtitle_text:
                sub = TextClip(
                    text=subtitle_text,
                    font=font,
                    font_size=sub_font_size,
                    color=_rgb_to_hex(text_color),
                    method="pillow",
                    size=(max_width, None),
                ).with_position(("center", int(vh * 0.58))).with_duration(duration)
                sub = _fade_in_out(sub, duration, hold_ratio=0.4)
                clips.append(sub)

            # Logo at top
            logo_clip = self._build_logo_clip(duration)
            if logo_clip is not None:
                logo_y = int(vh * 0.18)
                logo_clip = logo_clip.with_position(("center", logo_y))
                # Scale animation for logo
                logo_clip = logo_clip.with_effects([
                    vfx.Resize(lambda t: 0.85 + 0.15 * min(t / (duration * 0.5), 1.0))
                ])
                clips.append(logo_clip)

            # Decorative line between logo and text
            line_y = int(vh * 0.38)
            line_frame = self._decorative_line(vw, vh, line_y, accent_color)
            def make_line(t: float) -> np.ndarray:
                return line_frame
            line_clip = VideoClip(make_line, duration=duration)
            line_clip = _fade_in_out(line_clip, duration, hold_ratio=0.3)
            clips.append(line_clip)

            return CompositeVideoClip(clips, size=self.video_size)
        except Exception:
            self.logger.exception("Intro creation failed — skipping.")
            return None

    def _build_outro(self, duration: float = None) -> VideoClip | None:
        """Outro: load cached template MP4 or fallback to programmatic generation."""
        if duration is None:
            duration = self.canal.get("OUTRO_DURATION_SEC", self.OUTRO_DURATION)

        # Try loading cached template
        template_path = self._get_template_path("outro")
        if template_path:
            try:
                clip = VideoFileClip(str(template_path))
                if clip.duration > duration:
                    clip = clip.subclipped(0, duration)
                self.logger.info("Using cached outro template: %s", template_path)
                return clip
            except Exception as e:
                self.logger.warning("Failed to load outro template: %s — falling back to programmatic", e)

        # Fallback: original programmatic outro
        color_pal = self.canal.get("COLOR_PALETTE", COLOR_PALETTE)
        accent_color = color_pal.get("accent", (200, 160, 40))
        text_color = color_pal.get("text", (230, 230, 230))
        bg_color = self.canal.get("OUTRO_BG_COLOR", (5, 5, 5))
        font_size = self.canal.get("OUTRO_FONT_SIZE", 52)

        cta_like = self.canal.get("OUTRO_CTA_LIKE", "👍 Like")
        cta_sub = self.canal.get("OUTRO_CTA_SUBSCRIBE", "🔔 Suscríbete")
        cta_bell = self.canal.get("OUTRO_CTA_BELL", "📢 Comparte")
        canal_display = self.canal.get("CANAL_DISPLAY_NAME", CANAL_DISPLAY_NAME)

        try:
            font = self._resolve_font()
            vw, vh = self.video_size

            # Gradient background
            bg_frame = self._gradient_bg(vw, vh, bg_color, color_pal.get("secondary", (12, 10, 10)))
            def make_bg(t: float) -> np.ndarray:
                return bg_frame
            bg = VideoClip(make_bg, duration=duration)

            clips = [bg]

            # Logo
            logo_clip = self._build_logo_clip(duration)
            if logo_clip is not None:
                logo_y = int(vh * 0.12)
                logo_clip = logo_clip.with_position(("center", logo_y))
                clips.append(logo_clip)

            # Three CTA items stacked vertically
            cta_font_size = int(font_size * 0.70)
            cta_texts = [cta_like, cta_sub, cta_bell]
            # Position each CTA at a different vertical level
            cta_y_positions = [int(vh * 0.42), int(vh * 0.52), int(vh * 0.62)]
            cta_max_w = int(vw * 0.85)

            for i, cta_text in enumerate(cta_texts):
                cta = TextClip(
                    text=cta_text,
                    font=font,
                    font_size=cta_font_size,
                    color=_rgb_to_hex(text_color),
                    method="pillow",
                    size=(cta_max_w, None),
                ).with_position(("center", cta_y_positions[i])).with_duration(duration)

                # Staggered fade-in per CTA
                delay = i * 0.4
                cta = cta.with_start(delay)
                cta = cta.with_effects([
                    vfx.FadeIn(0.3),
                ])
                clips.append(cta)

            # Channel name at bottom
            name_y = int(vh * 0.76)
            name = TextClip(
                text=canal_display,
                font=font,
                font_size=font_size,
                color=_rgb_to_hex(accent_color),
                method="pillow",
                size=(int(vw * 0.80), None),
            ).with_position(("center", name_y)).with_duration(duration)
            name = _fade_in_out(name, duration, hold_ratio=0.5)
            clips.append(name)

            return CompositeVideoClip(clips, size=self.video_size)
        except Exception:
            self.logger.exception("Outro creation failed — skipping.")
            return None

    def _build_cta(self, duration: float = 2.5, audio_path: str | None = None) -> VideoClip | None:
        """Build the CTA (call-to-action) clip between body and outro.

        Preference order:
            1. Cached template MP4 (``output/templates/{canal}/cta.mp4``)
            2. Programmatic generation with channel branding + subscribe text

        If *audio_path* is provided it is returned as a side channel; the
        caller is responsible for placing the audio in the correct position
        in the final audio assembly.
        """
        # Try loading cached template
        template_path = self._get_template_path("cta")
        if template_path:
            try:
                clip = VideoFileClip(str(template_path))
                if self._dur(clip) > duration:
                    clip = clip.subclipped(0, duration)
                self.logger.info("Using cached CTA template: %s", template_path)
                return clip
            except Exception as e:
                self.logger.warning("Failed to load CTA template: %s — falling back to programmatic", e)

        # Fallback: programmatic CTA with gradient background + logo + text
        color_pal = self.canal.get("COLOR_PALETTE", COLOR_PALETTE)
        accent_color = color_pal.get("accent", (200, 160, 40))
        text_color = color_pal.get("text", (230, 230, 230))
        bg_color = self.canal.get("CTA_BG_COLOR", (8, 8, 10))
        font_size = self.canal.get("CTA_FONT_SIZE", 44)

        cta_text = self.canal.get(
            "CTA_TEXT",
            "Si este contenido te ha hecho reflexionar,\nsuscríbete para más historias como esta",
        )

        try:
            font = self._resolve_font()
            vw, vh = self.video_size

            # Dark gradient background
            bg_frame = self._gradient_bg(vw, vh, bg_color, color_pal.get("secondary", (12, 10, 10)))

            def make_bg(t: float) -> np.ndarray:
                return bg_frame
            bg = VideoClip(make_bg, duration=duration)

            clips: list[VideoClip] = [bg]

            # Channel logo centered (scaled down for CTA)
            logo_clip = self._build_logo_clip(duration)
            if logo_clip is not None:
                logo_size = self.canal.get("LOGO_SIZE", 180)
                cta_logo_size = int(logo_size * 0.7)
                logo_clip = logo_clip.resized(width=cta_logo_size, height=cta_logo_size)
                logo_y = int(vh * 0.18)
                logo_clip = logo_clip.with_position(("center", logo_y))
                # Gentle scale-in animation
                try:
                    logo_clip = logo_clip.with_effects([
                        vfx.Resize(lambda t: 0.8 + 0.2 * min(t / (duration * 0.4), 1.0))
                    ])
                except Exception:
                    pass  # Resize with lambda may not be supported — skip
                clips.append(logo_clip)

            # Subscribe text (multi-line supported via \n)
            text_max_w = int(vw * 0.78)
            text_y = int(vh * 0.45)
            for line_idx, line in enumerate(cta_text.split("\n")):
                txt = TextClip(
                    text=line.strip(),
                    font=font,
                    font_size=font_size if line_idx == 0 else int(font_size * 0.7),
                    color=_rgb_to_hex(accent_color if line_idx == 0 else text_color),
                    method="caption",
                    size=(text_max_w, None),
                ).with_position(("center", text_y + line_idx * (font_size + 12)))
                # Fade-in with staggered delay
                try:
                    txt = txt.with_effects([vfx.FadeIn(0.4)])
                except Exception:
                    pass  # FadeIn may fail on some clip types — skip gracefully
                txt = txt.with_start(line_idx * 0.3)
                txt = txt.with_duration(duration)
                clips.append(txt)

            # Decorative line above subscribe button area
            line_y = int(vh * 0.75)
            line_frame = self._decorative_line(vw, vh, line_y, accent_color)
            # Convert RGBA → RGB for vfx compatibility (FadeIn expects 3-channel input)
            line_frame_rgb = line_frame[:, :, :3]

            def make_line(t: float) -> np.ndarray:
                return line_frame_rgb
            line_clip = VideoClip(make_line, duration=duration)
            try:
                line_clip = line_clip.with_effects([vfx.FadeIn(0.5)])
            except Exception:
                pass  # vfx.FadeIn may fail on RGBA → skip gracefully
            clips.append(line_clip)

            # "🔔" icon or subscribe indicator at bottom
            icon_text = self.canal.get("CTA_ICON", "🔔")
            icon_y = int(vh * 0.81)
            icon = TextClip(
                text=icon_text,
                font=font,
                font_size=int(font_size * 0.8),
                color=_rgb_to_hex(accent_color),
                method="label",
            ).with_position(("center", icon_y)).with_duration(duration)
            try:
                icon = icon.with_effects([vfx.FadeIn(0.3)])
            except Exception:
                pass  # fade may not be supported — skip gracefully
            clips.append(icon)

            return CompositeVideoClip(clips, size=self.video_size)
        except Exception:
            self.logger.exception("CTA programmatic generation failed — skipping.")
            return None

    def _build_logo_clip(self, duration: float) -> ImageClip | None:
        """Generate a polished circular logo with channel initials + decorative ring."""
        try:
            logo_path_cfg = self.canal.get("LOGO_PATH", "")
            if logo_path_cfg and os.path.isfile(logo_path_cfg):
                logo_size = self.canal.get("LOGO_SIZE", 180)
                clip = ImageClip(logo_path_cfg).with_duration(duration)
                clip = clip.resized(width=logo_size, height=logo_size)
                return clip

            initials = self.canal.get("CANAL_INITIALS", "PO")
            logo_size = self.canal.get("LOGO_SIZE", 180)
            color_pal = self.canal.get("COLOR_PALETTE", COLOR_PALETTE)
            primary = color_pal.get("primary", (160, 22, 22))
            accent = color_pal.get("accent", (161, 117, 55))
            text_clr = color_pal.get("text", (225, 220, 215))
            secondary = color_pal.get("secondary", (12, 10, 10))

            img = Image.new("RGBA", (logo_size, logo_size), (0, 0, 0, 0))
            draw = ImageDraw.Draw(img)

            # Outer ring (accent color)
            ring_width = 4
            draw.ellipse(
                [0, 0, logo_size - 1, logo_size - 1],
                outline=accent + (180,),
                width=ring_width,
            )

            # Inner filled circle (dark bg)
            margin = ring_width + 2
            draw.ellipse(
                [margin, margin, logo_size - margin, logo_size - margin],
                fill=secondary + (230,),
            )

            # Gradient-like effect: inner accent ellipse
            inner_margin = logo_size // 5
            draw.ellipse(
                [inner_margin, inner_margin, logo_size - inner_margin, logo_size - inner_margin],
                outline=primary + (120,),
                width=2,
            )

            # Brain emoji icon or initials
            try:
                # Try to use a brain symbol if font supports it
                icon_font = ImageFont.truetype(self._resolve_font(), int(logo_size * 0.30))
                icon_text = "🧠"  # fallback to initials if emoji fails
                bbox = draw.textbbox((0, 0), initials, font=icon_font)
            except Exception:
                icon_font = ImageFont.load_default()
                icon_text = initials

            # Draw initials
            try:
                text_font = ImageFont.truetype(self._resolve_font(), int(logo_size * 0.40))
            except Exception:
                text_font = ImageFont.load_default()

            bbox = draw.textbbox((0, 0), initials, font=text_font)
            text_w = bbox[2] - bbox[0]
            text_h = bbox[3] - bbox[1]
            text_x = (logo_size - text_w) // 2
            text_y = (logo_size - text_h) // 2 - bbox[1]

            # Shadow behind initials
            shadow_off = 2
            draw.text((text_x + shadow_off, text_y + shadow_off), initials,
                      fill=primary + (180,), font=text_font)
            # Main text
            draw.text((text_x, text_y), initials, fill=text_clr + (240,), font=text_font)

            logo_array = np.array(img)
            clip = ImageClip(logo_array).with_duration(duration)
            return clip
        except Exception:
            self.logger.exception("Logo generation failed — skipping.")
            return None

    @staticmethod
    def _gradient_bg(vw: int, vh: int, center_color: tuple, edge_color: tuple) -> np.ndarray:
        """Create a radial gradient background frame."""
        cx, cy = vw / 2, vh / 2
        max_r = np.sqrt(cx ** 2 + cy ** 2)
        yy, xx = np.mgrid[0:vh, 0:vw]
        dist = np.sqrt((xx - cx) ** 2 + (yy - cy) ** 2) / max_r
        dist = np.clip(dist, 0, 1)

        # Interpolate between center and edge colors
        r = (center_color[0] * (1 - dist) + edge_color[0] * dist).astype(np.uint8)
        g = (center_color[1] * (1 - dist) + edge_color[1] * dist).astype(np.uint8)
        b = (center_color[2] * (1 - dist) + edge_color[2] * dist).astype(np.uint8)
        return np.stack([r, g, b], axis=2)

    @staticmethod
    def _decorative_line(vw: int, vh: int, y_pos: int, color: tuple) -> np.ndarray:
        """Create a single transparent frame with a decorative horizontal line."""
        frame = np.zeros((vh, vw, 4), dtype=np.uint8)
        line_w = int(vw * 0.15)
        x_start = (vw - line_w) // 2
        x_end = x_start + line_w
        line_y = y_pos
        # Draw a 2px line with alpha
        frame[line_y:line_y+2, x_start:x_end] = (*color, 180)
        return frame

    # ── Sound effects ───────────────────────────────────────────

    def _add_sfx(
        self,
        base_audio: AudioClip,
        scenes: list[dict],
        timestamps: list[dict],
    ) -> AudioClip:
        """Insert sound effects at trigger-word positions.

        Scans timestamps for trigger words, loads a matching SFX file from
        ``ASSETS_DIR/sfx/``, and layers it at the correct position.
        """
        sfx_dir = ASSETS_DIR / "sfx"
        if not sfx_dir.is_dir():
            return base_audio

        available_sfx: dict[str, Path] = {}
        for p in sfx_dir.iterdir():
            if p.suffix.lower() in (".mp3", ".wav", ".ogg", ".m4a", ".aac", ".flac"):
                available_sfx[p.stem.lower()] = p

        if not available_sfx:
            return base_audio

        audio_clips: list[AudioClip] = [base_audio]

        for ts in timestamps:
            word = str(ts.get("word", "")).lower().strip(".,!?;:¡¿")
            if word not in self.TRIGGER_WORDS:
                continue
            sfx_path = available_sfx.get(word)
            if sfx_path is None:
                continue

            try:
                start_time = float(ts.get("start", 0))
                sfx = AudioFileClip(str(sfx_path))
                sfx = sfx.with_start(start_time)
                # duck sfx slightly so it doesn't overpower voice
                sfx = sfx.with_effects([afx.MultiplyVolume(0.6)]) if MOVIEPY_V2 else sfx.volumex(0.6)
                audio_clips.append(sfx)
                self.logger.debug("SFX '%s' at %.1f s", word, start_time)
            except Exception:
                self.logger.exception("SFX '%s' failed — skipping.", word)

        if len(audio_clips) == 1:
            return base_audio
        return CompositeAudioClip(audio_clips)


# ── Module-level helpers ────────────────────────────────────────


def _find_ffmpeg_pids() -> set[int]:
    """Return PIDs of currently-running ffmpeg processes."""
    try:
        import subprocess
        out = subprocess.check_output(["pgrep", "-x", "ffmpeg"], text=True, timeout=5)
        return {int(pid) for pid in out.strip().split("\n") if pid}
    except Exception:
        return set()


def _kill_new_ffmpeg(before_pids: set[int]) -> None:
    """Kill ffmpeg processes that started after before_pids snapshot."""
    after = _find_ffmpeg_pids()
    new = after - before_pids
    import signal
    for pid in new:
        try:
            os.kill(pid, signal.SIGKILL)
            logging.getLogger("VideoEditor").warning("Killed hung ffmpeg PID %d", pid)
        except Exception:
            pass


def _rgb_to_hex(rgb: tuple[int, int, int]) -> str:
    """Convert (R, G, B) tuple to ``'#RRGGBB'`` hex string."""
    return "#{:02X}{:02X}{:02X}".format(*rgb)


def _fade_in_out(
    clip: VideoClip, total_duration: float, hold_ratio: float = 0.5
) -> VideoClip:
    """Apply a symmetric fade-in / fade-out leaving *hold_ratio* visible."""
    fade_dur = total_duration * (1.0 - hold_ratio) / 2.0
    try:
        if MOVIEPY_V2:
            return clip.with_effects([
                vfx.FadeIn(fade_dur),
                vfx.FadeOut(fade_dur),
            ])
        else:
            return clip.fadein(fade_dur).fadeout(fade_dur)
    except Exception:
        return clip


def _solid_color_clip(
    duration: float, size: tuple[int, int], color: tuple[int, int, int]
) -> VideoClip:
    """Return a solid-colour ``VideoClip``."""
    arr = np.full((size[1], size[0], 3), color, dtype=np.uint8)
    return VideoClip(lambda t: arr, duration=duration)
