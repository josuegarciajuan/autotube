"""Thumbnail verification service — repair logic with the perceptual check.

Ensures the cycle:
  * marks verified when YouTube already serves our image,
  * re-uploads (and re-verifies) when it serves something else,
  * never touches the API when the served image can't be fetched (private).
"""

import asyncio
import contextlib

import pytest

from api.services import thumbnail_verification_service as tvs


class _Cursor:
    def __init__(self, rows):
        self._rows = rows

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)


class _Conn:
    def __init__(self, rows):
        self._rows = rows

    def execute(self, sql, params=()):
        # The variant lookup must return nothing (no A/B test).
        if "video_ab_tests" in sql:
            return _Cursor([])
        return _Cursor(self._rows)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeDB:
    def __init__(self, rows):
        self._rows = rows
        self.updates = []

    def _connect(self):
        return _Conn(self._rows)

    def update_video(self, video_id, **kwargs):
        self.updates.append((video_id, kwargs))


class _FakeUploader:
    def __init__(self, canal):
        self.canal = canal
        self.set_calls = []

    def authenticate(self):
        return True

    def _get_service(self):
        return object()

    def _set_thumbnail(self, service, vid, path):
        self.set_calls.append((vid, str(path)))
        return True

    def _verify_thumbnail_applied(self, vid, path, attempts=None, delay=None):
        return True


def _row(tmp_path, vid=1, canal="canal2", yt="abc"):
    thumb = tmp_path / "thumb.jpg"
    thumb.write_bytes(b"x")  # existence only
    return (vid, 1, canal, yt, str(thumb), "scheduled")


@contextlib.contextmanager
def _patched(monkeypatch, served, match_result, uploader_cls):
    import config.settings as settings
    monkeypatch.setattr(settings, "YT_REMEDIATION_MODE", False)
    monkeypatch.setattr(tvs, "track_quota_exceeded_for_cycle", lambda c: False)
    import pipeline.thumbnail_compare as tc
    monkeypatch.setattr(tc, "fetch_served_thumbnail", lambda vid, timeout=20: served)
    monkeypatch.setattr(tc, "thumbnails_match",
                        lambda *a, **k: match_result)
    import pipeline.youtube_uploader as yu
    monkeypatch.setattr(yu, "YouTubeUploader", uploader_cls)
    yield


def test_cycle_marks_verified_when_image_matches(tmp_path, monkeypatch):
    db = _FakeDB([_row(tmp_path)])
    with _patched(monkeypatch, served=object(), match_result=True, uploader_cls=_FakeUploader):
        asyncio.run(tvs.run_thumbnail_verification_cycle(db))
    assert db.updates and db.updates[0][1].get("thumbnail_verified") == 1


def test_cycle_reuploads_and_repairs_on_mismatch(tmp_path, monkeypatch):
    db = _FakeDB([_row(tmp_path)])

    class _U(_FakeUploader):
        instances = []

        def __init__(self, canal):
            super().__init__(canal)
            _U.instances.append(self)

    with _patched(monkeypatch, served=object(), match_result=False, uploader_cls=_U):
        asyncio.run(tvs.run_thumbnail_verification_cycle(db))

    assert _U.instances and _U.instances[0].set_calls  # re-uploaded
    assert db.updates and db.updates[0][1].get("thumbnail_verified") == 1


def test_cycle_skips_when_served_unavailable(tmp_path, monkeypatch):
    db = _FakeDB([_row(tmp_path)])
    with _patched(monkeypatch, served=None, match_result=True, uploader_cls=_FakeUploader):
        asyncio.run(tvs.run_thumbnail_verification_cycle(db))
    # Cannot fetch → no verification write, no re-upload.
    assert db.updates == []
