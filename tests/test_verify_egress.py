"""Tests de scripts/verify_egress.py (gate end-to-end de egress)."""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "scripts" / "verify_egress.py"


@pytest.fixture()
def verify_module(monkeypatch):
    """Carga scripts/verify_egress.py real y parchea sus dependencias de red."""
    spec = importlib.util.spec_from_file_location("verify_egress_test", SRC)
    mod = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(__import__("sys").modules, "verify_egress_test", mod)
    spec.loader.exec_module(mod)  # noqa: BLE001

    agents = {}

    class FakeClient:
        def __init__(self, cfg):
            self.cfg = cfg

        def healthz(self):
            if not self.cfg["agent"]:
                raise RuntimeError("no accesible")
            return {"ok": True, "expected_ip": self.cfg["expected"]}

        def egress_check(self):
            return {"ok": True, "result": {"ip": self.cfg["curl"]}}

        def egress_check_browser(self):
            return {"ok": True, "result": {
                "browser_ip": self.cfg["browser"],
                "webrtc_disabled": self.cfg.get("webrtc", True),
            }}

    def fake_get_egress_client(slug):
        return FakeClient(agents[slug]) if slug in agents else None

    def fake_load_agents():
        return {slug: {"url": "http://x"} for slug in agents}

    monkeypatch.setattr(mod, "get_egress_client", fake_get_egress_client)
    monkeypatch.setattr(mod, "_load_agents", fake_load_agents)
    mod.agents = agents
    return mod


def test_all_green(verify_module):
    verify_module.agents["canal6"] = {
        "agent": True, "expected": "58.68.169.25",
        "curl": "58.68.169.25", "browser": "58.68.169.25", "webrtc": True,
    }
    assert verify_module._check_one("canal6", skip_browser=False)["ok"] is True


def test_curl_ip_mismatch_fails(verify_module):
    verify_module.agents["canal6"] = {
        "agent": True, "expected": "58.68.169.25",
        "curl": "194.233.67.64", "browser": "58.68.169.25", "webrtc": True,
    }
    res = verify_module._check_one("canal6", skip_browser=False)
    assert res["ok"] is False
    assert "IP de egress" in res["error"]


def test_browser_leak_fails(verify_module):
    verify_module.agents["canal6"] = {
        "agent": True, "expected": "58.68.169.25",
        "curl": "58.68.169.25", "browser": "194.233.67.64", "webrtc": True,
    }
    res = verify_module._check_one("canal6", skip_browser=False)
    assert res["ok"] is False
    assert "navegador" in res["error"].lower()


def test_missing_expected_ip_fails(verify_module):
    verify_module.agents["canal6"] = {
        "agent": True, "expected": "", "curl": "58.68.169.25", "browser": "", "webrtc": True,
    }
    res = verify_module._check_one("canal6", skip_browser=False)
    assert res["ok"] is False
    assert "expected_ip" in res["error"]


def test_webrtc_leak_fails(verify_module):
    verify_module.agents["canal6"] = {
        "agent": True, "expected": "58.68.169.25",
        "curl": "58.68.169.25", "browser": "58.68.169.25", "webrtc": False,
    }
    res = verify_module._check_one("canal6", skip_browser=False)
    assert res["ok"] is False
    assert "WebRTC" in res["error"]


def test_skip_browser(verify_module):
    verify_module.agents["canal6"] = {
        "agent": True, "expected": "58.68.169.25",
        "curl": "58.68.169.25", "browser": "194.233.67.64", "webrtc": True,
    }
    assert verify_module._check_one("canal6", skip_browser=True)["ok"] is True
