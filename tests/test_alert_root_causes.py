"""Regression tests for the alert root-cause fixes (oct 2026).

Covers:
  * checkpoint TTS restoration with timestamps (video #2714)
  * distributed stall policy (don't cancel inflight/pending work)
  * shorts daily-cap deferral (slots #40365/#40366)
  * coverage alert message refresh (stale deficit days)
"""

import json
import sqlite3
from datetime import datetime

from api.services.full_pipeline_worker import _restore_tts_checkpoint
from api.services import publish_coverage
from api.services import shorts_scheduler
from pipeline_dist.orchestrator_dist import _dist_stall_is_fatal


def test_restore_tts_checkpoint_loads_timestamps(tmp_path):
    audio = tmp_path / "narration.mp3"
    audio.write_bytes(b"audio")
    ts = tmp_path / "narration_timestamps.json"
    ts.write_text(json.dumps([{"start_ms": 0, "end_ms": 1200}]), encoding="utf-8")

    out = _restore_tts_checkpoint({
        "audio_path": str(audio),
        "timestamps_path": str(ts),
    })
    assert out is not None
    assert out["timestamps"] == [{"start_ms": 0, "end_ms": 1200}]
    assert out["timestamps_path"] == str(ts)


def test_restore_tts_checkpoint_missing_timestamps_returns_none(tmp_path):
    audio = tmp_path / "narration.mp3"
    audio.write_bytes(b"audio")
    assert _restore_tts_checkpoint({"audio_path": str(audio)}) is None


def test_restore_tts_checkpoint_missing_audio_returns_none(tmp_path):
    assert _restore_tts_checkpoint({
        "audio_path": str(tmp_path / "gone.mp3"),
        "timestamps": [{"start_ms": 0, "end_ms": 1}],
    }) is None


def test_dist_stall_does_not_cancel_inflight_units():
    assert not _dist_stall_is_fatal(
        status_seen=True, inflight=4, pending=0, elapsed=1200, stall_sec=900,
    )
    assert not _dist_stall_is_fatal(
        status_seen=True, inflight=0, pending=3, elapsed=1200, stall_sec=900,
    )
    # Engine not publishing results: the old fast-fail still applies.
    assert _dist_stall_is_fatal(
        status_seen=False, inflight=0, pending=0, elapsed=1200, stall_sec=900,
    )
    # Idle engine with no work and no progress: cancelled.
    assert _dist_stall_is_fatal(
        status_seen=True, inflight=0, pending=0, elapsed=1200, stall_sec=900,
    )


def test_cap_gate_reasons():
    assert shorts_scheduler._is_cap_gate_reason("tope duro diario del canal")
    assert shorts_scheduler._is_cap_gate_reason("tope global diario de shorts")
    assert not shorts_scheduler._is_cap_gate_reason("espaciado mismo-canal (120s)")


def test_defer_cap_blocked_slot_moves_one_day(tmp_path, monkeypatch):
    db_path = tmp_path / "alerts.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute("""
            CREATE TABLE shorts_planned_slots (
                id INTEGER PRIMARY KEY,
                date_key TEXT NOT NULL,
                scheduled_at TIMESTAMP NOT NULL,
                target_upload_at TIMESTAMP,
                target_publish_at TIMESTAMP,
                status TEXT NOT NULL DEFAULT 'pending',
                job_id INTEGER,
                error_message TEXT,
                updated_at TIMESTAMP
            )
        """)
        conn.execute(
            """INSERT INTO shorts_planned_slots
               (id, date_key, scheduled_at, target_upload_at, target_publish_at, status)
               VALUES (1, '2026-10-22', '2026-10-22 06:31:27',
                       '2026-10-22 05:52:00', '2026-10-22 05:52:00', 'pending')"""
        )
        conn.commit()

    monkeypatch.setattr("config.settings.DATABASE_PATH", str(db_path))
    shorts_scheduler._defer_cap_blocked_slot(None, 1, "tope duro diario del canal")

    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT date_key, scheduled_at, target_upload_at, status, error_message "
            "FROM shorts_planned_slots WHERE id=1"
        ).fetchone()
    assert row[0] == "2026-10-23"
    assert row[1].startswith("2026-10-23 06:31:27")
    assert row[2].startswith("2026-10-23 05:52:00")
    assert row[3] == "pending"
    assert "deferred +1 day" in row[4]


def test_deficit_alert_refreshes_even_with_daily_marker(monkeypatch):
    today = datetime.now().date().isoformat()
    calls = []

    class FakeDB:
        def get_system_state(self, key):
            return today

        def set_system_state(self, key, value):
            calls.append(("set_state", key, value))

    def fake_create_alert(db, **kwargs):
        calls.append(("alert", kwargs))
        return 42

    monkeypatch.setattr(
        "api.services.lifecycle_monitor.create_alert", fake_create_alert,
    )
    created = publish_coverage._maybe_alert_deficit(
        FakeDB(), "canal2", 3, ["2026-10-23"],
    )
    assert created is True
    assert any(item[0] == "alert" for item in calls)


def test_worker_requeues_transient_failure(monkeypatch):
    from api.services.full_pipeline_worker import _maybe_requeue_transient

    events = []

    class FakeDB:
        def get_job(self, job_id):
            return {"id": job_id, "status": "failed",
                    "error_msg": "Fallo el ensamblaje del video (RAM insuficiente)"}

        def increment_retry(self, job_id):
            events.append(("retry", job_id))
            return 1

        def update_job_requeue(self, job_id, error_msg=None):
            events.append(("queued", job_id, error_msg))

        def update_video(self, video_id, **kwargs):
            events.append(("video", video_id, kwargs))

    monkeypatch.setattr("config.settings.MAX_RETRY_ATTEMPTS", 3)
    assert _maybe_requeue_transient(FakeDB(), 7, 11, "") is True
    assert ("retry", 7) in events
    assert any(e[0] == "queued" and e[1] == 7 for e in events)
    assert any(e[0] == "video" and e[1] == 11 for e in events)


def test_worker_does_not_requeue_permanent_failure(monkeypatch):
    from api.services.full_pipeline_worker import _maybe_requeue_transient

    class FakeDB:
        def get_job(self, job_id):
            return {"id": job_id, "status": "failed",
                    "error_msg": "pre-validación: título inválido"}

    monkeypatch.setattr("config.settings.MAX_RETRY_ATTEMPTS", 3)
    assert _maybe_requeue_transient(FakeDB(), 7, 11, "") is False
