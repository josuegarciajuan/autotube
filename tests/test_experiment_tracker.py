"""Tests de ``scripts/experiment_tracker.py`` (instrumentación del experimento).

Cubren: cálculo de baseline, persistencia en system_state y programación
idempotente/reprogramable de los checkpoints T+7/T+21/T+45.
"""

from __future__ import annotations

import contextlib
import json
import sqlite3
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from scripts.experiment_tracker import (  # noqa: E402
    CHECKPOINT_DAYS,
    CHECKPOINTS_KEY,
    STARTED_KEY,
    _checkpoint_due_at,
    compute_baseline,
    schedule_checkpoints,
    store_baseline,
)

SCHEMA = """
CREATE TABLE channels (
    id INTEGER PRIMARY KEY, slug TEXT, name TEXT, active INTEGER DEFAULT 1
);
CREATE TABLE channel_stats_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT, channel_id INTEGER,
    subscribers INTEGER, total_views INTEGER, video_count INTEGER,
    estimated_minutes_watched REAL, fetched_at TEXT
);
CREATE TABLE videos (
    id INTEGER PRIMARY KEY, channel_id INTEGER, yt_video_id TEXT,
    titulo_final TEXT, actual_published_at TEXT, published_at TEXT,
    uploaded_at TEXT, status TEXT, manual_altered_content_done INTEGER DEFAULT 0
);
CREATE TABLE video_stats_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT, video_id INTEGER, views INTEGER
);
CREATE TABLE video_analytics_detailed (
    id INTEGER PRIMARY KEY AUTOINCREMENT, video_id INTEGER,
    report_type TEXT, metric_value REAL
);
CREATE TABLE shorts (
    id INTEGER PRIMARY KEY AUTOINCREMENT, channel_id INTEGER,
    hook_title TEXT, title TEXT, youtube_id TEXT, status TEXT,
    published_at TEXT, actual_published_at TEXT,
    manual_altered_content_done INTEGER DEFAULT 0
);
CREATE TABLE short_stats (
    id INTEGER PRIMARY KEY AUTOINCREMENT, short_id INTEGER,
    views INTEGER, fetched_at TEXT
);
CREATE TABLE system_state (
    key TEXT PRIMARY KEY, value TEXT, updated_at TEXT
);
CREATE TABLE scheduled_reminders (
    id INTEGER PRIMARY KEY AUTOINCREMENT, entity_type TEXT, entity_id INTEGER,
    title TEXT, message TEXT, alert_type TEXT, due_at TEXT,
    status TEXT DEFAULT 'pending', metadata_json TEXT, alert_id INTEGER,
    resolved_at TEXT
);
"""


