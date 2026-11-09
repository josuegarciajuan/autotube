"""Severe duration outliers must never be uploaded as long-form."""
from __future__ import annotations

import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import pipeline.video_validator as vv


def _cfg(**kw):
    base = dict(
        TITLE_POWER_WORDS=[],
        VIDEO_AVERAGE_DURATION_MIN=17,
        VIDEO_DURATION_DISCREPANCY_MIN=2,
        TEST_MODE=False,
    )
    base.update(kw)
    return SimpleNamespace(**base)


def test_severe_short_long_form_is_blocked(tmp_path, monkeypatch):
    path = tmp_path / "video.mp4"
    path.write_bytes(b"fake")
    v = vv.VideoValidator(_cfg())
    monkeypatch.setattr(v, "_get_duration_ffprobe", lambda p: 25.0)
    monkeypatch.setattr(v, "_has_video_stream", lambda p: True)
    monkeypatch.setattr(v, "_has_audio_stream", lambda p: True)
    monkeypatch.setattr(
        vv, "evaluate_render_gate",
        lambda video_path, channel_config=None: {"blocking": [], "warnings": []},
    )

    res = v.post_validate(str(path), "Un titulo", "descripcion", ["tag"])
    assert res.passed is False
    assert any("hard floor" in e for e in res.blocking_errors)


def test_normal_duration_is_not_blocked_by_outlier_gate(tmp_path, monkeypatch):
    path = tmp_path / "video.mp4"
    path.write_bytes(b"fake")
    v = vv.VideoValidator(_cfg())
    monkeypatch.setattr(v, "_get_duration_ffprobe", lambda p: 17 * 60.0)
    monkeypatch.setattr(v, "_has_video_stream", lambda p: True)
    monkeypatch.setattr(v, "_has_audio_stream", lambda p: True)
    monkeypatch.setattr(
        vv, "evaluate_render_gate",
        lambda video_path, channel_config=None: {"blocking": [], "warnings": []},
    )

    res = v.post_validate(str(path), "Un titulo", "descripcion", ["tag"])
    assert not any("hard floor" in e for e in res.blocking_errors)
