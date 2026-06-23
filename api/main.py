"""Autotube v2 FastAPI Application.

Serves the React SPA and REST API for the multi-channel video management panel.
"""
import sys
import json
import asyncio
import logging
from pathlib import Path
from contextlib import asynccontextmanager

sys.path.insert(0, str(Path(__file__).parent.parent))

import mimetypes
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse, RedirectResponse

from api.deps import get_db
from api.progress import get_progress_manager
from api.routers import channels, videos, scenes, jobs, schedules, ws as ws_router
from api.routers import auth
from database.db_extended import migrate_v2, ExtendedDatabase
from database.db import init_db
from config.settings import TOKENS_DIR, DATABASE_PATH


# ── Lifespan ─────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    init_db()
    migrate_v2()

    # Auto-sync channel configs from Python modules → DB
    try:
        from config.config_bridge import sync_all_configs_to_db
        synced = sync_all_configs_to_db()
        logger = logging.getLogger("autotube.startup")
        logger.info("Config sync: %d channel(s) synced → %s", len(synced), synced)
    except Exception as exc:
        logging.getLogger("autotube.startup").warning("Config sync skipped: %s", exc)
    
    # Launch schedule checker in background
    import asyncio
    schedule_task = asyncio.create_task(_schedule_checker_loop())
    
    yield
    
    # Shutdown
    schedule_task.cancel()
    try:
        await schedule_task
    except asyncio.CancelledError:
        pass


async def _schedule_checker_loop():
    """Background loop that checks content_schedules and collects YouTube stats."""
    import asyncio, logging, time
    logger = logging.getLogger("autotube.scheduler")
    
    last_stats_collection = 0
    
    while True:
        try:
            await asyncio.sleep(300)  # Check every 5 minutes
            await _process_due_schedules()
            
            # Collect YouTube stats every 6 hours
            now = time.time()
            if now - last_stats_collection > 21600:  # 6 hours
                await _collect_youtube_stats()
                last_stats_collection = now
                
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"Schedule checker error: {e}")
            await asyncio.sleep(60)


async def _process_due_schedules():
    """Find and execute due schedules."""
    import sqlite3, logging
    from datetime import datetime
    from config.settings import DATABASE_PATH
    logger = logging.getLogger("autotube.scheduler")
    
    conn = sqlite3.connect(str(DATABASE_PATH))
    conn.row_factory = sqlite3.Row
    
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    rows = conn.execute(
        "SELECT cs.*, c.slug as channel_slug FROM content_schedules cs "
        "JOIN channels c ON cs.channel_id = c.id "
        "WHERE cs.active = 1 AND datetime(cs.next_run_at) <= datetime(?)",
        (now,),
    ).fetchall()
    
    if not rows:
        conn.close()
        return
    
    for row in rows:
        s = dict(row)
        logger.info(f"Running schedule #{s['id']}: {s['channel_slug']} action={s['action']}")
        
        try:
            # Launch the generation job
            from api.services.generation_service import start_generation_job
            from database.db_extended import ExtendedDatabase
            db = ExtendedDatabase()
            
            # Create a video record
            cursor = conn.execute(
                "INSERT INTO videos (canal, channel_id, video_path, status, progress) VALUES (?, ?, '', 'generating', 0)",
                (s["channel_slug"], s["channel_id"]),
            )
            conn.commit()
            video_id = cursor.lastrowid
            
            # Create job record
            job_id = db.create_job(s["channel_id"], s["action"], video_id)
            
            # Update schedule: calculate next run
            if s["schedule_type"] == "recurring":
                next_run = f"datetime('now', '+{s['interval_h']} hours')"
                conn.execute(
                    "UPDATE content_schedules SET last_run_at = ?, next_run_at = {}, video_id = ? WHERE id = ?".format(next_run),
                    (now, video_id, s["id"]),
                )
            else:
                # One-time: deactivate after run
                conn.execute(
                    "UPDATE content_schedules SET last_run_at = ?, active = 0, video_id = ? WHERE id = ?",
                    (now, video_id, s["id"]),
                )
            conn.commit()
            
            # Fire and forget the generation (don't await)
            import asyncio
            asyncio.create_task(
                start_generation_job(
                    job_id=job_id,
                    channel_id=s["channel_id"],
                    video_id=video_id,
                    action=s["action"],
                    content_id=s.get("content_id"),
                )
            )
            
        except Exception as e:
            logger.error(f"Schedule #{s['id']} failed: {e}")
            conn.execute(
                "UPDATE content_schedules SET next_run_at = datetime('now', '+1 hour') WHERE id = ?",
                (s["id"],),
            )
            conn.commit()
    
    conn.close()


