#!/usr/bin/env python3
"""Watch reassemble_57.log, update both videos + generation_jobs tables.

This makes the frontend global progress bar (GenerationProgressBar) pick up
the job status automatically via the polling fallback (GET /api/jobs/{id}).
"""

import sqlite3
import time
import re
import os

LOG_FILE = "/root/autotube/logs/reassemble_57.log"
DB_FILE = "/root/autotube/autotube.db"
VIDEO_ID = 57
JOB_ID = None  # created on first run

conn = sqlite3.connect(DB_FILE)

# ── Create generation_job if not exists ──────────────────────
existing = conn.execute(
    "SELECT id FROM generation_jobs WHERE video_id=? AND action='reassemble' AND status='running'",
    (VIDEO_ID,),
).fetchone()

if existing:
    JOB_ID = existing[0]
    print(f"Using existing job #{JOB_ID}")
else:
    # Get channel_id from video
    ch = conn.execute("SELECT channel_id, canal FROM videos WHERE id=?", (VIDEO_ID,)).fetchone()
    channel_id = ch[0] if ch and ch[0] else 3
    conn.execute(
        "INSERT INTO generation_jobs (channel_id, video_id, action, status, progress, phase) VALUES (?,?,?,?,?,?)",
        (channel_id, VIDEO_ID, "reassemble", "running", 78, "video"),
    )
    JOB_ID = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    print(f"Created job #{JOB_ID}")

conn.execute(
    "UPDATE videos SET status='reassembling', progress=78, progress_phase='video' WHERE id=?",
    (VIDEO_ID,),
)
conn.commit()
print(f"DB: video {VIDEO_ID} -> reassembling/78%, job {JOB_ID}")

conn.close()

# ── Main loop ────────────────────────────────────────────────
while True:
    try:
        if not os.path.exists(LOG_FILE):
            time.sleep(30)
            continue

        with open(LOG_FILE) as f:
            content = f.read()

        # Completion
        if "SUCCESS: Video rendered" in content:
            conn = sqlite3.connect(DB_FILE)
            conn.execute("UPDATE videos SET status='ready', progress=100, progress_phase=NULL WHERE id=?", (VIDEO_ID,))
            conn.execute("UPDATE generation_jobs SET status='completed', progress=100, phase='done' WHERE id=?", (JOB_ID,))
            conn.commit()
            conn.close()
            print("✅ Render complete — video ready, job completed")
            break

        # Fatal error
        if "RuntimeError:" in content or "Traceback (most recent call last)" in content:
            conn = sqlite3.connect(DB_FILE)
            conn.execute("UPDATE videos SET status='error', progress=0, progress_phase='video' WHERE id=?", (VIDEO_ID,))
            conn.execute("UPDATE generation_jobs SET status='failed', phase='video' WHERE id=?", (JOB_ID,))
            conn.commit()
            conn.close()
            print("⚠️  Error detected — job marked failed")
            # Don't break — the standalone script might have retry logic

        # Extract frame_index % from MoviePy output
        matches = re.findall(r"frame_index:\s+(\d+)%", content)
        if matches:
            frame_pct = int(matches[-1])
            overall = 60 + int(frame_pct * 0.35)  # 60-95 range
            conn = sqlite3.connect(DB_FILE)
            conn.execute("UPDATE videos SET progress=? WHERE id=?", (overall, VIDEO_ID))
            conn.execute("UPDATE generation_jobs SET progress=?, phase='video' WHERE id=?", (overall, JOB_ID))
            conn.commit()
            conn.close()

        time.sleep(30)

    except Exception as e:
        print(f"Watcher error: {e}")
        time.sleep(30)
