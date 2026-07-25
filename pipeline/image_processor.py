"""Image processing with Pillow: resize, color grading, grain, vignette."""
import logging
import random
from pathlib import Path

import numpy as np
from PIL import Image, ImageEnhance, ImageFilter

from config import settings

logger = logging.getLogger(__name__)


class ImageProcessor:
    """Applies cinematic post-processing to images.

    Pipeline: resize → color grade → vignette → film grain → sharpen → save.
    """

    def __init__(self, style_config=None) -> None:
        if style_config is None:
            from config.config_bridge import get_channel_config
            style_config = get_channel_config(settings.ACTIVE_CHANNELS[0])
        self.config = style_config
        logger.info("ImageProcessor initialized with palette=%s", self.config.COLOR_PALETTE)

    def process(self, image_path, output_path=None):
        """Run the full processing pipeline on a single image.

        Args:
            image_path: Path or str to the source image.
            output_path: Destination path. Defaults to
                IMAGES_DIR / processed_{stem}.jpg.

        Returns:
            Path to the processed image file.
        """
        image_path = Path(image_path)
        if output_path is None:
            output_path = settings.IMAGES_DIR / f"processed_{image_path.stem}.jpg"
        else:
            output_path = Path(output_path)

        if output_path.exists():
            logger.info("Processed image already cached: %s", output_path)
            return output_path

        try:
            img = Image.open(image_path).convert("RGB")
        except Exception as exc:
            logger.error("Failed to load image %s: %s", image_path, exc)
            raise

        img = self._resize_center_crop(img, settings.VIDEO_RESOLUTION)

        desat = 1.0 - random.uniform(0.10, 0.20)
        tint_strength = random.uniform(0.05, 0.12)
        img = self._color_grade(img, desat, tint_strength)
        logger.debug("Color graded: desat=%.2f tint=%.2f", desat, tint_strength)

        vignette_intensity = random.uniform(0.3, 0.5)
        vignette_layer = self.create_vignette(settings.VIDEO_RESOLUTION, vignette_intensity)
        img = img.convert("RGBA")
        img = Image.alpha_composite(img, vignette_layer).convert("RGB")
        logger.debug("Vignette applied: intensity=%.2f", vignette_intensity)

        grain_opacity = random.uniform(0.03, 0.08)
        grain_layer = self.create_grain_overlay(settings.VIDEO_RESOLUTION, grain_opacity)
        img = Image.blend(img, grain_layer, grain_opacity)
        logger.debug("Film grain applied: opacity=%.2f", grain_opacity)

        img = img.filter(ImageFilter.UnsharpMask(radius=1.2, percent=80, threshold=3))
        logger.debug("Sharpening applied")

        output_path.parent.mkdir(parents=True, exist_ok=True)
        img.save(output_path, "JPEG", quality=95)
        logger.info("Processed and saved: %s", output_path)
        return output_path

    def process_batch(self, image_paths):
        """Process multiple images.

        Args:
            image_paths: List of source image paths (Path or str).

        Returns:
            List of output Path objects.
        """
        results = []
        for ip in image_paths:
            try:
                results.append(self.process(ip))
            except Exception as exc:
                logger.error("Skipping %s due to error: %s", ip, exc)
        return results

    def create_grain_overlay(
        self, size: tuple[int, int] = (1920, 1080), opacity: float | None = None
    ) -> Image.Image:
        """Procedural film grain using numpy.random.normal.

        Args:
            size: (width, height) of the overlay.
            opacity: Blending opacity (0.0 – 1.0). Defaults to
                FILM_GRAIN_OPACITY from config.

        Returns:
            RGB image with randomized monochromatic noise.
        """
        if opacity is None:
            opacity = self.config.FILM_GRAIN_OPACITY / 100.0

        w, h = size
        noise = np.random.normal(128, 48, (h, w, 3)).clip(0, 255).astype(np.uint8)
        grain_raw = Image.fromarray(noise, "RGB")
        gray = Image.new("RGB", size, (128, 128, 128))
        grain = Image.blend(grain_raw, gray, 0.6)

        return grain

    def create_vignette(
        self, size: tuple[int, int] = (1920, 1080), intensity: float = 0.4
    ) -> Image.Image:
        """Create a dark radial gradient vignette overlay.

        Args:
            size: (width, height) of the overlay.
            intensity: How dark the edges become (0.0 – 1.0).

        Returns:
            RGBA image with a transparent center and black edges.
        """
        w, h = size
        cx, cy = w / 2.0, h / 2.0
        max_dist = np.sqrt(cx**2 + cy**2)

        y_grid, x_grid = np.ogrid[:h, :w]
        dist = np.sqrt((x_grid - cx) ** 2 + (y_grid - cy) ** 2) / max_dist

        alpha = (dist * intensity).clip(0, 1) * 255
        vignette_array = np.zeros((h, w, 4), dtype=np.uint8)
        vignette_array[:, :, 3] = alpha.astype(np.uint8)

        return Image.fromarray(vignette_array, "RGBA")

    def _resize_center_crop(
        self, img: Image.Image, target_size: tuple[int, int]
    ) -> Image.Image:
        """Resize to target resolution by center-cropping, then scaling.

        Args:
            img: Source PIL image.
            target_size: (width, height) of the output.

        Returns:
            Resized RGB image.
        """
        tw, th = target_size
        iw, ih = img.size
        target_ratio = tw / th
        img_ratio = iw / ih

        if img_ratio > target_ratio:
            new_width = int(ih * target_ratio)
            left = (iw - new_width) // 2
            img = img.crop((left, 0, left + new_width, ih))
        else:
            new_height = int(iw / target_ratio)
            top = (ih - new_height) // 2
            img = img.crop((0, top, iw, top + new_height))

        return img.resize(target_size, Image.LANCZOS)

    def _color_grade(
        self, img: Image.Image, desaturation: float, tint_strength: float
    ) -> Image.Image:
        """Desaturate the image and apply a primary-color tint overlay.

        Args:
            img: Source RGB image.
            desaturation: Saturation multiplier (e.g. 0.85 for 15% less).
            tint_strength: Blend factor for the tint overlay (0.0 – 1.0).

        Returns:
            Color-graded RGB image.
        """
        img = ImageEnhance.Color(img).enhance(desaturation)

        primary = self.config.COLOR_PALETTE.get("primary", (180, 30, 30))
        # Per-channel override: IMAGE_TINT_COLOR decouples the image tint
        # from the branding palette (e.g. a neutral tint so warm scenes
        # don't get a cold cast). Falls back to COLOR_PALETTE.primary.
        tint_color = getattr(self.config, "IMAGE_TINT_COLOR", None)
        if tint_color is None:
            tint_color = primary
        # JSON round-trip may turn tuples into lists; ensure tuple for PIL
        if isinstance(tint_color, list):
            tint_color = tuple(tint_color)
        tint = Image.new("RGB", img.size, tint_color)
        img = Image.blend(img, tint, tint_strength)

        return img
