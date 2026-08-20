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
# 768×768 es el máximo nativo estable de SD 1.5. Antes era 512×512, que
# ampliado a 1080p quedaba muy borroso. Aun así se upscalea después con
# el AIImageUpscaler (ESPCN_x2 + unsharp mask) hasta la resolución mínima
# objetivo.
DEFAULT_WIDTH = 768
DEFAULT_HEIGHT = 768
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
        default_resolution=(768, 768),
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
        width: int = DEFAULT_WIDTH,
        height: int = DEFAULT_HEIGHT,
        upscale_min: Optional[tuple[int, int]] = None,
        upscale_model: Optional[str] = None,
        upscale_sharpen: bool = True,
        upscale_sharpen_amount: float = 0.4,
        upscale_sharpen_sigma: float = 2.0,
    ) -> None:
        self.device = device
        self.num_inference_steps = num_inference_steps
        self.model_id = model_id or MODEL_ID
        self.width = width
        self.height = height
        # Resolución mínima objetivo (w, h). Si la imagen generada es menor,
        # se upscalea localmente (ESPCN_x2 + unsharp mask) antes de guardar.
        self.upscale_min: Optional[tuple[int, int]] = None
        if upscale_min and len(upscale_min) == 2:
            self.upscale_min = (int(upscale_min[0]), int(upscale_min[1]))
        self.upscale_model: Optional[str] = upscale_model
        # Unsharp mask post-upscale (nitidez percibida).
        self.upscale_sharpen: bool = upscale_sharpen
        self.upscale_sharpen_amount: float = upscale_sharpen_amount
        self.upscale_sharpen_sigma: float = upscale_sharpen_sigma
        self._upscaler = None  # lazy singleton
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
            width: Image width (default self.width = 768).
            height: Image height (default self.height = 768).

        Returns:
            Path to the saved image, or ``None`` on failure.
        """
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)

        w = width or self.width
        h = height or self.height

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

            # ── Upscale local post-generación (ESPCN_x2 + unsharp mask) ──
            # SD 1.5 genera a 768×768; ampliado a 1080p el render se ve
            # borroso. Subimos a la resolución mínima objetivo antes de
            # guardar (mismo mecanismo que Pollinations).
            self._maybe_upscale(output_path)

            logger.info(
                "Local SD image generated: %s (%.1fs, %d steps)",
                output_path, elapsed, self.num_inference_steps,
            )
            return output_path

        except Exception as exc:
            logger.error("Local SD generation failed: %s", exc)
            return None

    # ── Internal: upscale ─────────────────────────────────────

    def _maybe_upscale(self, image_path: Path) -> None:
        """Upscale *image_path* a la resolución mínima configurada (si procede).

        No-op si no hay resolución mínima configurada, si el upscaler no
        está disponible o si la imagen ya cumple el mínimo. Nunca lanza:
        degrada silenciosamente al comportamiento original.
        """
        if not self.upscale_min or not image_path or not Path(image_path).exists():
            return
        try:
            if self._upscaler is None:
                from pipeline.ai_upscaler import AIImageUpscaler
                self._upscaler = AIImageUpscaler(
                    model=self.upscale_model or "espcn",
                    sharpen_enabled=self.upscale_sharpen,
                    sharpen_amount=self.upscale_sharpen_amount,
                    sharpen_sigma=self.upscale_sharpen_sigma,
                )
            self._upscaler.upscale_to_min(
                Path(image_path),
                self.upscale_min[0],
                self.upscale_min[1],
            )
        except Exception as exc:
            logger.debug("Local SD upscale skipped (%s): %s", image_path, exc)

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
