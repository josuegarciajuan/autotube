from datetime import datetime, timezone


def test_publish_target_hour_is_legacy_fallback_for_optimal_slots(monkeypatch):
    from pipeline import publish_scheduler as scheduler

    monkeypatch.setattr(scheduler, "_pick_optimal_slot", lambda *args: None)
    result = scheduler.calculate_target_public_time(
        slug="canal2", target_hour=17, timezone_str="Europe/Madrid",
        jitter_min=0, publish_window_spread_min=0, warmup_min=0,
    )

    assert result["peak_hour_local"] == 17
    assert result["peak_source"] in {"config", "history"}


def test_publish_target_hour_wins_over_optimal_slot(monkeypatch):
    from pipeline import publish_scheduler as scheduler

    monkeypatch.setattr(
        scheduler, "_pick_optimal_slot",
        lambda *args: {"target_hour": 9, "slot_rank": 1, "confidence": 1},
    )
    result = scheduler.calculate_target_public_time(
        slug="canal2", target_hour=17, timezone_str="Europe/Madrid",
        jitter_min=0, publish_window_spread_min=0, warmup_min=0,
        db=object(), channel_id=3,
    )

    assert result["peak_hour_local"] == 17
    assert result["peak_source"] in {"config", "history"}


def test_publish_window_spread_is_reported_and_applied(monkeypatch):
    from pipeline import publish_scheduler as scheduler

    monkeypatch.setattr(scheduler.random, "randint", lambda low, high: 7)
    result = scheduler.calculate_target_public_time(
        slug="canal2", target_hour=17, timezone_str="Europe/Madrid",
        jitter_min=0, publish_window_spread_min=10, warmup_min=0,
    )

    assert result["jitter_applied"] == 7
    assert ":07:00+" in result["target_public_at_local"]


def test_account_daily_uploads_counts_scheduled_shorts(tmp_path):
    from api.services import spam_mitigation

    class DB:
        def get_channels(self, active_only=False):
            return [{"id": 1, "google_account": "acct"}]

        def _connect(self):
            import sqlite3
            conn = sqlite3.connect(":memory:")
            conn.row_factory = sqlite3.Row
            conn.executescript("""
                CREATE TABLE videos (channel_id INTEGER, status TEXT,
                    uploaded_at TEXT, published_at TEXT);
                CREATE TABLE shorts (channel_id INTEGER, status TEXT,
                    published_at TEXT);
                INSERT INTO shorts VALUES (1, 'scheduled', date('now'));
            """)
            return conn

    assert spam_mitigation.get_account_daily_uploads("acct", DB()) == 1
