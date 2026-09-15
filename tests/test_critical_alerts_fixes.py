"""Tests de los fixes de alertas críticas (sep 2026).

Cubre:
- Circuit breaker de proveedor LLM (config.model_pool) y detección de errores
  no reintentables (cuota/creditos/auth).
- Dirección del mismatch de visibilidad long-form (solo crítico si YouTube está
  MÁS restringido que la BD).
"""

import pytest

from config import model_pool as mp
from api.services import review_governance as rg
from api.services import yt_state_reconciler as rec


# ── is_non_retryable_error ──────────────────────────────────────

@pytest.mark.parametrize("msg,expected", [
    ("Error code: 429 - insufficient_quota", True),
    ("You have no credits remaining", True),
    ("credit_balance_exhausted", True),
    ("Invalid API key provided", True),
    ("AuthenticationError: invalid api_key", True),
    ("Connection error - timed out", False),
    ("Rate limit reached for requests", False),
    ("temporary server error 500", False),
])
def test_is_non_retryable_error(msg, expected):
    assert mp.is_non_retryable_error(Exception(msg)) is expected


# ── provider circuit breaker ────────────────────────────────────

def test_mark_and_check_provider_availability():
    mp.reset_provider_circuit()
    assert mp.provider_is_available("openai") is True
    mp.mark_provider_unavailable("openai", ttl_seconds=60)
    assert mp.provider_is_available("openai") is False
    assert mp.provider_is_available("deepseek") is True
    mp.reset_provider_circuit()


def test_provider_cooldown_expires(monkeypatch):
    mp.reset_provider_circuit()
    mp.mark_provider_unavailable("openai", ttl_seconds=10)
    # Avanzar el reloj más allá del TTL
    monkeypatch.setattr(mp.time, "time", lambda: mp._PROVIDER_DISABLED_UNTIL["openai"] + 1)
    assert mp.provider_is_available("openai") is True
    mp.reset_provider_circuit()


def test_iter_models_skips_disabled_provider():
    mp.reset_provider_circuit()
    pool = mp.ModelPool(entries=[
        mp.ModelEntry(provider="deepseek", model_id="deepseek-chat",
                      api_key="k1", base_url="https://api.deepseek.com"),
        mp.ModelEntry(provider="openai", model_id="gpt-4o-mini",
                      api_key="k2", base_url="https://api.openai.com/v1"),
    ])
    mp.mark_provider_unavailable("openai", ttl_seconds=60)
    providers = [e.provider for e, _ in pool.iter_models()]
    assert providers == ["deepseek"]
    mp.reset_provider_circuit()


def test_iter_models_fails_open_when_all_disabled():
    mp.reset_provider_circuit()
    pool = mp.ModelPool(entries=[
        mp.ModelEntry(provider="deepseek", model_id="deepseek-chat",
                      api_key="k1", base_url="https://api.deepseek.com"),
        mp.ModelEntry(provider="openai", model_id="gpt-4o-mini",
                      api_key="k2", base_url="https://api.openai.com/v1"),
    ])
    mp.mark_provider_unavailable("openai", ttl_seconds=60)
    mp.mark_provider_unavailable("deepseek", ttl_seconds=60)
    providers = [e.provider for e, _ in pool.iter_models()]
    assert set(providers) == {"deepseek", "openai"}  # fail-open
    mp.reset_provider_circuit()


# ── dangerous vs benign visibility mismatch ─────────────────────

@pytest.mark.parametrize("db_privacy,external,expected", [
    ("public", "private", True),        # YouTube más restrictivo → peligroso
    ("public", "removed", True),
    ("public", "age_restricted", True),
    ("unlisted", "private", True),
    ("private", "public", False),       # lag benigno (verificador sin actualizar)
    ("unlisted", "public", False),
    ("public", "public", False),
    ("private", "private", False),
    ("", "public", False),
])
def test_review_dangerous_visibility_mismatch(db_privacy, external, expected):
    assert rg._is_dangerous_visibility_mismatch(db_privacy, external) is expected


# ── reconcile_recent_videos direction ───────────────────────────

class _Rows:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None


class _VidCtx:
    def __init__(self, fake):
        self.fake = fake

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=()):
        low = sql.lower().strip()
        if low.startswith("select") and "from videos" in low:
            return _Rows(self.fake.videos)
        if low.startswith("update videos"):
            self.fake.updates.append((sql, params))
            return _Rows([])
        return _Rows([])

    def commit(self):
        pass


class FakeVideosDB:
    def __init__(self, videos):
        self.videos = videos
        self.updates = []

    def _connect(self):
        return _VidCtx(self)


def _video(vid, privacy, status="published"):
    return {
        "id": vid, "channel_id": 4, "yt_video_id": f"YT{vid}",
        "status": status, "privacy_status": privacy,
        "yt_visibility": "", "yt_checked_at": None, "titulo_final": "t",
    }


def test_reconcile_benign_lag_does_not_alert(monkeypatch):
    db = FakeVideosDB([_video(10, privacy="private", status="uploaded_private")])
    monkeypatch.setattr(rec, "classify_video_visibility", lambda yt: "public")
    monkeypatch.setattr(rec, "_feed_public_ids", lambda db_, cid: {})

    emitted = []
    monkeypatch.setattr("api.services.lifecycle_monitor.emit_alert",
                        lambda *a, **k: emitted.append(k) or 1)

    summary = rec.reconcile_recent_videos(db)
    assert summary["checked"] == 1
    assert summary["alerts"] == 0
    assert emitted == [], "benign lag must not emit a critical alert"


def test_reconcile_skips_known_deleted_video(monkeypatch):
    """Vídeo ya marcado deleted_on_yt: la retirada ya está registrada → sin alerta."""
    db = FakeVideosDB([_video(12, privacy="public", status="deleted_on_yt")])
    monkeypatch.setattr(rec, "classify_video_visibility", lambda yt: "removed")
    monkeypatch.setattr(rec, "_feed_public_ids", lambda db_, cid: {})

    emitted = []
    monkeypatch.setattr("api.services.lifecycle_monitor.emit_alert",
                        lambda *a, **k: emitted.append(k) or 1)

    summary = rec.reconcile_recent_videos(db)
    assert summary["alerts"] == 0
    assert emitted == []


def test_reconcile_dangerous_mismatch_alerts(monkeypatch):
    db = FakeVideosDB([_video(11, privacy="public", status="published")])
    monkeypatch.setattr(rec, "classify_video_visibility", lambda yt: "private")
    monkeypatch.setattr(rec, "_feed_public_ids", lambda db_, cid: {})

    emitted = []
    monkeypatch.setattr("api.services.lifecycle_monitor.emit_alert",
                        lambda *a, **k: emitted.append(k) or 1)

    summary = rec.reconcile_recent_videos(db)
    assert summary["alerts"] == 1
    assert emitted and emitted[0]["alert_type"] == rec.ALERT_TYPE_LONGFORM_MISMATCH
    assert emitted[0]["severity"] == "critical"
