"""Tests de robustez del marcado IA en el navegador (auto-recuperación + sesión)."""

from __future__ import annotations

import contextlib
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import pipeline.youtube_browser as yb  # noqa: E402


def test_is_dead_context_error_matches_playwright_messages():
    assert yb._is_dead_context_error(
        Exception("BrowserContext.new_page: Target page, context or browser has been closed"))
    assert yb._is_dead_context_error(Exception("browser has been closed"))
    assert yb._is_dead_context_error(Exception("Target closed"))
    assert not yb._is_dead_context_error(Exception("Timeout 30000ms exceeded"))


def test_context_is_alive():
    assert yb._context_is_alive(None) is False

    class _Browser:
        def __init__(self, connected: bool):
            self._connected = connected

        def is_connected(self):
            return self._connected

    assert yb._context_is_alive(SimpleNamespace(browser=_Browser(True))) is True
    assert yb._context_is_alive(SimpleNamespace(browser=_Browser(False))) is False


def test_mark_altered_content_retries_dead_context_and_recovers(monkeypatch):
    b = object.__new__(yb.YouTubeBrowser)
    b._lock = threading.Lock()
    b.account = "acct"
    b.last_mark_reason = ""
    b._context = SimpleNamespace(new_page=lambda: object())
    b._playwright = None
    b._owning_thread_id = None
    b._ensure_browser = lambda: None

    calls = {"n": 0}

    def fake_do_mark(page, vid):
        calls["n"] += 1
        if calls["n"] == 1:
            raise Exception("Target page, context or browser has been closed")
        return True, "ok"

    b._do_mark = fake_do_mark
    reset = {"n": 0}
    b._reset_context = lambda: reset.__setitem__("n", reset["n"] + 1)
    b._alert_mark_failed = lambda *a, **k: None
    resolved = {"n": 0}
    b._resolve_session_expired = lambda: resolved.__setitem__("n", resolved["n"] + 1)

    monkeypatch.setattr("pipeline.browser_lock.browser_account_lock",
                        lambda acct, **k: contextlib.nullcontext())
    monkeypatch.setattr(yb.time, "sleep", lambda *_: None)

    assert b.mark_altered_content("VID") is True
    assert calls["n"] == 2          # reintentó tras contexto muerto
    assert reset["n"] == 1          # reconstruyó el contexto
    assert resolved["n"] == 1       # cerró alerta de sesión


def test_mark_altered_content_does_not_retry_session_expired(monkeypatch):
    b = object.__new__(yb.YouTubeBrowser)
    b._lock = threading.Lock()
    b.account = "acct"
    b.last_mark_reason = ""
    b._context = SimpleNamespace(new_page=lambda: object())
    b._playwright = None
    b._owning_thread_id = None
    b._ensure_browser = lambda: None

    calls = {"n": 0}

    def fake_do_mark(page, vid):
        calls["n"] += 1
        return False, "session_expired"

    b._do_mark = fake_do_mark
    b._reset_context = lambda: None
    failures: list = []
    b._alert_mark_failed = lambda vid, reason="": failures.append(reason)
    b._resolve_session_expired = lambda: None

    monkeypatch.setattr("pipeline.browser_lock.browser_account_lock",
                        lambda acct, **k: contextlib.nullcontext())
    monkeypatch.setattr(yb.time, "sleep", lambda *_: None)

    assert b.mark_altered_content("VID") is False
    assert calls["n"] == 1          # no reintenta: requiere re-login
    assert failures == ["session_expired"]


def test_mark_altered_content_robust_backoff(monkeypatch):
    class _FakeBrowser:
        def __init__(self, reasons):
            self._reasons = list(reasons)
            self.last_mark_reason = ""

        def mark_altered_content(self, vid):
            self.last_mark_reason = self._reasons.pop(0)
            return self.last_mark_reason in ("ok", "already")

    sleeps: list[float] = []
    monkeypatch.setattr(yb.time, "sleep", lambda s: sleeps.append(s))

    # radio_not_found → transitorio → reintenta → ok
    b = _FakeBrowser(["radio_not_found", "ok"])
    assert yb.mark_altered_content_robust(b, "V", attempts=3, base_backoff=10) is True
    assert sleeps == [10]

    # fallo permanente → no reintenta
    sleeps.clear()
    b2 = _FakeBrowser(["radio_disabled", "ok"])
    assert yb.mark_altered_content_robust(b2, "V", attempts=3, base_backoff=10) is False
    assert sleeps == []