async def _collect_youtube_stats():
    """Collect YouTube stats for all active channels with valid tokens."""
    import logging
    logger = logging.getLogger("autotube.stats")
    
    try:
        from database.db_extended import ExtendedDatabase
        from pipeline.youtube_stats import YouTubeStatsFetcher
        
        db = ExtendedDatabase()
        channels = db.get_channels(active_only=True)
        
        for ch in channels:
            slug = ch["slug"]
            token_path = TOKENS_DIR / f"{slug}.pickle"
            if not token_path.exists():
                continue
            
            try:
                fetcher = YouTubeStatsFetcher(slug)
                result = fetcher.collect_and_store(db)
                logger.info(
                    "Stats collected for %s: %s videos, channel=%s",
                    slug,
                    result.get("videos_updated", 0),
                    result.get("channel_updated", False),
                )
            except Exception as exc:
                logger.error("Stats collection failed for %s: %s", slug, exc)
    except Exception as exc:
        logger.error("Stats collector error: %s", exc)


# ── App ──────────────────────────────────────────────────────

app = FastAPI(
    title="Autotube Panel API",
    version="2.0.0",
    description="Multi-channel YouTube automation panel",
    lifespan=lifespan,
)

# CORS - allow embedding in CRM iframe
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── API Routers ──────────────────────────────────────────────

app.include_router(auth.router, prefix="/api", tags=["Auth"])
app.include_router(channels.router, prefix="/api/channels", tags=["Channels"])
app.include_router(videos.router, prefix="/api/videos", tags=["Videos"])
app.include_router(scenes.router, prefix="/api/scenes", tags=["Scenes"])
app.include_router(jobs.router, prefix="/api/jobs", tags=["Jobs"])
app.include_router(schedules.router, prefix="/api/schedules", tags=["Schedules"])

# WebSocket
@app.websocket("/ws/progress/{job_id}")
async def ws_progress(ws: WebSocket, job_id: int):
    await ws.accept()
    mgr = get_progress_manager()
    mgr.subscribe(job_id, ws)
    try:
        while True:
            # Keep connection alive, client sends pings
            data = await ws.receive_text()
            if data == "ping":
                await ws.send_text("pong")
    except WebSocketDisconnect:
        pass
    finally:
        mgr.unsubscribe(job_id, ws)


# ── Dashboard stats ──────────────────────────────────────────

@app.get("/api/stats")
def get_stats():
    db = get_db()
    stats = db.get_dashboard_stats()
    return stats


@app.get("/api/logs")
def get_logs(channel_id: int = None, limit: int = 30):
    db = get_db()
    logs = db.get_pipeline_logs(channel_id=channel_id, limit=limit)
    return logs


# ── Static file serving (output videos, images, thumbnails) ──

PROJECT_ROOT = Path(__file__).parent.parent
OUTPUT_ROOT = PROJECT_ROOT / "output"

# No-cache headers for media files to prevent stale thumbnails/videos
NO_CACHE_MEDIA = {
    "Cache-Control": "no-cache, no-store, must-revalidate",
    "Pragma": "no-cache",
    "Expires": "0",
}


def resolve_media_path(stored: str) -> Path | None:
    """Resolve a stored path (absolute, relative, or hybrid) to a filesystem Path.

    Handles three formats found in the DB:
      1. Absolute:  /root/autotube/output/videos/foo.mp4
      2. Relative:  output/videos/foo.mp4
      3. Other abs: /tmp/foo.mp4 (not under project — try as-is)

    Returns a Path if the file exists on disk, None otherwise.
    """
    if not stored or not isinstance(stored, str):
        return None

    raw = Path(stored)
    if raw.exists():
        return raw

    # Try project-root-relative
    rel = PROJECT_ROOT / stored
    if rel.exists():
        return rel

    # Try output-root-relative (for paths stored as 'videos/foo.mp4' without 'output/')
    out_rel = OUTPUT_ROOT / stored
    if out_rel.exists():
        return out_rel

    return None


