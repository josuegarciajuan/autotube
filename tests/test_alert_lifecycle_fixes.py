"""Tests de los fixes del ciclo de vida de alertas (sep 2026).

Cubre:
- ``_alert_mark_failed``: no alerta para razones no marcables
  (radio_disabled/radio_not_found) y adjunta el ítem a video/short (antes un
  short caía a una alerta de canal irresoluble).
- ``resolve_scheduled_reminder``: cierra también la alerta de sistema asociada.
- ``_resolve_skip_alerts``: cierra ``deploy_skipped`` tras un deploy correcto.
"""

import sqlite3

from pipeline.youtube_browser import YouTubeBrowser, UNMARKABLE_MARK_REASONS


class _FakeDB:
    def __init__(self):
        self.state = {}
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(
            """
            CREATE TABLE videos (id INTEGER PRIMARY KEY, yt_video_id TEXT, channel_id INTEGER);
            CREATE TABLE shorts (id INTEGER PRIMARY KEY, youtube_id TEXT, channel_id INTEGER);
            """
        )

    def get_system_state(self, key):
        return self.state.get(key)

    def set_system_state(self, key, value):
        self.state[key] = value

    def _connect(self):
        return self.conn


def _capture(monkeypatch, fake_db):
    alerts = []
    monkeypatch.setattr("database.db_extended.ExtendedDatabase", lambda *a, **k: fake_db)
    monkeypatch.setattr(
        "api.services.lifecycle_monitor.create_alert",
        lambda _db, **kw: alerts.append(kw),
    )
    return alerts


def test_unmarkable_reason_does_not_alert(monkeypatch):
    fake = _FakeDB()
    alerts = _capture(monkeypatch, fake)
    YouTubeBrowser._alert_mark_failed(object.__new__(YouTubeBrowser), "YT1", "radio_disabled")
    assert alerts == []
    assert "radio_disabled" in UNMARKABLE_MARK_REASONS


def test_alert_attaches_to_short_entity(monkeypatch):
    fake = _FakeDB()
    fake.conn.execute("INSERT INTO shorts (id, youtube_id, channel_id) VALUES (767, 'YT5', 7)")
    fake.conn.commit()
    alerts = _capture(monkeypatch, fake)

    YouTubeBrowser._alert_mark_failed(object.__new__(YouTubeBrowser), "YT5", "exception")

    assert alerts[0]["entity_type"] == "short"
    assert alerts[0]["entity_id"] == 767
    assert alerts[0]["channel_id"] == 7


def test_alert_attaches_to_video_entity(monkeypatch):
    fake = _FakeDB()
    fake.conn.execute("INSERT INTO videos (id, yt_video_id, channel_id) VALUES (42, 'YT9', 3)")
    fake.conn.commit()
    alerts = _capture(monkeypatch, fake)

    YouTubeBrowser._alert_mark_failed(object.__new__(YouTubeBrowser), "YT9", "save_disabled")

    assert alerts[0]["entity_type"] == "video"
    assert alerts[0]["entity_id"] == 42
    assert alerts[0]["channel_id"] == 3


class _ConnDB:
    def __init__(self, conn):
        self._conn = conn

    def _connect(self):
        return self._conn


def test_resolve_scheduled_reminder_closes_alert():
    from database.db_extended import ExtendedDatabase

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE scheduled_reminders (
            id INTEGER PRIMARY KEY, status TEXT, alert_id INTEGER, resolved_at TEXT);
        CREATE TABLE pipeline_alerts (
            id INTEGER PRIMARY KEY, resolved INTEGER DEFAULT 0,
            acknowledged INTEGER DEFAULT 0, resolved_at TEXT, message TEXT);
        """
    )
    conn.execute("INSERT INTO scheduled_reminders (id, status, alert_id) VALUES (3533, 'alerted', 2767)")
    conn.execute("INSERT INTO pipeline_alerts (id, resolved, message) VALUES (2767, 0, 'x')")
    conn.commit()

    assert ExtendedDatabase.resolve_scheduled_reminder(_ConnDB(conn), 3533) is True
    assert conn.execute("SELECT status FROM scheduled_reminders WHERE id=3533").fetchone()[0] == "resolved"
    assert conn.execute("SELECT resolved FROM pipeline_alerts WHERE id=2767").fetchone()[0] == 1


def test_resolve_skip_alerts_closes_deploy_skipped(monkeypatch):
    from scripts.deploy_safety import _resolve_skip_alerts

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE pipeline_alerts (
            id INTEGER PRIMARY KEY, alert_type TEXT, resolved INTEGER DEFAULT 0,
            acknowledged INTEGER DEFAULT 0, resolved_at TEXT, message TEXT);
        """
    )
    conn.execute("INSERT INTO pipeline_alerts (id, alert_type, resolved) VALUES (1, 'deploy_skipped', 0)")
    conn.execute("INSERT INTO pipeline_alerts (id, alert_type, resolved) VALUES (2, 'other', 0)")
    conn.commit()

    monkeypatch.setattr("database.db_extended.ExtendedDatabase", lambda *a, **k: _ConnDB(conn))
    _resolve_skip_alerts()

    assert conn.execute("SELECT resolved FROM pipeline_alerts WHERE id=1").fetchone()[0] == 1
    assert conn.execute("SELECT resolved FROM pipeline_alerts WHERE id=2").fetchone()[0] == 0
