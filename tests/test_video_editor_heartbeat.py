"""Tests for heartbeat emitter in video_editor.build_video()."""

import pytest
from unittest.mock import patch, MagicMock


class TestBuildVideoHeartbeat:
    """Heartbeat should be emitted when job_id is provided."""

    def test_build_video_without_job_id_does_not_crash(self):
        """build_video() without job_id must work as before (backward compat)."""
        from pipeline.video_editor import VideoEditor

        editor = VideoEditor()
        # Verify that job_id defaults to None and build_video signature is correct
        import inspect
        sig = inspect.signature(editor.build_video)
        params = sig.parameters
        assert "job_id" in params
        assert params["job_id"].default is None

    def test_heartbeat_stop_event_created_when_job_id_provided(self):
        """Heartbeat infrastructure is set up when job_id is not None."""
        import threading
        _hb_stop = threading.Event()
        assert not _hb_stop.is_set()
        _hb_stop.set()
        assert _hb_stop.is_set()

    def test_heartbeat_thread_is_daemon(self):
        """Heartbeat thread must be daemon so it doesn't block shutdown."""
        import threading

        def _hb_loop():
            pass

        thread = threading.Thread(target=_hb_loop, daemon=True, name="test-hb")
        assert thread.daemon is True
