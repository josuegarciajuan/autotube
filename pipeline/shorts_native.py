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

    def run(self) -> Optional[dict]:
        """Run the full native shorts pipeline. Returns short data dict on success."""
        logger.info(f"[{self.channel_slug}] Starting NATIVE SHORTS pipeline")

        try:
            # 1. Scrape content (share with main pipeline)
            short_content = self._phase_scrape_short()
            if not short_content:
                logger.warning(f"[{self.channel_slug}] No content for native short")
                return None

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

            # 4. Media (vertical-oriented)
            media = self._phase_media_short(short_script)
            if not media:
                logger.error(f"[{self.channel_slug}] Media fetch for short failed")
                return None

            # 5. Vertical render
            video_path = self._phase_render_short(short_script, audio_data, media)
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

            client = create_llm_client(enable_thinking=True)

            prompt = f"""Genera 3 ideas virales para un YouTube Short (30-90 segundos) en español.

CANAL: {display_name}
ESTILO: {niche}
DESCRIPCIÓN: {tagline}
{topic_warning}
Cada idea debe ser un tema sorprendente, polémico o curioso que funcione bien en formato corto vertical.
Formato: datos impactantes, cliffhangers, o revelaciones que enganchen en los primeros 3 segundos.

Devuelve SOLO un array JSON con 3 objetos, cada uno con campos "title" y "tema":
[{{"title": "...", "tema": "frase corta que identifica el tema (max 80 chars)"}}, ...]"""

            response = client.chat.completions.create(
                model=LLM_MODEL,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.9,
                max_tokens=500,
            )

            import json, re
            content = response.choices[0].message.content
            match = re.search(r"\[.*\]", content, re.DOTALL)
            if match:
                ideas = json.loads(match.group(0))
                logger.info("LLM generated %d topic ideas for native short", len(ideas))
                return ideas
        except Exception as e:
            logger.warning("LLM topic generation failed: %s", e)

        return []

    def _phase_script_short(self, content_item: dict) -> Optional[dict]:
        """Generate a 30-90 second script for a Short."""
        from config.llm_client import create_llm_client
        from config.settings import LLM_MODEL

        client = create_llm_client()

        niche = getattr(self.config, "CANAL_NARRATIVE_STYLE", "documental")
        display_name = getattr(self.config, "CANAL_DISPLAY_NAME", self.channel_slug)

        prompt = f"""Crea un guion para YouTube Shorts (~35-45 segundos de narración, ~70-90 palabras en español).

CANAL: {display_name}
ESTILO: {niche}
FORMATO: Video vertical 9:16, sin presentador, imágenes y texto en pantalla.
TEMA: {content_item.get('title', '')}

El guion debe tener:
1. HOOK inicial (3-8 segundos) — frase impactante que enganche
2. DESARROLLO rápido (20-25 segundos) — 2-3 datos clave
3. CIERRE con llamado a la acción (3-7 segundos) — "suscríbete para más"

Estructura en bloques:
- bloque_hook: texto del gancho inicial (1-2 frases)
- bloque_desarrollo: texto principal (2-3 frases)
- bloque_cierre: llamado a la acción (1-2 frases)

IMPORTANTE — BÚSQUEDA DE IMÁGENES:
- Para CADA bloque, genera "search_query_en" con 5-8 keywords EN INGLÉS que describan
  la escena visual exacta de ese bloque. Usa solo inglés (las APIs de stock no entienden español).
  Incluye: tema concreto + detalles visuales (iluminación, tipo de plano, atmósfera, época).
  NO uses adjetivos abstractos ("beautiful", "amazing"). Sé MUY concreto.
- Además, genera "theme_keywords_en" a nivel del short: 5-8 keywords EN INGLÉS que capturen
  el tema visual GLOBAL del short. Estas keywords se mezclarán en todas las búsquedas para
  mantener coherencia visual entre escenas.

Devuelve SOLO JSON:
{{"tema": "frase corta que identifica el tema (max 80 chars)", "titulo": "título corto y viral", "theme_keywords_en": ["global", "theme", "keywords", ...], "bloques": [{{"tipo": "hook", "texto": "narración en español", "search_query_en": "english stock search keywords"}}, {{"tipo": "desarrollo", "texto": "narración en español", "search_query_en": "english stock search keywords"}}, {{"tipo": "cierre", "texto": "narración en español", "search_query_en": "english stock search keywords"}}], "hashtags": ["#Shorts", ...], "hook_text": "frase para quemar en pantalla"}}"""

        try:
            response = client.chat.completions.create(
                model=LLM_MODEL,
                messages=[
                    {"role": "system", "content": "Eres un guionista de YouTube Shorts virales."},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.8,
                max_tokens=1000,
            )

            import json, re
            content = response.choices[0].message.content
            match = re.search(r"\{.*\}", content, re.DOTALL)
            if match:
                return json.loads(match.group(0))
            return None

        except Exception as e:
            logger.error(f"Short script generation error: {e}")
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

    def _phase_media_short(self, script: dict) -> Optional[list]:
        """Fetch vertical-oriented media for the short.

        Builds English search queries from LLM-generated ``search_query_en`` keywords
        combined with theme keywords and channel style modifiers for thematic coherence.
        Falls back to raw ``texto`` snippets if ``search_query_en`` is missing.
        """
        from pipeline.shorts_media import fetch_portrait_images, _build_portrait_query

        theme_kw = script.get("theme_keywords_en", [])
        style_mod = getattr(self.config, "IMAGE_STYLE_MODIFIERS", "")

        queries = []
        for b in script.get("bloques", []):
            search_en = b.get("search_query_en", "")
            if search_en and search_en.strip():
                # Build composite query: scene keywords + theme context + style modifiers
                q = _build_portrait_query(search_en, theme_kw, style_mod)
                queries.append(q)
            else:
                # Fallback for legacy scripts: use raw Spanish text (poor results)
                texto = b.get("texto", "")
                if texto.strip():
                    queries.append(texto[:80])

        if not queries:
            return []

        try:
            image_paths = fetch_portrait_images(queries, self.config, count=4)
            # Return as list of {path, type} dicts for render compatibility
            assets = [{"path": p, "type": "image"} for p in image_paths]
            return assets
        except Exception as e:
            logger.error(f"Short media error: {e}")
            return []

    def _phase_render_short(
        self, script: dict, audio: dict, media: list
    ) -> Optional[Path]:
        """Render the short video in vertical format."""
        from pipeline.shorts_renderer import ShortsRenderer

        renderer = ShortsRenderer(self.config)

        # For native shorts, use MoviePy to assemble
        try:
            from moviepy import (
                VideoFileClip, ImageClip, AudioFileClip,
                CompositeVideoClip, TextClip, concatenate_videoclips,
            )
        except ImportError:
            logger.error("MoviePy not available for native Shorts rendering")
            return None

        from config.settings import OUTPUT_DIR

        output_dir = OUTPUT_DIR / "videos" / "shorts"
        output_dir.mkdir(parents=True, exist_ok=True)

        import time
        ts = int(time.time())
        output_path = output_dir / f"native_short_{self.channel_slug}_{ts}.mp4"

        try:
            clips = []
            blocks = script.get("bloques", [])
            hook_text = script.get("hook_text", script.get("titulo", ""))

            for i, block in enumerate(blocks):
                media_item = media[i] if i < len(media) else None
                block_text = block.get("texto", "")

                if media_item and media_item.get("type") == "video":
                    clip = VideoFileClip(media_item["path"])
                    clip = clip.resized((1080, 1920))
                else:
                    # Create a colored background with text
                    from PIL import Image as PILImage
                    import numpy as np
                    bg = PILImage.new("RGB", (1080, 1920), (20, 20, 30))
                    bg_array = np.array(bg)
                    clip = ImageClip(bg_array).with_duration(5)
                    clip = clip.resized((1080, 1920))

                # Add burned text
                if block_text:
                    txt = TextClip(
                        text=block_text[:200],
                        font_size=50,
                        color="white",
                        stroke_color="black",
                        stroke_width=2,
                        font="Arial",
                        method="caption",
                        size=(900, None),
                    )
                    txt = txt.with_position(("center", 1400)).with_duration(clip.duration or 5)
                    clip = CompositeVideoClip([clip, txt])

                clips.append(clip)

            # Add audio
            audio_path = Path(audio["audio_path"]) if audio.get("audio_path") else None
            if audio_path and audio_path.exists():
                final = concatenate_videoclips(clips)
                audio_clip = AudioFileClip(str(audio_path))
                final = final.with_audio(audio_clip)
                final = final.with_duration(audio_clip.duration)
            else:
                final = concatenate_videoclips(clips)

            final.write_videofile(
                str(output_path),
                fps=30,
                codec="libx264",
                bitrate="6000k",
                audio_codec="aac",
                preset="medium",
            )

            final.close()
            for c in clips:
                try: c.close()
                except: pass

            return output_path

        except Exception as e:
            logger.error(f"Native Short render error: {e}")
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
