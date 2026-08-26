"""Regression tests for fail-closed Shorts rendering."""


def test_renderer_rejects_successful_ffmpeg_without_output(tmp_path, monkeypatch):
    from pipeline.shorts_renderer import ShortsRenderer

    source = tmp_path / "source.mp4"
    source.write_bytes(b"source")
    output = tmp_path / "missing.mp4"

    class Result:
        returncode = 0
        stderr = "encoder warning"

    monkeypatch.setattr("pipeline.shorts_renderer.subprocess.run", lambda *a, **kw: Result())
    renderer = ShortsRenderer()

    try:
        renderer._render_with_ffmpeg(source, output, 0, 1)
    except RuntimeError as exc:
        assert "no output file" in str(exc).lower()
    else:
        raise AssertionError("missing ffmpeg output must fail closed")
