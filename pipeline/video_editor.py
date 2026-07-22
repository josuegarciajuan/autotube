"""Video editor: assembles images + audio + subtitles + effects into final MP4."""

import os
import re
import random
import logging
import time
import subprocess
import asyncio
import hashlib
import threading
from pathlib import Path
from typing import Callable, Optional

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

from config.settings import (
    OUTPUT_DIR,
    AUDIO_DIR,
    IMAGES_DIR,
    VIDEOS_DIR,
    VIDEO_FPS,
    VIDEO_RESOLUTION,
    VIDEO_BITRATE,
    VIDEO_CODEC,
    FFMPEG_PRESET_DEFAULT,
    RENDER_TIMEOUT_MULTIPLIER,
    RENDER_TIMEOUT_MIN_SEC,
    RENDER_TIMEOUT_MAX_SEC,
    ASSETS_DIR,
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


# ── FFmpeg decoder semaphore (global, system-wide) ──────────────────
# Limits concurrent ffmpeg decoder processes to prevent RAM exhaustion
# when multiple renders run simultaneously. Each decoder consumes
# 50-300 MB RSS (up to 1.2 GB for 4K sources). With this cap, even
# 2-3 parallel renders stay within safe memory bounds.
_MAX_FFMPEG_DECODERS = 4
_ffmpeg_decoder_semaphore = threading.Semaphore(_MAX_FFMPEG_DECODERS)


def _kill_orphaned_ffmpeg_videoeditor():
    """Kill orphaned ffmpeg processes from previous crashed renders.

    Called before each build_video() to start with clean memory.
    Uses the same 3-layer strategy as generation_service.py.
    After killing, reaps zombie children to free kernel process-table slots.
    """
    import signal
    killed = 0
    reap_pids = set()

    try:
        pid = os.getpid()
        result = subprocess.run(
            ["pgrep", "-P", str(pid), "-f", "ffmpeg"],
            capture_output=True, text=True, timeout=5,
        )
        if result.stdout.strip():
            for cpid in result.stdout.strip().split():
                try:
                    os.kill(int(cpid), signal.SIGKILL)
                    reap_pids.add(int(cpid))
                    killed += 1
                except ProcessLookupError:
                    pass
    except Exception:
        pass
    try:
        result = subprocess.run(
            ["pgrep", "-P", "1", "-f", "ffmpeg"],
            capture_output=True, text=True, timeout=5,
        )
        if result.stdout.strip():
            for opid in result.stdout.strip().split():
                try:
                    os.kill(int(opid), signal.SIGKILL)
                    reap_pids.add(int(opid))
                    killed += 1
                except ProcessLookupError:
                    pass
    except Exception:
        pass

    # Reap zombie children so they don't linger in the process table
    for rpid in reap_pids:
        try:
            os.waitpid(rpid, os.WNOHANG)
        except (ChildProcessError, OSError):
            pass

    if killed > 0:
        import logging as _log
        _log.getLogger("autotube.video_editor").warning(
            "Killed %d orphaned ffmpeg process(es) + reaped zombies before render", killed
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
    SCENE_DURATION_MIN: float = 6.0   # Enforced — scenes shorter than this are merged
    SCENE_DURATION_MAX: float = 25.0  # Enforced — fewer sub-scenes = fewer unique media assets needed
    # Legacy aliases kept for backward compatibility
    MIN_SCENE_DURATION: float = SCENE_DURATION_MIN
    MAX_SCENE_DURATION: float = SCENE_DURATION_MAX
    # Hard cap: when accumulated clip duration exceeds this, force a new clip
    # from the fallback pool instead of extending the same image indefinitely.
    MAX_CLIP_EXTEND_SEC: float = 25.0

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
        # Validate visual config before any rendering starts
        # (also validated at API startup, but re-check here in case
        #  the channel config was modified since boot).
        if self.canal:
            slug = self.canal.get("slug") or self.canal.get("CANAL_NAME", "unknown")
            try:
                from config.config_validator import validate_channel_config
                vw = validate_channel_config(slug, self.canal)
                if vw:
                    for w in vw:
                        self.logger.warning("Config: %s", w)
            except Exception:
                pass  # validation is advisory, never block a render
        self.logger = logging.getLogger(self.__class__.__name__)
        # Allow per-channel/test resolution override (e.g. 480x270 for fast tests)
        self.video_size: tuple[int, int] = self.canal.get("VIDEO_RESOLUTION", VIDEO_RESOLUTION)
        self.fps: int = VIDEO_FPS

        # P3: Per-video uniform color grade (consistent across all clips)
        self._video_color_grade = None

        # P4: Asset deduplication tracking — prevents the same file from
        # being used for multiple scenes within one video build.
        # Images: LRU-based dedup (allow reuse after N different clips).
        # Videos: offset tracking — each scene gets a different segment.
        self._used_asset_paths: set[str] = set()
        self._video_offset_tracker: dict[str, float] = {}  # path → next_start_offset
        # LRU tracking for image reuse: key=path → last clip index when used
        self._image_last_clip_idx: dict[str, int] = {}
        self._current_clip_idx: int = 0
        # When reusing an image, vary Ken Burns focus to avoid visual repetition
        self._image_reuse_count: dict[str, int] = {}  # path → how many times reused

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
        job_id: int = None,                           # v3: for heartbeat emission during render
        video_id: int = None,                         # v4: for subprocess progress writes to DB
    ) -> Path:
        """Assemble the complete video from blocks, media, audio, and effects.

        v2 API (preferred):
            bloques: List of block dicts from LLM (tipo, texto, media_tipo, ...)
            media_assets: List of asset dicts from MediaFetcher (path, type, duration)
            audio_path: Path to TTS voice-over MP3
            timestamps: Word-level timestamps
            cta_audio_path: Optional voice-over for the CTA segment
            job_id: Optional job id for heartbeat emission during render.
            video_id: Optional video DB id for subprocess progress writes.

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
        
        # Collision avoidance: if output already exists (e.g. from a prior failed
        # retry), append a random suffix to prevent partial-file conflicts.
        if output_path.exists():
            import uuid
            suffix = uuid.uuid4().hex[:6]
            new_path = output_path.with_stem(f"{output_path.stem}_{suffix}")
            self.logger.info(
                "Output path %s already exists — using %s instead",
                output_path.name, new_path.name,
            )
            output_path = new_path
        
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
        self._image_last_clip_idx.clear()
        self._current_clip_idx = 0
        self._image_reuse_count.clear()

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

        # ── Heartbeat emitter (covers Steps 2-6: segment rendering through final mux) ──
        # Starts BEFORE the heavy segment rendering to prevent orphan detection
        # from killing the job during long renders (50+ scenes can take 40-90 min).
        _hb_stop = threading.Event() if job_id is not None else None
        _hb_thread = None
        if job_id is not None:
            _total_scenes = len(block_ranges)

            def _hb_loop():
                try:
                    from database.db_extended import ExtendedDatabase
                    _db = ExtendedDatabase()
                except Exception as _hb_init_exc:
                    self.logger.error("Heartbeat DB init failed: %s", _hb_init_exc)
                    return
                while not _hb_stop.is_set():
                    try:
                        _db.update_heartbeat(job_id)
                    except Exception:
                        pass
                    _hb_stop.wait(30)

            _hb_thread = threading.Thread(
                target=_hb_loop, daemon=True,
                name=f"heartbeat-render-{job_id}",
            )
            _hb_thread.start()
            self.logger.info(
                "Heartbeat emitter started for job #%d (every 30s, covers Steps 2-6 / %d scenes)",
                job_id, _total_scenes,
            )

        # ── v4: Segment-based body rendering (RAM bounded) ─────
        # Instead of creating all clips in memory (CompositeVideoClip → OOM),
        # render each scene as a standalone MP4 segment (1 decoder at a time),
        # then concatenate with ffmpeg xfade. Peak RAM = ~400 MB, constant.
        self.logger.info("Step 2/6: Rendering %d scene segments (RAM bounded)…", len(block_ranges))
        tts_duration = timestamps[-1].get("end", 0) if timestamps else 0

        # Collect fallback image pool (images only, videos would crash PIL)
        _real_image_paths: list[Path] = []
        if media_assets:
            for a in media_assets:
                ap = a.get("path")
                if ap and a.get("type") == "image" and Path(ap).exists():
                    _real_image_paths.append(Path(ap))

        # Temp dir for scene segments (resolved absolute to prevent ffmpeg concat path doubling)
        # Use video_id instead of job_id so segments survive API restarts and
        # reassembly retries (job_id changes, video content stays the same).
        seg_dir = Path(VIDEOS_DIR).resolve() / "segments"
        if video_id is not None and video_id > 0:
            seg_dir = seg_dir / str(video_id)
        elif job_id is not None:
            seg_dir = seg_dir / str(job_id)
        seg_dir.mkdir(parents=True, exist_ok=True)

        # ── Render checkpoint: scan for already-completed segments ──
        # Segments persist on disk after a failed render.  On reassembly,
        # skip re-rendering segments that already have a valid primary
        # output file (scene_NNNN.mp4, not _fb.mp4 or _black.mp4).
        completed_scenes: set[int] = set()
        _stale_fb = 0
        _stale_black = 0
        if seg_dir.exists():
            for f in seg_dir.glob("scene_*.mp4"):
                _stem = f.stem
                # Only accept primary renders (not fallback _fb or placeholder _black)
                if "_fb" in _stem or "_black" in _stem:
                    if "_fb" in _stem:
                        try:
                            f.unlink(missing_ok=True)
                            _stale_fb += 1
                        except OSError:
                            pass
                    elif "_black" in _stem:
                        try:
                            f.unlink(missing_ok=True)
                            _stale_black += 1
                        except OSError:
                            pass
                    continue
                # Extract scene index from "scene_NNNN"
                try:
                    _idx = int(_stem.split("_")[1])
                except (IndexError, ValueError):
                    continue
                if f.stat().st_size > 0:
                    completed_scenes.add(_idx)
                else:
                    # Corrupt/empty file — delete so it gets re-rendered
                    try:
                        f.unlink(missing_ok=True)
                    except OSError:
                        pass
        if completed_scenes:
            self.logger.info(
                "Resuming from checkpoint: %d/%d scenes already rendered "
                "(%d stale _fb, %d stale _black cleaned)",
                len(completed_scenes), len(block_ranges),
                _stale_fb, _stale_black,
            )

        segment_paths: list[str] = []
        self._current_clip_idx = 0
        n_fallback = 0
        rendered_this_run = 0

        for i, br in enumerate(block_ranges):
            # ── Checkpoint: skip already-rendered scenes ─────
            if i in completed_scenes:
                _existing = seg_dir / f"scene_{i:04d}.mp4"
                if _existing.exists() and _existing.stat().st_size > 0:
                    segment_paths.append(str(_existing.resolve()))
                    # Progress for skipped scenes
                    if video_id is not None and video_id > 0:
                        _total = len(block_ranges)
                        _update_interval = max(1, _total // 10)
                        _total_done = len(completed_scenes)  # already the final count for this scene
                        if (i == 0 or i == _total - 1
                                or (i + 1) % _update_interval == 0):
                            _pct = 60 + int(_total_done / _total * 15)
                            try:
                                from database.db_extended import ExtendedDatabase
                                ExtendedDatabase().update_video(
                                    video_id,
                                    progress=_pct,
                                    progress_phase="video",
                                )
                            except Exception:
                                pass
                    self._current_clip_idx += 1
                    continue
            # Match asset to scene (1:1 with scene_ranges)
            asset = None
            if scene_ranges:
                asset = media_assets[i] if i < len(media_assets) else None
            else:
                asset_idx = br.get("asset_idx", i)
                asset = media_assets[asset_idx] if asset_idx < len(media_assets) else None

            if asset is None:
                asset = {"type": "placeholder", "path": None}

            seg_path = seg_dir / f"scene_{i:04d}.mp4"
            media_type = asset.get("type", "?")

            # Try render as segment (1 attempt + 1 retry on failure)
            result_path = self._render_scene_segment(
                br, asset, str(seg_path), clip_idx=i,
                fallback_pool=_real_image_paths,
            )

            if result_path is None:
                # Scene failed or returned None (no media) — retry with fallback image
                self.logger.warning(
                    "  Scene %d FAILED (type=%s) — retrying with fallback image", i, media_type,
                )
                if _real_image_paths:
                    # Filter out images already used in this video
                    _available = [p for p in _real_image_paths
                                  if str(p) not in self._used_asset_paths]
                    if _available:
                        fb_asset = {
                            "type": "image",
                            "path": str(random.choice(_available)),
                        }
                        fb_path = seg_dir / f"scene_{i:04d}_fb.mp4"
                        result_path = self._render_scene_segment(
                            br, fb_asset, str(fb_path), clip_idx=i,
                            fallback_pool=_available,
                        )
                if result_path is None:
                    self.logger.warning(
                        "  Scene %d [%s]: ULTIMATE FALLBACK — using black placeholder",
                        i + 1, br.get("tipo", "?"),
                    )
                    result_path = self._render_black_segment(br, seg_dir / f"scene_{i:04d}_black.mp4")
                    n_fallback += 1
                else:
                    n_fallback += 1

            if result_path:
                segment_paths.append(result_path)
            else:
                # Last resort — can't happen with _render_black_segment, but be safe
                self.logger.error("  Scene %d: CRITICAL — could not render placeholder", i)
                segment_paths.append("")  # placeholder; will be caught by concat

            self._current_clip_idx += 1
            rendered_this_run += 1

            # ── v4: subprocess progress via DB ─────────────────
            # Only active when called from the pipeline worker
            # (video_id is set).  Writes ~10 progress updates
            # across the segment loop so the frontend sees real
            # progress instead of being stuck at 60 %.
            # Includes checkpoint-resumed scenes in the total.
            if video_id is not None and video_id > 0:
                _total = len(block_ranges)
                _update_interval = max(1, _total // 10)
                if (i == 0 or i == _total - 1
                        or (i + 1) % _update_interval == 0):
                    _total_done = len(completed_scenes) + rendered_this_run
                    _pct = 60 + int(_total_done / _total * 15)
                    try:
                        from database.db_extended import ExtendedDatabase
                        ExtendedDatabase().update_video(
                            video_id,
                            progress=_pct,
                            progress_phase="video",
                        )
                    except Exception:
                        pass  # never let a DB write crash the render

        # Remove empty entries AND corresponding block_ranges to keep 1:1 alignment
        _valid = []
        _valid_ranges = []
        for _j, (_sp, _br) in enumerate(zip(segment_paths, block_ranges)):
            if _sp and Path(_sp).exists():
                _valid.append(_sp)
                _valid_ranges.append(_br)
        segment_paths, block_ranges = _valid, _valid_ranges

        if not segment_paths:
            raise RuntimeError("No segments rendered — cannot build video.")

        self.logger.info(
            "  %d segments rendered (%d fallback, %d image/%d video ratio)",
            len(segment_paths), n_fallback,
            sum(1 for a in media_assets if a.get("type") == "image"),
            sum(1 for a in media_assets if a.get("type") == "video"),
        )

        # ── v6: merge consecutive black placeholders ──────────
        # When many consecutive scenes fail to get media (e.g. due to
        # off-niche theme poisoning), the concat gets overloaded with
        # black placeholder segments that add no visual value but consume
        # ffmpeg xfade processing. Merging them reduces segment count
        # dramatically and prevents xfade concat timeouts.
        segment_paths, block_ranges = _merge_consecutive_placeholders(
            segment_paths, block_ranges, seg_dir, self.logger,
        )

        # ── Concat segments with crossfades (+ grain + vignette) ──
        self.logger.info("Step 3/6: Concatenating %d segments with xfade…", len(segment_paths))
        body_path = seg_dir / "body.mp4"
        body_segment_path = self._concat_body_with_crossfades(
            segment_paths, block_ranges, str(body_path),
        )

        # Load body as VideoClip for final assembly (cheap — single file)
        body_video = VideoFileClip(body_segment_path)

        # ── Tack on CTA clip at body end if requested ──────────
        # (Some tests pass cta_audio_path to force a follow-up clip)

        self.logger.info("Step 4/6: Body video assembled (%d segments → %.1fs)",
                         len(segment_paths), self._dur(body_video))

        # ── Subtitles (toggleable) ─────────────────────────────
        subtitles_enabled = self.canal.get("SUBTITLES_ENABLED", False)
        if subtitles_enabled:
            self.logger.info("  Adding animated subtitles…")
            subtitle_clips = self._build_subtitles(timestamps, self.video_size)
            body_video = CompositeVideoClip(
                [body_video] + subtitle_clips, size=self.video_size,
            )
        else:
            self.logger.info("  Subtitles disabled (SUBTITLES_ENABLED=False)")

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

        # ── Intro + Outro voice generation (TTS from config templates) ──
        intro_voice_path: Optional[Path] = None
        intro_voice_clip = None
        intro_voice_dur: float = 0.0
        intro_voice_text = self.canal.get("INTRO_VOICE_TEXT", "")
        if intro_voice_text:
            intro_voice_path = self._tts_template_voice(intro_voice_text)
            if intro_voice_path:
                intro_voice_dur = self._get_voice_duration(intro_voice_path)
                try:
                    intro_voice_clip = AudioFileClip(str(intro_voice_path))
                except Exception as exc:
                    self.logger.warning("Failed to load intro voice: %s", exc)

        outro_voice_path: Optional[Path] = None
        outro_voice_clip = None
        outro_voice_dur: float = 0.0
        outro_voice_text = self.canal.get("OUTRO_VOICE_TEXT", "")
        if outro_voice_text:
            outro_voice_path = self._tts_template_voice(outro_voice_text)
            if outro_voice_path:
                outro_voice_dur = self._get_voice_duration(outro_voice_path)
                try:
                    outro_voice_clip = AudioFileClip(str(outro_voice_path))
                except Exception as exc:
                    self.logger.warning("Failed to load outro voice: %s", exc)

        self.logger.info("Template voices: intro=%s (%.1fs) outro=%s (%.1fs)",
                         "yes" if intro_voice_clip else "no", intro_voice_dur,
                         "yes" if outro_voice_clip else "no", outro_voice_dur)

        # ── CTA clip (between body and outro) ──────────────────
        self.logger.info("Step 4/6: Building CTA clip (%.1fs)…", cta_dur)
        cta_clip = self._build_cta(cta_dur, cta_audio_path)

        # ── Intro + Outro ──────────────────────────────────────
        self.logger.info("Step 5/6: Adding intro + outro…")
        intro_dur = max(
            self.canal.get("INTRO_DURATION_SEC", self.INTRO_DURATION),
            intro_voice_dur + 0.5 if intro_voice_dur > 0 else 0,
        )
        outro_dur = max(
            self.canal.get("OUTRO_DURATION_SEC", self.OUTRO_DURATION),
            outro_voice_dur + 0.5 if outro_voice_dur > 0 else 0,
        )
        intro = self._build_intro(intro_dur)
        outro = self._build_outro(outro_dur)

        # ── Assemble final video via concat demuxer ───────────────
        # Sequence: INTRO → BODY → CTA → OUTRO
        # Render intro/cta/outro as standalone segments, then concat all
        # using ffmpeg concat demuxer (stream-copy, near-zero RAM).
        self.logger.info("Step 5/6: Rendering intro/cta/outro segments + final concat…")

        # Render intro/cta/outro + body as segment files for concat
        _video_segments: list[str] = []

        def _render_seg(clip, path):
            """Render a single clip to video-only MP4 to a given path."""
            p = Path(path)
            p.parent.mkdir(parents=True, exist_ok=True)
            # Collision avoidance (same pattern as _render_scene_segment)
            if p.exists():
                import uuid
                p = p.with_stem(f"{p.stem}_{uuid.uuid4().hex[:4]}")
            try:
                clip.write_videofile(
                    str(p), fps=self.fps, codec=VIDEO_CODEC,
                    preset=self.canal.get("FFMPEG_PRESET", FFMPEG_PRESET_DEFAULT),
                    bitrate=VIDEO_BITRATE,
                    ffmpeg_params=["-pix_fmt", "yuv420p", "-an"],
                    logger=None,
                )
                return str(p.resolve())
            except Exception as e:
                self.logger.warning("Segment render failed for %s: %s", path, e)
                return None
            finally:
                try:
                    clip.close()
                except Exception:
                    pass

        # INTRO
        if intro is not None:
            res = _render_seg(intro.with_audio(self._silent_audio(intro_dur)), seg_dir / "intro.mp4")
            if res:
                _video_segments.append(res)
        # BODY
        _video_segments.append(body_segment_path)
        # CTA
        if cta_clip is not None:
            res = _render_seg(cta_clip, seg_dir / "cta.mp4")
            if res:
                _video_segments.append(res)
        # OUTRO
        if outro is not None:
            res = _render_seg(outro.with_audio(self._silent_audio(outro_dur)), seg_dir / "outro.mp4")
            if res:
                _video_segments.append(res)

        # Concat demuxer: stream-copy, no re-encode, RAM ~0
        # Use absolute paths to avoid relative-path confusion with seg_dir
        concat_list = seg_dir / "concat_list.txt"
        concat_list.write_text("\n".join(
            f"file '{Path(p).resolve()}'" for p in _video_segments if p
        ) + "\n")
        concat_output = output_path.with_suffix(".concat.mp4")
        result = subprocess.run([
            "ffmpeg", "-y", "-v", "error",
            "-f", "concat", "-safe", "0", "-i", str(concat_list),
            "-c", "copy",
            "-movflags", "+faststart",
            str(concat_output),
        ], capture_output=True, text=True, timeout=120)
        if result.returncode != 0:
            stderr_tail = result.stderr.strip()[-500:] if result.stderr else ""
            raise RuntimeError(f"ffmpeg concat failed (rc={result.returncode}): {stderr_tail}")
        if not concat_output.exists() or concat_output.stat().st_size == 0:
            raise RuntimeError("ffmpeg concat produced empty output")

        # Replace output_path with concat result
        output_path.unlink(missing_ok=True)
        concat_output.rename(output_path)

        # ── Assemble final audio sequence ──────────────────────
        # v2 with transitions: body audio = narration segments + transition silences + optional music
        # Sequence: intro_voice + BODY (narration + transitions) + CTA_audio + outro_voice
        # Each segment audio is padded to match its corresponding video segment duration.
        audio_parts: list[AudioClip] = []
        # Intro voice padded to intro_dur
        if intro_voice_clip is not None:
            intro_pad = max(0.0, intro_dur - intro_voice_dur)
            if intro_pad > 0.01:
                audio_parts.append(concatenate_audioclips(
                    [intro_voice_clip, self._silent_audio(intro_pad)]
                ))
            else:
                audio_parts.append(intro_voice_clip)
        else:
            audio_parts.append(self._silent_audio(intro_dur))

        # ── Build body audio: narration segments with transition silences ──
        tts_audio = AudioFileClip(audio_path)
        body_narr_parts: list[AudioClip] = []
        pos: float = 0.0  # cursor within the TTS audio (seconds)
        for scene_idx, br in enumerate(block_ranges):
            if br.get("is_transition"):
                body_narr_parts.append(self._silent_audio(br["duration"]))
            else:
                seg_dur = br["duration"]
                available = max(0.0, tts_duration - pos)
                if available < seg_dur and not br.get("is_transition"):
                    self.logger.warning(
                        "Audio truncation: scene %d needs %.1fs but only %.1fs TTS audio remaining "
                        "(tts_duration=%.1fs, pos=%.1fs). TTS may have failed for later blocks.",
                        scene_idx + 1, seg_dur, available, tts_duration, pos,
                    )
                seg_dur = min(seg_dur, available)
                if seg_dur > 0.01:
                    body_narr_parts.append(tts_audio.subclipped(pos, pos + seg_dur))
                    pos += seg_dur

        # ── Append any remaining TTS audio not covered by block_ranges ──
        leftover = tts_duration - pos
        if leftover > 0.1:
            self.logger.warning(
                "TTS audio has %.1fs leftover not covered by block_ranges — appending to body narration",
                leftover,
            )
            body_narr_parts.append(tts_audio.subclipped(pos, pos + leftover))
            pos += leftover

        body_narration = concatenate_audioclips(body_narr_parts)
        body_dur = self._dur(body_narration)

        # Compute expected body duration from block_ranges (sum of all scene durations)
        expected_body_dur = sum(br["duration"] for br in block_ranges)
        gap = expected_body_dur - body_dur
        if gap > 0.5:
            self.logger.warning(
                "Body narration (%.1fs) is shorter than expected (%.1fs) by %.1fs. "
                "Padding with silence to avoid video played without audio.",
                body_dur, expected_body_dur, gap,
            )
            body_narration = concatenate_audioclips([body_narration, self._silent_audio(gap)])
            body_dur = self._dur(body_narration)
        elif gap < -0.5:
            self.logger.warning(
                "Body narration (%.1fs) is LONGER than expected (%.1fs) by %.1fs. "
                "Video body may be too short — last frames will freeze.",
                body_dur, expected_body_dur, -gap,
            )

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
        # Outro voice padded to outro_dur
        if outro_voice_clip is not None:
            outro_pad = max(0.0, outro_dur - outro_voice_dur)
            if outro_pad > 0.01:
                audio_parts.append(concatenate_audioclips(
                    [outro_voice_clip, self._silent_audio(outro_pad)]
                ))
            else:
                audio_parts.append(outro_voice_clip)
        else:
            audio_parts.append(self._silent_audio(outro_dur))

        final_audio = concatenate_audioclips(audio_parts)

        # ── v4: Video already rendered (concat demuxer) ─────────
        # Skip MoviePy write_videofile — the video exists on disk.
        # Just validate the output and proceed to audio re-mux.
        self.logger.info("Step 6/6: Video assembled via segments — applying audio post-process…")

        # ── Timeline sync check ──────────────────────────────────
        body_vid_dur = self._dur(body_video)
        body_aud_dur = self._dur(body_audio)
        cta_has_voice = cta_audio_clip is not None
        self.logger.info(
            "Timeline: intro=%.1fs | body_video=%.1fs body_audio=%.1fs (match=%s) | "
            "cta=%.1fs (voice=%s) | outro=%.1fs | TOTAL=%.1fs",
            intro_dur, body_vid_dur, body_aud_dur,
            "OK" if abs(body_vid_dur - body_aud_dur) < 0.5 else "MISMATCH!",
            cta_dur, "yes" if cta_has_voice else "no",
            outro_dur,
            intro_dur + body_vid_dur + cta_dur + outro_dur,
        )
        if abs(body_vid_dur - body_aud_dur) >= 0.5:
            self.logger.warning(
                "⚠ Body video/audio mismatch: video=%.1fs audio=%.1fs gap=%.1fs",
                body_vid_dur, body_aud_dur, abs(body_vid_dur - body_aud_dur),
            )

        render_ok = output_path.exists() and output_path.stat().st_size > 0
        try:
            if render_ok:
                self.logger.info("✅ Video saved → %s (%.1f MB)",
                                 output_path, output_path.stat().st_size / 1024 / 1024)
                # ── Audio post-process: use final_audio (full sequence with intro/body/CTA/outro) ──
                _fix_path = output_path.with_suffix('.audio_fix.mp4')
                try:
                    import subprocess as _sp2

                    # Write final_audio to a temp AAC file for ffmpeg mux
                    _final_audio_temp = output_path.with_suffix('.final_audio.aac')
                    final_audio.write_audiofile(
                        str(_final_audio_temp),
                        codec='aac',
                        bitrate='192k',
                        fps=44100,
                        logger=None,
                    )

                    # Mux concat video + final_audio
                    _sp2.run([
                        'ffmpeg', '-y', '-v', 'error',
                        '-i', str(output_path),           # 0:v — concat video
                        '-i', str(_final_audio_temp),      # 1:a — full audio sequence
                        '-c:v', 'copy',
                        '-c:a', 'aac', '-b:a', '192k',
                        '-map', '0:v:0', '-map', '1:a:0',
                        '-movflags', '+faststart',
                        str(_fix_path),
                    ], check=True, timeout=180)

                    # Cleanup temp audio
                    try:
                        _final_audio_temp.unlink(missing_ok=True)
                    except Exception:
                        pass

                    # Replace original with fixed version
                    output_path.unlink()
                    _fix_path.rename(output_path)
                    self.logger.info("🔊 Audio post-processed: full sequence (intro+body+CTA+outro)")
                except Exception as _audio_fix_e:
                    self.logger.warning("Audio post-process failed (%s) — keeping original", _audio_fix_e)
                    if _fix_path.exists():
                        _fix_path.unlink(missing_ok=True)
                    if '_final_audio_temp' in dir() and _final_audio_temp.exists():
                        _final_audio_temp.unlink(missing_ok=True)
            else:
                self.logger.error("❌ Video file missing or empty: %s", output_path)
                raise RuntimeError(f"Video rendering failed: no output file at {output_path}")
        finally:
            # ── Stop heartbeat emitter ───────────────────────────
            if _hb_stop is not None:
                _hb_stop.set()
            if _hb_thread is not None and _hb_thread.is_alive():
                _hb_thread.join(timeout=5)
                if _hb_thread.is_alive():
                    self.logger.warning("Heartbeat thread did not stop within 5s for job #%d", job_id)
                else:
                    self.logger.info("Heartbeat emitter stopped for job #%d", job_id)

            # ── Cleanup ──────────────────────────────────────
            try:
                final_audio.close()
            except Exception:
                pass
            try:
                body_video.close()
            except Exception:
                pass
            # Force garbage collection
            import gc
            gc.collect()
            # Clean up temp segments — only on success.
            # On failure, leave segments on disk so the next reassembly can
            # resume from the checkpoint instead of re-rendering everything.
            try:
                import shutil
                if seg_dir.exists():
                    if render_ok:
                        shutil.rmtree(seg_dir, ignore_errors=True)
                        self.logger.info("🧹 Segment tempdir cleaned (after success): %s", seg_dir.name)
                    else:
                        self.logger.info(
                            "📦 Segment tempdir preserved for reassembly checkpoint: %s "
                            "(%d completed scenes out of %d)",
                            seg_dir.name, len(completed_scenes), len(block_ranges),
                        )
            except Exception:
                self.logger.debug("Could not clean segment tempdir: %s", seg_dir)
            # Clean up temp music
            if _music_temp_path and _music_temp_path.exists():
                try:
                    _music_temp_path.unlink()
                    self.logger.info("🧹 Temp music deleted: %s", _music_temp_path.name)
                except Exception:
                    pass

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

        # Safety: extend last range to cover full audio in proportional fallback
        if ranges and total_audio > 0:
            ranges[-1]["end"] = max(ranges[-1]["end"], total_audio)
            ranges[-1]["duration"] = ranges[-1]["end"] - ranges[-1]["start"]

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
        # falls below min_dur with the next scene. The outer loop runs
        # at most len(merged) times (each merge pass reduces the list
        # by at least one element). A hard safety cap prevents infinite
        # loops in pathological cases.
        merged = list(block_ranges)  # work on a mutable copy
        changed = True
        safety = 0
        max_safety = max(len(merged), 1) + 3  # generous upper bound
        while changed and safety < max_safety:
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

        if safety >= max_safety:
            self.logger.warning(
                "Scene merge safety limit reached (%d iterations) — "
                "some short scenes may remain. min_dur=%.1f",
                safety, min_dur,
            )

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
                           clip_idx: int = 0,
                           fallback_pool: list = None) -> Optional[VideoClip]:
        """Create a clip for one block: video, image, or None (no real media).

        When no real media is available (placeholder / duplicate / missing path),
        returns ``None`` so the caller can merge the scene with its neighbour
        instead of rendering the blue-text placeholder.

        Image deduplication (strict):
        - Images: NEVER reused within the same video. If an image path or
          content_hash was already used, returns None immediately.
        - Videos: offset tracking — different scenes get different segments
          of the same source video file (no more frozen/looped frames).
        """
        media_type = asset.get("type", "placeholder")
        block_dur = block_range["duration"]
        asset_path = str(asset.get("path", "")) if asset.get("path") else ""
        content_hash = asset.get("content_hash", "")

        # ── Image dedup: STRICT — never reuse any image in the same video ─
        if media_type == "image" and asset_path and asset_path in self._used_asset_paths:
            self.logger.warning(
                "Image %s already used in this video — returning None (will use fallback)",
                Path(asset_path).name,
            )
            return None
        if media_type == "image" and content_hash and content_hash in self._used_asset_paths:
            self.logger.warning(
                "Image hash %s already used in this video — returning None",
                content_hash[:12],
            )
            return None

        # Initialize per-video color grade on first non-placeholder clip (P3)
        if self._video_color_grade is None and media_type not in ("placeholder", "duplicate"):
            self._video_color_grade = {
                "contrast": random.uniform(1.02, 1.04),
                "brightness": random.uniform(0.99, 1.01),
                "saturation": random.uniform(0.97, 1.03),
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
                # Source exhausted or too short — reset offset so next scene
                # can loop from the beginning instead of failing permanently.
                self._video_offset_tracker[asset_path] = 0.0
                self.logger.info(
                    "Video %s failed/exhausted at offset %.1fs — falling back to image",
                    Path(asset_path).name, offset,
                )
                # Try fallback: use a random image from the pool instead of extending
                # the previous clip (which would lose the video scene entirely).
                if fallback_pool:
                    import random as _random2
                    # Shuffle pool and find first unused image to avoid duplicates
                    _shuffled = list(fallback_pool)
                    _random2.shuffle(_shuffled)
                    fb_path = None
                    for _fb in _shuffled:
                        _fb_str = str(_fb)
                        if _fb_str not in self._used_asset_paths:
                            fb_path = _fb_str
                            break
                    if fb_path:
                        self.logger.info("  Using fallback image for scene: %s", Path(fb_path).name)
                        clip = self._image_clip_for_block(Path(fb_path), block_dur)
                        self._used_asset_paths.add(fb_path)
                        self._image_last_clip_idx[fb_path] = self._current_clip_idx
                    else:
                        self.logger.warning("  All fallback images already used — returning None")
                        return None
                else:
                    return None
            else:
                # Advance the offset so the next scene (even with same file)
                # starts where this one ended.
                self._video_offset_tracker[asset_path] = offset + block_dur
                self._used_asset_paths.add(asset_path)
                if content_hash:
                    self._used_asset_paths.add(content_hash)
        if clip is None and asset_path and Path(asset_path).exists():
            clip = self._image_clip_for_block(Path(asset_path), block_dur)
            self._used_asset_paths.add(asset_path)
            self._image_last_clip_idx[asset_path] = self._current_clip_idx
            if content_hash:
                self._used_asset_paths.add(content_hash)
                self._image_last_clip_idx[content_hash] = self._current_clip_idx

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

        **Looping**: if the source video runs out before reaching
        ``start_offset + block_dur``, the clip wraps modulo the source
        duration.  This allows short Pexels clips (8-16s) to feed
        multiple sub-scenes by looping instead of failing immediately.

        Args:
            video_path: Absolute path to the source video file.
            block_dur:  Desired scene duration in seconds.
            start_offset:  Offset in seconds into the source where
                           this scene should begin.
        """
        try:
            clip = VideoFileClip(str(video_path))
            clip_dur = clip.duration() if callable(clip.duration) else clip.duration

            # Cap source resolution to output size to prevent
            # imageio-ffmpeg from decoding 4K frames in raw RGB.
            if clip.w > self.video_size[0] or clip.h > self.video_size[1]:
                clip = clip.resized(self.video_size)

            # ── Looping: wrap offset modulo clip duration ──────────
            # Prevents exhaustion for short stock videos (Pexels 8-16s)
            # while still advancing the offset sequentially between scenes.
            effective_start = start_offset % clip_dur
            effective_end = effective_start + block_dur

            if effective_end > clip_dur:
                # ── Clip wraps past the end — use freeze-frame extension ──
                # MoviePy v2 concat(subclip) corrupts frames across looping.
                # If > 50% of the scene would be freeze-frame, return None
                # so the caller uses a Ken Burns image from the fallback pool
                # instead of showing a mostly-static freeze-frame.
                effective_start = start_offset % clip_dur
                remaining = effective_end - clip_dur
                if remaining > block_dur * 0.5:
                    return None
                if clip_dur > 0 and remaining > 0.5:
                    try:
                        _clipped = clip.subclipped(effective_start, clip_dur)
                        _last_frame_arr = _clipped.get_frame(clip_dur - effective_start - 0.01)
                        _freeze = ImageClip(_last_frame_arr, duration=remaining)
                        if MOVIEPY_V2:
                            _freeze = _freeze.resized(self.video_size)
                        clip = concatenate_videoclips([_clipped, _freeze])
                    except Exception:
                        # Fallback: just trim to end, crossfade bridges the gap
                        clip = clip.subclipped(effective_start, min(effective_end, clip_dur))
                else:
                    clip = clip.subclipped(effective_start, min(effective_end, clip_dur))
            else:
                clip = clip.subclipped(effective_start, effective_end)

            # Apply subtle zoom for cinematic look.
            # Wrapped in try/except: double-resize on concatenated (looped) clips
            # can produce corrupt frames that crash ffmpeg downstream.
            try:
                zoom_factor = random.uniform(1.03, 1.05)
                zoom_w = int(self.video_size[0] * zoom_factor)
                zoom_h = int(self.video_size[1] * zoom_factor)
                clip = clip.resized((zoom_w, zoom_h)).resized(self.video_size)
            except Exception as zoom_err:
                self.logger.warning(
                    "Video zoom failed for %s (%s) — using clip without zoom",
                    video_path.name, zoom_err,
                )
            return clip
        except Exception as exc:
            self.logger.exception("VideoFileClip failed for %s, returning None for fallback", video_path)
            # Kill orphaned ffmpeg decoders left by the failed MoviePy open
            _kill_orphaned_ffmpeg_videoeditor()
            # Advance offset so subsequent scenes don't retry the same broken file
            self._video_offset_tracker[video_path] = start_offset + block_dur
            return None

    def _image_clip_for_block(self, image_path: Path, block_dur: float,
                               reuse_count: int = 0) -> VideoClip:
        """Create a Ken Burns image clip for the given duration.

        When *reuse_count* > 0 (same image used again after LRU gap),
        the Ken Burns parameters are varied to avoid visual monotony:
        even reuse → zoom-out (pull back), odd reuse → zoom-in (push in),
        and the zoom range is shifted to explore different areas of the image.
        """
        zoom_min = self.canal.get("KEN_BURNS_ZOOM_MIN", KEN_BURNS_ZOOM_MIN)
        zoom_max = self.canal.get("KEN_BURNS_ZOOM_MAX", KEN_BURNS_ZOOM_MAX)

        if reuse_count > 0:
            # Shift the zoom range to explore different image regions
            shift = reuse_count * 2
            zoom_min = min(zoom_min + shift, 18)  # cap both ends to avoid pixelation
            zoom_max = min(zoom_max + shift, 18)  # and prevent zoom_min > zoom_max
            # Alternate direction: even reuse → zoom-out, odd → zoom-in
            if reuse_count % 2 == 0:
                zoom = random.uniform(zoom_max * 0.9, zoom_max)  # start close, pull back
            else:
                zoom = random.uniform(zoom_min, zoom_min * 1.1)  # start wide, push in
            self.logger.debug(
                "Ken Burns reused image: count=%d zoom=%.1f range=[%.1f, %.1f] alt=%s",
                reuse_count, zoom, zoom_min, zoom_max,
                "zoom-out" if reuse_count % 2 == 0 else "zoom-in",
            )
        else:
            zoom = random.uniform(zoom_min, zoom_max)

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

        FAST PATH: when zoom_percent == 0, returns a simple ImageClip
        without per-frame recalculations (used in fast-test mode).
        """
        # ── Fast path: no zoom → simple ImageClip ────────────
        if zoom_percent == 0:
            try:
                return ImageClip(str(image_path), duration=duration).resized(self.video_size)
            except Exception:
                self.logger.exception("Failed to open %s for static clip", image_path)
                return self._placeholder_clip(duration)

        try:
            pil_img = Image.open(image_path).convert("RGB")
        except Exception:
            self.logger.exception("Failed to open %s", image_path)
            return self._placeholder_clip(duration)

        target_w, target_h = self.video_size
        src_w, src_h = pil_img.size

        # ── Memory guard: cap source images to 2560 px max dimension ──
        # Stock images can be 6000×4000+ (72 MB each in raw RGB).
        # Ken Burns zoom (max 8%) only needs ~2× output resolution,
        # so we downscale to 2560 px max side BEFORE creating the VideoClip
        # closure. This reduces per-image memory from 50-72 MB down to ~13 MB
        # while retaining more than enough resolution for smooth zoom.
        MAX_SOURCE_DIM = 2560
        if max(src_w, src_h) > MAX_SOURCE_DIM:
            orig_w, orig_h = src_w, src_h
            ratio = MAX_SOURCE_DIM / max(src_w, src_h)
            new_w, new_h = int(src_w * ratio), int(src_h * ratio)
            pil_img = pil_img.resize((new_w, new_h), Image.LANCZOS)
            src_w, src_h = new_w, new_h
            self.logger.debug(
                "Ken Burns source downscaled: %dx%d → %dx%d",
                orig_w, orig_h, src_w, src_h,
            )

        if src_w < target_w or src_h < target_h:
            ratio = max(target_w / src_w, target_h / src_h)
            new_w, new_h = int(src_w * ratio), int(src_h * ratio)
            pil_img = pil_img.resize((new_w, new_h), Image.LANCZOS)
            src_w, src_h = new_w, new_h

        zoom_factor = 1.0 + zoom_percent / 100.0

        # Pick the axis with more available panning room (pixels).
        # Portrait/square images pan vertically; landscape horizontally.
        # Guarantees at least one axis is non-zero.
        if (src_h - target_h) > (src_w - target_w):
            pan_dir_x, pan_dir_y = 0, random.choice([-1, 1])
        else:
            pan_dir_x, pan_dir_y = random.choice([-1, 1]), 0

        def make_frame(t: float) -> np.ndarray:
            progress = t / duration if duration > 0 else 0
            z = 1.0 + (zoom_factor - 1.0) * progress

            new_w = int(src_w * z)
            new_h = int(src_h * z)
            scaled = pil_img.resize((new_w, new_h), Image.LANCZOS)

            max_ox = max(0, (new_w - target_w) / 2)
            max_oy = max(0, (new_h - target_h) / 2)

            # Start centered, pan out to one edge.
            # Clamp (below) is a safety net — with this formula it is never hit.
            ox = max_ox * pan_dir_x * progress
            oy = max_oy * pan_dir_y * progress

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
        No external API or network access required.

        **v3: chunked synthesis** — generates the waveform in 30-second blocks
        to keep peak memory ~80 MB regardless of total duration, instead of
        1.5 GB+ for a 14-minute production video.
        """
        import numpy as np
        from pydub import AudioSegment

        sample_rate = 44100
        CHUNK_SEC = 30  # process 30 seconds at a time → ~80 MB peak per chunk
        n_chunks = int(np.ceil(duration_sec / CHUNK_SEC))
        all_chunks: list[AudioSegment] = []

        out_path = OUTPUT_DIR / "temp" / f"ambient_{int(time.time())}.mp3"
        out_path.parent.mkdir(parents=True, exist_ok=True)
        fade_samples = int(sample_rate * 0.5)  # 0.5s fade

        for ci in range(n_chunks):
            chunk_start = ci * CHUNK_SEC
            chunk_dur = min(CHUNK_SEC, duration_sec - chunk_start)
            # Add crossfade buffer (1s overlap) between chunks to avoid clicks
            overlap_buffer = 1.0 if ci < n_chunks - 1 else 0.0
            chunk_total = chunk_dur + overlap_buffer

            n_samples = int(sample_rate * chunk_total)
            t = np.linspace(0, chunk_total, n_samples, endpoint=False, dtype=np.float32)

            # Ambient drone: fundamental + soft harmonics, stereo
            fund_l = np.sin(2 * np.pi * 58.0 * (t + chunk_start), dtype=np.float32)
            fund_r = np.sin(2 * np.pi * 58.0 * (t + chunk_start) + 0.15, dtype=np.float32)
            harm_l = 0.35 * np.sin(2 * np.pi * 87.0 * (t + chunk_start), dtype=np.float32)
            harm_r = 0.35 * np.sin(2 * np.pi * 87.0 * (t + chunk_start) + 0.35, dtype=np.float32)
            octave_l = 0.15 * np.sin(2 * np.pi * 116.0 * (t + chunk_start), dtype=np.float32)
            octave_r = 0.15 * np.sin(2 * np.pi * 116.0 * (t + chunk_start) + 0.25, dtype=np.float32)

            swell = 0.55 + 0.45 * np.sin(
                2 * np.pi * 0.28 * (t + chunk_start)
                + np.sin(2 * np.pi * 0.09 * (t + chunk_start)) * 1.5,
                dtype=np.float32,
            )

            left = (fund_l + harm_l + octave_l) * swell
            right = (fund_r + harm_r + octave_r) * swell

            # Crossfade edges between chunks (linear ramp on overlap buffer)
            if ci > 0:
                cross_len = int(sample_rate * 1.0)
                ramp_in = np.linspace(0, 1, cross_len, dtype=np.float32)
                left[:cross_len] *= ramp_in
                right[:cross_len] *= ramp_in
            if ci < n_chunks - 1 and overlap_buffer > 0:
                cross_len = int(sample_rate * overlap_buffer)
                if cross_len > 0:
                    ramp_out = np.linspace(1, 0, cross_len, dtype=np.float32)
                    left[-cross_len:] *= ramp_out
                    right[-cross_len:] *= ramp_out

            # Fade in on first chunk, fade out on last
            if ci == 0 and fade_samples < n_samples:
                ramp = np.linspace(0, 1, fade_samples, dtype=np.float32)
                left[:fade_samples] *= ramp
                right[:fade_samples] *= ramp
            if ci == n_chunks - 1 and fade_samples < n_samples:
                ramp = np.linspace(1, 0, fade_samples, dtype=np.float32)
                left[-fade_samples:] *= ramp
                right[-fade_samples:] *= ramp

            peak = max(np.max(np.abs(left)), np.max(np.abs(right)), 1e-12)
            left = (left / peak * 32767 * 0.7).astype(np.int16)
            right = (right / peak * 32767 * 0.7).astype(np.int16)

            stereo = np.empty((n_samples, 2), dtype=np.int16)
            stereo[:, 0] = left
            stereo[:, 1] = right

            seg = AudioSegment(stereo.tobytes(), frame_rate=sample_rate,
                               sample_width=2, channels=2)
            all_chunks.append(seg)

            # Release per-chunk numpy arrays immediately
            del t, fund_l, fund_r, harm_l, harm_r, octave_l, octave_r
            del swell, left, right, stereo, seg
            import gc
            gc.collect()

        # ── Export: write each chunk to a temp WAV, then ffmpeg-concat ──
        # Avoids sum(all_chunks) which doubles RAM (~150 MB → 300 MB) and
        # triggers the OOM killer on ffmpeg during pydub's export().
        # Instead, write per-chunk WAVs to disk and let ffmpeg concatenate
        # them in a single pass — memory stays under ~50 MB.
        import tempfile as _tempfile
        _wav_dir = Path(_tempfile.mkdtemp(prefix="ambient_wav_"))
        _wav_paths: list[Path] = []
        try:
            for ci, chunk in enumerate(all_chunks):
                _wav_path = _wav_dir / f"c{ci:03d}.wav"
                chunk.export(str(_wav_path), format="wav")
                _wav_paths.append(_wav_path)
                del chunk
            del all_chunks

            # Build ffmpeg concat list
            _list_path = _wav_dir / "concat.txt"
            _list_path.write_text(
                "\n".join(f"file '{p}'" for p in _wav_paths)
            )
            import subprocess as _sp
            _sp.run(
                ["ffmpeg", "-y", "-f", "concat", "-safe", "0",
                 "-i", str(_list_path), "-b:a", "128k", str(out_path)],
                check=True, capture_output=True, timeout=120,
            )
        finally:
            import shutil as _shutil
            _shutil.rmtree(_wav_dir, ignore_errors=True)
            import gc as _gc
            _gc.collect()

        self.logger.info(
            "Ambient music generated: %s (%.1fs, %d chunks, %.0f KB)",
            out_path.name, duration_sec, n_chunks, out_path.stat().st_size / 1024,
        )
        return out_path

    def _silent_audio(self, duration: float) -> AudioClip:
        """Create a silent AudioClip of *duration* seconds."""
        if MOVIEPY_V2:
            return AudioClip(
                lambda t: np.zeros((2,)),
                duration=duration,
                fps=44100,
            )
        else:
            frame_fn = lambda t: np.zeros((2,))
            return AudioClip(make_frame=frame_fn, duration=duration, fps=self.fps)

    def _tts_template_voice(self, text: str) -> Optional[Path]:
        """Synthesize a voice-over MP3 for a fixed template phrase.

        Uses the channel's configured TTS engine (edge-tts or Kokoro) via voice_resolver.
        Caches result in output/voice_cache/{slug}_{engine}_{voice}_{hash}.mp3.
        """
        text_key = text.strip()
        if not text_key:
            return None

        from config.voice_resolver import resolve_channel_voice, build_tts_engine

        resolved = resolve_channel_voice(self.canal)
        engine_type = resolved["engine"]
        voice_id = resolved["voice"]

        txt_hash = hashlib.sha256(text_key.encode()).hexdigest()[:12]
        cache_dir = Path("output/voice_cache")
        cache_dir.mkdir(parents=True, exist_ok=True)
        slug = self.canal.get("CHANNEL_SLUG", self.canal.get("slug", "unknown"))
        cache_path = cache_dir / f"{slug}_{engine_type}_{voice_id}_{txt_hash}.mp3"

        if cache_path.exists() and cache_path.stat().st_size > 0:
            return cache_path

        try:
            engine = build_tts_engine(self.canal)
            audio_path, _ = engine.generate(text_key)
            if audio_path and Path(audio_path).exists():
                import shutil
                shutil.move(str(audio_path), str(cache_path))
                self.logger.info("Template voice generated: %s (engine=%s, voice=%s)",
                                 cache_path, engine_type, voice_id)
                return cache_path
        except Exception as exc:
            self.logger.warning("Template voice synthesis failed for '%s...': %s",
                                text_key[:50], exc)

        return None

    def _get_voice_duration(self, audio_path: Path) -> float:
        """Get duration of an audio file in seconds using ffprobe."""
        try:
            result = subprocess.run([
                "ffprobe", "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                str(audio_path),
            ], capture_output=True, text=True, timeout=10)
            return float(result.stdout.strip())
        except Exception:
            return 0.0

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
        """Return the first available font from a preferred list.

        Searches both exact paths and glob patterns in /usr/share/fonts.
        Returns an absolute path when possible so PIL.ImageFont.truetype()
        and MoviePy TextClip can both use it reliably.
        """
        candidates = [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSerif.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSerif-Bold.ttf",
        ]
        for path in candidates:
            if os.path.exists(path):
                return path
        # Fallback: glob search for any sans-serif font
        for pattern in ["*DejaVu*Sans*", "*Liberation*Sans*", "*Noto*Sans*", "*Arial*", "*FreeSans*"]:
            matches = list(Path("/usr/share/fonts").rglob(pattern))
            if matches:
                return str(matches[0])
        return "DejaVuSans"

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
        """Return a static subtle dark radial-gradient vignette overlay.

        Returns RGBA (4-channel) arrays so MoviePy v2 composites the
        vignette correctly as a semi-transparent overlay on top of
        the image clips.

        Parameters are hardcoded for a uniform subtle cinematic effect
        across all channels — barely perceptible at first glance.
        """
        vw, vh = size
        cx, cy = vw / 2, vh / 2
        max_r = np.sqrt(cx ** 2 + cy ** 2)

        # Hardcoded subtle vignette — same for all channels
        radius_factor = 0.90           # bright center covers 90% of frame
        vignette_rgb = (8, 8, 8)       # barely-perceptible dark tint at edges

        yy, xx = np.mgrid[0:vh, 0:vw]
        dist = np.sqrt((xx - cx) ** 2 + (yy - cy) ** 2)
        # Gentle linear-ish falloff — only the extreme corners darken
        ratio = np.clip(dist / (max_r * radius_factor), 0.0, 1.0)
        alpha = ratio ** 1.2

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
                clip = VideoFileClip(str(template_path), target_resolution=self.video_size)
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
                method="label",
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
                    method="label",
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
                clip = VideoFileClip(str(template_path), target_resolution=self.video_size)
                clip_dur = self._dur(clip)
                if clip_dur > duration:
                    clip = clip.subclipped(0, duration)
                elif clip_dur < duration:
                    if duration - clip_dur <= 1.0:
                        # Small gap: freeze-frame extension is acceptable
                        clip = clip.with_duration(duration)
                        self.logger.info(
                            "Cached outro template extended: %.1fs → %.1fs", clip_dur, duration,
                        )
                    else:
                        # Large gap: fall back to programmatic to avoid black screen
                        # from the template's crossfade-to-black overlay on the last frame
                        self.logger.info(
                            "Cached outro template too short (%.1fs < %.1fs) — falling back to programmatic",
                            clip_dur, duration,
                        )
                        clip.close()
                        clip = None  # signal fallback to programmatic branch
                if clip is not None:
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
                    method="label",
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
                method="label",
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
                clip = VideoFileClip(str(template_path), target_resolution=self.video_size)
                template_dur = self._dur(clip)
                # If template covers the full duration, use it (trim if too long)
                if template_dur >= duration or not audio_path:
                    if template_dur > duration:
                        clip = clip.subclipped(0, duration)
                    elif template_dur < duration:
                        # No audio — freeze-frame extend is fine
                        clip = clip.with_duration(duration)
                        self.logger.info(
                            "Cached CTA template extended: %.1fs → %.1fs (no audio)",
                            template_dur, duration,
                        )
                    self.logger.info("Using cached CTA template: %s", template_path)
                    return clip
                else:
                    # Template shorter than audio-driven duration — use programmatic
                    # so visual matches audio length without ugly freeze-frames
                    self.logger.info(
                        "Cached CTA template too short (%.1fs < %.1fs) — falling back to programmatic",
                        template_dur, duration,
                    )
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

    # ── Segment-based rendering (RAM-bounded per scene) ──────────

    def _render_scene_segment(
        self, block_range: dict, asset: dict, seg_path: str,
        clip_idx: int, fallback_pool: list,
    ) -> Optional[str]:
        """Render a single scene to a standalone MP4 segment (video-only, no audio).

        Peak RAM = 1 ffmpeg decoder + 1 encoder ≈ 300-500 MB, constant regardless
        of total scene count. After rendering, the source clip is closed and
        memory freed immediately.

        Returns the segment path on success, or None on failure (caller retries
        or falls back to image Ken Burns).

        v4 (Jul 2026): Segment-based architecture to eliminate OOM risk from
        keeping all MoviePy VideoFileClip decoders open simultaneously during
        CompositeVideoClip write_videofile.
        """
        seg_path = Path(seg_path)
        seg_path.parent.mkdir(parents=True, exist_ok=True)

        # Collision avoidance
        if seg_path.exists():
            import uuid
            seg_path = seg_path.with_stem(f"{seg_path.stem}_{uuid.uuid4().hex[:4]}")

        clip = None
        seg_clip = None
        try:
            # Reuse existing clip-creation logic (dedup, offset tracking, Ken Burns, etc.)
            clip = self._create_block_clip(
                block_range, asset, clip_idx=clip_idx,
                fallback_pool=fallback_pool,
            )
            if clip is None:
                return None  # caller will use fallback image

            # Set timeline position
            seg_clip = clip.with_start(0.0)  # segment starts at 0 internally

            # Apply color grade if set (same logic as original)
            if self._video_color_grade:
                try:
                    cg = self._video_color_grade
                    if MOVIEPY_V2:
                        seg_clip = seg_clip.with_effects([
                            vfx.ColorCorrection(
                                contrast=cg["contrast"],
                                brightness=cg["brightness"],
                                saturation=cg["saturation"],
                            )
                        ])
                except Exception:
                    pass

            # Render this single clip to a video-only MP4
            # Use CRF for visually lossless intermediate (prevents degradation
            # during the subsequent xfade re-encode).
            seg_clip.write_videofile(
                str(seg_path),
                fps=self.fps,
                codec=VIDEO_CODEC,
                preset=self.canal.get("FFMPEG_PRESET", FFMPEG_PRESET_DEFAULT),
                bitrate=None,  # CRF mode
                ffmpeg_params=[
                    "-crf", "16",
                    "-pix_fmt", "yuv420p",
                    "-an",  # no audio in segment
                    "-threads", "2",
                ],
                logger=None,
            )

            self.logger.debug("  Seg %d rendered: %s", clip_idx, seg_path.name)
            return str(seg_path)

        except Exception as exc:
            self.logger.warning(
                "  Seg %d FAILED: %s (%s)", clip_idx, seg_path.name, exc,
            )
            # Kill orphaned ffmpeg left by the failed render
            _kill_orphaned_ffmpeg_videoeditor()
            # Delete partial file
            try:
                if seg_path.exists():
                    seg_path.unlink(missing_ok=True)
            except Exception:
                pass
            return None

        finally:
            # IMMEDIATE release: close the source clip and free its ffmpeg decoder
            if seg_clip is not None:
                try:
                    seg_clip.close()
                except Exception:
                    pass
            if clip is not None:
                try:
                    clip.close()
                except Exception:
                    pass

    def _render_black_segment(
        self, block_range: dict, seg_path: str | Path,
    ) -> str:
        """Render a black placeholder segment for scenes with no media at all.

        Last-resort fallback — uses PIL to generate a black frame and MoviePy
        to render it as a video segment matching the block's duration.
        """
        seg_path = Path(seg_path)
        seg_path.parent.mkdir(parents=True, exist_ok=True)
        if seg_path.exists():
            import uuid
            seg_path = seg_path.with_stem(f"{seg_path.stem}_{uuid.uuid4().hex[:4]}")

        dur = block_range.get("duration", 5.0)
        try:
            clip = self._placeholder_clip(dur)
            clip.write_videofile(
                str(seg_path), fps=self.fps, codec=VIDEO_CODEC,
                preset=self.canal.get("FFMPEG_PRESET", FFMPEG_PRESET_DEFAULT),
                bitrate=VIDEO_BITRATE,
                ffmpeg_params=["-pix_fmt", "yuv420p", "-an"],
                logger=None,
            )
            clip.close()
            return str(seg_path)
        except Exception as exc:
            self.logger.warning("Black placeholder render failed: %s", exc)
            return ""

    def _concat_body_with_crossfades(
        self, segment_paths: list[str], block_ranges: list[dict],
        output_path: str,
    ) -> str:
        """Concatenate pre-rendered scene segments with xfade transitions.

        Uses ffmpeg's xfade filter to chain segments with the same crossfade
        duration and overlap formula as the original CompositeVideoClip path.
        Grain and vignette are folded into the same filter_complex as overlays.

        Returns the output path of the concatenated body video (video-only, no audio).
        """
        if not segment_paths:
            raise RuntimeError("No segments to concatenate")

        crossfade_duration = random.uniform(
            self.canal.get("CROSSFADE_MIN", 0.3),
            self.canal.get("CROSSFADE_MAX", 0.7),
        )

        # Compute total expected duration (same formula as _composite_scenes)
        total_scenes_dur = sum(br["duration"] for br in block_ranges)

        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        if output_path.exists():
            import uuid
            output_path = output_path.with_stem(f"{output_path.stem}_{uuid.uuid4().hex[:6]}")

        # Build ffmpeg filter_complex for xfade chain
        # Each segment is an input, chained with xfade
        n = len(segment_paths)
        ff_args = ["ffmpeg", "-y", "-v", "error"]

        for sp in segment_paths:
            ff_args += ["-i", str(sp)]

        filter_parts: list[str] = []
        stream_labels: list[str] = []

        # Build xfade chain: xfade=transition=fade:duration=X:offset=Y
        # First segment passes through. Then each new segment gets xfade with the running result.
        current_label = "[0:v]"
        cumulative_offset = 0.0
        stream_labels.append(current_label)

        for i in range(1, n):
            seg_dur = block_ranges[i-1]["duration"] if i-1 < len(block_ranges) else 5.0
            offset = cumulative_offset + seg_dur - crossfade_duration
            offset = max(0.0, offset)

            next_stream = f"[{i}:v]"
            output_label = f"[xfade{i}]"
            filter_parts.append(
                f"{current_label}{next_stream}xfade=transition=fade:duration={crossfade_duration:.3f}:offset={offset:.3f}{output_label}"
            )
            current_label = output_label
            stream_labels.append(current_label)
            cumulative_offset = offset

        # After xfade chain, compute padding to match total narration length
        composite_end = cumulative_offset + block_ranges[-1]["duration"] if block_ranges else 0
        if composite_end < total_scenes_dur:
            pad_dur = total_scenes_dur - composite_end
            pad_label = f"[padded]"
            filter_parts.append(
                f"{current_label}tpad=stop_mode=clone:stop_duration={pad_dur:.3f}{pad_label}"
            )
            current_label = pad_label

        # Grain + vignette overlays (if enabled)
        film_grain_opacity = self.canal.get("FILM_GRAIN_OPACITY", 0)
        vignette_intensity = self.canal.get("VIGNETTE_INTENSITY", 0)
        has_overlays = film_grain_opacity > 0 or vignette_intensity > 0

        if has_overlays:
            # Generate grain + vignette as temporary image clips, then overlay
            # For now: apply vignette via ffmpeg vignette filter (simpler, no temp files)
            overlay_parts = []
            if vignette_intensity > 0:
                vig_val = vignette_intensity / 100.0
                overlay_parts.append(f"vignette=PI/4:aspect=16/9")
            if film_grain_opacity > 0:
                # ffmpeg noise + blend
                grain_val = film_grain_opacity / 100.0
                overlay_parts.append(f"noise=alls={grain_val*20:.0f}:allf=t+u")
            if overlay_parts:
                vig_label = f"[final]"
                filter_parts.append(f"{current_label}{','.join(overlay_parts)}{vig_label}")
                current_label = vig_label

        filter_complex = ";".join(filter_parts)

        ff_args += [
            "-filter_complex", filter_complex,
            "-map", current_label,
            "-c:v", VIDEO_CODEC,
            "-preset", self.canal.get("FFMPEG_PRESET", FFMPEG_PRESET_DEFAULT),
            "-b:v", VIDEO_BITRATE,
            "-pix_fmt", "yuv420p",
            "-an",  # no audio in body segment
            "-movflags", "+faststart",
            str(output_path),
        ]

        self.logger.info(
            "Concat %d segments + xfade(%.1fs) + overlays → %s",
            n, crossfade_duration, output_path.name,
        )

        try:
            # v6: timeout scales with segment count (each xfade is expensive)
            scaled_timeout = max(900, n * 15)
            self.logger.info(
                "Concat %d segments + xfade(%.1fs) + overlays → %s (timeout=%ds)",
                n, crossfade_duration, output_path.name, scaled_timeout,
            )
            result = subprocess.run(ff_args, capture_output=True, text=True, timeout=scaled_timeout)
            if result.returncode != 0:
                stderr_tail = result.stderr.strip()[-500:] if result.stderr else ""
                raise RuntimeError(
                    f"ffmpeg xfade concat failed (rc={result.returncode}): {stderr_tail}"
                )
            if not output_path.exists() or output_path.stat().st_size == 0:
                raise RuntimeError("ffmpeg xfade concat produced empty output")
        except subprocess.TimeoutExpired:
            raise RuntimeError(
                f"ffmpeg xfade concat timed out after {scaled_timeout}s ({n} segments)"
            )

        return str(output_path)


def _merge_consecutive_placeholders(
    segment_paths: list[str],
    block_ranges: list[dict],
    seg_dir: Path,
    logger,
) -> tuple[list[str], list[dict]]:
    """Merge consecutive black placeholder segments into fewer, longer clips.

    When many consecutive scenes fail to get media assets (e.g. due to
    off-niche theme poisoning or provider circuit breakers), the concat
    gets overloaded with black placeholder segments that add no visual
    value but consume ffmpeg xfade processing time per segment.

    This function identifies runs of consecutive ``_black.mp4`` segments,
    concatenates them into a single longer placeholder using ffmpeg
    stream concat (fast, no re-encode), and merges their block ranges.

    Returns:
        (merged_paths, merged_ranges) — reduced lists with placeholder
        runs collapsed into single entries.
    """
    if not segment_paths:
        return segment_paths, block_ranges

    merged_paths: list[str] = []
    merged_ranges: list[dict] = []
    run_indices: list[int] = []

    for i, sp in enumerate(segment_paths):
        is_black = "_black" in Path(sp).stem

        if is_black:
            run_indices.append(i)
        else:
            # Flush any pending black run
            if run_indices:
                _flush_black_run(
                    run_indices, segment_paths, block_ranges,
                    seg_dir, merged_paths, merged_ranges, logger,
                )
                run_indices = []
            merged_paths.append(sp)
            merged_ranges.append(block_ranges[i])

    # Flush trailing black run
    if run_indices:
        _flush_black_run(
            run_indices, segment_paths, block_ranges,
            seg_dir, merged_paths, merged_ranges, logger,
        )

    if len(merged_paths) < len(segment_paths):
        logger.info(
            "Merged %d consecutive placeholder runs → reduced %d segments to %d",
            len(segment_paths) - len(merged_paths),
            len(segment_paths), len(merged_paths),
        )

    return merged_paths, merged_ranges


def _flush_black_run(
    indices: list[int],
    segment_paths: list[str],
    block_ranges: list[dict],
    seg_dir: Path,
    merged_paths: list[str],
    merged_ranges: list[dict],
    logger,
) -> None:
    """Concatenate a run of consecutive black placeholders into one segment."""
    if len(indices) == 1:
        # Single black segment — no merge needed
        merged_paths.append(segment_paths[indices[0]])
        merged_ranges.append(block_ranges[indices[0]])
        return

    first, last = indices[0], indices[-1]

    # Build merged block range with combined duration
    merged_range = dict(block_ranges[first])
    merged_range["duration"] = sum(
        block_ranges[j]["duration"] for j in indices
    )

    # Use ffmpeg concat demuxer (fast, no re-encode) to merge black clips
    merged_path = seg_dir / f"scene_{first:04d}_merged_black_{len(indices)}.mp4"
    if not merged_path.exists():
        concat_list = seg_dir / f"_blist_{first}.txt"
        with open(concat_list, "w") as f:
            for j in indices:
                f.write(f"file '{Path(segment_paths[j]).resolve()}'\n")
        try:
            subprocess.run(
                [
                    "ffmpeg", "-y", "-v", "error",
                    "-f", "concat", "-safe", "0",
                    "-i", str(concat_list),
                    "-c", "copy",
                    str(merged_path),
                ],
                capture_output=True, timeout=60,
                check=True,
            )
            logger.debug(
                "Merged %d black placeholders (indices %d..%d) → %s",
                len(indices), first, last, merged_path.name,
            )
        except Exception as exc:
            logger.warning(
                "Could not merge %d black placeholders: %s — using first one",
                len(indices), exc,
            )
            merged_paths.append(segment_paths[first])
            merged_ranges.append(block_ranges[first])
            return
        finally:
            concat_list.unlink(missing_ok=True)

    merged_paths.append(str(merged_path.resolve()))
    merged_ranges.append(merged_range)


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
