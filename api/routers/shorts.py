"""Shorts API routes for Autotube v2 panel.

Endpoints:
- GET  /api/shorts          — List shorts with filters
- GET  /api/shorts/{id}     — Get short detail
- POST /api/shorts/extract/{video_id}  — Extract clips from a video
- POST /api/shorts/{id}/render    — Render a short
- POST /api/shorts/{id}/publish   — Publish immediately
- PATCH /api/shorts/{id}          — Update metadata
- DELETE /api/shorts/{id}         — Delete a short
- GET  /api/shorts/stats          — Aggregate stats
"""

import logging
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, BackgroundTasks

from api.deps import get_db
from pipeline.shorts_scheduler import ShortsScheduler

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/shorts", tags=["Shorts"])


def _get_scheduler() -> ShortsScheduler:
    return ShortsScheduler()


@router.get("")
def list_shorts(
    channel_id: Optional[int] = Query(None),
    status: Optional[str] = Query(None),
    type: Optional[str] = Query(None, alias="type"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    """List shorts with optional filtering."""
    scheduler = _get_scheduler()
    shorts = scheduler.get_shorts(
        channel_id=channel_id,
        status=status,
        type_filter=type,
        limit=limit,
        offset=offset,
    )
    return shorts


@router.get("/stats")
def get_shorts_stats():
    """Get aggregate shorts statistics."""
    scheduler = _get_scheduler()
    return scheduler.get_stats()


@router.get("/today")
def get_today_shorts(channel_id: Optional[int] = Query(None)):
    """Get shorts scheduled for publication today."""
    scheduler = _get_scheduler()
    return scheduler.get_today_pending(channel_id=channel_id)


@router.get("/{short_id}")
def get_short(short_id: int):
    """Get a single short by ID."""
    scheduler = _get_scheduler()
    short = scheduler.get_short(short_id)
    if not short:
        raise HTTPException(404, "Short not found")
    return short


@router.post("/extract/{video_id}")
async def extract_clips(video_id: int, background_tasks: BackgroundTasks):
    """Extract clip shorts from a video's script.
    
    Reads the video's script and timestamps, uses LLM to identify
    high-impact moments, and schedules clips for staggered publication.
    """
    import sqlite3
    from config.settings import DATABASE_PATH
    from pipeline.shorts_extractor import ShortsExtractor
    from pipeline.shorts_scheduler import ShortsScheduler

    # Get video info
    conn = sqlite3.connect(str(DATABASE_PATH), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")

    video = conn.execute(
        """SELECT v.*, c.slug as channel_slug
           FROM videos v
           JOIN channels c ON v.channel_id = c.id
           WHERE v.id = ?""",
        (video_id,),
    ).fetchone()

    if not video:
        conn.close()
        raise HTTPException(404, "Video not found")

    channel_slug = video["channel_slug"]

    # Get script
    script_row = None
    if video["script_id"]:
        script_row = conn.execute(
            "SELECT * FROM scripts WHERE id = ?", (video["script_id"],)
        ).fetchone()

    if not script_row:
        conn.close()
        raise HTTPException(400, "Video has no script — cannot extract clips")

    script_text = script_row["contenido"] or script_row["script_text"] or ""

    # Get timestamps from timing_data
    import json as _json
    timing_data = {}
    if video.get("timing_data"):
        try:
            timing_data = _json.loads(video["timing_data"])
        except:
            pass

    timestamps = timing_data.get("phases", {}).get("tts_timestamps", [])

    conn.close()

    if not script_text:
        raise HTTPException(400, "Script text is empty")

    # Extract clips
    extractor = ShortsExtractor()
    clips = extractor.extract(
        script_text=script_text,
        timestamps=timestamps if isinstance(timestamps, list) else [],
    )

    if not clips:
        raise HTTPException(400, "No suitable clips found in script")

    # Schedule clips
    scheduler = ShortsScheduler()
    short_ids = scheduler.schedule_clips(video_id, channel_slug, clips)

    return {
        "message": f"Extracted and scheduled {len(short_ids)} clips",
        "clips_found": len(clips),
        "clips_scheduled": len(short_ids),
        "short_ids": short_ids,
    }


@router.post("/{short_id}/render")
async def render_short(short_id: int, background_tasks: BackgroundTasks):
    """Render a short from its source video clip."""
    import sqlite3
    from pathlib import Path
    from config.settings import DATABASE_PATH
    from pipeline.shorts_renderer import ShortsRenderer
    from pipeline.shorts_scheduler import ShortsScheduler

    scheduler = ShortsScheduler()
    short = scheduler.get_short(short_id)

    if not short:
        raise HTTPException(404, "Short not found")

    if short["type"] != "clip":
        raise HTTPException(400, "Only clip shorts can be rendered from source")

    if not short.get("source_video_id"):
        raise HTTPException(400, "Short has no source video — cannot render")

    # Get source video path + word timestamps for subtitle rendering
    conn = sqlite3.connect(str(DATABASE_PATH), timeout=30)
    conn.row_factory = sqlite3.Row
    source_video = conn.execute(
        "SELECT video_path, channel_id, timing_data FROM videos WHERE id = ?",
        (short["source_video_id"],),
    ).fetchone()
    conn.close()

    if not source_video or not source_video["video_path"]:
        raise HTTPException(400, "Source video file not found")

    source_path = Path(source_video["video_path"])
    if not source_path.exists():
        raise HTTPException(400, f"Source video file does not exist: {source_path}")

    # ── Extract word-level TTS timestamps for subtitles ──
    import json as _json
    tts_word_ts: list = []
    try:
        td_raw = source_video.get("timing_data") or "{}"
        td = _json.loads(td_raw) if isinstance(td_raw, str) else (td_raw or {})
        tts_word_ts = td.get("phases", {}).get("tts_timestamps", [])
        if not isinstance(tts_word_ts, list):
            tts_word_ts = []
    except Exception:
        pass

    # Render
    scheduler.mark_rendering(short_id)

    renderer = ShortsRenderer()
    output_path = renderer.render(
        source_path, short, word_timestamps=tts_word_ts if tts_word_ts else None,
    )

    if output_path and output_path.exists():
        scheduler.mark_ready(short_id, str(output_path))
        return {
            "message": "Short rendered successfully",
            "short_id": short_id,
            "file_path": str(output_path),
        }
    else:
        scheduler.mark_failed(short_id, "Render produced no output file")
        raise HTTPException(500, "Render failed — no output produced")


@router.post("/{short_id}/publish")
async def publish_short(short_id: int):
    """Publish a short to YouTube immediately (bypasses schedule)."""
    import sqlite3
    from pathlib import Path
    from config.settings import DATABASE_PATH
    from pipeline.shorts_scheduler import ShortsScheduler
    from api.deps import get_db as _get_db

    scheduler = ShortsScheduler()
    short = scheduler.get_short(short_id)

    if not short:
        raise HTTPException(404, "Short not found")

    if not short.get("file_path"):
        raise HTTPException(400, "Short has not been rendered yet")

    file_path = Path(short["file_path"])
    if not file_path.exists():
        raise HTTPException(400, f"Rendered file not found: {file_path}")

    # Get channel config
    conn = sqlite3.connect(str(DATABASE_PATH), timeout=30)
    conn.row_factory = sqlite3.Row
    ch = conn.execute("SELECT slug FROM channels WHERE id = ?", (short["channel_id"],)).fetchone()
    conn.close()

    if not ch:
        raise HTTPException(404, "Channel not found")

    from config.config_bridge import get_channel_config
    ch_config = get_channel_config(ch["slug"])

    # Upload
    from pipeline.youtube_uploader import YouTubeUploader

    uploader = YouTubeUploader(
        account_name=ch["slug"],
        channel_slug=ch["slug"],
    )

    if not uploader.authenticate():
        raise HTTPException(500, "YouTube authentication failed")

    title = short.get("title") or short.get("hook_title") or "Short sin título"

    # ── Cross-promotion: link to long-form video ──────────
    from pipeline.shorts_cross_promote import (
        get_best_longform_link, build_short_description, run_post_publish_promotion,
        should_cross_promote,
    )
    longform_url = None
    if should_cross_promote(ch_config):
        longform_url = get_best_longform_link(
            ch["id"],
            source_video_id=short.get("source_video_id"),
        )

    description = build_short_description(
        hook_text=short.get("hook_text", ""),
        hashtags=getattr(ch_config, "SHORTS_HASHTAGS", ["#Shorts"]),
        longform_url=longform_url,
        channel_url=getattr(ch_config, "YOUTUBE_CHANNEL_URL", ""),
    )

    tags = getattr(ch_config, "SHORTS_HASHTAGS", ["#Shorts"])[:60]

    result = uploader.upload(
        video_path=file_path,
        title=title[:100],
        description=description[:5000],
        tags=tags,
        category_id=getattr(ch_config, "YT_CATEGORY_ID", "24"),
        privacy="public",
    )

    video_id = result.get("video_id")
    if video_id:
        scheduler.mark_published(
            short_id,
            video_id,
            result.get("url", ""),
            short.get("file_path", ""),
        )
        # ── Post-publish cross-promotion ──────────────
        run_post_publish_promotion(
            channel_slug=ch["slug"],
            short_yt_id=video_id,
            channel_id=ch["id"],
            source_yt_id=longform_url.split("v=")[-1] if longform_url else None,
            source_video_id=short.get("source_video_id"),
            channel_config=ch_config,
        )
        return {
            "message": "Short published successfully",
            "youtube_id": video_id,
            "youtube_url": result.get("url", ""),
        }
    else:
        scheduler.mark_failed(short_id, "Upload returned no video ID")
        raise HTTPException(500, "Upload failed")


@router.patch("/{short_id}")
def update_short(short_id: int, data: dict):
    """Update short metadata (hook_title, hook_text, title, status, scheduled_date, ranking)."""
    scheduler = _get_scheduler()
    short = scheduler.get_short(short_id)
    if not short:
        raise HTTPException(404, "Short not found")

    ok = scheduler.update_short(short_id, **data)
    if not ok:
        raise HTTPException(400, "No valid fields to update")

    return {"message": "Short updated", "short_id": short_id}


@router.delete("/{short_id}")
def delete_short(short_id: int):
    """Delete a short from the database."""
    scheduler = _get_scheduler()
    short = scheduler.get_short(short_id)
    if not short:
        raise HTTPException(404, "Short not found")

    scheduler.delete_short(short_id)
    return {"message": "Short deleted", "short_id": short_id}


@router.get("/video/{video_id}")
def get_clips_for_video(video_id: int):
    """Get all clip shorts extracted from a source video."""
    scheduler = _get_scheduler()
    return scheduler.get_clips_for_video(video_id)


def _build_block_timestamps(bloques: list[dict], total_duration_sec: float) -> list[dict]:
    """Build approximate word-level timestamps from script blocks.

    Each block's text is assigned a position proportional to its index
    in the block list, using the total video duration to distribute time
    evenly across blocks. This lets the LLM extract timecodes that
    roughly match the real video timeline instead of inventing them.
    """
    if not bloques or total_duration_sec <= 0:
        return []

    n = len(bloques)
    timestamps = []

    for idx, block in enumerate(bloques):
        texto = block.get("texto", "")
        if not texto:
            continue
        words = texto.split()
        block_start = (idx / n) * total_duration_sec
        block_end = ((idx + 1) / n) * total_duration_sec
        word_duration = (block_end - block_start) / max(len(words), 1)

        for wi, word in enumerate(words):
            ts_start = round(block_start + wi * word_duration, 1)
            ts_end = round(ts_start + word_duration, 1)
            timestamps.append({"word": word, "start": ts_start, "end": ts_end})

    return timestamps


def _resolve_source_video(
    video: dict,
    video_id: int,
    clip_start: float,
    clip_end: float,
) -> tuple[Path, float] | tuple[None, None]:
    """Find or download the source video for a clip extraction.

    Returns (source_path, offset_seconds) where:
      - If local file:            offset = 0 (timecodes match original video)
      - If downloaded segment:    offset = padding (renderer uses relative timecodes)
    """
    import subprocess
    import tempfile
    from pathlib import Path

    # 1. Try local file
    if video.get("video_path"):
        for p in [Path(video["video_path"]), Path("/root/autotube") / str(video["video_path"])]:
            if p.exists():
                logger.info("Using local file for short: %s", p)
                return p, 0.0

    # 2. Download clip segment from YouTube
    yt_id = video.get("yt_video_id")
    if not yt_id:
        return None, None

    yt_url = video.get("yt_url") or f"https://www.youtube.com/watch?v={yt_id}"

    # Add buffer around the clip so ffmpeg -ss / -t can seek properly
    padding = 3.0
    section_start = max(0, clip_start - padding)
    section_end = clip_end + padding
    section_spec = f"*{section_start:.1f}-{section_end:.1f}"

    logger.info(
        "Downloading clip segment [%.1fs-%.1fs] from %s ...",
        section_start, section_end, yt_url[:60],
    )

    with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as tmp:
        tmp_path = tmp.name

    try:
        result = subprocess.run(
            ["yt-dlp",
             "--download-sections", section_spec,
             "-f", "best[height<=720]/best",
             "--no-playlist",
             "--socket-timeout", "30",
             "--force-overwrites",
             "--no-warnings",
             "-o", tmp_path,
             yt_url],
            capture_output=True, text=True, timeout=180,
        )

        if result.returncode != 0:
            stderr_tail = result.stderr.strip()[-300:] if result.stderr else "(no output)"
            logger.error("yt-dlp download failed (code %d): %s", result.returncode, stderr_tail)
            Path(tmp_path).unlink(missing_ok=True)
            return None, None

        if not Path(tmp_path).exists():
            logger.error("yt-dlp reported success but output file missing: %s", tmp_path)
            return None, None

        fsize_mb = Path(tmp_path).stat().st_size / (1024 * 1024)
        logger.info("Downloaded clip segment: %.1f MB → %s", fsize_mb, tmp_path)
        # Downloaded segment starts at section_start; the clip begins at padding seconds in
        return Path(tmp_path), padding

    except subprocess.TimeoutExpired:
        logger.error("yt-dlp download timed out (180s) for video #%d", video_id)
        Path(tmp_path).unlink(missing_ok=True)
        return None, None
    except Exception as e:
        logger.error("yt-dlp failed for video #%d: %s", video_id, e)
        Path(tmp_path).unlink(missing_ok=True)
        return None, None


@router.post("/extract-and-publish/{video_id}")
async def extract_and_publish(video_id: int):
    """Extract the #1 most impactful clip from a video, render it, 
    and publish immediately to YouTube. One-shot operation.

    Optimized flow:
      1. Get video info + script + blocks from DB (no download needed)
      2. LLM extracts best clip with real timestamps
      3. Download only the clip segment from YouTube (yt-dlp) if local file gone
      4. Render → upload YT → cross-promotion → cleanup temp files

    Returns { short_id, youtube_id, youtube_url, title } on success.
    """
    import sqlite3
    import json as _json
    import subprocess
    import tempfile
    from pathlib import Path
    from config.settings import DATABASE_PATH
    from pipeline.shorts_extractor import ShortsExtractor
    from pipeline.shorts_renderer import ShortsRenderer
    from pipeline.shorts_scheduler import ShortsScheduler
    from pipeline.youtube_uploader import YouTubeUploader
    from config.config_bridge import get_channel_config

    conn = sqlite3.connect(str(DATABASE_PATH), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")

    video = conn.execute(
        """SELECT v.*, c.slug as channel_slug
           FROM videos v
           JOIN channels c ON v.channel_id = c.id
           WHERE v.id = ?""",
        (video_id,),
    ).fetchone()

    if not video:
        conn.close()
        raise HTTPException(404, "Video not found")

    video = dict(video)  # sqlite3.Row → dict
    channel_slug = video["channel_slug"]

    # ── Phase 1: Script + blocks from DB (no download needed) ──
    script_text = ""
    bloques = []
    if video["script_id"]:
        script_row = conn.execute(
            "SELECT guion, bloques_json FROM scripts WHERE id = ?",
            (video["script_id"],),
        ).fetchone()
        if script_row:
            script_text = script_row["guion"] or ""
            try:
                bloques = _json.loads(script_row["bloques_json"] or "[]") if script_row["bloques_json"] else []
            except:
                bloques = []

    if not script_text and bloques:
        script_text = " ".join(b.get("texto", "") for b in bloques if b.get("texto"))

    if not script_text:
        bloques_raw = video.get("title_options") or "{}"
        try:
            fallback = _json.loads(bloques_raw) if isinstance(bloques_raw, str) else {}
        except:
            fallback = {}
        script_text = str(fallback.get("script", "")) or script_text

    if not script_text:
        conn.close()
        raise HTTPException(400, "Video has no script text — cannot extract clips")

    # ── Phase 2: LLM extracts best clip timecodes ──
    timestamps = _build_block_timestamps(bloques, video.get("duracion_seg") or 0)
    extractor = ShortsExtractor()
    clips = extractor.extract(script_text=script_text, timestamps=timestamps, max_clips=3, min_clips=1)

    if not clips:
        conn.close()
        raise HTTPException(400, "No suitable clip found in video script")

    best_clip = clips[0]

    # ── Extract word-level TTS timestamps for subtitle rendering ──
    tts_word_ts: list[dict] = []
    try:
        td_raw = video.get("timing_data") or "{}"
        td = _json.loads(td_raw) if isinstance(td_raw, str) else td_raw
        tts_word_ts = td.get("phases", {}).get("tts_timestamps", [])
        if not isinstance(tts_word_ts, list):
            tts_word_ts = []
        if tts_word_ts:
            logger.info("Loaded %d word-level TTS timestamps for subtitle generation", len(tts_word_ts))
    except Exception as _exc:
        logger.debug("Could not parse timing_data for subtitles: %s", _exc)

    conn.close()

    # ── Phase 3: Find or download source video ──
    source_path, clip_offset = _resolve_source_video(
        video, video_id, best_clip["start_time"], best_clip["end_time"],
    )
    if source_path is None:
        raise HTTPException(
            400,
            "Video file not available locally and could not be downloaded from YouTube. "
            "Try regenerating the video first.",
        )

    # If we downloaded a segment, adjust timecodes to be relative to the segment start
    if clip_offset > 0:
        original_start = best_clip["start_time"]
        original_end = best_clip["end_time"]
        clip_duration = original_end - original_start
        best_clip["start_time"] = clip_offset
        best_clip["end_time"] = clip_offset + clip_duration
        logger.debug(
            "Adjusted clip timecodes for downloaded segment: "
            "%.1f-%.1f → %.1f-%.1f (offset=%.1f)",
            original_start, original_end,
            best_clip["start_time"], best_clip["end_time"],
            clip_offset,
        )

    # ── Phase 4: Render → Upload → Promote ──
    _downloaded_temp = source_path  # track for cleanup
    renderer = ShortsRenderer()
    output_path = None

    try:
        # Pass word timestamps only for local file (clip_offset==0).
        # Downloaded segments have a different timebase that doesn't
        # match the original TTS timestamps.
        render_word_ts = tts_word_ts if clip_offset == 0 else None
        output_path = renderer.render(
            source_path, best_clip, word_timestamps=render_word_ts,
        )
        if not output_path or not output_path.exists():
            raise HTTPException(500, "Render failed — no output produced")

        scheduler = ShortsScheduler()
        short_ids = scheduler.schedule_clips(video_id, channel_slug, [best_clip])
        if not short_ids:
            raise HTTPException(500, "Failed to create short DB record")

        short_id = short_ids[0]
        scheduler.mark_ready(short_id, str(output_path))

        ch_config = get_channel_config(channel_slug)
        hashtags = getattr(ch_config, "SHORTS_HASHTAGS", ["#Shorts"])

        uploader = YouTubeUploader(account_name=channel_slug, channel_slug=channel_slug)
        if not uploader.authenticate():
            scheduler.mark_failed(short_id, "YouTube authentication failed")
            raise HTTPException(500, "YouTube authentication failed")

        title = best_clip.get("hook_title", "Short")[:100]
        hook_text = best_clip.get("hook_text", "")

        from pipeline.shorts_cross_promote import (
            get_best_longform_link, build_short_description, run_post_publish_promotion,
            should_cross_promote,
        )
        longform_url = None
        if should_cross_promote(ch_config):
            longform_url = get_best_longform_link(
                video["channel_id"],
                source_video_id=video_id,
            )

        description = build_short_description(
            hook_text=hook_text,
            hashtags=hashtags,
            longform_url=longform_url,
            channel_url=getattr(ch_config, "YOUTUBE_CHANNEL_URL", ""),
        )

        result = uploader.upload(
            video_path=output_path,
            title=title,
            description=description[:5000],
            tags=hashtags[:60],
            category_id=getattr(ch_config, "YT_CATEGORY_ID", "24"),
            privacy="public",
        )

        yt_id = result.get("video_id")
        if not yt_id:
            scheduler.mark_failed(short_id, "Upload returned no video ID")
            raise HTTPException(500, "Upload failed — no video ID returned")

        scheduler.mark_published(short_id, yt_id, result.get("url", ""), str(output_path))

        run_post_publish_promotion(
            channel_slug=channel_slug,
            short_yt_id=yt_id,
            channel_id=video["channel_id"],
            source_yt_id=longform_url.split("v=")[-1] if longform_url else None,
        )

        return {
            "short_id": short_id,
            "youtube_id": yt_id,
            "youtube_url": result.get("url", ""),
            "title": title,
            "message": "Short created and published successfully",
        }

    finally:
        # Cleanup temp file if we downloaded it from YouTube
        if _downloaded_temp and str(_downloaded_temp).startswith("/tmp/"):
            try:
                _downloaded_temp.unlink(missing_ok=True)
                logger.debug("Cleaned up temp download: %s", _downloaded_temp)
            except Exception:
                pass


@router.post("/generate-native/{channel_id}")
async def generate_native(channel_id: int):
    """Generate and publish a native Short in situ.
    
    Full pipeline: LLM script → edge-tts audio → FFmpeg vertical render → YouTube upload.
    Synchronous, takes ~20-60 seconds. Progress visible in the global progress bar.
    
    Returns { short_id, youtube_id, youtube_url, title } on success.
    """
    import sqlite3
    import json
    import re
    import subprocess
    import tempfile
    import time
    import traceback as tb
    from pathlib import Path
    from config.settings import DATABASE_PATH, LLM_MODEL, OUTPUT_DIR
    from config.llm_client import create_llm_client
    from config.config_bridge import get_channel_config
    from pipeline.youtube_uploader import YouTubeUploader

    # ── Global concurrency guard: strictly sequential — only ONE job at a time ──
    from api.services.generation_service import _DISPATCH_LOCK
    from database.db_extended import ExtendedDatabase
    with _DISPATCH_LOCK:
        db_concurrency = ExtendedDatabase(str(DATABASE_PATH))
        active = db_concurrency.count_active_jobs()
        if active > 0:
            raise HTTPException(409, f"Ya hay {active} generacion(es) en curso. Solo una a la vez.")

    logger.info("generate-native START for channel_id=%d", channel_id)

    # 1. Get channel info
    conn = sqlite3.connect(str(DATABASE_PATH), timeout=30)
    conn.row_factory = sqlite3.Row
    ch = conn.execute("SELECT * FROM channels WHERE id = ?", (channel_id,)).fetchone()
    conn.close()

    if not ch:
        raise HTTPException(404, "Channel not found")

    channel_slug = ch["slug"]
    ch_config = get_channel_config(channel_slug)
    niche = getattr(ch_config, "CANAL_NARRATIVE_STYLE", "documental")
    display_name = getattr(ch_config, "CANAL_DISPLAY_NAME", channel_slug)
    tagline = getattr(ch_config, "CANAL_TAGLINE", "")
    color_palette = getattr(ch_config, "COLOR_PALETTE", {})
    # Colors may be tuples (R,G,B) or hex strings
    def _to_hex(c):
        if isinstance(c, (tuple, list)) and len(c) == 3:
            return f"{int(c[0]):02x}{int(c[1]):02x}{int(c[2]):02x}"
        return str(c).lstrip("#").replace("#", "")
    primary_color = _to_hex(color_palette.get("primary", (255, 51, 85)))
    bg_color = _to_hex(color_palette.get("text_shadow", (10, 10, 26)))
    hashtags = getattr(ch_config, "SHORTS_HASHTAGS", ["#Shorts"])

    # 1b. Fetch recent topics to avoid repetition
    from database.db_extended import ExtendedDatabase
    dbx = ExtendedDatabase(str(DATABASE_PATH))
    recent_topics = dbx.get_recent_short_topics(channel_id, limit=15)
    topic_warning = ""
    if recent_topics:
        topic_list = "\n".join(f'  - "{t}"' for t in recent_topics)
        topic_warning = (
            f"\n\n⚠️ IMPORTANTE: NO repitas NINGUNO de estos temas ya publicados "
            f"recientemente en este canal:\n{topic_list}\n\n"
            f"Elige un tema COMPLETAMENTE DIFERENTE y fresco.\n"
        )

    # 2. Generate script via LLM
    client = create_llm_client(enable_thinking=False)

    prompt = f"""IMPORTANTE: responde ÚNICAMENTE con el JSON. Sin introducción, sin markdown, sin explicación. Solo el JSON.

Canal: {display_name} — {niche}
Tagline: {tagline}
{topic_warning}
Genera un Short viral de ~40-45 segundos de narración (~70-90 palabras en español). USA 5 BLOQUES, cada uno con 1-2 frases concisas. El JSON EXACTO es:

{{"tema": "frase corta que identifica el tema (max 80 chars)", "titulo": "título corto viral máximo 60 chars", "hook_text": "frase de gancho de 8-12 palabras para pantalla", "theme_keywords_en": ["global", "theme", "keywords", "visual", "context"], "bloques": [{{"tipo": "hook", "texto": "1-2 frases que enganchen en los primeros 3-4 segundos. Plantea una pregunta, misterio o hecho sorprendente.", "search_query_en": "5-8 english keywords describing exact scene visuals for stock image search. Be VERY concrete: include topic details, lighting, shot type, atmosphere. NO spanish."}}, {{"tipo": "desarrollo1", "texto": "1-2 frases con contexto. Explica el origen, la historia detrás del dato. Añade detalles concretos.", "search_query_en": "5-8 english keywords. Scene-specific. Include visual details."}}, {{"tipo": "desarrollo2", "texto": "1-2 frases con el dato más impactante. Profundiza en la revelación. Usa comparaciones o datos numéricos.", "search_query_en": "5-8 english keywords. Scene-specific. Include visual details."}}, {{"tipo": "climax", "texto": "1-2 frases con la consecuencia o el misterio sin resolver. Qué significa este dato. Por qué debería importarnos.", "search_query_en": "5-8 english keywords. Scene-specific. Include visual details."}}, {{"tipo": "cierre", "texto": "1-2 frases de cierre. Resume el impacto, deja una reflexión, e invita a suscribirse.", "search_query_en": "5-8 english keywords. Scene-specific. Include visual details."}}], "hashtags": ["#Shorts", "#Curiosidades"]}}

RESPONDE SOLO CON EL JSON. NADA MÁS."""

    try:
        response = client.chat.completions.create(
            model=LLM_MODEL,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.8, max_tokens=1400,
        )
        content = response.choices[0].message.content
        content = re.sub(r"^```(?:json)?\s*\n", "", content)
        content = re.sub(r"\n```\s*$", "", content).strip()
        match = re.search(r"\{.*\}", content, re.DOTALL)
        if not match:
            raise ValueError(f"No JSON found: {content[:200]}")
        raw = match.group(0)
        try:
            script = json.loads(raw)
        except json.JSONDecodeError:
            wrapper = json.loads(raw)
            if "video_idea" in wrapper:
                inner = wrapper["video_idea"]
                script = {"titulo": inner.get("title", ""), "hook_text": inner.get("hook_text", ""), "bloques": []}
            else:
                raise
    except Exception as e:
        logger.error("Script generation failed: %s", e)
        raise HTTPException(500, f"Script generation failed: {str(e)[:200]}")

    # 2b. Validate script completeness
    from pipeline.shorts_tts import validate_short_script
    script_errors = validate_short_script(script)
    if script_errors:
        raise HTTPException(422, f"Script validation failed: {'; '.join(script_errors)}")

    title = (script.get("titulo") or script.get("title") or "Short")[:100]
    hook_text = (script.get("hook_text") or "")[:100]
    bloques = script.get("bloques", [])
    topic = (script.get("tema") or "")[:200]  # store topic for dedup

    # 3. Segmented TTS (block-by-block, no mid-phrase truncation)
    output_dir = OUTPUT_DIR / "videos" / "shorts"
    output_dir.mkdir(parents=True, exist_ok=True)
    ts = int(time.time())
    audio_path = output_dir / f"native_audio_{channel_slug}_{ts}.mp3"
    srt_path = output_dir / f"native_audio_{channel_slug}_{ts}.srt"

    from pipeline.shorts_tts import synthesize_shorts_blocks

    try:
        tts_result = synthesize_shorts_blocks(
            bloques=bloques,
            ch_config=ch_config,
            output_audio_path=audio_path,
            output_srt_path=srt_path,
        )
        audio_duration = tts_result["duration_sec"] + 1.5
    except RuntimeError as e:
        raise HTTPException(500, f"TTS synthesis failed: {str(e)[:300]}")
    except Exception as e:
        raise HTTPException(500, f"TTS failed: {str(e)[:200]}")

    # 3b. Fetch portrait images — build theme-aware English queries
    from pipeline.shorts_media import fetch_portrait_images, render_slideshow_with_images, _build_portrait_query
    theme_kw = script.get("theme_keywords_en", [])
    style_mod = getattr(ch_config, "IMAGE_STYLE_MODIFIERS", "")

    portrait_queries = []
    for b in bloques:
        search_en = b.get("search_query_en", "")
        if search_en and search_en.strip():
            portrait_queries.append(_build_portrait_query(search_en, theme_kw, style_mod))
        else:
            texto = b.get("texto", "")
            if texto.strip():
                portrait_queries.append(texto[:80])

    if not portrait_queries:
        portrait_queries = [hook_text[:80]]

    image_paths = []
    try:
        image_paths = fetch_portrait_images(portrait_queries, ch_config, count=4)
        logger.info("Fetched %d portrait images for Short", len(image_paths))
    except Exception as e:
        logger.warning("Portrait image fetch failed (will use solid bg): %s", e)

    # 4. Render vertical video: slideshow of images + burned text + audio
    video_path = output_dir / f"native_short_{channel_slug}_{ts}.mp4"

    try:
        render_slideshow_with_images(
            image_paths=image_paths,
            audio_path=audio_path,
            hook_text=hook_text,
            output_path=video_path,
            audio_duration=audio_duration,
            bg_color_hex=bg_color,
            srt_path=srt_path if srt_path.exists() else None,
        )
    except Exception as e:
        logger.warning("Slideshow render failed (will use solid bg fallback): %s", e)
        # Solid-bg fallback with subtitles
        from pipeline.shorts_media import _build_solid_bg_filter
        filter_str = _build_solid_bg_filter(
            bg_color,
            srt_path=srt_path if srt_path.exists() else None,
        )
        render_cmd = [
            "ffmpeg", "-y",
            "-f", "lavfi", "-i", f"color=c=0x{bg_color}:s=1080x1920:d={audio_duration}:r=30",
            "-i", str(audio_path),
            "-filter_complex", filter_str,
            "-map", "[v]", "-map", "1:a",
            "-c:v", "libx264", "-preset", "fast", "-crf", "22",
            "-pix_fmt", "yuv420p", "-r", "30",
            "-c:a", "aac", "-b:a", "128k",
            "-shortest", "-movflags", "+faststart",
            str(video_path),
        ]
        result = subprocess.run(render_cmd, capture_output=True, text=True, timeout=120)
        if result.returncode != 0:
            raise RuntimeError(f"FFmpeg render failed: {result.stderr[-300:]}")

    if not video_path.exists():
        raise HTTPException(500, "Render produced no output file")

    # 5. Upload to YouTube
    uploader = YouTubeUploader(account_name=channel_slug, channel_slug=channel_slug)
    if not uploader.authenticate():
        raise HTTPException(500, "YouTube authentication failed")

    # ── Cross-promotion: link to long-form video ──────────
    from pipeline.shorts_cross_promote import (
        get_best_longform_link, build_short_description, run_post_publish_promotion,
        should_cross_promote,
    )
    longform_url = None
    if should_cross_promote(ch_config):
        longform_url = get_best_longform_link(channel_id)

    channel_url = getattr(ch_config, "YOUTUBE_CHANNEL_URL", "")
    description = build_short_description(
        hook_text=hook_text,
        hashtags=hashtags,
        longform_url=longform_url,
        channel_url=channel_url,
    )

    result = uploader.upload(
        video_path=video_path,
        title=title[:100],
        description=description[:5000],
        tags=hashtags[:60],
        category_id=getattr(ch_config, "YT_CATEGORY_ID", "24"),
        privacy="public",
    )

    yt_id = result.get("video_id")
    if not yt_id:
        raise HTTPException(500, "Upload failed — no video ID returned")

    # 6. Register in DB
    conn = sqlite3.connect(str(DATABASE_PATH), timeout=30)
    cursor = conn.execute(
        """INSERT INTO shorts
           (channel_id, type, title, hook_title, hook_text, topic,
            status, file_path, youtube_id, youtube_url, published_at)
           VALUES (?, 'native', ?, ?, ?, ?, 'published', ?, ?, ?, datetime('now','localtime'))""",
        (channel_id, title, title[:60], hook_text, topic,
         str(video_path), yt_id, result.get("url", "")),
    )
    short_id = cursor.lastrowid
    conn.commit()
    conn.close()

    # Update shorts_schedule produced_count
    conn = sqlite3.connect(str(DATABASE_PATH), timeout=30)
    today = __import__('datetime').date.today().isoformat()
    conn.execute(
        """UPDATE shorts_schedule SET produced_count = produced_count + 1
           WHERE channel_id = ? AND schedule_date = ?""",
        (channel_id, today),
    )
    conn.commit()
    conn.close()

    # ── Post-publish cross-promotion ──────────────
    run_post_publish_promotion(
        channel_slug=channel_slug,
        short_yt_id=yt_id,
        channel_id=channel_id,
        source_yt_id=longform_url.split("v=")[-1] if longform_url else None,
    )

    return {
        "short_id": short_id,
        "youtube_id": yt_id,
        "youtube_url": result.get("url", ""),
        "title": title,
        "hook_text": hook_text,
        "message": "Short nativo generado y publicado exitosamente",
    }
