"""Tests del embudo de alcance (Reporting API reach reports, v51/v60).

Cubre:
  - parser de reach (impresiones + CTR fracción→porcentaje).
  - parser de basic (retención/watch/views/subs).
  - upsert idempotente en video_reach_daily y agregación del embudo.
  - control de reportes ya procesados.
"""
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _get_db(tmp_path):
    from database.db_extended import ExtendedDatabase, migrate_v2
    db_path = tmp_path / "reach.db"
    db = ExtendedDatabase(db_path)
    migrate_v2(str(db_path))
    return db


def test_parse_reach_normaliza_ctr(tmp_path):
    from pipeline.youtube_reach import ReachReportClient

    client = ReachReportClient("canal2")
    csv_text = (
        "date,channel_id,video_id,video_thumbnail_impressions,"
        "video_thumbnail_impressions_ctr\n"
        "2026-09-10,UC123,VID1,1200,0.045\n"
        "2026-09-11,UC123,VID1,800,0.02\n"
    )
    rows = client.parse_reach(csv_text)
    assert len(rows) == 2
    assert rows[0]["impressions"] == 1200
    assert abs(rows[0]["impressions_ctr"] - 4.5) < 0.001
    assert abs(rows[1]["impressions_ctr"] - 2.0) < 0.001


def test_parse_basic_extrae_retencion_y_watch(tmp_path):
    from pipeline.youtube_reach import ReachReportClient

    client = ReachReportClient("canal2")
    csv_text = (
        "date,channel_id,video_id,average_view_duration_percentage,"
        "watch_time_minutes,views,subscribers_gained\n"
        "2026-09-10,UC123,VID1,21.5,60,40,2\n"
    )
    rows = client.parse_basic(csv_text)
    assert len(rows) == 1
    assert rows[0]["retention_pct"] == 21.5
    assert rows[0]["watch_minutes"] == 60
    assert rows[0]["views"] == 40
    assert rows[0]["subs_gained"] == 2


def test_reach_upsert_idempotente_y_funnel(tmp_path):
    db = _get_db(tmp_path)
    today = datetime.now().strftime("%Y-%m-%d")

    # Pasada 1: impresiones + CTR (reach). Pasada 2: retención/watch (basic).
    db.upsert_video_reach_daily(3, "VID1", today, impressions=1000, impressions_ctr=4.0)
    db.upsert_video_reach_daily(
        3, "VID1", today, retention_pct=22.0, watch_minutes=60, views=40, subs_gained=2
    )
    # Re-upsert de impresiones no debe perder la retención ya escrita.
    db.upsert_video_reach_daily(3, "VID1", today, impressions=1000, impressions_ctr=4.0)

    funnel = db.get_channel_funnel(3, days=30)
    assert funnel["impressions"] == 1000
    assert funnel["clicks"] == 40
    assert funnel["views"] == 40
    assert funnel["watch_hours"] == 1.0
    assert funnel["subs_gained"] == 2
    assert funnel["ctr_pct"] == 4.0
    assert funnel["retention_pct"] == 22.0
    assert funnel["video_count"] == 1
    assert funnel["conversion"]["impression_to_click_pct"] == 4.0
    assert len(funnel["videos"]) == 1


def test_reach_report_seen(tmp_path):
    db = _get_db(tmp_path)
    assert db.reach_report_seen("rep-1") is False
    db.mark_reach_report_seen("rep-1", job_id="job-1", report_type_id="channel_reach_basic_a1")
    assert db.reach_report_seen("rep-1") is True
    # Idempotente
    db.mark_reach_report_seen("rep-1", job_id="job-1", report_type_id="channel_reach_basic_a1")


def test_service_disabled_detection():
    from pipeline.youtube_reach import service_disabled_error

    class _E(Exception):
        pass

    assert service_disabled_error(_E("SERVICE_DISABLED")) is True
    assert service_disabled_error(_E("API has not been used in project 123 before")) is True
    assert service_disabled_error(_E("404 Not Found")) is False


# ── Estado explícito del Reporting API (observabilidad) ─────────────────────


