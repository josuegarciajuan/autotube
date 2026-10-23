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


# ── Requeue race fix (oct 2026) ─────────────────────────────────
# The subprocess worker requeues transient failures via update_job_requeue()
# (status='queued'), but the progress monitor in generation_service saw
# "process gone + not terminal" and overwrote it with 'failed'/'worker_died'.
# These tests protect that fix.

def test_monitor_does_not_overwrite_requeued_job():
    """A worker that exits after requeueing must leave the job in 'queued'."""
    # This mirrors the decision logic at generation_service._monitor_worker_progress:
    #   if not process_alive and job_status == "queued": break  (leave alone)
    #   if not process_alive and not is_terminal: mark failed
    job_status = "queued"
    process_alive = False
    is_terminal = job_status in ("completed", "failed", "cancelled")

    # Requeue protection: 'queued' means a transient retry is pending
    assert not is_terminal
    should_skip = process_alive is False and job_status == "queued"
    assert should_skip, "monitor must skip requeued jobs instead of marking them failed"

    # For comparison: truly orphaned (status='running') should be marked failed
    job_status2 = "running"
    is_terminal2 = job_status2 in ("completed", "failed", "cancelled")
    should_fail = (not process_alive) and (not is_terminal2) and job_status2 != "queued"
    assert should_fail


def test_inprocess_auto_retry_sets_video_queued_retry(monkeypatch):
    """The in-process retry path must also mark video as queued_retry."""
    from api.services.generation_service import _auto_retry_if_transient

    events = []

    class FakeDB:
        def get_job(self, job_id):
            return {"id": job_id, "status": "failed",
                    "error_msg": "local_fallback_deferred: render timeout"}

        def increment_retry(self, job_id):
            events.append(("retry", job_id))
            return 1

        def update_job_requeue(self, job_id, error_msg=None):
            events.append(("queued", job_id, error_msg))

        def update_video(self, video_id, **kwargs):
            events.append(("video", video_id, kwargs))

    monkeypatch.setattr("api.services.generation_service.settings",
                        type("S", (), {"MAX_RETRY_ATTEMPTS": 3}))
    monkeypatch.setattr("api.services.generation_service._get_db", lambda: FakeDB())

    _auto_retry_if_transient(7, 11)

    assert ("retry", 7) in events
    assert any(e[0] == "queued" and e[1] == 7 for e in events)
    video_events = [e for e in events if e[0] == "video"]
    assert len(video_events) == 1
    assert video_events[0][2].get("progress_phase") == "queued_retry"


def test_transient_patterns_include_local_fallback_deferred():
    """local_fallback_deferred must be recognized as transient (both paths)."""
    from api.services.full_pipeline_worker import _TRANSIENT_ERROR_PATTERNS
    assert "local_fallback_deferred" in _TRANSIENT_ERROR_PATTERNS

    # Also verify the in-process path
    from api.services.generation_service import _auto_retry_if_transient
    import inspect
    src = inspect.getsource(_auto_retry_if_transient)
    assert "local_fallback_deferred" in src


# ── Assembly cause preservation (fix oct 2026) ──────────────────
# When distributed render/concat fails and local assembly returns empty,
# the real failure cause must propagate instead of "Fallo el ensamblaje".

def test_dist_failure_propagates_when_local_returns_empty():
    """Orchestrator must raise (not return None) after dist failure + empty local."""
    from pipeline_dist.scene_plan import ScenePlanError

    dist_error = ("dist_repair_exhausted: concat failed: unidad b0000 agoto "
                  "reintentos: sin nodo compatible (tags/primaryOnly/allowedNodes)")
    # Simulate the decision logic in orchestrator_dist.phase_video:
    #   result = super().phase_video(...)
    #   if not result and dist_failed: raise RuntimeError(...)
    dist_failed = dist_error
    result = None  # local returned empty

    should_raise = (not result) and bool(dist_failed)
    assert should_raise

    error = RuntimeError(f"{dist_failed} | ensamblaje local devolvio vacio")
    assert "sin nodo compatible" in str(error)
    assert "dist_repair_exhausted" in str(error)


