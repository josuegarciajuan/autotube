"""Tests for the central egress delegation guards."""
import json

import pytest

from api.services import egress_client as ec
from api.services import egress_registry as reg
from api.services.egress_delegation import fail_closed_if_managed


@pytest.fixture
def agents_file(tmp_path, monkeypatch):
    """Point the agent registry at a controlled temporary file."""
    agents = tmp_path / "egress_agents.json"
    agents.write_text(json.dumps({
        "canal_managed": {
            "url": "http://agent",
            "token": "tok",
            "expected_ip": "1.2.3.4",
        }
    }))
    monkeypatch.setattr(reg, "_AGENTS_FILE", agents)
    monkeypatch.setattr(ec, "_AGENTS_FILE", agents)
    monkeypatch.setattr(reg, "_db_channel", lambda slug: None)


def test_fail_closed_guard_allows_unmanaged_channel(agents_file):
    fail_closed_if_managed("canal_unmanaged", "stats collection")


def test_fail_closed_guard_rejects_managed_channel(agents_file):
    with pytest.raises(ec.EgressAgentUnavailableError, match="fail-closed"):
        fail_closed_if_managed("canal_managed", "stats collection")
