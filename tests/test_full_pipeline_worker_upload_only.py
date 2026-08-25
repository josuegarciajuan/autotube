from __future__ import annotations

import json
import logging
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


class _WorkerDB:
    def __init__(self, checkpoint, *, quota=False, admission=False):
        self.videos = {
            10: {
                "checkpoint_data": json.dumps(checkpoint),
                "progress_phase": "upload",
                "publish_mode": "immediate",
                "status": "awaiting_upload",
            }
        }
        self.jobs = {1: {"id": 1}}
        self.job_updates = []
        self.video_updates = []
        self.quota = quota
        self.admission = admission

    def get_channel(self, channel_id):
        return {"id": channel_id, "slug": "canal2", "name": "Test", "config_json": "{}"}

    def get_job(self, job_id):
        return self.jobs.get(job_id)

    def update_job(self, job_id, **kwargs):
        self.job_updates.append((job_id, kwargs))

    def get_video(self, video_id):
        return self.videos.get(video_id)

    def get_script(self, script_id):
        return {"id": script_id, "titulo": "Test", "guion": "test"}

    def update_video(self, video_id, **kwargs):
        self.video_updates.append((video_id, kwargs))
        self.videos.setdefault(video_id, {}).update(kwargs)

    def update_heartbeat(self, job_id):
        pass

    def unlock_media_files(self, job_id):
        pass

    def is_quota_exhausted_for_channel(self, canal):
        return self.quota

    def mark_video_uploaded(self, video_id, yt_video_id, yt_url, status):
        self.update_video(video_id, yt_video_id=yt_video_id, yt_url=yt_url, status=status)


class _WorkerOrchestrator:
    def __init__(self, *args, **kwargs):
        self._upload_admission_denied = False

    def phase_upload(self, *args, **kwargs):
        return "yt-test-id"

    def collect_timing_json(self):
        return "{}"

    def cleanup(self):
        pass


class _RetryableFailureOrchestrator(_WorkerOrchestrator):
    admission_denied = False

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._upload_admission_denied = self.admission_denied

    def phase_upload(self, *args, **kwargs):
        return None


def _checkpoint(video_path: Path):
    return {
        "script": {"id": 1, "titulo": "Test", "guion": "test"},
        "video": {"video_path": str(video_path)},
        "metadata": {"title": "Test", "description": "", "tags": []},
    }


@pytest.fixture
def worker_setup(monkeypatch, tmp_path):
    from api.services import full_pipeline_worker as worker

    video_path = tmp_path / "video.mp4"
    video_path.write_bytes(b"test")
    monkeypatch.setattr(worker, "_get_available_memory_mb", lambda: 4096)
    monkeypatch.setattr(worker, "_kill_orphaned_ffmpeg", lambda: None)
    monkeypatch.setattr(worker, "log_lifecycle", lambda *args, **kwargs: None)
    monkeypatch.setattr(worker, "log_phase_start", lambda *args, **kwargs: None)
    monkeypatch.setattr(worker, "log_phase_end", lambda *args, **kwargs: None)
    monkeypatch.setattr(worker, "log_phase_error", lambda *args, **kwargs: None)
    monkeypatch.setattr(worker, "_save_checkpoint", lambda *args, **kwargs: True)
    monkeypatch.setattr(worker, "_load_checkpoint", lambda video_id, db: (
        db.get_video(video_id)["checkpoint_data"] and json.loads(db.get_video(video_id)["checkpoint_data"]),
        "upload",
        worker._PHASE_INDEX["upload"],
    ))
    monkeypatch.setattr(
        "config.config_bridge.get_channel_config",
        lambda slug: SimpleNamespace(AUTO_MARK_ALTERED_CONTENT=False),
    )
    monkeypatch.setattr("orchestrator.PipelineOrchestrator", _WorkerOrchestrator)
    return worker, video_path


def test_upload_only_success_does_not_report_fake_failure(worker_setup):
    worker, video_path = worker_setup
    db = _WorkerDB(_checkpoint(video_path))
    # run_job imports ExtendedDatabase locally, so patch the imported module.
    import database.db_extended as db_extended
    original = db_extended.ExtendedDatabase
    db_extended.ExtendedDatabase = lambda: db
    try:
        assert worker.run_job(1, 1, 10, action="upload_only") is True
    finally:
        db_extended.ExtendedDatabase = original
    assert any(update.get("status") == "completed" for _, update in db.job_updates)


def test_upload_only_test_mode_does_not_raise_unbound_local(worker_setup):
    worker, video_path = worker_setup
    db = _WorkerDB(_checkpoint(video_path))
    import database.db_extended as db_extended
    original = db_extended.ExtendedDatabase
    db_extended.ExtendedDatabase = lambda: db
    try:
        assert worker.run_job(1, 1, 10, action="upload_only", test_mode=True) is True
    finally:
        db_extended.ExtendedDatabase = original


@pytest.mark.parametrize("quota,admission", [(True, False), (False, True)])
def test_upload_only_transient_failure_is_deferred_without_upload_loop(
    worker_setup, monkeypatch, quota, admission
):
    worker, video_path = worker_setup
    db = _WorkerDB(_checkpoint(video_path), quota=quota, admission=admission)
    _RetryableFailureOrchestrator.admission_denied = admission
    monkeypatch.setattr("orchestrator.PipelineOrchestrator", _RetryableFailureOrchestrator)
    import database.db_extended as db_extended
    original = db_extended.ExtendedDatabase
    db_extended.ExtendedDatabase = lambda: db
    try:
        assert worker.run_job(1, 1, 10, action="upload_only") is False
    finally:
        db_extended.ExtendedDatabase = original
    assert db.videos[10]["status"] == "awaiting_upload"
    assert db.videos[10]["scheduled_upload_at"]


def test_worker_cli_upload_only_test_mode_dispatches_expected_flags(monkeypatch):
    from api.services import full_pipeline_worker as worker

    captured = {}
    monkeypatch.setattr(worker, "_setup_worker_logging", lambda job_id: logging.getLogger("test-worker"))
    monkeypatch.setattr(worker, "run_job", lambda **kwargs: captured.update(kwargs) or True)
    monkeypatch.setattr(sys, "argv", [
        "full_pipeline_worker.py", "--job-id", "1", "--channel-id", "1",
        "--video-id", "10", "--action", "upload_only", "--test-mode",
    ])
    with pytest.raises(SystemExit) as exc_info:
        worker.main()

    assert exc_info.value.code == 0
    assert captured == {
        "job_id": 1,
        "channel_id": 1,
        "video_id": 10,
        "action": "upload_only",
        "test_mode": True,
        "upload": True,
        "source_mode": "original",
        "viral_candidate_id": None,
    }
