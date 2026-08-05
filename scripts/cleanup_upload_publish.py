#!/usr/bin/env python3
"""
Cleanup script: upload ready videos + publish uploaded_private videos.
Runs standalone — no API server needed.
"""
import sqlite3
import sys
import json
import time
from pathlib import Path
from datetime import datetime

# ── Setup ────────────────────────────────────────────────────────
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.settings import DATABASE_PATH
from database.db_extended import ExtendedDatabase
from orchestrator import PipelineOrchestrator

db = ExtendedDatabase()


def get_channel_slug(channel_id):
    ch = db.get_channel(channel_id)
    return ch["slug"] if ch else None


# ── Phase 1: Upload ready videos ────────────────────────────────
print("=" * 60)
print("PHASE 1: Subiendo videos 'ready' a YouTube")
print("=" * 60)

ready_videos = db.get_videos(status="ready")
print(f"Videos ready: {len(ready_videos)}")

for v in ready_videos:
    video_id = v["id"]
    vpath = v.get("video_path", "")
    channel_id = v.get("channel_id") or 1
    slug = get_channel_slug(channel_id) or v.get("canal") or ""
    titulo = v.get("titulo_final", "Video sin titulo")
    
    print(f"\n📹 Video #{video_id} [{slug}]: {titulo[:60]}...")
    
    if not vpath or not Path(vpath).exists():
        print(f"  ❌ Archivo no encontrado: {vpath}")
        db.update_video(video_id, status="missing_file", progress_phase="upload")
        continue
    
    # Parse tags
    tags_raw = v.get("tags_json", "[]")
    if isinstance(tags_raw, str):
        try:
            tags = json.loads(tags_raw)
        except json.JSONDecodeError:
            tags = []
    else:
        tags = tags_raw or []
    
    # Create orchestrator for this channel
    try:
        orch = PipelineOrchestrator(canal=slug, db_video_id=video_id)
        
        if not orch.uploader.authenticate():
            print(f"  ❌ Fallo autenticación para canal {slug}")
            db.update_video(video_id, status="error", progress_phase="auth_failed")
            continue
        
        print(f"  ⬆️  Subiendo {Path(vpath).name} ({Path(vpath).stat().st_size / 1e6:.0f} MB)...")
        
        # Determine publish mode — if the channel uses scheduled, do scheduled upload
        channel_config_json = (db.get_channel(channel_id) or {}).get("config_json", "{}")
        try:
            channel_config = json.loads(channel_config_json) if isinstance(channel_config_json, str) else channel_config_json
        except json.JSONDecodeError:
            channel_config = {}
        
        pub_mode = channel_config.get("PUBLISH_MODE", "immediate")
        
        if pub_mode == "scheduled":
            # Upload as private for scheduled publishing
            privacy = "private"
            db.update_video(video_id, publish_mode="scheduled", progress_phase="upload")
            print(f"  Modo: scheduled (se sube como privado)")
        else:
            privacy = v.get("privacy_status", "public")
        
        thumb = v.get("thumbnail_path")
        thumb_path = Path(thumb) if thumb else None
        
        # Sync upload
        def uploader_cb(pct):
            db.update_video(video_id, progress=30 + int(pct * 0.6), progress_phase="upload")
        
        try:
            result = orch.uploader.upload(
                video_path=Path(vpath),
                title=titulo,
                description=v.get("description", ""),
                tags=tags,
                thumbnail_path=thumb_path,
                privacy=privacy,
                progress_callback=uploader_cb,
            )
        except Exception as upload_err:
            print(f"  ❌ Error al subir: {upload_err}")
            db.update_video(video_id, status="error", progress_phase="upload")
            continue
        
        if not result or not result.get("video_id"):
            print(f"  ❌ Upload no devolvió video_id: {result}")
            db.update_video(video_id, status="error", progress_phase="upload")
            continue
        
        yt_id = result["video_id"]
        yt_url = result.get("url", f"https://youtube.com/watch?v={yt_id}")
        
        if pub_mode == "scheduled":
            db.mark_video_uploaded(video_id, yt_id, yt_url, status="uploaded_private")
            db.update_video(video_id, progress=100, status="uploaded_private", 
                            privacy_status="private")
            print(f"  ✅ Subido (privado): {yt_url}")
        else:
            db.mark_video_uploaded(video_id, yt_id, yt_url, status="uploaded")
            db.update_video(video_id, progress=100, status="uploaded", privacy_status=privacy)
            print(f"  ✅ Subido: {yt_url}")
        
        # Clean up local mp4 after successful upload
        if Path(vpath).exists():
            try:
                Path(vpath).unlink()
                db.update_video(video_id, video_path="")
                print(f"  🗑️  Mp4 local eliminado")
            except Exception:
                pass
        
    except Exception as e:
        print(f"  ❌ Error general: {e}")
        db.update_video(video_id, status="error", progress_phase="upload_error")
        continue


# ── Phase 2: Publish uploaded_private videos ────────────────────
print("\n" + "=" * 60)
print("PHASE 2: Publicando videos 'uploaded_private'")
print("=" * 60)

private_videos = db.get_videos(status="uploaded_private")
print(f"Videos uploaded_private: {len(private_videos)}")

for v in private_videos:
    video_id = v["id"]
    yt_video_id = v.get("yt_video_id")
    channel_id = v.get("channel_id") or 1
    slug = get_channel_slug(channel_id) or v.get("canal") or ""
    titulo = v.get("titulo_final", "Sin titulo")
    
    print(f"\n📺 Video #{video_id} [{slug}]: yt={yt_video_id} | {titulo[:60]}...")
    
    if not yt_video_id:
        print(f"  ❌ Sin YouTube ID — no se puede publicar")
        continue
    
    try:
        orch = PipelineOrchestrator(canal=slug, db_video_id=video_id)
        
        if not orch.uploader.authenticate():
            print(f"  ❌ Fallo autenticación para canal {slug}")
            continue
        
        print(f"  🔓 Cambiando privacidad a 'public'...")
        result = orch.uploader.set_privacy(yt_video_id, "public")
        
        if result:
            db.update_video(
                video_id, 
                status="published", 
                privacy_status="public",
                published_at=datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            )
            print(f"  ✅ Publicado: https://youtube.com/watch?v={yt_video_id}")
        else:
            print(f"  ❌ Fallo al cambiar privacidad: {result}")
        
    except Exception as e:
        print(f"  ❌ Error: {e}")


# ── Phase 3: Summary ────────────────────────────────────────────
print("\n" + "=" * 60)
print("RESUMEN FINAL")
print("=" * 60)

conn = sqlite3.connect(str(DATABASE_PATH))
cursor = conn.cursor()
cursor.execute("SELECT status, COUNT(*) FROM videos GROUP BY status ORDER BY COUNT(*) DESC")
for r in cursor.fetchall():
    print(f"  {r[0]:<20} {r[1]:>4}")
cursor.execute("SELECT COUNT(*) FROM videos")
print(f"  {'TOTAL':<20} {cursor.fetchone()[0]:>4}")
conn.close()

print("\n✓ Script completado")
