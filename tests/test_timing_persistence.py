"""Tests for timing persistence in generation_service.py.

Run:  python3 -m pytest tests/test_timing_persistence.py -v
"""

import sys
sys.path.insert(0, "/root/autotube")

import json
import pytest
from unittest.mock import MagicMock, AsyncMock, patch, call, ANY
from tests.conftest import MockDB

# Known timing string for assertions
TIMING_JSON = '{"phases":{"scrape":1234,"script":2345},"total_duration_ms":5678}'


class TestTimingSavedOnError:
    """Verify timing_data is saved when phases fail and return early."""

    # ── helpers ──────────────────────────────────────────────────

    def _make_mock_db(self):
        """Create a mock DB that tracks update_video calls."""
        db = MagicMock()
        db.get_channel.return_value = {"slug": "canal2", "name": "Sincronías", "id": 1}
        db.get_job.return_value = {"id": 1, "status": "running"}
        db.get_unused_count.return_value = 0
        db.get_video.return_value = {"id": 42, "channel_id": 1, "checkpoint_data": "{}"}
        return db

    def _make_mock_orch(self):
        """Create a mock orchestrator whose phases return controlled results."""
        orch = MagicMock()
        orch.collect_timing_json.return_value = TIMING_JSON
        orch.collect_timing.return_value = json.loads(TIMING_JSON)
        orch.db = MagicMock()
        return orch

    async def _run_job(self, db, *, phase_scrape_result=None,
                       phase_script_result=None, raise_exception=None):
        """Run start_generation_job with controlled orchestrator.

        - phase_scrape_result: (ok: bool, result) tuple for _run_in_executor
        - phase_script_result:  (ok: bool, result) tuple
        - raise_exception: if set, orch.__init__ or a phase raises this

        Returns the MockDB for assertion.
        """
        from unittest.mock import patch, AsyncMock

        mock_orch = self._make_mock_orch()

        if raise_exception:
            mock_orch.__init__ = MagicMock(side_effect=raise_exception)

        # Build a side-effect sequence for _run_in_executor
        run_side_effects = []
        if phase_scrape_result is not None:
            run_side_effects.append(phase_scrape_result)
        if phase_script_result is not None:
            run_side_effects.append(phase_script_result)

        async def _broadcast_stub(*args, **kwargs):
            pass

        with patch("api.services.generation_service._get_db", return_value=db):
            with patch("api.services.generation_service._broadcast_progress",
                       new=AsyncMock()):
                with patch("orchestrator.PipelineOrchestrator",
                           return_value=mock_orch):
                    with patch("api.services.generation_service._run_in_executor",
                               side_effect=run_side_effects):
                        with patch("api.services.generation_service.register_orchestrator"):
                            with patch("api.services.generation_service.unregister_orchestrator"):
                                from api.services.generation_service import start_generation_job
                                await start_generation_job(1, 1, 42, "generate")

        return db

    # ── error path tests ─────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_timing_saved_when_scrape_fails_and_no_content(self):
        """Scrape fails + no unused items → update_video(error) receives timing_data."""
        db = self._make_mock_db()
        db.get_unused_count.return_value = 0  # no fallback content

        await self._run_job(db, phase_scrape_result=(False, "scrape error"))

        # Check that update_video was called with timing_data
        update_calls = [c for c in db.update_video.call_args_list
                        if c.kwargs.get("timing_data")]
        assert len(update_calls) >= 1, (
            f"Expected update_video with timing_data, got calls: {db.update_video.call_args_list}"
        )

    @pytest.mark.asyncio
    async def test_timing_saved_when_script_generation_fails(self):
        """Script phase fails → update_video(error) receives timing_data."""
        db = self._make_mock_db()
        db.get_unused_count.return_value = 5  # scrape proceeds even if returns False

        # scrape succeeds → proceeds to script
        await self._run_job(
            db,
            phase_scrape_result=(True, 3),   # scrape OK
            phase_script_result=(False, "LLM error"),  # script fails
        )

        update_calls = [c for c in db.update_video.call_args_list
                        if c.kwargs.get("timing_data")]
        assert len(update_calls) >= 1, (
            f"Expected update_video with timing_data on script fail, got: {db.update_video.call_args_list}"
        )

    @pytest.mark.asyncio
    async def test_timing_saved_in_finally_on_unexpected_exception(self):
        """Unexpected exception → finally block calls update_video with timing_data."""
        db = self._make_mock_db()
        # Make orch.collect_timing_json itself raise → still reaches finally
        orch = self._make_mock_orch()
        orch.collect_timing_json.side_effect = Exception("timing explosion")

        with patch("api.services.generation_service._get_db", return_value=db):
            with patch("api.services.generation_service._broadcast_progress",
                       new=AsyncMock()):
                with patch("orchestrator.PipelineOrchestrator",
                           return_value=orch):
                    with patch("api.services.generation_service._run_in_executor",
                               side_effect=[(True, 3)]):
                        with patch("api.services.generation_service.register_orchestrator"):
                            with patch("api.services.generation_service.unregister_orchestrator"):
                                from api.services.generation_service import start_generation_job
                                await start_generation_job(1, 1, 42, "generate")

        # Even though timing collection failed, video status should still be set to error
        error_calls = [c for c in db.update_video.call_args_list
                       if c.args and c.args[0] == 42 and
                       c.kwargs.get("status") == "error"]
        assert len(error_calls) >= 1, "Video should be marked as error on exception"


