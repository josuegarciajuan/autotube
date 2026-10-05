"""Egress monitor: descarta agentes sin canal en la DB.

Regresión: ``config/egress_agents.json`` puede conservar slugs retirados o
renombrados (p. ej. 'canal6'). El monitor creaba una alerta crítica
``egress_ip_down`` fantasma para un canal que ya no existe. Ahora se omite y se
resuelve cualquier alerta previa.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from api.services import egress_monitor


class _Rows:
    def __init__(self, slugs):
        self._slugs = slugs

    def fetchall(self):
        return [{"slug": s} for s in self._slugs]


class _FakeConn:
    def __init__(self, slugs, executed):
        self._slugs = slugs
        self._executed = executed

    def execute(self, sql, params=()):
        if "SELECT slug FROM channels" in sql:
            return _Rows(self._slugs)
        self._executed.append((sql, params))
        return type("Cur", (), {"rowcount": 1})()

    def commit(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeDB:
    def __init__(self, slugs):
        self._slugs = slugs
        self.state = {}
        self.executed = []

    def _connect(self):
        return _FakeConn(self._slugs, self.executed)

    def get_system_state(self, key):
        return self.state.get(key)

    def set_system_state(self, key, value):
        self.state[key] = value


def test_stale_egress_slug_is_skipped_and_alert_resolved(monkeypatch):
    db = _FakeDB(slugs=["canal2", "canal5"])
    monkeypatch.setattr(
        egress_monitor, "_load_agents",
        lambda: {"canal6": {"url": "http://x", "token": "t", "expected_ip": "1.2.3.4"}},
    )

    alerts = []
    import api.services.lifecycle_monitor as lm
    monkeypatch.setattr(lm, "create_alert", lambda *a, **k: alerts.append(k))

    # Si el monitor intentara comprobar el egress del slug huérfano, el cliente
    # no existiría/fallaría; forzamos un fallo para detectar un chequeo indebido.
    import api.services.egress_delegation as ed
    monkeypatch.setattr(ed, "egress_client_for", lambda slug: (_ for _ in ()).throw(AssertionError("no debe chequearse")))

    results = egress_monitor.check_all_egress(db)

    assert results == {}
    assert alerts == []  # ninguna alerta nueva
    assert any("UPDATE pipeline_alerts" in sql for sql, _ in db.executed)  # alerta previa resuelta


def test_known_slug_is_monitored(monkeypatch):
    db = _FakeDB(slugs=["canal2"])
    monkeypatch.setattr(
        egress_monitor, "_load_agents",
        lambda: {"canal2": {"url": "http://x", "token": "t", "expected_ip": "1.2.3.4"}},
    )

    class _Client:
        def egress_check(self):
            return {"result": {"ip": "1.2.3.4"}}

    import api.services.egress_delegation as ed
    monkeypatch.setattr(ed, "egress_client_for", lambda slug: _Client())

    results = egress_monitor.check_all_egress(db)
    assert results["canal2"]["ok"] is True
    assert db.get_system_state("egress_down_canal2") == "0"
