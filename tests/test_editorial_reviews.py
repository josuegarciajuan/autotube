import json
import sqlite3
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from api.services import editorial_reviews as reviews


class SqliteDB:
    def __init__(self, path):
        self.path = str(path)

    def _connect(self):
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn


def make_db(tmp_path):
    db = SqliteDB(tmp_path / "reviews.db")
    with db._connect() as conn:
        conn.executescript("""
        CREATE TABLE channels (id INTEGER PRIMARY KEY, slug TEXT, name TEXT);
        CREATE TABLE videos (
            id INTEGER PRIMARY KEY, channel_id INTEGER, canal TEXT,
            titulo_final TEXT, yt_video_id TEXT, yt_url TEXT, status TEXT,
            thumbnail_path TEXT, published_at TEXT, uploaded_at TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE video_stats_history (
            id INTEGER PRIMARY KEY, video_id INTEGER, fetched_at TEXT,
            impressions INTEGER DEFAULT 0, ctr REAL DEFAULT 0,
            average_view_percentage REAL DEFAULT 0, views INTEGER DEFAULT 0
        );
        CREATE TABLE pipeline_alerts (
            id INTEGER PRIMARY KEY AUTOINCREMENT, entity_type TEXT,
            entity_id INTEGER, channel_id INTEGER, alert_type TEXT,
            severity TEXT, title TEXT, message TEXT, metadata_json TEXT,
            acknowledged INTEGER DEFAULT 0, resolved INTEGER DEFAULT 0,
            resolved_at TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE UNIQUE INDEX idx_alerts_unique_active
          ON pipeline_alerts(entity_type, entity_id, alert_type)
          WHERE resolved = 0;
        INSERT INTO channels VALUES (4, 'canal4', 'ESR');
        INSERT INTO videos VALUES
          (10, 4, 'canal4', 'Expedición al hielo', 'yt-10',
           'https://youtu.be/yt-10', 'published', '/tmp/thumb.jpg',
           datetime('now', '-3 days'), datetime('now', '-3 days'), datetime('now'));
        """)
    reviews.ensure_review_schema(db)
    return db


def test_schedule_is_idempotent_and_only_creates_requested_checkpoints(tmp_path):
    db = make_db(tmp_path)
    first = reviews.schedule_video_reviews(db, 10, now=datetime.now(timezone.utc))
    second = reviews.schedule_video_reviews(db, 10, now=datetime.now(timezone.utc))
    assert first == 3
    assert second == 0
    with db._connect() as conn:
        rows = conn.execute(
            "SELECT review_kind, status FROM editorial_reviews WHERE video_id=10"
        ).fetchall()
    assert {row[0] for row in rows} == {"48h", "7d", "14d"}
    assert all(row[1] == "scheduled" for row in rows)


def test_success_emits_alert_and_does_not_mutate_video(tmp_path, monkeypatch):
    db = make_db(tmp_path)
    reviews.schedule_video_reviews(db, 10)
    monkeypatch.setattr(reviews, "fetch_external_video_state", lambda video: {
        "visibility": "public", "title": video["titulo_final"], "available": True
    })
    result = reviews.process_due_reviews(db, now=datetime.now(timezone.utc) + timedelta(days=8))
    assert result["succeeded"] == 2
    with db._connect() as conn:
        video = conn.execute("SELECT status, yt_video_id FROM videos WHERE id=10").fetchone()
        alerts = conn.execute(
            "SELECT alert_type, severity FROM pipeline_alerts ORDER BY id"
        ).fetchall()
    assert tuple(video) == ("published", "yt-10")
    assert any(row[0] == "editorial_review_success" and row[1] == "info" for row in alerts)


def test_failure_is_visible_and_retries(tmp_path, monkeypatch):
    db = make_db(tmp_path)
    reviews.schedule_video_reviews(db, 10)
    monkeypatch.setattr(reviews, "fetch_external_video_state", lambda video: (_ for _ in ()).throw(RuntimeError("network")))
    result = reviews.process_due_reviews(db, now=datetime.now(timezone.utc) + timedelta(days=3))
    assert result["failed"] == 1
    with db._connect() as conn:
        row = conn.execute(
            "SELECT status, attempts, last_error FROM editorial_reviews WHERE video_id=10 AND review_kind='48h'"
        ).fetchone()
        alert = conn.execute(
            "SELECT alert_type, message FROM pipeline_alerts WHERE alert_type='editorial_review_failed'"
        ).fetchone()
    assert tuple(row) == ("failed", 1, "network")
    assert alert and "network" in alert[1]
    second = reviews.process_due_reviews(db, now=datetime.now(timezone.utc) + timedelta(days=3, hours=2))
    assert second["failed"] == 1
    with db._connect() as conn:
        attempts = conn.execute(
            "SELECT attempts FROM editorial_reviews WHERE video_id=10 AND review_kind='48h'"
        ).fetchone()[0]
    assert attempts == 2


def test_performance_classification_distinguishes_insufficient_impressions_ctr_and_retention():
    cfg = SimpleNamespace(EDITORIAL_RECOVERY_REVIEW={
        "min_impressions": 100, "min_ctr_percent": 4, "min_retention_percent": 35
    })
    assert reviews.classify_performance({}, cfg)["classification"] == "insufficient_data_manual_collection"
    assert reviews.classify_performance({"impressions": 0, "ctr": 8}, cfg)["classification"] == "no_impressions"
    assert reviews.classify_performance({"impressions": 100, "ctr": 2, "average_view_percentage": 50}, cfg)["classification"] == "low_ctr"
    assert reviews.classify_performance({"impressions": 100, "ctr": 8, "average_view_percentage": 20}, cfg)["classification"] == "low_retention"


def test_channel_rules_are_read_through_config_bridge(monkeypatch):
    cfg = SimpleNamespace(EDITORIAL_RECOVERY_REVIEW={"enabled": False})
    monkeypatch.setattr(reviews, "get_channel_config", lambda slug: cfg)
    assert reviews.review_config("canal4")["enabled"] is False


def test_new_video_validation_blocks_only_when_channel_rules_match(tmp_path, monkeypatch):
    thumbnail = tmp_path / "thumb.jpg"
    thumbnail.write_bytes(b"jpg")
    cfg = SimpleNamespace(EDITORIAL_RECOVERY_REVIEW={
        "enabled": True, "require_thumbnail": True, "blocked_terms": ["prohibido"]
    })
    monkeypatch.setattr(reviews, "get_channel_config", lambda slug: cfg)
    assert reviews.validate_new_video("canal4", "tema prohibido", "Título", str(thumbnail))["allowed"] is False
    assert reviews.validate_new_video("canal4", "tema seguro", "Título", str(thumbnail))["allowed"] is True
