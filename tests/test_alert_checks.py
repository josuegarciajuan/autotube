"""Tests for the new non-silent alert checks (Fase 1-4 del plan de alertas).

Cubre:
- _check_tasks_alive: watchdog de loops de fondo (task_stalled)
- _check_awaiting_upload_stuck: videos generados que nunca se suben
- _check_upload_retry_loop: video en bucle de reintentos de subida
- _check_stats_collection_failed: recolección de stats en error (+ auto-resolve)
- _check_short_ready_stuck: shorts renderizados que nunca se suben
- _check_content_safety_starvation: canal sin contenido (inanición)
- _check_platform_publish_failed: fallos cross-platform no-auth
- emit_alert: journal de respaldo cuando la DB no está disponible
"""

import json
import sqlite3
from datetime import datetime

from database.db import init_db
from database.db_extended import ExtendedDatabase
from api.services.lifecycle_monitor import (
    _check_tasks_alive,
    _check_awaiting_upload_stuck,
    _check_upload_retry_loop,
    _check_stats_collection_failed,
    _check_short_ready_stuck,
    _check_video_failed_unalerted,
    _check_content_safety_starvation,
    _check_platform_publish_failed,
    emit_alert,
    touch_task_heartbeat,
    TASK_TIMEOUTS,
    _auto_resolve_completed,
    check_all_health,
)

# ── DDL base: init_db crea schema.sql (videos básica). Aquí añadimos las
# columnas/ tablas que usan los checks nuevos (en producción las crean las
# migraciones v2-v42 de db_extended).
_EXTRA_DDL = """
ALTER TABLE videos ADD COLUMN channel_id INTEGER;
ALTER TABLE videos ADD COLUMN status TEXT DEFAULT 'draft';
ALTER TABLE videos ADD COLUMN progress_phase TEXT;
ALTER TABLE videos ADD COLUMN generation_finished_at TIMESTAMP;
ALTER TABLE videos ADD COLUMN scheduled_upload_at TEXT;
ALTER TABLE videos ADD COLUMN error_message TEXT DEFAULT '';

CREATE TABLE IF NOT EXISTS channels (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    slug    TEXT,
    name    TEXT
);

CREATE TABLE IF NOT EXISTS generation_jobs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id  INTEGER,
    video_id    INTEGER,
    action      TEXT NOT NULL DEFAULT 'generate',
    status      TEXT NOT NULL DEFAULT 'queued',
    progress    INTEGER DEFAULT 0,
    phase       TEXT,
    error_msg   TEXT,
    started_at  TIMESTAMP,
    finished_at TIMESTAMP,
    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS shorts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id      INTEGER NOT NULL,
    type            TEXT NOT NULL DEFAULT 'clip',
    title           TEXT,
    status          TEXT NOT NULL DEFAULT 'pending',
    scheduled_date  TEXT,
    created_at      TEXT DEFAULT (datetime('now')),
    error_message   TEXT,
    file_path       TEXT
);

CREATE TABLE IF NOT EXISTS platform_videos (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    video_id            INTEGER,
    channel_id          INTEGER,
    platform            TEXT NOT NULL,
    status              TEXT NOT NULL DEFAULT 'uploading',
    error_message       TEXT,
    platform_video_id   TEXT,
    platform_video_url  TEXT,
    created_at          TEXT DEFAULT (datetime('now')),
    uploaded_at         TEXT
);

CREATE TABLE IF NOT EXISTS system_state (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL DEFAULT '',
    updated_at  TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS pipeline_alerts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_type     TEXT NOT NULL CHECK(entity_type IN ('video', 'short', 'system', 'channel')),
    entity_id       INTEGER,
    channel_id      INTEGER,
    alert_type      TEXT NOT NULL,
    severity        TEXT NOT NULL DEFAULT 'warning',
    title           TEXT NOT NULL,
    message         TEXT,
    metadata_json   TEXT,
    acknowledged    BOOLEAN DEFAULT 0,
    resolved        BOOLEAN DEFAULT 0,
    resolved_at     TIMESTAMP,
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_alerts_unique_active
ON pipeline_alerts(entity_type, entity_id, alert_type)
WHERE resolved = 0;
"""


def _build_db(tmp_path) -> ExtendedDatabase:
    path = tmp_path / "alert_checks_test.db"
    init_db(str(path))
    with sqlite3.connect(str(path)) as conn:
        conn.executescript(_EXTRA_DDL)
        # Un canal para los checks por canal
        conn.execute("INSERT INTO channels (id, slug, name) VALUES (1, 'canal1', 'Canal Uno')")
        conn.commit()
    return ExtendedDatabase(str(path))


