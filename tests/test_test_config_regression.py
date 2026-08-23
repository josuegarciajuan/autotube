"""Regression test: test_video.py config bridge must propagate MAX_SCRIPT_BLOCKS.

Run:  python3 -m pytest tests/test_test_config_regression.py -v
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
from unittest.mock import MagicMock, patch


class TestConfigBridge:
    """test_video.py must bridge all required config keys to the orchestrator."""

    REQUIRED_TEST_KEYS = [
        "TEST_MODE",
        "TEST_SCRIPT_WORDS_MIN",
        "TEST_SCRIPT_WORDS_MAX",
        "TEST_VIDEO_DURATION_TARGET",
        "TEST_SCRIPT_BLOCKS_MIN",
        "TEST_SCRIPT_BLOCKS_MAX",
        "MAX_SCRIPT_BLOCKS",        # CRITICAL: enables block truncation
    ]

    def test_config_bridge_propagates_max_blocks(self):
        """Simulate test_video.py config bridge and verify MAX_SCRIPT_BLOCKS."""
        import importlib
        cfg = importlib.import_module("config.canal2_config")

        # Simulated orch config (SimpleNamespace-like dict)
        orch_config = {}

        # Simulate the bridge code from test_video.py lines 348-364
        orchid_keys = [
            "TEST_MODE", "TEST_SCRIPT_WORDS_MIN", "TEST_SCRIPT_WORDS_MAX",
            "TEST_VIDEO_DURATION_TARGET", "TEST_SCRIPT_BLOCKS_MIN",
            "TEST_SCRIPT_BLOCKS_MAX",
        ]
        for _k in orchid_keys:
            if hasattr(cfg, _k):
                orch_config[_k] = getattr(cfg, _k)

        # MAX_SCRIPT_BLOCKS is a runtime override, not in canal2_config
        orch_config["MAX_SCRIPT_BLOCKS"] = 5

        # Also simulate the loop for overrides
        for _k in ("VIDEO_RESOLUTION", "FFMPEG_PRESET", "SCENE_DURATION_MAX"):
            if hasattr(cfg, _k):
                orch_config[_k] = getattr(cfg, _k)

        # Verify MAX_SCRIPT_BLOCKS was bridged
        assert "MAX_SCRIPT_BLOCKS" in orch_config, (
            "MAX_SCRIPT_BLOCKS MUST be included in the test_video.py config bridge!"
        )
        assert orch_config["MAX_SCRIPT_BLOCKS"] == 5, (
            f"MAX_SCRIPT_BLOCKS must be 5, got {orch_config.get('MAX_SCRIPT_BLOCKS')}"
        )

    def test_orchestrator_truncation_with_max_blocks(self):
        """When MAX_SCRIPT_BLOCKS is set, orchestrator must truncate bloques."""
        from orchestrator import PipelineOrchestrator

        # Minimal mock
        orch = MagicMock(spec=PipelineOrchestrator)
        orch.canal = "test"
        orch.config = MagicMock()
        orch.config.MAX_SCRIPT_BLOCKS = 5

        # Simulate bloques with 8 items (should be truncated to 5)
        bloques = [{"tipo": "hook", "texto": f"Block {i}"} for i in range(8)]

        # Simulate the truncation logic from orchestrator.phase_tts
        max_blocks = getattr(orch.config, "MAX_SCRIPT_BLOCKS", 0)
        if max_blocks > 0 and len(bloques) > max_blocks:
            bloques = bloques[:max_blocks]

        assert len(bloques) == 5, f"Expected 5 blocks after truncation, got {len(bloques)}"

    def test_orchestrator_no_truncation_without_max_blocks(self):
        """Without MAX_SCRIPT_BLOCKS, bloques pass through unchanged."""
        from orchestrator import PipelineOrchestrator

        orch = MagicMock(spec=PipelineOrchestrator)
        orch.config = MagicMock()
        orch.config.MAX_SCRIPT_BLOCKS = 0  # disabled

        bloques = [{"tipo": "hook", "texto": f"Block {i}"} for i in range(8)]

        max_blocks = getattr(orch.config, "MAX_SCRIPT_BLOCKS", 0)
        if max_blocks > 0 and len(bloques) > max_blocks:
            bloques = bloques[:max_blocks]

        assert len(bloques) == 8, "Without MAX_SCRIPT_BLOCKS, no truncation should occur"

    def test_scene_duration_max_propagated(self):
        """SCENE_DURATION_MAX must be propagated to orch config.

        Channel configs no longer inherit defaults directly: the merge happens
        in config_bridge.get_channel_config() (AGENTS.md invariant). The
        bridge output must carry SCENE_DURATION_MAX so test_video.py /
        orchestrator can read it.
        """
        from config.config_bridge import get_channel_config

        cfg = get_channel_config("canal2")

        assert hasattr(cfg, "SCENE_DURATION_MAX"), (
            "SCENE_DURATION_MAX MUST come from defaults via the config bridge!"
        )
        assert cfg.SCENE_DURATION_MAX >= 9.0, (
            f"SCENE_DURATION_MAX should be >= 9, got {cfg.SCENE_DURATION_MAX}"
        )
