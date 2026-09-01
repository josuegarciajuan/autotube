"""Tests for narrative-phase taxonomy wiring in the script generator."""

import sys
from pathlib import Path

import pytest
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.script_generator import ScriptGenerator


class _CfgWithPhases:
    CANAL_NAME = "canalX"
    SCRIPT_STRUCTURE = [
        {"id": "gancho", "step": "EL GANCHO",
         "scene_pacing": {"image_target_sec": 5.0, "video_target_sec": 4.5}},
        {"id": "desarrollo", "step": "EL SUCESO",
         "scene_pacing": {"image_target_sec": 6.0, "video_target_sec": 6.0}},
        {"id": "cierre", "step": "EL CIERRE",
         "scene_pacing": {"image_target_sec": 7.0, "video_target_sec": 6.0}},
    ]


class _CfgNoPhases:
    CANAL_NAME = "canalX"
    SCRIPT_STRUCTURE = []


def _sg(cfg):
    sg = ScriptGenerator(MagicMock(), cfg)
    return sg


def test_phase_taxonomy_reads_config_ids():
    sg = _sg(_CfgWithPhases())
    tax = sg._phase_taxonomy()
    assert [p["id"] for p in tax] == ["gancho", "desarrollo", "cierre"]


def test_phase_taxonomy_empty_when_no_structure():
    sg = _sg(_CfgNoPhases())
    assert sg._phase_taxonomy() == []


def test_build_phase_block_lists_ids_and_pacing():
    sg = _sg(_CfgWithPhases())
    block = sg._build_phase_block()
    assert "gancho" in block
    assert "EL GANCHO" in block
    assert "ritmo imagen" in block
    assert "desarrollo" in block


def test_build_phase_block_empty_without_structure():
    sg = _sg(_CfgNoPhases())
    assert sg._build_phase_block() == ""


def test_phase_taxonomy_ignores_entries_without_id():
    cfg = MagicMock()
    cfg.CANAL_NAME = "canalX"
    cfg.SCRIPT_STRUCTURE = [
        {"step": "SIN ID"},  # no id → ignored
        {"id": "gancho", "step": "G"},
    ]
    sg = _sg(cfg)
    assert [p["id"] for p in sg._phase_taxonomy()] == ["gancho"]
