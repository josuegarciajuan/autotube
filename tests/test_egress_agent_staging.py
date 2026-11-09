"""Staging y cleanup del agente + modo agente sin DB (nov 2026)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import pipeline.youtube_uploader as yu
from egress_agent.config import AgentConfig
from egress_agent.server import create_app


def _cfg(tmp_path, **kw):
    data = dict(
        slug="testcanal",
        google_account="acct",
        auth_token="",
        expected_ip="",
        egress_verify=False,  # test: sin guardia de IP
        project_root=str(tmp_path),
        staging_dir=str(tmp_path / "staging"),
        staging_max_gb=5,
        staging_ttl_hours=6,
        min_free_gb=0,
        max_body_mb=4096,
    )
    data.update(kw)
    return AgentConfig(**data)


def test_stage_and_cleanup(tmp_path):
    app = create_app(_cfg(tmp_path))
    c = TestClient(app)

    r = c.post("/stage", files={
        "video": ("v.mp4", b"x" * 1024, "video/mp4"),
        "ref": (None, "r1"),
    })
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    staged = Path(body["staged_path"])
    assert staged.exists()
    assert staged.parent.parent.name == "staging"

    r2 = c.post("/cleanup", json={"staged_path": str(staged)})
    assert r2.status_code == 200
    assert r2.json()["ok"] is True
    assert not staged.exists()
    assert not staged.parent.exists()  # dir vacío eliminado


def test_cleanup_rejects_path_outside_staging(tmp_path):
    app = create_app(_cfg(tmp_path))
    c = TestClient(app)
    outside = tmp_path / "outside.txt"
    outside.write_text("do not delete")
    r = c.post("/cleanup", json={"staged_path": str(outside)})
    assert r.status_code == 400
    assert outside.exists()


def test_stage_rejects_quota_exceeded(tmp_path):
    app = create_app(_cfg(tmp_path, staging_max_gb=0.000001))
    c = TestClient(app)
    r = c.post("/stage", files={
        "video": ("v.mp4", b"x" * 2048, "video/mp4"),
        "ref": (None, "r2"),
    })
    assert r.status_code == 413
    assert "cuota" in r.text.lower() or "quota" in r.text.lower()


def test_healthz_reports_staging_and_disk(tmp_path):
    app = create_app(_cfg(tmp_path, expected_ip="1.2.3.4", egress_verify=True))
    c = TestClient(app)
    # Sin red no se puede verificar la IP; healthz debe responder igualmente.
    r = c.get("/healthz")
    assert r.status_code == 200
    body = r.json()
    assert "staging_gb" in body
    assert "staging_max_gb" in body
    assert "disk_free_gb" in body


def test_from_env_reads_expected_ip(monkeypatch):
    monkeypatch.setenv("AGENT_SLUG", "canal6")
    monkeypatch.setenv("AGENT_EXPECTED_IP", "151.80.142.93")
    cfg = AgentConfig.from_env()
    assert cfg.expected_ip == "151.80.142.93"


def test_agent_mode_skips_db_side_effects(monkeypatch):
    """Con AUTOTUBE_AGENT_MODE el uploader no debe tocar la DB ni alertas."""
    monkeypatch.setattr(yu, "AGENT_MODE", True)
    import database.db_extended as dbe

    class _Boom:
        def __init__(self, *a, **k):
            raise AssertionError("el modo agente no debe abrir la DB")

    monkeypatch.setattr(dbe, "ExtendedDatabase", _Boom)

    u = yu.YouTubeUploader(account_name="canal6", channel_slug="canal6")
    assert u.config is None  # no config_bridge/DB
    # Ninguno de estos debe lanzar ni tocar la DB.
    u._resolve_yt_token_alert()
    u._alert_token_invalid(Exception("boom"))
    u._record_api_failure("videos.update", 50, "test", Exception("boom"))
    u._mark_quota_exhausted("test")
    u._alert_yt_upload_error("reason", "message")
    u._update_egress_transfer_state("/tmp/x.mp4", "done")
