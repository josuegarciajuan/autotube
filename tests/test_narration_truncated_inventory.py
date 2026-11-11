"""The narration-integrity gate must keep unusable videos out of inventory.

The upload gate holds a render that is >2 s shorter than its narration.  The
coverage/repack planners must not count those videos as publishable inventory;
otherwise days look covered while the upload never happens (incident nov 2026).
"""

from pipeline.audio_utils import narration_truncated


def test_narration_truncated_detects_short_render(monkeypatch):
    import pipeline.audio_utils as au

    monkeypatch.setattr(
        au, "media_duration",
        lambda p: 100.0 if str(p).endswith(".mp4") else 103.0,
    )
    assert au.narration_truncated("x.mp4", "x.mp3") is True


def test_narration_truncated_allows_matching_durations(monkeypatch):
    import pipeline.audio_utils as au

    monkeypatch.setattr(
        au, "media_duration",
        lambda p: 100.0 if str(p).endswith(".mp4") else 101.0,
    )
    assert au.narration_truncated("x.mp4", "x.mp3") is False


def test_narration_truncated_fails_open(monkeypatch):
    import pipeline.audio_utils as au

    monkeypatch.setattr(au, "media_duration", lambda p: None)
    assert au.narration_truncated("x.mp4", "x.mp3") is False


def test_repack_excludes_truncated_awaiting(monkeypatch):
    import pipeline.publish_scheduler as ps

    class FakeDB:
        def _connect(self):
            class Conn:
                def execute(self, sql, params):
                    class Cur:
                        def fetchall(self):
                            return [
                                {"id": 1, "video_path": "a.mp4", "audio_path": "a.mp3"},
                                {"id": 2, "video_path": "b.mp4", "audio_path": "b.mp3"},
                            ]
                    return Cur()
                def __enter__(self):
                    return self
                def __exit__(self, *a):
                    return False
            return Conn()

    monkeypatch.setattr(
        "pipeline.audio_utils.narration_truncated",
        lambda v, a, **k: str(v).startswith("a"),
    )
    rows = [
        {"id": 1, "status": "awaiting_upload"},
        {"id": 2, "status": "awaiting_upload"},
        {"id": 3, "status": "uploaded_private"},
    ]
    kept = ps._exclude_truncated_awaiting(FakeDB(), rows, "canal2")
    assert [r["id"] for r in kept] == [2, 3]


def test_coverage_pending_excludes_truncated(monkeypatch):
    import api.services.publish_coverage as pc

    class FakeDB:
        def _connect(self):
            class Conn:
                def execute(self, sql, params):
                    class Cur:
                        def fetchall(self):
                            return [
                                {"id": 1, "status": "awaiting_upload",
                                 "video_path": "a.mp4", "audio_path": "a.mp3",
                                 "target_public_at": "2026-11-12T10:00:00+00:00",
                                 "yt_video_id": None, "scheduled_upload_at": None},
                                {"id": 2, "status": "awaiting_upload",
                                 "video_path": "b.mp4", "audio_path": "b.mp3",
                                 "target_public_at": "2026-11-12T12:00:00+00:00",
                                 "yt_video_id": None, "scheduled_upload_at": None},
                            ]
                    return Cur()
                def __enter__(self):
                    return self
                def __exit__(self, *a):
                    return False
            return Conn()

    monkeypatch.setattr(
        "pipeline.audio_utils.narration_truncated",
        lambda v, a, **k: str(v).startswith("a"),
    )
    pending = pc._channel_pending(FakeDB(), 4)
    assert [p["id"] for p in pending] == [2]