def _insert_video(db, *, vid=1, status='awaiting_upload', channel_id=1, canal='canal1',
                  finished='2026-01-01 00:00:00', scheduled=None, err=''):
    with db._connect() as conn:
        conn.execute(
            """INSERT INTO videos (id, channel_id, canal, video_path, status, progress_phase,
               generation_finished_at, scheduled_upload_at, error_message, created_at)
               VALUES (?, ?, ?, '/tmp/dummy.mp4', ?, 'upload', ?, ?, ?, datetime('now', '-7 days'))""",
            (vid, channel_id, canal, status, finished, scheduled, err),
        )
        conn.commit()


def _insert_failed_video(db, vid=1, error_msg='Real pipeline crash'):
    """Vídeo terminal en 'error' con su último generation_job fallido."""
    with db._connect() as conn:
        conn.execute(
            """INSERT INTO videos (id, channel_id, canal, video_path, status, progress_phase,
               error_message, created_at)
               VALUES (?, 1, 'canal1', '/tmp/dummy.mp4', 'error', 'video',
                       ?, datetime('now', '-6 days'))""",
            (vid, error_msg),
        )
        conn.execute(
            """INSERT INTO generation_jobs (channel_id, video_id, action, status, error_msg)
               VALUES (1, ?, 'generate_only', 'failed', ?)""",
            (vid, error_msg),
        )
        conn.commit()


# ═══════════════════════════════════════════════════════════════
# Check 3: video failed (no re-alert de fallos terminales ya reportados)
# ═══════════════════════════════════════════════════════════════

def test_video_failed_creates_alert_once(tmp_path):
    db = _build_db(tmp_path)
    _insert_failed_video(db, vid=1)
    assert _check_video_failed_unalerted(db) == 1
    # El fallo terminal ya se reportó → resolver la alerta NO debe
    # re-crearla en el siguiente ciclo (bucle de fatiga 1/24h).
    with db._connect() as conn:
        conn.execute(
            "UPDATE pipeline_alerts SET resolved = 1, resolved_at = datetime('now') "
            "WHERE entity_type='video' AND entity_id=1 AND alert_type='failed'"
        )
        conn.commit()
    assert _check_video_failed_unalerted(db) == 0


def test_video_failed_interrupted_not_alerted(tmp_path):
    db = _build_db(tmp_path)
    # Job con error_msg vacío + vídeo 'interrupted' (reinicio) → transitorio.
    with db._connect() as conn:
        conn.execute(
            """INSERT INTO videos (id, channel_id, canal, video_path, status, progress_phase,
               created_at)
               VALUES (1, 1, 'canal1', '/tmp/dummy.mp4', 'error', 'interrupted',
                       datetime('now', '-6 days'))"""
        )
        conn.execute(
            """INSERT INTO generation_jobs (channel_id, video_id, action, status, error_msg)
               VALUES (1, 1, 'generate_only', 'failed', NULL)"""
        )
        conn.commit()
    assert _check_video_failed_unalerted(db) == 0


# ═══════════════════════════════════════════════════════════════
# Check 11: task-liveness watchdog
# ═══════════════════════════════════════════════════════════════

def test_tasks_alive_no_heartbeat_no_alert(tmp_path):
    db = _build_db(tmp_path)
    assert _check_tasks_alive(db) == 0


def test_tasks_alive_stale_heartbeat_creates_critical(tmp_path):
    db = _build_db(tmp_path)
    # Heartbeat viejo (hace > timeout) → alerta crítica
    task = "schedule_checker"
    with db._connect() as conn:
        old = "2020-01-01T00:00:00"
        conn.execute(
            "INSERT OR REPLACE INTO system_state(key, value) VALUES (?, ?)",
            (f"task_heartbeat_{task}", old),
        )
        conn.commit()
    assert _check_tasks_alive(db) == 1
    with db._connect() as conn:
        row = conn.execute(
            "SELECT severity, alert_type, title FROM pipeline_alerts WHERE alert_type='task_stalled'"
        ).fetchone()
    assert row is not None
    assert row["severity"] == "critical"
    assert task in row["title"]


def test_tasks_alive_fresh_heartbeat_no_alert(tmp_path):
    db = _build_db(tmp_path)
    touch_task_heartbeat("schedule_checker")  # heartbeat now → fresco
    assert _check_tasks_alive(db) == 0


