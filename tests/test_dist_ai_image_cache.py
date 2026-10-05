"""Tests de la caché de imágenes IA pre-generadas por la flota."""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from pipeline_dist.ai_image_cache import CachedLocalSDProvider, prompt_cache_key


class _FakeInner:
    def __init__(self):
        self.calls = []

    def generate(self, prompt, output_path, seed=None, negative_prompt=None,
                 width=None, height=None):
        self.calls.append({"prompt": prompt, "output_path": str(output_path)})
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        Path(output_path).write_bytes(b"LOCAL" + prompt.encode("utf-8"))
        return Path(output_path)


def test_prompt_cache_key_es_estable():
    assert prompt_cache_key("scene 1/3: fuego") == prompt_cache_key("scene 1/3: fuego")
    assert prompt_cache_key("a") != prompt_cache_key("b")


def test_cache_hit_copia_al_output_path_y_no_delega(tmp_path):
    prompt = "scene 7/136: a circle of stones enclosing a fire"
    src = tmp_path / "dist_0007.jpg"
    src.write_bytes(b"CACHED-IMAGE-BYTES")

    inner = _FakeInner()
    proxy = CachedLocalSDProvider(inner, {prompt_cache_key(prompt): str(src)})

    out = tmp_path / "output" / "ai_images" / "desarrollo" / "scene_006.jpg"
    got = proxy.generate(prompt, out)

    assert got == out
    assert out.read_bytes() == b"CACHED-IMAGE-BYTES"
    assert inner.calls == [], "un acierto de caché nunca debe generar en local"


def test_cache_miss_delega_en_el_proveedor_local(tmp_path):
    inner = _FakeInner()
    proxy = CachedLocalSDProvider(inner, {})

    out = tmp_path / "scene.jpg"
    got = proxy.generate("prompt sin cache", out)

    assert got == out
    assert out.read_bytes() == b"LOCALprompt sin cache"
    assert len(inner.calls) == 1


def test_sin_proveedor_interno_devuelve_none(tmp_path):
    proxy = CachedLocalSDProvider(None, {})
    assert proxy.generate("x", tmp_path / "nope.jpg") is None


def test_getattr_delega_en_el_proveedor_real():
    class _P:
        name = "local_sd"

    proxy = CachedLocalSDProvider(_P(), {})
    assert proxy.name == "local_sd"


# ── Worker: contrato de result.json ante fallo de modelo ────────────────────
import json
import subprocess
import tempfile

import pytest

WORKER = Path(__file__).resolve().parent.parent / "pipeline_dist" / "image_worker.py"


@pytest.mark.skipif(
    __import__("importlib").util.find_spec("diffusers") is None,
    reason="diffusers no instalado",
)
def test_worker_escribe_result_json_ok_false_si_falla_el_modelo():
    tmp = Path(tempfile.mkdtemp(prefix="atube-sd-worker-"))
    req = {
        "key": "0000", "index": 0, "prompt": "test",
        "negative_prompt": "", "seed": 1,
        "width": 64, "height": 64, "steps": 1,
        "output": "scene_0000.jpg",
        "model_id": "__no_such_sd_model__",
        "upscale_min": None,
    }
    (tmp / "request.json").write_text(json.dumps(req), encoding="utf-8")
    out = tmp / "out"
    env = dict(os.environ, HF_HUB_OFFLINE="1")
    rc = subprocess.run(
        [sys.executable, str(WORKER), "--request", str(tmp / "request.json"),
         "--out-dir", str(out)],
        capture_output=True, text=True, timeout=180, env=env,
    )
    assert rc.returncode == 1
    result = json.loads((out / "result.json").read_text(encoding="utf-8"))
    assert result["ok"] is False
    assert result.get("error")

