"""Tests del watchdog de procesos node atascados en D (io_uring)."""

from __future__ import annotations

import contextlib
import sqlite3
import sys
import types
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from api.services import system_watchdog as w  # noqa: E402

FAKE_PS = """\
1234 Dl 3600 node  node /root/autotube/frontend/node_modules/.bin/vite build
2222 Ss    10 node  node server.js
3333 D     20 python3 python3 algo.py
4444 D    1200 node  node driver.js
5555 D    2000 npm   install     npm install
6666 Dl   3000 npm   ci          npm ci
7777 D    1500 python3 python3 algo.py
8888 Ss      10 node  node server.js
9999 D     900 cp    cp -r /root/x/node_modules /tmp
"""


def _fake_run(*a, **k):
    return types.SimpleNamespace(stdout=FAKE_PS)


def test_find_stuck_filters_node_tools_d_only(monkeypatch):
    monkeypatch.setattr(w.subprocess, "run", _fake_run)
    stuck = w.find_stuck_node_processes(min_seconds=600)
    pids = sorted(s["pid"] for s in stuck)
    # node (1234, 4444) + npm (5555, 6666). Se excluyen Ss, python y el cp con
    # 'node_modules' (basename no es una herramienta Node).
    assert pids == [1234, 4444, 5555, 6666]


def test_strict_matcher_ignores_node_modules_substring():
    # Rutas/args con 'node'/'npm' como substring NO deben contar.
    assert w._is_node_tool_process("cp", "cp -r /root/x/node_modules /tmp") is False
    assert w._is_node_tool_process("cp", "cp -r /root/x/npm-cache /tmp") is False
    # Herramientas Node reales (incluidas envueltas) SÍ cuentan.
    assert w._is_node_tool_process("node", "node server.js") is True
    assert w._is_node_tool_process("npm", "install npm install") is True
    assert w._is_node_tool_process("sh", 'sh -c "npm ci"') is True
    assert w._is_node_tool_process("npx", "npx vite build") is True


def test_alert_emitted_when_stuck(monkeypatch):
    monkeypatch.setattr(w, "find_stuck_node_processes",
                        lambda min_seconds=600: [{"pid": 1, "elapsed_s": 3600, "args": "x"}])
    captured = {}
    import api.services.lifecycle_monitor as lm
    monkeypatch.setattr(lm, "create_alert", lambda *a, **k: captured.update(k) or 1)

    class DummyDB:
        @contextlib.contextmanager
        def _connect(self):
            yield None

    res = w.check_node_io_uring(DummyDB())
    assert res["alerted"] is True
    assert captured.get("alert_type") == "node_io_uring_stuck"
    assert captured.get("severity") == "warning"


def test_alert_resolved_when_clean(monkeypatch):
    monkeypatch.setattr(w, "find_stuck_node_processes", lambda min_seconds=600: [])
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE pipeline_alerts (alert_type TEXT, resolved INTEGER, resolved_at TEXT)")
    conn.execute("INSERT INTO pipeline_alerts VALUES ('node_io_uring_stuck', 0, NULL)")
    conn.commit()

    class DB:
        @contextlib.contextmanager
        def _connect(self):
            yield conn

    res = w.check_node_io_uring(DB())
    assert res["alerted"] is False
    assert conn.execute("SELECT resolved FROM pipeline_alerts").fetchone()[0] == 1
