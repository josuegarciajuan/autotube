"""Scheduled Publishing Event Logger.

Writes structured events about the scheduled-upload → warmup → go_public
lifecycle to a dedicated log file: `logs/scheduled_publishing.log`.

Usage:
    from api.services.scheduled_publish_logger import log_publish_event
    log_publish_event(event="uploaded_private", slug="canal2", ...)
    log_publish_event(event="go_public", slug="canal2", ...)

This makes it easy to check the full timeline of scheduled publishing
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
      - "uploaded_private": Video was uploaded as private, awaiting warmup.
      - "go_public": Video was set to public at the scheduled peak time.
      - "upload_dispatched": Upload scheduler dispatched an upload job.

    Extra kwargs are formatted as key=value in the log line.
    """
    logger = _get_logger()
    extra_parts = []
    for k, v in sorted(kwargs.items()):
        if v is not None:
            extra_parts.append(f"{k}={v}")
    extra_str = " | ".join(extra_parts) if extra_parts else ""

    # Tag: UPLOAD or PUBLISH for easy grep
    tag = {
        "uploaded_private": "📤 SUBIDA",
        "go_public": "✅ PUBLICACIÓN",
        "upload_dispatched": "📤 DESPACHO",
    }.get(event, "📌 EVENTO")

    line = f"[{slug}] {tag} | {extra_str}" if extra_str else f"[{slug}] {tag}"
    logger.info(line)
