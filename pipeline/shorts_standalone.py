"""Standalone shorts pipeline — high-effort shorts from trending niche topics.

Unlike native shorts (random LLM topics) or clip shorts (long-form excerpts),
standalone shorts discover trending topics via YouTube search and generate
premium vertical scripts designed specifically for the 50-55s format.

Uses: _phase_scrape_short → _phase_script_short → TTS → media → render → upload
from the existing NativeShortsPipeline for TTS/media/render phases.
"""

import logging
import random
from typing import Optional

logger = logging.getLogger("autotube.standalone_shorts")


def _fallback_topics_from_niche(ch_config, count: int = 3) -> list[dict]:
    """Generate valid topic dicts from the channel's niche when the LLM fails.

    Ensures standalone dispatch never silently no-ops on LLM/parsing errors:
    the short still gets generated (slightly less "trending", but functional).
    """
    niche_keywords = getattr(ch_config, "NICHE_KEYWORDS_ENG", []) or []
    tagline = getattr(ch_config, "CANAL_TAGLINE", "") or "datos y misterios"
    sample = [k for k in niche_keywords[: count + 2] if k] or [tagline]

    topics = []
    for i in range(min(count, len(sample))):
        kw = sample[i]
        topics.append({
            "title": f"Dato que no conocías sobre {kw}"[:40],
            "tema": f"Un dato impactante y poco conocido sobre {kw}, contado en 50 segundos.",
            "hook": f"Esto que vas a oír sobre {kw} lo cambia todo.",
        })
    return topics


def discover_standalone_topics(channel_slug: str, count: int = 3) -> list[dict]:
    """Discover trending topics in the channel's niche via YouTube search.

    Returns a list of topic dicts: {"title": "...", "tema": "..."}
    suitable for feeding into a standalone short pipeline.

    Falls back to niche-based topics if the LLM call fails or returns
    an invalid shape (previously the object-only JSON parser made every
    array response look like "no topics found").
    """
    from config.config_bridge import get_channel_config
    from config.llm_client import create_llm_client
    from config.llm_helpers import llm_json_array_call
    from config.settings import LLM_MODEL_CREATIVE

    ch_config = get_channel_config(channel_slug)
    niche_keywords = getattr(ch_config, "NICHE_KEYWORDS_ENG", [])
    channel_name = getattr(ch_config, "CANAL_DISPLAY_NAME", channel_slug)
    tagline = getattr(ch_config, "CANAL_TAGLINE", "")
    style = getattr(ch_config, "CANAL_NARRATIVE_STYLE", "documental")

    # Pick 5-10 keywords to describe the niche
    niche_sample = ", ".join(niche_keywords[:10]) if niche_keywords else tagline[:100]

    client = create_llm_client(enable_thinking=False, timeout=60.0, max_retries=1)

    prompt = f"""Eres un experto en YouTube Shorts y SEO para el canal "{channel_name}".

Nicho: {niche_sample}
Estilo: {style}

Genera {count} ideas para YouTube Shorts (50-55 segundos) que sean VIRALES, con alta probabilidad de ser compartidos y comentados. Las ideas deben ser:

1. ESPECÍFICAS y CONCRETAS (no abstractas ni genéricas)
2. Basadas en HECHOS reales, datos impactantes o curiosidades del nicho
3. Con un gancho que funcione en los primeros 3 segundos
4. Adaptadas al formato vertical (9:16) — ritmo rápido, payoff inmediato

Cada idea debe tener:
- "title": título del short (máx 40 chars, en español, con gancho)
- "tema": descripción de 1-2 frases del contenido específico (qué hecho/dato/historia se va a contar)
- "hook": la PRIMERA frase que dirá el narrador (máx 15 palabras, debe enganchar en <3 segundos)

Responde SOLO con un JSON array de {count} objetos."""

    try:
        result = llm_json_array_call(
            client,
            max_retries=2,
            retry_delay=1.0,
            model=LLM_MODEL_CREATIVE,
            messages=[
                {"role": "system", "content": prompt},
                {"role": "user", "content": f"Genera {count} ideas virales para shorts de {channel_name}."},
            ],
            temperature=0.95,
            max_tokens=2000,
        )

        if isinstance(result, list) and len(result) > 0:
            logger.info("[standalone] Discovered %d topics for %s", len(result), channel_slug)
            return result[:count]

    except Exception as e:
        logger.warning("[standalone] Topic discovery failed for %s: %s", channel_slug, e)

    logger.warning("[standalone] Using fallback niche topics for %s", channel_slug)
    return _fallback_topics_from_niche(ch_config, count=count)


