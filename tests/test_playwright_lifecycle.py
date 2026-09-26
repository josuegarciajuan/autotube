"""Unit tests for the Playwright driver lifecycle hardening.

Cubre los 6 mecanismos de fuga documentados en
``specs/playwright-driver-leak.md``:

1. ``_stop_playwright`` NUNCA llama ``pw.stop()`` desde otro hilo.
2. ``_stop_playwright`` conserva la entrada del registry si el stop falla.
3. ``_ensure_browser`` con cambio de hilo no para el driver ajeno.
4. ``check_session_valid`` para el driver async si el launch falla.
5. ``BrowserSessionManager.start()`` es atómico (no deja ``_playwright`` vivo).
6. El reaper mata solo drivers viejos del mismo PID (TTL + kill-switch).
"""
from __future__ import annotations

import asyncio
import os
import sys
import threading
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pipeline.youtube_browser as yb  # noqa: E402
import pipeline.playwright_reaper as pr  # noqa: E402


@pytest.fixture(autouse=True)
def _isolate_registry():
    """Snapshot/restore the global registry so tests never pollute each other."""
    with yb._registry_lock:
        saved = dict(yb._playwright_registry)
        yb._playwright_registry.clear()
    yield
    with yb._registry_lock:
        yb._playwright_registry.clear()
        yb._playwright_registry.update(saved)


def _register_foreign(pw, owner_native_id: int):
    """Register an instance pretending another thread owns it."""
    with yb._registry_lock:
        yb._playwright_registry[id(pw)] = {
            "pw": pw,
            "owner_native_id": owner_native_id,
            "owner_ident": 999999,
            "owner_name": "foreign-thread",
            "pid": os.getpid(),
            "started_at": time.time(),
            "driver_pid": None,
            "stop_requested": False,
            "orphaned": False,
            "orphaned_at": None,
            "orphan_reason": "",
        }


# ── 1+2. Owner-aware _stop_playwright ───────────────────────────────

def test_stop_playwright_same_thread_success_unregisters():
    fake = MagicMock()
    yb._register_playwright(fake)
    assert yb._stop_playwright(fake) is True
    fake.stop.assert_called_once()
    assert yb._get_registry_meta(fake) is None


def test_stop_playwright_cross_thread_is_deferred_never_called():
    fake = MagicMock()
    yb._register_playwright(fake)  # owned by the main test thread

    result = {}

    def _worker():
        result["ok"] = yb._stop_playwright(fake)

    t = threading.Thread(target=_worker)
    t.start()
    t.join(timeout=5)

    assert result["ok"] is False
    fake.stop.assert_not_called()
    meta = yb._get_registry_meta(fake)
    assert meta is not None, "una instancia viva NO debe desregistrarse"
    assert meta["orphaned"] is True
    assert meta["stop_requested"] is True


def test_stop_playwright_failure_keeps_registry_entry():
    fake = MagicMock()
    fake.stop.side_effect = RuntimeError("cannot switch to a different thread")
    yb._register_playwright(fake)

    assert yb._stop_playwright(fake) is False
    meta = yb._get_registry_meta(fake)
    assert meta is not None
    assert meta["orphaned"] is True
    assert meta.get("orphan_reason") == "stop-raised"


def test_cleanup_all_does_not_stop_foreign_instances():
    foreign = MagicMock()
    own = MagicMock()
    _register_foreign(foreign, owner_native_id=threading.get_native_id() + 12345)
    yb._register_playwright(own)

    yb._cleanup_all_playwrights(force=False)

    foreign.stop.assert_not_called()
    own.stop.assert_called_once()
    assert yb._get_registry_meta(foreign) is not None


# ── 3. _ensure_browser thread change defers (no cross-thread stop) ──

def test_ensure_browser_thread_change_marks_foreign_pw_orphaned(monkeypatch):
    b = object.__new__(yb.YouTubeBrowser)
    b.account = "test-account"
    b.fingerprint = {}
    b.proxy = None
    b._lock = threading.Lock()
    b._context = MagicMock()
    b._playwright = MagicMock()
    b._owning_thread_id = threading.get_native_id() + 777  # foreign/previous
    b.user_data_dir = Path("/tmp/autotube_test_profile_does_not_exist")
    b.session_file = Path("/tmp/autotube_test_session.json")
    b.last_mark_reason = ""

    _register_foreign(b._playwright, owner_native_id=b._owning_thread_id)

    new_pw = MagicMock()
    sentinel_ctx = MagicMock()
    new_pw.chromium.launch_persistent_context.return_value = sentinel_ctx
    monkeypatch.setattr(yb, "_get_or_create_playwright", lambda: new_pw)
    monkeypatch.setattr(b, "_cleanup_stale_locks", lambda: None)
    monkeypatch.setattr(yb.time, "sleep", lambda *_: None)

    old_pw = b._playwright
    b._ensure_browser()

    old_pw.stop.assert_not_called()  # no debe parar el driver de otro hilo
    meta = yb._get_registry_meta(old_pw)
    assert meta is not None and meta["orphaned"] is True
    assert b._playwright is new_pw
    assert b._context is sentinel_ctx


# ── 4. check_session_valid: finally cubre el launch ─────────────────

