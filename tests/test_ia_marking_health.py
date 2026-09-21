"""Tests de A4: salud del marcado IA (reconciliación + heartbeat + estancamiento)."""

from __future__ import annotations

import contextlib
import sqlite3
import sys
from datetime import datetime
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
                status TEXT DEFAULT '', yt_visibility TEXT DEFAULT '',
                manual_altered_content_done INTEGER DEFAULT 0,
                manual_altered_content_skip INTEGER DEFAULT 0);
            CREATE TABLE shorts (id INTEGER PRIMARY KEY, channel_id INTEGER,
                youtube_id TEXT, actual_published_at TEXT, published_at TEXT,
                yt_visibility TEXT DEFAULT '',
                manual_altered_content_done INTEGER DEFAULT 0,
                manual_altered_content_skip INTEGER DEFAULT 0);
            CREATE TABLE system_state (key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE pipeline_alerts (id INTEGER PRIMARY KEY, entity_type TEXT,
                entity_id INTEGER, channel_id INTEGER, alert_type TEXT,
                severity TEXT, title TEXT, message TEXT, metadata_json TEXT,
                acknowledged INTEGER DEFAULT 0, resolved INTEGER DEFAULT 0,
                resolved_at TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
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
        self.conn.commit()

    @contextlib.contextmanager
    def _connect(self):
        yield self.conn

    def get_system_state(self, key):
        row = self.conn.execute("SELECT value FROM system_state WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None


def _capture(monkeypatch):
    """Captura todas las alertas emitidas (puede haber más de una)."""
    calls: list[dict] = []
    import api.services.lifecycle_monitor as lm
    monkeypatch.setattr(lm, "emit_alert", lambda *a, **k: calls.append(k) or 1)
    return calls


def test_collect_status_counts():
    db = FakeDB()
    status = h.collect_ia_marking_status(db, grace_hours=72)
    assert status["recent_total"] >= 3
    assert "V_NEW_A" in status["unmarked_ids"]
    assert "S_NEW" in status["unmarked_ids"]
    assert "S_OK" not in status["unmarked_ids"]
    # pendientes en vivo: V_NEW_A + V_OLD + S_NEW
    assert status["backfill_pending"] == 3


def test_skipped_items_excluded():
    db = FakeDB()
    db.conn.execute("UPDATE videos SET manual_altered_content_skip=1 WHERE yt_video_id='V_NEW_A'")
    db.conn.execute("UPDATE shorts SET manual_altered_content_skip=1 WHERE youtube_id='S_NEW'")
    db.conn.commit()
    status = h.collect_ia_marking_status(db, grace_hours=72)
    assert "V_NEW_A" not in status["unmarked_ids"]
    assert "S_NEW" not in status["unmarked_ids"]
    # solo queda V_OLD como pendiente (S_OK marcado, V_NEW_A/S_NEW skipeados)
    assert status["backfill_pending"] == 1


def test_check_emits_critical_when_unmarked(monkeypatch):
    db = FakeDB()
    calls = _capture(monkeypatch)
    h.check_ia_marking_health(db, grace_hours=72)
    types = [c.get("alert_type") for c in calls]
    assert "altered_mark_missing" in types


def test_check_emits_info_heartbeat_when_clean(monkeypatch):
    db = FakeDB()
    # Marca todo lo reciente
    db.conn.execute("UPDATE videos SET manual_altered_content_done=1")
    db.conn.execute("UPDATE shorts SET manual_altered_content_done=1")
    # Progreso reciente del backfill para no disparar la alerta de estancamiento
    db.conn.execute("INSERT INTO system_state (key,value) VALUES ('ia_backfill_last_progress',?)",
                    (datetime.now().isoformat(timespec="seconds"),))
    # Alertas críticas previas que ya no aplican (incl. falso positivo de estancado)
    db.conn.execute(
        "INSERT INTO pipeline_alerts(id, entity_type, entity_id, alert_type, severity, title, resolved) "
        "VALUES (1, 'system', 0, 'altered_mark_missing', 'critical', 'sin marcar', 0)"
    )
    db.conn.execute(
        "INSERT INTO pipeline_alerts(id, entity_type, entity_id, alert_type, severity, title, resolved) "
        "VALUES (2, 'system', 0, 'ia_backfill_stalled', 'critical', 'estancado', 0)"
    )
    db.conn.commit()
    calls = _capture(monkeypatch)
    h.check_ia_marking_health(db, grace_hours=72)
    types = [c.get("alert_type") for c in calls]
    assert "ia_mark_health_ok" in types
    # Las críticas anteriores deben quedar cerradas
    rows = {
        r["alert_type"]: r["resolved"]
        for r in db.conn.execute(
            "SELECT alert_type, resolved FROM pipeline_alerts"
        ).fetchall()
    }
    assert rows["altered_mark_missing"] == 1
    assert rows["ia_backfill_stalled"] == 1


def test_check_emits_stalled_when_no_progress(monkeypatch):
    db = FakeDB()
    # Hay pendientes (V_OLD) y ningún progreso registrado → estancado.
    calls = _capture(monkeypatch)
    h.check_ia_marking_health(db, grace_hours=72)
    types = [c.get("alert_type") for c in calls]
    assert "ia_backfill_stalled" in types


def test_backfill_stalled_check():
    assert h.backfill_stalled_check(None) is True
    assert h.backfill_stalled_check("2020-01-01T00:00:00") is True
    assert h.backfill_stalled_check(datetime.now().isoformat(timespec="seconds")) is False
    # El estado real se guarda con tz (Europe/Madrid): no debe dar estancado.
    from datetime import timezone, timedelta as _td
    aware_now = datetime.now(timezone(_td(hours=2))).isoformat(timespec="seconds")
    assert h.backfill_stalled_check(aware_now) is False
    assert h.backfill_stalled_check("2020-01-01T00:00:00+02:00") is True
