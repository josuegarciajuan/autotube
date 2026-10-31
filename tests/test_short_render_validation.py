"""Regression tests for the shorts render validation gate (oct 2026).

Incident: four 1080x1920 renders had a 991-1657 s video stream while their
audio lasted 36-42 s. They entered the queue with `shorts.duration` NULL and
could be uploaded as "shorts". These tests pin the ffprobe gate that rejects
such artifacts before they are queued or uploaded.
"""

import pytest

from pipeline import shorts_media as sm


def _meta(**overrides):
    base = {
        "path": "/tmp/short.mp4",
        "exists": True,
        "bytes": 1024,
        "has_video": True,
        "has_audio": True,
        "width": 1080,
        "height": 1920,
        "video_duration": 42.0,
        "audio_duration": 42.0,
        "container_duration": 42.0,
    }
    base.update(overrides)
    return base


def _fake_probe(monkeypatch, meta):
    monkeypatch.setattr(sm, "_probe_short_media", lambda path: dict(meta))


def test_valid_short_is_accepted_and_reports_duration(tmp_path, monkeypatch):
    path = tmp_path / "short.mp4"
    path.write_bytes(b"x")
    _fake_probe(monkeypatch, _meta(path=str(path)))

    meta = sm.validate_short_render(path, max_duration_sec=58.0)

    assert meta["width"] == 1080
    assert meta["height"] == 1920
    assert meta["duration_sec"] == 42.0


def test_video_longer_than_audio_is_rejected(tmp_path, monkeypatch):
    path = tmp_path / "short.mp4"
    path.write_bytes(b"x")
    _fake_probe(monkeypatch, _meta(
        path=str(path), video_duration=991.0, audio_duration=42.0,
        container_duration=991.0,
    ))

    with pytest.raises(sm.ShortRenderValidationError) as exc:
        sm.validate_short_render(path, max_duration_sec=58.0)

    assert "exceeds" in exc.value.reason
    assert "991.0s" in exc.value.reason
    assert "42.0s" in exc.value.reason


def test_video_audio_mismatch_under_budget_is_rejected(tmp_path, monkeypatch):
    path = tmp_path / "short.mp4"
    path.write_bytes(b"x")
    _fake_probe(monkeypatch, _meta(
        path=str(path), video_duration=52.0, audio_duration=40.0,
        container_duration=52.0,
    ))

    with pytest.raises(sm.ShortRenderValidationError) as exc:
        sm.validate_short_render(path, max_duration_sec=58.0)

    assert "mismatch" in exc.value.reason


def test_horizontal_render_is_rejected(tmp_path, monkeypatch):
    path = tmp_path / "short.mp4"
    path.write_bytes(b"x")
    _fake_probe(monkeypatch, _meta(
        path=str(path), width=1920, height=1080,
    ))

    with pytest.raises(sm.ShortRenderValidationError) as exc:
        sm.validate_short_render(path, max_duration_sec=58.0)

    assert "resolution 1920x1080" in exc.value.reason


def test_missing_audio_stream_is_rejected(tmp_path, monkeypatch):
    path = tmp_path / "short.mp4"
    path.write_bytes(b"x")
    _fake_probe(monkeypatch, _meta(path=str(path), has_audio=False, audio_duration=None))

    with pytest.raises(sm.ShortRenderValidationError) as exc:
        sm.validate_short_render(path, max_duration_sec=58.0)

    assert "no audio stream" in exc.value.reason


def test_missing_or_empty_file_is_rejected(tmp_path):
    with pytest.raises(sm.ShortRenderValidationError):
        sm.validate_short_render(tmp_path / "nope.mp4", max_duration_sec=58.0)

    empty = tmp_path / "empty.mp4"
    empty.write_bytes(b"")
    with pytest.raises(sm.ShortRenderValidationError):
        sm.validate_short_render(empty, max_duration_sec=58.0)


def test_channel_budget_is_honoured(tmp_path, monkeypatch):
    path = tmp_path / "short.mp4"
    path.write_bytes(b"x")
    _fake_probe(monkeypatch, _meta(
        path=str(path), video_duration=70.0, audio_duration=70.0,
        container_duration=70.0,
    ))

    # Under the default 58 s budget it is rejected...
    with pytest.raises(sm.ShortRenderValidationError):
        sm.validate_short_render(path, max_duration_sec=58.0)

    # ...but a channel with a 90 s budget accepts it.
    meta = sm.validate_short_render(path, max_duration_sec=90.0)
    assert meta["duration_sec"] == 70.0


def test_sanitize_scene_ranges_keeps_coherent_ranges():
    ranges = [{"duration": 10.0}, {"duration": 12.0}, {"duration": 8.0}]
    assert sm.sanitize_short_scene_ranges(
        ranges, 30.0, max_duration_sec=58.0,
    ) == ranges


def test_sanitize_scene_ranges_drops_unit_mismatch():
    # 6 blocks whose "durations" sum to 991 s for 42 s of narration.
    ranges = [{"duration": 991.0}, {"duration": 900.0}]
    assert sm.sanitize_short_scene_ranges(
        ranges, 42.0, max_duration_sec=58.0,
    ) is None


def test_render_hybrid_rejects_absurd_scene_ranges_before_ffmpeg(tmp_path, monkeypatch):
    image = tmp_path / "img.jpg"
    image.write_bytes(b"img")
    audio = tmp_path / "audio.mp3"
    audio.write_bytes(b"audio")
    out = tmp_path / "out.mp4"

    def _must_not_run(*args, **kwargs):
        raise AssertionError("ffmpeg must not run for an absurd timeline")

    monkeypatch.setattr(sm.subprocess, "run", _must_not_run)

    with pytest.raises(sm.ShortRenderValidationError) as exc:
        sm.render_short_hybrid(
            asset_items=[{"type": "image", "path": str(image)}],
            audio_path=audio,
            output_path=out,
            audio_duration=42.0,
            scene_ranges=[{"duration": 991.0}],
            max_duration_sec=58.0,
        )

    assert "planned timeline" in exc.value.reason


class _FakeFfmpegResult:
    returncode = 0
    stderr = ""


def test_render_hybrid_fails_closed_when_probe_is_long(tmp_path, monkeypatch):
    image = tmp_path / "img.jpg"
    image.write_bytes(b"img")
    audio = tmp_path / "audio.mp3"
    audio.write_bytes(b"audio")
    out = tmp_path / "out.mp4"

    def _fake_run(cmd, **kwargs):
        # Simulate ffmpeg writing the output file.
        out.write_bytes(b"rendered")
        return _FakeFfmpegResult()

    monkeypatch.setattr(sm.subprocess, "run", _fake_run)
    _fake_probe(monkeypatch, _meta(
        path=str(out), video_duration=991.0, audio_duration=42.0,
        container_duration=991.0,
    ))

    with pytest.raises(sm.ShortRenderValidationError):
        sm.render_short_hybrid(
            asset_items=[{"type": "image", "path": str(image)}],
            audio_path=audio,
            output_path=out,
            audio_duration=42.0,
            scene_ranges=[{"duration": 42.0}],
            max_duration_sec=58.0,
        )
