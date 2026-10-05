"""Optimizaciones del render de shorts (oct 2026).

Cubre dos causas raíz de los timeouts de render híbrido:
  * la selección de fuente de vídeo debe preferir la resolución MÁS PEQUEÑA
    que cubra la pedida (no el 4K, que decodifica ~4x más lento);
  * el perfil de encode debe ser rápido (veryfast/ultrafast) y con hilos
    acotados para no matar la CPU cuando hay varios renders concurrentes.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def test_pexels_picks_smallest_resolution_covering_preferred():
    from pipeline.providers.pexels import PexelsVideoProvider

    files = [
        {"width": 2160, "height": 4096, "link": "4k"},
        {"width": 1080, "height": 1920, "link": "hd"},
        {"width": 720, "height": 1280, "link": "sd"},
    ]
    best = PexelsVideoProvider._pick_best_quality(files, (1080, 1920))
    assert best["link"] == "hd", f"eligió {best}"


def test_pexels_prefers_exact_match():
    from pipeline.providers.pexels import PexelsVideoProvider

    files = [
        {"width": 2160, "height": 4096, "link": "4k"},
        {"width": 1080, "height": 1920, "link": "hd"},
    ]
    best = PexelsVideoProvider._pick_best_quality(files, (1080, 1920))
    assert best["link"] == "hd"


def test_pexels_closest_above_when_no_exact():
    from pipeline.providers.pexels import PexelsVideoProvider

    files = [
        {"width": 2160, "height": 4096, "link": "4k"},
        {"width": 1440, "height": 2732, "link": "2k"},
    ]
    best = PexelsVideoProvider._pick_best_quality(files, (1080, 1920))
    assert best["link"] == "2k", f"eligió {best}"


def test_pexels_fallback_largest_when_nothing_meets_preferred():
    from pipeline.providers.pexels import PexelsVideoProvider

    files = [
        {"width": 720, "height": 1280, "link": "sd"},
        {"width": 540, "height": 960, "link": "tiny"},
    ]
    best = PexelsVideoProvider._pick_best_quality(files, (1080, 1920))
    assert best["link"] == "sd"


def test_shorts_encode_profile_is_fast_and_bounded():
    from pipeline import shorts_media as sm

    assert sm.FFMPEG_VIDEO_PRESET in {"veryfast", "superfast", "ultrafast"}, sm.FFMPEG_VIDEO_PRESET
    assert 2 <= sm.FFMPEG_VIDEO_THREADS <= 8
    assert str(sm.FFMPEG_VIDEO_CRF).isdigit()
