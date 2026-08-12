"""Multi-provider media sourcing system.

Provides a unified interface for searching and downloading stock video footage
from multiple sources: Pexels, Pixabay, Mixkit, Coverr, YouTube Creative Commons.

Also includes AI image generation providers for on-demand scene imagery:
  - PollinationsProvider (free, no-auth, Flux model)
  - LocalSDProvider (local CPU, Stable Diffusion 1.5)
  - PolloImageProvider (existing, credit-limited, Pollo AI)

AI providers follow the same ``name`` + ``generate(prompt, path)`` pattern
and expose metadata via ``AIProviderMetadata`` for the orchestrator.
"""

# Re-export AI providers for convenience
from pipeline.providers.pollinations_provider import PollinationsProvider  # noqa: F401
from pipeline.providers.local_sd_provider import LocalSDProvider            # noqa: F401
