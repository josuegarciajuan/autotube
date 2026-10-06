"""Tests for the Stable Horde AI image provider.

All network access is mocked: these tests never hit the real API.
They lock the request payload, the async→check→status flow, the WEBP→JPEG
conversion, the kudos-driven size fallback, and the fail-open behaviour.
"""
from __future__ import annotations

import base64
import os
import sys
from io import BytesIO
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from pipeline.providers import stable_horde_provider as shp
from pipeline.providers.stable_horde_provider import StableHordeProvider


# ── Fixtures ──────────────────────────────────────────────────────────

def _noise_webp_b64() -> str:
    """A random-noise WEBP (large enough to pass the 5 KB gate) as base64."""
    from PIL import Image

    img = Image.frombytes("RGB", (256, 256), os.urandom(256 * 256 * 3))
    buf = BytesIO()
    img.save(buf, format="WEBP")
    return base64.b64encode(buf.getvalue()).decode()


class _Resp:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


def _provider(tmp_path: Path, **kw) -> StableHordeProvider:
    defaults = dict(timeout_sec=60, poll_sec=1)
    defaults.update(kw)
    return StableHordeProvider(**defaults)


@pytest.fixture(autouse=True)
def _fast_sleep(monkeypatch):
    monkeypatch.setattr(shp.time, "sleep", lambda *_: None)


def _successful_reqs(calls: dict):
    """Build post/get/delete mocks for a happy-path generation."""
    def post(url, headers=None, json=None, timeout=None):
        calls["post"] = calls.get("post", []) + [json]
        # The API answers 202 Accepted on a successful async submission.
        return _Resp(202, {"id": "req-1", "kudos": 0})

    def get(url, headers=None, timeout=None):
        if "/check/" in url:
            return _Resp(200, {"done": True, "faulted": False, "is_possible": True})
        if "/status/" in url:
            return _Resp(200, {"generations": [{"state": "ok", "img": _noise_webp_b64()}]})
        return _Resp(404, text="not found")

    def delete(url, headers=None, timeout=None):
        calls["delete"] = calls.get("delete", 0) + 1
        return _Resp(200, {})

    return post, get, delete


# ── Tests ─────────────────────────────────────────────────────────────

def test_metadata_and_name():
    p = StableHordeProvider()
    assert p.name == "stable_horde"
    assert p.metadata.provider == "stable_horde"
    assert p.metadata.cost_per_image == 0.0
    assert p.metadata.supports_negative_prompt is True


def test_access_key_precedence(monkeypatch):
    monkeypatch.delenv("STABLE_HORDE_API_KEY", raising=False)
    assert StableHordeProvider().access_key == "0000000000"
    monkeypatch.setenv("STABLE_HORDE_API_KEY", "env-key")
    assert StableHordeProvider().access_key == "env-key"
    assert StableHordeProvider(access_key="arg-key").access_key == "arg-key"


def test_dimensions_rounded_to_64(tmp_path):
    p = _provider(tmp_path, width=700, height=333)
    assert p.width % 64 == 0 and p.width <= 700
    assert p.height % 64 == 0 and p.height <= 333
    assert p.width >= 64 and p.height >= 64


def test_generate_happy_path_writes_jpeg(tmp_path, monkeypatch):
    calls: dict = {}
    post, get, delete = _successful_reqs(calls)
    monkeypatch.setattr(shp.requests, "post", post, raising=False)
    monkeypatch.setattr(shp.requests, "get", get, raising=False)
    monkeypatch.setattr(shp.requests, "delete", delete, raising=False)

    out = tmp_path / "scene.jpg"
    p = _provider(tmp_path)
    result = p.generate("a blue circle", out, seed=42, negative_prompt="text")

    assert result == out and out.exists()
    assert out.stat().st_size > 5000
    from PIL import Image

    with Image.open(out) as im:
        assert im.format == "JPEG"
    # request contained the sanitised dimensions, model and negative prompt
    sent = calls["post"][0]
    assert sent["params"]["width"] % 64 == 0
    assert sent["params"]["negative_prompt"] == "text"
    assert sent["params"]["seed"] == "42"
    assert sent["models"] == ["stable_diffusion"]
    assert sent["censor_nsfw"] is True


def test_generate_faulted_returns_none(tmp_path, monkeypatch):
    monkeypatch.setattr(shp.requests, "post",
                        lambda *a, **k: _Resp(200, {"id": "r"}))
    monkeypatch.setattr(shp.requests, "get",
                        lambda url, **k: _Resp(200, {"faulted": True}))
    monkeypatch.setattr(shp.requests, "delete", lambda *a, **k: _Resp(200, {}))
    assert _provider(tmp_path).generate("x", tmp_path / "a.jpg") is None


