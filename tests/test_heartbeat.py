"""Tests for heartbeat system — prevents false orphan detection during long renders."""

import pytest
import sqlite3
import time
from database.db_extended import ExtendedDatabase


@pytest.fixture
def db():
    """In-memory database with schema and migration applied."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")

    # Create minimal schema
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS channels (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT, slug TEXT, active BOOLEAN DEFAULT 1,
            config_json TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            banner_url TEXT, avatar_url TEXT, description TEXT,
            yt_channel_id TEXT, yt_channel_url TEXT, google_account TEXT
        );
        CREATE TABLE IF NOT EXISTS videos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            canal TEXT, channel_id INTEGER REFERENCES channels(id),
            video_path TEXT, thumbnail_path TEXT, audio_path TEXT,
            yt_video_id TEXT, yt_url TEXT, titulo_final TEXT,
            duracion_seg INTEGER, privacy_status TEXT,
            uploaded_at TIMESTAMP, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            status TEXT DEFAULT 'draft', progress INTEGER DEFAULT 0,
            progress_phase TEXT, description TEXT, tags_json TEXT,
            title_options TEXT, checkpoint_data TEXT DEFAULT '{}',
            timing_data TEXT DEFAULT '{}'
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

    # Create ExtendedDatabase with overridden _connect to use our in-memory db
    db = ExtendedDatabase.__new__(ExtendedDatabase)
    db._conn = conn

    # Patch _connect to return/reuse our connection
    import contextlib

    @contextlib.contextmanager
    def _connect(self):
        try:
            yield self._conn
        finally:
            pass

    db._connect = _connect.__get__(db, ExtendedDatabase)

    yield db


class TestHeartbeatUpdate:
    """update_heartbeat() should set the timestamp."""

    def test_heartbeat_updates_timestamp(self, db):
        db.create_job(1, "generate_and_upload")
        job = db.get_next_queued_job()
        assert job is not None

        # Simulate job starting
        db._conn.execute(
            "UPDATE generation_jobs SET status='running', phase='video', started_at=CURRENT_TIMESTAMP WHERE id=?",
            (job["id"],),
        )
        db._conn.commit()

        # No heartbeat yet
        row = db._conn.execute(
            "SELECT last_heartbeat_at FROM generation_jobs WHERE id=?", (job["id"],)
        ).fetchone()
        assert row["last_heartbeat_at"] is None

        # Emit heartbeat
        db.update_heartbeat(job["id"])

        row = db._conn.execute(
            "SELECT last_heartbeat_at FROM generation_jobs WHERE id=?", (job["id"],)
        ).fetchone()
        assert row["last_heartbeat_at"] is not None

    def test_heartbeat_with_invalid_job_id(self, db):
        """Should not crash on non-existent job."""
        try:
            db.update_heartbeat(99999)
        except Exception as e:
            pytest.fail(f"update_heartbeat with invalid job_id should not raise: {e}")


class TestHeartbeatOrphanDetection:
    """Orphan detector must respect heartbeat for video-phase jobs."""

    def _create_running_video_job(self, db, seconds_ago=30, with_heartbeat=True):
        """Helper: create a job running in video phase."""
        db.create_job(1, "generate_and_upload")
        job = db.get_next_queued_job()

        # Simulate job running
        db._conn.execute(
            "UPDATE generation_jobs SET status='running', phase='video', "
            f"started_at = datetime('now', '-{seconds_ago} seconds') WHERE id=?",
            (job["id"],),
        )

        if with_heartbeat:
            db._conn.execute(
                "UPDATE generation_jobs SET last_heartbeat_at = datetime('now', '-10 seconds') WHERE id=?",
                (job["id"],),
            )

        db._conn.commit()
        return job

    def test_job_with_recent_heartbeat_not_orphaned(self, db):
        """A job that emitted a heartbeat 10s ago is alive."""
        self._create_running_video_job(db, seconds_ago=120, with_heartbeat=True)

        result = db.cleanup_orphaned_jobs(timeout_minutes=60)
        assert result["jobs_failed"] == 0, (
            f"Job should NOT be orphaned with recent heartbeat: {result}"
        )

    def test_job_with_stale_heartbeat_is_orphaned(self, db):
        """A job whose last heartbeat was >20 min ago is dead."""
        job = self._create_running_video_job(db, seconds_ago=3600, with_heartbeat=False)

        # Set heartbeat to 25 min ago (beyond 20 min threshold)
        db._conn.execute(
            "UPDATE generation_jobs SET last_heartbeat_at = datetime('now', '-1500 seconds') WHERE id=?",
            (job["id"],),
        )
        # Also need a video row for the JOIN
        db._conn.execute(
            "INSERT INTO videos (id, canal, channel_id, status) VALUES (99, 'test', 1, 'generating')"
        )
        db._conn.execute(
            "UPDATE generation_jobs SET video_id=99 WHERE id=?", (job["id"],)
        )
        db._conn.commit()

        result = db.cleanup_orphaned_jobs(timeout_minutes=60)
        assert result["jobs_failed"] >= 1, (
            f"Job with stale heartbeat (>20 min) SHOULD be orphaned: {result}"
        )

    def test_job_without_heartbeat_uses_started_at_fallback(self, db):
        """A job that never emitted a heartbeat uses started_at-based legacy timeout."""
        self._create_running_video_job(db, seconds_ago=29000, with_heartbeat=False)  # ~8h ago

        # Need a video for the JOIN
        db._conn.execute(
            "INSERT INTO videos (id, canal, channel_id, status) VALUES (98, 'test', 1, 'generating')"
        )
        db._conn.execute(
            "UPDATE generation_jobs SET video_id=98 WHERE last_heartbeat_at IS NULL AND phase='video'"
        )
        db._conn.commit()

        result = db.cleanup_orphaned_jobs(timeout_minutes=60)
        assert result["jobs_failed"] >= 1, (
            f"Job without heartbeat + >480 min started SHOULD be orphaned: {result}"
        )

    def test_job_without_heartbeat_under_legacy_timeout_survives(self, db):
        """A job without heartbeat that started <480 min ago survives legacy check."""
        self._create_running_video_job(db, seconds_ago=600, with_heartbeat=False)  # 10 min ago

        db._conn.execute(
            "INSERT INTO videos (id, canal, channel_id, status) VALUES (97, 'test', 1, 'generating')"
        )
        db._conn.execute(
            "UPDATE generation_jobs SET video_id=97 WHERE last_heartbeat_at IS NULL AND phase='video'"
        )
        db._conn.commit()

        result = db.cleanup_orphaned_jobs(timeout_minutes=60)
        assert result["jobs_failed"] == 0, (
            f"Job without heartbeat but <480 min started should survive: {result}"
        )
