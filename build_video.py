#!/usr/bin/env python3
"""Quick video build for script 8 using existing assets."""
import sys, json, logging, shutil
sys.path.insert(0, '/root/autotube')
from pathlib import Path

logging.basicConfig(level=logging.WARNING, format='%(message)s')

from database.db import Database
from database.db_extended import migrate_v2
from config import canal2_config as cfg
from pipeline.tts_engine import TTSEngine
from pipeline.video_editor import VideoEditor
from pipeline.thumbnail_maker import ThumbnailMaker

db = Database()
migrate_v2()
script = db.get_script(8)

# Parse scenes
tts = TTSEngine({"voice": cfg.VOICE_ID, "rate": cfg.VOICE_RATE, "pitch": cfg.VOICE_PITCH, "volume": cfg.VOICE_VOLUME})
scenes = tts.parse_scenes(script['guion'])
print(f"Scenes: {len(scenes)}")

# Use existing processed images, split across scenes
all_images = sorted(Path('output/images').glob('processed_*'))
imgs_per_scene = max(1, len(all_images) // len(scenes))
image_paths = []
for i in range(len(scenes)):
    start = i * imgs_per_scene
    end = start + imgs_per_scene
    image_paths.append(all_images[start:end])
print(f"Images: {len(all_images)} total, ~{imgs_per_scene}/scene")

# Timestamps
ts_path = Path('output/audio/narration_1781549683_timestamps.json')
timestamps = json.loads(ts_path.read_text())
print(f"Timestamps: {len(timestamps)} words")

# Build video
audio_path = 'output/audio/narration_1781549683.mp3'
editor = VideoEditor(cfg)
video_path = editor.build_video(scenes, image_paths, audio_path, timestamps)
print(f"VIDEO: {video_path} ({video_path.stat().st_size/1024/1024:.1f} MB)")

# Thumbnail
titulo_opts = json.loads(script['titulo_options'])
titulo = titulo_opts[0]
maker = ThumbnailMaker(cfg)
thumb = maker.make_from_video_frame(video_path, titulo)
print(f"THUMB: {thumb}")

# Copy to web
web_dir = Path('/var/www/html/atupuerta/autotube/videos')
shutil.copy2(video_path, web_dir / video_path.name)
shutil.copy2(thumb, web_dir / thumb.name)
print(f"\nhttps://lamami.online/autotube/videos/{video_path.name}")
