import json
import sqlite3
from datetime import datetime, timezone, timedelta

from api.services.packaging_policy import validate_title, validate_thumbnail_overlay, validate_video_packaging
from api.services.recovery_checkpoints import (
    CHECKPOINT_HOURS, classify_checkpoint, run_due_checkpoints, should_run_checkpoint_review,
)
from api.services.upload_scheduler import validate_upload_packaging


def test_title_requires_specific_case_and_fact_framing():
    cfg = type("Cfg", (), {
        "TITLE_MAX_CHARS": 65,
        "TITLE_MIN_CHARS": 28,
        "TITLE_REQUIRED_SPECIFICITY": ["year", "place_or_person"],
        "TITLE_BANNED_PATTERNS": ["nadie puede explicar", "te dejará sin palabras"],
    })
    assert validate_title("El caso de 1971 en Madrid: qué ocurrió", cfg).valid
    result = validate_title("La sincronía más increíble de la historia", cfg)
    assert not result.valid
    assert "specificity" in result.reasons


def test_thumbnail_overlay_rejects_repetitive_shock_badges():
    result = validate_thumbnail_overlay("OCULTO | REAL | PROHIBIDO", max_chars=42)
    assert not result.valid
    assert "repetitive_claims" in result.reasons


def test_checkpoint_is_due_and_idempotent_without_quota():
    now = datetime(2026, 8, 31, 12, tzinfo=timezone.utc)
    published = now - timedelta(hours=48)
    db = FakeDB(video={"id": 7, "channel_id": 2, "canal": "canal2",
                       "titulo_final": "El caso de 1971 en Madrid",
                       "published_at": published.isoformat(), "status": "published"},
                stats=None)
    first = run_due_checkpoints(db, now=now)
    assert first == 1
    alert = db.alerts[0]
    assert alert["alert_type"] == "recovery_checkpoint_48h"
    assert alert["metadata"]["classification"] == "metrics_unavailable"
    assert alert["metadata"]["next_checkpoint_hours"] == 96
    assert run_due_checkpoints(db, now=now) == 0


def test_checkpoint_classification_uses_available_metrics():
    assert classify_checkpoint({"impressions": 1000, "ctr": 0.01,
                                "average_view_percentage": 0.5}) == "low_ctr"
    assert classify_checkpoint({"impressions": 1000, "ctr": 0.06,
                                "average_view_percentage": 0.12}) == "early_retention_drop"


def test_malformed_metrics_are_unavailable_and_exact_thresholds_are_healthy():
    assert classify_checkpoint({"impressions": "nan", "ctr": 0.02,
                                "averageViewPercentage": "oops"}) == "metrics_unavailable"
    assert classify_checkpoint({"impressions": 100, "ctr": 2.0,
                                "averageViewPercentage": 20.0}) == "diagnostic_ok"


def test_recovery_is_scoped_by_channel_config():
    now = datetime(2026, 8, 31, 12, tzinfo=timezone.utc)
    video = {"id": 8, "channel_id": 3, "canal": "canal3",
             "published_at": (now - timedelta(hours=48)).isoformat()}
    db = FakeDB(video=video, stats={"impressions": 100, "ctr": 3.0}, enabled=False)
    assert run_due_checkpoints(db, now=now) == 0


def test_final_packaging_gate_rejects_missing_thumbnail(tmp_path):
    cfg = type("Cfg", (), {"TITLE_MAX_CHARS": 65, "TITLE_MIN_CHARS": 1,
                            "TITLE_REQUIRED_SPECIFICITY": [],
                            "TITLE_BANNED_PATTERNS": []})
    result = validate_video_packaging({"titulo_final": "Caso concreto",
                                       "thumbnail_path": str(tmp_path / "gone.jpg")}, cfg)
    assert not result.valid
    assert "invalid_image" in result.reasons


def test_upload_choke_point_fails_closed_for_invalid_packaging(tmp_path):
    cfg = type("Cfg", (), {"TITLE_MAX_CHARS": 65, "TITLE_MIN_CHARS": 1,
                            "TITLE_REQUIRED_SPECIFICITY": [], "TITLE_BANNED_PATTERNS": []})
    assert not validate_upload_packaging(
        {"titulo_final": "Caso concreto", "thumbnail_path": str(tmp_path / "missing.jpg")}, cfg
    ).valid


def test_v49_migration_smoke_is_idempotent(tmp_path):
    from database.db import init_db
    from database.db_extended import ExtendedDatabase, migrate_v2
    path = tmp_path / "fresh.db"
    init_db(str(path))
    ExtendedDatabase(str(path))
    migrate_v2(str(path))
    migrate_v2(str(path))
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT name FROM sqlite_master WHERE name='recovery_checkpoints'").fetchone()


def test_all_standard_checkpoints_are_scheduled_once():
    now = datetime(2026, 8, 31, 12, tzinfo=timezone.utc)
    db = FakeDB(video={"id": 9, "channel_id": 2, "canal": "canal2",
                       "published_at": (now - timedelta(hours=400)).isoformat()},
                stats={"impressions": 200, "ctr": 3.0})
    assert run_due_checkpoints(db, now=now) == 4
    assert {a["metadata"]["checkpoint_hours"] for a in db.alerts} == {48, 96, 168, 336}


def test_checkpoint_review_obeys_manual_scheduler_pause():
    assert should_run_checkpoint_review(False)
    assert not should_run_checkpoint_review(True)


class FakeDB:
    def __init__(self, video, stats, enabled=True):
        self.video = video
        self.stats = stats
        self.enabled = enabled
        self.alerts = []

    def get_videos(self, **kwargs):
        return [self.video]

    def is_recovery_enabled(self, channel_id, slug):
        return self.enabled

    def get_video_latest_stats(self, video_id):
        return self.stats

    def create_recovery_alert(self, **kwargs):
        if any(a["alert_type"] == kwargs["alert_type"] and a["entity_id"] == kwargs["entity_id"] for a in self.alerts):
            return None
        self.alerts.append(kwargs)
        return len(self.alerts)

    def has_recovery_checkpoint(self, video_id, checkpoint_hours):
        return any(a["metadata"]["checkpoint_hours"] == checkpoint_hours for a in self.alerts)
