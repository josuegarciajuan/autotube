"""Tests del worker de concat distribuido (identidad ffmpeg)."""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

WORKER = Path(__file__).resolve().parent.parent / "pipeline_dist" / "concat_worker.py"


def _run(cmd):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=120)


def _seg(path, color, dur=1):
    _run(["ffmpeg", "-y", "-v", "error",
          "-f", "lavfi", "-i", f"color=c={color}:s=160x90:r=24:d={dur}",
          "-c:v", "libx264", "-pix_fmt", "yuv420p", str(path)])


def _sha(p):
    h = hashlib.sha256()
    h.update(Path(p).read_bytes())
    return h.hexdigest()


@pytest.fixture()
def segs(tmp_path):
    a, b = tmp_path / "seg_0000.mp4", tmp_path / "seg_0001.mp4"
    _seg(a, "red")
    _seg(b, "blue")
    return [a, b]


def test_worker_concat_ok_and_identity(tmp_path, segs):
    filt = "[0:v][1:v]concat=n=2:v=1:a=0[concat]"
    spec = {
        "inputs": [s.name for s in segs],
        "filter_complex": filt,
        "map_label": "[concat]",
        "codec": "libx264", "preset": "fast", "bitrate": "6000k",
        "pix_fmt": "yuv420p", "threads": 4, "output": "batch_0000.mp4", "timeout": 120,
    }
    spec_path = tmp_path / "spec.json"
    spec_path.write_text(json.dumps(spec), encoding="utf-8")
    out_dir = tmp_path / "out"
    r = _run([sys.executable, str(WORKER), "--spec", str(spec_path),
              "--out-dir", str(out_dir)])
    assert r.returncode == 0, r.stderr
    result = json.loads((out_dir / "result.json").read_text())
    assert result["ok"] is True
    assert (out_dir / "batch_0000.mp4").exists()

    # Identidad: el worker debe producir EXACTAMENTE el mismo comando que local.
    ref = tmp_path / "ref.mp4"
    rr = _run(["ffmpeg", "-y", "-v", "error", "-i", str(segs[0]), "-i", str(segs[1]),
               "-filter_complex", filt, "-map", "[concat]",
               "-c:v", "libx264", "-preset", "fast", "-b:v", "6000k", "-threads", "4",
               "-pix_fmt", "yuv420p", "-an", "-movflags", "+faststart", str(ref)])
    assert rr.returncode == 0, rr.stderr
    assert result["sha256"] == _sha(ref)
