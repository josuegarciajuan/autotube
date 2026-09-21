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
