"""Regression: Ken Burns zoompan must not multiply the scene duration.

Nov 2026 incident: ``render_short_hybrid`` fed ``-loop 1 -t <seg>`` (≈25 fps of
repeated frames) into ``zoompan d=frames``. Because ``d`` is applied per input
frame, a 4 s scene became ~100x longer (41 s timeline → 958 s video).

The fix feeds exactly ONE frame (``-framerate 1 -t 1``) so ``d=frames``
expands the animation only once.
"""


class _StopRender(Exception):
    pass


def test_image_input_feeds_single_frame_to_zoompan(tmp_path, monkeypatch):
    from pipeline import shorts_media

    img = tmp_path / "scene.jpg"
    img.write_bytes(b"not-a-real-jpeg")
    audio = tmp_path / "voice.mp3"
    audio.write_bytes(b"not-a-real-mp3")
    out = tmp_path / "out.mp4"

    captured = {}

    def fake_run(cmd, *args, **kwargs):
        captured["cmd"] = cmd
        raise _StopRender()

    monkeypatch.setattr(shorts_media.subprocess, "run", fake_run)

    try:
        shorts_media.render_short_hybrid(
            asset_items=[{"path": str(img), "type": "image"}],
            audio_path=audio,
            output_path=out,
            audio_duration=4.0,
            crossfade_dur=0.0,
            scene_ranges=[{"duration": 4.0}],
            max_duration_sec=58.0,
        )
    except _StopRender:
        pass
    else:  # pragma: no cover - the fake always raises
        raise AssertionError("render should have invoked ffmpeg")

    cmd = captured["cmd"]
    path_idx = cmd.index(str(img))

    # Single input frame: -loop 1 -framerate 1 -t 1 -i <image>
    assert cmd[path_idx - 7:path_idx - 1] == [
        "-loop", "1", "-framerate", "1", "-t", "1",
    ], cmd[path_idx - 8:path_idx + 1]

    # zoompan must animate 4s * 30fps = 120 output frames (not 120 per frame).
    assert "d=120:" in " ".join(cmd)


def test_video_input_still_limits_read_to_scene_duration(tmp_path, monkeypatch):
    from pipeline import shorts_media

    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"not-a-real-mp4")
    audio = tmp_path / "voice.mp3"
    audio.write_bytes(b"not-a-real-mp3")

    captured = {}

    def fake_run(cmd, *args, **kwargs):
        captured["cmd"] = cmd
        raise _StopRender()

    monkeypatch.setattr(shorts_media.subprocess, "run", fake_run)

    try:
        shorts_media.render_short_hybrid(
            asset_items=[{"path": str(clip), "type": "video"}],
            audio_path=audio,
            output_path=tmp_path / "out.mp4",
            audio_duration=4.0,
            crossfade_dur=0.0,
            scene_ranges=[{"duration": 4.0}],
            max_duration_sec=58.0,
        )
    except _StopRender:
        pass
    else:  # pragma: no cover - the fake always raises
        raise AssertionError("render should have invoked ffmpeg")

    cmd = captured["cmd"]
    path_idx = cmd.index(str(clip))
    assert cmd[path_idx - 3:path_idx - 1] == ["-t", "4.000"], cmd[path_idx - 4:path_idx + 1]
