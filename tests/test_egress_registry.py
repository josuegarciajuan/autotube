"""Tests del registro durable de egress gestionado (fail-closed).

Cubren el invariante clave: si la DB dice que un canal es gestionado, la
pérdida del JSON secreto NO lo convierte en local; se bloquea con un error.
"""
from __future__ import annotations

import json

import pytest

from api.services import egress_registry as reg


class _Rows:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return self._rows


class _Conn:
    def __init__(self, rows):
        self._rows = rows
        self.executed = []

    def execute(self, sql, params=()):
        if "SELECT slug FROM channels WHERE egress_managed = 1" in sql:
            return _Rows(self._rows)
        self.executed.append((sql, params))
        return type("Cur", (), {"rowcount": 1})()

    def commit(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _DB:
    def __init__(self, managed_slugs=()):
        self._rows = [{"slug": s} for s in managed_slugs]
        self.state = {}

    def _connect(self):
        return _Conn(self._rows)

    def set_system_state(self, key, value):
        self.state[key] = value

    def get_system_state(self, key):
        return self.state.get(key)


def _write_registry(tmp_path, monkeypatch, data):
    f = tmp_path / "egress_agents.json"
    f.write_text(json.dumps(data))
    monkeypatch.setattr(reg, "_AGENTS_FILE", f)
    return f


def test_db_flag_without_registry_is_managed_and_raises(tmp_path, monkeypatch, ):
    _write_registry(tmp_path, monkeypatch, {})
    monkeypatch.setattr(reg, "_db_channel", lambda slug: {
        "slug": slug, "egress_managed": 1, "egress_expected_ip": "1.2.3.4",
    })

    assert reg.is_egress_managed("canal6") is True
    with pytest.raises(reg.EgressConfigurationError):
        reg.get_profile("canal6")


def test_registry_only_complete_profile(tmp_path, monkeypatch):
    _write_registry(tmp_path, monkeypatch, {
        "canal6": {"url": "http://gateway", "token": "tok", "expected_ip": "1.2.3.4"},
    })
    monkeypatch.setattr(reg, "_db_channel", lambda slug: None)

    profile = reg.get_profile("canal6")
    assert profile is not None
    assert profile.url == "http://gateway"
    assert profile.token == "tok"
    assert profile.expected_ip == "1.2.3.4"
    assert profile.source == "registry"


def test_db_flag_with_complete_registry(tmp_path, monkeypatch):
    _write_registry(tmp_path, monkeypatch, {
        "canal6": {"url": "http://gateway", "token": "tok", "expected_ip": "1.2.3.4"},
    })
    monkeypatch.setattr(reg, "_db_channel", lambda slug: {
        "slug": slug, "egress_managed": 1,
        "egress_expected_ip": "1.2.3.4", "egress_status": "up",
    })

    profile = reg.get_profile("canal6")
    assert profile is not None
    assert profile.source == "db"
    assert profile.status == "up"


def test_incomplete_expected_ip_raises(tmp_path, monkeypatch):
    _write_registry(tmp_path, monkeypatch, {
        "canal6": {"url": "http://gateway", "token": "tok"},
    })
    monkeypatch.setattr(reg, "_db_channel", lambda slug: None)

    with pytest.raises(reg.EgressConfigurationError, match="expected_ip"):
        reg.get_profile("canal6")


def test_unmanaged_returns_none(tmp_path, monkeypatch):
    _write_registry(tmp_path, monkeypatch, {})
    monkeypatch.setattr(reg, "_db_channel", lambda slug: {
        "slug": slug, "egress_managed": 0,
    })
    assert reg.is_egress_managed("canal2") is False
    assert reg.get_profile("canal2") is None


def test_get_managed_slugs_unions_db_and_registry(tmp_path, monkeypatch):
    _write_registry(tmp_path, monkeypatch, {
        "canal7": {"url": "http://gateway7", "token": "t7", "expected_ip": "5.6.7.8"},
    })
    db = _DB(managed_slugs=["canal6"])
    assert reg.get_managed_slugs(db) == {"canal6", "canal7"}


def test_find_local_credentials(tmp_path, monkeypatch):
    monkeypatch.setattr(reg, "PROJECT_ROOT", tmp_path)
    (tmp_path / "tokens").mkdir()
    (tmp_path / "config").mkdir()
    (tmp_path / "tokens" / "canal6.pickle").write_text("x")
    (tmp_path / "config" / "client_secret_canal6.json").write_text("{}")
    (tmp_path / "tokens" / "acct_browser_profile").mkdir()

    found = reg.find_local_credentials(
        "canal6",
        reg.ManagedProfile(
            slug="canal6", url="http://x", token="t",
            expected_ip="1.2.3.4", google_account="acct",
        ),
    )
    assert len(found) == 3
    assert any(p.endswith("canal6.pickle") for p in found)
    assert any(p.endswith("client_secret_canal6.json") for p in found)
    assert any(p.endswith("acct_browser_profile") for p in found)


def test_reconcile_marks_error_when_profile_incomplete(tmp_path, monkeypatch):
    db = _DB(managed_slugs=["canal6"])
    monkeypatch.setattr(reg, "get_managed_slugs", lambda _db=None: {"canal6"})

    def _raise(slug, db_row=None):
        raise reg.EgressConfigurationError("falta url")

    monkeypatch.setattr(reg, "require_profile", _raise)
    import api.services.lifecycle_monitor as lm
    monkeypatch.setattr(lm, "create_alert", lambda *a, **k: None)

    report = reg.reconcile_on_startup(db)
    assert "canal6" in report["errors"]
    assert db.get_system_state("egress_config_error_canal6") == "1"
