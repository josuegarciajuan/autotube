"""PollinationsProvider — free, no-auth AI image generation via Pollinations.ai.

Pollinations.ai is a community-funded, open-source image generation service.
No API key, no account, no registration required.

API: GET https://image.pollinations.ai/prompt/{url_encoded_prompt}
     ?width=W&height=H&model=flux&seed=S&nologo=true

Models available:
    - ``flux`` (default) — best overall quality
    - ``flux-realism`` — photorealistic style
    - ``turbo`` — faster generation, lower quality
    - ``sdxl`` — legacy SDXL model

Usage::

    from pipeline.providers.pollinations_provider import PollinationsProvider

    provider = PollinationsProvider()
    path = provider.generate(
        "cinematic landscape, mountains at sunset, 16:9, dramatic lighting",
        Path("output/test.jpg"),
        seed=42,
    )
    if path:
        print(f"Image saved: {path}")
"""

from __future__ import annotations

import hashlib
import logging
import time
import urllib.parse
from pathlib import Path
from typing import Optional

import requests

from pipeline.ai_provider_metadata import AIProviderMetadata

logger = logging.getLogger(__name__)

BASE_URL = "https://image.pollinations.ai/prompt"
DEFAULT_MODEL = "flux"
DEFAULT_WIDTH = 1280
DEFAULT_HEIGHT = 720
REQUEST_TIMEOUT = 60  # seconds


class PollinationsProvider:
    """Generate images via the free Pollinations.ai API.

    Zero authentication. Zero cost. Zero rate-limit headaches (community-funded).
    """

    # Rate limits per official docs (APIDOCS.md):
    #   Anonymous: 1 req / 15s  (~4/min)  — no signup
    #   Seed:      1 req / 5s   (~12/min) — free registration
    #   Flower:    1 req / 3s   (~20/min) — paid
    #   Nectar:    unlimited              — enterprise
    # No daily quota — only per-request throttling. Community-funded.
    METADATA = AIProviderMetadata(
        provider="pollinations",
        display_name="Pollinations.ai (Flux)",
        auth_required=False,
        model=DEFAULT_MODEL,
        default_resolution=(1280, 720),
        max_resolution=(1920, 1080),
        avg_latency_seconds=1.5,         # measured: 1.4-1.6s regardless of prompt complexity
        rate_limit_per_minute=4,          # Anonymous tier: 1 req / 15s
        rate_limit_per_day=None,          # No daily cap — rate-throttled only
        quality_score=7.0,
        cost_per_image=0.0,
        supports_seed=True,
        supports_negative_prompt=False,
        uses_local_resources=False,
        ram_usage_mb=0,
        cpu_cores_used=0,
        disk_model_gb=0.0,
    )

    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        width: int = 1280,
        height: int = 720,
        cache_dir: Optional[str] = None,
    ) -> None:
        self.model = model
        self.width = width
        self.height = height
        self.cache_dir = Path(cache_dir) if cache_dir else None
        if self.cache_dir:
            self.cache_dir.mkdir(parents=True, exist_ok=True)

    # ── Properties ──────────────────────────────────────────

    @property
    def name(self) -> str:
        return "pollinations"

    @property
    def metadata(self) -> AIProviderMetadata:
        return self.METADATA

    # ── Public API ──────────────────────────────────────────

    def generate(
        self,
        prompt: str,
        output_path: Path,
        seed: Optional[int] = None,
        negative_prompt: Optional[str] = None,
        width: Optional[int] = None,
        height: Optional[int] = None,
    ) -> Optional[Path]:
        """Generate an image from a text prompt.

        Args:
            prompt: Text description of the image to generate.
            output_path: Where to save the generated image.
            seed: Random seed for reproducibility (optional).
            negative_prompt: Ignored (not supported by Pollinations API).
            width: Override default width.
            height: Override default height.

        Returns:
            Path to the saved image, or ``None`` on failure.
        """
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)

        # Check cache first (by prompt hash)
        cached = self._check_cache(prompt)
        if cached:
            logger.info("Pollinations cache hit: %s", prompt[:60])
            return cached

        w = width or self.width
        h = height or self.height

        try:
            url = self._build_url(prompt, w, h, seed)
            logger.info("Pollinations request: %s...", prompt[:80])

            start = time.monotonic()
            resp = requests.get(url, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            elapsed = time.monotonic() - start

            output_path.write_bytes(resp.content)
            file_size_kb = len(resp.content) / 1024

            logger.info(
                "Pollinations image generated: %s (%.1f KB, %.1fs)",
                output_path, file_size_kb, elapsed,
            )

            # Save to cache
            self._save_cache(prompt, output_path)
            return output_path

        except requests.Timeout:
            logger.error("Pollinations request timed out after %ds", REQUEST_TIMEOUT)
        except requests.HTTPError as exc:
            status = exc.response.status_code if exc.response else 0
            if status == 429:
                logger.warning("Pollinations rate-limited (429) — sleeping 30s and retrying once")
                time.sleep(30)
                try:
                    resp = requests.get(self._build_url(prompt, w, h, seed), timeout=REQUEST_TIMEOUT)
                    resp.raise_for_status()
                    output_path.write_bytes(resp.content)
                    self._save_cache(prompt, output_path)
                    logger.info("Pollinations retry succeeded after rate-limit cooldown")
                    return output_path
                except Exception:
                    logger.error("Pollinations retry also failed after rate-limit")
            else:
                logger.error("Pollinations HTTP error %d: %s", status, exc)
        except requests.RequestException as exc:
            logger.error("Pollinations request failed: %s", exc)
        except Exception as exc:
            logger.error("Pollinations unexpected error: %s", exc)

        return None

    # ── Internal ────────────────────────────────────────────

    def _build_url(
        self, prompt: str, width: int, height: int, seed: Optional[int]
    ) -> str:
        """Build the full request URL with query parameters."""
        encoded = urllib.parse.quote(prompt, safe="")
        params = {
            "width": str(width),
            "height": str(height),
            "model": self.model,
            "nologo": "true",
        }
        if seed is not None:
            params["seed"] = str(seed)
        qs = urllib.parse.urlencode(params)
        return f"{BASE_URL}/{encoded}?{qs}"

    def _cache_key(self, prompt: str) -> str:
        """Deterministic cache key from prompt text."""
        return hashlib.md5(prompt.encode("utf-8")).hexdigest()[:16]

    def _cache_path(self, prompt: str) -> Optional[Path]:
        """Return the filesystem path where a cached image would live."""
        if not self.cache_dir:
            return None
        return self.cache_dir / f"pollinations_{self._cache_key(prompt)}.jpg"

    def _check_cache(self, prompt: str) -> Optional[Path]:
        """Return cached image path if it exists, else None."""
        p = self._cache_path(prompt)
        if p and p.exists() and p.stat().st_size > 0:
            return p
        return None

    def _save_cache(self, prompt: str, source_path: Path) -> None:
        """Copy the generated image to the cache directory."""
        cache_path = self._cache_path(prompt)
        if cache_path is None:
            return
        try:
            import shutil
            if not cache_path.exists():
                shutil.copy2(source_path, cache_path)
        except Exception as exc:
            logger.debug("Failed to cache Pollinations image: %s", exc)
