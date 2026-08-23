"""Tests for orchestrator.py → phase_generate_script() acceptance logic.

Run:  python3 -m pytest tests/test_orchestrator.py -v
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import ast
import pytest
from unittest.mock import MagicMock, patch
from tests.conftest import MockDB, MockConfigCanal2


class TestPhaseGenerateScript:
    """Test phase_generate_script() acceptance/rejection/cancel."""

    def _make_mock_script_result(self, word_count: int, script_id: int = 1):
        """Build a script dict as returned by generate_v2."""
        return {
            "id": script_id,
            "guion": "palabra " * word_count,
            "titulo_options": ["Título test"],
            "escenas": [],
            "duracion_estimada": word_count / 165.0,
        }

    def _setup_orchestrator(self, db, content_items: list, script_results: list):
        """Create a mock orchestrator with controlled content and script gen."""
        from orchestrator import PipelineOrchestrator
        from config.config_bridge import get_channel_config
        import importlib

        # Load real canal2 config for the orchestrator
        cfg = importlib.import_module("config.canal2_config")

        # Override DB content
        db.raw_content = content_items

        # Mock get_unused_content to return our items
        def mock_get_unused(canal, limit=200, strategy="best_first"):
            return db.raw_content[:limit]
        db.get_unused_content = mock_get_unused

        # Create orchestrator
        with patch("orchestrator.ExtendedDatabase", return_value=db):
            with patch("orchestrator.init_db"):
                orch = PipelineOrchestrator(canal="canal2", db_video_id=1)

        # Mock script_gen.generate to return controlled results
        script_gen = MagicMock()
        script_gen.generate.side_effect = script_results
        script_gen._get_word_target.return_value = {
            "words_min": 2356, "words_max": 3603, "duration_target": 14.0,
            "blocks_min": 10, "blocks_max": 30, "palabras_objetivo": 2772,
        }
        script_gen.set_stop_event = MagicMock()
        script_gen.set_progress_callback = MagicMock()
        orch._script_gen = script_gen
        orch._emit_progress = MagicMock()
        orch._extract_theme = MagicMock()

        return orch

    def test_accepts_script_above_threshold(self):
        """Script with enough content → accepted."""
        db = MockDB()
        orch = self._setup_orchestrator(
            db,
            content_items=[{"id": 1, "title": "Test", "text": "source", "score": 100}],
            script_results=[self._make_mock_script_result(4000)],
        )
        result = orch.phase_generate_script()
        assert result is not None
        assert result["id"] == 1

    def test_returns_none_when_all_fail(self):
        """All items fail → returns None."""
        db = MockDB()
        orch = self._setup_orchestrator(
            db,
            content_items=[
                {"id": 1, "title": "T1", "text": "a", "score": 100},
                {"id": 2, "title": "T2", "text": "b", "score": 90},
            ],
            script_results=[None, None],
        )
        result = orch.phase_generate_script()
        assert result is None

    def test_returns_none_when_no_content(self):
        """No unused content → None."""
        db = MockDB()
        orch = self._setup_orchestrator(
            db,
            content_items=[],
            script_results=[],
        )
        result = orch.phase_generate_script()
        assert result is None

    def test_exhausts_all_items_eventually(self):
        """Multiple items, all fail → exhausts and returns None."""
        db = MockDB()
        items = [{"id": i, "title": f"T{i}", "text": "x", "score": 100-i}
                 for i in range(1, 6)]
        orch = self._setup_orchestrator(
            db,
            content_items=items,
            script_results=[None] * 5,
        )
        result = orch.phase_generate_script()
        assert result is None


class TestDiskCleanup:
    """Cleanup runs ONCE in run_full_pipeline (pre-pipeline, lock-aware),
    never inside phase_media/phase_video where it could delete freshly
    downloaded assets before assembly uses them."""

    def test_cleanup_in_run_full_pipeline_not_in_phases(self):
        """video_clips/output/temp cleanup must live in run_full_pipeline and
        NOT in phase_media/phase_video."""
        import ast
        src = Path("/root/autotube/orchestrator.py").read_text()
        tree = ast.parse(src)

        run_cleanup_ok = False
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef):
                func_src = ast.get_source_segment(src, node)
                if node.name == "run_full_pipeline":
                    assert "video_clips" in func_src, \
                        "run_full_pipeline must clean video_clips dir"
                    assert "output/temp" in func_src, \
                        "run_full_pipeline must clean output/temp dir"
                    run_cleanup_ok = True
                elif node.name in ("phase_media", "phase_video"):
                    # Regression guard: neither phase may delete downloaded assets
                    assert "video_clips" not in func_src, \
                        f"{node.name} must NOT clean video_clips (deletes fresh assets)"

        assert run_cleanup_ok, "run_full_pipeline method not found in orchestrator.py"

    def test_phase_video_still_logs_disk_space(self):
        """phase_video must still log disk space before render."""
        import ast
        src = Path("/root/autotube/orchestrator.py").read_text()
        tree = ast.parse(src)

        found = False
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "phase_video":
                func_src = ast.get_source_segment(src, node)
                assert "shutil.disk_usage" in func_src, \
                    "phase_video must still log disk space"
                assert "Disk free before render" in func_src, \
                    "phase_video must log disk space"
                found = True
        assert found, "phase_video method not found in orchestrator.py"
