"""LocalSDProvider — Stable Diffusion 1.5 running locally on CPU.

Uses the ``diffusers`` library with the ``runwayml/stable-diffusion-v1-5``
checkpoint. No GPU required — all inference runs on CPU with attention slicing
and VAE tiling optimizations.

Dependencies (installed automatically on first use if missing):
    ``pip install diffusers transformers accelerate safetensors``

Model download: ~5 GB (one-time, cached by HuggingFace Hub).

Usage::

    from pipeline.providers.local_sd_provider import LocalSDProvider

    provider = LocalSDProvider()
    path = provider.generate(
        "cinematic landscape, mountains at sunset, dramatic lighting, 16:9",
        Path("output/test.jpg"),
        negative_prompt="blurry, low quality, watermark, text",
        seed=42,
    )
    if path:
        print(f"Image saved: {path}")
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Optional

from pipeline.ai_provider_metadata import AIProviderMetadata

logger = logging.getLogger(__name__)

MODEL_ID = "runwayml/stable-diffusion-v1-5"
DEFAULT_STEPS = 20            # CPU-optimized (fewer steps = faster, still decent quality)
DEFAULT_WIDTH = 512
DEFAULT_HEIGHT = 512
REQUEST_TIMEOUT = 600         # 10 minutes for CPU generation


class LocalSDProvider:
    """Generate images locally using Stable Diffusion 1.5 on CPU.

    Lazy-loads the model pipeline on first use to avoid unnecessary
    memory usage when this provider is never called.
    """

    METADATA = AIProviderMetadata(
        provider="local_sd",
        display_name="Stable Diffusion 1.5 (Local CPU)",
        auth_required=False,
        model=MODEL_ID,
        default_resolution=(512, 512),
        max_resolution=(768, 768),
        avg_latency_seconds=180.0,    # ~3 min on this Xeon
        rate_limit_per_minute=2,      # 2 workers in parallel
        rate_limit_per_day=None,      # unlimited
        quality_score=6.5,
        cost_per_image=0.0,
        supports_seed=True,
        supports_negative_prompt=True,
        uses_local_resources=True,
        ram_usage_mb=4500,
        cpu_cores_used=3,
        disk_model_gb=5.0,
    )

    def __init__(
        self,
        device: str = "cpu",
        num_inference_steps: int = DEFAULT_STEPS,
        model_id: Optional[str] = None,
    ) -> None:
        self.device = device
        self.num_inference_steps = num_inference_steps
        self.model_id = model_id or MODEL_ID
        self._pipe = None          # lazy-loaded

    # ── Properties ──────────────────────────────────────────

    @property
    def name(self) -> str:
        return "local_sd"

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
        """Generate an image from a text prompt using local SD 1.5.

        Args:
            prompt: Text description of the image.
            output_path: Where to save the generated PNG.
            seed: Random seed for reproducibility.
            negative_prompt: Things to avoid in the image.
            width: Image width (default 512).
            height: Image height (default 512).

        Returns:
            Path to the saved image, or ``None`` on failure.
        """
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)

        w = width or DEFAULT_WIDTH
        h = height or DEFAULT_HEIGHT

        try:
            pipe = self._load_pipe()
        except Exception as exc:
            logger.error("Failed to load SD pipeline: %s", exc)
            return None

        try:
            import torch

            generator = None
            if seed is not None:
                generator = torch.Generator(device=self.device).manual_seed(seed)

            neg = negative_prompt or ""
            logger.info(
                "Local SD generating: %s... (%dx%d, %d steps)",
                prompt[:80], w, h, self.num_inference_steps,
            )

            start = time.monotonic()

            result = pipe(
                prompt=prompt,
                negative_prompt=neg,
                width=w,
                height=h,
                num_inference_steps=self.num_inference_steps,
                generator=generator,
            )

            elapsed = time.monotonic() - start

            image = result.images[0]
            image.save(str(output_path))

            logger.info(
                "Local SD image generated: %s (%.1fs, %d steps)",
                output_path, elapsed, self.num_inference_steps,
            )
            return output_path

        except Exception as exc:
            logger.error("Local SD generation failed: %s", exc)
            return None

    def is_available(self) -> bool:
        """Check whether the SD pipeline can be loaded.

        Returns ``True`` if all dependencies are installed and the model
        is accessible. Useful for pre-flight checks before dispatching.
        """
        try:
            self._load_pipe()
            return True
        except Exception:
            return False

    # ── Internal ────────────────────────────────────────────

    def _load_pipe(self):
        """Lazy-load the Stable Diffusion pipeline with CPU optimizations.

        Returns the loaded pipeline, caching it for the lifetime of
        this instance.
        """
        if self._pipe is not None:
            return self._pipe

        logger.info("Loading Stable Diffusion pipeline from %s (this may take a while)...", self.model_id)

        try:
            import torch
            from diffusers import StableDiffusionPipeline
        except ImportError as exc:
            raise RuntimeError(
                "diffusers/torch not installed. Run: "
                "pip install diffusers transformers accelerate safetensors"
            ) from exc

        load_start = time.monotonic()

        self._pipe = StableDiffusionPipeline.from_pretrained(
            self.model_id,
            torch_dtype=torch.float32,
            safety_checker=None,          # Skip 1.5 GB safety checker download
            requires_safety_checker=False,
        )

        # CPU optimizations
        self._pipe = self._pipe.to(self.device)
        self._pipe.enable_attention_slicing(slice_size="auto")

        # VAE tiling for memory efficiency on larger images
        try:
            self._pipe.enable_vae_tiling()
        except Exception:
            pass

        load_time = time.monotonic() - load_start
        logger.info("SD pipeline loaded in %.1fs (device=%s)", load_time, self.device)

        return self._pipe
