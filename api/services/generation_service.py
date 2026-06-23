"""Generation service — orchestrates pipeline execution as async background jobs.

Broadcasts progress via WebSocket to the frontend panel.
"""

import json
import sys
import logging
import asyncio
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from database.db_extended import ExtendedDatabase
from database.db import Database
from config.settings import OUTPUT_DIR

logger = logging.getLogger("autotube.generation")


def _get_db() -> ExtendedDatabase:
    """Lazy DB connection to avoid import issues."""
    return ExtendedDatabase()


async def _broadcast_progress(job_id: int, progress: int, phase: str, 
                               message: str, status: str = "running",
                               video_id: int = None):
    """Send progress update to WebSocket subscribers."""
    from api.progress import get_progress_manager
    mgr = get_progress_manager()
    data = {
        "job_id": job_id,
        "status": status,
        "progress": progress,
        "phase": phase,
        "message": message,
    }
    if video_id:
        data["video_id"] = video_id
    await mgr.broadcast(job_id, data)
    
    # Also update DB
    db = _get_db()
    db.update_job(job_id, progress=progress, phase=phase, status=status)
    if video_id:
        db.update_job(job_id, video_id=video_id)
    if status == "failed" and not message.startswith("Error:"):
        db.update_job(job_id, error_msg=message)
    if status == "completed":
        db.update_job(job_id, status="completed", progress=100)


