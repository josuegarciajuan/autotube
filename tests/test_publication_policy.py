from datetime import datetime, timezone

import pytest
from api.services.publication_policy import upload_publication_kwargs, validate_upload_visibility


def test_scheduled_upload_is_private_and_has_future_publication():
    result = upload_publication_kwargs(
        publish_mode="scheduled", now=datetime(2030, 1, 1, tzinfo=timezone.utc), warmup_min=120
    )
    assert result["privacy"] == "private"
    assert result["publish_at"] > "2030-01-01T00:00:00"


def test_immediate_upload_retains_explicit_public_policy():
    assert upload_publication_kwargs(publish_mode="immediate") == {"privacy": "public"}


def test_scheduled_upload_without_publish_at_is_rejected():
    with pytest.raises(ValueError):
        validate_upload_visibility(publish_mode="scheduled", privacy="public", publish_at=None)


def test_video_manual_endpoints_reject_future_scheduled_publication(monkeypatch):
    from api.routers import videos
    from fastapi import HTTPException

    class DB:
        def get_video(self, _id):
            return {"id": 1, "channel_id": 1, "yt_video_id": "yt1",
                    "publish_mode": "scheduled", "target_public_at": "2099-01-01T12:00:00Z"}

    monkeypatch.setattr(videos, "get_db", lambda: DB())
    with pytest.raises(HTTPException) as exc:
        videos.publish_video_now(1)
    assert exc.value.status_code == 409

    with pytest.raises(HTTPException) as privacy_exc:
        videos.set_video_privacy(1, {"privacy_status": "public"})
    assert privacy_exc.value.status_code == 409


def test_legacy_cleanup_scripts_are_blocked_by_default():
    import subprocess
    for script in ("scripts/cleanup_phase2.py", "scripts/cleanup_upload_publish.py"):
        result = subprocess.run(["python3", script], capture_output=True, text=True)
        assert result.returncode == 0
        assert "DRY-RUN/BLOCKED" in result.stdout
