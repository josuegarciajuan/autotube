"""Pipeline orchestrator for Autotube.

Coordinates the entire video generation pipeline:
content scraping → script generation → TTS → media fetch → video assembly,
thumbnail generation, metadata creation and YouTube upload.
"""

import json
import logging
import random
import sys
import time
import threading
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

from config.config_bridge import get_channel_config
from config.settings import (
    ACTIVE_CHANNELS,
    WEEK1_VIDEOS_PER_DAY,
    WEEK2_VIDEOS_PER_DAY,
    WEEK3_VIDEOS_PER_DAY,
    PIPELINE_START_DATE,
    OUTPUT_DIR,
    LOGS_DIR,
    LOG_LEVEL,
    LOG_FORMAT,
)
from database.db import init_db
from database.db_extended import ExtendedDatabase

logger = logging.getLogger(__name__)

# Channel configurations — populated via config bridge
CHANNEL_CONFIGS: dict[str, object] = {}


class PipelineOrchestrator:
    """Master orchestrator for the Autotube content pipeline."""

    def __init__(self, canal: str = "canal1", db_path: str = None, db_video_id: Optional[int] = None,
                 progress_callback: Optional[callable] = None):
        self.canal = canal
        self.db_video_id = db_video_id  # Si != None, modo API: update en vez de insert
        self._progress_cb = progress_callback  # (percent, phase, message) → None

        # Phase timing tracking
        self._timing: dict = {"phases": {}}
        self._pipeline_start: float = time.time()

        # Load config via bridge (DB-aware) or fall back to Python module
        if canal not in CHANNEL_CONFIGS:
            try:
                CHANNEL_CONFIGS[canal] = get_channel_config(canal)
            except ImportError:
                raise ValueError(f"Unknown channel: {canal}. No config module found.")
        self.config = CHANNEL_CONFIGS[canal]

        # Initialize database
        self.db = ExtendedDatabase(db_path)
        init_db(db_path)

        # Lazy-loaded components
        self._scraper = None
        self._script_gen = None
        self._tts = None
        self._media_fetcher = None
        self._image_fetcher = None
        self._image_processor = None
        self._video_editor = None
        self._thumbnail_maker = None
        self._metadata_gen = None
        self._uploader = None

        # Inter-phase state (computed once, used in subsequent phases)
        self._last_scene_ranges: list[dict] | None = None

    @property
    def scraper(self):
        if self._scraper is None:
            from scrapers.reddit import RedditScraper
            from scrapers.wikipedia import WikipediaScraper
            self._scraper = {
                "reddit": RedditScraper(config=self.config),
                "wikipedia": WikipediaScraper(config=self.config),
            }
        return self._scraper

    @property
    def script_gen(self):
        if self._script_gen is None:
            from pipeline.script_generator import ScriptGenerator
            self._script_gen = ScriptGenerator(self.db, self.config)
        return self._script_gen

    @property
    def tts(self):
        if self._tts is None:
            from config.voice_resolver import build_tts_engine
            self._tts = build_tts_engine(self.config)
        return self._tts

    @property
    def media_fetcher(self):
        if self._media_fetcher is None:
            from pipeline.media_fetcher import MediaFetcher
            self._media_fetcher = MediaFetcher(config=self.config)
        return self._media_fetcher

    @property
    def image_fetcher(self):
        """Legacy image fetcher — kept for backward compatibility."""
        if self._image_fetcher is None:
            from pipeline.image_fetcher import ImageFetcher
            self._image_fetcher = ImageFetcher(config=self.config)
        return self._image_fetcher

    @property
    def image_processor(self):
        if self._image_processor is None:
            from pipeline.image_processor import ImageProcessor
            self._image_processor = ImageProcessor(self.config)
        return self._image_processor

    @property
    def video_editor(self):
        if self._video_editor is None:
            from pipeline.video_editor import VideoEditor
            self._video_editor = VideoEditor(self.config)
        return self._video_editor

    @property
    def thumbnail_maker(self):
        if self._thumbnail_maker is None:
            from pipeline.thumbnail_maker import ThumbnailMaker
            self._thumbnail_maker = ThumbnailMaker(self.config)
        return self._thumbnail_maker

    @property
    def metadata_gen(self):
        if self._metadata_gen is None:
            from pipeline.metadata_generator import MetadataGenerator
            self._metadata_gen = MetadataGenerator(self.config)
        return self._metadata_gen

    @property
    def uploader(self):
        if self._uploader is None:
            from pipeline.youtube_uploader import YouTubeUploader
            self._uploader = YouTubeUploader(
                account_name=self.canal,
                db=self.db,
                channel_slug=self.canal,
            )
        return self._uploader

    def cleanup(self):
        """Release heavy resources (Kokoro pipeline) to free RAM between phases.

        Called by generation_service after the video subprocess is spawned
        and at job completion.  Releasing the TTS engine (~500 MB PyTorch model)
        prevents uvicorn from accumulating RAM during the hour-long render.
        """
        import gc
        if self._tts is not None:
            if hasattr(self._tts, 'unload'):
                try:
                    self._tts.unload()
                    logger.info("[%s] Kokoro pipeline unloaded via cleanup()", self.canal)
                except Exception as _exc:
                    logger.debug("[%s] Kokoro unload in cleanup: %s", self.canal, _exc)
            self._tts = None
        gc.collect()
        try:
            import ctypes
            ctypes.CDLL("libc.so.6").malloc_trim(0)
        except Exception:
            pass
        logger.info("[%s] Orchestrator cleanup complete", self.canal)

    def _emit_progress(self, percent: int, phase: str, message: str) -> None:
        """Fire the progress callback (if set). No-op when running CLI standalone."""
        if self._progress_cb:
            try:
                self._progress_cb(percent, phase, message)
            except Exception:
                pass  # never let progress emission crash the pipeline

    def collect_timing(self) -> dict:
        """Return accumulated phase timings + wall-clock duration.

        Returns:
            dict with keys ``phases`` (phase_name → duration_ms) and
            ``total_duration_ms`` (wall-clock since orchestrator was created).
        """
        if self._pipeline_start is not None:
            total = int((time.time() - self._pipeline_start) * 1000)
        else:
            total = sum(self._timing.get("phases", {}).values())
        return {
            "phases": dict(self._timing.get("phases", {})),
            "total_duration_ms": total,
        }

    def collect_timing_json(self) -> str:
        """collect_timing() as a JSON string — for DB persistence."""
        import json
        return json.dumps(self.collect_timing())

    # ── Phase runners ──────────────────────────────────────────

    def phase_scrape(self) -> int:
        """Scrape new content from all sources. Returns items added."""
        import concurrent.futures
        start = time.time()
        added = 0
        self._emit_progress(5, "scrape", "Buscando historias en Reddit y Wikipedia...")

        # Reddit scraping with per-source timeout (5 min per scraper)
        for scraper_name, s in self.scraper.items():
            try:
                self._emit_progress(7, "scrape", f"Scraping {scraper_name}...")
                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                    future = executor.submit(s.save_to_db, self.db)
                    count = future.result(timeout=300)  # 5 min hard timeout per scraper
                added += count
                logger.info(f"[{self.canal}] {scraper_name}: {count} items scraped")
            except concurrent.futures.TimeoutError:
                logger.error(f"[{self.canal}] {scraper_name} scrape timed out after 5min")
                self.db.log_pipeline(self.canal, "scrape", "error", "timeout after 5min")
            except Exception as e:
                logger.error(f"[{self.canal}] {scraper_name} scrape failed: {e}")
                self.db.log_pipeline(self.canal, "scrape", "error", str(e))

        self._emit_progress(10, "scrape", f"Contenido obtenido: {added} fuentes")
        duration_ms = int((time.time() - start) * 1000)
        self._timing["phases"]["scrape"] = duration_ms
        self.db.log_pipeline(self.canal, "scrape", "success",
                             f"Added {added} items", duration_ms=duration_ms)
        return added

    def phase_generate_script(self) -> Optional[dict]:
        """Generate ONE script from unused content. Returns script dict or None."""
        start = time.time()

        content_items = self.db.get_unused_content(canal=self.canal, limit=5)
        if not content_items:
            logger.warning(f"[{self.canal}] No unused content available for script generation")
            self.db.log_pipeline(self.canal, "script", "skipped",
                                 "No unused content")
            return None

        self._emit_progress(15, "script", "Eligiendo mejor contenido y generando guion con IA...")
        result = self.script_gen.generate(content_items[0])
        duration_ms = int((time.time() - start) * 1000)

        self._timing["phases"]["script"] = duration_ms
        if result:
            words = len(result.get("guion", "").split()) if result.get("guion") else 0
            self._emit_progress(23, "script", f"Guion generado: {words} palabras, {result.get('id')}")
            self.db.log_pipeline(self.canal, "script", "success",
                                 f"Script {result.get('id')} generated",
                                 content_id=result.get("id"),
                                 duration_ms=duration_ms)
        else:
            self.db.log_pipeline(self.canal, "script", "error",
                                 "Script generation failed",
                                 duration_ms=duration_ms)
        return result

    def phase_tts(self, script: dict, job_id: int = None) -> Optional[dict]:
        """Generate TTS audio for a script using the channel's configured engine.

        Each narrative block gets its own voice settings (rate/pitch/block_speed)
        based on block type (hook, desarrollo, climax, reflexion, cierre).

        Emits heartbeats during synthesis to prevent orphan detection timeout.
        """
        start = time.time()
        self._emit_progress(30, "tts", "Generando narracion con IA (TTS)...")

        try:
            import json

            # Get bloques from script JSON
            bloques_raw = script.get("bloques") or script.get("bloques_json")
            if isinstance(bloques_raw, str):
                bloques = json.loads(bloques_raw)
            else:
                bloques = bloques_raw or []

            if bloques:
                # Test mode: limit number of blocks for faster iteration
                max_blocks = getattr(self.config, "MAX_SCRIPT_BLOCKS", 0)
                if max_blocks > 0 and len(bloques) > max_blocks:
                    logger.info("[%s] Test mode: truncating %d blocks → %d (MAX_SCRIPT_BLOCKS=%d)",
                                self.canal, len(bloques), max_blocks, max_blocks)
                    bloques = bloques[:max_blocks]

                # v2: segmented synthesis with per-block voice
                logger.info("[%s] Using segmented TTS with %d blocks", self.canal, len(bloques))

                # ── Heartbeat emitter (prevents orphan timeout during long synthesis) ──
                _hb_stop = threading.Event()
                _hb_thread = None
                if job_id is not None:
                    def _hb_loop_tts():
                        while not _hb_stop.is_set():
                            try:
                                self.db.update_heartbeat(job_id)
                            except Exception:
                                pass
                            _hb_stop.wait(30)
                    _hb_thread = threading.Thread(target=_hb_loop_tts, daemon=True, name=f"tts-heartbeat-{job_id}")
                    _hb_thread.start()
                    logger.info("[%s] TTS heartbeat emitter started for job #%d (every 30s)", self.canal, job_id)

                audio_path, timestamps = self.tts.generate_segmented(bloques)

                # Stop heartbeat
                if _hb_stop is not None:
                    _hb_stop.set()
                if _hb_thread is not None and _hb_thread.is_alive():
                    _hb_thread.join(timeout=2)

                # ── Release Kokoro pipeline memory after TTS ──
                if hasattr(self.tts, 'unload'):
                    try:
                        self.tts.unload()
                        logger.info("[%s] Kokoro pipeline unloaded after TTS", self.canal)
                    except Exception as _ue:
                        logger.debug("[%s] Kokoro unload: %s", self.canal, _ue)
            else:
                # Legacy fallback: single-segment synthesis from guion text
                logger.info("[%s] No bloques found — using legacy single-segment TTS", self.canal)
                guion = script.get("guion", "")
                audio_path, timestamps = self.tts.generate(guion)

            audio_dur = int(timestamps[-1]["end_ms"]/1000) if timestamps and "end_ms" in timestamps[-1] else len(timestamps) if timestamps else 0
            self._emit_progress(38, "tts", f"Audio generado: {audio_dur}s de narracion")

            # ── Generate CTA audio (separate from body narration) ──
            cta_audio_path = None
            try:
                # Priority 1: LLM-generated cta field from script metadata
                cta_obj = script.get("cta", {})
                if isinstance(cta_obj, dict) and cta_obj.get("texto", "").strip():
                    cta_text = cta_obj["texto"].strip()
                else:
                    cta_text = None

                # Priority 2: Channel config SCRIPT_END_HOOK (channel-specific teaser)
                if not cta_text:
                    cta_text = getattr(self.config, "SCRIPT_END_HOOK", None)
                    # Clean up any remaining placeholders like {next_story}
                    if cta_text:
                        import re as _re
                        cta_text = _re.sub(r'\{[^}]+\}', 'la proxima historia', cta_text)

                if cta_text:
                    self._emit_progress(39, "tts", "Generando audio de cierre (CTA)...")
                    cta_path, _ = self.tts.generate(cta_text)
                    cta_audio_path = cta_path
                    logger.info("[%s] CTA audio generated: %s", self.canal, cta_path)
                else:
                    logger.info("[%s] No CTA text found — CTA segment will be silent", self.canal)
            except Exception as exc:
                logger.warning("[%s] CTA audio generation failed (non-fatal): %s", self.canal, exc)

            # ── Release Kokoro pipeline after CTA ──────────────
            # The TTS engine stays loaded after CTA generation but is no longer
            # needed during the render (video subprocess).  Release it now to
            # prevent PyTorch model (~500 MB) from consuming RAM for the entire
            # render duration (1h+).
            if hasattr(self.tts, 'unload'):
                try:
                    self.tts.unload()
                    logger.info("[%s] Kokoro pipeline unloaded after CTA", self.canal)
                except Exception as _ue:
                    logger.debug("[%s] Kokoro unload after CTA: %s", self.canal, _ue)

            result = {
                "audio_path": audio_path,
                "timestamps_path": str(Path(audio_path).with_name(f"{Path(audio_path).stem}_timestamps.json")),
                "timestamps": timestamps,
                "cta_audio_path": cta_audio_path,
            }

            duration_ms = int((time.time() - start) * 1000)
            self._timing["phases"]["tts"] = duration_ms
            self.db.log_pipeline(self.canal, "tts", "success",
                                  f"Audio: {audio_path}",
                                  content_id=script.get("id"),
                                  duration_ms=duration_ms)
            return result

        except Exception as e:
            logger.error(f"[{self.canal}] TTS failed: {e}")
            self.db.log_pipeline(self.canal, "tts", "error", str(e))
            return None

    def phase_media(self, script: dict, audio_data: Optional[dict] = None) -> Optional[list[dict]]:
        """Fetch media (video/image) for each enforceable scene range.

        When ``audio_data`` is provided (with TTS timestamps), scene ranges are
        computed BEFORE fetching so each sub-scene gets its own distinct asset.
        Without audio_data, falls back to per-block fetch.
        """
        start = time.time()
        self._emit_progress(42, "images", "Buscando imagenes y videos para el video...")

        try:
            import json

            # Get bloques from script
            bloques_raw = script.get("bloques") or script.get("bloques_json")
            if isinstance(bloques_raw, str):
                bloques = json.loads(bloques_raw)
            else:
                bloques = bloques_raw or []

            if not bloques:
                logger.warning("[%s] No bloques in script — falling back to legacy image fetcher", self.canal)
                return self._phase_images_legacy(script)

            # Compute scene_ranges from TTS timestamps if available (so subscenes
            # each get their own media instead of sharing the parent block's image)
            scene_ranges = None
            timestamps = audio_data.get("timestamps") if audio_data else None
            if timestamps and bloques:
                try:
                    scene_ranges = self.video_editor._compute_block_ranges(bloques, timestamps)
                    logger.info("[%s] Computed %d scene ranges from TTS timestamps "
                                "(each subscene gets its own media)", self.canal, len(scene_ranges))
                except Exception as e:
                    logger.warning("[%s] Could not compute scene ranges: %s", self.canal, e)

            # ── Save scene_ranges for phase_video (ensures 1:1 alignment) ──
            self._last_scene_ranges = scene_ranges

            logger.info("[%s] Fetching media for %d scenes", self.canal,
                        len(scene_ranges) if scene_ranges else len(bloques))

            # Fetch one asset per scene (scene_ranges or bloques as fallback)
            media_assets = self.media_fetcher.fetch_for_script(
                bloques=bloques,
                scene_ranges=scene_ranges,
            )

            # Post-process images only (videos are used as-is)
            skip_processing = getattr(self.config, "IMAGE_PROCESSING_DISABLED", False)
            if skip_processing:
                logger.info("[%s] Image processing disabled (test mode) — using raw images", self.canal)
            for asset in media_assets:
                if asset["type"] == "image" and asset["path"] and not skip_processing:
                    try:
                        asset["path"] = self.image_processor.process(asset["path"])
                    except Exception as exc:
                        logger.warning("[%s] Image processing failed for %s: %s",
                                       self.canal, asset["path"], exc)

            # Count stats
            n_video = sum(1 for a in media_assets if a["type"] == "video")
            n_image = sum(1 for a in media_assets if a["type"] == "image")
            n_placeholder = sum(1 for a in media_assets if a["type"] == "placeholder")
            self._emit_progress(53, "images", f"Imagenes listas: {n_video} videos + {n_image} imagenes")

            duration_ms = int((time.time() - start) * 1000)
            self._timing["phases"]["media"] = duration_ms
            self.db.log_pipeline(
                self.canal, "media", "success",
                f"Fetched {len(media_assets)} assets ({n_video} video, {n_image} image, {n_placeholder} placeholder)",
                content_id=script.get("id"),
                duration_ms=duration_ms,
            )
            return media_assets

        except Exception as e:
            logger.error(f"[{self.canal}] Media fetch failed: {e}")
            self.db.log_pipeline(self.canal, "media", "error", str(e))
            return None

    def _phase_images_legacy(self, script: dict) -> Optional[list]:
        """Legacy image fetching — kept for scripts without bloques field."""
        return self.phase_images(script)

    def phase_images(self, script: dict) -> Optional[list]:
        """Legacy image fetching using old ImageFetcher (kept for backward compat)."""
        start = time.time()

        try:
            import json
            escenas_raw = script.get("escenas") or script.get("escenas_json")
            if isinstance(escenas_raw, str):
                escenas = json.loads(escenas_raw)
            else:
                escenas = escenas_raw or []

            image_paths = self.image_fetcher.fetch_for_script(escenas)

            # Process each image
            processed = []
            for scene_images in image_paths:
                scene_processed = []
                for img_path in scene_images:
                    processed_path = self.image_processor.process(img_path)
                    scene_processed.append(processed_path)
                processed.append(scene_processed)

            duration_ms = int((time.time() - start) * 1000)
            self._timing["phases"]["images"] = duration_ms
            total_imgs = sum(len(s) for s in processed)
            self.db.log_pipeline(self.canal, "images", "success",
                                  f"Fetched & processed {total_imgs} images (legacy)",
                                  content_id=script.get("id"),
                                  duration_ms=duration_ms)
            return processed

        except Exception as e:
            logger.error(f"[{self.canal}] Image pipeline (legacy) failed: {e}")
            self.db.log_pipeline(self.canal, "images", "error", str(e))
            return None

    def phase_video(self, script: dict, audio_data: dict,
                     media_assets: list, job_id: int = None) -> Optional[dict]:
        """Assemble the final video from blocks + media assets.

        Uses v2 block-based API when bloques are available in the script,
        falling back to legacy scene-based assembly.
        """
        start = time.time()

        try:
            import json
            import shutil

            try:
                _disk = shutil.disk_usage(Path("output"))
                _free_gb = _disk.free / (1024**3)
                logger.info("[%s] Disk free before render: %.1f GB", self.canal, _free_gb)
                if _free_gb < 1.0:
                    logger.warning(
                        "[%s] ⚠️  Only %.1f GB free — render may fail due to disk space!",
                        self.canal, _free_gb,
                    )
            except Exception:
                pass

            # Get bloques from script
            bloques_raw = script.get("bloques") or script.get("bloques_json")
            if isinstance(bloques_raw, str):
                bloques = json.loads(bloques_raw)
            else:
                bloques = bloques_raw or []

            # Select a title
            titulo_raw = script.get("titulo_options", "[]")
            if isinstance(titulo_raw, str):
                titulo_options = json.loads(titulo_raw)
            else:
                titulo_options = titulo_raw or ["Historia Impactante"]
            titulo_selected = titulo_options[0] if titulo_options else "Historia Impactante"

            if bloques and media_assets:
                # ── v2: block-based assembly ─────────────────
                self._emit_progress(60, "video", f"Renderizando video con {len(bloques)} bloques (MoviePy)...")
                logger.info("[%s] Building video with v2 block API: %d blocks, %d assets",
                            self.canal, len(bloques), len(media_assets))
                video_path = self.video_editor.build_video(
                    bloques=bloques,
                    media_assets=media_assets,
                    audio_path=audio_data["audio_path"],
                    timestamps=audio_data["timestamps"],
                    scene_ranges=getattr(self, "_last_scene_ranges", None),
                    job_id=job_id,
                    cta_audio_path=audio_data.get("cta_audio_path"),
                )
            else:
                # ── Legacy fallback: scene-based assembly ────
                logger.info("[%s] No bloques — using legacy scene-based video assembly", self.canal)
                scenes = self.tts.parse_scenes(script.get("guion", ""))
                if not scenes:
                    logger.error(f"[{self.canal}] No scenes parsed from script")
                    return None

                if not isinstance(media_assets, list) or not media_assets:
                    logger.error(f"[{self.canal}] No media assets for legacy assembly")
                    return None

                # Convert flat media_assets to per-scene format for legacy
                # (approximate: one asset per scene)
                legacy_image_paths = []
                for asset in media_assets:
                    if asset["type"] in ("image", "video") and asset["path"]:
                        legacy_image_paths.append([asset["path"]])
                    else:
                        legacy_image_paths.append([])

                video_path = self.video_editor.build_video(
                    scenes=scenes,
                    image_paths=legacy_image_paths,
                    audio_path=audio_data["audio_path"],
                    timestamps=audio_data["timestamps"],
                    job_id=job_id,
                )

            # Generate thumbnail (non-fatal: video is still valid without it)
            thumbnail_path = None
            try:
                self._emit_progress(65, "video", "Generando miniatura viral (Pollo AI)...")
                # Collect scene images for thumbnail from media_assets (images only, not videos)
                scene_images_for_thumb = []
                if isinstance(media_assets, list):
                    for asset in media_assets:
                        if asset["type"] == "image" and asset["path"]:
                            scene_images_for_thumb.append([asset["path"]])

                keywords_raw = script.get("keywords_json") if isinstance(script.get("keywords_json"), str) else (script.get("keywords") or script.get("keywords_json", []))
                if isinstance(keywords_raw, str):
                    keywords = json.loads(keywords_raw)
                else:
                    keywords = keywords_raw or []

                thumbnail_path = self.thumbnail_maker.make_viral_thumbnail(
                    title=titulo_selected,
                    overlay_text="",
                    keywords=keywords,
                    scene_images=scene_images_for_thumb or [],
                    script_text=script.get("guion", "")[:1500],
                    canal_slug=self.canal,
                    channel_display_name=getattr(self.config, "CANAL_DISPLAY_NAME", ""),
                    channel_description=getattr(self.config, "CHANNEL_ABOUT_SECTION", ""),
                    channel_theme=getattr(self.config, "CANAL_TAGLINE", ""),
                )
            except Exception as thumb_exc:
                logger.warning("[%s] Thumbnail generation failed (non-fatal): %s", self.canal, thumb_exc)
                thumbnail_path = ""

            duration_ms = int((time.time() - start) * 1000)
            self._timing["phases"]["video_assembly"] = duration_ms
            self.db.log_pipeline(self.canal, "video", "success",
                                  f"Video: {video_path}",
                                  content_id=script.get("id"),
                                  duration_ms=duration_ms)

            # ── Save video to database ──
            channel_id = self._get_channel_id()
            timestamps = audio_data.get("timestamps", [])
            duracion_seg = int(timestamps[-1]["end_ms"] / 1000) if timestamps and "end_ms" in timestamps[-1] else (
                int(timestamps[-1]["end"]) if timestamps else 0)

            if self.db_video_id is not None:
                # API mode: update the pre-created record (single-source-of-truth)
                self.db.update_video(
                    self.db_video_id,
                    script_id=script.get("id"),
                    video_path=str(video_path),
                    thumbnail_path=str(thumbnail_path),
                    audio_path=audio_data["audio_path"],
                    titulo_final=titulo_selected,
                    duracion_seg=duracion_seg,
                    channel_id=channel_id,
                )
                video_id = self.db_video_id
            else:
                # CLI standalone mode: insert a new record
                video_id = self.db.insert_video(
                    script_id=script.get("id"),
                    canal=self.canal,
                    video_path=str(video_path),
                    thumbnail_path=str(thumbnail_path),
                    audio_path=audio_data["audio_path"],
                    titulo_final=titulo_selected,
                    duracion_seg=duracion_seg,
                    channel_id=channel_id,
                )

            return {
                "video_path": str(video_path),
                "thumbnail_path": str(thumbnail_path),
                "thumbnail_base_path": str(
                    # Use raw Pollo image (before text/gradient) so phase_metadata
                    # recomposes from scratch instead of double-stacking effects.
                    getattr(self.thumbnail_maker, '_last_raw_base', None) or str(thumbnail_path)
                ),
                "titulo": titulo_selected,
                "video_id": video_id,
            }

        except Exception as e:
            logger.error(f"[{self.canal}] Video assembly failed: {e}")
            self.db.log_pipeline(self.canal, "video", "error", str(e))
            return None

    def _get_channel_id(self) -> int:
        """Resolve the database channel_id from the canal slug.
        
        Returns the channels.id for self.canal, or None if not found.
        """
        try:
            with self.db._connect() as conn:
                row = conn.execute(
                    "SELECT id FROM channels WHERE slug = ?", (self.canal,)
                ).fetchone()
            return row["id"] if row else None
        except Exception:
            return None

    def phase_metadata(self, script: dict, video_data: dict,
                        source_content: dict = None) -> Optional[dict]:
        """Generate SEO metadata via AI and regenerate thumbnail with overlay text.
        
        Args:
            script: Script dict from phase_generate_script
            video_data: Video data dict from phase_video (with video_path, thumbnail_path)
            source_content: Optional raw_content dict for additional context
            
        Returns:
            metadata dict: {titles, selected_title, description, tags, thumbnail_text, ...}
            Also updates video_data['thumbnail_path'] in-place with the enhanced thumbnail.
        """
        start = time.time()
        self._emit_progress(57, "video", "Ensamblando video...")

        try:
            # 1. Generate AI-powered metadata
            logger.info(f"[{self.canal}] Phase 5a: Generating SEO metadata via AI...")
            metadata = self.metadata_gen.generate(script, source_content)
            
            if not metadata:
                logger.warning(f"[{self.canal}] Metadata generation returned empty — using fallback")
                metadata = self.metadata_gen._fallback_metadata(script)
            
            logger.info(
                f"[{self.canal}] Metadata: title='{metadata['selected_title'][:60]}', "
                f"{len(metadata['tags'])} tags, thumbnail_text='{metadata['thumbnail_text']}'"
            )
            
            # 2. Regenerate thumbnail with viral composition + marketing overlay text
            if metadata.get("thumbnail_text"):
                logger.info(
                    f"[{self.canal}] Phase 5b: Regenerating viral thumbnail with overlay text "
                    f"'{metadata['thumbnail_text']}'"
                )
                try:
                    import json as _json_meta
                    keywords = []
                    kw_raw = script.get("keywords") or script.get("keywords_json", "[]")
                    if isinstance(kw_raw, str):
                        try: keywords = _json_meta.loads(kw_raw)
                        except: pass
                    else:
                        keywords = kw_raw or []
                    
                    # Reuse the base image from phase_video to avoid re-generating with Pollo AI
                    base_img = video_data.get("thumbnail_base_path", "")
                    
                    new_thumb = self.thumbnail_maker.make_viral_thumbnail(
                        title=metadata["selected_title"],
                        overlay_text=metadata["thumbnail_text"],
                        keywords=keywords,
                        scene_images=None,
                        script_text=script.get("guion", "")[:1500],
                        canal_slug=self.canal,
                        channel_display_name=getattr(self.config, "CANAL_DISPLAY_NAME", ""),
                        channel_description=getattr(self.config, "CHANNEL_ABOUT_SECTION", ""),
                        channel_theme=getattr(self.config, "CANAL_TAGLINE", ""),
                        base_image_path=Path(base_img) if base_img else None,
                        video_id=video_data.get("video_id", 0),
                    )
                    video_data["thumbnail_path"] = str(new_thumb)
                    video_data["titulo"] = metadata["selected_title"]
                    logger.info(f"[{self.canal}] Enhanced viral thumbnail: {new_thumb}")
                except Exception as e:
                    logger.warning(f"[{self.canal}] Thumbnail regeneration failed: {e} — keeping original")
            
            duration_ms = int((time.time() - start) * 1000)
            self._timing["phases"]["metadata"] = duration_ms
            self.db.log_pipeline(self.canal, "metadata", "success",
                                 f"Titles: {len(metadata.get('titles',[]))}, Tags: {len(metadata.get('tags',[]))}",
                                 content_id=script.get("id"),
                                 duration_ms=duration_ms)

            # ── Update video record in DB with SEO metadata ──
            video_id = video_data.get("video_id")
            if video_id:
                try:
                    import json as _json_upd
                    self.db.update_video(
                        video_id,
                        titulo_final=metadata.get("selected_title", video_data.get("titulo", "")),
                        description=metadata.get("description", ""),
                        tags_json=_json_upd.dumps(metadata.get("tags", []), ensure_ascii=False),
                        title_options=_json_upd.dumps(metadata.get("titles", []), ensure_ascii=False),
                        thumbnail_path=video_data.get("thumbnail_path", ""),
                        status="ready",
                        progress=100,
                    )
                    logger.info(f"[{self.canal}] Video #{video_id} metadata saved to DB")
                except Exception as e:
                    logger.warning(f"[{self.canal}] Failed to update video #{video_id} metadata: {e}")

            return metadata
            
        except Exception as e:
            logger.error(f"[{self.canal}] Metadata generation failed: {e}")
            self.db.log_pipeline(self.canal, "metadata", "error", str(e))
            
            # Fallback: return basic metadata so pipeline can continue
            return self.metadata_gen._fallback_metadata(script)

    def phase_upload(self, script: dict, video_data: dict,
                      metadata: dict = None, job_id: int = None) -> Optional[str]:
        """Upload video to YouTube. Returns video_id or None.
        
        Args:
            script: Script dict with content info
            video_data: Video data dict with paths
            metadata: Optional SEO metadata dict from phase_metadata().
                      If provided, uses AI-optimized title/description/tags.
                      If None, falls back to config templates (backward compat).
            job_id: Optional generation_jobs.id. If provided, heartbeats are
                    emitted during upload to prevent false orphan detection.
        """
        start = time.time()
        self._emit_progress(80, "upload", "Preparando subida a YouTube...")

        _saved_uploader_db = None  # for finally restore

        try:
            # Authenticate with YouTube
            self._emit_progress(83, "upload", "Autenticando con YouTube...")
            if not self.uploader.authenticate():
                logger.error(f"[{self.canal}] YouTube authentication failed")
                self.db.log_pipeline(self.canal, "upload", "error",
                                      "Auth failed")
                return None

            # Determine title, description, tags — prefer AI metadata over templates
            if metadata:
                title = metadata.get("selected_title", video_data.get("titulo", "Video sin título"))
                description = metadata.get("description", "")
                tags = metadata.get("tags", [])
                logger.info(f"[{self.canal}] Using AI-optimized metadata for upload: "
                           f"title='{title[:60]}', {len(tags)} tags")
            else:
                # Fallback: build from config templates (original behavior)
                import json as _json
                title = video_data.get("titulo", "Video sin título")
                
                kw_raw = script.get("keywords") or script.get("keywords_json", "[]")
                if isinstance(kw_raw, str):
                    tags = _json.loads(kw_raw)
                else:
                    tags = kw_raw or []
                
                seo_desc = script.get("descripcion_seo", "")
                chapters_raw = script.get("chapters", [])
                if isinstance(chapters_raw, str):
                    chapters_raw = _json.loads(chapters_raw)
                
                if chapters_raw:
                    chapters_text = "\n".join(
                        f"{ch.get('time', '0:00')} — {ch.get('title', '')}"
                        for ch in chapters_raw
                    )
                else:
                    chapters_text = "0:00 — Introducción\n0:45 — Desarrollo\n3:20 — Conclusión"
                
                description = self.config.DESCRIPTION_TEMPLATE.format(
                    titulo=title,
                    descripcion_seo=seo_desc,
                    chapters=chapters_text,
                )
                logger.info(f"[{self.canal}] Using template metadata for upload (no AI metadata)")

            # Upload — API mode: suppress uploader's own _log_to_db (single video record)
            _saved_uploader_db = self._uploader.db

            if self.db_video_id is not None:
                self._uploader.db = None

            self._emit_progress(88, "upload", "Subiendo video a YouTube...")
            
            # ── Heartbeat callback for upload phase ──────────────
            # Prevents false orphan detection during slow uploads by
            # pulsing the job's last_heartbeat_at between chunks.
            upload_heartbeat = None
            if job_id is not None:
                def _pulse():
                    try:
                        self.db.update_heartbeat(job_id)
                    except Exception:
                        pass
                upload_heartbeat = _pulse
            
            result = self.uploader.upload(
                video_path=Path(video_data["video_path"]),
                title=title,
                description=description,
                tags=tags,
                thumbnail_path=Path(video_data["thumbnail_path"]),
                category_id=metadata.get("category_id", self.config.YT_CATEGORY_ID) if metadata else self.config.YT_CATEGORY_ID,
                privacy=self.config.YT_PRIVACY_STATUS,
                heartbeat_callback=upload_heartbeat,
            )

            video_id = result.get("video_id")
            url = result.get("url", "")

            if video_id:
                channel_id = self._get_channel_id()
                
                import json as _json2
                tags_json_str = _json2.dumps(tags, ensure_ascii=False) if tags else None
                titles_json_str = None
                if metadata and metadata.get("titles"):
                    titles_json_str = _json2.dumps(metadata["titles"], ensure_ascii=False)
                
                db_video_id = None  # always defined, used below for stats + lifecycle
                if self.db_video_id is not None:
                    # API mode: update the pre-created record (don't insert a new one)
                    self.db.update_video(
                        self.db_video_id,
                        titulo_final=title,
                        description=description,
                        tags_json=tags_json_str,
                        title_options=titles_json_str,
                        privacy_status=self.config.YT_PRIVACY_STATUS,
                        channel_id=channel_id,
                    )
                    # Note: mark_video_uploaded is called by the API layer (generation_service)
                    # to ensure the tracked record gets yt_video_id/yt_url
                    db_video_id = self.db_video_id  # for stats + lifecycle below
                else:
                    # CLI standalone mode: insert + mark
                    db_video_id = self.db.insert_video(
                        script_id=script.get("id"),
                        canal=self.canal,
                        video_path=video_data["video_path"],
                        thumbnail_path=video_data["thumbnail_path"],
                        audio_path=video_data.get("audio_path", ""),
                        titulo_final=title,
                        privacy_status=self.config.YT_PRIVACY_STATUS,
                        channel_id=channel_id,
                        description=description,
                        tags_json=tags_json_str,
                        title_options=titles_json_str,
                    )
                    if db_video_id:
                        self.db.mark_video_uploaded(db_video_id, video_id, url)

            duration_ms = int((time.time() - start) * 1000)
            self._timing["phases"]["upload"] = duration_ms
            self.db.log_pipeline(self.canal, "upload", "success",
                                  f"YouTube ID: {video_id}",
                                  content_id=script.get("id"),
                                  duration_ms=duration_ms)

            # ── Post-upload: baseline stats snapshot (0 views/likes, para DB cache) ──
            try:
                self.db.insert_video_stats(
                    video_id=db_video_id or self.db_video_id,
                    yt_video_id=video_id,
                    stats={"viewCount": 0, "likeCount": 0, "commentCount": 0},
                )
                logger.debug(f"[{self.canal}] Baseline stats saved for video {video_id}")
            except Exception as stats_exc:
                logger.warning(f"[{self.canal}] Failed to save baseline stats: {stats_exc}")

            # ── Post-upload: schedule lifecycle promotion actions ──
            try:
                from pipeline.video_lifecycle import VideoLifecycleManager
                lifecycle = VideoLifecycleManager(self.canal)
                script_text = script.get("script_text") or script.get("texto_completo", "")
                db_vid_for_lifecycle = db_video_id or self.db_video_id
                channel_id_for_lifecycle = self._get_channel_id()
                lifecycle.on_video_published(
                    db_video_id=db_vid_for_lifecycle,
                    yt_video_id=video_id,
                    channel_id=channel_id_for_lifecycle,
                    script_text=script_text,
                )
                logger.info(f"[{self.canal}] Lifecycle actions scheduled for video {video_id}")
            except Exception as lifecycle_exc:
                logger.warning(f"[{self.canal}] Lifecycle scheduling failed (non-critical): {lifecycle_exc}")

            return video_id

        except Exception as e:
            logger.error(f"[{self.canal}] Upload failed: {e}")
            self.db.log_pipeline(self.canal, "upload", "error", str(e))
            return None
        finally:
            # Restore uploader.db (was set to None in API mode to suppress duplicate DB records)
            if hasattr(self, '_uploader') and self._uploader is not None:
                self._uploader.db = _saved_uploader_db

    # ── Full pipeline ──────────────────────────────────────────

    def run_full_pipeline(self, skip_upload: bool = False, job_id: int = None) -> bool:
        """Execute the complete pipeline for one video. Returns True on success.
        
        job_id: Optional generation_jobs.id for heartbeat emission during long phases.
        """
        logger.info(f"{'='*60}")
        logger.info(f"[{self.canal}] STARTING FULL PIPELINE")
        logger.info(f"{'='*60}")

        # ── Disk cleanup before pipeline (moved from phase_media) ──
        import shutil
        _cleanup_dirs = [
            Path("output/video_clips"),
            Path("output/temp"),
        ]
        for _d in _cleanup_dirs:
            if _d.exists():
                try:
                    shutil.rmtree(_d)
                    _d.mkdir(parents=True, exist_ok=True)
                    logger.info("[%s] Cleaned up %s before pipeline", self.canal, _d)
                except Exception as _exc:
                    logger.warning("[%s] Could not clean %s: %s", self.canal, _d, _exc)

        # Phase 0: Scrape fresh content for this video
        logger.info(f"[{self.canal}] Phase 0/6: Scraping fresh content...")
        self.phase_scrape()

        # Phase 1: Generate script from best scraped item
        logger.info(f"[{self.canal}] Phase 1/6: Generating script...")
        script = self.phase_generate_script()
        if not script:
            logger.error(f"[{self.canal}] PIPELINE ABORTED: No script generated from scraped content")
            return False
        logger.info(f"[{self.canal}] Script ready (ID: {script.get('id')})")

        # Phase 2: TTS
        logger.info(f"[{self.canal}] Phase 2/6: Generating TTS audio...")
        audio_data = self.phase_tts(script, job_id=job_id)
        if not audio_data:
            logger.error(f"[{self.canal}] PIPELINE ABORTED: TTS failed")
            return False
        logger.info(f"[{self.canal}] TTS audio: {audio_data['audio_path']}")

        # Phase 3: Media (video + image hybrid)
        logger.info(f"[{self.canal}] Phase 3/6: Fetching media assets (video + image)...")
        media_assets = self.phase_media(script, audio_data)
        if not media_assets:
            logger.error(f"[{self.canal}] PIPELINE ABORTED: Media fetch failed")
            return False
        n_video = sum(1 for a in media_assets if a.get("type") == "video")
        n_image = sum(1 for a in media_assets if a.get("type") == "image")
        logger.info(f"[{self.canal}] Media ready ({len(media_assets)} assets: {n_video} video, {n_image} image)")

        # Phase 4: Video assembly
        logger.info(f"[{self.canal}] Phase 4/6: Assembling video...")
        video_data = self.phase_video(script, audio_data, media_assets, job_id=job_id)
        if not video_data:
            logger.error(f"[{self.canal}] PIPELINE ABORTED: Video assembly failed")
            return False
        logger.info(f"[{self.canal}] Video: {video_data['video_path']}")

        # Phase 5: SEO metadata + enhanced thumbnail
        logger.info(f"[{self.canal}] Phase 5/6: Generating SEO metadata & optimized thumbnail...")
        metadata = self.phase_metadata(script, video_data)
        if metadata:
            logger.info(
                f"[{self.canal}] Metadata ready: '{metadata['selected_title'][:60]}' "
                f"({len(metadata['titles'])} titles, {len(metadata['tags'])} tags)"
            )

        # Phase 6: Upload (optional)
        if not skip_upload:
            logger.info(f"[{self.canal}] Phase 6/6: Uploading to YouTube with optimized metadata...")
            video_id = self.phase_upload(script, video_data, metadata)
            if video_id:
                logger.info(f"[{self.canal}] PIPELINE COMPLETE: https://youtube.com/watch?v={video_id}")
            else:
                logger.warning(f"[{self.canal}] Pipeline complete but upload failed — video saved locally")
        else:
            logger.info(f"[{self.canal}] Upload skipped (--skip-upload)")
            # Save video record to DB so dashboard / web copy can find it
            import json as _json3
            
            title = metadata["selected_title"] if metadata else video_data.get("titulo", "Sin título")
            description = metadata.get("description", "") if metadata else ""
            tags_json_str = _json3.dumps(metadata.get("tags", []), ensure_ascii=False) if metadata else None
            titles_json_str = _json3.dumps(metadata.get("titles", []), ensure_ascii=False) if metadata else None
            channel_id = self._get_channel_id()

            # Reuse the video record created by phase_video (avoid duplicate insert)
            db_vid = video_data.get("video_id")
            if db_vid is not None:
                self.db.update_video(
                    db_vid,
                    titulo_final=title,
                    description=description,
                    tags_json=tags_json_str,
                    title_options=titles_json_str,
                    privacy_status="public",
                    channel_id=channel_id,
                )
            else:
                self.db.insert_video(
                    script_id=script.get("id"),
                    canal=self.canal,
                    video_path=video_data["video_path"],
                    thumbnail_path=video_data.get("thumbnail_path", ""),
                    audio_path=audio_data.get("audio_path", ""),
                    titulo_final=title,
                    privacy_status="public",
                    channel_id=channel_id,
                    description=description,
                    tags_json=tags_json_str,
                    title_options=titles_json_str,
                )
            logger.info(f"[{self.canal}] PIPELINE COMPLETE: Video saved to {video_data['video_path']}")

        # Mark script as used
        self.db.mark_script_used(script.get("id"))

        return True

    # ── Scheduling ─────────────────────────────────────────────

    def get_videos_per_day(self) -> int:
        """Determine how many videos to produce today based on pipeline age."""
        if PIPELINE_START_DATE:
            start_date = datetime.fromisoformat(PIPELINE_START_DATE).replace(tzinfo=timezone.utc)
        else:
            # Assume started today
            start_date = datetime.now(timezone.utc)

        days_running = (datetime.now(timezone.utc) - start_date).days

        if days_running < 7:
            return WEEK1_VIDEOS_PER_DAY
        elif days_running < 14:
            return WEEK2_VIDEOS_PER_DAY
        else:
            return WEEK3_VIDEOS_PER_DAY

    def scheduled_run(self):
        """Entry point for APScheduler — run one video pipeline iteration."""
        try:
            count_today = self.db.get_videos_today(self.canal)
            max_today = self.get_videos_per_day()

            if count_today >= max_today:
                logger.info(f"[{self.canal}] Daily quota reached ({count_today}/{max_today}). Skipping.")
                return

            self.run_full_pipeline(skip_upload=False)
        except Exception as e:
            logger.exception(f"[{self.canal}] Scheduled run crashed: {e}")
            self.db.log_pipeline(self.canal, "orchestrator", "error", str(e))

    def start_scheduler(self):
        """Start APScheduler for continuous operation."""
        videos_per_day = self.get_videos_per_day()

        if videos_per_day <= 0:
            logger.info(f"[{self.canal}] videos_per_day=0 — nothing scheduled")
            return

        # Space out videos evenly across the day
        interval_hours = 24 / videos_per_day
        interval_seconds = int(interval_hours * 3600)

        scheduler = BackgroundScheduler()
        scheduler.add_job(
            self.scheduled_run,
            IntervalTrigger(seconds=interval_seconds),
            id=f"{self.canal}_pipeline",
            name=f"{self.canal} video pipeline",
            replace_existing=True,
        )

        # Also schedule scraping every 6 hours
        scheduler.add_job(
            self.phase_scrape,
            IntervalTrigger(hours=6),
            id=f"{self.canal}_scrape",
            name=f"{self.canal} scrape",
            replace_existing=True,
        )

        scheduler.start()
        logger.info(f"[{self.canal}] Scheduler started: {videos_per_day} video(s)/day "
                     f"(every {interval_hours:.1f}h) + scrape every 6h")

        return scheduler


