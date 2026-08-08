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


def discover_standalone_topics(channel_slug: str, count: int = 3) -> list[dict]:
    """Discover trending topics in the channel's niche via YouTube search.

    Returns a list of topic dicts: {"title": "...", "tema": "..."}
    suitable for feeding into a standalone short pipeline.
    """
    from config.config_bridge import get_channel_config
    from config.llm_client import create_llm_client
    from config.llm_helpers import llm_json_call
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
        result = llm_json_call(
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

    return []


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
        from pipeline.shorts_tts import synthesize_shorts_blocks
        from pathlib import Path
        import tempfile, os

        output_dir = Path(tempfile.gettempdir()) / "autotube_standalone"
        output_dir.mkdir(exist_ok=True)
        audio_path = output_dir / f"standalone_{os.getpid()}.mp3"
        srt_path = output_dir / f"standalone_{os.getpid()}.srt"

        tts_result = synthesize_shorts_blocks(
            bloques=script["bloques"],
            ch_config=pipeline.config,
            output_audio_path=audio_path,
            output_srt_path=srt_path,
        )
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

        # Build scene ranges
        timestamps = tts_result.get("timestamps", [])
        scene_ranges = []
        for i, _ in enumerate(script["bloques"]):
            start = timestamps[i]["start"] if i < len(timestamps) else 0
            end = timestamps[i]["end"] if i < len(timestamps) else audio_duration
            scene_ranges.append({"start": start, "end": end, "block_index": i})

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
        title = topic.get("title", "Untitled")[:100]
        result = pipeline._phase_upload_short(
            short_content=short_content,
            script=script,
            render_path=str(render_path),
            title=title,
            target_upload_at=target_upload_at,
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
