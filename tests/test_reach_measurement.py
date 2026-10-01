"""Tests de F1 — medición fiable del embudo de alcance.

Cubre los dos bugs que invalidaban el diagnóstico:
  1. El basic report trae varios segmentos por vídeo/día; antes cada uno
     sobrescribía al anterior (horas y vistas perdidas).
  2. La fecha real del CSV es YYYYMMDD, pero el filtro del panel usa ISO; el
     "último 30 días" no aplicaba.
"""
from datetime import datetime, timedelta
from pathlib import Path

from pipeline.youtube_reach import ReachReportClient
from database.db_extended import ExtendedDatabase, migrate_v2, normalize_reach_date


def _db(tmp_path):
    db_path = tmp_path / "reach_measure.db"
    db = ExtendedDatabase(db_path)
    migrate_v2(str(db_path))
    return db


def test_normalize_reach_date():
    assert normalize_reach_date("20260910") == "2026-09-10"
    assert normalize_reach_date("2026-09-10") == "2026-09-10"
    assert normalize_reach_date(None) == ""


def test_parse_basic_agrega_segmentos():
    client = ReachReportClient("canal2")
    csv_text = (
        "date,channel_id,video_id,average_view_duration_percentage,"
        "watch_time_minutes,views,subscribers_gained,subscribers_lost,engaged_views\n"
        "20260910,UC1,VID1,20.0,60,40,2,1,10\n"
        "20260910,UC1,VID1,40.0,40,20,1,0,5\n"
    )
    rows = client.parse_basic(csv_text)
    assert len(rows) == 1  # agregado por vídeo/día
    r = rows[0]
    assert r["views"] == 60
    assert r["watch_minutes"] == 100
    assert r["subs_gained"] == 3
    assert r["subs_lost"] == 1
    assert r["engaged_views"] == 15
    # Retención ponderada por watch-time: (20*60 + 40*40) / 100 = 28.0
    assert abs(r["retention_pct"] - 28.0) < 0.01


def test_parse_traffic_agrega_fuentes():
    client = ReachReportClient("canal2")
    csv_text = (
        "date,channel_id,video_id,traffic_source_type,views,"
        "watch_time_minutes,average_view_duration_percentage\n"
        "20260910,UC1,VID1,YT_SEARCH,30,60,25.0\n"
        "20260910,UC1,VID1,RELATED_VIDEO,10,20,35.0\n"
    )
    rows = client.parse_traffic(csv_text)
    by_src = {r["traffic_source"]: r for r in rows}
    assert by_src["YT_SEARCH"]["views"] == 30
    assert by_src["RELATED_VIDEO"]["views"] == 10


def test_upsert_normaliza_fecha_y_funnel_filtra_ventana(tmp_path):
    db = _db(tmp_path)
    recent = (datetime.now() - timedelta(days=2)).strftime("%Y%m%d")
    old = (datetime.now() - timedelta(days=60)).strftime("%Y%m%d")

    # yt_video_id debe existir en videos para que el embudo lo cuente por formato;
    # aquí usamos el agregado global del canal (get_channel_funnel no filtra formato).
    db.upsert_video_reach_daily(3, "VID1", recent, impressions=1000, impressions_ctr=4.0)
    db.upsert_video_reach_daily(3, "VID2", old, impressions=9999, impressions_ctr=9.0)

    funnel = db.get_channel_funnel(3, days=30)
    assert funnel["impressions"] == 1000  # la fila de hace 60 días queda fuera
    assert funnel["ctr_pct"] == 4.0


def test_upsert_basic_no_pierde_retencion_por_segmentos(tmp_path):
    db = _db(tmp_path)
    today = datetime.now().strftime("%Y-%m-%d")
    db.upsert_video_reach_daily(
        3, "VID1", today, watch_minutes=100, views=60,
        retention_pct=28.0, subs_gained=3,
    )
    funnel = db.get_channel_funnel(3, days=30)
    assert funnel["views"] == 60
    assert funnel["watch_hours"] == round(100 / 60.0, 1)
    assert funnel["subs_gained"] == 3
    assert funnel["retention_pct"] == 28.0