def run_standalone_short(
    channel_slug: str,
    topic: dict,
    channel_id: int = 0,
    job_id: int = None,
    target_upload_at: str = None,
) -> Optional[int]:
    """Run a complete standalone short pipeline for a given topic.

    Reuses NativeShortsPipeline for TTS/media/render/upload phases,
    injecting the pre-discovered topic instead of random LLM generation.

    Args:
        channel_slug: channel slug (e.g., 'canal2')
        topic: topic dict with 'title', 'tema', 'hook' keys
        channel_id: DB channel ID
        job_id: generation_jobs ID for progress tracking
        target_upload_at: scheduled publish time (ISO 8601)

    Returns:
        short_id (int) on success, None on failure
    """
    from pipeline.shorts_native import NativeShortsPipeline

    try:
        pipeline = NativeShortsPipeline(channel_slug)

        # ── Use the pre-discovered topic ──────────────────
        # Override the random topic generation by pre-setting the content
        short_content = {
            "title": topic.get("title", "Untitled"),
            "tema": topic.get("tema", ""),
            "hook": topic.get("hook", ""),
            "source": "standalone_discovery",
        }

        # ── Script generation ─────────────────────────────
        script = pipeline._phase_script_short(short_content)
        if not script or not script.get("bloques"):
            logger.error("[standalone] Script generation failed for %s", channel_slug)
            return None

        # ── TTS ───────────────────────────────────────────
        from pipeline.shorts_tts import synthesize_shorts_blocks, trim_blocks_to_word_budget
        from pathlib import Path
        import tempfile, os

        output_dir = Path(tempfile.gettempdir()) / "autotube_standalone"
        output_dir.mkdir(exist_ok=True)
        audio_path = output_dir / f"standalone_{os.getpid()}.mp3"
        srt_path = output_dir / f"standalone_{os.getpid()}.srt"

        # ── Enforce the audio duration budget BEFORE TTS ──
        # The LLM often exceeds ~105 words (audio > 58 s) and
        # synthesize_shorts_blocks then raises "Short audio too long".
        # Trim first, and if a slow voice still overflows, retry with a
        # tighter budget instead of failing the whole standalone short.
        tts_result = None
        for _attempt in range(2):
            trim_blocks_to_word_budget(
                script["bloques"],
                max_words=90 if _attempt == 0 else 72,
            )
            try:
                tts_result = synthesize_shorts_blocks(
                    bloques=script["bloques"],
                    ch_config=pipeline.config,
                    output_audio_path=audio_path,
                    output_srt_path=srt_path,
                )
                break
            except RuntimeError as _tts_err:
                if "too long" not in str(_tts_err) or _attempt == 1:
                    raise
                logger.warning(
                    "[standalone] Audio still too long after trim (%s) — "
                    "retrying with tighter budget",
                    str(_tts_err)[:80],
                )
        if tts_result is None:
            logger.error("[standalone] TTS failed for %s", channel_slug)
            return None
        audio_duration = tts_result.get("duration_sec", 50)

        # ── Media ─────────────────────────────────────────
        from pipeline.shorts_media import fetch_short_assets_exhaustive, render_short_hybrid

        theme_kw = script.get("theme_keywords_en") or []
        assets = fetch_short_assets_exhaustive(
            blocks=script["bloques"],
            ch_config=pipeline.config,
            theme_keywords=theme_kw,
            theme_ctx=getattr(pipeline, "_theme_context", None),
            channel_id=channel_id,
            channel_slug=channel_slug,
        )

        # Build scene ranges — canonical pattern (normalises ms→sec via
        # _compute_block_ranges, same as NativeShortsPipeline + shorts_scheduler)
        scene_ranges = None
        try:
            from pipeline.video_editor import VideoEditor
            # NOTE: VideoEditor.__init__ takes `canal_config`, not `ch_config`
            # (passing ch_config raised TypeError → scene_ranges fell back to
            # uniform durations, degrading scene timing on every standalone short)
            editor = VideoEditor(canal_config=pipeline.config)
            bloques = script["bloques"]
            ts = tts_result.get("timestamps", [])
            if bloques and ts:
                scene_ranges = editor._compute_block_ranges(bloques, ts)
                for sr in scene_ranges:
                    sr["duracion_sec"] = sr.get("duration", 5.0)
        except Exception as e:
            logger.warning("[standalone] scene_ranges compute failed (will use uniform): %s", e)
            scene_ranges = None

        # ── Render ─────────────────────────────────────────
        render_path = output_dir / f"standalone_render_{os.getpid()}.mp4"
        render_short_hybrid(
            asset_items=assets,
            audio_path=audio_path,
            output_path=render_path,
            audio_duration=audio_duration,
            srt_path=srt_path,
            scene_ranges=scene_ranges,
        )

        # ── Upload ─────────────────────────────────────────
        # _phase_upload_short(script, video_path) — el título sale del script,
        # no se pasan kwargs extra (bug previo: 'short_content'/'render_path'/
        # 'target_upload_at' no existían en la firma → TypeError).
        result = pipeline._phase_upload_short(
            script=script,
            video_path=render_path,
        )

        # ── Cleanup temp files ─────────────────────────────
        for f in [audio_path, srt_path, render_path]:
            try:
                Path(f).unlink(missing_ok=True)
            except Exception:
                pass

        if result and result.get("short_id"):
            logger.info("[standalone] ✅ Short published: %s → %s",
                        title[:40], result.get("url", ""))
            return result["short_id"]

        return None

    except Exception as e:
        logger.error("[standalone] Pipeline failed for %s: %s", channel_slug, e)
        return None
