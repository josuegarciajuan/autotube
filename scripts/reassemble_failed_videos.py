#!/usr/bin/env python3
"""Reassemble all videos that failed with 'ensamblaje' (old-code regression).

Waits for the currently running long-form generation to finish, then
reassembles failed videos one at a time via POST /api/videos/{id}/reassemble,
polling each job to completion before starting the next.

The reassembly reuses the already-rendered scene segments on disk, so each
video only re-concatenates + re-muxes (~10-15 min) instead of a full render.

Usage: nohup python3 scripts/reassemble_failed_videos.py > logs/reassemble.log 2>&1 &
"""

import sys
import time
import urllib.request
import json
import sqlite3

sys.path.insert(0, "/root/autotube")

API = "http://localhost:8000"
DB_PATH = "/root/autotube/autotube.db"
POLL_SEC = 30
MAX_ATTEMPTS = 40  # per reassembly job (~40 * 30s = 20 min cap)

FAILED_IDS = list(range(2156, 2171))  # 2156..2170 inclusive


def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def active_longform_jobs() -> int:
    """Match the API's real concurrency guard (count_active_longform_jobs)."""
    with db() as c:
        row = c.execute(
            """SELECT COUNT(*) AS n FROM generation_jobs
               WHERE status IN ('running', 'queued')
                 AND action NOT IN ('generate_native_short', 'generate_clip_short', 'upload_only')"""
        ).fetchone()
    return row["n"] if row else 0


def wait_for_idle():
    print(f"[{time.strftime('%H:%M:%S')}] Esperando a que termine la generacion en curso...")
    while active_longform_jobs() > 0:
        time.sleep(POLL_SEC)
    print(f"[{time.strftime('%H:%M:%S')}] Sin generaciones activas — listo para reensamblar.")


def reassemble(video_id: int) -> bool:
    url = f"{API}/api/videos/{video_id}/reassemble"
    try:
        req = urllib.request.Request(url, method="POST")
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = json.loads(resp.read().decode())
        job_id = body.get("job_id")
        print(f"[{time.strftime('%H:%M:%S')}] Video #{video_id}: reassemble encolado (job {job_id})")
    except urllib.error.HTTPError as e:
        err = e.read().decode()[:200]
        print(f"[{time.strftime('%H:%M:%S')}] Video #{video_id}: HTTP {e.code} — {err}")
        return False
    except Exception as e:
        print(f"[{time.strftime('%H:%M:%S')}] Video #{video_id}: error encolando: {e}")
        return False

    # Poll the job until terminal
    attempts = 0
    while attempts < MAX_ATTEMPTS:
        time.sleep(POLL_SEC)
        attempts += 1
        with db() as c:
            row = c.execute(
                "SELECT status, substr(error_msg, 1, 160) AS err FROM generation_jobs WHERE id = ?",
                (job_id,),
            ).fetchone()
        if row is None:
            print(f"[{time.strftime('%H:%M:%S')}] Video #{video_id}: job {job_id} no encontrado en DB")
            return False
        st = row["status"]
        if st in ("completed", "done", "success"):
            print(f"[{time.strftime('%H:%M:%S')}] Video #{video_id}: ✅ reassembly OK")
            return True
        if st == "failed":
            print(f"[{time.strftime('%H:%M:%S')}] Video #{video_id}: ❌ reassembly fallido: {row['err']}")
            return False
    print(f"[{time.strftime('%H:%M:%S')}] Video #{video_id}: ⏱️ timeout esperando job {job_id}")
    return False


def main():
    wait_for_idle()
    # After idle, include 2171 if it ended in error (old code would have failed it)
    with db() as c:
        rows = c.execute(
            """SELECT id, status, progress_phase, error_message FROM videos
               WHERE id IN ({}) OR id = 2171
               ORDER BY id""".format(",".join("?" * len(FAILED_IDS))),
            list(FAILED_IDS),
        ).fetchall()
    candidates = []
    for r in rows:
        if r["status"] == "error":
            candidates.append(r["id"])
    print(f"[{time.strftime('%H:%M:%S')}] Videos a reensamblar: {candidates}")

    for vid in candidates:
        # Ensure no other long-form job grabbed the slot (scheduler may start)
        wait_for_idle()
        if not reassemble(vid):
            print(f"[{time.strftime('%H:%M:%S')}] Video #{vid}: reintento en 2 min...")
            time.sleep(120)
            wait_for_idle()
            reassemble(vid)

    print(f"[{time.strftime('%H:%M:%S')}] Reassembly batch terminado.")


if __name__ == "__main__":
    main()
