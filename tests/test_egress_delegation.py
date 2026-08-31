"""Tests for the central egress delegation guards."""
import json

import pytest

from api.services import egress_client as ec
from api.services.egress_delegation import fail_closed_if_managed


@pytest.fixture
def agents_file(tmp_path, monkeypatch):
    """Point the agent registry at a controlled temporary file."""
    agents = tmp_path / "egress_agents.json"
    agents.write_text(json.dumps({"canal_managed": {"url": "http://agent"}}))
    monkeypatch.setattr(ec, "_AGENTS_FILE", agents)


def test_fail_closed_guard_allows_unmanaged_channel(agents_file):
    fail_closed_if_managed("canal_unmanaged", "stats collection")


def test_fail_closed_guard_rejects_managed_channel(agents_file):
    with pytest.raises(ec.EgressAgentUnavailableError, match="fail-closed"):
        fail_closed_if_managed("canal_managed", "stats collection")
