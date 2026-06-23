"""Video editor: assembles images + audio + subtitles + effects into final MP4."""

import os
import re
import random
import logging
from pathlib import Path
from typing import Callable

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

from config.settings import (
    VIDEOS_DIR,
    AUDIO_DIR,
    IMAGES_DIR,
    ASSETS_DIR,
    VIDEO_FPS,
    VIDEO_RESOLUTION,
    VIDEO_BITRATE,
    VIDEO_CODEC,
)
from config.canal1_config import (
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


def _db_to_linear(db: float) -> float:
    """Convert dB relative to full scale to linear amplitude factor."""
    return 10 ** (db / 20)


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
    MIN_SCENE_DURATION: float = 5.0
    MAX_SCENE_DURATION: float = 12.0

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
    ) -> Path:
        """Assemble the complete video from blocks, media, audio, and effects.

        v2 API (preferred):
            bloques: List of block dicts from LLM (tipo, texto, media_tipo, ...)
            media_assets: List of asset dicts from MediaFetcher (path, type, duration)
            audio_path: Path to TTS voice-over MP3
            timestamps: Word-level timestamps

        v1 API (legacy fallback):
            scenes: List of scene dicts from parse_scenes()
            image_paths: List[list[Path]] per-scene image paths

        Pipeline stages:
            1. Compute block time ranges from timestamps
            2. Create clips: VideoFileClip (video) or Ken Burns (image)
            3. Composite with crossfade transitions
            4. Add subtitles (if SUBTITLES_ENABLED)
            5. Add intro + outro
            6. Render MP4
        """
        if output_path is None:
            safe_stem = Path(audio_path).stem.replace(" ", "_")
            output_path = str(VIDEOS_DIR / f"{safe_stem}.mp4")
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)

        timestamps = self._normalize_timestamps(timestamps)

        # ── Determine API mode ─────────────────────────────────
        use_blocks = bloques is not None and media_assets is not None
        if not use_blocks and scenes is not None and image_paths is not None:
            self.logger.warning("Using legacy build_video path (scenes + image_paths)")
            return self._build_video_legacy(
                scenes=scenes, image_paths=image_paths,
                audio_path=audio_path, timestamps=timestamps,
                output_path=output_path, music_volume=music_volume,
            )
        if not use_blocks:
            raise ValueError("Either (bloques + media_assets) or (scenes + image_paths) must be provided")

        self.logger.info("🎬 Building video (v2 blocks) → %s", output_path)

        # ── Compute block time ranges ──────────────────────────
        block_ranges = self._compute_block_ranges(bloques, timestamps)
        if not block_ranges:
            raise RuntimeError("No block ranges computed — cannot build video.")
        self.logger.info("Step 1/6: %d blocks with time ranges computed", len(block_ranges))

        # ── Create clips per block ─────────────────────────────
        block_clips: list[VideoClip] = []
        total_audio_end = timestamps[-1].get("end", 0) if timestamps else 0
        for i, br in enumerate(block_ranges):
            asset = media_assets[i] if i < len(media_assets) else {"type": "placeholder", "path": None}
            clip = self._create_block_clip(br, asset)
            clip = clip.with_start(br["start"])
            block_clips.append(clip)

            media_type = asset.get("type", "?")
            self.logger.info("  Block %d [%s]: %.1f-%.1fs (%.1fs) media=%s",
                             i + 1, br.get("tipo", "?"), br["start"], br["end"],
                             br["duration"], media_type)

        # Extend last clip to cover full audio (no black gap before outro)
        if block_clips and total_audio_end > 0:
            last = block_clips[-1]
            last_end = last.start + (last.duration() if callable(last.duration) else last.duration)
            if last_end < total_audio_end:
                new_dur = total_audio_end - last.start
                self.logger.info("  Extending last clip by %.1fs to cover audio end", new_dur - (last_end - last.start))
                block_clips[-1] = last.with_duration(new_dur)

        if not block_clips:
            raise RuntimeError("No clips created — cannot build video.")

        # ── Composite with crossfades ──────────────────────────
        self.logger.info("Step 2/6: Compositing %d clips with crossfades…", len(block_clips))
        body_video = self._composite_scenes(block_clips)

        # ── Audio ──────────────────────────────────────────────
        self.logger.info("Step 3/6: Loading audio…")
        intro_dur = self.canal.get("INTRO_DURATION_SEC", self.INTRO_DURATION)
        silence = self._silent_audio(intro_dur)
        tts_audio = AudioFileClip(audio_path).with_start(intro_dur)
        body_audio = CompositeAudioClip([silence, tts_audio])

        # ── Subtitles (toggleable) ─────────────────────────────
        subtitles_enabled = self.canal.get("SUBTITLES_ENABLED", True)
        if subtitles_enabled:
            self.logger.info("Step 4/6: Adding animated subtitles…")
            subtitle_clips = self._build_subtitles(timestamps, self.video_size)
            body_video = CompositeVideoClip(
                [body_video] + subtitle_clips, size=self.video_size,
            )
        else:
            self.logger.info("Step 4/6: Subtitles disabled (SUBTITLES_ENABLED=False)")

        # ── Intro + Outro ──────────────────────────────────────
        self.logger.info("Step 5/6: Adding intro + outro…")
        outro_dur = self.canal.get("OUTRO_DURATION_SEC", self.OUTRO_DURATION)
        intro = self._build_intro(intro_dur)
        outro = self._build_outro(outro_dur)

        if intro is not None:
            intro = intro.with_audio(self._silent_audio(intro_dur))
            body_video = concatenate_videoclips([intro, body_video])
        if outro is not None:
            outro = outro.with_audio(self._silent_audio(outro_dur))
            body_video = concatenate_videoclips([body_video, outro])

        body_video = body_video.with_audio(body_audio)

        # ── Render ─────────────────────────────────────────────
        self.logger.info("Step 6/6: Rendering MP4 (%dx%d, %d fps)…",
                         self.video_size[0], self.video_size[1], self.fps)
        render_ok = False
        try:
            body_video.write_videofile(
                str(output_path),
                fps=self.fps,
                codec=VIDEO_CODEC,
                bitrate=VIDEO_BITRATE,
                preset="medium",
                audio_codec="aac",
                threads=os.cpu_count() or 4,
                ffmpeg_params=["-movflags", "+faststart"],
            )
            render_ok = True
        except Exception as exc:
            self.logger.error("❌ MoviePy render crashed: %s", exc)
            import traceback
            crash_log = output_path.with_suffix(".crash.log")
            crash_log.write_text(traceback.format_exc())
            self.logger.error("   Full traceback saved to %s", crash_log)
        finally:
            body_video.close()
            body_audio.close()

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
        """
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
                })
                word_idx += n_words

            # Safety: extend last range to audio end
            if ranges and total_audio_end > 0:
                ranges[-1]["end"] = max(ranges[-1]["end"], total_audio_end)
                ranges[-1]["duration"] = ranges[-1]["end"] - ranges[-1]["start"]
            return ranges

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
            })
            cumulative += dur
        return ranges

    def _create_block_clip(self, block_range: dict, asset: dict) -> VideoClip:
        """Create a clip for one block: video, image, or placeholder."""
        media_type = asset.get("type", "placeholder")
        block_dur = block_range["duration"]
        asset_path = asset.get("path")

        if media_type == "video" and asset_path and Path(asset_path).exists():
            return self._video_clip_for_block(asset_path, block_dur)
        elif media_type == "image" and asset_path and Path(asset_path).exists():
            return self._image_clip_for_block(asset_path, block_dur)
        else:
            # Placeholder: solid color with block text hint
            return self._placeholder_clip_with_text(block_range, block_dur)

    def _video_clip_for_block(self, video_path: Path, block_dur: float) -> VideoClip:
        """Load a video clip, trim or loop to match block duration."""
        try:
            clip = VideoFileClip(str(video_path))
            clip_dur = clip.duration() if callable(clip.duration) else clip.duration

            if clip_dur >= block_dur:
                # Trim to block duration
                clip = clip.subclipped(0, block_dur)
            else:
                # Loop the video to fill the block
                loops_needed = int(block_dur / clip_dur) + 1
                looped = [clip] * loops_needed
                clip = concatenate_videoclips(looped)
                clip = clip.subclipped(0, block_dur)

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

    # ── Legacy build_video (backward compatible) ───────────────

    def _build_video_legacy(
        self,
        scenes: list[dict],
        image_paths: list,
        audio_path: str,
        timestamps: list[dict],
        output_path: Path,
        music_volume: float,
    ) -> Path:
        """Legacy video assembler using scenes + image_paths (pre-v2 API)."""
        phrases = self._extract_phrases(timestamps)
        if not phrases:
            raise RuntimeError("No phrases extracted — cannot build video.")

        scene_boundaries = self._compute_scene_boundaries(scenes, timestamps)
        phrases_per_image = self.canal.get("PHRASES_PER_IMAGE", 2)
        no_repeat = self.canal.get("NO_REPEAT_IMAGES", True)

        # Shuffle queues (legacy)
        scene_queues: list[list[Path]] = []
        scene_last_img: list[Path | None] = []
        for scene_imgs in image_paths:
            queue = list(scene_imgs)
            if no_repeat and queue:
                random.shuffle(queue)
            scene_queues.append(queue)
            scene_last_img.append(None)

        def _pick_image_for_scene(scene_idx: int) -> Path | None:
            if scene_queues[scene_idx]:
                return scene_queues[scene_idx].pop(0)
            if image_paths[scene_idx]:
                scene_queues[scene_idx] = list(image_paths[scene_idx])
                if no_repeat and len(scene_queues[scene_idx]) > 1:
                    random.shuffle(scene_queues[scene_idx])
                if scene_queues[scene_idx]:
                    return scene_queues[scene_idx].pop(0)
            for offset in [1, -1, 2, -2]:
                nb = scene_idx + offset
                if 0 <= nb < len(image_paths) and image_paths[nb]:
                    return random.choice(image_paths[nb])
            for imgs in image_paths:
                if imgs:
                    return random.choice(imgs)
            return None

        def _find_scene_index(midpoint: float) -> int:
            for idx, boundary in enumerate(scene_boundaries):
                if midpoint < boundary:
                    return idx
            return len(scene_boundaries) - 1

        image_clips: list[VideoClip] = []
        for i in range(0, len(phrases), phrases_per_image):
            group = phrases[i:i + phrases_per_image]
            start = group[0]["start"]
            end = group[-1]["end"]
            midpoint = (start + end) / 2
            dur = end - start
            if dur <= 0:
                continue

            scene_idx = _find_scene_index(midpoint)
            img_path = _pick_image_for_scene(scene_idx)
            if img_path is None:
                clip = self._placeholder_clip(dur)
            else:
                scene_last_img[scene_idx] = img_path
                zoom = random.uniform(
                    self.canal.get("KEN_BURNS_ZOOM_MIN", KEN_BURNS_ZOOM_MIN),
                    self.canal.get("KEN_BURNS_ZOOM_MAX", KEN_BURNS_ZOOM_MAX),
                )
                clip = self._single_ken_burns_clip(img_path, dur, zoom)
            clip = clip.with_start(start).with_duration(dur)
            image_clips.append(clip)

        if not image_clips:
            raise RuntimeError("No image clips created.")

        body_video = self._composite_scenes(image_clips)

        intro_dur = self.canal.get("INTRO_DURATION_SEC", self.INTRO_DURATION)
        silence = self._silent_audio(intro_dur)
        tts_audio = AudioFileClip(audio_path).with_start(intro_dur)
        body_audio = CompositeAudioClip([silence, tts_audio])

        subtitles_enabled = self.canal.get("SUBTITLES_ENABLED", True)
        if subtitles_enabled:
            subtitle_clips = self._build_subtitles(timestamps, self.video_size)
            body_video = CompositeVideoClip([body_video] + subtitle_clips, size=self.video_size)

        outro_dur = self.canal.get("OUTRO_DURATION_SEC", self.OUTRO_DURATION)
        intro = self._build_intro(intro_dur)
        outro = self._build_outro(outro_dur)
        if intro is not None:
            intro = intro.with_audio(self._silent_audio(intro_dur))
            body_video = concatenate_videoclips([intro, body_video])
        if outro is not None:
            outro = outro.with_audio(self._silent_audio(outro_dur))
            body_video = concatenate_videoclips([body_video, outro])
        body_video = body_video.with_audio(body_audio)

        body_video.write_videofile(
            str(output_path), fps=self.fps, codec=VIDEO_CODEC,
            bitrate=VIDEO_BITRATE, preset="medium", audio_codec="aac",
            threads=os.cpu_count() or 4,
            ffmpeg_params=["-movflags", "+faststart"],
        )
        body_video.close()
        body_audio.close()
        return output_path

    # ── Image clips + Ken Burns ─────────────────────────────────

    def _create_image_clips(
        self,
        scenes: list[dict],
        image_paths: list[list[Path]],
        timestamps: list[dict],
    ) -> list[VideoClip]:
        """Create an ImageClip per scene with Ken Burns zoom/pan.

        Scene duration is determined from the last timestamp of its words.
        Falls back to a *random* value between ``MIN_SCENE_DURATION`` and
        ``MAX_SCENE_DURATION`` when timestamps are unavailable.
        """
        clips: list[VideoClip] = []
        scene_durations = self._scene_durations_from_timestamps(scenes, timestamps)

        for i, scene in enumerate(scenes):
            images_for_scene = image_paths[i] if i < len(image_paths) else []
            if not images_for_scene:
                self.logger.warning(
                    "No images for scene %d — creating black placeholder.", i
                )
                clip = self._placeholder_clip(scene_durations[i])
                clips.append(clip)
                continue

            image_path = random.choice(images_for_scene)
            duration = scene_durations[i]
            zoom = random.uniform(
                self.canal.get("KEN_BURNS_ZOOM_MIN", KEN_BURNS_ZOOM_MIN),
                self.canal.get("KEN_BURNS_ZOOM_MAX", KEN_BURNS_ZOOM_MAX),
            )
            clip = self._single_ken_burns_clip(image_path, duration, zoom)
            clips.append(clip)
            self.logger.debug("Scene %d: %s @ %.1f s (zoom %.1f%%)",
                              i, image_path.name, duration, zoom)

        return clips

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

    def _compute_scene_boundaries(
        self, scenes: list[dict], timestamps: list[dict]
    ) -> list[float]:
        """Compute time boundaries for each scene proportional to its text length.

        Returns a list of end-times (in seconds) for each scene. Scene i spans
        from ``boundaries[i-1]`` (or 0) to ``boundaries[i]``.

        This is used in ``build_video()`` to assign each phrase group to the
        correct scene so images are always relevant to what's being narrated.
        """
        if not scenes:
            return []

        # Total audio duration from timestamps
        total_audio = timestamps[-1].get("end", 0) if timestamps else 60.0
        if total_audio <= 0:
            total_audio = 60.0

        # Compute proportional duration based on text length per scene
        scene_lengths = [len(s.get("text", "")) for s in scenes]
        total_len = sum(scene_lengths)
        if total_len <= 0:
            # Equal split if no text lengths
            per_scene = total_audio / len(scenes)
            return [(i + 1) * per_scene for i in range(len(scenes))]

        boundaries: list[float] = []
        cumulative = 0.0
        for length in scene_lengths:
            proportion = length / total_len
            segment_dur = proportion * total_audio
            cumulative += segment_dur
            boundaries.append(cumulative)

        return boundaries

    def _scene_durations_from_timestamps(
        self, scenes: list[dict], timestamps: list[dict]
    ) -> list[float]:
        """Partition flat word timestamps into per-scene durations."""
        durations: list[float] = []

        # Check if timestamps have scene_index markers
        has_scene_markers = any("scene_index" in ts for ts in (timestamps or []))

        if not timestamps or not has_scene_markers:
            # Even distribution based on total audio duration.
            # Timestamps are already normalized to seconds (start / end keys).
            # No upper cap — the prompt ensures enough scenes for dynamic pacing.
            min_dur = self.canal.get("SCENE_DURATION_MIN", self.MIN_SCENE_DURATION)
            max_dur = self.canal.get("SCENE_DURATION_MAX", self.MAX_SCENE_DURATION)
            total_sec = timestamps[-1].get("end", 0) if timestamps else 60.0
            total_dur = max(total_sec, len(scenes) * min_dur)
            per_scene = total_dur / max(len(scenes), 1)
            per_scene = max(per_scene, min_dur)
            if per_scene > max_dur:
                self.logger.warning(
                    "Scene duration %.1fs > %.1fs — consider generating "
                    "more scenes for dynamic pacing (got %d scenes for %.0fs audio)",
                    per_scene, max_dur, len(scenes), total_sec,
                )
            return [per_scene] * len(scenes)

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
        """

        crossfade_duration = random.uniform(
            self.canal.get("CROSSFADE_MIN", 0.3),
            self.canal.get("CROSSFADE_MAX", 0.7),
        )
        positioned: list[VideoClip] = []
        cursor = 0.0

        for i, clip in enumerate(clips):
            start = max(0.0, cursor - (crossfade_duration if i > 0 else 0.0))
            dur = clip.duration

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
            idx = min(int(t * 100), len(voice_active_slots) - 1)
            return duck_factor if voice_active_slots[idx] else music_factor

        try:
            if MOVIEPY_V2:
                music = music.with_effects([afx.MultiplyVolume(lambda t: volume_fn(t))])
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
        """Return sorted list of audio files in ``ASSETS_DIR/music``."""
        music_dir = ASSETS_DIR / "music"
        if not music_dir.is_dir():
            return []
        return sorted(
            p
            for p in music_dir.iterdir()
            if p.suffix.lower() in (".mp3", ".wav", ".ogg", ".m4a", ".aac", ".flac")
        )

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

    def _build_intro(self, duration: float = None) -> VideoClip | None:
        """Intro: channel name + subtitle over radial gradient background → fade in."""
        if duration is None:
            duration = self.canal.get("INTRO_DURATION_SEC", self.INTRO_DURATION)
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
                method="caption",
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
                    method="caption",
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
        """Outro: CTA buttons (like/subscribe/bell) + channel name → fade out."""
        if duration is None:
            duration = self.canal.get("OUTRO_DURATION_SEC", self.OUTRO_DURATION)
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

            for i, cta_text in enumerate(cta_texts):
                cta = TextClip(
                    text=cta_text,
                    font=font,
                    font_size=cta_font_size,
                    color=_rgb_to_hex(text_color),
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
                method="caption",
                size=(int(vw * 0.80), None),
            ).with_position(("center", name_y)).with_duration(duration)
            name = _fade_in_out(name, duration, hold_ratio=0.5)
            clips.append(name)

            return CompositeVideoClip(clips, size=self.video_size)
        except Exception:
            self.logger.exception("Outro creation failed — skipping.")
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
