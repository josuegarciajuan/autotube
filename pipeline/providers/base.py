"""Abstract base class and data model for video providers."""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


@dataclass
class VideoAsset:
    """Represents a discovered video clip ready for download.

    Attributes:
        url: Public page URL of the video (for attribution).
        file_path: Local filesystem path after download (set by download()).
        duration: Video duration in seconds.
        resolution: (width, height) in pixels.
        provider: Name of the provider that sourced this asset.
    """
    url: str
    file_path: Path
    duration: float
    resolution: tuple  # (width, height)
    provider: str


@dataclass
class SearchPage:
    """Paginated search results from a video provider.

    Attributes:
        assets: List of VideoAsset candidates on this page.
        page: Current page number (1-indexed).
        per_page: Results per page requested.
        total_available: Total results accessible via the API (e.g. totalHits,
                         max 500 for Pixabay). Use 0 if unknown.
    """
    assets: list[VideoAsset] = field(default_factory=list)
    page: int = 1
    per_page: int = 20
    total_available: int = 0

    @property
    def has_more(self) -> bool:
        """True if there are more pages to fetch."""
        if self.total_available <= 0:
            return bool(self.assets)  # if unknown, assume more if we got results
        return self.page * self.per_page < self.total_available


class BaseVideoProvider(ABC):
    """Abstract base class for all video providers.

    Each concrete provider (Pexels, Pixabay, Mixkit, YouTube CC) must
    implement search(), search_page(), and download(), and expose a
    human-readable name.
    """

    def __init__(self, api_key: Optional[str] = None):
        """Initialize the provider with an optional API key.

        Args:
            api_key: Provider-specific API key. If not provided,
                     implementations should fall back to the appropriate
                     environment variable.
        """
        self.api_key = api_key

    @abstractmethod
    def search(self, query: str, min_duration: float, max_duration: float,
               resolution: tuple = (1920, 1080),
               page: int = 1, per_page: int = 20) -> Optional[VideoAsset]:
        """Search for a video matching the criteria (first good hit only).

        Args:
            query: Search query string (keywords).
            min_duration: Minimum acceptable duration in seconds.
            max_duration: Maximum acceptable duration in seconds.
            resolution: Preferred resolution as (width, height).
            page: Page number (1-indexed) for paginated APIs.
            per_page: Results per page.

        Returns:
            VideoAsset if a suitable video is found, None otherwise.
        """
        ...

    def search_page(self, query: str, min_duration: float, max_duration: float,
                    resolution: tuple = (1920, 1080),
                    page: int = 1, per_page: int = 20) -> SearchPage:
        """Search for ALL matching videos on a page (paginated).

        Default implementation calls search() and wraps the result.
        Providers with real pagination APIs (Pexels, Pixabay) should override
        this to return total_available and all candidates on the page.

        Args:
            query: Search query string.
            min_duration: Minimum acceptable duration in seconds.
            max_duration: Maximum acceptable duration in seconds.
            resolution: Preferred resolution as (width, height).
            page: Page number (1-indexed).
            per_page: Results per page.

        Returns:
            SearchPage with all matching VideoAsset candidates on this page.
        """
        assets: list[VideoAsset] = []
        total = 0
        # Try search() — providers that override search_page() won't reach here
        result = self.search(query, min_duration, max_duration, resolution,
                             page=page, per_page=per_page)
        if result:
            assets = [result]
            total = 1
        return SearchPage(
            assets=assets, page=page, per_page=per_page,
            total_available=total,
        )

    @abstractmethod
    def download(self, asset: VideoAsset, output_dir: Path) -> Path:
        """Download the video to output_dir.

        Args:
            asset: The VideoAsset to download (from a prior search()).
            output_dir: Directory where the video file will be saved.

        Returns:
            Local filesystem path to the downloaded file.
        """
        ...

    @property
    @abstractmethod
    def name(self) -> str:
        """Human-readable provider name."""
        ...
