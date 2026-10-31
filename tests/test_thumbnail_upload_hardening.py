"""Thumbnail upload hardening — never lose an uploaded video on thumb errors.

Covers:
  * non-HTTP failures from thumbnails().set() are contained (return False),
  * the egress agent reports an explicit thumbnail outcome and never claims
    an unverified image as applied.
"""

import pytest

from pipeline.youtube_uploader import YouTubeUploader
from egress_agent import youtube_api as egress_youtube


def test_set_thumbnail_contains_non_http_errors(tmp_path):
    thumb = tmp_path / "thumb.jpg"
    thumb.write_bytes(b"not-really-a-jpeg")

    class _Set:
        def execute(self):
            raise ValueError("media library exploded")

    class _Thumbs:
        def set(self, **kwargs):
            return _Set()

    class _Svc:
        def thumbnails(self):
            return _Thumbs()

    uploader = object.__new__(YouTubeUploader)
    uploader.channel_slug = "canal2"
    uploader.account_name = "default"
    uploader._raise_if_quota_exceeded = lambda *a, **k: None

    assert uploader._set_thumbnail(_Svc(), "vid123", thumb) is False


class _Req:
    def next_chunk(self):
        return (None, {"id": "vid123"})


class _InsertSvc:
    def videos(self):
        return self

    def insert(self, **kwargs):
        return object()


def _run_agent_upload(monkeypatch, tmp_path, meta, set_ok):
    video = tmp_path / "v.mp4"
    video.write_bytes(b"x")
    monkeypatch.setattr(egress_youtube, "_service", lambda cfg: _InsertSvc())
    monkeypatch.setattr(egress_youtube, "MediaFileUpload", lambda *a, **k: object())
    monkeypatch.setattr(egress_youtube, "_resumable", lambda req: {"id": "vid123"})
    monkeypatch.setattr(
        egress_youtube, "_set_thumbnail",
        lambda svc, vid, path: set_ok,
    )
    return egress_youtube.upload_video(object(), str(video), meta)


def test_egress_reports_unknown_when_api_accepts_but_unverified(monkeypatch, tmp_path):
    thumb = tmp_path / "thumb.jpg"
    thumb.write_bytes(b"x")
    res = _run_agent_upload(monkeypatch, tmp_path, {"thumbnail_path": str(thumb)}, True)
    assert res["ok"] is True
    assert res["thumbnail_applied"] is None


def test_egress_reports_false_when_thumbnail_set_fails(monkeypatch, tmp_path):
    thumb = tmp_path / "thumb.jpg"
    thumb.write_bytes(b"x")
    res = _run_agent_upload(monkeypatch, tmp_path, {"thumbnail_path": str(thumb)}, False)
    assert res["ok"] is True
    assert res["thumbnail_applied"] is False
    assert any(w.get("type") == "thumbnail" for w in res["warnings"])


def test_egress_reports_false_when_thumbnail_path_missing(monkeypatch, tmp_path):
    res = _run_agent_upload(
        monkeypatch, tmp_path, {"thumbnail_path": str(tmp_path / "nope.jpg")}, True,
    )
    assert res["ok"] is True
    assert res["thumbnail_applied"] is False
