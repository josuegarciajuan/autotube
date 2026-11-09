from __future__ import annotations
import os, sqlite3, sys
from types import SimpleNamespace
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
import api.services.quality_monitors as qm


class FakeDB:
    def __init__(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript("""
            CREATE TABLE channels (id INTEGER PRIMARY KEY, name TEXT, slug TEXT, active INTEGER);
            CREATE TABLE videos (id INTEGER PRIMARY KEY, channel_id INTEGER, yt_video_id TEXT,
                                 duracion_seg INTEGER, uploaded_at TEXT);
            CREATE TABLE pipeline_log (id INTEGER PRIMARY KEY, canal TEXT, phase TEXT,
                                       status TEXT, message TEXT, created_at TEXT);
            CREATE TABLE pipeline_alerts (id INTEGER PRIMARY KEY, entity_type TEXT, entity_id INTEGER,
                channel_id INTEGER, alert_type TEXT, severity TEXT, title TEXT, message TEXT,
                metadata_json TEXT, resolved INTEGER DEFAULT 0, resolved_at TEXT);
            INSERT INTO channels VALUES (3, 'Sincronias', 'canal2', 1);
        """)
        self.conn.commit()

    def get_channels(self, active_only=True):
        return [dict(r) for r in self.conn.execute("SELECT * FROM channels WHERE active=1")]

    def _connect(self):
        return self.conn

    def create_alert(self, **kw):
        cur = self.conn.execute(
            """INSERT INTO pipeline_alerts
               (entity_type, entity_id, channel_id, alert_type, severity, title, message, metadata_json)
               VALUES (?,?,?,?,?,?,?,?)""",
            (kw.get("entity_type", "system"), kw.get("entity_id"), kw.get("channel_id"),
             kw["alert_type"], kw.get("severity", "warning"), kw["title"],
             kw.get("message", ""), "{}"),
        )
        self.conn.commit()
        return cur.lastrowid

    def resolve_alerts(self, *, alert_type, channel_id=None):
        self.conn.execute(
            "UPDATE pipeline_alerts SET resolved=1 WHERE alert_type=? AND channel_id=?",
            (alert_type, channel_id),
        )
        self.conn.commit()
        return 1


def test_duration_drift_alert_and_resolution(monkeypatch):
    db = FakeDB()
    for i in range(6):
        dur = 25 * 60 if i < 4 else 15 * 60  # 4/6 outside 17±2 (15-19)
        db.conn.execute(
            "INSERT INTO videos (channel_id, yt_video_id, duracion_seg, uploaded_at) "
            "VALUES (3, ?, ?, datetime('now'))",
            (f"v{i}", dur),
        )
    db.conn.commit()
    monkeypatch.setattr(
        "config.config_bridge.get_channel_config",
        lambda slug, force_reload=False: SimpleNamespace(
            VIDEO_AVERAGE_DURATION_MIN=17, VIDEO_DURATION_DISCREPANCY_MIN=2
        ),
    )
    out = qm.check_duration_drift(db)
    assert out[0]["status"] == "alert"
    assert out[0]["outside"] == 4

    db.conn.execute("UPDATE videos SET duracion_seg=17*60")
    db.conn.commit()
    out = qm.check_duration_drift(db)
    assert out[0]["status"] == "ok"


def test_video_phase_regression_alert(monkeypatch):
    db = FakeDB()
    db.conn.execute(
        "INSERT INTO pipeline_log (canal, phase, status, message, created_at) "
        "VALUES ('canal2', 'video', 'error', 'boom', datetime('now'))"
    )
    db.conn.commit()
    out = qm.check_video_phase_regression(db)
    assert out[0]["status"] == "alert"
    assert out[0]["errors"] == 1

    db.conn.execute("DELETE FROM pipeline_log")
    db.conn.commit()
    out = qm.check_video_phase_regression(db)
    assert out[0]["status"] == "ok"
