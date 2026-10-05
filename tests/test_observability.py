"""Tests for the structured observability core (``pipeline/observability.py``).

Covers: JSON event emission, disabled/off no-op, redaction, context injection,
idempotent setup, fail-open on disk/handler errors, sampling and the API-log
rotation helper.
"""

import json
import logging
import logging.handlers

import pytest

from pipeline import observability


@pytest.fixture
def obs_env(tmp_path, monkeypatch):
    """Fresh obs logging environment backed by a temp dir."""
    log_dir = tmp_path / "obs"
    monkeypatch.setenv("OBS_LOG_ENABLED", "true")
    monkeypatch.setenv("OBS_LOG_LEVEL", "detail")
    monkeypatch.setenv("OBS_LOG_DIR", str(log_dir))
    monkeypatch.setenv("OBS_LOG_MAX_MB", "50")
    monkeypatch.setenv("OBS_LOG_BACKUPS", "3")
    monkeypatch.setenv("OBS_LOG_SAMPLE_RATE", "1.0")

    _reset_obs_state()
    yield log_dir
    _reset_obs_state()


def _reset_obs_state():
    observability.clear_context()
    observability._state.update({
        "configured": False, "enabled": False, "mode": "off",
        "sample_rate": 1.0, "handler": None,
    })
    logger = logging.getLogger(observability.OBS_LOGGER_NAME)
    for handler in list(logger.handlers):
        try:
            logger.removeHandler(handler)
            handler.close()
        except Exception:  # pragma: no cover - defensive
            pass


def _read_events(log_dir):
    path = log_dir / "obs.log"
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


# ── Emission / JSON ──────────────────────────────────────────────────

def test_obs_event_writes_parseable_json(obs_env):
    observability.setup_obs_logging(force=True)
    observability.set_context(channel="canal2", job_id=42)
    observability.obs_event("unit_event", foo="bar", scene_idx=1, n=3)

    events = _read_events(obs_env)
    assert events, "expected at least one JSON event"
    event = events[-1]
    assert event["event"] == "unit_event"
    assert event["foo"] == "bar"
    assert event["channel"] == "canal2"
    assert event["job_id"] == 42
    assert event["scene_idx"] == 1
    assert event["n"] == 3
    assert event["level"] == "info"
    assert "ts" in event


def test_disabled_enabled_false_is_noop(obs_env, monkeypatch):
    monkeypatch.setenv("OBS_LOG_ENABLED", "false")
    observability.setup_obs_logging(force=True)
    observability.obs_event("should_not_write", scene_idx=1)
    assert _read_events(obs_env) == []


def test_level_off_is_noop(obs_env, monkeypatch):
    monkeypatch.setenv("OBS_LOG_ENABLED", "true")
    monkeypatch.setenv("OBS_LOG_LEVEL", "off")
    observability.setup_obs_logging(force=True)
    observability.obs_event("should_not_write", scene_idx=1)
    assert _read_events(obs_env) == []


# ── Redaction ────────────────────────────────────────────────────────

def test_redact_masks_sensitive_keys():
    payload = {
        "token": "abc",
        "api_key": "xyz",
        "authorization": "Bearer 123",
        "cookie": "session=1",
        "secret": "s",
        "password": "p",
        "key": "k",
        "safe": "ok",
        "keywords": ["history"],
        "nested": {"access_token": "t", "public": 1},
    }
    red = observability.redact(payload)
    assert red["token"] == observability._REDACTION_MARK
    assert red["api_key"] == observability._REDACTION_MARK
    assert red["authorization"] == observability._REDACTION_MARK
    assert red["cookie"] == observability._REDACTION_MARK
    assert red["secret"] == observability._REDACTION_MARK
    assert red["password"] == observability._REDACTION_MARK
    assert red["key"] == observability._REDACTION_MARK
    assert red["safe"] == "ok"
    assert red["keywords"] == ["history"]  # not over-redacted
    assert red["nested"]["access_token"] == observability._REDACTION_MARK
    assert red["nested"]["public"] == 1


def test_redact_never_raises_on_weird_input():
    class Weird:
        def __str__(self):
            return "weird"

    assert observability.redact({"a": Weird()}) is not None
    assert observability.redact(None) is None


def test_digest_text_stable_and_safe():
    d1 = observability.digest_text("hello world")
    d2 = observability.digest_text("hello world")
    assert d1 == d2
    assert d1["length"] == 11
    assert len(d1["sha1"]) == 40
    assert "hello" not in json.dumps(d1)


# ── Context filter / contextvars ─────────────────────────────────────

def test_context_filter_injects_correlation():
    observability.set_context(channel="canal3", video_id=9, job_id=1)
    record = logging.LogRecord(
        "x", logging.INFO, __file__, 10, "msg", None, None,
    )
    assert observability.ContextFilter().filter(record) is True
    assert record.channel == "canal3"
    assert record.video_id == 9
    assert record.job_id == 1
    observability.clear_context()


