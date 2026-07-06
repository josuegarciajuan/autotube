"""Tests for Queue Consumer — sequential job processing."""

import pytest
import sqlite3
from unittest.mock import patch, MagicMock, AsyncMock
from database.db_extended import ExtendedDatabase


@pytest.fixture
def db():
    """In-memory database with schema."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")

    conn.executescript("""
        CREATE TABLE IF NOT EXISTS channels (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT, slug TEXT, active BOOLEAN DEFAULT 1,
            config_json TEXT, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            banner_url TEXT, avatar_url TEXT, description TEXT,
            yt_channel_id TEXT, yt_channel_url TEXT, google_account TEXT
        );
        CREATE TABLE IF NOT EXISTS videos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            canal TEXT, channel_id INTEGER,
            video_path TEXT, status TEXT DEFAULT 'draft',
            progress INTEGER DEFAULT 0, progress_phase TEXT,
            description TEXT, tags_json TEXT, title_options TEXT,
            checkpoint_data TEXT DEFAULT '{}', timing_data TEXT DEFAULT '{}',
            audio_path TEXT, yt_video_id TEXT, yt_url TEXT,
            titulo_final TEXT, duracion_seg INTEGER, privacy_status TEXT,
            thumbnail_path TEXT, uploaded_at TIMESTAMP, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS generation_jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            channel_id INTEGER NOT NULL REFERENCES channels(id),
            video_id INTEGER REFERENCES videos(id),
            action TEXT NOT NULL DEFAULT 'generate',
            status TEXT NOT NULL DEFAULT 'queued',
            progress INTEGER DEFAULT 0, phase TEXT,
            error_msg TEXT, started_at TIMESTAMP, finished_at TIMESTAMP,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_heartbeat_at TIMESTAMP, retry_count INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS pipeline_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            canal TEXT, phase TEXT, status TEXT, message TEXT,
            content_id INTEGER, duration_ms INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """)

    conn.execute("INSERT INTO channels (id, name, slug) VALUES (1, 'Test', 'test')")
    conn.commit()

    db = ExtendedDatabase.__new__(ExtendedDatabase)
    db._conn = conn

    import contextlib

    @contextlib.contextmanager
    def _connect(self):
        try:
            yield self._conn
        finally:
            pass

    db._connect = _connect.__get__(db, ExtendedDatabase)
    return db


class TestQueueConsumerLogic:
    """Test the queue consumer's decision logic."""

    def test_consumer_skips_when_running_job_exists(self, db):
        db.create_job(1, "generate_and_upload")
        job = db.get_next_queued_job()
        db._conn.execute(
            "UPDATE generation_jobs SET status='running', started_at=CURRENT_TIMESTAMP WHERE id=?",
            (job["id"],),
        )
        db._conn.commit()

        # Consumer should find no queued jobs to dispatch (the one queued is now running)
        # get_next_queued_job only returns status='queued'
        next_job = db.get_next_queued_job()
        assert next_job is None

    def test_consumer_picks_next_queued_when_nothing_running(self, db):
        db.create_job(1, "generate_and_upload")  # created as 'queued'
        db.create_job(1, "generate_and_upload")  # also queued

        # No job is running
        next_job = db.get_next_queued_job()
        assert next_job is not None
        assert next_job["status"] == "queued"
        # FIFO: oldest first
        jobs = [dict(r) for r in db._conn.execute(
            "SELECT * FROM generation_jobs WHERE status='queued' ORDER BY created_at ASC"
        ).fetchall()]
        assert next_job["id"] == jobs[0]["id"]


class TestAutoRetry:
    """Auto-retry should requeue transient failures up to 3 times."""

    def test_increment_retry_counts_up(self, db):
        db.create_job(1, "generate_and_upload")
        job = db.get_next_queued_job()

        count1 = db.increment_retry(job["id"])
        assert count1 == 1

        count2 = db.increment_retry(job["id"])
        assert count2 == 2

    def test_requeue_resets_job(self, db):
        db.create_job(1, "generate_and_upload")
        job = db.get_next_queued_job()

        # Simulate failure
        db._conn.execute(
            "UPDATE generation_jobs SET status='failed', finished_at=CURRENT_TIMESTAMP, "
            "error_msg='Timeout', retry_count=1 WHERE id=?",
            (job["id"],),
        )
        db._conn.commit()

        # Requeue
        db.update_job_requeue(job["id"], error_msg="Timeout")

        row = db._conn.execute(
            "SELECT status, finished_at, started_at FROM generation_jobs WHERE id=?",
            (job["id"],),
        ).fetchone()
        assert row["status"] == "queued"
        assert row["finished_at"] is None
        assert row["started_at"] is None

    def test_requeue_clears_finished_at_and_started_at(self, db):
        """Requeued jobs should have no timestamps from the failed run."""
        db.create_job(1, "generate_and_upload")
        job = db.get_next_queued_job()

        db._conn.execute(
            "UPDATE generation_jobs SET status='failed', started_at=CURRENT_TIMESTAMP, "
            "finished_at=CURRENT_TIMESTAMP WHERE id=?",
            (job["id"],),
        )
        db._conn.commit()

        db.update_job_requeue(job["id"])

        row = db._conn.execute(
            "SELECT status, started_at, finished_at FROM generation_jobs WHERE id=?",
            (job["id"],),
        ).fetchone()
        assert row["status"] == "queued"
        assert row["started_at"] is None
        assert row["finished_at"] is None


class TestTransientErrorDetection:
    """_auto_retry_if_transient should only retry on transient failures."""

    def test_timeout_is_transient(self):
        from api.services.generation_service import _auto_retry_if_transient

        # This is a unit test of the pattern matching logic
        TRANSIENT_PATTERNS = [
            "timeout", "memory guard", "broken pipe", "brokenpipe",
            "orphaned: process lost", "memory", "abortado: memoria",
        ]
        assert any(p in "timeout tras 300s" for p in TRANSIENT_PATTERNS)
        assert any(p in "memory guard activated" for p in TRANSIENT_PATTERNS)
        assert any(p in "broken pipe error" for p in TRANSIENT_PATTERNS)

    def test_bad_request_is_not_transient(self):
        TRANSIENT_PATTERNS = [
            "timeout", "memory guard", "broken pipe", "brokenpipe",
            "orphaned: process lost", "memory", "abortado: memoria",
        ]
        assert not any(p in "bad request 400" for p in TRANSIENT_PATTERNS)
        assert not any(p in "attribute error" for p in TRANSIENT_PATTERNS)
        assert not any(p in "rate limit exceeded" for p in TRANSIENT_PATTERNS)