def test_tasks_alive_resolves_stalled_alert_after_fresh_heartbeat(tmp_path):
    db = _build_db(tmp_path)
    task = "queue_consumer"
    with db._connect() as conn:
        conn.execute(
            "INSERT INTO system_state(key, value) VALUES (?, ?)",
            (f"task_heartbeat_{task}", "2020-01-01T00:00:00"),
        )
        conn.commit()
    assert _check_tasks_alive(db) == 1

    with db._connect() as conn:
        conn.execute(
            "UPDATE system_state SET value = ? WHERE key = ?",
            (datetime.utcnow().isoformat(timespec="seconds"), f"task_heartbeat_{task}"),
        )
        conn.commit()

    assert _check_tasks_alive(db) == 0
    with db._connect() as conn:
        row = conn.execute(
            "SELECT resolved, message FROM pipeline_alerts "
            "WHERE alert_type='task_stalled' AND entity_id=7"
        ).fetchone()
    assert row["resolved"] == 1
    assert "heartbeat recuperado" in row["message"]


def test_stalled_alert_is_not_resolved_without_valid_heartbeat(tmp_path):
    db = _build_db(tmp_path)
    task = "queue_consumer"
    with db._connect() as conn:
        conn.execute(
            "INSERT INTO pipeline_alerts(entity_type, entity_id, alert_type, severity, title) "
            "VALUES ('system', 7, 'task_stalled', 'critical', 'stalled')"
        )
        conn.commit()
    assert _check_tasks_alive(db) == 0
    with db._connect() as conn:
        row = conn.execute(
            "SELECT resolved FROM pipeline_alerts WHERE alert_type='task_stalled'"
        ).fetchone()
    assert row["resolved"] == 0


def test_failed_alert_resolves_when_intentional_reassemble_retry_is_queued(tmp_path):
    db = _build_db(tmp_path)
    _insert_failed_video(
        db, vid=22,
        error_msg="Killed by operator para liberar gate; se reintentará",
    )
    with db._connect() as conn:
        conn.execute(
            "INSERT INTO pipeline_alerts(entity_type, entity_id, channel_id, alert_type, severity, title) "
            "VALUES ('video', 22, 1, 'failed', 'critical', 'failed')"
        )
        conn.execute(
            "INSERT INTO generation_jobs(channel_id, video_id, action, status) "
            "VALUES (1, 22, 'reassemble', 'queued')"
        )
        conn.commit()

    assert _auto_resolve_completed(db) == 1
    with db._connect() as conn:
        row = conn.execute(
            "SELECT resolved, message FROM pipeline_alerts "
            "WHERE entity_type='video' AND entity_id=22 AND alert_type='failed'"
        ).fetchone()
    assert row["resolved"] == 1
    assert "reintento" in row["message"]


def test_spam_false_positive_verification_resolves_active_alert(tmp_path):
    db = _build_db(tmp_path)
    with db._connect() as conn:
        conn.execute(
            "INSERT INTO pipeline_alerts(entity_type, entity_id, channel_id, alert_type, severity, title) "
            "VALUES ('channel', 1, 1, 'spam_strike', 'critical', 'strike')"
        )
        conn.execute(
            "INSERT INTO system_state(key, value) VALUES (?, ?)",
            ("spam_block_verification_1", json.dumps({"status": "cleared_false_positive"})),
        )
        conn.commit()

    assert check_all_health(db)["alerts_resolved"] == 1
    with db._connect() as conn:
        row = conn.execute(
            "SELECT resolved, message FROM pipeline_alerts WHERE alert_type='spam_strike'"
        ).fetchone()
    assert row["resolved"] == 1
    assert "falso positivo" in row["message"]


# ═══════════════════════════════════════════════════════════════
# Check 12: awaiting_upload_stuck
# ═══════════════════════════════════════════════════════════════

def test_awaiting_upload_stuck_creates_warning(tmp_path):
    db = _build_db(tmp_path)
    _insert_video(db, vid=1, status='awaiting_upload', finished='2026-01-01 00:00:00')
    created = _check_awaiting_upload_stuck(db)
    assert created == 1
    with db._connect() as conn:
        row = conn.execute(
            "SELECT alert_type, severity FROM pipeline_alerts WHERE alert_type='awaiting_upload_stuck'"
        ).fetchone()
    assert row is not None and row["severity"] == "warning"


def test_awaiting_upload_with_future_schedule_not_alerted(tmp_path):
    db = _build_db(tmp_path)
    _insert_video(db, vid=1, status='awaiting_upload', finished='2026-01-01 00:00:00',
                  scheduled='2099-01-01 00:00:00')
    assert _check_awaiting_upload_stuck(db) == 0