@app.get("/api/static/{file_path:path}")
async def serve_static(file_path: str):
    """Serve files from output dir. Handles paths like 'output/...' or absolute."""
    full_path = resolve_media_path(file_path)
    if full_path is None:
        # Fallback: try candidates like the legacy logic
        candidates = [
            OUTPUT_ROOT / file_path,
            PROJECT_ROOT / file_path,
        ]
        if file_path.startswith("/"):
            candidates.append(Path(file_path))
        for p in candidates:
            if p.exists() and p.is_file():
                full_path = p
                break

    if full_path is None:
        raise HTTPException(404, f"File not found: {file_path}")

    return FileResponse(full_path, headers=NO_CACHE_MEDIA)


@app.get("/api/video-file/{video_id}")
async def serve_video_file(video_id: int, request: Request):
    """Stream a video file with range request support for seekable playback.
    
    If the local mp4 was deleted after upload (Fase F), redirect to YouTube.
    """
    db = get_db()
    v = db.get_video(video_id)
    if not v:
        raise HTTPException(404, "Video not found")
    
    stored_path = v.get("video_path", "")
    video_path = resolve_media_path(stored_path) if stored_path else None
    
    # Local file was deleted after upload — redirect to YouTube
    if video_path is None or not video_path.exists():
        if v.get("yt_video_id"):
            yt_url = v.get("yt_url") or f"https://youtube.com/watch?v={v['yt_video_id']}"
            return RedirectResponse(url=yt_url, status_code=302)
        raise HTTPException(404, "Video file not found on disk")
    
    file_size = video_path.stat().st_size

    file_size = video_path.stat().st_size
    range_header = request.headers.get("range")

    if range_header:
        # Parse range header
        start_str, end_str = range_header.replace("bytes=", "").split("-")
        start = int(start_str)
        end = int(end_str) if end_str else file_size - 1
        chunk_size = end - start + 1

        async def ranged_stream():
            with open(video_path, "rb") as f:
                f.seek(start)
                bytes_sent = 0
                while bytes_sent < chunk_size:
                    buf = f.read(min(65536, chunk_size - bytes_sent))
                    if not buf:
                        break
                    bytes_sent += len(buf)
                    yield buf

        return StreamingResponse(
            ranged_stream(),
            status_code=206,
            media_type="video/mp4",
            headers={
                "Content-Range": f"bytes {start}-{end}/{file_size}",
                "Accept-Ranges": "bytes",
                "Content-Length": str(chunk_size),
                **NO_CACHE_MEDIA,
            },
        )
    else:
        return FileResponse(video_path, media_type="video/mp4", headers=NO_CACHE_MEDIA)


@app.get("/api/thumbnail/{video_id}")
async def serve_thumbnail(video_id: int):
    """Serve the thumbnail for a video with no-cache headers."""
    db = get_db()
    v = db.get_video(video_id)
    if not v or not v.get("thumbnail_path"):
        raise HTTPException(404, "Thumbnail not found")

    thumb_path = resolve_media_path(v["thumbnail_path"])
    if thumb_path is None:
        raise HTTPException(404, "Thumbnail file not found on disk")

    return FileResponse(thumb_path, media_type="image/jpeg", headers=NO_CACHE_MEDIA)


# ── Static files (React SPA) ─────────────────────────────────

STATIC_DIR = Path(__file__).parent.parent / "frontend" / "dist"
NO_CACHE = {"Cache-Control": "no-cache, no-store, must-revalidate", "Pragma": "no-cache", "Expires": "0"}

if STATIC_DIR.exists():
    # Static files with no-cache headers
    class NoCacheStaticFiles(StaticFiles):
        async def __call__(self, scope, receive, send):
            async def send_wrapper(message):
                if message["type"] == "http.response.start":
                    headers = dict(message.get("headers", []))
                    headers[b"cache-control"] = b"no-cache, no-store, must-revalidate"
                    headers[b"pragma"] = b"no-cache"
                    headers[b"expires"] = b"0"
                    message["headers"] = [(k, v) for k, v in headers.items()]
                await send(message)
            await super().__call__(scope, receive, send_wrapper)
    
    app.mount("/assets", NoCacheStaticFiles(directory=STATIC_DIR / "assets"), name="assets")
    
    @app.get("/{path:path}")
    async def spa_fallback(path: str):
        """Serve React SPA — all non-API routes go to index.html."""
        if path.startswith("api/"):
            raise HTTPException(404, "API endpoint not found")
        full_path = STATIC_DIR / path
        if full_path.exists() and full_path.is_file():
            return FileResponse(full_path, headers=NO_CACHE)
        return FileResponse(STATIC_DIR / "index.html", headers=NO_CACHE)
    
    @app.get("/")
    async def root():
        return FileResponse(STATIC_DIR / "index.html", headers=NO_CACHE)