@pytest.mark.asyncio
async def test_check_session_valid_stops_pw_when_launch_fails(monkeypatch, tmp_path):
    account = "leaktest_acct_launchfail"
    (tmp_path / f"{account}_browser_profile").mkdir()
    monkeypatch.setattr(yb, "TOKENS_DIR", tmp_path)
    monkeypatch.setattr(yb, "_ensure_xvfb", lambda: None)
    yb._session_check_cache.pop(account, None)
    yb._browser_instances.pop(account, None)

    fake_pw = MagicMock()
    fake_pw.stop = AsyncMock()
    fake_pw.chromium.launch_persistent_context = AsyncMock(
        side_effect=RuntimeError("launch exploded")
    )

    class _FakeAP:
        async def start(self):
            return fake_pw

    import playwright.async_api as ap
    monkeypatch.setattr(ap, "async_playwright", lambda: _FakeAP())

    result = await yb.check_session_valid(account, cache_seconds=0)
    assert result["status"] == "error"
    fake_pw.stop.assert_awaited_once()


# ── 5. social_browser atomic start / tolerant stop ──────────────────

@pytest.mark.asyncio
async def test_social_browser_start_cleans_up_on_failure(monkeypatch, tmp_path):
    from pipeline import social_browser as sb

    fake_pw = MagicMock()
    fake_pw.stop = AsyncMock()
    fake_browser = MagicMock()
    fake_browser.close = AsyncMock()
    fake_browser.new_context = AsyncMock(side_effect=RuntimeError("ctx boom"))
    fake_pw.chromium.launch = AsyncMock(return_value=fake_browser)

    class _FakeAP:
        async def start(self):
            return fake_pw

    import playwright.async_api as ap
    monkeypatch.setattr(ap, "async_playwright", lambda: _FakeAP())

    bsm = sb.BrowserSessionManager(user_data_dir=str(tmp_path))
    with pytest.raises(RuntimeError):
        await bsm.start()

    assert bsm._playwright is None
    assert bsm._browser is None
    assert bsm._context is None
    fake_pw.stop.assert_awaited_once()
    fake_browser.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_social_browser_stop_is_tolerant(tmp_path):
    from pipeline import social_browser as sb

    bsm = sb.BrowserSessionManager(user_data_dir=str(tmp_path))
    bsm._context = MagicMock()
    bsm._context.close = AsyncMock(side_effect=RuntimeError("ctx close"))
    bsm._browser = MagicMock()
    bsm._browser.close = AsyncMock(side_effect=RuntimeError("browser close"))
    bsm._playwright = MagicMock()
    bsm._playwright.stop = AsyncMock()
    browser = bsm._browser

    await bsm.stop()  # no debe lanzar
    assert bsm._playwright is None
    assert bsm._browser is None
    browser.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_social_browser_stop_continues_after_cancelled(tmp_path):
    from pipeline import social_browser as sb

    bsm = sb.BrowserSessionManager(user_data_dir=str(tmp_path))
    bsm._context = MagicMock()
    bsm._context.close = AsyncMock(side_effect=asyncio.CancelledError())
    bsm._browser = MagicMock()
    bsm._browser.close = AsyncMock()
    bsm._playwright = MagicMock()
    bsm._playwright.stop = AsyncMock()
    browser, pw = bsm._browser, bsm._playwright

    with pytest.raises(asyncio.CancelledError):
        await bsm.stop()

    browser.close.assert_awaited_once()
    pw.stop.assert_awaited_once()


# ── 6. Reaper ───────────────────────────────────────────────────────

def test_reap_once_kills_only_old_drivers(monkeypatch):
    killed = []
    monkeypatch.setenv("PLAYWRIGHT_REAPER_ENABLED", "true")
    monkeypatch.setattr(pr, "_kill_pid", lambda pid, parent=None: killed.append(pid) or True)

    children = [
        {"pid": 111, "age_seconds": 10, "cmdline": ["node", "run-driver"]},
        {"pid": 222, "age_seconds": 7200, "cmdline": ["node", "run-driver"]},
    ]
    stats = pr.reap_once(ttl=3600, parent_pid=999, children=children, registry_entries=[])

    assert killed == [222]
    assert stats["killed"] == 1
    assert stats["skipped_recent"] == 1


def test_reap_once_respects_kill_switch(monkeypatch):
    monkeypatch.setenv("PLAYWRIGHT_REAPER_ENABLED", "false")
    called = []
    monkeypatch.setattr(pr, "_kill_pid", lambda pid, parent=None: called.append(pid) or True)

    stats = pr.reap_once(
        ttl=0, parent_pid=999,
        children=[{"pid": 42, "age_seconds": 999999}],
        registry_entries=[],
    )
    assert stats["enabled"] is False
    assert called == []


def test_kill_pid_refuses_non_driver_process(monkeypatch):
    monkeypatch.setattr(pr, "_parent_pid", lambda pid: 999)
    monkeypatch.setattr(pr, "_read_cmdline", lambda pid: [b"python3", b"other_script.py"])
    real_kill = []
    monkeypatch.setattr(pr.os, "kill", lambda pid, sig: real_kill.append(pid))

    assert pr._kill_pid(123, 999) is False
    assert real_kill == []


def test_kill_pid_refuses_foreign_parent(monkeypatch):
    monkeypatch.setattr(pr, "_parent_pid", lambda pid: 1)
    monkeypatch.setattr(pr, "_read_cmdline", lambda pid: [b"node", b"run-driver"])
    real_kill = []
    monkeypatch.setattr(pr.os, "kill", lambda pid, sig: real_kill.append(pid))

    assert pr._kill_pid(123, 999) is False
    assert real_kill == []


def test_driver_cmdline_detection():
    assert pr._is_driver_cmdline([b"node", b"/x/playwright/driver/package/cli.js", b"run-driver"])
    assert pr._is_driver_cmdline(["node", "run-driver"])
    assert not pr._is_driver_cmdline([b"python3", b"main.py"])
    assert not pr._is_driver_cmdline([b"node", b"/x/playwright/driver/package/cli.js"])
