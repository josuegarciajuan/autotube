"""Contratos del upscaler local de imágenes IA (AIImageUpscaler).

Cubre: no-op cuando ya se cumple el mínimo, degradación cuando el modelo
no está disponible, upscale real cuando la red está disponible, y la
integración con PollinationsProvider (upscale antes de cachear).
"""

from pathlib import Path
from unittest.mock import MagicMock

import numpy as np
import pytest
from PIL import Image

from pipeline.ai_upscaler import AIImageUpscaler
from pipeline.providers.pollinations_provider import PollinationsProvider


def _make_image(path: Path, w: int, h: int) -> Path:
    """Crea una imagen JPEG sintética del tamaño pedido."""
    arr = np.zeros((h, w, 3), dtype=np.uint8)
    arr[..., 0] = 200
    arr[..., 1] = 120
    arr[..., 2] = 40
    Image.fromarray(arr).save(path)
    return path


class _FakeNet:
    """Red ESPCN simulada: duplica la imagen (2×), como el modelo real."""

    def upsample(self, img):
        import cv2
        return cv2.resize(img, (img.shape[1] * 2, img.shape[0] * 2), interpolation=cv2.INTER_LANCZOS4)


def test_noop_when_image_already_meets_minimum(tmp_path: Path):
    upscaler = AIImageUpscaler()
    img = _make_image(tmp_path / "big.jpg", 2048, 1152)

    assert upscaler.upscale_to_min(img, 1920, 1080) is True

    with Image.open(img) as im:
        assert im.size == (2048, 1152)  # inalterado


def test_noop_when_model_unavailable(tmp_path: Path):
    upscaler = AIImageUpscaler()
    img = _make_image(tmp_path / "small.jpg", 1024, 576)

    # Simula: modelo no descargable / dnn_superres ausente
    upscaler._get_net = MagicMock(return_value=None)

    assert upscaler.upscale_to_min(img, 1920, 1080) is False

    with Image.open(img) as im:
        assert im.size == (1024, 576)  # intacta — degradación segura


def test_upscale_2x_when_below_minimum(tmp_path: Path):
    upscaler = AIImageUpscaler()
    img = _make_image(tmp_path / "small.jpg", 1024, 576)

    upscaler._get_net = MagicMock(return_value=_FakeNet())

    assert upscaler.upscale_to_min(img, 1920, 1080) is True

    with Image.open(img) as im:
        w, h = im.size
        assert w >= 1920
        assert h >= 1080


def test_upscale_does_not_touch_original_when_net_fails(tmp_path: Path):
    upscaler = AIImageUpscaler()
    img = _make_image(tmp_path / "small.jpg", 1024, 576)

    net = MagicMock()
    net.upsample.side_effect = RuntimeError("OOM")
    upscaler._get_net = MagicMock(return_value=net)

    assert upscaler.upscale_to_min(img, 1920, 1080) is False

    with Image.open(img) as im:
        assert im.size == (1024, 576)


# ── Integración con PollinationsProvider ────────────────────────────

def test_provider_upscales_before_caching(tmp_path: Path):
    provider = PollinationsProvider(upscale_min=(1920, 1080))
    img = _make_image(tmp_path / "scene.jpg", 1024, 576)

    fake_upscaler = MagicMock()
    provider._upscaler = fake_upscaler

    provider._maybe_upscale(img)

    fake_upscaler.upscale_to_min.assert_called_once_with(img, 1920, 1080)


def test_provider_skips_upscale_when_not_configured(tmp_path: Path):
    provider = PollinationsProvider()  # sin upscale_min
    img = _make_image(tmp_path / "scene.jpg", 1024, 576)

    provider._maybe_upscale(img)  # no debe lanzar

    assert provider._upscaler is None  # nunca se instanció


def test_provider_skips_missing_file(tmp_path: Path):
    provider = PollinationsProvider(upscale_min=(1920, 1080))
    missing = tmp_path / "no_existe.jpg"

    provider._maybe_upscale(missing)  # no debe lanzar

    assert provider._upscaler is None


def test_provider_accepts_upscale_min_variants():
    p1 = PollinationsProvider(upscale_min=(1920, 1080))
    assert p1.upscale_min == (1920, 1080)

    p2 = PollinationsProvider(upscale_min=(1280, 720))
    assert p2.upscale_min == (1280, 720)

    p3 = PollinationsProvider()  # sin configurar
    assert p3.upscale_min is None

    p4 = PollinationsProvider(upscale_min=("1920", "1080"))
    assert p4.upscale_min == (1920, 1080)  # coerción a int