def test_awaiting_upload_spam_blocked_channel_not_alerted(tmp_path):
    db = _build_db(tmp_path)
    _insert_video(db, vid=1, status='awaiting_upload', finished='2026-01-01 00:00:00')
    db.is_channel_spam_blocked = lambda channel_id: True

    assert _check_awaiting_upload_stuck(db) == 0


def test_awaiting_upload_stuck_alert_auto_resolves_after_video_leaves_active_states(tmp_path):
    db = _build_db(tmp_path)
    _insert_video(db, vid=1, status='awaiting_upload', finished='2026-01-01 00:00:00')
    assert _check_awaiting_upload_stuck(db) == 1

    with db._connect() as conn:
        conn.execute("UPDATE videos SET status = 'ready' WHERE id = 1")
        conn.commit()

    assert _auto_resolve_completed(db) == 1
    with db._connect() as conn:
        resolved = conn.execute(
            "SELECT resolved FROM pipeline_alerts "
            "WHERE alert_type = 'awaiting_upload_stuck' AND entity_id = 1"
        ).fetchone()
    assert resolved["resolved"] == 1


def test_quota_recovery_timeout_covers_sleep_interval():
    assert TASK_TIMEOUTS["quota_recovery"] == 2400


# ═══════════════════════════════════════════════════════════════
# Check 13: upload_retry_loop
# ═══════════════════════════════════════════════════════════════

def _insert_failed_upload_job(db, video_id, n):
    with db._connect() as conn:
        for i in range(n):
            conn.execute(
                """INSERT INTO generation_jobs (channel_id, video_id, action, status, created_at)
                   VALUES (1, ?, 'upload_only', 'failed', datetime('now', '-1 hour'))""",
                (video_id,),
            )
        conn.commit()


def test_upload_retry_loop_over_threshold(tmp_path):
    db = _build_db(tmp_path)
    _insert_video(db, vid=1, status='awaiting_upload', finished='2026-01-01 00:00:00')
    _insert_failed_upload_job(db, 1, 4)  # umbral = 4
    assert _check_upload_retry_loop(db) == 1


def test_upload_retry_loop_under_threshold(tmp_path):
    db = _build_db(tmp_path)
    _insert_video(db, vid=1, status='awaiting_upload', finished='2026-01-01 00:00:00')
    _insert_failed_upload_job(db, 1, 2)
    assert _check_upload_retry_loop(db) == 0


# ═══════════════════════════════════════════════════════════════
# Check 14: stats_collection_failed
# ═══════════════════════════════════════════════════════════════

def test_stats_collection_error_creates_alert(tmp_path):
    db = _build_db(tmp_path)
    with db._connect() as conn:
        conn.execute(
            "INSERT INTO system_state(key, value) VALUES ('stats_collection_state', ?)",
            (json.dumps({"status": "error", "error": "token expired"}),),
        )
        conn.commit()
    assert _check_stats_collection_failed(db) == 1


def test_stats_collection_success_auto_resolves(tmp_path):
    db = _build_db(tmp_path)
    # Crear primero un estado error + alerta activa
    with db._connect() as conn:
        conn.execute(
            "INSERT INTO system_state(key, value) VALUES ('stats_collection_state', ?)",
            (json.dumps({"status": "error", "error": "boom"}),),
        )
        conn.execute(
            """INSERT INTO pipeline_alerts (entity_type, entity_id, alert_type, severity, title)
               VALUES ('system', 0, 'stats_collection_failed', 'warning', 'Recolección de stats falló')"""
        )
        conn.commit()
    # Ahora la recolección funciona → auto-resuelve
    with db._connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO system_state(key, value) VALUES ('stats_collection_state', ?)",
            (json.dumps({"status": "success"}),),
        )
        conn.commit()
    assert _check_stats_collection_failed(db) == 0
    with db._connect() as conn:
        row = conn.execute(
            "SELECT resolved FROM pipeline_alerts WHERE alert_type='stats_collection_failed'"
        ).fetchone()
    assert row["resolved"] == 1


# ═══════════════════════════════════════════════════════════════
# Check 15: short_ready_stuck
# ═══════════════════════════════════════════════════════════════

def test_short_ready_stuck_creates_alert(tmp_path):
    db = _build_db(tmp_path)
    with db._connect() as conn:
        conn.execute(
            """INSERT INTO shorts (id, channel_id, title, status, scheduled_date, created_at)
               VALUES (1, 1, 'Short A', 'ready', NULL, datetime('now', '-2 days'))"""
        )
        conn.commit()
    assert _check_short_ready_stuck(db) == 1


