import json
import sqlite3

from api.routers.cross_platform import _mapping_or_empty
from api.services.lifecycle_monitor import _auto_resolve_recovery_checkpoints
from api.services.publish_coverage import _maybe_alert_dry, _resolve_dry_alert
from pipeline.youtube_browser import YouTubeBrowser


def test_cross_platform_config_treats_null_nested_values_as_empty_mappings():
    assert _mapping_or_empty(None) == {}
    assert _mapping_or_empty([]) == {}
    assert _mapping_or_empty('{"x": 1}') == {"x": 1}


def test_recovery_checkpoint_alert_resolves_only_after_a_later_checkpoint():
    db = _AlertDB()
    db.add_alert("recovery_checkpoint_48h", 7, {"checkpoint_hours": 48})
    assert _auto_resolve_recovery_checkpoints(db) == 0
    db.add_checkpoint(7, 96)
    assert _auto_resolve_recovery_checkpoints(db) == 1
    assert db.conn.execute("SELECT resolved FROM pipeline_alerts WHERE id=1").fetchone()[0] == 1


def test_publish_coverage_dry_alert_is_channel_scoped_and_recovers():
    db = _AlertDB()
    assert _maybe_alert_dry(db, "canal2", channel_id=2) is True
    assert _maybe_alert_dry(db, "canal3", channel_id=3) is True
    rows = db.conn.execute(
        "SELECT entity_id FROM pipeline_alerts ORDER BY entity_id"
    ).fetchall()
    assert {row[0] for row in rows} == {2, 3}
    assert _resolve_dry_alert(db, "canal2", channel_id=2) == 1
    rows = db.conn.execute(
        "SELECT entity_id, resolved FROM pipeline_alerts ORDER BY entity_id"
    ).fetchall()
    assert rows[0][1] == 1
    assert rows[1][1] == 0


def test_altered_content_failure_is_attached_to_video(monkeypatch):
    db = _AlertDB()
    db.video_lookup = {"id": 42, "channel_id": 3}
    monkeypatch.setattr("database.db_extended.ExtendedDatabase", lambda: db)
    alerts = []
    monkeypatch.setattr(
        "api.services.lifecycle_monitor.create_alert",
        lambda _db, **kwargs: alerts.append(kwargs),
    )

    YouTubeBrowser._alert_mark_failed(object.__new__(YouTubeBrowser), "yt-42")

    assert alerts[0]["entity_type"] == "video"
    assert alerts[0]["entity_id"] == 42
    assert alerts[0]["channel_id"] == 3


class _AlertDB:
    def __init__(self):
        self.state = {}
        self.alerts = []
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript("""
            CREATE TABLE pipeline_alerts (
              id INTEGER PRIMARY KEY, entity_type TEXT, entity_id INTEGER,
              channel_id INTEGER, alert_type TEXT, severity TEXT,
              title TEXT, message TEXT, metadata_json TEXT,
              resolved INTEGER DEFAULT 0, acknowledged INTEGER DEFAULT 0,
              resolved_at TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
            CREATE TABLE recovery_checkpoints (video_id INTEGER, checkpoint_hours INTEGER);
            CREATE TABLE videos (id INTEGER, yt_video_id TEXT, channel_id INTEGER);
        """)

    def get_system_state(self, key):
        return self.state.get(key)

    def set_system_state(self, key, value):
        self.state[key] = value

    def _connect(self):
        if hasattr(self, "video_lookup"):
            self.conn.execute(
                "DELETE FROM videos"
            )
            self.conn.execute(
                "INSERT INTO videos VALUES (?, 'yt-42', ?)",
                (self.video_lookup["id"], self.video_lookup["channel_id"]),
            )
            self.conn.commit()
        return self.conn

    def add_alert(self, alert_type, entity_id, metadata):
        self.conn.execute(
            "INSERT INTO pipeline_alerts(id, entity_type, entity_id, alert_type, metadata_json) VALUES (?, 'video', ?, ?, ?)",
            (len(self.alerts) + 1, entity_id, alert_type, json.dumps(metadata)),
        )
        self.conn.commit()
        self.alerts.append({"alert_type": alert_type, "entity_id": entity_id,
                            "metadata": json.dumps(metadata), "resolved": False})

    def add_checkpoint(self, video_id, hours):
        self.conn.execute("INSERT INTO recovery_checkpoints VALUES (?, ?)", (video_id, hours))
        self.conn.commit()
