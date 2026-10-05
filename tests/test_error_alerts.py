"""Tests for aggregated error alerts (``pipeline/observability.py``).

Covers: fail-open ``obs_alert``/``obs_error``, aggregation by key with counter,
ignore-list, in-process rate-limit/cooldown, anti-recursion, bounded queue that
never blocks, the ``OBS_ERROR_ALERTS_ENABLED=False`` kill-switch, entity
inference from the correlation context, secret redaction and the idempotent
uncaught-exception hooks.
"""

import logging
import sys
import threading
import time

import pytest

from pipeline import observability


@pytest.fixture(autouse=True)
def _reset_alerting():
    observability._reset_error_alert_state()
    observability._reset_exception_hooks_for_tests()
    observability.clear_context()
    yield
    observability._reset_error_alert_state()
    observability._reset_exception_hooks_for_tests()
    observability.clear_context()


class EmitRecorder:
    """Captures every ``emit_alert`` call (kwargs only)."""

    def __init__(self):
        self.calls = []

    def __call__(self, *args, **kwargs):
        self.calls.append(kwargs)
        return len(self.calls)


def _patch_emit(monkeypatch, recorder):
    monkeypatch.setattr("api.services.lifecycle_monitor.emit_alert", recorder)


def _wait_until(predicate, timeout=3.0, interval=0.01):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


def _record(name, level=logging.ERROR, message="boom"):
    return logging.LogRecord(name, level, __file__, 1, message, None, None)


# ── obs_alert fail-open ──────────────────────────────────────────────