def test_generate_impossible_returns_none(tmp_path, monkeypatch):
    monkeypatch.setattr(shp.requests, "post",
                        lambda *a, **k: _Resp(200, {"id": "r"}))
    monkeypatch.setattr(shp.requests, "get",
                        lambda url, **k: _Resp(200, {"is_possible": False}))
    monkeypatch.setattr(shp.requests, "delete", lambda *a, **k: _Resp(200, {}))
    assert _provider(tmp_path).generate("x", tmp_path / "a.jpg") is None


def test_generate_timeout_returns_none(tmp_path, monkeypatch):
    clock = {"t": 0.0}
    monkeypatch.setattr(shp.time, "monotonic", lambda: clock["t"])
    monkeypatch.setattr(shp.time, "sleep", lambda s: clock.__setitem__("t", clock["t"] + s))
    monkeypatch.setattr(shp.requests, "post",
                        lambda *a, **k: _Resp(200, {"id": "r"}))
    monkeypatch.setattr(shp.requests, "get",
                        lambda url, **k: _Resp(200, {"done": False}))
    monkeypatch.setattr(shp.requests, "delete", lambda *a, **k: _Resp(200, {}))
    assert _provider(tmp_path, timeout_sec=10, poll_sec=5).generate(
        "x", tmp_path / "a.jpg") is None


def test_generate_http_error_returns_none(tmp_path, monkeypatch):
    monkeypatch.setattr(shp.requests, "post",
                        lambda *a, **k: _Resp(500, text="boom"))
    monkeypatch.setattr(shp.requests, "delete", lambda *a, **k: _Resp(200, {}))
    assert _provider(tmp_path).generate("x", tmp_path / "a.jpg") is None


def test_censored_state_returns_none(tmp_path, monkeypatch):
    monkeypatch.setattr(shp.requests, "post",
                        lambda *a, **k: _Resp(200, {"id": "r"}))
    def _get(url, **k):
        if "/check/" in url:
            return _Resp(200, {"done": True})
        return _Resp(200, {"generations": [{"state": "censored", "img": ""}]})
    monkeypatch.setattr(shp.requests, "get", _get)
    monkeypatch.setattr(shp.requests, "delete", lambda *a, **k: _Resp(200, {}))
    assert _provider(tmp_path).generate("x", tmp_path / "a.jpg") is None


def test_retries_smaller_after_kudos_rejection(tmp_path, monkeypatch):
    posts: list = []

    def post(url, headers=None, json=None, timeout=None):
        posts.append(json)
        if len(posts) == 1:
            return _Resp(403, {"message": "requires 9.44 kudos", "rc": "KudosUpfront"})
        return _Resp(200, {"id": "r2"})

    monkeypatch.setattr(shp.requests, "post", post)
    def _get(url, **k):
        if "/check/" in url:
            return _Resp(200, {"done": True})
        return _Resp(200, {"generations": [{"state": "ok", "img": _noise_webp_b64()}]})
    monkeypatch.setattr(shp.requests, "get", _get)
    monkeypatch.setattr(shp.requests, "delete", lambda *a, **k: _Resp(200, {}))

    p = _provider(tmp_path, width=704, height=448)  # >692 → kudos-gated
    result = p.generate("x", tmp_path / "a.jpg")
    assert result is not None
    assert len(posts) == 2
    assert posts[0]["params"]["width"] == 704
    assert posts[1]["params"]["width"] == shp.FALLBACK_WIDTH
    assert posts[1]["params"]["height"] == shp.FALLBACK_HEIGHT


def test_non_kudos_failure_does_not_retry_smaller(tmp_path, monkeypatch):
    posts: list = []

    def post(url, headers=None, json=None, timeout=None):
        posts.append(json)
        return _Resp(202, {"id": "r"})

    monkeypatch.setattr(shp.requests, "post", post)
    def _get(url, **k):
        if "/check/" in url:
            return _Resp(200, {"faulted": True})
        return _Resp(200, {})
    monkeypatch.setattr(shp.requests, "get", _get)
    monkeypatch.setattr(shp.requests, "delete", lambda *a, **k: _Resp(200, {}))

    p = _provider(tmp_path, width=704, height=448)
    assert p.generate("x", tmp_path / "a.jpg") is None
    assert len(posts) == 1, "un fallo no-kudos no debe reintentar a menor tamaño"


def test_default_config_wires_stable_horde():
    import config.defaults as defaults

    chain = defaults.MEDIA_STRATEGY["ai_image_providers"]
    assert "stable_horde" in chain
    # order: pollinations → local_sd (fleet cache, instant) → stable_horde.
    # local_sd goes first so the pre-generated fleet cache wins; stable_horde
    # is the fallback when the fleet is unavailable/uncached.
    assert chain.index("local_sd") < chain.index("stable_horde")
    assert defaults.MEDIA_STRATEGY["ai_stable_horde_model"] == "stable_diffusion"