def test_bind_context_restores_previous():
    observability.set_context(channel="canal2")
    with observability.bind_context(phase="script", scene_idx=5):
        ctx = observability.get_context()
        assert ctx["phase"] == "script"
        assert ctx["scene_idx"] == 5
        assert ctx["channel"] == "canal2"
    assert observability.get_context().get("phase") is None
    assert observability.get_context()["channel"] == "canal2"


def test_obs_context_scope_as_decorator():
    @observability.obs_context_scope(phase="media")
    def _fn():
        return observability.get_context().get("phase")

    assert _fn() == "media"
    assert observability.get_context().get("phase") is None


# ── Idempotency ──────────────────────────────────────────────────────

def test_setup_obs_logging_is_idempotent(obs_env):
    observability.setup_obs_logging(force=True)
    logger = logging.getLogger(observability.OBS_LOGGER_NAME)
    owned = [h for h in logger.handlers
             if getattr(h, "_autotube_obs_handler", False)]
    assert len(owned) == 1
    first = observability._state["handler"]

    observability.setup_obs_logging()  # second call, no force
    owned_after = [h for h in logger.handlers
                   if getattr(h, "_autotube_obs_handler", False)]
    assert len(owned_after) == 1
    assert observability._state["handler"] is first


# ── Fail-open ────────────────────────────────────────────────────────

def test_fail_open_when_dir_not_creatable(tmp_path, monkeypatch):
    blocker = tmp_path / "not_a_dir"
    blocker.write_text("x")
    monkeypatch.setenv("OBS_LOG_ENABLED", "true")
    monkeypatch.setenv("OBS_LOG_LEVEL", "detail")
    monkeypatch.setenv("OBS_LOG_DIR", str(blocker / "sub"))
    _reset_obs_state()

    # Must not raise, and obs_event must remain a safe no-op.
    observability.setup_obs_logging(force=True)
    observability.obs_event("boom", scene_idx=1)
    assert observability._state["enabled"] is False
    _reset_obs_state()


def test_fail_open_when_handler_build_fails(obs_env, monkeypatch):
    def _raise(*args, **kwargs):
        raise RuntimeError("handler boom")

    monkeypatch.setattr(observability, "_build_obs_handler", _raise)
    observability.setup_obs_logging(force=True)  # must not raise
    observability.obs_event("boom", scene_idx=1)  # must not raise
    assert observability._state["enabled"] is False


# ── Sampling ─────────────────────────────────────────────────────────

def test_sampling_zero_drops_scene_events(obs_env, monkeypatch):
    monkeypatch.setenv("OBS_LOG_SAMPLE_RATE", "0.0")
    observability.setup_obs_logging(force=True)
    observability.obs_event("scene_asset_selected", scene_idx=1)
    assert _read_events(obs_env) == []


def test_sampling_one_keeps_scene_events(obs_env, monkeypatch):
    monkeypatch.setenv("OBS_LOG_SAMPLE_RATE", "1.0")
    observability.setup_obs_logging(force=True)
    observability.obs_event("scene_asset_selected", scene_idx=1)
    events = _read_events(obs_env)
    assert len(events) == 1
    assert events[0]["scene_idx"] == 1


# ── LLM text policy: detail hashes, trace keeps full ────────────────

def test_llm_prompt_hashed_in_detail(obs_env):
    observability.setup_obs_logging(force=True)
    observability.obs_event("llm_call", prompt="secret prompt")
    event = _read_events(obs_env)[-1]
    assert isinstance(event["prompt"], dict)
    assert "sha1" in event["prompt"]
    assert "secret prompt" not in json.dumps(event)


def test_llm_prompt_full_in_trace(obs_env, monkeypatch):
    monkeypatch.setenv("OBS_LOG_LEVEL", "trace")
    observability.setup_obs_logging(force=True)
    observability.obs_event("llm_call", prompt="secret prompt")
    event = _read_events(obs_env)[-1]
    assert event["prompt"] == "secret prompt"


# ── api.log rotation helper ──────────────────────────────────────────

def test_build_api_log_handler_uses_expected_rotation(tmp_path):
    handler = observability.build_api_log_handler(
        tmp_path / "api.log", max_bytes=12_345, backup_count=7,
    )
    try:
        assert isinstance(handler, logging.handlers.RotatingFileHandler)
        assert handler.maxBytes == 12_345
        assert handler.backupCount == 7
    finally:
        handler.close()


def test_build_api_log_handler_defaults(tmp_path):
    handler = observability.build_api_log_handler(tmp_path / "api.log")
    try:
        assert handler.maxBytes == 50 * 1024 * 1024
        assert handler.backupCount == 10
    finally:
        handler.close()
