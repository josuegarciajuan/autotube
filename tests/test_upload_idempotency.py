"""Idempotencia de /upload: mismo ref no debe subir dos veces."""
from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

import egress_agent.server as server_mod
from egress_agent.config import AgentConfig


def test_same_upload_ref_returns_cached_video(tmp_path, monkeypatch):
    cfg = AgentConfig(
        slug="testcanal",
        google_account="acct",
        auth_token="",
        expected_ip="",
        egress_verify=False,
        project_root=str(tmp_path),
        staging_dir=str(tmp_path / "staging"),
        state_dir=str(tmp_path / "state"),
        staging_max_gb=5,
        staging_ttl_hours=6,
        min_free_gb=0,
        max_body_mb=4096,
    )
    (tmp_path / "staging").mkdir()
    staged = tmp_path / "staging" / "video.mp4"
    staged.write_bytes(b"x" * 1024)

    calls = {"n": 0}

    def fake_upload(cfg, path, meta):
        calls["n"] += 1
        return {"ok": True, "video_id": "vid123",
                "url": "https://youtu.be/vid123", "thumbnail_applied": None}

    monkeypatch.setattr(server_mod.youtube_api, "upload_video", fake_upload)
    app = server_mod.create_app(cfg)
    c = TestClient(app)

    payload = {
        "meta": (None, json.dumps({"upload_ref": "ref-abc", "title": "t"})),
        "staged_path": (None, str(staged)),
    }
    r1 = c.post("/upload", files=payload)
    assert r1.status_code == 200
    body1 = r1.json()
    assert body1["ok"] is True
    assert body1["video_id"] == "vid123"

    r2 = c.post("/upload", files=payload)
    assert r2.status_code == 200
    body2 = r2.json()
    assert body2["ok"] is True
    assert body2["video_id"] == "vid123"

    assert calls["n"] == 1  # la segunda vez salió del ledger
    ledger = Path(cfg.state_dir) / "uploads" / "ref-abc.json"
    assert ledger.exists()
    data = json.loads(ledger.read_text())
    assert data["status"] == "done"
    assert data["video_id"] == "vid123"
