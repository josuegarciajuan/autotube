"""Base class for social media platform publishers.

Each platform extends SocialPlatform and implements:
- login(page, username, password) → bool
- publish(page, content) → str (post URL)
"""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class SocialContent:
    """Content to be published on a social platform."""
    platform: str
    text: str                          # main caption / tweet text
    media_path: str = ""               # path to video/image file
    yt_url: str = ""                   # YouTube video URL for cross-linking
    thread_parts: list[str] = field(default_factory=list)  # for Twitter threads
    hashtags: list[str] = field(default_factory=list)
    scheduled_delay_min: int = 0       # how long after go_public this posts


# ── SocialPlatform ───────────────────────────────────────


class SocialPlatform(ABC):
    """Abstract base for a social media platform publisher.

    Subclasses implement platform-specific login and publish logic
    using Playwright Page objects.
    """

    platform: str = "__base__"

    @abstractmethod
    async def login(self, page, username: str, password: str) -> bool:
        """Log into the platform using browser automation.

        Args:
            page: Playwright Page object (already navigated or blank).
            username: Platform username/email.
            password: Platform password.

        Returns:
            True if login succeeded, False otherwise.
        """
        ...

    @abstractmethod
    async def publish(self, page, content: SocialContent) -> str:
        """Publish content to the platform.

        Args:
            page: Playwright Page object (already authenticated).
            content: Content to publish (text, media, etc.).

        Returns:
            URL of the published post on success, empty string on failure.
        """
        ...

    async def validate_login(self, page, username: str) -> bool:
        """Check if the current session is still logged in as the expected user.

        Default implementation navigates to the platform home page and looks
        for a logged-in indicator. Override for platform-specific checks.
        """
        return False  # Subclasses must override or use cookies

    @staticmethod
    def human_delay(page, min_ms: int = 300, max_ms: int = 2000):
        """Helper for adding human-like delays between actions."""
        import asyncio, random
        return asyncio.sleep(random.uniform(min_ms, max_ms) / 1000.0)


# ── Publisher registry ─────────────────────────────────────

_PUBLISHERS: dict[str, SocialPlatform] = {}


def register_publisher(platform: str, cls: type[SocialPlatform]):
    """Register a platform publisher class."""
    _PUBLISHERS[platform] = cls()
    logger.debug("Registered social publisher: %s", platform)


def get_publisher(platform: str) -> SocialPlatform:
    """Get a registered publisher by platform name.

    Lazily imports and registers platform publishers on first call.
    """
    if platform not in _PUBLISHERS:
        _lazy_load(platform)
    if platform not in _PUBLISHERS:
        raise ValueError(f"No publisher registered for platform '{platform}'")
    return _PUBLISHERS[platform]


def _lazy_load(platform: str):
    """Lazy-import and register a platform publisher module."""
    try:
        import importlib
        mod = importlib.import_module(f"pipeline.social_publishers.{platform}_publisher")
        # The module should call register_publisher() at import time
        logger.debug("Lazy-loaded publisher module for %s", platform)
    except ImportError:
        logger.warning("No publisher module found for platform '%s'", platform)
    except Exception as exc:
        logger.error("Error loading publisher for '%s': %s", platform, exc)
