"""API-based video publishers for cross-platform distribution.

Unlike pipeline/social_publishers/ (Playwright browser automation for text posts),
these publishers use platform REST APIs to upload full videos for monetization.

Platforms:
    - Facebook: Graph API v18+ video upload (In-Stream Ads) + Reels
    - Rumble: Upload API (Rumble Player revenue, licensing)
    - TikTok: Content Posting API (primary) + Playwright fallback
"""

from api.services.publishers.base import (
    AbstractVideoPublisher,
    VideoMetadata,
    UploadResult,
    get_publisher,
    register_publisher,
)
from api.services.publishers.platform_manager import PlatformPublishManager

__all__ = [
    "AbstractVideoPublisher",
    "VideoMetadata",
    "UploadResult",
    "get_publisher",
    "register_publisher",
    "PlatformPublishManager",
]
