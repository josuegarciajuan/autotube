"""Tests for auto-recovery system — bug detection and startup recovery.

Run:  python3 -m pytest tests/test_auto_recovery.py -v
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import json
import pytest
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
    """Test auto_recover_on_startup() behaviour."""

    @patch("api.services.generation_service._get_db")
    @patch("api.services.generation_service._is_bug_crash")
    @patch("api.services.generation_service._run_reassembly_job")
    def test_marks_running_jobs_as_failed(self, mock_run_reassembly, mock_is_bug, mock_get_db):
        """All running/queued jobs are marked as failed."""
        db = MagicMock()
        mock_get_db.return_value = db
        conn = MagicMock()
        db._connect.return_value = conn
        conn.execute.return_value.fetchall.return_value = []  # no error videos
        conn.execute.return_value.rowcount = 3

        async def _run():
            from api.services.generation_service import auto_recover_on_startup
            await auto_recover_on_startup()

        import asyncio
        asyncio.run(_run())

        calls = [c[0][0] for c in conn.execute.call_args_list if isinstance(c[0], tuple)]
        assert any("UPDATE generation_jobs SET status='failed'" in str(c) for c in calls)

    @patch("api.services.generation_service._get_db")
    @patch("api.services.generation_service._is_bug_crash")
    @patch("api.services.generation_service._run_reassembly_job")
    def test_resets_stuck_videos(self, mock_run_reassembly, mock_is_bug, mock_get_db):
        """Videos stuck in 'generating'/'reassembling' are reset to 'error'."""
        db = MagicMock()
        mock_get_db.return_value = db
        conn = MagicMock()
        db._connect.return_value = conn
        conn.execute.return_value.fetchall.return_value = []
        conn.execute.return_value.rowcount = 2

        async def _run():
            from api.services.generation_service import auto_recover_on_startup
            await auto_recover_on_startup()

        import asyncio
        asyncio.run(_run())

        calls = [c[0][0] for c in conn.execute.call_args_list if isinstance(c[0], tuple)]
        assert any("generating" in str(c) and "reassembling" in str(c) for c in calls)

    @patch("api.services.generation_service._get_db")
    @patch("api.services.generation_service._is_bug_crash")
    @patch("api.services.generation_service._run_reassembly_job")
    def test_skips_bug_crashes(self, mock_run_reassembly, mock_is_bug, mock_get_db):
        """Bug-crash videos are skipped (no reassemble job created)."""
        db = MagicMock()
        mock_get_db.return_value = db
        mock_is_bug.return_value = True

        conn = MagicMock()
        db._connect.return_value = conn
        conn.execute.return_value.fetchall.return_value = [
            {"id": 42, "status": "error", "progress_phase": "video",
             "checkpoint_data": json.dumps({"tts": {"audio_path": "/tmp/test.mp3"}, "media": {"assets": []}}),
             "channel_id": 3, "script_id": None, "audio_path": None, "canal": "canal2"}
        ]
        conn.execute.return_value.rowcount = 0

        async def _run():
            from api.services.generation_service import auto_recover_on_startup
            await auto_recover_on_startup()

        import asyncio
        asyncio.run(_run())

        db.create_job.assert_not_called()
        mock_run_reassembly.assert_not_called()

    @patch("api.services.generation_service._get_db")
    @patch("api.services.generation_service._is_bug_crash")
    @patch("api.services.generation_service._run_reassembly_job")
    def test_recovers_interrupted_videos(self, mock_run_reassembly, mock_is_bug, mock_get_db):
        """Interrupted videos with valid checkpoint are auto-recovered."""
        db = MagicMock()
        mock_get_db.return_value = db
        mock_is_bug.return_value = False

        conn = MagicMock()
        db._connect.return_value = conn
        db.create_job.return_value = 77
        conn.execute.return_value.fetchone.side_effect = [(0,), None]

        audio_path = "/root/autotube/output/audio/test.mp3"
        conn.execute.return_value.fetchall.return_value = [
            {"id": 42, "status": "error", "progress_phase": "orphaned",
             "checkpoint_data": json.dumps({"tts": {"audio_path": audio_path}, "media": {"assets": []}}),
             "channel_id": 3, "script_id": None, "audio_path": None, "canal": "canal2"}
        ]
        conn.execute.return_value.rowcount = 1

        with patch.object(Path, "exists", return_value=True):
            async def _run():
                from api.services.generation_service import auto_recover_on_startup
                await auto_recover_on_startup()

            import asyncio
            asyncio.run(_run())

        db.create_job.assert_called_once_with(3, "reassemble", 42)
        mock_run_reassembly.assert_called_once()

    @patch("api.services.generation_service._get_db")
    @patch("api.services.generation_service._is_bug_crash")
    @patch("api.services.generation_service._run_reassembly_job")
    def test_skips_videos_without_checkpoint(self, mock_run_reassembly, mock_is_bug, mock_get_db):
        """Videos with no checkpoint data are not recovered."""
        db = MagicMock()
        mock_get_db.return_value = db
        mock_is_bug.return_value = False

        conn = MagicMock()
        db._connect.return_value = conn
        conn.execute.return_value.fetchall.return_value = [
            {"id": 42, "status": "error", "progress_phase": "video",
             "checkpoint_data": "{}", "channel_id": 3, "script_id": None, "audio_path": None, "canal": "canal2"}
        ]
        conn.execute.return_value.rowcount = 0

        async def _run():
            from api.services.generation_service import auto_recover_on_startup
            await auto_recover_on_startup()

        import asyncio
        asyncio.run(_run())

        db.create_job.assert_not_called()

    @patch("api.services.generation_service._get_db")
    @patch("api.services.generation_service._is_bug_crash")
    @patch("api.services.generation_service._run_reassembly_job")
    def test_skips_videos_with_missing_audio(self, mock_run_reassembly, mock_is_bug, mock_get_db):
        """Videos with checkpoint but missing audio file are not recovered."""
        db = MagicMock()
        mock_get_db.return_value = db
        mock_is_bug.return_value = False

        conn = MagicMock()
        db._connect.return_value = conn
        conn.execute.return_value.fetchall.return_value = [
            {"id": 42, "status": "error", "progress_phase": "video",
             "checkpoint_data": json.dumps({"tts": {"audio_path": "/nonexistent/file.mp3"}, "media": {"assets": []}}),
             "channel_id": 3, "script_id": None, "audio_path": None, "canal": "canal2"}
        ]
        conn.execute.return_value.rowcount = 0

        async def _run():
            from api.services.generation_service import auto_recover_on_startup
            await auto_recover_on_startup()

        import asyncio
        asyncio.run(_run())

        db.create_job.assert_not_called()

    @patch("api.services.generation_service._get_db")
    @patch("api.services.generation_service._is_bug_crash")
    @patch("api.services.generation_service._run_reassembly_job")
    def test_recovers_multiple_videos(self, mock_run_reassembly, mock_is_bug, mock_get_db):
        """Multiple error videos: some bugs, some recoverable, some unrecoverable."""
        db = MagicMock()
        mock_get_db.return_value = db
        mock_is_bug.side_effect = [True, False]

        conn = MagicMock()
        db._connect.return_value = conn
        db.create_job.return_value = 100
        conn.execute.return_value.fetchone.side_effect = [(0,), (0,), None]

        audio_path = "/root/autotube/output/audio/test.mp3"
        conn.execute.return_value.fetchall.return_value = [
            {"id": 10, "status": "error", "progress_phase": "video",
             "checkpoint_data": json.dumps({"tts": {"audio_path": audio_path}, "media": {"assets": []}}),
             "channel_id": 1, "script_id": None, "audio_path": None, "canal": "canal1"},
            {"id": 20, "status": "error", "progress_phase": "orphaned",
             "checkpoint_data": json.dumps({"tts": {"audio_path": audio_path}, "media": {"assets": []}}),
             "channel_id": 2, "script_id": None, "audio_path": None, "canal": "canal2"},
            {"id": 30, "status": "error", "progress_phase": "video",
             "checkpoint_data": "{}", "channel_id": 3, "script_id": None, "audio_path": None, "canal": "canal3"},
        ]
        conn.execute.return_value.rowcount = 2

        with patch.object(Path, "exists", return_value=True):
            async def _run():
                from api.services.generation_service import auto_recover_on_startup
                await auto_recover_on_startup()

            import asyncio
            asyncio.run(_run())

        assert db.create_job.call_count == 1
        db.create_job.assert_called_with(2, "reassemble", 20)
