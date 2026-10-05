"""Paridad de la petición IA entre la ruta local y la distribuida (SuperServer).

Ambas rutas deben construir EXACTAMENTE la misma petición (prompt/negative/seed)
para que la imagen generada en la flota sea funcionalmente equivalente a la de
la casa. Este test blinda `media_fetcher._build_ai_request()`, que es la única
fuente de la petición que consumen `_try_ai_image_chain` (local) y
`pipeline_dist.orchestrator_dist._prefetch_ai_images_dist` (flota).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _fetcher():
    import config.canal4_config as cfg
    from pipeline.media_fetcher import MediaFetcher
    return MediaFetcher(cfg)


def _scene():
    return {
        "tipo": "desarrollo",
        "texto": "a circle of stones enclosing a fire",
        "duration": 6.0,
        "search_query_en": "circle of stones enclosing a fire",
        "media_tipo": "imagen",
        "asset_idx": 3,
    }


def test_build_ai_request_es_determinista():
    mf = _fetcher()
    a = mf._build_ai_request(_scene(), 3, 10)
    b = mf._build_ai_request(_scene(), 3, 10)
    assert a == b, "la petición debe ser estable para la misma escena"


def test_build_ai_request_incluye_marcador_concepto_y_negative():
    mf = _fetcher()
    prompt, negative, seed = mf._build_ai_request(_scene(), 3, 10)
    assert "scene 4/10" in prompt, "el marcador de escena debe ir en el prompt"
    assert "stones" in prompt.lower() or "circle" in prompt.lower(), \
        "el concepto de la escena debe estar en el prompt"
    assert isinstance(negative, str) and negative.strip()
    assert seed is None or isinstance(seed, int)


def test_prompt_unico_por_escena():
    mf = _fetcher()
    p0 = mf._build_ai_request(_scene(), 0, 5)[0]
    p1 = mf._build_ai_request(_scene(), 1, 5)[0]
    assert "scene 1/5" in p0 and "scene 2/5" in p1
    assert p0 != p1, "escenas distintas no deben colisionar en el hash de prompt"
