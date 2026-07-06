"""PolloImageProvider — AI image generation for scenes where stock photos fail.

This provider wraps ``SceneImageGenerator`` and exposes a ``search()`` method
compatible with the MediaFetcher fallback chain.  Because Pollo AI is slow
(~7 min/image), it should only be invoked as a *last resort* after all stock
photo APIs have been exhausted.
"""

import logging
from pathlib import Path
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from pipeline.theme_extractor import ThemeContext

logger = logging.getLogger(__name__)


class PolloImageProvider:
    """AI image generation for scenes where stock photos fail.

    Usage::

        provider = PolloImageProvider(theme_context=ctx)
        result = provider.search("dark forest with moonlight")
        if result:
            print("Generated:", result)

    The ``.name`` property returns ``"pollo_ai"`` for attribution / logging.
    """

    def __init__(self, theme_context: "Optional[ThemeContext]" = None) -> None:
        self._generator = None  # lazy init
        self.theme = theme_context

    # ── Public API ──────────────────────────────────────────

    def search(self, query: str) -> Optional[Path]:
        """Generate an AI image for the given query.

        Returns the filesystem path to the generated image, or None on failure.
        """
        if self._generator is None:
            try:
                from pipeline.ai_image_generator import SceneImageGenerator
                self._generator = SceneImageGenerator()
            except Exception as exc:
                logger.error("Failed to init SceneImageGenerator: %s", exc)
                return None

        # Build a full description from the query + theme
        full_description = query
        if self.theme:
            full_description = self.theme.to_pollo_prompt(query)

        return self._generator.generate_scene_image(full_description, self.theme)

    # ── Properties ──────────────────────────────────────────

    @property
    def name(self) -> str:
        return "pollo_ai"
