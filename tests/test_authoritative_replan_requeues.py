"""Regression: authoritative replan must not cancel deliberate retries.

``authoritative_replan`` historically cancelled every queued job.  That wiped
the manual/transient requeues for videos in ``progress_phase='queued_retry'``
and left them stranded in error (oct 2026: #2712, #2714, #2723, #2729, #2734,
#2738).
"""
import sqlite3

from api.services.planning_service import _cancel_queued_jobs_for_replan


def _seed():
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE videos (
            id INTEGER PRIMARY KEY,
            is_marathon INTEGER DEFAULT 0,
            progress_phase TEXT
        );
        CREATE TABLE generation_jobs (
            id INTEGER PRIMARY KEY,
            video_id INTEGER,
            status TEXT,
            error_msg TEXT,
            finished_at TEXT
        );
        """
    )
    conn.executemany(
        "INSERT INTO videos (id, is_marathon, progress_phase) VALUES (?, ?, ?)",
        [
            (1, 0, "video"),
            (2, 0, "queued_retry"),
            (3, 1, "video"),
        ],
    )
    conn.executemany(
        "INSERT INTO generation_jobs (id, video_id, status) VALUES (?, ?, ?)",
        [
            (10, 1, "queued"),
            (11, 2, "queued"),
            (12, 3, "queued"),
            (13, 1, "running"),
        ],
    )
    conn.commit()
    return conn


def test_replan_cancels_only_stale_queued_jobs():
    conn = _seed()

    cancelled = _cancel_queued_jobs_for_replan(conn)

    assert cancelled == 1
    statuses = dict(conn.execute("SELECT id, status FROM generation_jobs"))
    assert statuses == {
        10: "cancelled",   # stale queued job
        11: "queued",      # deliberate retry
        12: "queued",      # marathon
        13: "running",     # never touched
    }
