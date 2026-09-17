"""Tests de T3.1: endpoint de métricas del experimento."""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from api.routers import analytics  # noqa: E402


class _FakeDB:
    def __init__(self, baseline=None, reminders=None):
        self._baseline = baseline
        self._reminders = reminders or []

    def get_system_state(self, key):
        if key == "experiment_started_at":
            return "2026-09-17T15:32:42"
        if key == "experiment_baseline":
            return self._baseline
        return None

    def list_scheduled_reminders(self, status=None, limit=50):
        return self._reminders


def test_experiment_endpoint_shape(monkeypatch):
    monkeypatch.setattr(analytics, "get_db", lambda: _FakeDB(
        baseline='{"channels": []}',
        reminders=[
            {"alert_type": "experiment_checkpoint", "title": "T+7",
             "due_at": "2026-09-24T09:00:00", "status": "pending"},
            {"alert_type": "other", "title": "x", "due_at": "x", "status": "pending"},
        ],
    ))
    monkeypatch.setattr(
        "scripts.experiment_tracker.compute_baseline",
        lambda db: {"channels": [], "captured_at": "now"},
    )

    res = analytics.get_experiment_metrics()
    assert res["started_at"] == "2026-09-17T15:32:42"
    assert res["baseline"] == {"channels": []}
    assert res["live"] == {"channels": [], "captured_at": "now"}
    assert len(res["checkpoints"]) == 1
    assert res["checkpoints"][0]["title"] == "T+7"


def test_experiment_endpoint_survives_bad_baseline(monkeypatch):
    monkeypatch.setattr(analytics, "get_db", lambda: _FakeDB(baseline="{no-json"))
    monkeypatch.setattr(
        "scripts.experiment_tracker.compute_baseline",
        lambda db: {"channels": []},
    )
    res = analytics.get_experiment_metrics()
    assert res["baseline"] is None
    assert res["live"] == {"channels": []}
