"""Tests del cliente egress (server → agente) y su contrato fail-closed."""
import json

import pytest

from api.services import egress_client as ec


@pytest.fixture
def agents_file(tmp_path, monkeypatch):
    """Apunta el mapa de agentes a un fichero temporal controlado."""
    f = tmp_path / "egress_agents.json"
    data = {
        "canal_managed": {"url": "http://127.0.0.1:9", "token": "tok-ok"},
    }
    f.write_text(json.dumps(data))
    monkeypatch.setattr(ec, "_AGENTS_FILE", f)
    return f


def test_is_egress_managed_true(agents_file):
    assert ec.is_egress_managed("canal_managed") is True


def test_is_egress_managed_false_for_others(agents_file):
    assert ec.is_egress_managed("canal2") is False


def test_get_egress_client_none_for_unmanaged(agents_file):
    assert ec.get_egress_client("canal2") is None


def test_get_egress_client_returns_client(agents_file):
    client = ec.get_egress_client("canal_managed")
    assert client is not None
    assert client.slug == "canal_managed"
    assert client.base_url == "http://127.0.0.1:9"


def test_fail_closed_when_agent_unreachable(agents_file):
    """Canal gestionado + agente caído → excepción, NUNCA silencio ni fallback."""
    client = ec.get_egress_client("canal_managed")
    with pytest.raises(ec.EgressAgentUnavailableError):
        client.ping()


def test_fail_closed_browser_action(agents_file):
    client = ec.get_egress_client("canal_managed")
    with pytest.raises(ec.EgressAgentUnavailableError):
        client.browser_action("mark_altered", account="x")


def test_egress_agents_missing_file(tmp_path, monkeypatch):
    """Sin fichero de agentes → ningún canal es gestionado."""
    missing = tmp_path / "no_existe.json"
    monkeypatch.setattr(ec, "_AGENTS_FILE", missing)
    assert ec.is_egress_managed("canal2") is False
    assert ec.get_egress_client("canal2") is None
