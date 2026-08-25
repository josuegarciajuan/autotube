from __future__ import annotations

import sqlite3

from scripts.repair_fake_upload_failures import repair_database


def _make_db(path):
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE videos (id INTEGER PRIMARY KEY, yt_video_id TEXT, status TEXT);
        CREATE TABLE generation_jobs (
            id INTEGER PRIMARY KEY, video_id INTEGER, action TEXT, status TEXT,
            error_msg TEXT, finished_at TEXT
        );
        INSERT INTO videos VALUES (1, 'yt-ok', 'uploaded_private');
        INSERT INTO videos VALUES (2, 'yt-ok-2', 'uploaded');
        INSERT INTO videos VALUES (3, NULL, 'uploaded');
        INSERT INTO videos VALUES (4, 'yt-no', 'ready');
        INSERT INTO generation_jobs VALUES
            (100, 1, 'upload_only', 'failed', 'UnboundLocalError: _upload_retryable_fail', NULL),
            (101, 2, 'upload_only', 'failed', 'ordinary upload failure', NULL),
            (102, 3, 'upload_only', 'failed', '_upload_retryable_fail', NULL),
            (103, 4, 'upload_only', 'failed', '_upload_retryable_fail', NULL),
            (7436, 1, 'upload_only', 'failed', '_upload_retryable_fail', NULL),
            (7583, 2, 'upload_only', 'failed', '_upload_retryable_fail', NULL);
        """
    )
    conn.commit()
    conn.close()


def test_repair_is_narrow_idempotent_and_dry_run_safe(tmp_path):
    path = tmp_path / "test.db"
    _make_db(path)

    assert repair_database(path, dry_run=True) == [100]
    conn = sqlite3.connect(path)
    assert conn.execute("SELECT status FROM generation_jobs WHERE id=100").fetchone()[0] == "failed"
    conn.close()

    assert repair_database(path) == [100]
    assert repair_database(path) == []
    conn = sqlite3.connect(path)
    assert conn.execute("SELECT status, error_msg FROM generation_jobs WHERE id=100").fetchone() == ("completed", None)
    assert conn.execute("SELECT status FROM generation_jobs WHERE id IN (7436, 7583) ORDER BY id").fetchall() == [("failed",), ("failed",)]
    conn.close()
