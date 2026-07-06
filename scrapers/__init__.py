"""Scrapers package — auto-registers all scraper plugins on import."""

from scrapers.base import BaseScraper, SCRAPER_REGISTRY, register_scraper, get_scraper

# Import scrapers to trigger @register_scraper decorators
from scrapers.reddit import RedditScraper          # noqa: F401
from scrapers.wikipedia import WikipediaScraper    # noqa: F401
from scrapers.quora import QuoraScraper            # noqa: F401
from scrapers.atlas_obscura import AtlasObscuraScraper  # noqa: F401
from scrapers.rss_scraper import RSSScraper        # noqa: F401
from scrapers.google_news import GoogleNewsScraper # noqa: F401

__all__ = [
    "BaseScraper",
    "SCRAPER_REGISTRY",
    "register_scraper",
    "get_scraper",
    "RedditScraper",
    "WikipediaScraper",
    "QuoraScraper",
    "AtlasObscuraScraper",
    "RSSScraper",
    "GoogleNewsScraper",
]