class FakeDB:
    """DB mínima con la superficie que usa experiment_tracker."""

    def __init__(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)

    @contextlib.contextmanager
    def _connect(self):
        yield self.conn

    def get_system_state(self, key):
        row = self.conn.execute(
            "SELECT value FROM system_state WHERE key = ?", (key,)
        ).fetchone()
        return row["value"] if row else None

    def set_system_state(self, key, value):
        self.conn.execute(
            "INSERT INTO system_state (key, value, updated_at) VALUES (?, ?, '') "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        self.conn.commit()

    def create_scheduled_reminder(self, title, message, due_at, entity_id=None,
                                  entity_type="system",
                                  alert_type="scheduled_reminder_due",
                                  metadata=None):
        cur = self.conn.execute(
            "INSERT INTO scheduled_reminders (entity_type, entity_id, title, message,"
            " alert_type, due_at, status, metadata_json) VALUES (?,?,?,?,?,?,'pending',?)",
            (entity_type, entity_id or 0, title, message, alert_type, due_at,
             json.dumps(metadata or {})),
        )
        self.conn.commit()
        return cur.lastrowid

    def list_scheduled_reminders(self, status=None, limit=50):
        if status:
            rows = self.conn.execute(
                "SELECT * FROM scheduled_reminders WHERE status = ? LIMIT ?",
                (status, limit),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM scheduled_reminders LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]


def _seed(db: FakeDB) -> None:
    db.conn.execute("INSERT INTO channels (id, slug, name, active) VALUES (4, 'canal3', 'C3', 1)")
    db.conn.execute("INSERT INTO channels (id, slug, name, active) VALUES (6, 'test', 'T', 1)")
    # older snapshot (>7d) y actual
    db.conn.execute(
        "INSERT INTO channel_stats_history (channel_id, subscribers, total_views,"
        " video_count, estimated_minutes_watched, fetched_at) "
        "VALUES (4, 200, 100000, 300, 30000, '2026-09-01 08:00:00')"
    )
    db.conn.execute(
        "INSERT INTO channel_stats_history (channel_id, subscribers, total_views,"
        " video_count, estimated_minutes_watched, fetched_at) "
        "VALUES (4, 274, 153194, 411, 44561, '2026-09-17 08:00:00')"
    )
    # long-form
    db.conn.execute(
        "INSERT INTO videos (id, channel_id, yt_video_id, titulo_final, status) "
        "VALUES (1, 4, 'v1', 'Un video', 'published')"
    )
    db.conn.execute("INSERT INTO video_stats_history (video_id, views) VALUES (1, 50)")
    db.conn.execute(
        "INSERT INTO video_analytics_detailed (video_id, report_type, metric_value) "
        "VALUES (1, 'retention_pct', 40.0)"
    )
    # shorts con alcance medible a 7 días
    db.conn.execute(
        "INSERT INTO shorts (id, channel_id, hook_title, youtube_id, status,"
        " published_at) VALUES (10, 4, 'Tema A', 's1', 'published', '2026-09-10 10:00:00')"
    )
    db.conn.execute(
        "INSERT INTO shorts (id, channel_id, hook_title, youtube_id, status,"
        " published_at) VALUES (11, 4, 'Tema B', 's2', 'published', '2026-09-10 10:00:00')"
    )
    db.conn.execute(
        "INSERT INTO short_stats (short_id, views, fetched_at) "
        "VALUES (10, 100, '2026-09-12 10:00:00')"
    )
    db.conn.execute(
        "INSERT INTO short_stats (short_id, views, fetched_at) "
        "VALUES (11, 300, '2026-09-12 10:00:00')"
    )
    db.conn.commit()


def test_compute_baseline_values():
    db = FakeDB()
    _seed(db)
    baseline = compute_baseline(db)

    assert [c["slug"] for c in baseline["channels"]] == ["canal3"]  # 'test' excluido
    ch = baseline["channels"][0]
    assert ch["subscribers"] == 274
    assert ch["net_subs_7d"] == 74  # 274 - 200
    assert ch["watch_hours"] == 742.7
    assert ch["longform"]["n"] == 1
    assert ch["longform"]["avg_views"] == 50.0
    assert ch["longform"]["retention_pct"] == 40.0
    assert ch["shorts"]["n"] == 2
    assert ch["shorts"]["avg_views"] == 200.0
    assert ch["shorts"]["reach7d_avg"] == 200.0
    assert ch["repeated_titles"] == 0


def test_store_baseline_persists_state():
    db = FakeDB()
    _seed(db)
    store_baseline(db)
    assert db.get_system_state(STARTED_KEY)
    stored = json.loads(db.get_system_state("experiment_baseline"))
    assert stored["channels"][0]["slug"] == "canal3"
    # No se re-sella el start en una segunda pasada
    started = db.get_system_state(STARTED_KEY)
    store_baseline(db)
    assert db.get_system_state(STARTED_KEY) == started


def test_checkpoint_due_dates():
    assert _checkpoint_due_at("2026-09-17", 7) == "2026-09-24T09:00:00"
    assert _checkpoint_due_at("2026-09-17T15:00:00", 21) == "2026-10-08T09:00:00"


def test_schedule_checkpoints_creates_three():
    db = FakeDB()
    _seed(db)
    scheduled = schedule_checkpoints(db, "2026-09-17")
    assert len(scheduled) == len(CHECKPOINT_DAYS) == 3
    reminders = db.list_scheduled_reminders(status="pending")
    assert len(reminders) == 3
    assert all(r["alert_type"] == "experiment_checkpoint" for r in reminders)
    dates = sorted(r["due_at"] for r in reminders)
    assert dates == ["2026-09-24T09:00:00", "2026-10-08T09:00:00", "2026-11-01T09:00:00"]
    state = json.loads(db.get_system_state(CHECKPOINTS_KEY))
    assert state["start"] == "2026-09-17"


def test_schedule_checkpoints_idempotent_and_reschedules():
    db = FakeDB()
    _seed(db)
    schedule_checkpoints(db, "2026-09-17")
    schedule_checkpoints(db, "2026-09-17")  # no duplica
    assert len(db.list_scheduled_reminders(status="pending")) == 3

    schedule_checkpoints(db, "2026-09-20")  # reprograma sin crear nuevos
    reminders = db.list_scheduled_reminders(status="pending")
    assert len(reminders) == 3
    assert min(r["due_at"] for r in reminders) == "2026-09-27T09:00:00"
