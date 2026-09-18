"""Tests de A4: salud del marcado IA (reconciliación + heartbeat)."""

from __future__ import annotations

import contextlib
import sqlite3
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from api.services import ia_marking_health as h  # noqa: E402


class FakeDB:
    def __init__(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript("""
            CREATE TABLE channels (id INTEGER PRIMARY KEY, slug TEXT);
            CREATE TABLE videos (id INTEGER PRIMARY KEY, channel_id INTEGER,
                yt_video_id TEXT, uploaded_at TEXT, published_at TEXT,
                manual_altered_content_done INTEGER DEFAULT 0);
            CREATE TABLE shorts (id INTEGER PRIMARY KEY, channel_id INTEGER,
                youtube_id TEXT, actual_published_at TEXT, published_at TEXT,
                manual_altered_content_done INTEGER DEFAULT 0);
            CREATE TABLE system_state (key TEXT PRIMARY KEY, value TEXT);
        """)
        self.conn.execute("INSERT INTO channels (id, slug) VALUES (3,'canal2')")
        # recientes: 2 sin marcar, 1 marcado; 1 antiguo sin marcar (fuera de gracia)
        self.conn.execute("INSERT INTO videos (id,channel_id,yt_video_id,uploaded_at,"
                          "manual_altered_content_done) VALUES (1,3,'V_NEW_A',datetime('now','-1 hours'),0)")
        self.conn.execute("INSERT INTO videos (id,channel_id,yt_video_id,uploaded_at,"
                          "manual_altered_content_done) VALUES (2,3,'V_OLD',datetime('now','-10 days'),0)")
        self.conn.execute("INSERT INTO shorts (id,channel_id,youtube_id,actual_published_at,"
                          "manual_altered_content_done) VALUES (10,3,'S_NEW',datetime('now','-1 hours'),0)")
        self.conn.execute("INSERT INTO shorts (id,channel_id,youtube_id,actual_published_at,"
                          "manual_altered_content_done) VALUES (11,3,'S_OK',datetime('now','-1 hours'),1)")
        self.conn.execute("INSERT INTO system_state (key,value) VALUES ('ia_backfill_pending','300')")
        self.conn.commit()

    @contextlib.contextmanager
    def _connect(self):
        yield self.conn

    def get_system_state(self, key):
        row = self.conn.execute("SELECT value FROM system_state WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None


def test_collect_status_counts():
    db = FakeDB()
    status = h.collect_ia_marking_status(db, grace_hours=72)
    # El item 'V_OLD' (2026-09-01) queda fuera de la ventana de 72h.
    assert status["recent_total"] >= 3
    assert "V_NEW_A" in status["unmarked_ids"]
    assert "S_NEW" in status["unmarked_ids"]
    assert "S_OK" not in status["unmarked_ids"]
    assert status["backfill_pending"] == 300


def test_check_emits_critical_when_unmarked(monkeypatch):
    db = FakeDB()
    captured = {}

    import api.services.lifecycle_monitor as lm
    monkeypatch.setattr(lm, "emit_alert",
                        lambda *a, **k: captured.update(k) or 1)
    h.check_ia_marking_health(db, grace_hours=72)
    assert captured.get("alert_type") == "altered_mark_missing"
    assert captured.get("severity") == "critical"


def test_check_emits_info_heartbeat_when_clean(monkeypatch):
    db = FakeDB()
    # Marca todo lo reciente
    db.conn.execute("UPDATE videos SET manual_altered_content_done=1")
    db.conn.execute("UPDATE shorts SET manual_altered_content_done=1")
    db.conn.commit()
    captured = {}
    import api.services.lifecycle_monitor as lm
    monkeypatch.setattr(lm, "emit_alert",
                        lambda *a, **k: captured.update(k) or 1)
    h.check_ia_marking_health(db, grace_hours=72)
    assert captured.get("alert_type") == "ia_mark_health_ok"
    assert captured.get("severity") == "info"
