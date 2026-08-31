#!/usr/bin/env python3
"""DEPRECADO — no usar.

La recuperación de videos fallidos es responsabilidad de
``api.services.generation_service.auto_recover_on_startup()`` (crea jobs
``reassemble`` en 'queued' que el ``_queue_consumer`` global procesa de uno en
uno). Este script standalone compite con ese mecanismo: su ``wait_for_idle()``
cuenta los jobs encolados como "activos" y se cuelga esperando, y sus jobs
colisionan con los del auto-recovery en cada reinicio de la API.

Riesgos de ejecutarlo:
- Se queda en bucle infinito en ``wait_for_idle()`` mientras existan jobs
  encolados (contaba 'queued' como activos).
- Un reinicio de la API mata su job en curso ("Server restarted...") y el
  auto-recovery crea jobs duplicados para los mismos videos.

Si necesitas reensamblar videos manualmente, usa:
    POST /api/videos/{id}/reassemble
y espera a que el job termine antes del siguiente.
"""

import sys

sys.exit(0)  # DEPRECADO — ver docstring. auto_recover_on_startup + queue consumer.

# ── Código legacy (no ejecutado) ──────────────────────────────────
import time
import urllib.request
import json
import sqlite3

from pathlib import Path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
from config.settings import DATABASE_PATH

API = "http://localhost:8000"
DB_PATH = DATABASE_PATH
POLL_SEC = 60
# A reassembly re-renders 100-170 scenes (40-90 min), so the poll cap must
# be much larger than the old 20 min. 120 * 60s = 2h ceiling per job.
MAX_ATTEMPTS = 120


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
    # Discover failed videos dynamically (assembly-phase errors, with a
    # checkpoint so they can actually be reassembled). No hardcoded IDs.
    with db() as c:
        rows = c.execute(
            """SELECT v.id
               FROM videos v
               WHERE v.status = 'error'
                 AND v.progress_phase = 'video'
                 AND v.checkpoint_data IS NOT NULL
               ORDER BY v.id"""
        ).fetchall()
    candidates = [r["id"] for r in rows]
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
