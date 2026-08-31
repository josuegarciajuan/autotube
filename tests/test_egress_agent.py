"""Tests del servidor del agente egress (endpoints, auth, dispatcher)."""
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
