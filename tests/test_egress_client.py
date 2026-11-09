"""Tests del cliente egress (server → agente) y su contrato fail-closed."""
import json

import pytest

from api.services import egress_client as ec
from api.services import egress_registry as reg


@pytest.fixture
def agents_file(tmp_path, monkeypatch):
    """Apunta el registro de agentes a un fichero temporal controlado."""
    f = tmp_path / "egress_agents.json"
    data = {
        "canal_managed": {
            "url": "http://127.0.0.1:9",
            "token": "tok-ok",
            "expected_ip": "1.2.3.4",
        },
    }
    f.write_text(json.dumps(data))
    # El registro durable es la fuente real; parcheamos ambos alias.
    monkeypatch.setattr(reg, "_AGENTS_FILE", f)
    monkeypatch.setattr(ec, "_AGENTS_FILE", f)
    # Sin DB: ningún canal está marcado como gestionado por flag.
    monkeypatch.setattr(reg, "_db_channel", lambda slug: None)
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
    """Sin fichero de agentes y sin flag DB → ningún canal es gestionado."""
    missing = tmp_path / "no_existe.json"
    monkeypatch.setattr(reg, "_AGENTS_FILE", missing)
    monkeypatch.setattr(ec, "_AGENTS_FILE", missing)
    monkeypatch.setattr(reg, "_db_channel", lambda slug: None)
    assert ec.is_egress_managed("canal2") is False
    assert ec.get_egress_client("canal2") is None
