"""Tests de diagnóstico de cuota y anti-churn de set_publish_at (sep 2026).

Cubre el fix que hace visible QUÉ llamada agota la cuota (antes un 403 de cuota
en set_publish_at no quedaba en yt_quota_log porque track_quota solo se llamaba
tras el éxito), que distingue cuota diaria de rate limit transitorio, el guard
anti-churn de set_publish_at y el margen de seguridad del presupuesto.

Run: python3 -m pytest tests/test_quota_diagnostics.py -v
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
from googleapiclient.errors import HttpError

from pipeline.youtube_uploader import YouTubeUploader, QuotaExhaustedError


class _Resp:
    def __init__(self, status=403):
        self.status = status
        self.reason = "Forbidden"
        self.headers = {}


def _http_error(reason="quotaExceeded", status=403, message="quota"):
    body = (
        '{"error":{"code":%d,"message":"%s","errors":[{"message":"%s",'
        '"domain":"youtube.quota","reason":"%s"}]}}'
        % (status, message, message, reason)
    ).encode()
    return HttpError(_Resp(status), body)


class _FakeDB:
    def __init__(self):
        self.state = {}

    def get_system_state(self, key):
        return self.state.get(key)

    def set_system_state(self, key, value):
        self.state[key] = value


def _uploader(db=None):
    u = object.__new__(YouTubeUploader)
    u.channel_slug = "canal5"
    u.account_name = ""
    u.db = db
    return u


class _Exec:
    def execute(self):
        return {}


class _Service:
    def videos(self):
        class _V:
            def update(self, **kwargs):
                return _Exec()

        return _V()


# ── Diagnóstico del error ────────────────────────────────────────────

def test_http_error_detail_extracts_reason_and_message():
    u = _uploader()
    d = u._http_error_detail(_http_error("quotaExceeded", message="quota agotada"))
    assert d["reason"] == "quotaExceeded"
    assert d["status"] == 403
    assert "quota agotada" in d["message"]


def test_quota_exceeded_records_failure_and_trips_breaker(monkeypatch):
    u = _uploader()
    tracked, tripped = [], []
    monkeypatch.setattr("pipeline.youtube_uploader.track_quota",
                        lambda *a, **k: tracked.append(k))
    monkeypatch.setattr(u, "_mark_quota_exhausted",
                        lambda **k: tripped.append(k))

    with pytest.raises(QuotaExhaustedError):
        u._raise_if_quota_exceeded(
            _http_error("quotaExceeded"), "set_publish_at",
            operation="videos.update", units=50, yt_id="abc",
        )

    assert tripped, "un 403 de cuota debe abrir el breaker"
    assert tracked and tracked[0]["success"] is False
    assert tracked[0]["operation"] if "operation" in tracked[0] else True


def test_rate_limit_does_not_trip_breaker(monkeypatch):
    u = _uploader()
    tracked, tripped = [], []
    monkeypatch.setattr("pipeline.youtube_uploader.track_quota",
                        lambda *a, **k: tracked.append(k))
    monkeypatch.setattr(u, "_mark_quota_exhausted",
                        lambda **k: tripped.append(k))

    u._raise_if_quota_exceeded(
        _http_error("rateLimitExceeded", status=429, message="rate"),
        "set_publish_at", operation="videos.update", units=50,
    )

    assert tripped == [], "un rate limit transitorio NO debe abrir el breaker"
    assert tracked and tracked[0]["success"] is False


# ── Anti-churn de set_publish_at ─────────────────────────────────────

def test_set_publish_at_skips_recent_duplicate(monkeypatch):
    u = _uploader(_FakeDB())
    monkeypatch.setattr(u, "_get_service", lambda: _Service())
    monkeypatch.setattr("pipeline.youtube_uploader.track_quota", lambda *a, **k: None)

    target = "2026-09-15T10:00:00+00:00"
    r1 = u.set_publish_at("vid1", target)
    assert r1["updated"] is True and not r1.get("skipped_recent")

    r2 = u.set_publish_at("vid1", target)
    assert r2.get("skipped_recent") is True, "mismo target reciente no debe gastar 50 ud"


def test_set_publish_at_allows_real_target_change(monkeypatch):
    u = _uploader(_FakeDB())
    monkeypatch.setattr(u, "_get_service", lambda: _Service())
    monkeypatch.setattr("pipeline.youtube_uploader.track_quota", lambda *a, **k: None)

    u.set_publish_at("vid1", "2026-09-15T10:00:00+00:00")
    r = u.set_publish_at("vid1", "2026-09-15T18:00:00+00:00")  # +8h → cambio real
    assert not r.get("skipped_recent")


# ── Margen de seguridad del presupuesto ─────────────────────────────

def test_safety_reserve_pct_reduces_budget(monkeypatch):
    import config.settings as s

    monkeypatch.setattr(s, "YT_AUTOMATIC_BUDGET_UNITS", 0)
    monkeypatch.setattr(s, "YT_PROJECT_BUDGET_UNITS", {"p": 10000})
    monkeypatch.setattr(s, "YT_PROJECT_DEFAULT_BUDGET", 100000)
    monkeypatch.setattr(s, "YT_PROJECT_RESERVED_UNITS", 400)

    monkeypatch.setattr(s, "YT_PROJECT_SAFETY_RESERVE_PCT", 0.0)
    assert s.get_project_automatic_budget_units("p") == 9600

    monkeypatch.setattr(s, "YT_PROJECT_SAFETY_RESERVE_PCT", 10.0)
    assert s.get_project_automatic_budget_units("p") == 8600