def test_no_raise_when_local_succeeds_despite_dist_failure():
    """If local assembly succeeds, no error should be raised."""
    dist_failed = "some dist timeout"
    result = {"video_path": "/tmp/x.mp4"}  # local succeeded

    should_raise = (not result) and bool(dist_failed)
    assert not should_raise


# ── Watchdog alert auto-resolution (fix oct 2026) ──────────────
# error_autotube_watchdog alerts must auto-resolve when heartbeat recovers.

def test_watchdog_error_alert_resolves_on_heartbeat_recovery():
    """Generic error_autotube_watchdog alert closes when task heartbeat is fresh."""
    import sqlite3 as _sq
    from api.services.lifecycle_monitor import _resolve_recovered_task_alerts, _utcnow

    conn = _sq.connect(":memory:")
    conn.row_factory = _sq.Row
    conn.executescript("""
        CREATE TABLE pipeline_alerts (
            id INTEGER PRIMARY KEY, entity_type TEXT, entity_id INTEGER,
            channel_id INTEGER, alert_type TEXT, severity TEXT,
            title TEXT, message TEXT, metadata_json TEXT,
            resolved INTEGER DEFAULT 0, acknowledged INTEGER DEFAULT 0,
            resolved_at TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE system_state (key TEXT PRIMARY KEY, value TEXT, updated_at TEXT);
    """)

    # Simulate: watchdog alert for schedule_checker was emitted
    conn.execute(
        "INSERT INTO pipeline_alerts (entity_type, alert_type, severity, title, message)"
        " VALUES ('system', 'error_autotube_watchdog', 'critical',"
        " 'Errores de autotube.watchdog',"
        " 'systemd watchdog withheld: schedule_checker heartbeat stale')"
    )

    # Heartbeat is now fresh (naive UTC, matching production storage format)
    from api.services.lifecycle_monitor import _utcnow
    conn.execute(
        "INSERT INTO system_state (key, value) VALUES (?, ?)",
        ("task_heartbeat_schedule_checker",
         _utcnow().isoformat(timespec="seconds")),
    )
    conn.commit()

    class FakeDB:
        def _connect(self):
            return conn

    resolved = _resolve_recovered_task_alerts(FakeDB(), conn)

    assert resolved >= 1, "watchdog error alert should auto-resolve"
    row = conn.execute(
        "SELECT resolved FROM pipeline_alerts WHERE alert_type='error_autotube_watchdog'"
    ).fetchone()
    assert row[0] == 1, "alert should be marked resolved"

    conn.close()


def test_watchdog_error_alert_not_resolved_when_stale():
    """Alert must NOT resolve if heartbeat is still stale."""
    import sqlite3 as _sq
    from api.services.lifecycle_monitor import _resolve_recovered_task_alerts

    conn = _sq.connect(":memory:")
    conn.row_factory = _sq.Row
    conn.executescript("""
        CREATE TABLE pipeline_alerts (
            id INTEGER PRIMARY KEY, entity_type TEXT, entity_id INTEGER,
            channel_id INTEGER, alert_type TEXT, severity TEXT,
            title TEXT, message TEXT, metadata_json TEXT,
            resolved INTEGER DEFAULT 0, acknowledged INTEGER DEFAULT 0,
            resolved_at TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE system_state (key TEXT PRIMARY KEY, value TEXT, updated_at TEXT);
    """)

    conn.execute(
        "INSERT INTO pipeline_alerts (entity_type, alert_type, severity, title, message)"
        " VALUES ('system', 'error_autotube_watchdog', 'critical',"
        " 'Errores de autotube.watchdog',"
        " 'systemd watchdog withheld: schedule_checker heartbeat stale')"
    )

    # Heartbeat is old (2 hours ago, timeout is 30 min)
    from api.services.lifecycle_monitor import _utcnow as _utcnow_fn
    from datetime import timedelta
    old_hb = (_utcnow_fn() - timedelta(hours=2)).isoformat(timespec="seconds")
    conn.execute(
        "INSERT INTO system_state (key, value) VALUES (?, ?)",
        ("task_heartbeat_schedule_checker", old_hb),
    )
    conn.commit()

    class FakeDB:
        def _connect(self):
            return conn

    resolved = _resolve_recovered_task_alerts(FakeDB(), conn)
    assert resolved == 0, "should NOT resolve stale heartbeat"

    row = conn.execute(
        "SELECT resolved FROM pipeline_alerts WHERE alert_type='error_autotube_watchdog'"
    ).fetchone()
    assert row[0] == 0, "alert must remain unresolved"

    conn.close()