async def start_generation_job(job_id: int, channel_id: int, video_id: int,
                                 action: str, content_id: int = None):
    """Run the full pipeline as an async background job.
    
    Args:
        job_id: The generation_jobs.id for progress tracking
        channel_id: The channels.id 
        video_id: The videos.id (pre-created record)
        action: 'generate' or 'generate_and_upload'
        content_id: Optional specific raw_content.id to use
    """
    db = _get_db()
    ch = db.get_channel(channel_id)
    if not ch:
        await _broadcast_progress(job_id, 0, "error", "Channel not found", "failed")
        return
    
    canal = ch["slug"]
    db.update_job(job_id, status="running", started_at=None)
    
    try:
        # Import orchestrator
        from orchestrator import PipelineOrchestrator
        
        orch = PipelineOrchestrator(canal=canal, db_video_id=video_id)  # API mode: update pre-created record
        loop = asyncio.get_event_loop()
        
        # ── Phase 0: Scrape fresh content ────────────────────
        await _broadcast_progress(job_id, 5, "scrape", "Buscando nuevo contenido (Reddit + Wikipedia)...",
                                   video_id=video_id)
        await loop.run_in_executor(None, orch.phase_scrape)
        await _broadcast_progress(job_id, 12, "scrape", "Contenido listo para generar guion",
                                   video_id=video_id)
        
        # ── Phase 1: Script Generation ───────────────────────
        await _broadcast_progress(job_id, 15, "script", "Generando guion con IA...",
                                   video_id=video_id)
        
        script = await loop.run_in_executor(None, orch.phase_generate_script)
        
        # Fallback: if no unused content, retry scrape once more
        if not script:
            await _broadcast_progress(job_id, 17, "script", "Sin contenido. Reintentando scrape...",
                                       video_id=video_id)
            await asyncio.sleep(5)
            await loop.run_in_executor(None, orch.phase_scrape)
            script = await loop.run_in_executor(None, orch.phase_generate_script)
        
        if not script:
            await _broadcast_progress(job_id, 20, "script", 
                                       "Error: No se pudo generar el guion (sin contenido disponible)", "failed", video_id)
            db.update_video(video_id, status="error", progress_phase="script")
            return
        
        db.update_video(video_id, progress=25, progress_phase="script")
        await _broadcast_progress(job_id, 25, "script", 
                                   f"Guion generado: {script.get('titulo_selected', 'Sin título')[:60]}",
                                   video_id=video_id)
        
        # ── Phase 2: TTS ─────────────────────────────────────
        await _broadcast_progress(job_id, 30, "tts", "Generando voz con IA (TTS)...",
                                   video_id=video_id)
        
        # Run TTS in thread executor to avoid asyncio.run() conflict
        import concurrent.futures
        audio_data = await loop.run_in_executor(None, orch.phase_tts, script)
        
        if not audio_data:
            await _broadcast_progress(job_id, 35, "tts", "Error: Fallo la generacion de voz", "failed", video_id)
            db.update_video(video_id, status="error", progress_phase="tts")
            return
        
        db.update_video(video_id, progress=40, progress_phase="tts")
        await _broadcast_progress(job_id, 40, "tts", "Audio generado correctamente",
                                   video_id=video_id)
        
        # ── Phase 3: Images ──────────────────────────────────
        await _broadcast_progress(job_id, 45, "images", "Buscando imagenes...",
                                   video_id=video_id)
        
        image_paths = await loop.run_in_executor(None, orch.phase_media, script)
        if not image_paths:
            await _broadcast_progress(job_id, 50, "images", "Error: No se encontraron imagenes", "failed", video_id)
            db.update_video(video_id, status="error", progress_phase="images")
            return
        
        db.update_video(video_id, progress=55, progress_phase="images")
        await _broadcast_progress(job_id, 55, "images", 
                                   f"Imagenes procesadas ({sum(len(s) for s in image_paths)} total)",
                                   video_id=video_id)
        
        # ── Phase 4: Video Assembly ──────────────────────────
        await _broadcast_progress(job_id, 60, "video", "Ensamblando video...",
                                   video_id=video_id)
        
        video_data = await loop.run_in_executor(None, orch.phase_video, script, audio_data, image_paths)
        if not video_data:
            await _broadcast_progress(job_id, 75, "video", "Error: Fallo el ensamblaje del video", "failed", video_id)
            db.update_video(video_id, status="error", progress_phase="video")
            return
        
        db.update_video(video_id, progress=75, progress_phase="video")
        
        # Extract actual duration
        try:
            from moviepy import VideoFileClip
            clip = VideoFileClip(video_data["video_path"])
            duracion = int(clip.duration)
            clip.close()
        except Exception:
            duracion = script.get("duracion_estimada", 0) * 60
        
        # ── Phase 5: SEO Metadata + Enhanced Thumbnail ─────────
        await _broadcast_progress(job_id, 78, "metadata", "Generando metadatos SEO con IA...",
                                   video_id=video_id)
        
        try:
            metadata = await loop.run_in_executor(None, orch.phase_metadata, script, video_data)
        except Exception as e:
            logger.warning(f"Metadata generation failed (non-fatal): {e}")
            metadata = None
        
        if metadata:
            await _broadcast_progress(job_id, 82, "metadata",
                                       f"Metadatos generados: {len(metadata.get('titles',[]))} títulos, "
                                       f"{len(metadata.get('tags',[]))} tags",
                                       video_id=video_id)
            
            # Update video record with AI-generated metadata
            titulo = metadata.get("selected_title", video_data.get("titulo", "Sin título"))
            db.update_video(
                video_id,
                titulo_final=titulo,
                video_path=video_data["video_path"],
                thumbnail_path=video_data["thumbnail_path"],
                audio_path=audio_data.get("audio_path", ""),
                duracion_seg=duracion,
                title_options=json.dumps(metadata.get("titles", [])),
                tags_json=json.dumps(metadata.get("tags", [])),
                description=metadata.get("description", ""),
            )
        else:
            # Fallback: use original script metadata
            titulo = video_data.get("titulo", script.get("titulo_selected", "Sin título"))
            db.update_video(
                video_id,
                titulo_final=titulo,
                video_path=video_data["video_path"],
                thumbnail_path=video_data["thumbnail_path"],
                audio_path=audio_data.get("audio_path", ""),
                duracion_seg=duracion,
                title_options=script.get("titulo_options", ""),
                tags_json=script.get("keywords_json", ""),
                description="",
            )
        
        await _broadcast_progress(job_id, 80, "video", 
                                   f"Video ensamblado ({duracion}s)",
                                   video_id=video_id)
        
        # ── Phase 5: Save scenes ─────────────────────────────
        await _broadcast_progress(job_id, 85, "thumbnail", "Guardando escenas...",
                                   video_id=video_id)
        
        try:
            guion = script.get("guion", "")
            escenas_raw = script.get("escenas") or script.get("escenas_json", "[]")
            if isinstance(escenas_raw, str):
                escenas = json.loads(escenas_raw)
            else:
                escenas = escenas_raw or []
            
            scenes_data = []
            for i, escena in enumerate(escenas):
                img = ""
                if image_paths and i < len(image_paths) and image_paths[i]:
                    img = str(image_paths[i][0]) if isinstance(image_paths[i], list) else str(image_paths[i])
                
                scenes_data.append({
                    "description": escena if isinstance(escena, str) else escena.get("descripcion", str(escena)),
                    "script_text": "",
                    "image_path": img,
                    "duration_ms": (duracion * 1000) // max(len(escenas), 1) if duracion else 0,
                })
            
            if scenes_data:
                db.insert_scenes_batch(video_id, scenes_data)
        except Exception as e:
            logger.error(f"Error saving scenes: {e}")
        
        await _broadcast_progress(job_id, 90, "thumbnail", "Miniatura generada",
                                   video_id=video_id)
        
        # ── Phase 6: Upload ──────────────────────────────────
        await _broadcast_progress(job_id, 92, "upload", "Subiendo a YouTube...",
                                   video_id=video_id)
        
        video_yt_id = await loop.run_in_executor(None, orch.phase_upload, script, video_data, metadata)
        
        if video_yt_id:
            # Store YouTube ID on the API-tracked record (single-source-of-truth)
            yt_url = f"https://youtube.com/watch?v={video_yt_id}"
            db.mark_video_uploaded(video_id, video_yt_id, yt_url)
            
            await _broadcast_progress(job_id, 100, "upload", 
                                       f"Subido! youtube.com/watch?v={video_yt_id}",
                                       "completed", video_id)
            db.update_video(video_id, status="uploaded", progress=100)
        else:
            await _broadcast_progress(job_id, 95, "upload", 
                                       "Video generado pero fallo la subida a YouTube",
                                       "completed", video_id)
            db.update_video(video_id, status="ready", progress=100)
        
        # Mark script as used
        orch.db.mark_script_used(script.get("id"))
        
    except Exception as e:
        logger.exception(f"Generation job {job_id} failed: {e}")
        await _broadcast_progress(job_id, 0, "error", f"Error: {str(e)[:200]}", "failed", video_id)
        db.update_video(video_id, status="error", progress_phase="error")


