"""Tests del render gate (anti-escenas negras/silenciosas)."""
from __future__ import annotations

import subprocess
from types import SimpleNamespace

import pytest

from pipeline.render_gate import evaluate_render_gate


def _run(cmd):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=120)


def _clean(path):
    _run(["ffmpeg", "-y", "-v", "error",
          "-f", "lavfi", "-i", "testsrc=size=320x180:rate=24:duration=6",
          "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100:duration=6",
          "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(path)])


def _black(path):
    _run(["ffmpeg", "-y", "-v", "error",
          "-f", "lavfi", "-i", "color=c=black:s=320x180:r=24:d=6",
          "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100:duration=6",
          "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(path)])


def _silent(path):
    _run(["ffmpeg", "-y", "-v", "error",
          "-f", "lavfi", "-i", "testsrc=size=320x180:rate=24:duration=6",
          "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono",
          "-t", "6", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(path)])


def _head_tail_black(path):
    """20s: 2s negro (intro) + 16s contenido + 2s negro (outro)."""
    _run(["ffmpeg", "-y", "-v", "error",
          "-f", "lavfi", "-i", "color=c=black:s=320x180:r=24:d=2",
          "-f", "lavfi", "-i", "testsrc=size=320x180:rate=24:duration=16",
          "-f", "lavfi", "-i", "color=c=black:s=320x180:r=24:d=2",
          "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100:duration=20",
          "-filter_complex", "[0:v][1:v][2:v]concat=n=3:v=1:a=0[v]",
          "-map", "[v]", "-map", "3:a", "-c:v", "libx264", "-pix_fmt", "yuv420p",
          "-c:a", "aac", "-shortest", str(path)])


def _body_black(path):
    """20s: 2s contenido + 8s negro EN MEDIO + 10s contenido (defecto real)."""
    _run(["ffmpeg", "-y", "-v", "error",
          "-f", "lavfi", "-i", "testsrc=size=320x180:rate=24:duration=2",
          "-f", "lavfi", "-i", "color=c=black:s=320x180:r=24:d=8",
          "-f", "lavfi", "-i", "testsrc=size=320x180:rate=24:duration=10",
          "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100:duration=20",
          "-filter_complex", "[0:v][1:v][2:v]concat=n=3:v=1:a=0[v]",
          "-map", "[v]", "-map", "3:a", "-c:v", "libx264", "-pix_fmt", "yuv420p",
          "-c:a", "aac", "-shortest", str(path)])


@pytest.fixture()
def cfg():
    # Umbrales pequeños para que 6s de negro/silencio bloqueen.
    return SimpleNamespace(
        RENDER_GATE_ENABLED=True,
        RENDER_GATE_BLACK_MIN_SEC=1.0, RENDER_GATE_MAX_BLACK_SEC=2.0,
        RENDER_GATE_SILENCE_MIN_SEC=1.0, RENDER_GATE_MAX_SILENCE_SEC=3.0,
    )


def test_clean_video_passes(tmp_path, cfg):
    v = tmp_path / "clean.mp4"
    _clean(v)
    res = evaluate_render_gate(str(v), channel_config=cfg)
    assert res["passed"] is True
    assert res["blocking"] == []


def test_black_video_blocks(tmp_path, cfg):
    v = tmp_path / "black.mp4"
    _black(v)
    res = evaluate_render_gate(str(v), channel_config=cfg)
    assert res["passed"] is False
    assert any("negro" in b for b in res["blocking"])
    assert res["black_sec"] > 2.0


def test_silent_video_blocks(tmp_path, cfg):
    v = tmp_path / "silent.mp4"
    _silent(v)
    res = evaluate_render_gate(str(v), channel_config=cfg)
    assert res["passed"] is False
    assert any("silencio" in b for b in res["blocking"])
    assert res["silence_sec"] > 3.0


def test_kill_switch_disables_gate(tmp_path):
    v = tmp_path / "black.mp4"
    _black(v)
    cfg = SimpleNamespace(RENDER_GATE_ENABLED=False)
    res = evaluate_render_gate(str(v), channel_config=cfg)
    assert res["enabled"] is False
    assert res["passed"] is True


def test_edge_black_intro_outro_ignored(tmp_path):
    """Las tarjetas de marca (negro corto en head/tail) NO bloquean."""
    v = tmp_path / "edges.mp4"
    _head_tail_black(v)
    cfg = SimpleNamespace(
        RENDER_GATE_ENABLED=True,
        RENDER_GATE_BLACK_MIN_SEC=1.0, RENDER_GATE_MAX_BLACK_SEC=2.0,
        RENDER_GATE_EDGE_IGNORE_SEC=5.0, RENDER_GATE_EDGE_MAX_FRACTION=0.15,
    )
    res = evaluate_render_gate(str(v), channel_config=cfg)
    assert res["passed"] is True, res["blocking"]
    assert res["black_sec"] == 0.0
    assert res["black_ignored_segments"] == 2
    assert res["black_ignored_sec"] > 3.0


def test_edge_exemption_does_not_mask_full_black(tmp_path, cfg):
    """Un vídeo íntegramente negro (domina la duración) sigue bloqueando."""
    v = tmp_path / "allblack.mp4"
    _black(v)
    res = evaluate_render_gate(str(v), channel_config=cfg)
    assert res["passed"] is False
    assert res["black_ignored_segments"] == 0


def test_body_black_still_blocks(tmp_path):
    """Un tramo negro real en medio del vídeo sigue bloqueando."""
    v = tmp_path / "body.mp4"
    _body_black(v)
    cfg = SimpleNamespace(
        RENDER_GATE_ENABLED=True,
        RENDER_GATE_BLACK_MIN_SEC=1.0, RENDER_GATE_MAX_BLACK_SEC=2.0,
        RENDER_GATE_EDGE_IGNORE_SEC=5.0, RENDER_GATE_EDGE_MAX_FRACTION=0.15,
    )
    res = evaluate_render_gate(str(v), channel_config=cfg)
    assert res["passed"] is False
    assert any("negro" in b for b in res["blocking"])
    assert res["black_ignored_segments"] == 0
