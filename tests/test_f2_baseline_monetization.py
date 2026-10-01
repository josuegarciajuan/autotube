"""Tests de F2 — baseline reconciliado, monetización y reproceso idempotente."""
from datetime import datetime

from database.db_extended import ExtendedDatabase, migrate_v2
from scripts.experiment_tracker import (
    BASELINE_KEY, RECONCILED_KEY, store_baseline, store_reconciled_baseline,
)
from api.services.monetization import (
    longform_public_hours_analytics, monetization_status, set_ypp_confirmed,
)
from pipeline.youtube_reach import ReachReportClient, REPORT_TYPE_REACH


def _db(tmp_path):
    path = tmp_path / "f2.db"
    db = ExtendedDatabase(path)
    migrate_v2(str(path))
    return db


def _seed_channel(db, subs=100):
    with db._connect() as conn:
        conn.execute("INSERT INTO channels(id, slug, name, active) VALUES (3,'canal2','Sincronías',1)")
        conn.execute(
            "INSERT INTO channel_stats_history"
            "(channel_id, subscribers, total_views, video_count, estimated_minutes_watched, fetched_at)"
            " VALUES (3, ?, 1000, 50, 600, datetime('now'))",
            (subs,),
        )
        conn.commit()


def test_reconciled_baseline_detects_anomaly_without_touching_original(tmp_path):
    db = _db(tmp_path)
    _seed_channel(db, subs=100)
    store_baseline(db)
    # Aparece una fila posterior con MENOS subs (dato corrupto, como canal3).
    with db._connect() as conn:
        conn.execute(
            "INSERT INTO channel_stats_history"
            "(channel_id, subscribers, total_views, video_count, estimated_minutes_watched, fetched_at)"
            " VALUES (3, 90, 1100, 51, 700, datetime('now','+1 minute'))"
        )
        conn.commit()

    rec = store_reconciled_baseline(db)
    assert any(n["type"] == "subscribers_below_baseline" for n in rec["reconciliation_notes"])
    # El baseline original NO se reescribe.
    import json
    original = json.loads(db.get_system_state(BASELINE_KEY))
    assert original["channels"][0]["subscribers"] == 100
    assert db.get_system_state(RECONCILED_KEY) is not None


def test_monetization_separates_analytics_from_ypp(tmp_path):
    db = _db(tmp_path)
    with db._connect() as conn:
        conn.execute("INSERT INTO channels(id, slug, name, active) "
                     "VALUES (3,'canal2','Sincronías',1)")
        conn.execute(
            "INSERT INTO videos(canal, video_path, yt_video_id, channel_id) "
            "VALUES ('canal2','', 'V1', 3)"
        )
        conn.commit()
    db.upsert_video_reach_daily(3, "V1", datetime.now().strftime("%Y-%m-%d"),
                                watch_minutes=600)  # 10 h
    hours = longform_public_hours_analytics(db)
    assert hours["by_channel_hours"]["canal2"] == 10.0

    set_ypp_confirmed(db, 500.0, note="Studio")
    status = monetization_status(db)
    assert status["ypp_confirmed"]["hours"] == 500.0
    assert status["ypp_confirmed"]["progress_pct"] == 12.5
    assert "NO equivalen" in status["note"]


def test_sync_ignore_seen_reprocesses():
    csv = (
        "date,channel_id,video_id,video_thumbnail_impressions,"
        "video_thumbnail_impressions_ctr\n"
        "20260910,UC1,VID1,100,0.05\n"
    )

    class DB:
        def __init__(self):
            self.upserts = []
            self.seen = {"rep1"}

        def get_channel_by_slug(self, slug):
            return {"id": 3}

        def reach_report_seen(self, report_id):
            return report_id in self.seen

        def mark_reach_report_seen(self, *a, **k):
            pass

        def upsert_video_reach_daily(self, *a, **k):
            self.upserts.append((a, k))

    def _client():
        c = ReachReportClient("canal2")
        c._service = object()  # bypass auth
        c.ensure_jobs = lambda: {REPORT_TYPE_REACH: "job1"}
        c.list_reports = lambda job_id, limit=200: [{"id": "rep1", "downloadUrl": "http://x"}]
        c.download_report = lambda url: csv
        return c

    db = DB()
    normal = _client().sync(db, max_reports_per_job=1, ignore_seen=False)
    assert normal["reach_rows"] == 0  # ya visto → no reprocesa

    forced = _client().sync(db, max_reports_per_job=1, ignore_seen=True)
    assert forced["reach_rows"] == 1  # reproceso histórico