class _FakeDB:
    """DB mínima para ejercitar ReachReportClient.sync sin red."""

    def __init__(self):
        self.seen: set[str] = set()
        self.upserts: list[dict] = []

    def get_channel_by_slug(self, slug):
        return {"id": 3, "slug": slug}

    def reach_report_seen(self, report_id):
        return report_id in self.seen

    def mark_reach_report_seen(self, report_id, job_id="", report_type_id=""):
        self.seen.add(report_id)

    def upsert_video_reach_daily(self, channel_id, yt_video_id, date, **kwargs):
        self.upserts.append(
            {"channel_id": channel_id, "yt_video_id": yt_video_id, "date": date, **kwargs}
        )


def _client_with_fake_service(report_types=None):
    from pipeline.youtube_reach import ReachReportClient

    client = ReachReportClient("canal3")
    client._service = object()  # truthy: saltamos la construcción real del servicio
    client._report_types = report_types or {}
    return client


def test_sync_status_disabled():
    """API deshabilitada → status 'disabled' (nunca 'collected')."""
    client = _client_with_fake_service()
    client.api_disabled = True
    client.disabled_hint = "https://console.cloud.google.com/apis/library/youtubereporting.googleapis.com"
    client.ensure_jobs = lambda: {}

    summary = client.sync(_FakeDB())

    assert summary["status"] == "disabled"
    assert summary["api_disabled"] is True
    assert summary["disabled_hint"].startswith("https://console")


def test_sync_status_awaiting_reports():
    """Jobs creados pero sin informes todavía → 'awaiting_reports' (~48 h)."""
    from pipeline.youtube_reach import REPORT_TYPE_REACH

    client = _client_with_fake_service()
    client.ensure_jobs = lambda: {REPORT_TYPE_REACH: "job-1"}
    client.list_reports = lambda job_id, limit=200: []

    summary = client.sync(_FakeDB())

    assert summary["status"] == "awaiting_reports"
    assert summary["jobs"] == 1
    assert summary["reports_available"] == 0
    assert summary["reports_downloaded"] == 0


def test_sync_backfill_sin_tope_descarga_todos_los_informes():
    """max_reports_per_job=0 → backfill completo (>10 informes en una pasada)."""
    from pipeline.youtube_reach import REPORT_TYPE_REACH

    client = _client_with_fake_service()
    client.ensure_jobs = lambda: {REPORT_TYPE_REACH: "job-1"}

    def _reports(job_id, limit=200):
        return [
            {
                "id": f"rep-{i}",
                "downloadUrl": f"https://example.test/rep-{i}",
                "endTime": f"2026-09-{i:02d}T00:00:00Z",
            }
            for i in range(1, 16)  # 15 > 10 (tope antiguo)
        ]

    client.list_reports = _reports
    client.download_report = lambda url: (
        "date,channel_id,video_id,video_thumbnail_impressions,"
        "video_thumbnail_impressions_ctr\n"
        f"2026-09-10,UC123,VID1,100,0.05\n"
    )

    db = _FakeDB()
    summary = client.sync(db, max_reports_per_job=0)

    assert summary["status"] == "collected"
    assert summary["reports_downloaded"] == 15
    assert summary["reports_pending"] == 0
    assert summary["reach_rows"] == 15
    assert len(db.upserts) == 15


def test_seo_summary_marca_reach_pendiente(tmp_path):
    """Sin reach → has_reach_data False; con reach → reporting_api y conteo real."""
    db = _get_db(tmp_path)
    chans = [{"id": 3, "name": "Canal", "slug": "canal3"}]

    with db._connect() as conn:
        summary = db._build_seo_summary(conn, chans)
    assert summary[3]["has_reach_data"] is False
    assert summary[3]["reach_status"] == "pending"

    today = datetime.now().strftime("%Y-%m-%d")
    db.upsert_video_reach_daily(3, "VID1", today, impressions=500, impressions_ctr=4.0)

    with db._connect() as conn:
        summary = db._build_seo_summary(conn, chans)
    assert summary[3]["has_reach_data"] is True
    assert summary[3]["reach_status"] == "reporting_api"
    assert summary[3]["total_impressions_30d"] == 500
    assert summary[3]["impression_video_count"] == 1
    assert summary[3]["avg_ctr_30d"] == 4.0
