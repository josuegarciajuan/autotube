"""Regression tests for the 2026-10-06 alert-remediation workstreams.

Covers:
  * preflight cleanup preserving recently written clips (media/IA)
  * shorts word budget adopting prosody + float rate parsing
  * watchdog timeouts for previously unregistered loops
  * LLM JSON recovery of a truncated response
"""
from __future__ import annotations

import logging
import os
import time
from pathlib import Path
from types import SimpleNamespace

import pytest


# ── WS1: preflight cleanup preserves recent clips ────────────────────

def test_preflight_cleanup_preserves_recent_files(monkeypatch, tmp_path):
    from api.services import full_pipeline_worker as fpw

    clips = tmp_path / "output" / "video_clips"
    clips.mkdir(parents=True)
    old = clips / "old.mp4"
    new = clips / "new.mp4"
    old.write_bytes(b"x")
    new.write_bytes(b"x")
    old_ts = time.time() - 10 * 3600
    os.utime(old, (old_ts, old_ts))

    monkeypatch.setattr(fpw, "_PROJECT_ROOT", tmp_path)

    class _FakeDB:
        def get_locked_file_paths(self):
            return set()

        def get_error_video_media_paths(self, max_age_hours=48):
            return set()

    monkeypatch.setattr("database.db_extended.ExtendedDatabase", lambda: _FakeDB())

    fpw._preflight_cleanup(logging.getLogger("test"))

    assert new.exists(), "recent clip must be preserved (concurrent fetch)"
    assert not old.exists(), "stale clip should be deleted"


# ── WS3: shorts word budget / rate parsing ───────────────────────────

def test_parse_rate_pct_handles_multiplier_float():
    from pipeline.shorts_tts import parse_rate_pct

    assert parse_rate_pct("-18%") == pytest.approx(-18.0)
    assert parse_rate_pct("+5%") == pytest.approx(5.0)
    assert parse_rate_pct(0.80) == pytest.approx(-20.0)
    assert parse_rate_pct("0.9") == pytest.approx(-10.0, abs=0.01)
    assert parse_rate_pct(None) == 0.0


def test_word_budget_accounts_for_prosody_floor():
    from pipeline.shorts_tts import voice_aware_word_budget

    fast = voice_aware_word_budget(58, rate="+0%", block_count=6)
    slow = voice_aware_word_budget(
        58, rate="+0%", block_count=6,
        prosody_floor_pct=-30.0, avg_pause_ms=600,
    )
    assert slow < fast


def test_word_budget_capped_for_audio_safety():
    from pipeline.shorts_tts import MAX_WORD_COUNT, voice_aware_word_budget

    # Fast rate would compute > cap; the hard cap must win.
    assert voice_aware_word_budget(58, rate="+50%", block_count=1) == MAX_WORD_COUNT
    assert MAX_WORD_COUNT <= 95


def test_prosody_budget_params_reads_config():
    from pipeline.shorts_tts import prosody_budget_params

    cfg = SimpleNamespace(
        PROSODY_PROFILES={
            "neutro": {"rate": "+0%", "pause_after_ms": 250},
            "suspense": {"rate": "-18%", "pause_after_ms": 600},
        },
        TTS_STRATEGY={"rate_base": 0.80, "rate_climax": "-30%"},
    )
    floor, avg_pause = prosody_budget_params(cfg)
    assert floor == pytest.approx(-30.0)
    assert avg_pause == pytest.approx(425.0)


# ── WS6: watchdog registration ───────────────────────────────────────

def test_watchdog_registers_previously_unregistered_loops():
    from api.services.lifecycle_monitor import TASK_TIMEOUTS

    for name in (
        "retention_feedback", "media_retention", "generation_hold_refresh",
    ):
        assert name in TASK_TIMEOUTS, f"{name} must be registered"
    # editorial_reviews sleeps 3600s + work: the timeout needs real margin.
    assert TASK_TIMEOUTS["editorial_reviews"] > 3600


# ── WS5: LLM JSON recovery ───────────────────────────────────────────

class _FakeClient:
    def __init__(self, content: str):
        self._content = content
        self.chat = SimpleNamespace(
            completions=SimpleNamespace(create=self._create),
        )

    def _create(self, **kwargs):
        msg = SimpleNamespace(content=self._content, reasoning_content=None)
        choice = SimpleNamespace(finish_reason="length", message=msg)
        return SimpleNamespace(choices=[choice])


def test_llm_json_call_recovers_truncated_response():
    from config.llm_helpers import llm_json_call

    truncated = (
        '{"era": "1980", "key_motifs": ["a", "b"], '
        '"primary_subject": "unterminated'
    )
    result = llm_json_call(_FakeClient(truncated), max_retries=1)
    assert isinstance(result, dict)
    assert result.get("era") == "1980"
    assert result.get("key_motifs") == ["a", "b"]


def test_llm_json_call_does_not_mutate_caller_kwargs():
    from config.llm_helpers import llm_json_call

    kwargs = {"temperature": 0.5}
    client = _FakeClient('{"ok": true}')
    llm_json_call(client, max_retries=1, **kwargs)
    assert kwargs["temperature"] == 0.5
