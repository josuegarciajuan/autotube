"""Tests for pipeline defenses: zombie guard, orphan detector.

Run:  python3 -m pytest tests/test_defenses.py -v
"""

import sys
sys.path.insert(0, "/root/autotube")

import pytest
from unittest.mock import MagicMock, patch, AsyncMock
from tests.conftest import MockDB


class TestZombieGuard:
    """Test _broadcast_progress() refuses to resurrect dead jobs."""

    def _make_db_with_job(self, job_id: int, status: str):
        """Create a DB stub that returns a specific job status."""
        db = MagicMock()
        db.get_job = MagicMock(return_value={"id": job_id, "status": status})
        db.update_job = MagicMock()
        return db

    @patch("api.progress.get_progress_manager")
    @patch("api.services.generation_service._get_db")
    @pytest.mark.asyncio
    async def test_zombie_cannot_resurrect_failed(self, mock_get_db, mock_mgr):
        """Job is 'failed' → progress(status='running') is ignored."""
        db = self._make_db_with_job(1, "failed")
        mock_get_db.return_value = db
        mock_mgr_instance = MagicMock()
        mock_mgr_instance.broadcast = AsyncMock()
        mock_mgr.return_value = mock_mgr_instance

        from api.services.generation_service import _broadcast_progress
        await _broadcast_progress(1, 50, "script", "test", status="running")

        db.update_job.assert_not_called()

    @patch("api.progress.get_progress_manager")
    @patch("api.services.generation_service._get_db")
    @pytest.mark.asyncio
    async def test_zombie_cannot_resurrect_completed(self, mock_get_db, mock_mgr):
        """Job is 'completed' → progress(status='running') is ignored."""
        db = self._make_db_with_job(2, "completed")
        mock_get_db.return_value = db
        mock_mgr_instance = MagicMock()
        mock_mgr_instance.broadcast = AsyncMock()
        mock_mgr.return_value = mock_mgr_instance

        from api.services.generation_service import _broadcast_progress
        await _broadcast_progress(2, 50, "script", "test", status="running")

        db.update_job.assert_not_called()

    @patch("api.progress.get_progress_manager")
    @patch("api.services.generation_service._get_db")
    @pytest.mark.asyncio
    async def test_normal_progress_still_works(self, mock_get_db, mock_mgr):
        """Job is 'running' → normal progress update proceeds."""
        db = self._make_db_with_job(3, "running")
        mock_get_db.return_value = db
        mock_mgr_instance = MagicMock()
        mock_mgr_instance.broadcast = AsyncMock()
        mock_mgr.return_value = mock_mgr_instance

        from api.services.generation_service import _broadcast_progress
        await _broadcast_progress(3, 50, "script", "test", status="running")

        db.update_job.assert_called()

    def test_cancel_request_still_works(self):
        """cancel_job() with registered orchestrator → request_stop() called."""
        from api.services.generation_service import (
            register_orchestrator, unregister_orchestrator, cancel_job,
            _active_orchestrators,
        )
        mock_orch = MagicMock()
        mock_orch.request_stop = MagicMock()
        register_orchestrator(99, mock_orch)

        result = cancel_job(99)
        assert result is True
        mock_orch.request_stop.assert_called_once()

        unregister_orchestrator(99)

    def test_cancel_unknown_job(self):
        """Cancelling unregistered job → False."""
        from api.services.generation_service import cancel_job
        result = cancel_job(99999)
        assert result is False