def test_short_ready_with_file_not_alerted(tmp_path):
    # Cola unificada (ago 2026): 'ready' con archivo válido está EN COLA,
    # esperando la válvula de goteo (topes 1/día + cuota) — NO es huérfano.
    db = _build_db(tmp_path)
    with db._connect() as conn:
        conn.execute(
            """INSERT INTO shorts (id, channel_id, title, status, scheduled_date, created_at, file_path)
               VALUES (1, 1, 'Short A', 'ready', NULL, datetime('now', '-2 days'), '/tmp/short_a.mp4')"""
        )
        conn.commit()
    assert _check_short_ready_stuck(db) == 0


def test_short_ready_future_date_not_alerted(tmp_path):
    db = _build_db(tmp_path)
    with db._connect() as conn:
        conn.execute(
            """INSERT INTO shorts (id, channel_id, title, status, scheduled_date, created_at)
               VALUES (1, 1, 'Short A', 'ready', '2099-01-01', datetime('now', '-2 days'))"""
        )
        conn.commit()
    assert _check_short_ready_stuck(db) == 0


# ═══════════════════════════════════════════════════════════════
# Check 16: content_safety_starvation
# ═══════════════════════════════════════════════════════════════

def test_content_safety_starvation_creates_alert(tmp_path):
    db = _build_db(tmp_path)
    for i in range(1, 4):  # 3 fallos en 24h
        _insert_video(db, vid=i, status='error', canal='canal1',
                      finished='2026-01-01 00:00:00',
                      err='No se pudo generar el guion (sin contenido disponible)')
        with db._connect() as conn:
            conn.execute("UPDATE videos SET progress_phase='script', created_at=datetime('now', '-1 hour') WHERE id=?", (i,))
            conn.commit()
    assert _check_content_safety_starvation(db) == 1


# ═══════════════════════════════════════════════════════════════
# Check 17: platform_publish_failed (no-auth)
# ═══════════════════════════════════════════════════════════════

def test_platform_publish_failed_non_auth(tmp_path):
    db = _build_db(tmp_path)
    with db._connect() as conn:
        conn.execute(
            """INSERT INTO platform_videos (video_id, channel_id, platform, status, error_message, created_at)
               VALUES (1, 1, 'facebook', 'failed', 'rate limit exceeded', datetime('now', '-1 hour'))"""
        )
        conn.commit()
    assert _check_platform_publish_failed(db) == 1
    with db._connect() as conn:
        row = conn.execute(
            "SELECT alert_type FROM pipeline_alerts WHERE alert_type LIKE 'platform_publish_failed_%'"
        ).fetchone()
    assert row is not None and "facebook" in row["alert_type"]


def test_platform_publish_failed_auth_pattern_skipped(tmp_path):
    db = _build_db(tmp_path)
    with db._connect() as conn:
        conn.execute(
            """INSERT INTO platform_videos (video_id, channel_id, platform, status, error_message, created_at)
               VALUES (1, 1, 'rumble', 'failed', 'token expired', datetime('now', '-1 hour'))"""
        )
        conn.commit()
    assert _check_platform_publish_failed(db) == 0  # lo cubre platform_token_expired_*


# ═══════════════════════════════════════════════════════════════
# emit_alert: journal fallback cuando la DB no está disponible
# ═══════════════════════════════════════════════════════════════

def test_emit_alert_journal_when_db_down(monkeypatch, tmp_path):
    from api.services import lifecycle_monitor as lm

    class BrokenDB:
        def _connect(self):
            raise RuntimeError("DB down")

    # Forzar journal en un fichero temporal del test
    journal = tmp_path / "alerts_fallback.log"
    monkeypatch.setattr(lm, "_ALERTS_JOURNAL_PATH", journal)

    result = emit_alert(BrokenDB(), entity_type='system', entity_id=1,
                        alert_type='test_db_down', severity='warning',
                        title='Test', message='fallback journal')
    assert result is None
    assert journal.exists()
    entry = json.loads(journal.read_text().strip())
    assert entry["alert_type"] == "test_db_down"
    assert entry["title"] == "Test"


def test_emit_alert_creates_in_db(tmp_path):
    db = _build_db(tmp_path)
    alert_id = emit_alert(db, entity_type='video', entity_id=42, channel_id=1,
                          alert_type='test_emit', severity='warning',
                          title='Test emit', message='ok')
    assert alert_id is not None
    # Dedup: segunda llamada no duplica
    assert emit_alert(db, entity_type='video', entity_id=42, channel_id=1,
                      alert_type='test_emit', severity='warning',
                      title='Test emit', message='ok') is None
    with db._connect() as conn:
        count = conn.execute("SELECT COUNT(*) as c FROM pipeline_alerts WHERE alert_type='test_emit'").fetchone()["c"]
    assert count == 1
