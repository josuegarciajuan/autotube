#!/usr/bin/env python3
"""Reanuda de uno en uno los vídeos que quedaron en 'error' por fallo de TTS.

Tras arreglar el motor TTS (edge-tts 7.3.1 + fallback a Kokoro), los vídeos que
murieron en la fase ``tts`` conservan en su checkpoint el scrape y el guion, así
que se recuperan con ``POST /api/videos/{id}/resume`` (reanuda desde la última
fase completada → rehace TTS).

Este script serializa los resumes para NO chocar con el guardia de concurrencia
long-form: solo lanza uno cuando ``count_running_longform_jobs() < límite``
efectivo y el canal no tiene ya un job activo. Es idempotente: salta los vídeos
que ya no están en 'error'.

Uso:
    python3 scripts/recover_failed_tts_videos.py 2642 2644 2645 2646 2647
"""
from __future__ import annotations

import argparse
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.settings import effective_max_concurrent_longform_jobs  # noqa: E402
from database.db_extended import ExtendedDatabase  # noqa: E402

API = "http://127.0.0.1:8000"


def _ts() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _post_resume(video_id: int) -> tuple[int | None, str]:
    req = urllib.request.Request(f"{API}/api/videos/{video_id}/resume", method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")[:200]
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")[:200]
    except Exception as exc:  # noqa: BLE001 — reportar y reintentar en el siguiente tick
        return None, str(exc)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("ids", nargs="+", type=int, help="IDs de vídeos en 'error' por TTS")
    ap.add_argument("--max-hours", type=float, default=12.0)
    ap.add_argument("--poll", type=int, default=90, help="segundos entre comprobaciones")
    args = ap.parse_args()

    db = ExtendedDatabase()
    deadline = time.time() + args.max_hours * 3600
    pending = list(dict.fromkeys(args.ids))
    print(f"[{_ts()}] recovery start: {pending}", flush=True)

    while pending and time.time() < deadline:
        progressed = False
        for vid in list(pending):
            v = db.get_video(vid)
            if not v:
                print(f"[{_ts()}] v{vid}: no existe — descartado", flush=True)
                pending.remove(vid)
                continue
            if v.get("yt_video_id"):
                print(f"[{_ts()}] v{vid}: ya subido — descartado", flush=True)
                pending.remove(vid)
                continue
            if v.get("status") != "error":
                print(f"[{_ts()}] v{vid}: status={v.get('status')} — reanudado/listo", flush=True)
                pending.remove(vid)
                continue

            limit = effective_max_concurrent_longform_jobs()
            running = db.count_running_longform_jobs()
            ch_active = v.get("channel_id") and db.get_active_job_for_channel(v["channel_id"])
            if running >= limit or ch_active:
                print(
                    f"[{_ts()}] v{vid}: esperando hueco "
                    f"(running={running}/{limit}, canal_activo={bool(ch_active)})",
                    flush=True,
                )
                continue

            code, body = _post_resume(vid)
            print(f"[{_ts()}] v{vid}: resume -> HTTP {code} {body}", flush=True)
            if code == 200:
                pending.remove(vid)
                progressed = True

        if pending:
            time.sleep(args.poll)
        elif progressed:
            pass

    if pending:
        print(f"[{_ts()}] timeout — pendientes: {pending}", flush=True)
        return 1
    print(f"[{_ts()}] recovery done", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
