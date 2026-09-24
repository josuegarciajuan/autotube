#!/usr/bin/env python3
"""Shorts worker — standalone process for short video generation.

Executed as an independent subprocess so API restarts (and the event loop)
are NOT blocked by in-process short generation (ffmpeg, TTS, LLM calls), and so
the deploy gate (`scripts/deploy_safety.py`) does not abort while a short is
rendering. The worker owns the WHOLE lifecycle: generation + finalization
(link slot, statuses, retries, defer, alerts).

The worker communicates progress back to the database; the API polls the DB.

Usage (spawned by the API via ``_spawn_short_worker``):
    python3 api/services/shorts_worker.py \
        --channel-id 7 --channel-slug canal5 --job-id 11072 --slot-id 0 \
        --standalone --generate-only

Design principles:
  - Survives parent process death (start_new_session=True)
  - All state is persisted to DB — nothing in memory is critical
  - Writes worker_pid + heartbeat so the API/orphan-detector sees it as alive
  - Exits cleanly on SIGTERM
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import threading
import time
import traceback
from pathlib import Path

# Ensure project root on sys.path
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

logger = logging.getLogger("autotube.shorts_worker")
_shutdown_requested = False


def _setup_logging(job_id: int | None = None, channel_slug: str = "unknown"):
    """Configure logging to stderr (captured to a per-job log by the spawner)."""
    logger.setLevel(logging.DEBUG)
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] shorts_worker(%(channel)s): %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))
    old_factory = logging.getLogRecordFactory()

    def _record_factory(*args, **kwargs):
        record = old_factory(*args, **kwargs)
        record.channel = channel_slug
        return record

    logging.setLogRecordFactory(_record_factory)
    logger.handlers.clear()
    logger.addHandler(handler)
    logger.propagate = False
    return logger


def _handle_sigterm(signum, frame):
    """Graceful shutdown on SIGTERM."""
    global _shutdown_requested
    logging.getLogger("autotube.shorts_worker").warning(
        "Received SIGTERM — attempting graceful shutdown..."
    )
    _shutdown_requested = True


def _init_db():
    from database.db import init_db
    from database.db_extended import migrate_v2, ExtendedDatabase
    init_db()
    migrate_v2()
    return ExtendedDatabase()


def _start_heartbeat(db, job_id: int) -> threading.Event:
    """Pulse ``last_heartbeat_at`` so the orphan detector doesn't kill the worker.

    Self-terminates (hard) if the job row disappears (ghost-worker guard).
    """
    stop = threading.Event()

    def _pulse():
        while not stop.is_set():
            try:
                row = db.get_job(job_id)
                if row is None:
                    os._exit(1)
                db.update_heartbeat(job_id)
            except Exception:
                pass  # best-effort
            stop.wait(30)

    threading.Thread(target=_pulse, daemon=True, name="shorts-heartbeat").start()
    return stop


def _dispatch(args) -> "int | None":
    """Run the requested short generation. Returns short_id or None."""
    from api.services.shorts_scheduler import (
        _dispatch_native_short, _dispatch_clip_short, _dispatch_standalone_short,
    )

    if args.standalone:
        logger.info("dispatching standalone short: channel=%s job=%d",
                    args.channel_slug, args.job_id)
        return _dispatch_standalone_short(
            args.channel_id, args.channel_slug,
            slot_rank=args.slot_rank, job_id=args.job_id,
            target_upload_at=args.target_upload_at,
            generate_only=args.generate_only,
        )
    if args.clip:
        logger.info("dispatching clip short: channel=%s job=%d source_video=%s",
                    args.channel_slug, args.job_id, args.source_video_id)
        return _dispatch_clip_short(
            args.channel_id, args.channel_slug, args.source_video_id,
            slot_rank=args.slot_rank, job_id=args.job_id,
            pre_rendered_short_id=args.pre_rendered_short_id,
            target_upload_at=args.target_upload_at,
            generate_only=args.generate_only,
        )
    logger.info("dispatching native short: channel=%s job=%d slot=%d",
                args.channel_slug, args.job_id, args.slot_id)
    return _dispatch_native_short(
        args.channel_id, args.channel_slug,
        slot_rank=args.slot_rank, job_id=args.job_id,
        target_upload_at=args.target_upload_at,
        generate_only=args.generate_only,
        slot_id=args.slot_id or None,
    )


def main():
    parser = argparse.ArgumentParser(description="Shorts generation worker")
    parser.add_argument("--channel-id", type=int, required=True)
    parser.add_argument("--channel-slug", type=str, required=True)
    parser.add_argument("--slot-id", type=int, default=0)
    parser.add_argument("--job-id", type=int, default=0)
    parser.add_argument("--slot-rank", type=int, default=0, help="Slot rank (0=top priority)")
    parser.add_argument("--native", action="store_true", help="Generate native short")
    parser.add_argument("--clip", action="store_true", help="Generate clip short")
    parser.add_argument("--standalone", action="store_true", help="Generate standalone short")
    parser.add_argument("--source-video-id", type=int, default=None,
                        help="Source long-form video id (clip shorts)")
    parser.add_argument("--pre-rendered-short-id", type=int, default=None,
                        help="Pre-rendered clip short id (skip render)")
    parser.add_argument("--target-upload-at", type=str, default=None)
    parser.add_argument("--generate-only", action="store_true",
                        help="Render only; leave the short in the queue (no upload)")
    args = parser.parse_args()

    log = _setup_logging(job_id=args.job_id, channel_slug=args.channel_slug)
    short_type = "standalone" if args.standalone else ("clip" if args.clip else "native")
    log.info(
        "Shorts worker started: channel=%s (id=%d) slot=%d type=%s job=%d",
        args.channel_slug, args.channel_id, args.slot_id, short_type, args.job_id,
    )

    signal.signal(signal.SIGTERM, _handle_sigterm)

    db = _init_db()
    job_id = args.job_id

    # Mark running + publish worker_pid so the API and deploy gate see it as a
    # live subprocess (survives API restart). Heartbeat keeps it from being
    # reaped as an orphan.
    heartbeat_stop = threading.Event()
    if job_id:
        try:
            db.update_job(job_id, status="running", worker_pid=os.getpid(),
                          pipeline_phase="shorts")
            heartbeat_stop = _start_heartbeat(db, job_id)
        except Exception as exc:
            log.warning("could not register worker_pid/heartbeat: %s", exc)

    short_id = None
    exc = None
    try:
        short_id = _dispatch(args)
        if short_id:
            log.info("Short generated: short_id=%s", short_id)
        else:
            log.error("Short generation FAILED (returned None)")
    except Exception as e:  # noqa: BLE001 - we finalize with the exception
        exc = e
        log.error("Shorts worker failed: %s", e)
        log.debug("Traceback:\n%s", traceback.format_exc())
    finally:
        # Finalize in-process too (worker owns the lifecycle). Safe even if the
        # dispatcher already wrote statuses — finalize is idempotent on success.
        if job_id:
            try:
                from api.services.shorts_scheduler import _finalize_short_dispatch
                _finalize_short_dispatch(
                    args.slot_id or None, job_id, args.channel_id,
                    short_id=short_id, exc=exc, generate_only=args.generate_only,
                )
            except Exception as _fin_exc:
                log.error("could not finalize short dispatch: %s", _fin_exc)
        heartbeat_stop.set()
        try:
            import gc
            gc.collect()
        except Exception:
            pass

    log.info("Shorts worker exiting (success=%s)", bool(short_id))
    sys.exit(0 if short_id else 1)


if __name__ == "__main__":
    main()