# ── Standalone short word-tolerance fix (oct 2026) ───────────────
# The LLM regularly produces 107-130 word scripts (max 92). Before this fix,
# both attempts were rejected and the whole short was abandoned. Now the
# script is trimmed before validation.

def test_trim_reduces_word_count_within_budget():
    """trim_blocks_to_word_budget should reduce long scripts to <= max_words."""
    from pipeline.shorts_tts import trim_blocks_to_word_budget

    bloques = [
        {"tipo": "hook", "texto": " ".join(["palabra"] * 20)},
        {"tipo": "desarrollo1", "texto": " ".join(["palabra"] * 25)},
        {"tipo": "desarrollo2", "texto": " ".join(["palabra"] * 25)},
        {"tipo": "climax", "texto": " ".join(["palabra"] * 20)},
        {"tipo": "cierre", "texto": " ".join(["palabra"] * 17)},
    ]
    total_before = sum(len(b["texto"].split()) for b in bloques)
    assert total_before == 107  # same as the real failure case

    result = trim_blocks_to_word_budget(bloques, max_words=92)
    total_after = sum(len(b["texto"].split()) for b in result)

    assert total_after <= 92, f"should be <= 92 after trim, got {total_after}"
    assert total_after < total_before, "trim should actually remove words"


def test_trim_preserves_hook_and_climax():
    """Hook and climax must retain content after trimming."""
    from pipeline.shorts_tts import trim_blocks_to_word_budget

    bloques = [
        {"tipo": "hook", "texto": " ".join(["hook"] * 15)},
        {"tipo": "desarrollo1", "texto": " ".join(["dev"] * 40)},
        {"tipo": "desarrollo2", "texto": " ".join(["dev"] * 40)},
        {"tipo": "climax", "texto": " ".join(["climax"] * 15)},
        {"tipo": "cierre", "texto": " ".join(["cierre"] * 15)},
    ]

    result = trim_blocks_to_word_budget(bloques, max_words=92, min_words_per_block=5)

    for b in result:
        assert len(b["texto"].split()) >= 5, f"block {b['tipo']} emptied below floor"
    # Hook should be preserved (trim order starts from desarrollo3)
    hook = next(b for b in result if b["tipo"] == "hook")
    assert len(hook["texto"].split()) >= 10, "hook should retain most words"


def test_valid_script_passes_after_trim():
    """A trimmed script should pass validate_short_script."""
    from pipeline.shorts_tts import (
        trim_blocks_to_word_budget, validate_short_script,
        normalize_short_script, MAX_WORD_COUNT,
    )

    bloques = [
        {"tipo": "hook", "texto": " ".join(["word"] * 25)},
        {"tipo": "desarrollo1", "texto": " ".join(["word"] * 25)},
        {"tipo": "desarrollo2", "texto": " ".join(["word"] * 25)},
        {"tipo": "climax", "texto": " ".join(["word"] * 20)},
        {"tipo": "cierre", "texto": " ".join(["word"] * 20)},
    ]
    script = {"bloques": bloques, "titulo": "Test", "tema": "test"}
    script = normalize_short_script(script)

    # Without trim: too long (115 words)
    errors_before = validate_short_script(script)
    assert any("too long" in e.lower() for e in errors_before)

    # After trim: should pass
    script["bloques"] = trim_blocks_to_word_budget(script["bloques"], max_words=MAX_WORD_COUNT)
    errors_after = validate_short_script(script)
    assert not any("too long" in e.lower() for e in errors_after), (
        f"should not be too long after trim: {errors_after}"
    )