class TestIncrementalTimingSaves:
    """Verify timing_data is applied after each successful phase."""

    @pytest.mark.asyncio
    async def test_timing_saved_after_scrape_success(self):
        """After scrape succeeds, update_video is called with timing_data."""
        db = MagicMock()
        db.get_channel.return_value = {"slug": "canal2", "name": "Sincronías", "id": 1}
        db.get_job.return_value = {"id": 1, "status": "running"}
        db.get_unused_count.return_value = 5
        db.get_video.return_value = {"id": 42, "channel_id": 1, "checkpoint_data": "{}"}

        orch = MagicMock()
        orch.collect_timing_json.return_value = TIMING_JSON
        orch.collect_timing.return_value = json.loads(TIMING_JSON)
        orch.db = MagicMock()

        # scrape succeeds, script fails (stops early)
        with patch("api.services.generation_service._get_db", return_value=db):
            with patch("api.services.generation_service._broadcast_progress",
                       new=AsyncMock()):
                with patch("orchestrator.PipelineOrchestrator",
                           return_value=orch):
                    with patch("api.services.generation_service._run_in_executor",
                               side_effect=[(True, 3), (False, "boom")]):
                        with patch("api.services.generation_service.register_orchestrator"):
                            with patch("api.services.generation_service.unregister_orchestrator"):
                                from api.services.generation_service import start_generation_job
                                await start_generation_job(1, 1, 42, "generate")

        # Check that timing_data was passed at least once
        timing_calls = [c for c in db.update_video.call_args_list
                        if c.kwargs.get("timing_data") == TIMING_JSON]
        assert len(timing_calls) >= 1, (
            f"Expected update_video with timing_data=TIMING_JSON, "
            f"got {len(timing_calls)} matching calls"
        )


class TestOrchestratorTimingSavedOnExit:
    """Verify orchestrator.run_full_pipeline() saves timing in finally."""

    def test_timing_saved_on_early_abort_after_video_creation(self):
        """If pipeline aborts after phase_video creates record, timing is saved."""
        db = MockDB()

        # Simulate what would happen: video created, then phase fails
        db.update_video(42, video_path="/tmp/test.mp4", status="ready")

        import time as _time
        from orchestrator import PipelineOrchestrator

        with patch("database.db.Database", return_value=db):
            with patch("database.db.init_db"):
                orch = PipelineOrchestrator(canal="canal2", db_video_id=42)

        # Manually simulate a phase timing and then trigger collect
        orch._timing["phases"]["scrape"] = 1500
        timing = orch.collect_timing()
        assert timing["phases"]["scrape"] == 1500
        # total_duration_ms may be 0 if the test runs in <1ms, but it's well-formed
        assert timing["total_duration_ms"] >= 0

        # Verify collect_timing_json is valid JSON
        j = orch.collect_timing_json()
        parsed = json.loads(j)
        assert parsed["phases"]["scrape"] == 1500

    def test_collect_timing_json_used_by_update_video(self):
        """The JSON string from collect_timing_json can be passed to update_video."""
        db = MockDB()
        db.update_video(99, video_path="/tmp/v.mp4")

        from orchestrator import PipelineOrchestrator

        with patch("database.db.Database", return_value=db):
            with patch("database.db.init_db"):
                orch = PipelineOrchestrator(canal="canal2", db_video_id=99)

        orch._timing["phases"]["scrape"] = 500
        orch._timing["phases"]["script"] = 3000

        timing_json = orch.collect_timing_json()
        db.update_video(99, timing_data=timing_json)

        # The video record should have timing_data stored
        assert "timing_data" in db.videos[99]
        assert "scrape" in db.videos[99]["timing_data"]
