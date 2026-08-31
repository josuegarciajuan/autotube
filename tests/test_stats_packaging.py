"""Tests de instrumentación de packaging (B1: ctr/impressions, ago 2026).

Verifica que:
  - insert_video_stats persiste ctr (como %) e impressions desde el dict.
  - la conversión fracción→porcentaje es correcta (0.05 → 5.0).
  - get_all_videos_analytics incluye las métricas nuevas.
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import sqlite3
import pytest


@pytest.fixture
def stats_db(tmp_path):
    """DB mínima con video_stats_history (misma columna que producción)."""
    db_path = tmp_path / "stats.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript("""
        CREATE TABLE video_stats_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            video_id INTEGER, yt_video_id TEXT,
            views INTEGER DEFAULT 0, likes INTEGER DEFAULT 0, comments INTEGER DEFAULT 0,
            estimated_minutes_watched REAL DEFAULT 0,
            average_view_duration REAL DEFAULT 0,
            subscribers_gained INTEGER DEFAULT 0,
            estimated_revenue_min REAL DEFAULT 0,
            estimated_revenue_max REAL DEFAULT 0,
            embeddable INTEGER DEFAULT 1,
            analytics_data_exists INTEGER DEFAULT 0,
            impressions INTEGER DEFAULT 0,
            ctr REAL DEFAULT 0,
            retention_pct REAL DEFAULT 0,
            analytics_window_days INTEGER DEFAULT 365,
            fetched_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE videos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            canal TEXT, yt_video_id TEXT, status TEXT DEFAULT 'published',
            channel_id INTEGER
        );
    """)
    conn.execute("INSERT INTO videos (id, canal, yt_video_id, status) VALUES (1, 'canal3', 'ABC123', 'published')")
    conn.commit()
    conn.close()
    return db_path


def _get_db(db_path):
    from database.db_extended import ExtendedDatabase
    return ExtendedDatabase(db_path)


def test_insert_video_stats_guarda_ctr_porcentaje(stats_db):
    db = _get_db(stats_db)
    # La Analytics API devuelve fracción (0.05 = 5%): insert_video_stats debe
    # convertir a porcentaje si el valor es <= 1.
    row_id = db.insert_video_stats(1, "ABC123", {
        "viewCount": 100, "likeCount": 5, "commentCount": 1,
        "impressions": 2000,
        "impressionsClickThroughRate": 0.05,
    })
    assert row_id is not None
    with sqlite3.connect(str(stats_db)) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT impressions, ctr FROM video_stats_history WHERE id = ?", (row_id,)
        ).fetchone()
    assert row["impressions"] == 2000
    assert abs(row["ctr"] - 5.0) < 0.001  # 0.05 → 5.0%


def test_insert_video_stats_acepta_ctr_ya_porcentaje(stats_db):
    db = _get_db(stats_db)
    # Si el dict ya trae 'ctr' en porcentaje (5.0), no se vuelve a multiplicar.
    row_id = db.insert_video_stats(1, "ABC123", {
        "viewCount": 10, "impressions": 100, "ctr": 5.0,
    })
    with sqlite3.connect(str(stats_db)) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT impressions, ctr FROM video_stats_history WHERE id = ?", (row_id,)
        ).fetchone()
    assert row["impressions"] == 100
    assert abs(row["ctr"] - 5.0) < 0.001


def test_batch_update_video_analytics_persiste_ctr(stats_db):
    db = _get_db(stats_db)
    db.insert_video_stats(1, "ABC123", {"viewCount": 50})
    updated = db.batch_update_video_analytics(
        {"ABC123": 1},
        {"ABC123": {
            "estimatedMinutesWatched": "10",
            "averageViewDuration": "120",
            "subscribersGained": "1",
            "impressions": "1500",
            "impressionsClickThroughRate": "0.042",
        }},
    )
    assert updated == 1
    with sqlite3.connect(str(stats_db)) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT impressions, ctr FROM video_stats_history WHERE video_id = 1"
        ).fetchone()
    assert row["impressions"] == 1500
    assert abs(row["ctr"] - 4.2) < 0.001


def test_get_all_videos_analytics_metrics_ampliados(monkeypatch):
    """El bulk query incluye impressions + impressionsClickThroughRate + likes/comments."""
    import pipeline.youtube_stats as ys

    captured = {}

    class FakeQuery:
        def __init__(self, **kw):
            captured.update(kw)
        def execute(self):
            return {"rows": [["VID1", "10", "120", "1", "20.5", "50",
                              "1500", "0.042", "30", "5"]]}

    class FakeReports:
        def query(self, **kw):
            return FakeQuery(**kw)

    class FakeService:
        def reports(self):
            return FakeReports()

    obj = ys.YouTubeStatsFetcher("canal3")
    obj._analytics_service = FakeService()

    result = obj.get_all_videos_analytics(["VID1"], days=30)
    assert "impressions,impressionsClickThroughRate" in captured.get("metrics", "")
    # ago 2026: likes/comments vía Analytics API (fallback cuando el Data API
    # está agotado — yt-dlp ya no los expone en la watch page pública).
    assert "likes,comments" in captured.get("metrics", "")
    assert result["VID1"]["impressions"] == "1500"
    assert result["VID1"]["impressionsClickThroughRate"] == "0.042"
    assert result["VID1"]["likes"] == "30"
    assert result["VID1"]["comments"] == "5"
    # v50: la ventana analizada viaja con el snapshot
    assert result["VID1"]["analytics_window_days"] == "30"


# ── v50: retención + ventana en el snapshot ─────────────────────

def test_insert_video_stats_guarda_retencion_y_ventana(stats_db):
    db = _get_db(stats_db)
    row_id = db.insert_video_stats(1, "ABC123", {
        "viewCount": 100, "impressions": 500,
        "averageViewPercentage": 21.5,
        "analytics_window_days": 365,
    })
    with sqlite3.connect(str(stats_db)) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT retention_pct, analytics_window_days FROM video_stats_history WHERE id = ?",
            (row_id,),
        ).fetchone()
    assert abs(row["retention_pct"] - 21.5) < 0.001
    assert row["analytics_window_days"] == 365


def test_batch_update_video_analytics_guarda_retencion(stats_db):
    db = _get_db(stats_db)
    db.insert_video_stats(1, "ABC123", {"viewCount": 50})
    updated = db.batch_update_video_analytics(
        {"ABC123": 1},
        {"ABC123": {
            "estimatedMinutesWatched": "10",
            "averageViewDuration": "120",
            "subscribersGained": "1",
            "impressions": "1500",
            "impressionsClickThroughRate": "0.042",
            "averageViewPercentage": "19.3",
            "analytics_window_days": "365",
        }},
    )
    assert updated == 1
    with sqlite3.connect(str(stats_db)) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT impressions, ctr, retention_pct, analytics_window_days FROM video_stats_history WHERE video_id = 1"
        ).fetchone()
    assert row["impressions"] == 1500
    assert abs(row["ctr"] - 4.2) < 0.001
    assert abs(row["retention_pct"] - 19.3) < 0.001
    assert row["analytics_window_days"] == 365


def test_update_video_packaging_snapshot_afina_ventana_30d(stats_db):
    db = _get_db(stats_db)
    db.insert_video_stats(1, "ABC123", {"viewCount": 50, "impressions": 1000, "ctr": 2.0})
    ok = db.update_video_packaging_snapshot(1, "ABC123", 700, 4.5, window_days=30)
    assert ok is True
    with sqlite3.connect(str(stats_db)) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT impressions, ctr, analytics_window_days FROM video_stats_history WHERE video_id = 1"
        ).fetchone()
    assert row["impressions"] == 700
    assert abs(row["ctr"] - 4.5) < 0.001
    assert row["analytics_window_days"] == 30


# ── v50: métrica correcta + conversión fracción→% en deep ──────

def test_get_video_impressions_ctr_metric_correcta_y_porcentaje(monkeypatch):
    """El deep query usa `impressionsClickThroughRate` (plural) y convierte la
    fracción 0.05 → 5.0% (antes: métrica errónea + sin conversión)."""
    import pipeline.youtube_stats as ys

    captured = {}

    class FakeQuery:
        def __init__(self, **kw):
            captured.update(kw)
        def execute(self):
            return {"rows": [["VID1", 2000, 0.05]]}

    class FakeReports:
        def query(self, **kw):
            return FakeQuery(**kw)

    class FakeService:
        def reports(self):
            return FakeReports()

    obj = ys.YouTubeStatsFetcher("canal3")
    obj._analytics_service = FakeService()

    result = obj.get_video_impressions_ctr(["VID1"], days=30)
    assert "impressions,impressionsClickThroughRate" in captured.get("metrics", "")
    assert "impressionClickThroughRate" not in captured.get("metrics", "")
    assert result["VID1"]["impressions"] == 2000
    assert abs(result["VID1"]["ctr_percent"] - 5.0) < 0.001
    assert result["VID1"]["analytics_window_days"] == 30
