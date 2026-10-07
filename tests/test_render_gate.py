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


def _dark_branding(path):
    """6s de fondo oscuro tipo tarjeta de marca (RGB 0x141414, YAVG≈20)."""
    _run(["ffmpeg", "-y", "-v", "error",
          "-f", "lavfi", "-i", "color=c=0x141414:s=320x180:r=24:d=6",
          "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100:duration=6",
          "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(path)])


def _tail_black_with_visible_tail(path):
    """20s: 14s contenido + 3s negro (14-17) + 3s contenido (17-20).

    Simula el CTA/outro negro seguido de un end-card visible: el negro NO toca
    el EOF, así que la exención por ventana debe cubrirlo.
    """
    _run(["ffmpeg", "-y", "-v", "error",
          "-f", "lavfi", "-i", "testsrc=size=320x180:rate=24:duration=14",
          "-f", "lavfi", "-i", "color=c=black:s=320x180:r=24:d=3",
          "-f", "lavfi", "-i", "testsrc=size=320x180:rate=24:duration=3",
          "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100:duration=20",
          "-filter_complex", "[0:v][1:v][2:v]concat=n=3:v=1:a=0[v]",
          "-map", "[v]", "-map", "3:a", "-c:v", "libx264", "-pix_fmt", "yuv420p",
          "-c:a", "aac", "-shortest", str(path)])


def _mid_black(path):
    """20s: 10s contenido + 3s negro (10-13) + 7s contenido (fuera de ventana)."""
    _run(["ffmpeg", "-y", "-v", "error",
          "-f", "lavfi", "-i", "testsrc=size=320x180:rate=24:duration=10",
          "-f", "lavfi", "-i", "color=c=black:s=320x180:r=24:d=3",
          "-f", "lavfi", "-i", "testsrc=size=320x180:rate=24:duration=7",
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


def test_dark_branding_passes_with_low_pix_th(tmp_path, cfg):
    """Un fondo oscuro de marca (YAVG≈20) NO cuenta como negro con pix_th=0.03.

    Regresión: con pix_th=0.10 las tarjetas de marca se clasificaban enteras
    como negro y bloqueaban el vídeo.
    """
    v = tmp_path / "dark_branding.mp4"
    _dark_branding(v)
    res = evaluate_render_gate(str(v), channel_config=cfg)
    assert res["passed"] is True, res["blocking"]
    assert res["black_sec"] == 0.0


def test_branding_window_ignores_black_before_visible_tail(tmp_path):
    """Negro del CTA/outro con un end-card visible después NO bloquea.

    El tramo negro (14-17s) no toca el EOF; la ventana de marca lo exime.
    """
    v = tmp_path / "tail_black.mp4"
    _tail_black_with_visible_tail(v)
    cfg = SimpleNamespace(
        RENDER_GATE_ENABLED=True,
        RENDER_GATE_BLACK_MIN_SEC=1.0, RENDER_GATE_MAX_BLACK_SEC=2.0,
        RENDER_GATE_EDGE_IGNORE_SEC=5.0, RENDER_GATE_EDGE_MAX_FRACTION=0.15,
    )
    res = evaluate_render_gate(str(v), channel_config=cfg)
    assert res["passed"] is True, res["blocking"]
    assert res["black_sec"] == 0.0
    assert res["black_ignored_segments"] == 1


def test_mid_body_black_outside_window_blocks(tmp_path):
    """Un tramo negro fuera de la ventana de marca sigue bloqueando."""
    v = tmp_path / "mid_black.mp4"
    _mid_black(v)
    cfg = SimpleNamespace(
        RENDER_GATE_ENABLED=True,
        RENDER_GATE_BLACK_MIN_SEC=1.0, RENDER_GATE_MAX_BLACK_SEC=2.0,
        RENDER_GATE_EDGE_IGNORE_SEC=5.0, RENDER_GATE_EDGE_MAX_FRACTION=0.15,
    )
    res = evaluate_render_gate(str(v), channel_config=cfg)
    assert res["passed"] is False
    assert any("negro" in b for b in res["blocking"])
