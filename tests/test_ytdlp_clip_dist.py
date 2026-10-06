"""Tests de la descarga distribuida de clips (nodos residenciales `ytdlp`)."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from pipeline_dist import dsl_client, ytdlp_clip_dist


@pytest.fixture
def enabled(monkeypatch):
    monkeypatch.setenv("AUTOTUBE_DIST_YTDLP", "1")


def test_disabled_returns_none(monkeypatch, tmp_path):
    monkeypatch.delenv("AUTOTUBE_DIST_YTDLP", raising=False)
    assert ytdlp_clip_dist.download_clip_dist("https://y/v", "*1-2", tmp_path / "c.mp4") is None


def test_success_copies_clip(tmp_path, enabled, monkeypatch):
    art = tmp_path / "art"
    art.mkdir()
    (art / "vid.mp4").write_bytes(b"clip-bytes")
    monkeypatch.setattr(dsl_client, "submit", lambda *a, **k: "eid")
    monkeypatch.setattr(dsl_client, "wait", lambda *a, **k: {
        "status": "done",
        "result": {"clips": [{"key": "vid", "outDir": str(art), "filename": "vid.mp4",
                              "sha256": "h", "bytes": 10}]},
    })
    dst = tmp_path / "out" / "clip.mp4"
    res = ytdlp_clip_dist.download_clip_dist("https://y/v", "*12.0-25.0", dst, video_id="vid")
    assert res == dst and dst.read_bytes() == b"clip-bytes"


def test_failed_returns_none(tmp_path, enabled, monkeypatch):
    monkeypatch.setattr(dsl_client, "submit", lambda *a, **k: "eid")
    monkeypatch.setattr(dsl_client, "wait", lambda *a, **k: {"status": "failed", "error": "403"})
    assert ytdlp_clip_dist.download_clip_dist("https://y/v", "*1-2", tmp_path / "c.mp4") is None
