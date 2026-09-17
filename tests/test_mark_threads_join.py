"""Tests de T2.4: el worker espera al marcado IA antes de salir."""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from api.services import full_pipeline_worker as fpw  # noqa: E402


def test_join_waits_for_pending_mark_thread():
    fpw._PENDING_MARK_THREADS.clear()
    done: list[int] = []
    t = threading.Thread(target=lambda: (time.sleep(0.15), done.append(1)))
    t.start()
    fpw._PENDING_MARK_THREADS.append(t)
    fpw._join_pending_mark_threads(timeout_sec=5)
    assert done == [1]
    fpw._PENDING_MARK_THREADS.clear()


def test_join_respects_timeout():
    fpw._PENDING_MARK_THREADS.clear()
    release = threading.Event()
    t = threading.Thread(target=release.wait)
    t.start()
    started = time.monotonic()
    fpw._join_pending_mark_threads(timeout_sec=0.2)
    elapsed = time.monotonic() - started
    release.set()
    t.join(timeout=2)
    fpw._PENDING_MARK_THREADS.clear()
    assert elapsed < 1.5, "el join debe respetar el timeout y no colgarse"
