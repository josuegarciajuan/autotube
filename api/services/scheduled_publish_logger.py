"""Scheduled Publishing Event Logger.

Writes structured events about the scheduled-upload lifecycle to a
dedicated log file: `logs/scheduled_publishing.log`.

Events:
- "uploaded_scheduled": Video uploaded with YouTube native publishAt.
  YouTube will auto-publish the video at the scheduled time.
- "go_public": Video was set to public (legacy lifecycle action).
  Kept for backward compatibility with videos uploaded before publishAt migration.
- "upload_dispatched": Upload scheduler dispatched an upload job.

Usage:
    from api.services.scheduled_publish_logger import log_publish_event
    log_publish_event(event="uploaded_scheduled", slug="canal2",
                       uploaded_at="2026-07-31 18:45:00",
                       scheduled_for_utc="2026-07-31T19:00:00Z",
                       scheduled_for_local="31/07 21:00 Europe/Madrid",
                       ...)
    log_publish_event(event="go_public", slug="canal2", ...)

This makes it easy to audit the full timeline of scheduled publishing
with a single command:  tail -f logs/scheduled_publishing.log
"""

import logging
from pathlib import Path
from datetime import datetime

LOG_NAME = "autotube.scheduled_publish"
EVENT_LOG_FILE = Path("logs/scheduled_publishing.log")

_logger: logging.Logger | None = None


def _get_logger() -> logging.Logger:
    """Get or create the dedicated scheduled-publish logger (lazy init)."""
    global _logger
    if _logger is not None:
        return _logger

    _logger = logging.getLogger(LOG_NAME)
    _logger.setLevel(logging.INFO)
    _logger.propagate = True  # also send to root logger (api.log handler)

    # Dedicated file handler — atomic writes
    EVENT_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    fh = logging.FileHandler(EVENT_LOG_FILE, encoding="utf-8")
    fh.setLevel(logging.INFO)
    fh.setFormatter(logging.Formatter(
        "%(asctime)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))
    _logger.addHandler(fh)

    return _logger


def log_publish_event(event: str, slug: str, **kwargs) -> None:
    """Log a scheduled-publishing lifecycle event.

    Events:
      - "uploaded_scheduled": Video uploaded with YouTube native publishAt.
        Extra kwargs: uploaded_at, scheduled_for_utc, scheduled_for_local,
        db_video_id, video_title, yt_video_id, peak_hour, peak_source,
        warmup_min.
      - "go_public": Video was set to public via legacy lifecycle.
        Extra kwargs: db_video_id, video_title, yt_video_id, uploaded_at,
        target_public_at, actual_public_at, local_time.
      - "upload_dispatched": Upload scheduler dispatched an upload job.

    Extra kwargs are formatted as key=value in the log line.
    """
    logger = _get_logger()
    extra_parts = []
    for k, v in sorted(kwargs.items()):
        if v is not None:
            extra_parts.append(f"{k}={v}")
    extra_str = " | ".join(extra_parts) if extra_parts else ""

    # Tag for easy grep
    tag = {
        "uploaded_scheduled": "📤 SUBIDA PROGRAMADA",
        "go_public": "✅ PUBLICACIÓN",
        "upload_dispatched": "📤 DESPACHO",
    }.get(event, "📌 EVENTO")

    line = f"[{slug}] {tag} | {extra_str}" if extra_str else f"[{slug}] {tag}"
    logger.info(line)
