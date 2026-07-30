"""Shorts native pipeline: produces original short-form vertical videos.

Independent pipeline for creating native YouTube Shorts from scratch:
scrape → short script → TTS → media → vertical render → upload.

Reuses components from the main pipeline but with vertical-oriented config.
"""

import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


class NativeShortsPipeline:
    """Pipeline for producing original, native YouTube Shorts."""

    def __init__(self, channel_slug: str, db_path: str = None):
        from config.config_bridge import get_channel_config

        self.channel_slug = channel_slug
        self.config = get_channel_config(channel_slug)

        from config.settings import DATABASE_PATH
        from database.db_extended import ExtendedDatabase, migrate_v2
        from database.db import init_db

        db_path = db_path or str(DATABASE_PATH)
        init_db(db_path)
        migrate_v2(db_path)
        self.db = ExtendedDatabase(db_path)

        # Lazy-loaded components
        self._scraper = None
        self._script_gen = None
        self._tts = None
        self._media_fetcher = None
        self._renderer = None
        self._uploader = None
        self._theme_extractor = None  # v8: ThemeExtractor for visual coherence
        self._theme_context = None     # v8: cached ThemeContext per run

    def run(self) -> Optional[dict]:
        """Run the full native shorts pipeline. Returns short data dict on success."""
        logger.info(f"[{self.channel_slug}] Starting NATIVE SHORTS pipeline")

        try:
            # 1. Scrape content (share with main pipeline)
            short_content = self._phase_scrape_short()
            if not short_content:
                logger.warning(f"[{self.channel_slug}] No content for native short")
                return None

            # 1b. Extract visual theme context (v8) — run BEFORE script generation
            #     so the LLM can generate theme-aware search queries
            self._extract_theme_for_short(short_content)

            # 2. Generate short script (30-90 seconds)
            short_script = self._phase_script_short(short_content)
            if not short_script:
                logger.error(f"[{self.channel_slug}] Short script generation failed")
                return None

            # 3. TTS
            audio_data = self._phase_tts_short(short_script)
            if not audio_data:
                logger.error(f"[{self.channel_slug}] TTS for short failed")
                return None

            # 3b. Compute scene_ranges from TTS timestamps (v9 — sub-scene splitting)
            #     Splits blocks longer than SCENE_DURATION_MAX (10s) into sub-scenes
            #     so each sub-scene gets its own distinct visual asset.
            scene_ranges = None
            try:
                from pipeline.video_editor import VideoEditor
                editor = VideoEditor(self.config)
                bloques = short_script.get("bloques", [])
                timestamps = audio_data.get("timestamps", [])
                scene_ranges = editor._compute_block_ranges(bloques, timestamps)
                # Add 'duracion_sec' for fetch_short_assets_exhaustive compatibility
                for sr in scene_ranges:
                    sr["duracion_sec"] = sr.get("duration", 5.0)
                logger.info(
                    "[%s] Computed %d scene ranges from %d blocks (TTS=%.1fs)",
                    self.channel_slug, len(scene_ranges), len(bloques),
                    audio_data.get("duration_sec", 0),
                )
            except Exception as e:
                logger.warning(
                    "[%s] Scene range computation failed — falling back to raw blocks: %s",
                    self.channel_slug, e,
                )
                scene_ranges = None

            # 4. Media (vertical-oriented) — uses scene_ranges for sub-scene fetching
            media = self._phase_media_short(short_script, scene_ranges=scene_ranges)
            if not media:
                logger.error(f"[{self.channel_slug}] Media fetch for short failed")
                return None

            # 5. Vertical render — uses scene_ranges for variable segment durations
            video_path = self._phase_render_short(
                short_script, audio_data, media, scene_ranges=scene_ranges,
            )
            if not video_path:
                logger.error(f"[{self.channel_slug}] Short render failed")
                return None

            # 6. Upload
            result = self._phase_upload_short(short_script, video_path)
            return result

        except Exception as e:
            logger.error(f"[{self.channel_slug}] Native shorts pipeline failed: {e}")
            return None

    def _phase_scrape_short(self) -> Optional[dict]:
        """Get content for a short-form video.
        
        Uses LLM to generate a viral topic idea in the channel's niche.
        More reliable than scraping for short-form content.
        """
        items = self._generate_topic_idea()
        if not items:
            return None
        import random
        return random.choice(items)

    def _extract_theme_for_short(self, short_content: dict) -> None:
        """Extract visual theme context for the short (v8).

        Uses the same ThemeExtractor as the long-form pipeline to get
        genre, era, key_motifs, forbidden_elements, mood, lighting, etc.
        This enriches the script prompt and search queries with visual
        coherence data.
        """
        if self._theme_context is not None:
            return  # already extracted

        content_title = short_content.get("title", "")
        content_tema = short_content.get("tema", content_title)
        if not content_tema:
            return

        try:
            from pipeline.theme_extractor import ThemeExtractor
            if self._theme_extractor is None:
                self._theme_extractor = ThemeExtractor(config=self.config)

            channel_name = getattr(self.config, "CANAL_DISPLAY_NAME", self.channel_slug)
            channel_theme = getattr(self.config, "CANAL_TAGLINE", "")
            niche_keywords = getattr(self.config, "NICHE_KEYWORDS_ENG", None)

            self._theme_context = self._theme_extractor.extract(
                content_text=content_tema[:3000],
                channel_name=channel_name,
                channel_theme=channel_theme,
                niche_keywords=niche_keywords,
            )
            if self._theme_context and self._theme_context.theme_keywords_en:
                logger.info(
                    "[%s] Shorts theme extracted: genre=%s era=%s keywords=%s",
                    self.channel_slug,
                    self._theme_context.genre,
                    self._theme_context.era,
                    self._theme_context.theme_keywords_en,
                )
            else:
                logger.warning(
                    "[%s] Shorts theme extraction returned empty — using generic fallback",
                    self.channel_slug,
                )
                self._theme_context = None
        except Exception as exc:
            logger.warning(
                "[%s] Shorts theme extraction failed (non-fatal): %s",
                self.channel_slug, exc,
            )
            self._theme_context = None

    def _generate_topic_idea(self) -> list[dict]:
        """Use LLM to generate a viral short-form topic in the channel's niche.

        Fetches recently published native short topics and instructs the LLM to avoid them.
        """
        try:
            from config.llm_client import create_llm_client
            from config.settings import LLM_MODEL

            niche = getattr(self.config, "CANAL_NARRATIVE_STYLE", "documental")
            display_name = getattr(self.config, "CANAL_DISPLAY_NAME", self.channel_slug)
            tagline = getattr(self.config, "CANAL_TAGLINE", "")

            # Fetch recent topics to avoid repetition
            channel_id = self.db.get_channel_by_slug(self.channel_slug)
            channel_id = channel_id.get("id") if channel_id else None
            topic_warning = ""
            if channel_id:
                recent_topics = self.db.get_recent_short_topics(channel_id, limit=15)
                if recent_topics:
                    topic_list = "\n".join(f'  - "{t}"' for t in recent_topics)
                    topic_warning = (
                        f"\n\n⚠️ EVITA estos temas ya publicados recientemente:\n"
                        f"{topic_list}\n\nElige temas COMPLETAMENTE DIFERENTES.\n"
                    )

            client = create_llm_client(enable_thinking=False)

            prompt = f"""Genera 3 ideas virales para un YouTube Short (30-90 segundos) en español.

CANAL: {display_name}
ESTILO: {niche}
DESCRIPCIÓN: {tagline}
{topic_warning}
Cada idea debe ser un tema sorprendente, polémico o curioso que funcione bien en formato corto vertical.
Formato: datos impactantes, cliffhangers, o revelaciones que enganchen en los primeros 3 segundos.

Devuelve SOLO un array JSON con 3 objetos, cada uno con campos "title" y "tema":
[{{"title": "...", "tema": "frase corta que identifica el tema (max 80 chars)"}}, ...]"""

            from config.llm_helpers import llm_json_call_or_fallback
            result = llm_json_call_or_fallback(
                client,
                fallback=[],
                max_retries=3,
                retry_delay=2.0,
                model=LLM_MODEL,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.9,
                max_tokens=500,
            )
            if isinstance(result, list) and result:
                logger.info("LLM generated %d topic ideas for native short", len(result))
                return result
        except Exception as e:
            logger.warning("LLM topic generation failed after retries: %s", e)

        return []

    def _phase_script_short(self, content_item: dict, include_subscribe_cta: bool = False) -> Optional[dict]:
        """Generate a 30-90 second script for a Short.

        Args:
            content_item: dict with at least 'title' key
            include_subscribe_cta: if True, cierre should NOT include subscription
                                   language (a separate subscribe_cta block will be
                                   appended programmatically by the caller).
        """
        from config.llm_client import create_llm_client
        from config.settings import LLM_MODEL

        client = create_llm_client()

        niche = getattr(self.config, "CANAL_NARRATIVE_STYLE", "documental")
        display_name = getattr(self.config, "CANAL_DISPLAY_NAME", self.channel_slug)

        # ── Build theme context block for the prompt (v8) ──────
        theme_block = ""
        tc = self._theme_context
        if tc:
            theme_lines = [
                "\nCONTEXTO TEMÁTICO DEL SHORT (ancla visual para las escenas):"
            ]
            if tc.genre and tc.genre != "documental":
                theme_lines.append(f"- Género: {tc.genre}")
            if tc.era and tc.era != "atemporal":
                theme_lines.append(f"- Época: {tc.era}")
            if tc.primary_subject:
                theme_lines.append(f"- Sujeto visual: {tc.primary_subject}")
            if tc.key_motifs:
                theme_lines.append(f"- Motivos visuales: {', '.join(tc.key_motifs[:4])}")
            if tc.mood:
                theme_lines.append(f"- Mood: {tc.mood}")
            if tc.lighting:
                theme_lines.append(f"- Iluminación: {tc.lighting}")
            if tc.forbidden_elements:
                theme_lines.append(
                    f"- ⛔ NUNCA incluir en search_query_en: {', '.join(tc.forbidden_elements)}"
                )
            theme_lines.append(
                "\nUsa este contexto para generar search_query_en que anclen cada escena "
                "en el MISMO mundo visual."
            )
            theme_block = "\n".join(theme_lines) + "\n\n"

        # Cierre: natural conclusion (no subscription text if caller will add subscribe_cta)
        if include_subscribe_cta:
            cierre_desc = "6. cierre (3-5 seg) — conclusión natural del short, SIN pedir suscripción"
        else:
            cierre_desc = "6. cierre (3-5 seg) — cierre natural"

        prompt = f"""Crea un guion para YouTube Shorts (~50-58 segundos de narración, ~90-110 palabras en español).

CANAL: {display_name}
ESTILO: {niche}
FORMATO: Video vertical 9:16, sin presentador, videos e imágenes en pantalla.
TEMA: {content_item.get('title', '')}
{theme_block}Usa entre 5 y 7 bloques narrativos:
1. hook (3-5 seg) — frase impactante que enganche
2. desarrollo1 (7-10 seg) — contexto y origen del dato
3. desarrollo2 (7-10 seg) — dato más impactante, comparaciones
4. desarrollo3 (opcional, 5-8 seg) — detalle adicional si el tema lo justifica
5. climax (5-8 seg) — consecuencia, revelación o misterio
{cierre_desc}

IMPORTANTE — BÚSQUEDA DE ASSETS (imágenes y videos):
- Para CADA bloque, genera "search_query_en" con 5-8 keywords EN INGLÉS que describan
  la escena visual exacta de ese bloque. Usa solo inglés (las APIs de stock no entienden español).
  Incluye: tema concreto + detalles visuales (iluminación, tipo de plano, atmósfera, época, acción).
  NO uses adjetivos abstractos ("beautiful", "amazing"). Sé MUY concreto.
  Piensa en lo que se VERÍA en pantalla mientras se narra ese texto exacto.
- Además, genera "theme_keywords_en" a nivel del short: 5-8 keywords EN INGLÉS que capturen
  el tema visual GLOBAL del short. Estas keywords se mezclarán en todas las búsquedas para
  mantener coherencia visual entre escenas.

Devuelve SOLO JSON con entre 5 y 7 bloques:
{{"tema": "frase corta que identifica el tema (max 80 chars)", "titulo": "título corto y viral", "theme_keywords_en": ["global", "theme", "keywords", ...], "bloques": [{{"tipo": "hook", "texto": "narración en español", "search_query_en": "english stock search keywords"}}, {{"tipo": "desarrollo1", "texto": "narración en español", "search_query_en": "english stock search keywords"}}, {{"tipo": "desarrollo2", "texto": "narración en español", "search_query_en": "english stock search keywords"}}, {{"tipo": "desarrollo3", "texto": "narración en español (opcional)", "search_query_en": "english stock search keywords"}}, {{"tipo": "climax", "texto": "narración en español", "search_query_en": "english stock search keywords"}}, {{"tipo": "cierre", "texto": "narración en español", "search_query_en": "english stock search keywords"}}], "hashtags": ["#Shorts", ...], "hook_text": "frase para quemar en pantalla"}}

El array bloques debe tener 5, 6 o 7 elementos. Si usas 5, omite desarrollo3.
NADA MAS fuera del JSON."""

        try:
            from config.llm_helpers import llm_json_call_or_fallback
            result = llm_json_call_or_fallback(
                client,
                fallback={},
                max_retries=3,
                retry_delay=2.0,
                model=LLM_MODEL,
                messages=[
                    {"role": "system", "content": "Eres un guionista de YouTube Shorts virales."},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.8,
                max_tokens=1800,
            )
            if result:
                return result
            return None

        except Exception as e:
            logger.error(f"Short script generation error after retries: {e}")
            return None

    def _phase_tts_short(self, script: dict) -> Optional[dict]:
        """Generate TTS audio block-by-block for a short script.

        Uses per-block rate/pitch from TTS_STRATEGY config to avoid
        monotonous narration and ensures nothing is mid-phrase truncated.
        """
        import time
        from pathlib import Path
        from config.settings import OUTPUT_DIR
        from pipeline.shorts_tts import synthesize_shorts_blocks

        bloques = script.get("bloques", [])
        if not bloques:
            logger.error("No blocks in short script")
            return None

        output_dir = OUTPUT_DIR / "videos" / "shorts"
        output_dir.mkdir(parents=True, exist_ok=True)
        ts = int(time.time())
        audio_path = output_dir / f"pipeline_audio_{self.channel_slug}_{ts}.mp3"
        srt_path   = output_dir / f"pipeline_audio_{self.channel_slug}_{ts}.srt"

        try:
            result = synthesize_shorts_blocks(
                bloques=bloques,
                ch_config=self.config,
                output_audio_path=audio_path,
                output_srt_path=srt_path,
            )
            return {
                "audio_path":   result["audio_path"],
                "timestamps":   result["timestamps"],
                "srt_path":     result["srt_path"],
                "duration_sec": result["duration_sec"],
            }
        except Exception as e:
            logger.error("Short TTS error: %s", e)
            return None

    def _phase_media_short(self, script: dict, scene_ranges: list | None = None) -> Optional[list]:
        """Fetch assets exhaustively (v2) — one distinct asset per scene.

        Uses the new v2 exhaustive search with query pool, provider pagination,
        cross-short dedup, and 50-60% portrait video mix.

        When ``scene_ranges`` is provided, fetches one asset per sub-scene
        (enabling ~10s visual variety).  Falls back to ``script['bloques']``
        otherwise.
        """
        from pipeline.shorts_media import fetch_short_assets_exhaustive

        theme_kw = script.get("theme_keywords_en", [])
        bloques = script.get("bloques", [])
        channel_id = getattr(self, "channel_id", 0)

        fetch_list = scene_ranges if scene_ranges else bloques

        if not fetch_list:
            return []

        try:
            assets = fetch_short_assets_exhaustive(
                fetch_list, self.config, theme_kw,
                theme_ctx=self._theme_context,  # v8: pass full ThemeContext
                channel_id=channel_id, channel_slug=self.channel_slug,
            )
            logger.info("Fetched %d assets for native Short (fetch_list=%d)",
                        len(assets), len(fetch_list))
            return assets
        except Exception as e:
            logger.error("Short media fetch failed: %s", e)
            return []

    def _phase_render_short(
        self, script: dict, audio: dict, media: list,
        scene_ranges: list | None = None,
    ) -> Optional[Path]:
        """Render the short video using hybrid FFmpeg render (v2).

        Mixes video clips, Ken Burns still images, xfade transitions, and
        burned SRT subtitles. Falls back to solid-colour background if no
        valid assets are available.

        When ``scene_ranges`` is provided, each segment uses its actual
        narration duration instead of a uniform split — enabling sub-scene
        splitting for ~10s visual variety.
        """
        from pipeline.shorts_media import render_short_hybrid
        from config.settings import OUTPUT_DIR

        output_dir = OUTPUT_DIR / "videos" / "shorts"
        output_dir.mkdir(parents=True, exist_ok=True)

        import time
        ts = int(time.time())
        output_path = output_dir / f"native_short_{self.channel_slug}_{ts}.mp4"

        audio_path = Path(audio["audio_path"]) if audio.get("audio_path") else None
        audio_duration = audio.get("duration_sec", None)
        if audio_duration is not None:
            audio_duration = audio_duration + 1.5

        color_palette = getattr(self.config, "COLOR_PALETTE", {})
        def _to_hex(c):
            if isinstance(c, (tuple, list)) and len(c) == 3:
                return f"{int(c[0]):02x}{int(c[1]):02x}{int(c[2]):02x}"
            return str(c).lstrip("#").replace("#", "")
        bg_color = _to_hex(color_palette.get("text_shadow", (10, 10, 26)))

        # Filter and align assets with scene_ranges for the renderer
        if scene_ranges and len(scene_ranges) == len(media):
            paired = [(a, sr) for a, sr in zip(media, scene_ranges) if a is not None]
            render_assets = [p[0] for p in paired]
            render_ranges = [p[1] for p in paired]
            logger.info(
                "[%s] Filtered to %d valid assets (from %d scene_ranges)",
                self.channel_slug, len(render_assets), len(scene_ranges),
            )
        else:
            render_assets = [a for a in (media or []) if a is not None]
            render_ranges = None

        try:
            render_short_hybrid(
                asset_items=render_assets or [],
                audio_path=audio_path,
                output_path=output_path,
                audio_duration=audio_duration,
                bg_color_hex=bg_color,
                scene_ranges=render_ranges,
            )
            return output_path
        except Exception as e:
            logger.error("Native Short hybrid render failed: %s", e)
            # Fallback to solid bg
            try:
                render_short_hybrid(
                    asset_items=[],
                    audio_path=audio_path,
                    output_path=output_path,
                    audio_duration=audio_duration,
                    bg_color_hex=bg_color,
                )
                return output_path
            except Exception as e2:
                logger.error("Native Short solid-bg render also failed: %s", e2)
                return None

    def _phase_upload_short(self, script: dict, video_path: Path) -> Optional[dict]:
        """Upload the short to YouTube."""
        from pipeline.youtube_uploader import YouTubeUploader

        uploader = YouTubeUploader(
            account_name=self.channel_slug,
            channel_slug=self.channel_slug,
        )

        if not uploader.authenticate():
            logger.error(f"[{self.channel_slug}] Upload auth failed for short")
            return None

        title = script.get("titulo", "Short sin título")[:100]

        # ── Cross-promotion: link to long-form video ──────────
        from pipeline.shorts_cross_promote import (
            get_best_longform_link, build_short_description, run_post_publish_promotion,
            should_cross_promote,
        )
        longform_url = None
        channel_id = self.db.get_channel_by_slug(self.channel_slug).get("id") if self.db else None
        if should_cross_promote(self.config) and channel_id is not None:
            longform_url = get_best_longform_link(channel_id)

        channel_url = getattr(self.config, "YOUTUBE_CHANNEL_URL", "")
        description = build_short_description(
            hook_text=script.get("hook_text", ""),
            hashtags=script.get("hashtags", ["Shorts"]),
            longform_url=longform_url,
            channel_url=channel_url,
        )

        result = uploader.upload(
            video_path=video_path,
            title=title,
            description=description[:5000],
            tags=script.get("hashtags", [])[:60],
            category_id=getattr(self.config, "YT_CATEGORY_ID", "24"),
            privacy="public",
        )

        video_id = result.get("video_id")
        if video_id:
            # ── Post-publish cross-promotion ──────────────
            run_post_publish_promotion(
                channel_slug=self.channel_slug,
                short_yt_id=video_id,
                channel_id=channel_id,
                source_yt_id=longform_url.split("v=")[-1] if longform_url else None,
                channel_config=self.config,
            )
            return {
                "youtube_id": video_id,
                "youtube_url": result.get("url", ""),
                "title": title,
                "file_path": str(video_path),
            }
        return None
