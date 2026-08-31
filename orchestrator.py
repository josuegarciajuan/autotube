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
from datetime import datetime, timezone, timedelta
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


def _safe_log_error(db, canal: str, phase: str, error_msg: str):
    """Log a pipeline error safely — if the DB is locked, fall back to stderr+file.
    
    Prevents the crash cascade where the error handler itself crashes because
    the SQLite database is locked by another process.  Seen in the wild:
    worker 661 (canal4) failed to INSERT into raw_content because canal2 was
    writing — and then the error handler also failed to INSERT into pipeline_log
    because the lock was STILL held.  This function isolates that failure so the
    orchestrator can continue operating even when DB logging is unavailable.
    """
    try:
        db.log_pipeline(canal, phase, "error", error_msg)
    except Exception as log_exc:
        fallback = f"[safe_log_error] DB locked — writing to stderr: [{canal}] {phase}: {error_msg}"
        print(fallback, file=sys.stderr)
        logging.getLogger("autotube.safe_log").warning(
            "Failed to log pipeline error to DB (%s) — DB may be locked: %s", error_msg, log_exc
        )


class PipelineOrchestrator:
    """Master orchestrator for the Autotube content pipeline."""

    def __init__(self, canal: str, db_path: str = None, db_video_id: Optional[int] = None,
                 progress_callback: Optional[callable] = None,
                 source_mode: str = "original", viral_candidate_id: Optional[int] = None,
                 is_marathon: bool = False, marathon_config: dict = None):
        self.canal = canal
        self.db_video_id = db_video_id  # Si != None, modo API: update en vez de insert
        self._progress_cb = progress_callback  # (percent, phase, message) → None
        self.source_mode = source_mode  # "original" | "viral" | "marathon"
        self.viral_candidate_id = viral_candidate_id  # raw_content.id for viral mode
        self.is_marathon = is_marathon  # flag for marathon ~1h video mode
        self.marathon_config = marathon_config or {}  # {duration_target, num_sections, narrative_format, ...}

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

        # Upload failure classification (read by the worker to decide retry
        # vs ready). True when the local quota dispatcher denied admission
        # (UploadAdmissionDeniedError) — retryable, but NOT quota exhaustion:
        # must NOT trip the per-project quota breaker.
        self._upload_admission_denied = False

        # Inter-phase state (computed once, used in subsequent phases)
        self._last_scene_ranges: list[dict] | None = None
        # Final media assets (1:1 with scene_ranges) — kept for the
        # verification report (scripts/test_visual_coherence.py).
        self._last_media_assets: list[dict] | None = None

        # Theme extraction (lazy-loaded, shared across phases)
        self._theme_extractor = None
        self._theme_context = None

    @property
    def scraper(self):
        if self._scraper is None:
            from scrapers.reddit import RedditScraper
            from scrapers.wikipedia import WikipediaScraper
            self._scraper = {
                "reddit": RedditScraper(config=self.config),
                "wikipedia": WikipediaScraper(config=self.config),
            }
            # ── Secondary scrapers (fallback when primaries yield too little) ──
            self._secondary_scrapers = {}
            try:
                from scrapers.atlas_obscura import AtlasObscuraScraper
                self._secondary_scrapers["atlas_obscura"] = AtlasObscuraScraper(
                    config=self.config,
                )
                logger.debug("[%s] Atlas Obscura scraper available as fallback", self.canal)
            except Exception as e:
                logger.debug("[%s] Atlas Obscura scraper unavailable: %s", self.canal, e)
            try:
                from scrapers.quora import QuoraScraper
                self._secondary_scrapers["quora"] = QuoraScraper(config=self.config)
                logger.debug("[%s] Quora scraper available as fallback", self.canal)
            except Exception as e:
                logger.debug("[%s] Quora scraper unavailable: %s", self.canal, e)
            # Add viral scraper if enabled for this channel
            if getattr(self.config, "VIRAL_ENABLED", False):
                try:
                    from scrapers.youtube_viral import YouTubeViralScraper
                    self._scraper["youtube_viral"] = YouTubeViralScraper(config=self.config)
                except Exception as e:
                    logger.warning("[%s] Could not load viral scraper: %s", self.canal, e)
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
    def _media_strategy(self) -> dict:
        """Channel MEDIA_STRATEGY with defaults deep-merged (cached)."""
        if not hasattr(self, "_cached_media_strategy"):
            from config import defaults as _def
            channel_strategy = getattr(self.config, "MEDIA_STRATEGY", {}) or {}
            default_strategy = getattr(_def, "MEDIA_STRATEGY", {}) or {}
            merged = dict(default_strategy)
            merged.update(channel_strategy)
            self._cached_media_strategy = merged
        return self._cached_media_strategy

    def _generate_and_inject_visual_bible(
        self, bloques: list[dict], scene_ranges: list[dict] | None
    ) -> None:
        """Generate a visual bible via LLM and inject into the media fetcher.

        Called during ``phase_images`` before media fetching begins.
        On failure the method degrades gracefully — the media fetcher
        falls back to its 3-layer prompt (style + concept + tech).
        """
        from pipeline.visual_bible import VisualBibleGenerator
        from pipeline.visual_coherence import VisualCoherenceEngine

        try:
            n_scenes = len(scene_ranges) if scene_ranges else len(bloques)
            # The final timestamp-derived ranges are the bible's scene units.
            if scene_ranges:
                script_text = "\n\n".join(
                    f"ESCENA_FINAL {i} [{r.get('start', 0):.3f}-{r.get('end', 0):.3f}s]: "
                    f"{r.get('fragment_text') or r.get('texto', '')}"
                    for i, r in enumerate(scene_ranges)
                )
            else:
                script_text = "\n\n".join(
                    b.get("texto", "") for b in bloques if b.get("texto")
                )
            if not script_text or n_scenes == 0:
                logger.warning("[%s] Visual bible: empty script — skipping", self.canal)
                return

            self._emit_progress(43, "images",
                "Generando biblia visual con IA...")

            bible_gen = VisualBibleGenerator(
                script_generator=self.script_gen,
                channel_cfg=self.config,
            )
            bible_model = self._media_strategy.get("visual_bible_model")
            visual_bible = bible_gen.generate(
                script_text=script_text,
                num_scenes=n_scenes,
                model_name=bible_model or None,
            )

            # Build coherence engine enriched with the bible
            coherence = VisualCoherenceEngine(self.config, visual_bible.to_dict())

            # Inject into media fetcher
            self.media_fetcher.set_visual_context(
                visual_bible=visual_bible.to_dict(),
                coherence_engine=coherence,
            )
            logger.info(
                "[%s] Visual bible injected: universe=%s..., %d scenes, "
                "entity=%s",
                self.canal,
                visual_bible.visual_universe[:50],
                len(visual_bible.scene_visual_map),
                visual_bible.central_entity.get("type", "none"),
            )
            self._emit_progress(44, "images",
                "Biblia visual generada. Buscando media...")
        except Exception as exc:
            logger.warning(
                "[%s] Visual bible generation failed: %s — proceeding without bible",
                self.canal, exc,
            )

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

    @property
    def theme_extractor(self):
        """Lazy-loaded ThemeExtractor — extracts visual keywords for content coherence."""
        if self._theme_extractor is None:
            from pipeline.theme_extractor import ThemeExtractor, NicheGuardrailError
            self._theme_extractor = ThemeExtractor(config=self.config)
        return self._theme_extractor

    def _extract_and_set_theme(self, content_text: str, content_title: str = "") -> None:
        """Extract visual theme from content and inject into script gen + media fetcher.

        This ensures the LLM generates search queries with thematic coherence,
        and the media fetcher enriches stock searches with theme keywords.

        Safe to call multiple times — only re-extracts if theme_context is None.
        """
        if self._theme_context is not None:
            return  # already extracted for this pipeline run

        if not content_text or len(content_text.strip()) < 100:
            logger.warning(
                "[%s] Content too short for theme extraction (%d chars) — skipping",
                self.canal, len(content_text.strip()),
            )
            return

        from pipeline.theme_extractor import NicheGuardrailError as _NicheGuardrailError

        try:
            channel_name = getattr(self.config, "CANAL_DISPLAY_NAME", self.canal)
            channel_theme = getattr(self.config, "CANAL_TAGLINE", "")
            niche_keywords = getattr(self.config, "NICHE_KEYWORDS_ENG", None)

            self._theme_context = self.theme_extractor.extract(
                content_text=content_text[:4000],
                channel_name=channel_name,
                channel_theme=channel_theme,
                niche_keywords=niche_keywords,
            )

            if self._theme_context and self._theme_context.theme_keywords_en:
                logger.info(
                    "[%s] Theme extracted: genre=%s era=%s keywords=%s",
                    self.canal,
                    self._theme_context.genre,
                    self._theme_context.era,
                    self._theme_context.theme_keywords_en,
                )

                # Inject into downstream components
                self.script_gen.set_theme_context(self._theme_context)
                self.media_fetcher.set_theme_context(self._theme_context)
            else:
                logger.warning("[%s] Theme extraction returned empty keywords", self.canal)
                self._theme_context = None
        except _NicheGuardrailError:
            raise  # Fatal — propagate to abort the pipeline
        except Exception as exc:
            logger.warning("[%s] Theme extraction failed (non-fatal): %s", self.canal, exc)
            self._theme_context = None

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

    def _emit_progress(self, percent: int, phase: str, message: str, **kwargs) -> None:
        """Fire the progress callback (if set). No-op when running CLI standalone.

        Extra kwargs (current/total/sub_phase/detail...) are forwarded so the
        frontend can show richer data (bytes, scene x/y, sub-phase).
        """
        if self._progress_cb:
            try:
                self._progress_cb(percent, phase, message, **kwargs)
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
        # Viral scraper: skip in non-viral mode (runs on-demand in _phase_generate_script_viral)
        scraper_names = list(self.scraper.keys())
        active_scrapers = [n for n in scraper_names
                          if not (n == "youtube_viral" and self.source_mode != "viral")]
        n_active = len(active_scrapers) if active_scrapers else 1
        scraper_idx = 0
        for scraper_name, s in self.scraper.items():
            if scraper_name == "youtube_viral" and self.source_mode != "viral":
                logger.debug("[%s] Skipping viral scraper in %s mode", self.canal, self.source_mode)
                continue
            # Stagger progress per scraper: 6→9% range
            scraper_pct = 6 + int((scraper_idx / n_active) * 3)
            scraper_idx += 1
            try:
                timeout = 600 if scraper_name == "youtube_viral" else 300
                progress_msg = (
                    f"Buscando videos virales en YouTube ({scraper_name})..."
                    if scraper_name == "youtube_viral"
                    else f"Scraping {scraper_name}..."
                )
                self._emit_progress(scraper_pct, "scrape", progress_msg)
                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                    future = executor.submit(s.save_to_db, self.db)
                    count = future.result(timeout=timeout)
                added += count
                logger.info(f"[{self.canal}] {scraper_name}: {count} items scraped")
            except concurrent.futures.TimeoutError:
                logger.error(f"[{self.canal}] {scraper_name} scrape timed out after {timeout}s")
                _safe_log_error(self.db, self.canal, "scrape", f"timeout after {timeout}s")
            except Exception as e:
                logger.error(f"[{self.canal}] {scraper_name} scrape failed: {e}")
                _safe_log_error(self.db, self.canal, "scrape", str(e))

        # ── Fallback: if primaries yielded too little, try secondaries ──
        MIN_CONTENT_THRESHOLD = 3
        if added < MIN_CONTENT_THRESHOLD and self._secondary_scrapers:
            logger.warning(
                "[%s] Primary scrapers only added %d items (< %d) — "
                "activating secondary scrapers: %s",
                self.canal, added, MIN_CONTENT_THRESHOLD,
                list(self._secondary_scrapers.keys()),
            )
            for scraper_name, s in self._secondary_scrapers.items():
                try:
                    self._emit_progress(8, "scrape",
                                        f"Scraping {scraper_name} (fallback)...")
                    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                        future = executor.submit(s.save_to_db, self.db)
                        count = future.result(timeout=300)
                    added += count
                    logger.info(
                        "[%s] %s (fallback): %d items scraped",
                        self.canal, scraper_name, count,
                    )
                except Exception as e:
                    logger.error(
                        "[%s] %s fallback scrape failed: %s",
                        self.canal, scraper_name, e,
                    )

        self._emit_progress(10, "scrape", f"Contenido obtenido: {added} fuentes")
        duration_ms = int((time.time() - start) * 1000)
        self._timing["phases"]["scrape"] = duration_ms
        self.db.log_pipeline(self.canal, "scrape", "success",
                             f"Added {added} items", duration_ms=duration_ms)
        return added

    def phase_generate_script(self) -> Optional[dict]:
        """Generate ONE script from unused content. Returns script dict or None.

        When source_mode='viral', uses the pre-translated/adapted viral script
        from raw_content instead of calling the LLM script generator.
        When is_marathon=True, generates a long-form ~1h marathon script.
        """
        start = time.time()

        if self.source_mode == "viral":
            return self._phase_generate_script_viral(start)

        # ── Marathon mode: long-form deep script generation ──
        if self.is_marathon:
            return self._phase_generate_script_marathon(start)

        content_items = self.db.get_unused_content(canal=self.canal, limit=5)
        if not content_items:
            logger.warning(f"[{self.canal}] No unused content available for script generation")
            self.db.log_pipeline(self.canal, "script", "skipped",
                                 "No unused content")
            return None

        # ── Anti-strike: filtro de seguridad de contenido ─────────────
        # Rechaza temas sensibles (menores, autolesión, claims médicos, violencia
        # gráfica, desinformación sanitaria) antes de guionar. Itera sobre los
        # candidatos: si un topic se marca, se descarta (mark_content_used) y se
        # prueba con el siguiente, evitando long-forms sobre temas que YouTube
        # elimina.
        for content_item in content_items:
            _ct = content_item.get("text", "")
            _ct_title = content_item.get("title", "")
            try:
                from pipeline.content_safety import classify_topic_safety
                _verdict = classify_topic_safety(
                    topic=_ct_title, title=_ct_title,
                    script_texts=[_ct],
                    config=getattr(self, "config", None),
                    use_llm=False,  # determinista: barato para filtrar candidatos
                )
            except Exception as _cs_exc:
                logger.warning(f"[{self.canal}] Content-safety check error (fail-open): {_cs_exc}")
                _verdict = None
            if _verdict is not None and not _verdict.safe:
                logger.warning(
                    "[%s] Contenido rechazado por seguridad: '%s' — %s (probando siguiente)",
                    self.canal, (_ct_title or _ct)[:80], _verdict.reason,
                )
                try:
                    self.db.mark_content_used(content_item.get("id"))
                except Exception as _mcu:
                    logger.warning(f"[{self.canal}] mark_content_used failed: {_mcu}")
                continue
            # Tema seguro → generar guion con este item.
            break
        else:
            logger.warning(f"[{self.canal}] Todos los contenidos candidatos rechazados por seguridad")
            return None

        # ── Extract visual theme BEFORE script generation (Bug fix: ThemeExtractor was dead code) ──
        content_text = content_item.get("text", "")
        content_title = content_item.get("title", "")
        self._extract_and_set_theme(content_text, content_title)

        self._emit_progress(15, "script", "Eligiendo mejor contenido y generando guion con IA...")
        result = self.script_gen.generate(content_item)

        # ── Anti-strike: verificación post-guion (el LLM puede enmarcar un
        # tema neutro de forma sensible) ──
        if result:
            try:
                from pipeline.content_safety import classify_topic_safety
                _tit = (result.get("titulo")
                        or (result.get("titulo_options") or [None])[0]
                        or "")
                _guion = result.get("guion", "") or ""
                _post = classify_topic_safety(
                    topic=_tit, title=_tit, script_texts=[_guion],
                    config=getattr(self, "config", None),
                )
                if not _post.safe:
                    logger.warning(
                        "[%s] Guion rechazado por seguridad (post-guion): '%s' — %s",
                        self.canal, _tit[:80], _post.reason,
                    )
                    try:
                        self.db.mark_content_used(content_item.get("id"))
                    except Exception as _mcu2:
                        logger.warning(f"[{self.canal}] mark_content_used failed: {_mcu2}")
                    # A safe source may still become unsafe after the LLM
                    # reframes it. Consume that candidate and continue with
                    # the next source instead of turning one rejection into
                    # channel-wide starvation.
                    return self.phase_generate_script()
            except Exception as _pc_exc:
                logger.warning(f"[{self.canal}] Post-guion safety check error (fail-open): {_pc_exc}")

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
            _safe_log_error(self.db, self.canal, "script", "Script generation failed")
            # Note: duration_ms logging lost in error path, but safety-first
        return result

    def _get_viral_scraper(self):
        """Get or create the YouTubeViralScraper instance for on-demand use.

        Returns None if viral is not enabled or scraper cannot be loaded.
        """
        if getattr(self, "_viral_scraper_instance", None) is not None:
            return self._viral_scraper_instance

        if not getattr(self.config, "VIRAL_ENABLED", False):
            return None

        try:
            from scrapers.youtube_viral import YouTubeViralScraper
            self._viral_scraper_instance = YouTubeViralScraper(config=self.config)
            self._viral_scraper_instance._db = self.db  # for URL dedup checks
            return self._viral_scraper_instance
        except Exception as e:
            logger.warning("[%s] Could not load viral scraper: %s", self.canal, e)
            return None

    @staticmethod
    def _build_viral_fallback_dict(processed: dict, db_id: Optional[int]) -> dict:
        """Build a viral_content pseudo-dict from processed candidate data.

        When the DB insert/update fails (e.g., race condition, transient error),
        this builds a dict with all the fields needed for script generation.
        It does NOT set a fake raw_content id (id is None/null → FK-safe).
        """
        result = {
            "source_mode": "viral",
            "viral_script_es": processed.get("viral_script_es", ""),
            "viral_original_title": processed.get("viral_original_title", ""),
            "viral_original_video_url": processed.get("viral_original_video_url", ""),
            "viral_original_thumbnail_url": processed.get("viral_original_thumbnail_url"),
            "viral_meta_json": processed.get("viral_meta_json", "{}"),
            "viral_score": processed.get("viral_score", 0),
            "viral_views": processed.get("viral_views", 0),
        }
        # Only set id if we have a valid one from DB
        if db_id:
            result["id"] = db_id
        return result

    def _phase_generate_script_viral(self, start: float) -> Optional[dict]:
        """Generate script from a viral mirror candidate — two-phase approach.

        FASE A (Discovery): Fast path checks DB; if no candidates, runs
        multi-strategy YouTube search to find viral videos.
        FASE B (Processing): Downloads, transcribes, translates, adapts and
        builds script blocks from the best candidate. Tries up to 5 candidates
        if earlier ones fail.
        """
        logger.info("[%s] VIRAL SCRIPT: Two-phase approach starting", self.canal)
        self._emit_progress(12, "script", "Descubriendo videos virales...")

        # ── Phase A: Discovery ──────────────────────────────────
        viral_content = None
        saved_to_db = False  # whether we already inserted into raw_content

        # A1 — Fast path: use explicitly provided candidate
        if self.viral_candidate_id:
            viral_content = self.db.get_content_by_id(self.viral_candidate_id)
            if viral_content and viral_content.get("source_mode") == "viral":
                logger.info("[%s] Using explicit viral candidate #%d: '%s'",
                            self.canal, viral_content["id"],
                            viral_content.get("viral_original_title", "")[:60])
            else:
                logger.warning("[%s] Explicit candidate #%d not found or not viral",
                               self.canal, self.viral_candidate_id)
                viral_content = None

        # A2 — Fast path: check DB for unused candidates
        if not viral_content:
            candidates = self.db.get_viral_candidates(self.canal, limit=3)
            if candidates:
                viral_content = candidates[0]
                self.viral_candidate_id = viral_content["id"]
                logger.info("[%s] Fast path: DB candidate #%d '%s' (score=%.1f, views=%d)",
                            self.canal, viral_content["id"],
                            viral_content.get("viral_original_title", "")[:60],
                            viral_content.get("viral_score", 0),
                            viral_content.get("viral_views", 0))

        # A3 — Broad discovery: run multi-strategy search on-demand
        if not viral_content:
            logger.info("[%s] No unused candidates in DB — running on-demand discovery", self.canal)
            self._emit_progress(13, "script", "Buscando videos virales en YouTube (6 estrategias)...")

            scraper = self._get_viral_scraper()
            if not scraper:
                logger.error("[%s] VIRAL SCRIPT: Viral scraper could not be loaded", self.canal)
                return self._viral_fallback_to_original(start)

            try:
                discovered = scraper.discover_multi_strategy(db=self.db)
            except Exception as exc:
                logger.warning("[%s] Discovery failed: %s", self.canal, exc)
                import traceback
                logger.debug(traceback.format_exc())
                return self._viral_fallback_to_original(start)

            if not discovered:
                logger.warning("[%s] Discovery returned 0 candidates — nothing found on YouTube", self.canal)
                return self._viral_fallback_to_original(start)

            # ── Phase B: Process candidates ─────────────────────
            self._emit_progress(14, "script",
                                f"Procesando {min(5, len(discovered))} candidatos virales...")

            max_attempts = min(5, len(discovered))
            for attempt, candidate in enumerate(discovered[:max_attempts], 1):
                logger.info("[%s] Attempt %d/%d: '%s' (score=%.0f, views=%s)",
                            self.canal, attempt, max_attempts,
                            candidate.get("title", "")[:70],
                            candidate.get("viral_score", 0),
                            candidate.get("views", 0))

                try:
                    processed = scraper.process_candidate(candidate)
                except Exception as exc:
                    logger.warning("[%s] Candidate %d processing crashed: %s", self.canal, attempt, exc)
                    continue

                if processed:
                    # Save to raw_content for future fast-path use
                    try:
                        canal = getattr(scraper, "canal", self.canal)
                        content_id = self.db.insert_raw_content_viral(
                            source=processed["source"],
                            url=processed["url"],
                            title=processed["title"],
                            text=processed["text"],
                            subreddit=processed.get("subreddit"),
                            score=processed.get("score", 0),
                            canal=canal,
                            source_mode="viral",
                            viral_original_title=processed.get("viral_original_title"),
                            viral_original_description=processed.get("viral_original_description"),
                            viral_original_thumbnail_url=processed.get("viral_original_thumbnail_url"),
                            viral_original_video_url=processed.get("viral_original_video_url"),
                            viral_views=processed.get("viral_views", 0),
                            viral_upload_date=processed.get("viral_upload_date"),
                            viral_duration_sec=processed.get("viral_duration_sec", 0),
                            viral_channel_name=processed.get("viral_channel_name"),
                            viral_score=processed.get("viral_score", 0.0),
                            viral_script_es=processed.get("viral_script_es"),
                            viral_meta_json=processed.get("viral_meta_json"),
                        )
                        saved_to_db = True

                        if content_id:
                            # Use the returned ID to fetch the full row
                            db_content = self.db.get_content_by_id(content_id)
                            if db_content:
                                viral_content = db_content
                                self.viral_candidate_id = content_id
                            else:
                                # Row was inserted/updated but can't be read back — rare race condition
                                logger.warning("[%s] Insert/update returned id=%s but get_content_by_id failed — building fallback",
                                               self.canal, content_id)
                                viral_content = self._build_viral_fallback_dict(processed, content_id)
                        else:
                            # insert_raw_content_viral returned None — unexpected error
                            # Try fetching by URL as last resort
                            db_content = self.db.get_content_by_url(processed["url"], self.canal)
                            if db_content:
                                viral_content = db_content
                                self.viral_candidate_id = viral_content["id"]
                                logger.warning("[%s] insert_raw_content_viral returned None but row found via get_content_by_url (id=%s)",
                                               self.canal, viral_content["id"])
                            else:
                                logger.warning("[%s] insert_raw_content_viral returned None and get_content_by_url also failed — building fallback",
                                               self.canal)
                                viral_content = self._build_viral_fallback_dict(processed, None)
                    except Exception as exc:
                        logger.warning("[%s] Failed to save candidate to DB: %s", self.canal, exc)
                        # Still usable — build pseudo-dict (no DB id)
                        viral_content = self._build_viral_fallback_dict(processed, None)
                    break  # success!
                else:
                    logger.warning("[%s] Candidate %d returned None — trying next", self.canal, attempt)

            if not viral_content:
                logger.warning("[%s] All %d candidates failed during processing", self.canal, max_attempts)
                return self._viral_fallback_to_original(start)

        # ── Build script from viral_content vía ScriptGenerator ────
        # v22.2: Viral content now passes through the SAME script generation
        # pipeline as original content. The viral_script_es is treated as
        # source research material (not final narration), fed into
        # ScriptGenerator.generate_v2() which creates an original script
        # with outline, batch generation, enrichment, and validation.
        #
        # This replaces the old approach of using viral_script_es directly
        # as blocks, which caused literal translations of comedy/meme content
        # to be published as documentary narration (e.g., Bill Wurtz incident).
        #
        # Controlled by VIRAL_CONTENT_MODE per channel ("rewrite" = default).
        viral_mode = getattr(self.config, "VIRAL_CONTENT_MODE", "rewrite")
        logger.info("[%s] VIRAL: content mode=%s", self.canal, viral_mode)

        if not viral_content:
            return self._viral_fallback_to_original(start)

        script_es = viral_content.get("viral_script_es", "")
        original_title = viral_content.get("viral_original_title", "")
        viral_meta_json = viral_content.get("viral_meta_json", "{}")

        if not script_es:
            logger.error("[%s] Viral candidate has no script (viral_script_es empty)", self.canal)
            return self._viral_fallback_to_original(start)

        # ── Extract visual theme from viral script ──
        self._extract_and_set_theme(script_es, original_title)

        # Parse viral metadata
        try:
            viral_meta = json.loads(viral_meta_json) if isinstance(viral_meta_json, str) else viral_meta_json
        except (json.JSONDecodeError, TypeError):
            viral_meta = {}

        # ── Inject metadata for later phases ──
        if viral_content.get("viral_original_description"):
            viral_meta.setdefault("original_description", viral_content["viral_original_description"])
        if viral_content.get("viral_original_video_url"):
            viral_meta.setdefault("original_url", viral_content["viral_original_video_url"])

        translated_title = viral_meta.get("translated_title") or original_title or "Video"

        # ── Feed viral content through ScriptGenerator (same as original pipeline) ──
        self._emit_progress(15, "script",
                            f"Generando guion documental original desde fuente viral...")
        logger.info(
            "[%s] VIRAL: Feeding %d words of source material through ScriptGenerator",
            self.canal, len(script_es.split()),
        )

        content_item = {
            "id": viral_content.get("id") or None,
            "title": translated_title,
            "text": script_es,  # original Spanish content, not a translation
            "source": "youtube_viral",
            "subreddit": viral_content.get("subreddit", ""),
            "score": viral_content.get("viral_score", 0),
            "category": getattr(self.config, "CANAL_CATEGORY", "documental"),
            "_palabras_objetivo": None,  # let ScriptGenerator compute from channel config
        }

        # Call the same pipeline used for original content
        result = self.script_gen.generate(content_item)
        duration_ms = int((time.time() - start) * 1000)
        self._timing["phases"]["script"] = duration_ms

        if not result:
            logger.warning("[%s] VIRAL: ScriptGenerator returned None — falling back to original", self.canal)
            return self._viral_fallback_to_original(start)

        # ── Attach viral metadata to result for later phases (title gen, thumbnail, etc.) ──
        result["_viral_meta"] = viral_meta
        result["_viral_meta_json"] = json.dumps(viral_meta, ensure_ascii=False)
        result["_viral_original_thumbnail"] = viral_content.get("viral_original_thumbnail_url", "")
        result["_viral_original_title"] = original_title
        result["_viral_original_video_url"] = viral_content.get("viral_original_video_url", "")
        result["_viral_content_id"] = viral_content.get("id", 0)

        word_count = len(result.get("guion", "").split()) if result.get("guion") else 0
        self._emit_progress(23, "script",
                            f"Guion viral generado: {word_count} palabras, script #{result.get('id')}")
        self.db.log_pipeline(self.canal, "script", "success",
                              f"Viral script {result.get('id')} via ScriptGenerator",
                              content_id=result.get("id"), duration_ms=duration_ms)

        # Mark viral content as used
        if viral_content.get("id", 0) > 0:
            self.db.mark_content_used(viral_content["id"])

        return result

    def _viral_fallback_to_original(self, start: float) -> Optional[dict]:
        """Ultimate fallback: switch to original mode with explicit warning."""
        logger.warning(
            "[%s] ⚠ VIRAL FALLBACK: No viral candidates found after exhaustive search. "
            "Switching to original mode as last resort.", self.canal
        )
        self.source_mode = "original"
        self._emit_progress(15, "script",
                            "⚠ Sin candidatos virales — generando guion original con IA...")
        content_items = self.db.get_unused_content(canal=self.canal, limit=5)
        if not content_items:
            logger.error("[%s] No original content available either — pipeline cannot continue", self.canal)
            return None
        return self.script_gen.generate(content_items[0])

    def _phase_generate_script_marathon(self, start: float) -> Optional[dict]:
        """Generate a long-form ~1h marathon script with deep content.

        Uses extended Wikipedia scraping (deep mode), a multi-chapter outline,
        and the ScriptGenerator's marathon-specific path for high word-count scripts.
        """
        cfg = self.config
        mc = self.marathon_config

        duration_target = mc.get("duration_target", 60)
        num_sections = mc.get("num_sections", 12)
        narrative_format = mc.get("narrative_format", "top_cases")

        logger.info(
            "[MARATHON][%s] Starting marathon script: %dmin, %d sections, format=%s",
            self.canal, duration_target, num_sections, narrative_format,
        )
        self._emit_progress(12, "marathon_script",
                            f"Generando guion maratón de {duration_target}min con IA...")

        # ── Deep scrape: fetch rich content for marathon ──
        content_items = []
        try:
            wiki_scraper = self.scraper.get("wikipedia")
            if wiki_scraper:
                self._emit_progress(14, "marathon_scrape",
                                    f"Raspando artículos profundos de Wikipedia...")
                # Use deep mode: full articles with higher category limit
                deep_items = wiki_scraper.scrape(
                    config=self.config,
                    mode="deep",
                    limit=20,
                )
                if deep_items:
                    # ── Save to raw_content so they get valid DB IDs ──
                    # Deep-scraped Wikipedia articles were previously passed
                    # directly to generate_marathon() without DB IDs, causing
                    # FK violations on scripts.raw_content_id and triggering
                    # the emergency fallback (2-min videos instead of 60-min).
                    saved_items = []
                    for article in deep_items:
                        article_id = self.db.insert_raw_content(
                            source="wikipedia_deep",
                            url=article.get("url", ""),
                            title=article.get("title", "")[:200],
                            text=article.get("text", "")[:8000],
                            score=article.get("score", 1),
                            canal=self.canal,
                        )
                        if article_id:
                            article["id"] = article_id
                            saved_items.append(article)
                    content_items.extend(saved_items)
                    logger.info(
                        "[MARATHON][%s] Deep scrape: %d articles (%d total chars)",
                        self.canal, len(deep_items),
                        sum(len(item.get("text", "")) for item in deep_items),
                    )
        except Exception as exc:
            logger.warning("[MARATHON][%s] Deep scrape failed: %s — falling back to normal scrape",
                           self.canal, exc)
            # Fallback to normal content
            fallback = self.db.get_unused_content(canal=self.canal, limit=10)
            if fallback:
                content_items = fallback

        # ── Fase 3 bis: el maratón ABSORBE la cola de temas pendientes ──
        # Con la fábrica continua la cola acumula contenido sin publicar; se
        # fusionan los temas unused (raw_content sin consumir) con el scrape
        # profundo para que el maratón digiera la cola del canal en vez de
        # repetir los mismos temas en vídeos sueltos. Dedup por título/url.
        if content_items:
            try:
                extra = self.db.get_unused_content(canal=self.canal, limit=10)
                if extra:
                    seen_titles = {
                        (str(i.get("title") or "")[:120], str(i.get("url") or ""))
                        for i in content_items if i.get("id")
                    }
                    merged = 0
                    for item in extra:
                        key = (str(item.get("title") or "")[:120], str(item.get("url") or ""))
                        if key in seen_titles or not item.get("id"):
                            continue
                        seen_titles.add(key)
                        content_items.append(item)
                        merged += 1
                    if merged:
                        logger.info(
                            "[MARATHON][%s] Cola absorbida: +%d temas pendientes fusionados al maratón",
                            self.canal, merged,
                        )
            except Exception as exc:
                logger.debug("[MARATHON][%s] Merge de cola no disponible: %s", self.canal, exc)

        if not content_items:
            # Last resort: use any available content
            content_items = self.db.get_unused_content(canal=self.canal, limit=10)

        if not content_items:
            logger.error("[MARATHON][%s] No content available for marathon script", self.canal)
            return None

        # ── Generate marathon script ──
        self._emit_progress(16, "marathon_script",
                            f"Generando outline con {mc.get('outline_chapters', 15)} capítulos...")

        result = self.script_gen.generate_marathon(
            content_items=content_items,
            canal_config=cfg,
            duration_target=duration_target,
            num_sections=num_sections,
            narrative_format=narrative_format,
            outline_chapters=mc.get("outline_chapters", 15),
            words_min=mc.get("script_words_min", 8000),
            words_max=mc.get("script_words_max", 12000),
            blocks_min=mc.get("script_blocks_min", 50),
            blocks_max=mc.get("script_blocks_max", 90),
            llm_max_batches=mc.get("llm_max_batches", 150),
            llm_max_empty_strikes=mc.get("llm_max_empty_strikes", 20),
        )

        duration_ms = int((time.time() - start) * 1000)
        self._timing["phases"]["marathon_script"] = duration_ms

        if result:
            words = len(result.get("guion", "").split()) if result.get("guion") else 0
            self._emit_progress(23, "marathon_script",
                                f"[MARATHON] Guion generado: {words} palabras ({num_sections} secciones)")
            self.db.log_pipeline(self.canal, "marathon_script", "success",
                                 f"Marathon script {result.get('id')}: {words} words, "
                                 f"{num_sections} sections, {duration_target}min target",
                                 content_id=result.get("id"),
                                 duration_ms=duration_ms)
        else:
            _safe_log_error(self.db, self.canal, "marathon_script",
                            "Marathon script generation failed")

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

                # ── Acquire cross-process TTS lock (prevents concurrent Kokoro for same channel) ──
                channel_id = self._get_channel_id()
                tts_lock_acquired = False
                if channel_id is not None:
                    tts_lock_acquired = self.db.acquire_tts_lock(channel_id, job_id or 0)
                    if not tts_lock_acquired:
                        logger.warning("[%s] TTS lock BUSY for channel_id=%d — another Kokoro worker is active. Waiting up to 60s...",
                                    self.canal, channel_id)
                        # Poll for up to 60 seconds
                        for _ in range(12):
                            time.sleep(5)
                            tts_lock_acquired = self.db.acquire_tts_lock(channel_id, job_id or 0)
                            if tts_lock_acquired:
                                logger.info("[%s] TTS lock acquired after wait", self.canal)
                                break
                        if not tts_lock_acquired:
                            logger.error("[%s] TTS lock STILL busy after 60s — proceeding anyway (potential RTF degradation)", self.canal)
                    else:
                        logger.info("[%s] TTS lock acquired for channel_id=%d", self.canal, channel_id)

                try:
                    tts_total = len(bloques)
                    def _tts_progress(i_block: int, total: int):
                        if total > 0:
                            pct = 31 + int((i_block / total) * 6)
                            self._emit_progress(pct, "tts",
                                f"Generando voz: bloque {i_block}/{total}...")
                    audio_path, timestamps = self.tts.generate_segmented(
                        bloques, progress_cb=_tts_progress,
                    )
                finally:
                    if tts_lock_acquired and channel_id is not None:
                        self.db.release_tts_lock(channel_id, job_id or 0)
                        logger.info("[%s] TTS lock released for channel_id=%d", self.canal, channel_id)

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
            _safe_log_error(self.db, self.canal, "tts", str(e))
            return None

    def phase_media(self, script: dict, audio_data: Optional[dict] = None, job_id: int = None) -> Optional[list[dict]]:
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
            self._last_media_assets = None

            # ── Phase 3: Visual Bible (LLM-generated visual direction) ──
            if self._media_strategy.get("visual_bible_enabled", False):
                self._generate_and_inject_visual_bible(bloques, scene_ranges)

            logger.info("[%s] Fetching media for %d scenes", self.canal,
                        len(scene_ranges) if scene_ranges else len(bloques))

            # Fetch one asset per scene (scene_ranges or bloques as fallback)
            n_total = len(scene_ranges) if scene_ranges else len(bloques)
            def _media_progress(i_scene: int, total: int):
                if total > 0:
                    pct = 46 + int((i_scene / total) * 6)
                    self._emit_progress(pct, "images",
                        f"Descargando media: {i_scene}/{total}...")
            media_assets = self.media_fetcher.fetch_for_script(
                bloques=bloques,
                scene_ranges=scene_ranges,
                progress_cb=_media_progress,
            )
            self._last_media_assets = media_assets

            # Post-process images only (videos are used as-is)
            skip_processing = getattr(self.config, "IMAGE_PROCESSING_DISABLED", False)
            if skip_processing:
                logger.info("[%s] Image processing disabled (test mode) — using raw images", self.canal)
            for asset in media_assets:
                if asset["type"] == "image" and asset["path"] and not skip_processing:
                    try:
                        asset["path"] = self.image_processor.process(Path(asset["path"]))
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
            # ── Lock media files for this job ──
            if job_id is not None:
                _media_paths = [str(a["path"]) for a in media_assets if a.get("path")]
                if _media_paths:
                    _locked = self.db.lock_media_files(job_id, _media_paths)
                    logger.info("[%s] Locked %d media files for job #%d", self.canal, _locked, job_id)
            return media_assets

        except Exception as e:
            logger.error(f"[{self.canal}] Media fetch failed: {e}")
            _safe_log_error(self.db, self.canal, "media", str(e))
            return None

    def _media_fetch_on_demand(self, query: str, duration: float):
        """Fetch a single image urgently for dynamic gap filling.

        Called by the VideoEditor during rendering when a scene has no
        available asset. Searches all image providers for one image
        matching the query. Returns a Path or None.
        """
        from pathlib import Path as _Path2
        try:
            asset = self.media_fetcher.fetch_single_image_urgent(query)
            if asset and asset.get("path") and _Path2(asset["path"]).exists():
                return _Path2(asset["path"])
        except Exception as e:
            logger.warning("[%s] On-demand image fetch failed for query=%r: %s",
                           self.canal, query[:60], e)
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
            _safe_log_error(self.db, self.canal, "images", str(e))
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
                # Set on-demand image fetcher for dynamic gap filling
                self.video_editor._on_demand_fetcher = self._media_fetch_on_demand
                video_path = self.video_editor.build_video(
                    bloques=bloques,
                    media_assets=media_assets,
                    audio_path=audio_data["audio_path"],
                    timestamps=audio_data["timestamps"],
                    scene_ranges=getattr(self, "_last_scene_ranges", None),
                    job_id=job_id,
                    cta_audio_path=audio_data.get("cta_audio_path"),
                    video_id=self.db_video_id,
                    progress_cb=self._emit_progress,
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
                # Check if viral mode with original thumbnail URL
                viral_thumb_url = script.get("_viral_original_thumbnail", "")
                if self.source_mode == "viral" and viral_thumb_url:
                    self._emit_progress(65, "video", "Clonando miniatura viral (Vision AI + Pollo AI)...")
                    logger.info("[%s] VIRAL THUMB: Cloning from '%s'", self.canal, viral_thumb_url[:100])
                    from pipeline.viral_cloner import clone_thumbnail
                    video_id_for_thumb = self.db_video_id or 0
                    thumbnail_path = clone_thumbnail(
                        original_thumbnail_url=viral_thumb_url,
                        channel_slug=self.canal,
                        channel_display_name=getattr(self.config, "CANAL_DISPLAY_NAME", ""),
                        channel_description=getattr(self.config, "CHANNEL_ABOUT_SECTION", ""),
                        channel_theme=getattr(self.config, "CANAL_TAGLINE", ""),
                        script_text=script.get("guion", "")[:1500],
                        keywords=keywords if 'keywords' in dir() else [],
                        video_id=video_id_for_thumb,
                    )
                    logger.info("[%s] Viral thumbnail cloned: %s", self.canal, thumbnail_path)
                    # Save raw base path for phase_metadata recompose (pre-F4 Pollo image)
                    if video_id_for_thumb:
                        _viral_raw_base = Path("output/thumbnails") / self.canal / f"viral_raw_{video_id_for_thumb}.jpg"
                        if _viral_raw_base.exists():
                            self._viral_raw_base_path = str(_viral_raw_base)
                            logger.info("[%s] Saved viral raw base for metadata phase: %s", self.canal, self._viral_raw_base_path)
                else:
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
                import traceback
                logger.debug(traceback.format_exc())
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
            source_url = script.get("_viral_original_video_url", "") if self.source_mode == "viral" else ""

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
                    source_url=source_url,
                    source_mode=self.source_mode,
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
                    source_url=source_url,
                    source_mode=self.source_mode,
                )

            # ── Flush cross-video dedup history (v9) ──────────
            try:
                self.media_fetcher.flush_asset_history(self.db, video_id)
            except Exception as exc:
                logger.debug("[%s] flush_asset_history failed (non-critical): %s",
                             self.canal, exc)

            return {
                "video_path": str(video_path),
                "thumbnail_path": str(thumbnail_path),
                "thumbnail_base_path": str(
                    # Viral mode: use saved raw base from clone_thumbnail
                    # Non-viral mode: use raw Pollo image from thumbnail_maker
                    getattr(self, '_viral_raw_base_path', None) or
                    getattr(self.thumbnail_maker, '_last_raw_base', None) or 
                    str(thumbnail_path)
                ),
                "titulo": titulo_selected,
                "video_id": video_id,
            }

        except Exception as e:
            logger.error(f"[{self.canal}] Video assembly failed: {e}")
            _safe_log_error(self.db, self.canal, "video", str(e))
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

    def _rebuild_video_data_from_db(self, fallback: dict | None = None) -> dict | None:
        """Reconstruct video_data from the DB when the checkpoint lacks 'video'.

        upload_only workers skip the render phase, so video_data normally comes
        from checkpoint_data['video']. If that entry is missing (legacy videos
        rendered before the checkpoint was introduced), rebuild it from the
        videos table columns so the upload does not crash with a NoneType.

        Returns a dict with video_path/thumbnail_path/titulo, or None if the
        video record cannot be found.
        """
        if self.db is None or self.db_video_id is None:
            return None
        try:
            row = self.db.get_video(self.db_video_id)
            if not row:
                return None
            vp = row.get("video_path") or ""
            if not vp or not __import__("pathlib").Path(vp).exists():
                return None
            rebuilt = {
                "video_path": str(vp),
                "thumbnail_path": str(row.get("thumbnail_path") or ""),
                "titulo": str(row.get("titulo_final") or ""),
            }
            if fallback and isinstance(fallback, dict):
                for k in ("thumbnail_path", "titulo"):
                    if not rebuilt.get(k) and fallback.get(k):
                        rebuilt[k] = fallback[k]
            logger.info(
                "[%s] Reconstruido video_data desde DB para video #%s (path=%s)",
                self.canal, self.db_video_id, rebuilt["video_path"],
            )
            return rebuilt
        except Exception as exc:
            logger.warning("[%s] _rebuild_video_data_from_db failed: %s", self.canal, exc)
            return None

    # ═══════════════════════════════════════════════════════════════════
    # Phase 1.5: Pre-validation (after script, before TTS)
    # ═══════════════════════════════════════════════════════════════════

    def phase_pre_validate(self, script: dict) -> 'PreValidationResult':
        """Early gate: sanity-checks before investing compute in TTS/media/render.

        BLOCKING: empty title, empty script body → raises RuntimeError.
        WARNING: duration estimate outside config range → logs warning.

        Raises:
            RuntimeError: If a blocking check fails. The caller should
                abort the pipeline without wasting further resources.
        """
        from pipeline.video_validator import VideoValidator

        validator = VideoValidator(self.config)
        result = validator.pre_validate(script)

        # Log each check
        for check in result.checks:
            level = logging.WARNING if not check.passed else logging.INFO
            logger.log(
                level,
                "[%s] Pre-validate [%s]: %s %s",
                self.canal,
                check.name,
                "✓" if check.passed else "✗",
                check.message,
            )

        # Log summary
        if result.warnings:
            logger.warning(
                "[%s] Pre-validate WARNINGS (%d): %s",
                self.canal,
                len(result.warnings),
                "; ".join(result.warnings),
            )

        if not result.passed:
            error_report = "\n".join(f"  - {e}" for e in result.blocking_errors)
            logger.error(
                "[%s] PRE-VALIDATION FAILED — aborting before TTS/render:\n%s",
                self.canal,
                error_report,
            )
            raise RuntimeError(
                f"Pre-validation failed for {self.canal}: {result.blocking_errors[0]}"
            )

        logger.info(
            "[%s] Pre-validation PASSED (%d checks)",
            self.canal,
            len(result.checks),
        )
        return result

    # ═══════════════════════════════════════════════════════════════════
    # Phase 5.5: Post-validation (after metadata, before upload)
    # ═══════════════════════════════════════════════════════════════════

    def phase_post_validate(
        self,
        video_data: dict,
        metadata: dict,
        script: dict = None,
    ) -> 'PostValidationResult':
        """Late gate: quality check before upload. Never discards.

        BLOCKING: file missing, file corrupt → raises RuntimeError.
        WARNING: duration out of range → logs warning (never blocks).
        AUTO-FIX: missing description/tags → LLM regenerate.
        BELT+SUSPENDERS: verify title power word guarantee.

        Args:
            video_data: Dict from phase_video (video_path, etc.)
            metadata: Dict from phase_metadata (selected_title, description, tags)
            script: Original script dict for LLM re-generation context

        Returns:
            PostValidationResult with checks and possibly updated metadata.

        Raises:
            RuntimeError: If a blocking check fails (file missing/corrupt).
                The video record is preserved for diagnosis — never deleted.
        """
        from pipeline.video_validator import VideoValidator, PostValidationResult

        video_path = video_data.get("video_path", "")
        title = metadata.get("selected_title", video_data.get("titulo", ""))
        description = metadata.get("description", "")
        tags = metadata.get("tags", [])

        validator = VideoValidator(self.config)
        result = validator.post_validate(
            video_path=video_path,
            title=title,
            description=description,
            tags=tags,
            metadata_gen=self.metadata_gen,
            script=script,
        )

        # Log each check
        for check in result.checks:
            if not check.passed and check.severity == "blocking":
                logger.error(
                    "[%s] Post-validate [%s] ✗ %s",
                    self.canal, check.name, check.message,
                )
            elif not check.passed:
                logger.warning(
                    "[%s] Post-validate [%s] ⚠ %s",
                    self.canal, check.name, check.message,
                )
            elif check.auto_fixed:
                logger.info(
                    "[%s] Post-validate [%s] ↻ %s",
                    self.canal, check.name, check.message,
                )
            else:
                logger.info(
                    "[%s] Post-validate [%s] ✓ %s",
                    self.canal, check.name, check.message,
                )

        # ── Apply auto-fix updates to metadata dict ────────────────
        if result.auto_fixes_applied:
            if result.updated_title and result.updated_title != title:
                metadata["selected_title"] = result.updated_title
            if result.updated_description and result.updated_description != description:
                metadata["description"] = result.updated_description
            if result.updated_tags and result.updated_tags != tags:
                metadata["tags"] = result.updated_tags
            logger.info(
                "[%s] Post-validate auto-fixes applied: %s",
                self.canal, ", ".join(result.auto_fixes_applied),
            )

        # ── Handle blocking errors ─────────────────────────────────
        if not result.passed:
            error_report = "\n".join(f"  - {e}" for e in result.blocking_errors)
            logger.error(
                "[%s] POST-VALIDATION FAILED — blocking upload:\n%s\n"
                "Video record preserved for diagnosis.",
                self.canal,
                error_report,
            )
            raise RuntimeError(
                f"Post-validation blocked upload for {self.canal}: "
                f"{result.blocking_errors[0]}"
            )

        # ── Log warnings ───────────────────────────────────────────
        if result.warnings:
            logger.warning(
                "[%s] Post-validate WARNINGS (%d) — uploading anyway, "
                "monitor should investigate:\n  • %s",
                self.canal,
                len(result.warnings),
                "\n  • ".join(result.warnings),
            )

        logger.info(
            "[%s] Post-validation PASSED (%d checks, %d auto-fixes, %d warnings)",
            self.canal,
            len(result.checks),
            len(result.auto_fixes_applied),
            len(result.warnings),
        )
        return result

    def phase_metadata(self, script: dict, video_data: dict,
                        source_content: dict = None) -> Optional[dict]:
        """Generate SEO metadata via AI and regenerate thumbnail with overlay text.

        When source_mode='viral', uses cloned metadata from the viral source
        instead of generating new metadata with AI.
        
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
            if self.source_mode == "viral" and script.get("_viral_meta_json"):
                # Viral mode: use cloned metadata instead of AI generation
                logger.info(f"[{self.canal}] Phase 5a: Building viral metadata from clone...")
                from pipeline.viral_cloner import build_viral_metadata
                metadata = build_viral_metadata(
                    viral_meta_json=script["_viral_meta_json"],
                    channel_slug=self.canal,
                )
                if metadata:
                    logger.info(
                        f"[{self.canal}] Viral metadata: title='{metadata['selected_title'][:60]}', "
                        f"{len(metadata['tags'])} tags"
                    )
                else:
                    logger.warning("[%s] Viral metadata build returned empty — using fallback", self.canal)
                    metadata = self.metadata_gen._fallback_metadata(script)
            else:
                # 1. Generate AI-powered metadata (original mode)
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
                    kw_raw = (script.get("keywords") or script.get("keywords_json", "[]")) if script else "[]"
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
            _safe_log_error(self.db, self.canal, "metadata", str(e))
            
            # Fallback: return basic metadata so pipeline can continue
            return self.metadata_gen._fallback_metadata(script)

    def phase_upload(self, script: dict, video_data: dict,
                      metadata: dict = None, job_id: int = None,
                      planned_public_at: str = None,
                      skip_lifecycle_scheduling: bool = False) -> Optional[str]:
        """Upload video to YouTube. Returns video_id or None.
        
        Args:
            script: Script dict with content info
            video_data: Video data dict with paths
            metadata: Optional SEO metadata dict from phase_metadata().
                      If provided, uses AI-optimized title/description/tags.
                      If None, falls back to config templates (backward compat).
            job_id: Optional generation_jobs.id. If provided, heartbeats are
                    emitted during upload to prevent false orphan detection.
            planned_public_at: Optional ISO8601 string from planning system.
                      If provided and publish_mode=scheduled, overrides the
                      calculated target_public_at to align with the planning.
            skip_lifecycle_scheduling: If True, skip scheduling lifecycle
                      actions (caller handles it separately, e.g. worker).
        """
        start = time.time()
        self._emit_progress(80, "upload", "Preparando subida a YouTube...")

        _saved_uploader_db = None  # for finally restore

        from pipeline.youtube_uploader import (
            QuotaExhaustedError,
            UploadAdmissionDeniedError,
        )

        try:
            # ── Robustez upload_only: reconstruir video_data si el checkpoint
            # no persistió la entrada 'video' (vídeos renderizados por una
            # versión anterior del pipeline). Sin esto, video_data=None crashea
            # con 'NoneType' object has no attribute 'get' en la subida.
            if not video_data or not isinstance(video_data, dict):
                video_data = self._rebuild_video_data_from_db()
            elif not video_data.get("video_path"):
                video_data = self._rebuild_video_data_from_db(video_data)
            if not video_data or not video_data.get("video_path"):
                logger.error(
                    "[%s] No se pudo reconstruir video_data para upload (db_video_id=%s) — abortando",
                    self.canal, self.db_video_id,
                )
                return None

            # Authenticate with YouTube
            self._emit_progress(83, "upload", "Autenticando con YouTube...")
            if not self.uploader.authenticate():
                logger.error(f"[{self.canal}] YouTube authentication failed")
                _safe_log_error(self.db, self.canal, "upload", "Auth failed")
                return None

            # ── Scheduled publishing: check if channel uses scheduled mode ──
            publish_mode = getattr(self.config, "PUBLISH_MODE", "immediate")
            publish_schedule_info = None
            
            if publish_mode == "scheduled":
                from pipeline.publish_scheduler import calculate_target_public_time, planned_target_is_off_peak
                primary_kw = getattr(self.config, "SEO_PRIMARY_KEYWORD", "")
                secondary_kws = getattr(self.config, "SEO_SECONDARY_KEYWORDS", [])
                tz = getattr(self.config, "PUBLISH_TIMEZONE", "Europe/Madrid")
                target_h = getattr(self.config, "PUBLISH_TARGET_HOUR", None)
                warmup = getattr(self.config, "PUBLISH_WARMUP_MIN", 60)
                spread_min = getattr(self.config, "PUBLISH_WINDOW_SPREAD_MIN", 90)
                channel_id = self._get_channel_id()

                # ── If the planning system provided a target, use it ──
                if planned_public_at:
                    from datetime import datetime as _dt, timezone as _tz
                    target_dt = _dt.fromisoformat(planned_public_at.replace("Z", "+00:00"))
                    if target_dt.tzinfo is None:
                        import pytz
                        local_tz = pytz.timezone(tz)
                        target_dt = local_tz.localize(target_dt).astimezone(_tz.utc)

                    now_utc_dt = _dt.now(_tz.utc)

                    # Recalculate if planned time has already passed OR lands on an
                    # off-peak hour (heuristic seed vs data-driven optimal slots).
                    off_peak = planned_target_is_off_peak(
                        planned_public_at, channel_id, self.db, tz,
                    )
                    if (target_dt < now_utc_dt + timedelta(minutes=warmup)) or off_peak:
                        if off_peak and target_dt >= now_utc_dt + timedelta(minutes=warmup):
                            logger.info(
                                "[%s] Planned public time %s is off-peak — recalculating to optimal slot.",
                                self.canal, target_dt.isoformat(),
                            )
                        else:
                            logger.info(
                                "[%s] Planned public time within warmup or past (%s). Recalculating.",
                                self.canal, target_dt.isoformat(),
                            )
                        try:
                            fallback_info = calculate_target_public_time(
                                slug=self.canal,
                                primary_keyword=primary_kw,
                                secondary_keywords=secondary_kws,
                                timezone_str=tz,
                                target_hour=target_h,
                                jitter_min=0,
                                warmup_min=warmup,
                                publish_window_spread_min=spread_min,
                                db=self.db,
                                channel_id=channel_id,
                            )
                            target_dt = _dt.fromisoformat(
                                fallback_info["target_public_at"].replace("Z", "+00:00")
                            )
                            if target_dt.tzinfo is None:
                                target_dt = target_dt.replace(tzinfo=_tz.utc)
                            # Persist recalculated target
                            try:
                                if self.db and self.db_video_id:
                                    self.db.update_video(
                                        self.db_video_id,
                                        target_public_at=target_dt.isoformat(),
                                    )
                            except Exception:
                                pass
                        except Exception:
                            target_dt = now_utc_dt + timedelta(minutes=warmup)

                    publish_schedule_info = {
                        "target_public_at": target_dt.isoformat(),
                        "peak_hour_local": target_h or 0,
                        "peak_source": "planning",
                        "niche": "planning",
                        "jitter_applied": 0,
                        "warmup_min": warmup,
                    }
                    logger.info("[%s] Using planned public time: %s",
                                self.canal, target_dt.isoformat())
                else:
                    publish_schedule_info = calculate_target_public_time(
                        slug=self.canal,
                        primary_keyword=primary_kw,
                        secondary_keywords=secondary_kws,
                        timezone_str=tz,
                        target_hour=target_h,
                        jitter_min=0,
                        jitter_after=0,
                        warmup_min=warmup,
                        publish_window_spread_min=spread_min,
                        db=self.db,
                        channel_id=channel_id,
                    )

                target_utc = publish_schedule_info["target_public_at"]

                # ── Upload as private with scheduledPublishTime ──
                # YouTube will auto-publish at the target time. No go_public needed.
                upload_privacy = "private"

                # ── Format local time for logging ──
                local_info = ""
                try:
                    import pytz
                    from datetime import datetime as _dt2, timezone as _tz2
                    local_tz = pytz.timezone(tz)
                    target_local = _dt2.fromisoformat(target_utc.replace("Z", "+00:00")).astimezone(local_tz)
                    local_info = f" ({target_local.strftime('%d/%m %H:%M')} {tz})"
                except Exception:
                    pass

                logger.info(
                    "[%s] 📤 SUBIDA PROGRAMADA | público: %s%s | "
                    "peak=%d (src=%s) | warmup=%dmin",
                    self.canal,
                    target_utc[:19], local_info,
                    publish_schedule_info["peak_hour_local"],
                    publish_schedule_info["peak_source"],
                    warmup,
                )
                # ── Audit log ──
                try:
                    from api.services.scheduled_publish_logger import log_publish_event
                    from datetime import datetime as _dt3, timezone as _tz3
                    uploaded_at = _dt3.now(_tz3.utc).strftime("%Y-%m-%d %H:%M:%S")
                    scheduled_for_local = ""
                    try:
                        import pytz as _pytz
                        local_tz2 = _pytz.timezone(tz)
                        target_dt = _dt3.fromisoformat(target_utc.replace("Z", "+00:00"))
                        if target_dt.tzinfo is None:
                            target_dt = target_dt.replace(tzinfo=_tz3.utc)
                        local_dt = target_dt.astimezone(local_tz2)
                        scheduled_for_local = local_dt.strftime("%d/%m %H:%M") + f" {tz}"
                    except Exception:
                        pass
                    video_title = video_data.get("titulo", "?") if video_data else "?"
                    log_publish_event(
                        event="uploaded_scheduled",
                        slug=self.canal,
                        video_title=video_title[:80],
                        yt_video_id="(pending)",
                        db_video_id=self.db_video_id,
                        uploaded_at=uploaded_at,
                        scheduled_for_utc=target_utc,
                        scheduled_for_local=scheduled_for_local,
                        peak_hour=publish_schedule_info["peak_hour_local"],
                        peak_source=publish_schedule_info["peak_source"],
                        warmup_min=warmup,
                    )
                except Exception:
                    pass
            else:
                upload_privacy = self.config.YT_PRIVACY_STATUS

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
                # ── Never upload with an empty title (ago 2026) ──
                # An empty title made slugify_filename("") → "video", so every
                # empty-titled video collided on the same quota reference_id
                # and one of them got a false "quota agotada" breaker trip.
                # Fall back to the script title / first title option.
                title = (
                    video_data.get("titulo")
                    or (script.get("titulo_selected") if script else None)
                    or (script.get("titulo") if script else None)
                    or ((script.get("titulo_options") or [None])[0] if script else None)
                    or "Video sin título"
                )
                
                kw_raw = script.get("keywords") or script.get("keywords_json", "[]")
                if isinstance(kw_raw, str):
                    tags = _json.loads(kw_raw)
                else:
                    tags = kw_raw or []
                
                seo_desc = script.get("descripcion_seo", "") if script else ""
                chapters_raw = script.get("chapters", []) if script else []
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

            # ── SEO filename slug from final title ─────────────────
            # YouTube uses the uploaded file name as a ranking signal.
            # We construct a keyword-rich slug so the temp copy sent to
            # YouTube carries the video title in its filename.
            from pipeline.utils import slugify_filename
            filename_slug = slugify_filename(title)
            logger.info("[%s] SEO filename slug: '%s' (from '%s')", self.canal, filename_slug, title[:60])

            # Upload — API mode: suppress uploader's own _log_to_db (single video record)
            _saved_uploader_db = self._uploader.db

            if self.db_video_id is not None:
                self._uploader.db = None

            self._emit_progress(88, "upload", "Subiendo video a YouTube...")
            
            # ── Granular upload progress (per chunk) ───────────────
            # The uploader reports pct (0-100) + bytes per chunk. Map into the
            # 88→100 slot of the phase and attach bytes so the frontend can
            # render real MB, speed and ETA.
            def _upload_pct_cb(pct: int):
                self._emit_progress(
                    min(99, 88 + int(pct * 0.12)), "upload",
                    f"Subiendo... {pct}%",
                )

            def _upload_detail_cb(info: dict):
                pct = info.get("pct") or 0
                self._emit_progress(
                    min(99, 88 + int(pct * 0.12)), "upload",
                    f"Subiendo... {pct}%",
                    current=info.get("bytes_done"),
                    total=info.get("bytes_total"),
                    sub_phase="chunk",
                )

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
            
            # ── Build publish_at for scheduled publishing ──
            upload_publish_at = None
            if publish_mode == "scheduled" and publish_schedule_info:
                upload_publish_at = publish_schedule_info["target_public_at"]

            result = self.uploader.upload(
                video_path=Path(video_data["video_path"]),
                title=title,
                description=description,
                tags=tags,
                thumbnail_path=Path(video_data["thumbnail_path"]),
                category_id=metadata.get("category_id", self.config.YT_CATEGORY_ID) if metadata else self.config.YT_CATEGORY_ID,
                privacy=upload_privacy,
                heartbeat_callback=upload_heartbeat,
                progress_callback=_upload_pct_cb,
                progress_callback_detail=_upload_detail_cb,
                suggested_video_filename=filename_slug,
                suggested_thumb_filename=filename_slug,
                publish_at=upload_publish_at,
                # ── Stable quota reference (ago 2026) ──
                # Prefer the DB video id so two videos never share the same
                # reservation reference even if they map to the same renamed
                # temp file (empty-title "video.mp4" collision → false quota).
                quota_reference_id=(
                    f"upload:{self.canal}:db:{self.db_video_id}"
                    if self.db_video_id is not None else None
                ),
            )

            video_id = result.get("video_id")
            url = result.get("url", "")

            # ── Determine upload status based on publish mode ──
            if publish_mode == "scheduled":
                upload_status = "uploaded_private"
            else:
                upload_status = "uploaded"

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
                    update_kwargs = dict(
                        titulo_final=title,
                        description=description,
                        tags_json=tags_json_str,
                        title_options=titles_json_str,
                        privacy_status=upload_privacy,
                        channel_id=channel_id,
                        publish_mode=publish_mode,
                    )
                    if publish_schedule_info:
                        update_kwargs["target_public_at"] = publish_schedule_info["target_public_at"]
                        update_kwargs["peak_source"] = publish_schedule_info["peak_source"]
                    self.db.update_video(self.db_video_id, **update_kwargs)
                    # Note: mark_video_uploaded is called by the API layer (generation_service)
                    # to ensure the tracked record gets yt_video_id/yt_url
                    db_video_id = self.db_video_id  # for stats + lifecycle below
                else:
                    # CLI standalone mode: insert + mark
                    db_video_id = self.db.insert_video(
                        script_id=script.get("id") if script else None,
                        canal=self.canal,
                        video_path=video_data["video_path"],
                        thumbnail_path=video_data["thumbnail_path"],
                        audio_path=video_data.get("audio_path", ""),
                        titulo_final=title,
                        privacy_status=upload_privacy,
                        channel_id=channel_id,
                        description=description,
                        tags_json=tags_json_str,
                        title_options=titles_json_str,
                    )
                    if db_video_id:
                        self.db.mark_video_uploaded(db_video_id, video_id, url, status=upload_status)
                        # ── Store scheduled publishing info ──
                        if publish_schedule_info:
                            self.db.update_video(
                                db_video_id,
                                publish_mode=publish_mode,
                                target_public_at=publish_schedule_info["target_public_at"],
                                peak_source=publish_schedule_info["peak_source"],
                            )

            duration_ms = int((time.time() - start) * 1000)
            self._timing["phases"]["upload"] = duration_ms
            self.db.log_pipeline(self.canal, "upload", "success",
                                  f"YouTube ID: {video_id}",
                                  content_id=script.get("id") if script else None,
                                  duration_ms=duration_ms)

            # ── Post-upload: schedule processing health checks ──
            # Monitors YouTube's processingStatus at 5min, 30min, 2h to detect
            # encoding failures that happen after the upload verification succeeds.
            try:
                db_vid = db_video_id or self.db_video_id
                if db_vid:
                    from api.services.upload_health_checker import schedule_checks
                    schedule_checks(db_vid, video_id, self.canal, self.db)
                    logger.info(f"[{self.canal}] Health checks scheduled for video #{db_vid}")
            except Exception as hc_exc:
                logger.warning(f"[{self.canal}] Failed to schedule health checks: {hc_exc}")

            # ── Post-upload: real YouTube stats snapshot ──
            try:
                from pipeline.youtube_stats import YouTubeStatsFetcher
                fetcher = YouTubeStatsFetcher(self.canal)
                if fetcher.authenticate():
                    real_stats = fetcher.get_video_stats(video_id)
                    if real_stats and not real_stats.get("is_mock"):
                        self.db.insert_video_stats(
                            video_id=db_video_id or self.db_video_id,
                            yt_video_id=video_id,
                            stats=real_stats,
                        )
                        logger.info(f"[{self.canal}] Real stats collected for video {video_id}: {real_stats.get('viewCount', '?')} views, {real_stats.get('likeCount', '?')} likes")
                    else:
                        # Fallback: baseline si la API no devuelve datos reales
                        self.db.insert_video_stats(
                            video_id=db_video_id or self.db_video_id,
                            yt_video_id=video_id,
                            stats={"viewCount": 0, "likeCount": 0, "commentCount": 0},
                        )
                        logger.info(f"[{self.canal}] Baseline stats saved for video {video_id} (API returned mock/no data)")
                else:
                    # Auth failed: store baseline
                    self.db.insert_video_stats(
                        video_id=db_video_id or self.db_video_id,
                        yt_video_id=video_id,
                        stats={"viewCount": 0, "likeCount": 0, "commentCount": 0},
                    )
                    logger.warning(f"[{self.canal}] Auth failed for stats fetch, saved baseline for video {video_id}")
            except Exception as stats_exc:
                logger.warning(f"[{self.canal}] Failed to collect post-upload stats: {stats_exc}")
                # Fallback: baseline
                try:
                    self.db.insert_video_stats(
                        video_id=db_video_id or self.db_video_id,
                        yt_video_id=video_id,
                        stats={"viewCount": 0, "likeCount": 0, "commentCount": 0},
                    )
                except Exception:
                    pass

            # ── Post-upload: schedule lifecycle promotion actions ──
            # NOTE: In scheduled mode with publishAt, YouTube auto-publishes; 
            # no go_public lifecycle action is needed.
            channel_id_for_lifecycle = None  # init outside try — also used by playlist code below
            if not skip_lifecycle_scheduling:
                try:
                    from pipeline.video_lifecycle import VideoLifecycleManager
                    lifecycle = VideoLifecycleManager(self.canal)
                    script_text = script.get("guion", "") if script else ""
                    db_vid_for_lifecycle = db_video_id or self.db_video_id
                    channel_id_for_lifecycle = self._get_channel_id()
                    
                    if publish_mode == "scheduled" and publish_schedule_info:
                        # Scheduled mode via publishAt: schedule comments/social/CTR
                        # relative to target_public_at. No go_public needed.
                        lifecycle.on_video_uploaded_scheduled(
                            db_video_id=db_vid_for_lifecycle,
                            yt_video_id=video_id,
                            channel_id=channel_id_for_lifecycle,
                            script_text=script_text,
                            target_public_at=publish_schedule_info["target_public_at"],
                        )
                        logger.info(f"[{self.canal}] Scheduled lifecycle actions for video {video_id} "
                                   f"(YouTube auto-publish at: {publish_schedule_info['target_public_at']})")
                    else:
                        # Immediate mode: standard lifecycle from upload time
                        lifecycle.on_video_published(
                            db_video_id=db_vid_for_lifecycle,
                            yt_video_id=video_id,
                            channel_id=channel_id_for_lifecycle,
                            script_text=script_text,
                        )
                        logger.info(f"[{self.canal}] Lifecycle actions scheduled for video {video_id}")
                except Exception as lifecycle_exc:
                    logger.warning(f"[{self.canal}] Lifecycle scheduling failed (non-critical): {lifecycle_exc}")
            else:
                logger.debug("[%s] Lifecycle scheduling skipped (caller handles it)", self.canal)

            # ── Fallback: get channel id if lifecycle block didn't set it ──
            if channel_id_for_lifecycle is None:
                try:
                    channel_id_for_lifecycle = self._get_channel_id()
                except Exception:
                    pass

            # ── Post-upload: add video to the selected playlist ──
            if channel_id_for_lifecycle is None:
                logger.warning("[%s] Skipping playlist assignment — channel_id not available", self.canal)
            else:
                    try:
                        db_vid = db_video_id or self.db_video_id
                        if db_vid:
                            vid_record = self.db.get_video(db_vid)
                            tgt_slug = vid_record.get("target_playlist_slug") if vid_record else None
                            tgt_playlist_db_id = vid_record.get("target_playlist_id") if vid_record else None
                            if tgt_slug and video_id:
                                from pipeline.youtube_playlists import YouTubePlaylistManager
                                pl_mgr = YouTubePlaylistManager(self.canal)
                                pl_mgr.authenticate()
                                result = pl_mgr.add_video_to_playlist_by_slug(
                                    video_id, tgt_slug,
                                    channel_id=channel_id_for_lifecycle
                                )
                                if result.get("was_already_present"):
                                    logger.info("[%s] Video %s already in playlist '%s'",
                                               self.canal, video_id, tgt_slug)
                                elif result.get("yt_playlist_item_id"):
                                    logger.info("[%s] Added video %s to playlist '%s' (item: %s)",
                                               self.canal, video_id, tgt_slug,
                                               result["yt_playlist_item_id"])
                                    # ── Record assignment in DB immediately ──
                                    if tgt_playlist_db_id:
                                        self.db.add_video_to_playlist_db(
                                            db_vid, tgt_playlist_db_id,
                                            yt_playlist_item_id=result["yt_playlist_item_id"],
                                        )
                                        logger.info("[%s] ✅ DB recorded: video %d → playlist %d (slug='%s')",
                                                   self.canal, db_vid, tgt_playlist_db_id, tgt_slug)
                                    else:
                                        # Lookup playlist DB id from slug
                                        from database.db_extended import ExtendedDatabase
                                        ext_db2 = ExtendedDatabase()
                                        pl_cached = ext_db2.get_playlist_by_slug(channel_id_for_lifecycle, tgt_slug)
                                        if pl_cached:
                                            self.db.add_video_to_playlist_db(
                                                db_vid, pl_cached["id"],
                                                yt_playlist_item_id=result["yt_playlist_item_id"],
                                            )
                                            logger.info("[%s] ✅ DB recorded (slug lookup): video %d → playlist '%s'",
                                                       self.canal, db_vid, tgt_slug)
                                else:
                                    logger.warning("[%s] Could not add video %s to playlist '%s': %s",
                                                 self.canal, video_id, tgt_slug, result.get("error", "unknown"))
                    except Exception as pl_exc:
                        logger.warning("[%s] Playlist assignment failed (non-critical): %s",
                                     self.canal, pl_exc)

            return video_id

        except QuotaExhaustedError:
            # ── Quota exhaustion: record exhaustion timestamp; generation continues ──
            logger.error(f"[{self.canal}] Quota agotada — subidas pausadas (generación sigue activa)")
            try:
                from database.db_extended import ExtendedDatabase
                _qdb = ExtendedDatabase()
                _qdb.set_quota_exhausted(channel_slug=self.canal)
            except Exception:
                pass
            try:
                from api.services.lifecycle_monitor import create_alert
                from database.db_extended import ExtendedDatabase as _E
                _adb = _E()
                create_alert(_adb,
                             entity_type='system', entity_id=None, channel_id=None,
                             alert_type='quota_exhausted', severity='critical',
                             title='YouTube API quota agotada',
                             message=f'Cuota agotada durante subida del canal {self.canal}. '
                                     'Subidas pausadas. Generación sigue activa. '
                                     'Recuperación automática al reset de medianoche (PT).',
                             metadata={'channel': self.canal})
            except Exception:
                pass
            return None

        except UploadAdmissionDeniedError as exc:
            # ── Local admission denial (reference collision, budget, unknown
            # project...). NOT YouTube quota: retryable, but must NOT trip the
            # per-project quota breaker nor create a "quota agotada" alert. ──
            self._upload_admission_denied = True
            logger.warning(
                "[%s] Upload admission denied locally (retryable, no quota impact): %s",
                self.canal, exc,
            )
            _safe_log_error(self.db, self.canal, "upload", f"admission_denied: {exc}")
            return None

        except Exception as e:
            logger.error(f"[{self.canal}] Upload failed: {e}")
            _safe_log_error(self.db, self.canal, "upload", str(e))
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
        logger.info(f"[{self.canal}] STARTING FULL PIPELINE (mode: {self.source_mode})")
        if self.source_mode == "viral" and self.viral_candidate_id:
            logger.info(f"[{self.canal}] Viral candidate ID: {self.viral_candidate_id}")
        logger.info(f"{'='*60}")

        # Reset per-run state
        self._theme_context = None

        # ── Disk cleanup before pipeline (lock-aware: never delete files owned by active jobs) ──
        import time as _time
        try:
            locked_paths = self.db.get_locked_file_paths()
        except Exception:
            locked_paths = set()
        try:
            error_paths = self.db.get_error_video_media_paths(max_age_hours=48)
        except Exception:
            error_paths = set()
        preserved_paths = locked_paths | error_paths
        _cleanup_dirs = [
            Path("output/video_clips"),
            Path("output/temp"),
        ]
        for _d in _cleanup_dirs:
            if _d.exists():
                _deleted = 0
                for _f in _d.iterdir():
                    if not _f.is_file():
                        continue
                    _rel = str(_f)
                    # Never delete files locked by another running/queued job,
                    # nor clips referenced by a recent error-state video awaiting reassembly.
                    if _rel in preserved_paths:
                        continue
                    try:
                        _f.unlink()
                        _deleted += 1
                    except OSError:
                        pass
                logger.info("[%s] Cleaned %d stale files from %s (%d locked+error files preserved)",
                            self.canal, _deleted, _d, len(preserved_paths))

        # ── Playlist selection: pick a random playlist BEFORE scraping ──
        target_playlist = getattr(self, '_target_playlist', None)
        target_playlist_kw = getattr(self, '_target_playlist_kw', [])
        if target_playlist is None:
            try:
                from database.db_extended import ExtendedDatabase
                ext_db = ExtendedDatabase()
                channel_id = self._get_channel_id()
                playlists = ext_db.get_channel_youtube_playlists(channel_id) if channel_id else []
                if playlists:
                    target_playlist = random.choice(playlists)
                    # Load full playlist config for keywords
                    config_json = {}
                    ch = ext_db.get_channel(channel_id)
                    if ch:
                        cj = ch.get("config_json", "{}")
                        if isinstance(cj, str):
                            import json; config_json = json.loads(cj) if cj else {}
                        else:
                            config_json = cj or {}
                    generated = config_json.get("PLAYLISTS_GENERATED", [])
                    for pl_cfg in generated:
                        if pl_cfg.get("slug") == target_playlist.get("slug"):
                            target_playlist_kw = pl_cfg.get("keywords_en", [])
                            break
                    # Store on video record
                    if self.db_video_id is not None:
                        self.db.update_video(self.db_video_id,
                                             target_playlist_id=target_playlist["id"],
                                             target_playlist_slug=target_playlist["slug"])
                    logger.info("[%s] 🎯 Target playlist: '%s' (slug=%s, %d keywords)",
                               self.canal, target_playlist.get("name"),
                               target_playlist.get("slug"), len(target_playlist_kw))
                else:
                    logger.warning("[%s] No playlists in DB — playlist selection skipped", self.canal)
            except Exception as e:
                logger.warning("[%s] Playlist selection failed (non-critical): %s", self.canal, e)
        else:
            logger.info("[%s] 🎯 Using pre-selected playlist: '%s' (slug=%s, %d keywords)",
                       self.canal, target_playlist.get("name"),
                       target_playlist.get("slug"), len(target_playlist_kw))

        # Phase 0: Scrape fresh content for this video
        logger.info(f"[{self.canal}] Phase 0/6: Scraping fresh content...")

        # ── Viral mode: use selected playlist keywords for queries ──
        if self.source_mode == "viral" and getattr(self.config, "VIRAL_ENABLED", False):
            try:
                from pipeline.viral_query_builder import build_viral_queries

                if target_playlist:
                    pl_name = target_playlist.get("name", "")
                    pl_keywords = target_playlist_kw
                    logger.info("[%s] Viral query builder — playlist='%s' (pre-selected)",
                               self.canal, pl_name)

                    queries = build_viral_queries(
                        channel_slug=self.canal,
                        channel_name=getattr(self.config, "CANAL_DISPLAY_NAME", self.canal),
                        channel_theme=getattr(self.config, "CANAL_TAGLINE", ""),
                        playlist_name=pl_name,
                        playlist_description="",
                        canal_keywords_eng=getattr(self.config, "NICHE_KEYWORDS_ENG", []),
                        playlist_keywords=pl_keywords,
                        db=self.db,
                        config=self.config,
                    )

                    # Inject queries into the youtube_viral scraper
                    if queries and "youtube_viral" in self.scraper:
                        self.scraper["youtube_viral"]._suggested_queries = queries
                        logger.info("[%s] Injected %d playlist-driven queries into viral scraper",
                                    self.canal, len(queries))
                    else:
                        logger.warning("[%s] Query builder returned no queries — scraper will use defaults", self.canal)
                else:
                    logger.info("[%s] No playlist found — viral scraper will use default keywords", self.canal)
            except Exception as e:
                logger.warning("[%s] Viral query builder failed (non-fatal): %s — using default keywords", self.canal, e)

        self.phase_scrape()

        # Phase 1: Generate script from best scraped item
        logger.info(f"[{self.canal}] Phase 1/6: Generating script...")
        script = self.phase_generate_script()
        if not script:
            logger.error(f"[{self.canal}] PIPELINE ABORTED: No script generated from scraped content")
            return False
        logger.info(f"[{self.canal}] Script ready (ID: {script.get('id')})")

        # Phase 1.5: Pre-validation (early gate — saves compute if script is broken)
        logger.info(f"[{self.canal}] Phase 1.5/6: Pre-validating script quality...")
        try:
            self.phase_pre_validate(script)
        except RuntimeError as ve:
            logger.error(f"[{self.canal}] PIPELINE ABORTED: Pre-validation failed: {ve}")
            return False

        # Phase 2: TTS
        logger.info(f"[{self.canal}] Phase 2/6: Generating TTS audio...")
        audio_data = self.phase_tts(script, job_id=job_id)
        if not audio_data:
            logger.error(f"[{self.canal}] PIPELINE ABORTED: TTS failed")
            return False
        logger.info(f"[{self.canal}] TTS audio: {audio_data['audio_path']}")

        # Phase 3: Media (video + image hybrid)
        logger.info(f"[{self.canal}] Phase 3/6: Fetching media assets (video + image)...")
        media_assets = self.phase_media(script, audio_data, job_id=job_id)
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

        # Phase 5.5: Post-validation (quality gate — never discards)
        logger.info(f"[{self.canal}] Phase 5.5/6: Post-validating video & metadata quality...")
        try:
            val_result = self.phase_post_validate(video_data, metadata, script)
            # Auto-fixes may have updated metadata in-place — log them
            if val_result.auto_fixes_applied:
                logger.info(
                    f"[{self.canal}] Auto-fixes applied: {', '.join(val_result.auto_fixes_applied)}"
                )
            if val_result.warnings:
                logger.warning(
                    f"[{self.canal}] Post-validate warnings (%d): see log above",
                    len(val_result.warnings),
                )
        except RuntimeError as ve:
            logger.error(f"[{self.canal}] PIPELINE ABORTED: Post-validation failed: {ve}")
            # Save video record with validation_failed status before returning
            vid_db = video_data.get("video_id")
            if vid_db:
                try:
                    self.db.update_video(
                        vid_db,
                        status="validation_failed",
                        progress_phase="post_validate",
                    )
                except Exception:
                    pass
            return False

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
            _safe_log_error(self.db, self.canal, "orchestrator", str(e))

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


def run_single(canal: str, skip_upload: bool = False):
    """Run a single pipeline execution for one channel."""
    setup_logging()
    orch = PipelineOrchestrator(canal=canal)
    success = orch.run_full_pipeline(skip_upload=skip_upload)
    return 0 if success else 1


def run_scheduled(canal: str):
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
