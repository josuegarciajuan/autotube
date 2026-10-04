"""Smoke del worker de escena (ffmpeg puro, sin red ni DB)."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

WORKER = os.path.join(os.path.dirname(__file__), "..", "pipeline_dist", "scene_worker.py")


def _manifest(index=0, frames=30, fps=30.0, kind="test", path=""):
    return {
        "schema_version": 1,
        "scene_key": f"{index:04d}",
        "index": index,
        "frames": frames,
        "fps": fps,
        "width": 320,
        "height": 180,
        "duration": frames / fps,
        "asset": {"kind": kind, "path": path, "sha256": "", "trim_start": 0.0},
        "motion": {"zoom_start": 1.0, "zoom_end": 1.0, "focus": "center"},
        "color": {"contrast": 1.0, "brightness": 1.0, "saturation": 1.0},
        "encoding": {"codec": "libx264", "crf": 16, "preset": "veryfast", "pix_fmt": "yuv420p"},
    }


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg no disponible")
def test_worker_render_test_source_30_frames():
    tmp = tempfile.mkdtemp(prefix="atube-worker-")
    try:
        man = os.path.join(tmp, "manifest.json")
        with open(man, "w", encoding="utf-8") as fh:
            json.dump(_manifest(frames=30), fh)
        out = os.path.join(tmp, "out")
        rc = subprocess.run(
            [sys.executable, WORKER, "--manifest", man, "--out-dir", out],
            capture_output=True, text=True,
        )
        assert rc.returncode == 0, rc.stderr
        result = json.load(open(os.path.join(out, "result.json"), encoding="utf-8"))
        assert result["ok"] is True
        assert result["frames"] == 30
        assert os.path.getsize(os.path.join(out, "scene.mp4")) > 1024
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg no disponible")
def test_worker_image_con_zoom():
    tmp = tempfile.mkdtemp(prefix="atube-worker-img-")
    try:
        img = os.path.join(tmp, "img.jpg")
        subprocess.run(
            ["ffmpeg", "-y", "-v", "error", "-f", "lavfi",
             "-i", "testsrc=size=640x360:duration=0.1", "-frames:v", "1", img],
            check=True,
        )
        man = _manifest(frames=45, kind="image", path="img.jpg")
        man["motion"] = {"zoom_start": 1.0, "zoom_end": 1.15, "focus": "center"}
        man_path = os.path.join(tmp, "manifest.json")
        with open(man_path, "w", encoding="utf-8") as fh:
            json.dump(man, fh)
        out = os.path.join(tmp, "out")
        rc = subprocess.run(
            [sys.executable, WORKER, "--manifest", man_path, "--out-dir", out],
            capture_output=True, text=True,
        )
        assert rc.returncode == 0, rc.stderr
        result = json.load(open(os.path.join(out, "result.json"), encoding="utf-8"))
        assert result["ok"] is True and result["frames"] == 45
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
