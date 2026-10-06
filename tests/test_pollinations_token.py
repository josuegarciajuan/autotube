"""Tests de PollinationsProvider: token/headers y disyuntor ante 402 (x402)."""
from __future__ import annotations

from pathlib import Path

import pytest
import requests

from pipeline.providers import pollinations_provider as pp


@pytest.fixture(autouse=True)
def _reset_shared_wall():
    """The x402 breaker is process-wide by design; reset it between tests."""
    pp._SHARED_WALL_UNTIL = 0.0
    yield
    pp._SHARED_WALL_UNTIL = 0.0


def _fake_402_get(calls):
    def _get(url, timeout=None, headers=None):
        calls.append({"url": url, "headers": headers or {}})

        class _Resp:
            status_code = 402
            content = b""

            def raise_for_status(self):
                raise requests.HTTPError(response=self)

        return _Resp()

    return _get


def test_token_in_url_and_headers():
    p = pp.PollinationsProvider(token="tok123", referrer="autotube.local")
    url = p._build_url("ciudad antigua", 1280, 720, 42)
    assert "token=tok123" in url
    assert "referrer=autotube.local" in url
    assert p._headers() == {"Authorization": "Bearer tok123"}


def test_no_token_no_auth():
    p = pp.PollinationsProvider()
    url = p._build_url("x", 100, 100, None)
    assert "token=" not in url
    assert p._headers() == {}


def test_402_sets_circuit_breaker(monkeypatch, tmp_path):
    calls: list = []
    monkeypatch.setattr(pp.requests, "get", _fake_402_get(calls))
    monkeypatch.setenv("POLLINATIONS_X402_COOLDOWN_SEC", "3600")
    p = pp.PollinationsProvider()

    out = tmp_path / "a.jpg"
    assert p.generate("escena uno", out) is None
    assert p._wall_until > 0
    assert len(calls) == 1
    assert calls[0]["url"].startswith(pp.BASE_URL)

    # Segunda llamada: el disyuntor evita otra petición HTTP.
    assert p.generate("escena dos", out) is None
    assert len(calls) == 1


def test_token_sent_as_bearer(monkeypatch, tmp_path):
    seen = {}

    def _get(url, timeout=None, headers=None):
        seen["headers"] = headers or {}

        class _Resp:
            status_code = 402
            content = b""

            def raise_for_status(self):
                raise requests.HTTPError(response=self)

        return _Resp()

    monkeypatch.setattr(pp.requests, "get", _get)
    p = pp.PollinationsProvider(token="tok-abc")
    p.generate("escena", tmp_path / "b.jpg")
    assert seen["headers"].get("Authorization") == "Bearer tok-abc"
