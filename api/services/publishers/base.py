"""Abstract base class and registry for API-based video publishers."""

import importlib
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)

# ── Registry ──────────────────────────────────────────────────

_PUBLISHERS: dict[str, "AbstractVideoPublisher"] = {}

_MODULE_MAP = {
    "facebook": "api.services.publishers.facebook_video",
    "rumble": "api.services.publishers.rumble",
    "tiktok": "api.services.publishers.tiktok_api",
    "dailymotion": "api.services.publishers.dailymotion",
}


def register_publisher(platform: str, publisher: "AbstractVideoPublisher"):
    """Register a concrete publisher (called at import time)."""
    _PUBLISHERS[platform] = publisher
    logger.debug("Registered publisher: %s", platform)


def get_publisher(platform: str) -> "AbstractVideoPublisher":
    """Get a publisher instance, lazy-loading if needed."""
    if platform not in _PUBLISHERS and platform in _MODULE_MAP:
        try:
            importlib.import_module(_MODULE_MAP[platform])
        except ImportError as exc:
            logger.warning("Cannot load publisher module for %s: %s", platform, exc)
            raise
    if platform not in _PUBLISHERS:
        raise ValueError(f"Unknown publisher platform: {platform}")
    return _PUBLISHERS[platform]


# ── Data Classes ──────────────────────────────────────────────


@dataclass
class VideoMetadata:
    """Metadata for a video to be uploaded to a platform."""
    video_path: str
    title: str
    description: str = ""
    tags: list[str] = field(default_factory=list)
    thumbnail_path: Optional[str] = None
    category: str = ""
    language: str = "es"
    privacy: str = "public"
    schedule_at: Optional[str] = None       # ISO 8601 UTC
    cross_reference_yt: bool = False         # include YT link in description
    yt_video_url: Optional[str] = None       # YouTube URL for cross-ref


@dataclass
class UploadResult:
    """Result of a platform video upload."""
    success: bool
    platform: str = ""
    platform_video_id: Optional[str] = None
    platform_video_url: Optional[str] = None
    status: str = "pending"                  # processing | published | failed
    error: Optional[str] = None


# ── Abstract Base Class ───────────────────────────────────────


class AbstractVideoPublisher(ABC):
    """Abstract base for API-based video publishers.

    Each concrete implementation handles:
    - Authentication (OAuth, API key, etc.)
    - Resumable video upload
    - Status polling
    - Optional metadata updates
    """

    platform: str = "__base__"
    _MAX_RETRIES = 5
    _RETRY_BASE_DELAY = 2  # seconds → exponential backoff: 2, 4, 8, 16, 32

    def __init__(self):
        self._encryption = None

    def _get_encryption(self):
        """Lazy-load credential encryption."""
        if self._encryption is None:
            from pipeline.social_encryption import get_encryption
            self._encryption = get_encryption()
        return self._encryption

    def _get_credentials(self, channel_id: int) -> dict | None:
        """Load credentials from channel_social_accounts table."""
        from database.db_extended import ExtendedDatabase
        db = ExtendedDatabase()
        acct = db.get_enabled_social_accounts(channel_id)
        for a in acct:
            if a["platform"] == self.platform:
                return a
        return None

    def _decrypt_password(self, encrypted: str) -> str:
        """Decrypt stored credential."""
        return self._get_encryption().decrypt(encrypted) if encrypted else ""

    @abstractmethod
    async def upload(self, metadata: VideoMetadata,
                     progress_cb=None) -> UploadResult:
        """Upload a video to the platform. Must be implemented by subclasses."""
        ...

    async def get_status(self, platform_video_id: str) -> dict:
        """Get current processing/publishing status. Override per platform."""
        return {"status": "unknown"}

    async def update_metadata(self, platform_video_id: str,
                              title: str = None, description: str = None,
                              tags: list[str] = None) -> bool:
        """Update video metadata after upload. Optional override."""
        return False

    async def get_stats(self, platform_video_id: str) -> dict:
        """Get video analytics/stats. Optional override."""
        return {}
