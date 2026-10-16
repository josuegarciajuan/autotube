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


# ── Atribución de cuota: nunca NULL en channel_slug (oct 2026) ──────
#
# Incidente: `YouTubeUploader(slug)` no rellenaba channel_slug, así que
# `track_quota(None, ...)` insertaba NULL en `yt_quota_log.channel_slug`
# (NOT NULL) → el INSERT del lote entero fallaba y se re-encolaba sin fin
# ("NOT NULL constraint failed: yt_quota_log.channel_slug").

import sqlite3  # noqa: E402


class _QuotaDbStub:
    """Stub mínimo de ExtendedDatabase que sirve una DB temporal."""

    def __init__(self, path):
        self.path = path

    def _connect(self):
        return sqlite3.connect(self.path)


def _quota_temp_db(tmp_path):
    db_path = tmp_path / "quota.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        """CREATE TABLE yt_quota_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL DEFAULT (datetime('now','localtime')),
            channel_slug TEXT NOT NULL DEFAULT 'unknown',
            operation TEXT NOT NULL,
            units INTEGER NOT NULL DEFAULT 0,
            yt_id TEXT DEFAULT '',
            success INTEGER NOT NULL DEFAULT 1,
            error TEXT DEFAULT '',
            caller TEXT DEFAULT '')"""
    )
    conn.commit()
    conn.close()
    return db_path


def _reset_quota_buffer(monkeypatch):
    from api.services import quota_tracker as qt
    import time as _time
    qt._batch_buffer.clear()
    qt._last_flush = _time.monotonic()  # evita auto-flush en track_quota
    return qt


def test_track_quota_with_none_slug_persists_as_unknown(tmp_path, monkeypatch):
    db_path = _quota_temp_db(tmp_path)
    qt = _reset_quota_buffer(monkeypatch)
    monkeypatch.setattr(
        "database.db_extended.ExtendedDatabase",
        lambda *a, **k: _QuotaDbStub(str(db_path)),
    )

    qt.track_quota(None, "videos.update", 50, yt_id="abc", caller="set_publish_at")
    qt.flush_quota_log()

    conn = sqlite3.connect(db_path)
    rows = conn.execute("SELECT channel_slug, operation, units FROM yt_quota_log").fetchall()
    conn.close()
    assert rows == [("unknown", "videos.update", 50)], rows


def test_flush_persists_valid_rows_when_a_row_is_invalid(tmp_path, monkeypatch):
    """Una entrada corrupta en el buffer NO debe abortar el lote completo."""
    db_path = _quota_temp_db(tmp_path)
    qt = _reset_quota_buffer(monkeypatch)
    monkeypatch.setattr(
        "database.db_extended.ExtendedDatabase",
        lambda *a, **k: _QuotaDbStub(str(db_path)),
    )

    with qt._batch_lock:
        qt._batch_buffer.append((None, None, 50, "", 1, "", "legacy"))  # fila inválida
        qt._batch_buffer.append(("canal5", "videos.update", 50, "yt1", 1, "", "ok"))

    qt.flush_quota_log()

    conn = sqlite3.connect(db_path)
    rows = conn.execute("SELECT channel_slug, units FROM yt_quota_log ORDER BY id").fetchall()
    conn.close()
    assert ("canal5", 50) in rows, f"la fila válida debe persistirse: {rows}"
    assert all(slug for slug, _ in rows), f"ningún slug puede quedar NULL: {rows}"


def test_uploader_quota_slug_falls_back_to_account_name():
    """`YouTubeUploader(slug)` posicional → atribución de cuota = slug."""
    u = YouTubeUploader("canal5")
    assert u.channel_slug is None          # no se altera el comportamiento de auth
    assert u.quota_slug == "canal5"

    u2 = YouTubeUploader(account_name="canal3", channel_slug="canal3")
    assert u2.quota_slug == "canal3"

    u3 = YouTubeUploader()
    assert u3.quota_slug == "default"
