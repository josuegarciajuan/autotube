"""Contrato mínimo de la fachada youtube_gateway."""
from __future__ import annotations

from api.services import youtube_gateway as gw


class _FakeClient:
    def __init__(self):
        self.calls = []

    def api_call(self, op, params):
        self.calls.append((op, params))
        return {"ok": True, "result": {"updated": True, "via": "fake"}}


def test_managed_delegates(monkeypatch):
    fake = _FakeClient()
    monkeypatch.setattr(gw, "client_for", lambda slug: fake)
    res = gw.set_publish_at("canal6", "vid1", "2026-11-10T10:00:00Z")
    assert res["updated"] is True
    assert fake.calls[0][0] == "set_publish_at"
    assert fake.calls[0][1]["kwargs"]["video_id"] == "vid1"


def test_unmanaged_uses_local(monkeypatch):
    monkeypatch.setattr(gw, "client_for", lambda slug: None)
    called = {"n": 0}

    def local():
        called["n"] += 1
        return {"updated": True, "via": "local"}

    res = gw.set_publish_at("canal2", "vid1", "2026-11-10T10:00:00Z", local_fn=local)
    assert res["via"] == "local"
    assert called["n"] == 1
