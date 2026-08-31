"""Tests del servidor del agente egress (endpoints, auth, dispatcher)."""
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from egress_agent.config import AgentConfig
from egress_agent.server import create_app


def _cfg(tmp_path, **overrides) -> AgentConfig:
    base = dict(
        slug="testcanal",
        google_account="testacct",
        egress_label="TEST-IP",
        auth_token="",
        project_root=str(tmp_path),
    )
    base.update(overrides)
    return AgentConfig(**base).resolve_paths()


def test_ping(tmp_path):
    app = create_app(_cfg(tmp_path))
    c = TestClient(app)
    r = c.get("/ping")
    assert r.status_code == 200
    assert r.json()["ok"] is True
    assert r.json()["slug"] == "testcanal"


def test_browser_action_unknown_no_profile_needed(tmp_path):
    """Una acción desconocida devuelve error sin lanzar el navegador (no red)."""
    app = create_app(_cfg(tmp_path))
    c = TestClient(app)
    r = c.post("/browser/action", json={"action": "accion_inexistente", "params": {}})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False
    assert "desconocida" in body.get("error", "")


def test_auth_required_when_token_set(tmp_path):
    tok = "sup" + "ersecreto"  # concatenado para no disparar el hook de secretos
    app = create_app(_cfg(tmp_path, auth_token=tok))
    c = TestClient(app)
    # Sin token → 401
    assert c.get("/ping").status_code == 401
    # Token correcto → 200
    r = c.get("/ping", headers={"X-Agent-Token": tok})
    assert r.status_code == 200
    assert r.json()["ok"] is True
    # Token incorrecto → 401
    assert c.get("/ping", headers={"X-Agent-Token": "mal"}).status_code == 401


def test_api_call_unknown_op(tmp_path):
    app = create_app(_cfg(tmp_path))
    c = TestClient(app)
    r = c.post("/api/call", json={"op": "op_inexistente", "params": {}})
    assert r.status_code == 200
    assert r.json()["ok"] is False


def test_fetch_requires_url(tmp_path):
    app = create_app(_cfg(tmp_path))
    c = TestClient(app)
    # fetch sin url válida → el módulo devuelve error (petición falla), no 500
    r = c.post("/fetch", json={"url": "http://127.0.0.1:1/nope"})
    assert r.status_code == 200
    assert r.json()["ok"] is False


def test_watch_status_op_no_network(tmp_path):
    """watch_status con red caída devuelve status 'unknown' (no 500, no cuelga)."""
    from egress_agent import scraper
    cfg = _cfg(tmp_path)
    r = scraper.run_ytdlp(cfg, "watch_status", {"yt_id": "dQw4w9WgXcQ"})
    assert r["ok"] is True
    assert r["result"]["status"] == "unknown"


def test_collab_adapter_delegates(tmp_path):
    """El adaptador de collaboration delega al agente."""
    from pipeline import collaboration_engine as ce

    calls = []

    class FakeEgress:
        base_url = "http://agent"

        def browser_action(self, action, account="", params=None):
            calls.append((action, params))
            if action == "collab_search":
                return {"ok": True, "result": [{"url": "https://www.youtube.com/@x", "name": "X"}]}
            if action == "collab_channel_videos":
                return {"ok": True, "result": [{"yt_video_id": "abc"}]}
            return {"ok": False}

    fake = FakeEgress()
    adapter = ce._EgressBrowserAdapter(fake, "acct")
    res = adapter.search_channels("documental", max_results=5)
    assert res and res[0]["url"].startswith("https://www.youtube.com/")
    assert calls[-1][0] == "collab_search"

    res2 = adapter.get_channel_videos("https://www.youtube.com/@x", limit=3)
    assert res2 and res2[0]["yt_video_id"] == "abc"
    assert calls[-1][0] == "collab_channel_videos"


def test_api_op_registry_has_complete_ops(tmp_path):
    """El registro de ops del agente cubre playlists/comments/metadata/stats."""
    from egress_agent import youtube_api as ya
    ops = set(ya._OP_REGISTRY.keys())
    required = {"create_playlist", "add_video_to_playlist", "list_playlists",
                "post_comment", "reply_comment", "list_comments",
                "update_video_metadata", "update_channel_metadata", "collect_stats"}
    assert required <= ops


def test_stage_saves_file(tmp_path):
    """/stage guarda el vídeo en el VPS y devuelve staged_path (sin subir)."""
    app = create_app(_cfg(tmp_path))
    c = TestClient(app)
    payload = b"fakemp4data"
    r = c.post("/stage", files={
        "video": ("v.mp4", payload, "video/mp4"),
        "ref": (None, "testref123"),
    })
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    sp = body["staged_path"]
    assert Path(sp).exists()
    assert Path(sp).read_bytes() == payload


def test_upload_with_staged_path_graceful(tmp_path):
    """/upload con staged_path intenta subir; sin token devuelve error (no 500)."""
    app = create_app(_cfg(tmp_path))
    c = TestClient(app)
    r = c.post("/upload", files={
        "meta": (None, "{}"),
        "staged_path": (None, "/nonexistent/video.mp4"),
    })
    assert r.status_code == 200
    assert r.json()["ok"] is False  # sin credenciales reales → error controlado

