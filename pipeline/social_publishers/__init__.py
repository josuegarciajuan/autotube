"""Social media platform publishers.

Each platform has a publisher class that handles login + content publishing
via Playwright browser automation.
"""

from pipeline.social_publishers.base import SocialPlatform, get_publisher  # noqa: F401
