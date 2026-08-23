"""Tests for auto-recovery system — bug detection and startup recovery.

Run:  python3 -m pytest tests/test_auto_recovery.py -v
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import json
import pytest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch, PropertyMock


# ══════════════════════════════════════════════════════════════════════
# _is_bug_crash tests
# ══════════════════════════════════════════════════════════════════════

class TestIsBugCrash:
    """Test _is_bug_crash() logic for distinguishing bugs vs interruptions."""

    def _mock_video_row(self, **overrides):
        row = {
            "id": 99,
            "status": "error",
            "progress_phase": "video",
            "checkpoint_data": json.dumps({
                "tts": {"audio_path": "output/audio/test.mp3"},
                "media": {"assets": []},
            }),
            "audio_path": "/tmp/test.mp3",
            "script_id": 75,
            "canal": "canal2",
        }
        row.update(overrides)
        return row

    @patch("api.services.generation_service._glob.glob")
    @patch("api.services.generation_service._get_db")
    def test_orphaned_is_not_bug(self, mock_get_db, mock_glob):
        """progress_phase='orphaned' → False (it was an interruption)."""
        from api.services.generation_service import _is_bug_crash
        row = self._mock_video_row(progress_phase="orphaned")
        assert _is_bug_crash(99, row) is False
        mock_glob.assert_not_called()  # won't reach crash log check

    @patch("api.services.generation_service._glob.glob")
    @patch("api.services.generation_service._get_db")
    def test_orphaned_with_trailing_whitespace(self, mock_get_db, mock_glob):
        """Whitespace in progress_phase is stripped."""
        from api.services.generation_service import _is_bug_crash
        row = self._mock_video_row(progress_phase="  orphaned  ")
        assert _is_bug_crash(99, row) is False

    @patch("api.services.generation_service._glob.glob")
    @patch("api.services.generation_service._get_db")
    def test_generic_error_phase_is_bug(self, mock_get_db, mock_glob):
        """progress_phase='error' (generic catch-all) → True (likely a bug)."""
        from api.services.generation_service import _is_bug_crash
        row = self._mock_video_row(progress_phase="error")
        assert _is_bug_crash(99, row) is True

    @patch("api.services.generation_service._glob.glob")
    @patch("api.services.generation_service._get_db")
    def test_interrupted_is_not_bug(self, mock_get_db, mock_glob):
        """progress_phase='interrupted' (set by auto-recovery) → not a bug."""
        from api.services.generation_service import _is_bug_crash
        row = self._mock_video_row(progress_phase="interrupted")
        assert _is_bug_crash(99, row) is False

    # ── crash log detection ──────────────────────────────────────

    @patch("api.services.generation_service._get_db")
    @patch("api.services.generation_service._glob.glob")
    def test_crash_log_with_traceback_is_bug(self, mock_glob, mock_get_db):
        """Crash log containing 'Traceback (most recent call last)' → bug."""
        from api.services.generation_service import _is_bug_crash

        mock_glob.return_value = ["/tmp/test.crash.log"]
        with patch("pathlib.Path.read_text") as mock_read:
            mock_read.return_value = "Traceback (most recent call last)\n  File \"x.py\"..."
            row = self._mock_video_row()
            assert _is_bug_crash(99, row) is True

    @patch("api.services.generation_service._get_db")
    @patch("api.services.generation_service._glob.glob")
    def test_no_crash_log_not_a_bug(self, mock_glob, mock_get_db):
        """No crash log and no pipeline_log signatures → not a bug."""
        from api.services.generation_service import _is_bug_crash

        mock_glob.return_value = []  # no files found by glob
        # Mock DB so pipeline_log check returns no results
        db = MagicMock()
        mock_get_db.return_value = db
        conn = MagicMock()
        db._connect.return_value = conn
        conn.execute.return_value.fetchall.return_value = []  # no matching logs

        row = self._mock_video_row()
        assert _is_bug_crash(99, row) is False

    @patch("api.services.generation_service._get_db")
    @patch("api.services.generation_service._glob.glob")
    def test_crash_log_without_traceback_not_bug(self, mock_glob, mock_get_db):
        """Crash log exists but no traceback → not a code bug."""
        from api.services.generation_service import _is_bug_crash

        mock_glob.return_value = ["/tmp/test.crash.log"]
        db = MagicMock()
        mock_get_db.return_value = db
        conn = MagicMock()
        db._connect.return_value = conn
        conn.execute.return_value.fetchall.return_value = []  # no pipeline_log matches

        with patch("pathlib.Path.read_text") as mock_read:
            mock_read.return_value = "Some other error without traceback"
            row = self._mock_video_row()
            assert _is_bug_crash(99, row) is False

    # ── pipeline_log crash signatures ───────────────────────────

    @patch("api.services.generation_service._glob.glob")
    @patch("api.services.generation_service._get_db")
    def test_pipeline_log_signature_is_bug(self, mock_get_db, mock_glob):
        """pipeline_log contains 'MoviePy render crashed' → bug."""
        from api.services.generation_service import _is_bug_crash

        mock_glob.return_value = []  # no crash log files
        db = MagicMock()
        mock_get_db.return_value = db
        conn = MagicMock()
        db._connect.return_value = conn
        conn.execute.return_value.fetchall.return_value = [
            {"message": "MoviePy render crashed: TypeError"}
        ]

        row = self._mock_video_row()
        assert _is_bug_crash(99, row) is True

    @patch("api.services.generation_service._glob.glob")
    @patch("api.services.generation_service._get_db")
    def test_pipeline_log_broadcast_error_is_bug(self, mock_get_db, mock_glob):
        """pipeline_log contains 'operands could not be broadcast' → bug."""
        from api.services.generation_service import _is_bug_crash

        mock_glob.return_value = []
        db = MagicMock()
        mock_get_db.return_value = db
        conn = MagicMock()
        db._connect.return_value = conn
        conn.execute.return_value.fetchall.return_value = [
            {"message": "operands could not be broadcast together with shapes (2000,) (2000,2)"}
        ]

        row = self._mock_video_row()
        assert _is_bug_crash(99, row) is True

    # ── edge cases ───────────────────────────────────────────────

    def test_none_row_returns_true_safe(self):
        """video_row=None → True (can't determine, be safe)."""
        from api.services.generation_service import _is_bug_crash
        assert _is_bug_crash(99, None) is True

    @patch("api.services.generation_service._glob.glob")
    @patch("api.services.generation_service._get_db")
    def test_empty_progress_phase_goes_to_crash_check(self, mock_get_db, mock_glob):
        """Empty progress_phase → not orphaned/error → continues to crash check."""
        from api.services.generation_service import _is_bug_crash

        mock_glob.return_value = []
        db = MagicMock()
        mock_get_db.return_value = db
        conn = MagicMock()
        db._connect.return_value = conn
        conn.execute.return_value.fetchall.return_value = []

        row = self._mock_video_row(progress_phase="")
        # Neither crash log nor pipeline_log → not a bug
        assert _is_bug_crash(99, row) is False

    @patch("api.services.generation_service._glob.glob")
    @patch("api.services.generation_service._get_db")
    def test_truncated_checkpoint_data(self, mock_get_db, mock_glob):
        """Broken JSON checkpoint → handled gracefully, falls through."""
        from api.services.generation_service import _is_bug_crash

        mock_glob.return_value = []
        db = MagicMock()
        mock_get_db.return_value = db
        conn = MagicMock()
        db._connect.return_value = conn
        conn.execute.return_value.fetchall.return_value = []

        row = self._mock_video_row(checkpoint_data="{broken json")
        # JSON parse error → cp = {} → no audio_path → falls to wildcard glob
        # No crash log → not a bug
        assert _is_bug_crash(99, row) is False


# ══════════════════════════════════════════════════════════════════════
# auto_recover_on_startup tests
# ══════════════════════════════════════════════════════════════════════

class TestAutoRecoverOnStartup:
    """Test auto_recover_on_startup() against a real in-memory DB.

    The current implementation is PID-aware: it queries the real DB with
    many distinct statements (queued kill, pgrep for running workers, stuck
    video reset, shorts re-queue, reassemble job creation).  Mocking every
    statement with a MagicMock conn is unmaintainable — we use a real
    sqlite3 :memory: DB and mock only the external bits (pgrep, _is_bug_crash,
    audio file existence).
    """

    _db_seq = 0

    @pytest.fixture
    def recovery_db(self):
        import sqlite3
        from database.db_extended import ExtendedDatabase

        # Shared-cache in-memory DB with a UNIQUE per-test name (the shared
        # cache persists for the process lifetime). A monotonic counter avoids
        # id(request) collisions after GC. auto_recover_on_startup() CLOSES its
        # connection at the end, so tests need a fresh connection.
        TestAutoRecoverOnStartup._db_seq += 1
        db_name = f"recovery_db_{TestAutoRecoverOnStartup._db_seq}"
        conn = sqlite3.connect(f"file:{db_name}?mode=memory&cache=shared", uri=True)
        conn.row_factory = sqlite3.Row
        conn.executescript("""
            CREATE TABLE channels (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT, slug TEXT, active BOOLEAN DEFAULT 1,
                config_json TEXT
            );
            CREATE TABLE videos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                canal TEXT, channel_id INTEGER,
                status TEXT DEFAULT 'draft',
                progress_phase TEXT,
                checkpoint_data TEXT DEFAULT '{}',
                is_marathon INTEGER DEFAULT 0,
                progress INTEGER DEFAULT 0,
                scheduled_upload_at TIMESTAMP,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE generation_jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                channel_id INTEGER, video_id INTEGER,
                action TEXT DEFAULT 'generate',
                status TEXT DEFAULT 'queued',
                error_msg TEXT,
                started_at TIMESTAMP, finished_at TIMESTAMP,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE shorts_planned_slots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                channel_id INTEGER, date_key TEXT, scheduled_at TEXT,
                short_type TEXT DEFAULT 'native', status TEXT DEFAULT 'pending',
                job_id INTEGER, error_message TEXT,
                updated_at TIMESTAMP
            );
            CREATE TABLE pipeline_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                canal TEXT, phase TEXT, status TEXT, message TEXT,
                content_id INTEGER, duration_ms INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)
        conn.execute(
            "INSERT INTO channels (id, name, slug, active) VALUES (1, 'Canal', 'canal2', 1)"
        )
        conn.commit()

        db = ExtendedDatabase.__new__(ExtendedDatabase)
        db._conn = conn
        db._db_name = db_name
        # Keeper connection: auto_recover_on_startup() closes its connection at
        # the end; the shared in-memory DB is dropped when the LAST connection
        # closes. Keeping one open preserves the schema/data for assertions.
        db._keeper = sqlite3.connect(
            f"file:{db_name}?mode=memory&cache=shared", uri=True)
        db._connect = lambda: conn  # plain connection: auto_recover calls db._connect() directly
        return db

    def _fresh_conn(self, db):
        import sqlite3
        conn = sqlite3.connect(f"file:{db._db_name}?mode=memory&cache=shared", uri=True)
        conn.row_factory = sqlite3.Row
        return conn

    def _run_recovery(self, db, is_bug=None, audio_exists=None, worker_stdout="",
                      is_bug_side_effect=None):
        """Run auto_recover_on_startup with the given mocks. Returns (db, fresh_conn)."""
        from api.services.generation_service import auto_recover_on_startup

        patchers = [
            patch("api.services.generation_service._get_db", return_value=db),
            patch("subprocess.run",
                  return_value=SimpleNamespace(stdout=worker_stdout)),
        ]
        if is_bug_side_effect is not None:
            patchers.append(patch(
                "api.services.generation_service._is_bug_crash",
                side_effect=is_bug_side_effect))
        elif is_bug is not None:
            patchers.append(patch(
                "api.services.generation_service._is_bug_crash",
                return_value=is_bug))
        if audio_exists is not None:
            patchers.append(patch("pathlib.Path.exists", return_value=audio_exists))

        for p in patchers:
            p.start()
        try:
            async def _run():
                await auto_recover_on_startup()
            import asyncio
            asyncio.run(_run())
        finally:
            for p in patchers:
                p.stop()
        return db, self._fresh_conn(db)

    def _video_row(self, **overrides):
        row = {
            "id": 99, "canal": "canal2", "channel_id": 1,
            "status": "error", "progress_phase": "video",
            "checkpoint_data": json.dumps({
                "tts": {"audio_path": "output/audio/test.mp3"},
                "media": {"assets": []},
            }),
            "is_marathon": 0, "progress": 0,
        }
        row.update(overrides)
        return row

    def _insert_video(self, conn, row):
        conn.execute(
            "INSERT INTO videos (id, canal, channel_id, status, progress_phase,"
            " checkpoint_data, is_marathon, progress) VALUES (?,?,?,?,?,?,?,?)",
            (row["id"], row["canal"], row["channel_id"], row["status"],
             row["progress_phase"], row["checkpoint_data"],
             row["is_marathon"], row["progress"]),
        )

    def test_marks_running_jobs_as_failed(self, recovery_db):
        """Queued + running jobs (worker dead) are marked failed on restart."""
        conn = recovery_db._conn
        conn.execute(
            "INSERT INTO generation_jobs (id, channel_id, video_id, action, status) "
            "VALUES (1, 1, NULL, 'generate', 'queued')"
        )
        conn.execute(
            "INSERT INTO generation_jobs (id, channel_id, video_id, action, status) "
            "VALUES (2, 1, 50, 'generate_and_upload', 'running')"
        )
        conn.execute(
            "INSERT INTO videos (id, canal, channel_id, status) "
            "VALUES (50, 'canal2', 1, 'generating')"
        )
        conn.commit()

        _db, conn = self._run_recovery(recovery_db, worker_stdout="")  # worker DEAD

        statuses = dict(conn.execute(
            "SELECT id, status FROM generation_jobs").fetchall())
        assert statuses[1] == "failed", "queued job must be marked failed"
        assert statuses[2] == "failed", "running job with dead worker must be failed"
        vid_status = conn.execute(
            "SELECT status FROM videos WHERE id=50").fetchone()["status"]
        assert vid_status == "error"

    def test_resets_stuck_videos(self, recovery_db):
        """Videos stuck in generating/reassembling with a dead job → error."""
        conn = recovery_db._conn
        conn.execute(
            "INSERT INTO generation_jobs (id, channel_id, video_id, action, status) "
            "VALUES (1, 1, 60, 'generate_and_upload', 'failed')"
        )
        conn.execute(
            "INSERT INTO videos (id, canal, channel_id, status, progress_phase) "
            "VALUES (60, 'canal2', 1, 'generating', 'video')"
        )
        conn.commit()

        _db, conn = self._run_recovery(recovery_db, worker_stdout="")

        row = conn.execute(
            "SELECT status, progress_phase FROM videos WHERE id=60").fetchone()
        assert row["status"] == "error"
        assert row["progress_phase"] == "interrupted"

    def test_skips_bug_crashes(self, recovery_db):
        """Bug-crash videos are skipped (no reassemble job created)."""
        conn = recovery_db._conn
        self._insert_video(conn, self._video_row(id=42))
        conn.commit()

        _db, conn = self._run_recovery(recovery_db, is_bug=True, audio_exists=True)

        assert conn.execute(
            "SELECT COUNT(*) FROM generation_jobs "
            "WHERE action='reassemble'").fetchone()[0] == 0
        row = conn.execute("SELECT status FROM videos WHERE id=42").fetchone()
        assert row["status"] == "error"

    def test_recovers_interrupted_videos(self, recovery_db):
        """Interrupted videos with valid checkpoint + audio are auto-recovered."""
        conn = recovery_db._conn
        self._insert_video(conn, self._video_row(id=42))
        conn.commit()

        _db, conn = self._run_recovery(recovery_db, is_bug=False, audio_exists=True)

        jobs = conn.execute(
            "SELECT video_id, action, status FROM generation_jobs "
            "WHERE action='reassemble'").fetchall()
        assert len(jobs) == 1
        assert jobs[0]["video_id"] == 42
        assert jobs[0]["status"] == "queued"
        vid_status = conn.execute(
            "SELECT status FROM videos WHERE id=42").fetchone()["status"]
        assert vid_status == "reassembling"

    def test_skips_videos_without_checkpoint(self, recovery_db):
        """Videos with no checkpoint data are not recovered."""
        conn = recovery_db._conn
        self._insert_video(conn, self._video_row(id=42, checkpoint_data="{}"))
        conn.commit()

        _db, conn = self._run_recovery(recovery_db, is_bug=False)

        assert conn.execute(
            "SELECT COUNT(*) FROM generation_jobs").fetchone()[0] == 0
        row = conn.execute("SELECT status FROM videos WHERE id=42").fetchone()
        assert row["status"] == "error"

    def test_skips_videos_with_missing_audio(self, recovery_db):
        """Videos with checkpoint but missing audio file are not recovered."""
        conn = recovery_db._conn
        self._insert_video(conn, self._video_row(id=42))
        conn.commit()

        _db, conn = self._run_recovery(recovery_db, is_bug=False, audio_exists=False)

        assert conn.execute(
            "SELECT COUNT(*) FROM generation_jobs").fetchone()[0] == 0

    def test_recovers_multiple_videos(self, recovery_db):
        """Multiple error videos: some bugs, some recoverable, some unrecoverable."""
        conn = recovery_db._conn
        self._insert_video(conn, self._video_row(id=10, channel_id=1))
        self._insert_video(conn, self._video_row(id=20, channel_id=1))
        self._insert_video(conn, self._video_row(id=30, checkpoint_data="{}"))
        conn.commit()

        def fake_is_bug(video_id, row):
            return video_id == 10  # only video 10 is a bug

        _db, conn = self._run_recovery(
            recovery_db, audio_exists=True, is_bug_side_effect=fake_is_bug)

        jobs = conn.execute(
            "SELECT video_id FROM generation_jobs WHERE action='reassemble'").fetchall()
        assert len(jobs) == 1, f"Only video 20 should be recovered, got {jobs}"
        assert jobs[0]["video_id"] == 20