async def start_upload_job(job_id: int, video_id: int):
    """Upload only — for re-uploading existing videos."""
    db = _get_db()
    v = db.get_video(video_id)
    if not v:
        await _broadcast_progress(job_id, 0, "upload", "Video not found", "failed")
        return
    
    db.update_job(job_id, status="running")
    channel_id = v.get("channel_id") or 1
    ch = db.get_channel(channel_id)
    canal = ch["slug"] if ch else v.get("canal", "canal1")
    
    try:
        await _broadcast_progress(job_id, 10, "upload", "Autenticando con YouTube...", video_id=video_id)
        
        from orchestrator import PipelineOrchestrator
        orch = PipelineOrchestrator(canal=canal)
        
        if not orch.uploader.authenticate():
            await _broadcast_progress(job_id, 20, "upload", "Error: Fallo autenticacion YouTube", "failed", video_id)
            return
        
        await _broadcast_progress(job_id, 30, "upload", "Subiendo video...", video_id=video_id)
        
        import json
        tags_raw = v.get("tags_json", "[]")
        if isinstance(tags_raw, str):
            try:
                tags = json.loads(tags_raw)
            except json.JSONDecodeError:
                tags = []
        else:
            tags = tags_raw or []
        
        result = orch.uploader.upload(
            video_path=Path(v["video_path"]),
            title=v.get("titulo_final", "Video sin título"),
            description=v.get("description", ""),
            tags=tags,
            thumbnail_path=Path(v["thumbnail_path"]) if v.get("thumbnail_path") else None,
            privacy=v.get("privacy_status", "unlisted"),
        )
        
        video_yt_id = result.get("video_id")
        if video_yt_id:
            url = result.get("url", f"https://youtube.com/watch?v={video_yt_id}")
            db.mark_video_uploaded(video_id, video_yt_id, url)
            await _broadcast_progress(job_id, 100, "upload", f"Subido: {url}", "completed", video_id)
        else:
            await _broadcast_progress(job_id, 50, "upload", "Error: Fallo la subida", "failed", video_id)
            
    except Exception as e:
        logger.exception(f"Upload job {job_id} failed: {e}")
        await _broadcast_progress(job_id, 0, "upload", f"Error: {str(e)[:200]}", "failed", video_id)


async def regenerate_scene_audio_task(scene_id: int, canal: str):
    """Regenerate TTS for a single scene."""
    db = _get_db()
    import sqlite3
    with db._connect() as conn:
        row = conn.execute("SELECT * FROM video_scenes WHERE id = ?", (scene_id,)).fetchone()
    if not row:
        return
    
    scene = dict(row)
    try:
        from pipeline.tts_engine import TTSEngine
        voice_config = {
            "voice": "es-ES-AlvaroNeural",
            "rate": "-8%",
            "pitch": "-20Hz",
            "volume": "+0%",
        }
        tts = TTSEngine(voice_config)
        audio_path, timestamps = tts.generate(scene["script_text"])
        db.update_scene(scene_id, audio_path=audio_path)
    except Exception as e:
        logger.error(f"Scene audio regeneration failed: {e}")


async def replace_scene_image_task(scene_id: int, description: str):
    """Replace image for a single scene."""
    db = _get_db()
    try:
        from pipeline.image_fetcher import ImageFetcher
        from pipeline.image_processor import ImageProcessor
        from config import canal1_config as cfg
        
        fetcher = ImageFetcher()
        processor = ImageProcessor(cfg)
        
        image_paths = fetcher.fetch_for_script([description])
        if image_paths and image_paths[0]:
            img = image_paths[0][0]
            processed = processor.process(img)
            db.update_scene(scene_id, image_path=str(processed))
    except Exception as e:
        logger.error(f"Scene image replacement failed: {e}")
