import json
from datetime import datetime, timezone, timedelta

from api.services.packaging_policy import validate_title, validate_thumbnail_overlay
from api.services.recovery_checkpoints import (
    CHECKPOINT_HOURS, classify_checkpoint, run_due_checkpoints,
)


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


class FakeDB:
    def __init__(self, video, stats):
        self.video = video
        self.stats = stats
        self.alerts = []

    def get_videos(self, **kwargs):
        return [self.video]

    def get_video_latest_stats(self, video_id):
        return self.stats

    def create_recovery_alert(self, **kwargs):
        if any(a["alert_type"] == kwargs["alert_type"] and a["entity_id"] == kwargs["entity_id"] for a in self.alerts):
            return None
        self.alerts.append(kwargs)
        return len(self.alerts)

    def has_recovery_checkpoint(self, video_id, checkpoint_hours):
        return False
