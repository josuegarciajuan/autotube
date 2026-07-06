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

    # Get source video path
    conn = sqlite3.connect(str(DATABASE_PATH), timeout=30)
    conn.row_factory = sqlite3.Row
    source_video = conn.execute(
        "SELECT video_path, channel_id FROM videos WHERE id = ?",
        (short["source_video_id"],),
    ).fetchone()
    conn.close()

    if not source_video or not source_video["video_path"]:
        raise HTTPException(400, "Source video file not found")

    source_path = Path(source_video["video_path"])
    if not source_path.exists():
        raise HTTPException(400, f"Source video file does not exist: {source_path}")

    # Render
    scheduler.mark_rendering(short_id)

    renderer = ShortsRenderer()
    output_path = renderer.render(source_path, short)

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
    description = f"""{short.get('hook_text', '')}

{' '.join(f'#{t}' for t in getattr(ch_config, 'SHORTS_HASHTAGS', ['#Shorts']))}

📺 Video completo en el canal."""

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


@router.post("/extract-and-publish/{video_id}")
async def extract_and_publish(video_id: int):
    """Extract the #1 most impactful clip from a video, render it, 
    and publish immediately to YouTube. One-shot operation.

    Returns { short_id, youtube_id, youtube_url, title } on success.
    """
    import sqlite3
    import json as _json
    from pathlib import Path
    from config.settings import DATABASE_PATH
    from pipeline.shorts_extractor import ShortsExtractor
    from pipeline.shorts_renderer import ShortsRenderer
    from pipeline.shorts_scheduler import ShortsScheduler
    from pipeline.youtube_uploader import YouTubeUploader
    from config.config_bridge import get_channel_config

    # 1. Get video info
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

    # 2. Resolve source video file
    source_path = None
    if video["video_path"]:
        candidate = Path(video["video_path"])
        # Try project-root-relative or absolute
        for p in [candidate, Path("/root/autotube") / str(video["video_path"])]:
            if p.exists():
                source_path = p
                break

    # If no local file but video is on YouTube, try to download
    if source_path is None and video.get("yt_video_id"):
        try:
            import subprocess
            import tempfile
            yt_url = video.get("yt_url") or f"https://www.youtube.com/watch?v={video['yt_video_id']}"
            # Try yt-dlp (lightweight download, just the first 3 minutes)
            with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as tmp:
                tmp_path = tmp.name
            result = subprocess.run(
                ["yt-dlp", "-f", "best[height<=1080]", "--max-filesize", "100M",
                 "-o", tmp_path, "--no-playlist", yt_url],
                capture_output=True, text=True, timeout=120,
            )
            if result.returncode == 0 and Path(tmp_path).exists():
                source_path = Path(tmp_path)
        except Exception as e:
            logger.warning("yt-dlp download failed for video #%d: %s", video_id, e)

    if source_path is None:
        conn.close()
        raise HTTPException(
            400,
            "Video file not available locally and could not be downloaded. "
            "Try regenerating the video first.",
        )

    # 2. Get script text
    script_text = ""
    if video["script_id"]:
        script_row = conn.execute(
            "SELECT * FROM scripts WHERE id = ?",
            (video["script_id"],),
        ).fetchone()
        if script_row:
            script_text = script_row["contenido"] or script_row["script_text"] or ""

    # Try bloques_json if available
    if not script_text:
        bloques_raw = video.get("title_options") or "{}"
        try:
            bloques = _json.loads(bloques_raw) if isinstance(bloques_raw, str) else {}
        except:
            bloques = {}
        script_text = str(bloques.get("script", "")) or script_text

    if not script_text:
        conn.close()
        raise HTTPException(400, "Video has no script text — cannot extract clips")

    conn.close()

    # 3. Extract best clip (ranking=1 only)
    extractor = ShortsExtractor()
    clips = extractor.extract(script_text=script_text, timestamps=[], max_clips=3, min_clips=1)

    if not clips:
        raise HTTPException(400, "No suitable clip found in video script")

    best_clip = clips[0]  # Already sorted by ranking

    # 4. Render the clip
    renderer = ShortsRenderer()
    output_path = renderer.render(source_path, best_clip)

    if not output_path or not output_path.exists():
        raise HTTPException(500, "Render failed — no output produced")

    # 5. Schedule + mark as ready
    scheduler = ShortsScheduler()
    short_ids = scheduler.schedule_clips(video_id, channel_slug, [best_clip])
    if not short_ids:
        raise HTTPException(500, "Failed to create short DB record")

    short_id = short_ids[0]
    scheduler.mark_ready(short_id, str(output_path))

    # 6. Upload to YouTube
    ch_config = get_channel_config(channel_slug)
    hashtags = getattr(ch_config, "SHORTS_HASHTAGS", ["#Shorts"])

    uploader = YouTubeUploader(account_name=channel_slug, channel_slug=channel_slug)
    if not uploader.authenticate():
        scheduler.mark_failed(short_id, "YouTube authentication failed")
        raise HTTPException(500, "YouTube authentication failed")

    title = best_clip.get("hook_title", "Short")[:100]
    hook_text = best_clip.get("hook_text", "")
    description = f"""{hook_text}

{' '.join(f'#{t}' for t in hashtags[:10])}

📺 Video completo en el canal."""

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

    return {
        "short_id": short_id,
        "youtube_id": yt_id,
        "youtube_url": result.get("url", ""),
        "title": title,
        "message": "Short created and published successfully",
    }


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
    from pathlib import Path
    from config.settings import DATABASE_PATH, LLM_API_KEY, LLM_BASE_URL, LLM_MODEL, OUTPUT_DIR
    from config.config_bridge import get_channel_config
    from pipeline.youtube_uploader import YouTubeUploader

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

    # 2. Generate script via LLM
    from openai import OpenAI
    client = OpenAI(api_key=LLM_API_KEY, base_url=LLM_BASE_URL)

    prompt = f"""IMPORTANTE: responde ÚNICAMENTE con el JSON. Sin introducción, sin markdown, sin explicación. Solo el JSON.

Canal: {display_name} — {niche}
Tagline: {tagline}

Genera un Short viral de ~40-45 segundos de narración (~70-90 palabras en español). USA 5 BLOQUES, cada uno con 1-2 frases concisas. El JSON EXACTO es:

{{"titulo": "título corto viral máximo 60 chars", "hook_text": "frase de gancho de 8-12 palabras para pantalla", "bloques": [{{"tipo": "hook", "texto": "1-2 frases que enganchen en los primeros 3-4 segundos. Plantea una pregunta, misterio o hecho sorprendente."}}, {{"tipo": "desarrollo1", "texto": "1-2 frases con contexto. Explica el origen, la historia detrás del dato. Añade detalles concretos."}}, {{"tipo": "desarrollo2", "texto": "1-2 frases con el dato más impactante. Profundiza en la revelación. Usa comparaciones o datos numéricos."}}, {{"tipo": "climax", "texto": "1-2 frases con la consecuencia o el misterio sin resolver. Qué significa este dato. Por qué debería importarnos."}}, {{"tipo": "cierre", "texto": "1-2 frases de cierre. Resume el impacto, deja una reflexión, e invita a suscribirse."}}], "hashtags": ["#Shorts", "#Curiosidades"]}}

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

    # 3b. Fetch portrait images
    portrait_queries = [b.get("texto", "")[:80] for b in bloques]
    portrait_queries = [q for q in portrait_queries if q.strip()]
    if not portrait_queries:
        portrait_queries = [hook_text[:80]]

    from pipeline.shorts_media import fetch_portrait_images, render_slideshow_with_images

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

    # Build channel subscription link
    channel_url = getattr(ch_config, "YOUTUBE_CHANNEL_URL", "")
    sub_link = f"\n\n📺 Suscríbete: {channel_url}?sub_confirmation=1" if channel_url else ""

    description = f"""{hook_text}

{' '.join(f'#{t}' for t in hashtags[:10])}{sub_link}

🔔 Suscríbete para más contenido como este."""

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
           (channel_id, type, title, hook_title, hook_text,
            status, file_path, youtube_id, youtube_url, published_at)
           VALUES (?, 'native', ?, ?, ?, 'published', ?, ?, ?, datetime('now'))""",
        (channel_id, title, title[:60], hook_text,
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

    return {
        "short_id": short_id,
        "youtube_id": yt_id,
        "youtube_url": result.get("url", ""),
        "title": title,
        "hook_text": hook_text,
        "message": "Short nativo generado y publicado exitosamente",
    }
