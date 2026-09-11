"""Tests for the validation_failed packaging recovery sweep."""

from api.services.packaging_policy import ValidationResult
import api.services.packaging_recovery as recovery


class FakeDB:
    def __init__(self, videos):
        self._videos = videos
        self.updated = []

    def get_videos(self, status=None, limit=None, **kwargs):
        return [dict(v) for v in self._videos if status is None or v.get("status") == status]

    def get_channel(self, channel_id):
        return {"id": channel_id, "slug": f"canal{channel_id}"}

    def update_video(self, video_id, **kwargs):
        self.updated.append((video_id, kwargs))
        return True


def _video(tmp_path, vid, valid):
    path = tmp_path / f"v{vid}.mp4"
    path.write_bytes(b"x" * 16)
    return {
        "id": vid,
        "channel_id": 2,
        "status": "validation_failed",
        "video_path": str(path),
        "titulo_final": f"titulo {vid}",
        "thumbnail_path": str(path),
        "thumbnail_text": "",
        "_valid": valid,
    }


def _patch(monkeypatch, videos):
    import api.services.upload_scheduler as scheduler
    import config.config_bridge as bridge

    def fake_validate(video, cfg):
        row = next(v for v in videos if v["titulo_final"] == video.get("titulo_final"))
        return ValidationResult(row["_valid"], () if row["_valid"] else ("specificity",))

    monkeypatch.setattr(scheduler, "validate_upload_packaging", fake_validate)
    monkeypatch.setattr(bridge, "get_channel_config", lambda slug: object())


def test_recovers_only_valid_videos(tmp_path, monkeypatch):
    good = _video(tmp_path, 1, valid=True)
    bad = _video(tmp_path, 2, valid=False)
    videos = [good, bad]
    _patch(monkeypatch, videos)

    db = FakeDB(videos)
    result = recovery.recover_packaging_held_videos(db=db)

    assert result["recovered"] == 1
    assert result["scanned"] == 2
    assert db.updated == [
        (1, {
            "status": "awaiting_upload",
            "progress": 5,
            "progress_phase": "upload",
            "scheduled_upload_at": None,
            "error_message": "Requeued by packaging recovery",
        })
    ]
    actions = {d["video_id"]: d["action"] for d in result["details"]}
    assert actions[1] == "requeued"
    assert actions[2] == "still_invalid"


def test_dry_run_does_not_write(tmp_path, monkeypatch):
    good = _video(tmp_path, 1, valid=True)
    _patch(monkeypatch, [good])

    db = FakeDB([good])
    result = recovery.recover_packaging_held_videos(db=db, dry_run=True)

    assert result["recovered"] == 1
    assert db.updated == []
    assert result["details"][0]["action"] == "would_requeue"


def test_skips_missing_file(tmp_path, monkeypatch):
    video = _video(tmp_path, 1, valid=True)
    video["video_path"] = str(tmp_path / "gone.mp4")
    _patch(monkeypatch, [video])

    db = FakeDB([video])
    result = recovery.recover_packaging_held_videos(db=db)

    assert result["recovered"] == 0
    assert result["details"][0]["action"] == "missing_file"
    assert db.updated == []