def setup_logging():
    """Configure logging for the orchestrator."""
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=getattr(logging, LOG_LEVEL.upper(), logging.INFO),
        format=LOG_FORMAT,
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(LOGS_DIR / "autotube.log", encoding="utf-8"),
        ],
    )
    # Reduce noise from third-party libraries
    for lib in ["urllib3", "googleapiclient", "google.auth", "apscheduler", "PIL"]:
        logging.getLogger(lib).setLevel(logging.WARNING)


def run_single(canal: str = "canal1", skip_upload: bool = False):
    """Run a single pipeline execution for one channel."""
    setup_logging()
    orch = PipelineOrchestrator(canal=canal)
    success = orch.run_full_pipeline(skip_upload=skip_upload)
    return 0 if success else 1


def run_scheduled(canal: str = "canal1"):
    """Run the pipeline in scheduled mode (continuous)."""
    setup_logging()
    orch = PipelineOrchestrator(canal=canal)

    logger.info(f"[{canal}] Starting scheduled mode. Press Ctrl+C to stop.")
    logger.info(f"[{canal}] Videos per day: {orch.get_videos_per_day()}")
    logger.info(f"[{canal}] Unused content: {orch.db.get_unused_count(canal)}")

    scheduler = orch.start_scheduler()

    try:
        # Keep the main thread alive
        import signal
        stop_event = signal.Event()
        signal.signal(signal.SIGINT, lambda s, f: stop_event.set())
        signal.signal(signal.SIGTERM, lambda s, f: stop_event.set())
        stop_event.wait()
    except KeyboardInterrupt:
        logger.info("Shutting down...")
    finally:
        if scheduler:
            scheduler.shutdown(wait=False)
        logger.info("Scheduler stopped.")

    return 0
