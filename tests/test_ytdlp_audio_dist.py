"""Tests de la descarga distribuida de audio (nodos residenciales `ytdlp`).

Sin red: se mockea `pipeline_dist.dsl_client`. Se blinda la activación por flag
y el fail-open (cualquier fallo devuelve None para caer a la ruta local).
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from pipeline_dist import dsl_client, ytdlp_audio_dist


@pytest.fixture
def enabled(monkeypatch):
    monkeypatch.setenv("AUTOTUBE_DIST_YTDLP", "1")


def _fake_wait_done(out_dir: Path, filename: str = "vid.mp3"):
    def _wait(eid, timeout=None, **kw):
        return {
            "status": "done",
            "result": {"audios": [{"key": "vid", "outDir": str(out_dir),
                                   "filename": filename, "sha256": "abc", "bytes": 9}]},
        }
    return _wait


def test_disabled_returns_none_without_calling_engine(tmp_path, monkeypatch):
    monkeypatch.delenv("AUTOTUBE_DIST_YTDLP", raising=False)
    called = {"n": 0}
    monkeypatch.setattr(dsl_client, "submit", lambda *a, **k: called.__setitem__("n", called["n"] + 1) or "x")
    out = ytdlp_audio_dist.download_audio_dist("https://y/v", "vid", tmp_path / "vid.mp3")
    assert out is None and called["n"] == 0


def test_is_enabled_flag(monkeypatch):
    monkeypatch.setenv("AUTOTUBE_DIST_YTDLP", "true")
    assert ytdlp_audio_dist.is_enabled() is True
    monkeypatch.setenv("AUTOTUBE_DIST_YTDLP", "0")
    assert ytdlp_audio_dist.is_enabled() is False


def test_success_copies_artifact(tmp_path, enabled, monkeypatch):
    src_dir = tmp_path / "art"
    src_dir.mkdir()
    (src_dir / "vid.mp3").write_bytes(b"audio-bytes")
    monkeypatch.setattr(dsl_client, "submit", lambda *a, **k: "eid-1")
    monkeypatch.setattr(dsl_client, "wait", _fake_wait_done(src_dir))
    dst = tmp_path / "out" / "vid.mp3"
    result = ytdlp_audio_dist.download_audio_dist("https://y/v", "vid", dst)
    assert result == dst and dst.exists()
    assert dst.read_bytes() == b"audio-bytes"


def test_failed_status_returns_none(tmp_path, enabled, monkeypatch):
    monkeypatch.setattr(dsl_client, "submit", lambda *a, **k: "eid-2")
    monkeypatch.setattr(dsl_client, "wait",
                        lambda *a, **k: {"status": "failed", "error": "exit 127"})
    assert ytdlp_audio_dist.download_audio_dist("https://y/v", "vid", tmp_path / "a.mp3") is None


def test_no_audios_returns_none(tmp_path, enabled, monkeypatch):
    monkeypatch.setattr(dsl_client, "submit", lambda *a, **k: "eid-3")
    monkeypatch.setattr(dsl_client, "wait", lambda *a, **k: {"status": "done", "result": {"audios": []}})
    assert ytdlp_audio_dist.download_audio_dist("https://y/v", "vid", tmp_path / "a.mp3") is None


def test_missing_artifact_returns_none(tmp_path, enabled, monkeypatch):
    monkeypatch.setattr(dsl_client, "submit", lambda *a, **k: "eid-4")
    monkeypatch.setattr(dsl_client, "wait", _fake_wait_done(tmp_path / "nope"))
    assert ytdlp_audio_dist.download_audio_dist("https://y/v", "vid", tmp_path / "a.mp3") is None


def test_submit_exception_is_fail_open(tmp_path, enabled, monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("engine caído")
    monkeypatch.setattr(dsl_client, "submit", _boom)
    assert ytdlp_audio_dist.download_audio_dist("https://y/v", "vid", tmp_path / "a.mp3") is None