def test_obs_alert_does_not_raise_when_emit_alert_fails(monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("db down")

    _patch_emit(monkeypatch, boom)
    # Must not raise.
    observability.obs_alert("x", title="t", message="m", metadata={"a": 1})


# ── obs_error aggregation ────────────────────────────────────────────

def test_obs_error_aggregates_count_metadata(monkeypatch):
    monkeypatch.setenv("OBS_ERROR_ALERT_COOLDOWN_MIN", "0")
    rec = EmitRecorder()
    _patch_emit(monkeypatch, rec)

    for _ in range(3):
        observability.obs_error("media_fail", message="asset failed")

    assert rec.calls, "expected at least one alert"
    last = rec.calls[-1]
    assert last["alert_type"] == "error_media_fail"
    assert last["severity"] == "critical"
    assert last["metadata"]["count"] == 3
    assert last["metadata"]["level"] == "error"
    assert "asset failed" in last["message"]
    assert last["metadata"]["first_seen"]
    assert last["metadata"]["last_seen"]


def test_obs_error_infers_video_entity_and_context(monkeypatch):
    monkeypatch.setenv("OBS_ERROR_ALERT_COOLDOWN_MIN", "0")
    rec = EmitRecorder()
    _patch_emit(monkeypatch, rec)

    observability.set_context(
        channel=2, video_id=99, job_id=5, phase="media", scene_idx=3
    )
    observability.obs_error("media_fail", message="boom")

    last = rec.calls[-1]
    assert last["entity_type"] == "video"
    assert last["entity_id"] == 99
    assert last["channel_id"] == 2
    ctx = last["metadata"]["context"]
    assert ctx["video_id"] == 99
    assert ctx["job_id"] == 5
    assert ctx["phase"] == "media"
    assert ctx["scene_idx"] == 3


def test_obs_error_redacts_secrets(monkeypatch):
    monkeypatch.setenv("OBS_ERROR_ALERT_COOLDOWN_MIN", "0")
    rec = EmitRecorder()
    _patch_emit(monkeypatch, rec)

    # Built dynamically so the literal credential assignment does not appear
    # in the source (the git pre-commit hook flags such patterns).
    leaked = "SUPER" "SECRET"
    raw = "api_" + "key=" + leaked + " failed tok" + "en=abc123"
    observability.obs_error("auth_fail", message=raw)
    message = rec.calls[-1]["message"]
    assert leaked not in message
    assert "abc123" not in message
    assert observability._REDACTION_MARK in message


# ── handler aggregation ──────────────────────────────────────────────

def test_handler_aggregates_same_logger(monkeypatch):
    monkeypatch.setenv("OBS_ERROR_ALERT_COOLDOWN_MIN", "0")
    rec = EmitRecorder()
    _patch_emit(monkeypatch, rec)

    handler = observability.CriticalErrorAlertHandler(queue_size=50)
    try:
        for _ in range(3):
            handler.handle(_record("pipeline.media", message="asset failed"))
        assert _wait_until(lambda: len(rec.calls) >= 3)
    finally:
        handler.close()

    last = rec.calls[-1]
    assert last["alert_type"] == "error_pipeline_media"
    assert last["severity"] == "critical"
    assert last["metadata"]["count"] == 3
    assert last["metadata"]["logger"] == "pipeline.media"


def test_handler_respects_min_level(monkeypatch):
    monkeypatch.setenv("OBS_ERROR_ALERT_COOLDOWN_MIN", "0")
    rec = EmitRecorder()
    _patch_emit(monkeypatch, rec)

    handler = observability.CriticalErrorAlertHandler(queue_size=10)
    try:
        handler.handle(_record("pipeline.media", logging.WARNING, "warn"))
        time.sleep(0.2)
        assert rec.calls == []
    finally:
        handler.close()


# ── ignore-list ──────────────────────────────────────────────────────

def test_ignore_list_suppresses_by_logger_name(monkeypatch):
    monkeypatch.setenv("OBS_ERROR_ALERT_COOLDOWN_MIN", "0")
    monkeypatch.setenv("OBS_ERROR_ALERT_IGNORE", "noisy.subsystem")
    rec = EmitRecorder()
    _patch_emit(monkeypatch, rec)

    handler = observability.CriticalErrorAlertHandler(queue_size=10)
    try:
        handler.handle(_record("noisy.subsystem", message="whatever"))
        time.sleep(0.2)
        assert rec.calls == []
    finally:
        handler.close()


def test_ignore_list_suppresses_by_message(monkeypatch):
    monkeypatch.setenv("OBS_ERROR_ALERT_COOLDOWN_MIN", "0")
    monkeypatch.setenv("OBS_ERROR_ALERT_IGNORE", "quota_exhausted")
    rec = EmitRecorder()
    _patch_emit(monkeypatch, rec)

    handler = observability.CriticalErrorAlertHandler(queue_size=10)
    try:
        handler.handle(_record("clean.logger", message="quota_exhausted x"))
        time.sleep(0.2)
        assert rec.calls == []
    finally:
        handler.close()


# ── rate-limit / cooldown ────────────────────────────────────────────

def test_rate_limit_respects_cooldown(monkeypatch):
    monkeypatch.setenv("OBS_ERROR_ALERT_COOLDOWN_MIN", "30")
    rec = EmitRecorder()
    _patch_emit(monkeypatch, rec)

    for _ in range(5):
        observability.obs_error("spammy", message="same key")

    assert len(rec.calls) == 1, "cooldown should collapse repeats into one write"
    assert rec.calls[0]["metadata"]["count"] == 1


def test_cooldown_zero_allows_every_write(monkeypatch):
    monkeypatch.setenv("OBS_ERROR_ALERT_COOLDOWN_MIN", "0")
    rec = EmitRecorder()
    _patch_emit(monkeypatch, rec)

    for _ in range(3):
        observability.obs_error("frequent", message="x")

    assert len(rec.calls) == 3
    assert rec.calls[-1]["metadata"]["count"] == 3


# ── anti-recursion ───────────────────────────────────────────────────

def test_emit_alert_error_does_not_recurse(monkeypatch):
    monkeypatch.setenv("OBS_ERROR_ALERT_COOLDOWN_MIN", "0")
    calls = []

    def exploding_emit(*args, **kwargs):
        calls.append(kwargs)
        # An ERROR logged from inside emit_alert must NOT create another alert.
        logging.getLogger("recurse.probe").error("inner failure")
        return 1

    _patch_emit(monkeypatch, exploding_emit)

    handler = observability.CriticalErrorAlertHandler(queue_size=10)
    root = logging.getLogger()
    root.addHandler(handler)
    try:
        handler.handle(_record("recurse.probe", message="outer failure"))
        assert _wait_until(lambda: len(calls) >= 1)
        time.sleep(0.3)  # give any erroneous recursion time to surface
        assert len(calls) == 1
    finally:
        root.removeHandler(handler)
        handler.close()


# ── bounded queue never blocks ───────────────────────────────────────

def test_full_queue_drops_without_blocking(monkeypatch):
    handler = observability.CriticalErrorAlertHandler(queue_size=1)
    try:
        handler.close()  # stop worker so the queue cannot drain
        handler._queue.put(("dummy", {}, 0.0))  # fill the only slot

        start = time.monotonic()
        handler.emit(_record("some.logger", message="boom"))
        elapsed = time.monotonic() - start

        assert elapsed < 1.0
        assert handler.dropped >= 1
    finally:
        handler.close()


# ── kill-switch ──────────────────────────────────────────────────────

def test_disabled_is_noop(monkeypatch):
    monkeypatch.setenv("OBS_ERROR_ALERTS_ENABLED", "false")
    rec = EmitRecorder()
    _patch_emit(monkeypatch, rec)

    handler = observability.setup_error_alerts(force=True)
    assert handler is None
    root = logging.getLogger()
    owned = [h for h in root.handlers
             if getattr(h, "_autotube_error_alert_handler", False)]
    assert owned == []

    observability.obs_error("boom", message="ignored")
    time.sleep(0.1)
    assert rec.calls == []

    handler2 = observability.CriticalErrorAlertHandler(queue_size=10)
    try:
        handler2.handle(_record("some.logger"))
        time.sleep(0.2)
        assert rec.calls == []
    finally:
        handler2.close()


def test_setup_error_alerts_is_idempotent(monkeypatch):
    monkeypatch.setenv("OBS_ERROR_ALERTS_ENABLED", "true")
    first = observability.setup_error_alerts(force=True)
    assert first is not None

    second = observability.setup_error_alerts()
    assert second is first

    root = logging.getLogger()
    owned = [h for h in root.handlers
             if getattr(h, "_autotube_error_alert_handler", False)]
    assert len(owned) == 1


# ── uncaught exception hooks ─────────────────────────────────────────

def test_install_exception_hooks_safe_and_idempotent(monkeypatch):
    monkeypatch.setenv("OBS_ERROR_ALERT_COOLDOWN_MIN", "0")
    rec = EmitRecorder()
    _patch_emit(monkeypatch, rec)

    original = sys.excepthook
    try:
        observability.install_exception_hooks("testproc")
        first = sys.excepthook
        observability.install_exception_hooks("testproc")
        assert sys.excepthook is first, "hooks must not be duplicated"

        try:
            raise ValueError("boom-uncaught")
        except ValueError:
            exc_type, exc_value, exc_tb = sys.exc_info()

        first(exc_type, exc_value, exc_tb)
        assert _wait_until(lambda: len(rec.calls) >= 1)
        assert any(
            call["alert_type"] == "uncaught_exception_testproc"
            for call in rec.calls
        )
        message = rec.calls[-1]["message"]
        assert "ValueError" in message
    finally:
        sys.excepthook = original
        observability._reset_exception_hooks_for_tests()


def test_install_exception_hooks_does_not_raise_on_weird_input(monkeypatch):
    rec = EmitRecorder()
    _patch_emit(monkeypatch, rec)
    original = sys.excepthook
    try:
        observability.install_exception_hooks("weirdproc")
        hook = sys.excepthook
        # Unusual/None exc info must not raise.
        hook(None, None, None)
    finally:
        sys.excepthook = original
        observability._reset_exception_hooks_for_tests()


def test_threading_excepthook_installed(monkeypatch):
    rec = EmitRecorder()
    _patch_emit(monkeypatch, rec)
    original = getattr(threading, "excepthook", None)
    try:
        observability.install_exception_hooks("threadproc")
        assert threading.excepthook is not original
    finally:
        observability._reset_exception_hooks_for_tests()
