#!/usr/bin/env python3
"""Shorts worker — standalone process for short video generation.

Executed as an independent subprocess so API restarts (and the event loop)
are NOT blocked by in-process short generation (ffmpeg, TTS, LLM calls).

The worker communicates progress back to the database; the API polls the DB.

Usage (spawned by API):
    python3 api/services/shorts_worker.py \
        --channel-id 6 --channel-slug test --slot-id 24200 \
        --job-id 5000 [--native] [--clip] [--slot-rank 0]

Design principles:
  - Survives parent process death (start_new_session=True)
  - All state is persisted to DB — nothing in memory is critical
  - Progress is written to the shorts table for API polling
  - Exits cleanly on SIGTERM, killed on SIGKILL
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import time
import traceback
from pathlib import Path

# Ensure project root on sys.path
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


def _setup_logging(job_id: int | None = None, channel_slug: str = "unknown"):
    """Configure logging to stderr (captured by systemd journal)."""
    logger = logging.getLogger("autotube.shorts_worker")
    logger.setLevel(logging.DEBUG)

    # Stderr handler — captured by systemd journal via subprocess.STDOUT
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] shorts_worker(%(channel)s): %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))
    # Add channel context to all log records
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
    log = logging.getLogger("autotube.shorts_worker")
    log.warning("Received SIGTERM — attempting graceful shutdown...")
    # Give the current phase a moment to wrap up
    _shutdown_requested = True


def main():
    parser = argparse.ArgumentParser(description="Shorts generation worker")
    parser.add_argument("--channel-id", type=int, required=True)
    parser.add_argument("--channel-slug", type=str, required=True)
    parser.add_argument("--slot-id", type=int, default=0)
    parser.add_argument("--job-id", type=int, default=0)
    parser.add_argument("--native", action="store_true", help="Generate native short")
    parser.add_argument("--clip", action="store_true", help="Generate clip short")
    parser.add_argument("--slot-rank", type=int, default=0, help="Slot rank (0=top priority)")
    args = parser.parse_args()

    log = _setup_logging(job_id=args.job_id, channel_slug=args.channel_slug)
    log.info(
        "Shorts worker started: channel=%s (id=%d) slot=%d type=%s job=%d",
        args.channel_slug, args.channel_id, args.slot_id,
        "native" if args.native else "clip", args.job_id,
    )

    # Register signal handlers
    signal.signal(signal.SIGTERM, _handle_sigterm)

    try:
        if args.native:
            from api.services.shorts_scheduler import _dispatch_native_short
            short_id = _dispatch_native_short(
                channel_id=args.channel_id,
                channel_slug=args.channel_slug,
                slot_rank=args.slot_rank,
                job_id=args.job_id,
            )
            if short_id:
                log.info("Native short generated: short_id=%d", short_id)
            else:
                log.error("Native short generation FAILED (returned None)")
                sys.exit(1)
        elif args.clip:
            from api.services.shorts_scheduler import _dispatch_clip_short
            result = _dispatch_clip_short(
                channel_id=args.channel_id,
                channel_slug=args.channel_slug,
                slot_rank=args.slot_rank,
                job_id=args.job_id,
            )
            if result:
                log.info("Clip short generated: short_id=%d", result)
            else:
                log.error("Clip short generation FAILED (returned None)")
                sys.exit(1)
        else:
            log.error("No --native or --clip flag provided")
            parser.print_help()
            sys.exit(1)

    except Exception as exc:
        log.error("Shorts worker failed: %s", exc)
        log.debug("Traceback:\n%s", traceback.format_exc())
        sys.exit(1)

    log.info("Shorts worker exiting normally")
    sys.exit(0)


if __name__ == "__main__":
    main()
