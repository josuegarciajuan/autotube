#!/usr/bin/env python3
"""Phase 2: Upload remaining ready + publish all uploaded_private."""
import sys, json, time, argparse
from pathlib import Path
from datetime import datetime

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

parser = argparse.ArgumentParser(description="Legacy cleanup; dry-run by default")
parser.add_argument("--apply", action="store_true", help="explicitly allow upload/publication actions")
args = parser.parse_args()
if not args.apply:
    print("DRY-RUN/BLOCKED: cleanup_phase2 no ejecuta subidas ni publicaciones sin --apply")
    sys.exit(0)

from database.db_extended import ExtendedDatabase
from orchestrator import PipelineOrchestrator

db = ExtendedDatabase()

# ── Step 1: Upload remaining ready video (ID 471) ────────────────
print("=" * 60)
print("STEP 1: Upload video #471")
print("=" * 60)

v = db.get_video(471)
if v and v["status"] == "ready":
    ch = db.get_channel(v.get("channel_id") or 4)
    slug = ch["slug"] if ch else ""
    vpath = v.get("video_path", "")
    
    print(f"  [{slug}] {v.get('titulo_final', '')[:60]}")
    print(f"  Archivo: {vpath}")
    
    if not Path(vpath).exists():
        print(f"  ❌ No encontrado")
        db.update_video(471, status="missing_file")
    else:
        tags_raw = v.get("tags_json", "[]")
        tags = json.loads(tags_raw) if isinstance(tags_raw, str) else (tags_raw or [])
        
        orch = PipelineOrchestrator(canal=slug, db_video_id=471)
        if not orch.uploader.authenticate():
            print(f"  ❌ Auth failed")
        else:
            def cb(pct):
                db.update_video(471, progress=30 + int(pct * 0.6), progress_phase="upload")
            
            print(f"  ⬆️  Subiendo ({Path(vpath).stat().st_size / 1e6:.0f} MB)...")
            from api.services.publication_policy import upload_publication_kwargs
            publication = upload_publication_kwargs(
                publish_mode=str(v.get("publish_mode") or "immediate").lower(),
                target_public_at=v.get("target_public_at"),
            )
            result = orch.uploader.upload(
                video_path=Path(vpath),
                title=v.get("titulo_final", "Sin titulo"),
                description=v.get("description", ""),
                tags=tags,
                thumbnail_path=Path(v["thumbnail_path"]) if v.get("thumbnail_path") else None,
                **publication,
                progress_callback=cb,
            )
            
            if result and result.get("video_id"):
                yt_id = result["video_id"]
                yt_url = result.get("url", f"https://youtube.com/watch?v={yt_id}")
                db.mark_video_uploaded(471, yt_id, yt_url)
                db.update_video(471, progress=100, status="uploaded")
                print(f"  ✅ {yt_url}")
                
                if Path(vpath).exists():
                    Path(vpath).unlink()
                    db.update_video(471, video_path="")
                    print(f"  🗑️  mp4 eliminado")
            else:
                print(f"  ❌ Upload failed: {result}")
else:
    print(f"  Video 471 status={v['status'] if v else 'NOT FOUND'} — skipping")


# ── Step 2: Publish all uploaded_private ────────────────────────
print("\n" + "=" * 60)
print("STEP 2: Publish uploaded_private videos")
print("=" * 60)

private = db.get_videos(status="uploaded_private")
print(f"Videos to publish: {len(private)}")

success, fail = 0, 0

for v in private:
    vid = v["id"]
    yt = v.get("yt_video_id")
    ch = db.get_channel(v.get("channel_id") or 1)
    slug = ch["slug"] if ch else v.get("canal", "")
    
    if not yt:
        print(f"  #{vid}: ❌ no yt_video_id")
        fail += 1
        continue
    
    print(f"  #{vid} [{slug}] yt={yt} → public...", end=" ", flush=True)
    
    try:
        orch = PipelineOrchestrator(canal=slug, db_video_id=vid)
        if not orch.uploader.authenticate():
            print("❌ auth")
            fail += 1
            continue
        
        from api.services.publication_policy import validate_manual_publication
        validate_manual_publication(
            publish_mode=str(v.get("publish_mode") or "immediate").lower(),
            target_public_at=v.get("target_public_at"),
        )
        orch.uploader.set_privacy(yt, "public")
        db.update_video(vid, status="published", privacy_status="public",
                         published_at=datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
        print("✅")
        success += 1
    except Exception as e:
        print(f"❌ {e}")
        fail += 1

print(f"\nPublished: {success} | Failed: {fail}")


# ── Summary ─────────────────────────────────────────────────────
print("\n" + "=" * 60)
print("FINAL STATUS")
print("=" * 60)

import sqlite3
conn = sqlite3.connect("autotube.db")
c = conn.cursor()
c.execute("SELECT status, COUNT(*) FROM videos GROUP BY status ORDER BY COUNT(*) DESC")
for r in c.fetchall():
    print(f"  {r[0]:<20} {r[1]:>4}")
c.execute("SELECT COUNT(*) FROM videos")
print(f"  {'TOTAL':<20} {c.fetchone()[0]:>4}")
conn.close()
print("\n✓ Done")
