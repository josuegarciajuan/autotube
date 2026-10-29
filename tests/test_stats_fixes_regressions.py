"""Regresiones de la auditoría de recolección de stats (oct 2026).

Cubre los fallos corregidos:
  - Analytics de Shorts (antes nunca se consultaban sus IDs).
  - Persistencia de lotes parciales al vencer el deadline.
  - Snapshot de canal con Analytics aunque falle el scrape público.
  - Paginación real del watch-time diario (365 días).
  - Media/proyección YPP limitada a los últimos 30 días.
  - Conteo real de filas en batch_update_short_analytics.
  - Resultado global `partial` cuando un canal agota su deadline.
  - Watch-time de maratones sin sumar snapshots históricos.
  - Agente egress usa `estimatedMinutesWatched` y cubre shorts.
"""
import json
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest


# ── Fakes comunes ──────────────────────────────────────────────

class FakeStatsDB:
    def __init__(self):
        self.videos = [{"id": 1, "yt_video_id": "VID1"}]
        self.shorts = [{"id": 2, "youtube_id": "SHORT1", "type": "native"}]
        self.channel_stats = []
        self.video_stats = []
        self.short_stats = []
        self.video_analytics_calls = []
        self.short_analytics_calls = []
        self.daily = []

    def get_channel_by_slug(self, slug):
        return {"id": 10, "slug": slug, "name": "Test"}

    def get_videos(self, channel_id=None, limit=10000):
        return list(self.videos)

    def get_shorts(self, channel_id=None, status="published", limit=10000):
        return list(self.shorts)

    def insert_channel_stats(self, channel_id, stats):
        self.channel_stats.append((channel_id, dict(stats)))
        return 1

    def insert_video_stats(self, video_id, yt_id, stats):
        self.video_stats.append((video_id, yt_id, dict(stats)))
        return 1

    def insert_short_stats(self, short_id, yt_id, stats, short_type=None):
        self.short_stats.append((short_id, yt_id, dict(stats), short_type))
        return 1

    def batch_update_video_analytics(self, mapping, data):
        self.video_analytics_calls.append((mapping, data))
        return len(mapping)

    def batch_update_short_analytics(self, mapping, data):
        self.short_analytics_calls.append((mapping, data))
        return len(mapping)

    def insert_video_analytics_batch(self, *a, **k):
        return 1

    def upsert_daily_watchtime(self, channel_id, daily):
        self.daily.extend(daily)
        return len(daily)


def _fetcher(monkeypatch, slug="canal3"):
    import pipeline.youtube_stats as ys

    f = ys.YouTubeStatsFetcher(slug)
    f._analytics_service = object()
    monkeypatch.setattr(f, "authenticate", lambda: True)
    monkeypatch.setattr(f, "get_channel_stats", lambda: {
        "subscriberCount": "100", "viewCount": "1000", "videoCount": "10",
    })
    monkeypatch.setattr(f, "get_channel_analytics", lambda days=365: {
        "estimatedMinutesWatched": "600",
    })
    monkeypatch.setattr(f, "get_channel_daily_analytics", lambda days=365: [])
    monkeypatch.setattr(f, "get_video_stats_batch", lambda ids, deadline=None: {
        yt: {"viewCount": "5", "likeCount": "1", "commentCount": "0",
             "is_mock": False}
        for yt in ids
    })
    return f


# ── Shorts reciben Analytics ───────────────────────────────────

def test_analytics_incluye_ids_de_shorts(monkeypatch):
    f = _fetcher(monkeypatch)
    captured = {}

    def fake_all(ids, days=365, deadline=None):
        captured["ids"] = list(ids)
        return {
            "VID1": {"estimatedMinutesWatched": "10",
                     "averageViewDuration": "30",
                     "subscribersGained": "1",
                     "averageViewPercentage": "40"},
            "SHORT1": {"estimatedMinutesWatched": "7",
                       "averageViewDuration": "12",
                       "subscribersGained": "2",
                       "averageViewPercentage": "55"},
        }

    monkeypatch.setattr(f, "get_all_videos_analytics", fake_all)
    db = FakeStatsDB()

    result = f.collect_and_store(db, deep=False, use_data_api=True,
                                 scrape_fallback=False)

    assert captured["ids"] == ["VID1", "SHORT1"]
    assert db.video_analytics_calls, "long-forms deben actualizarse"
    assert db.short_analytics_calls, "shorts deben actualizarse con Analytics"
    _, short_data = db.short_analytics_calls[0]
    assert short_data["SHORT1"]["estimatedMinutesWatched"] == "7"
    assert result["analytics_updated"] == 2


def test_lote_parcial_se_persiste_antes_del_deadline(monkeypatch):
    f = _fetcher(monkeypatch)
    monkeypatch.setattr(f, "get_channel_daily_analytics",
                        lambda days=365: [])
    monkeypatch.setattr(f, "get_all_videos_analytics",
                        lambda ids, days=365, deadline=None: {})
    monkeypatch.setattr(f, "get_video_stats_batch", lambda ids, deadline=None: {
        "_deadline_exceeded": True,
        "VID1": {"viewCount": "5", "likeCount": "1", "commentCount": "0",
                 "is_mock": False},
        "VID2": {"viewCount": "0", "likeCount": "0", "commentCount": "0",
                 "is_mock": True},
    })
    db = FakeStatsDB()
    db.videos.append({"id": 3, "yt_video_id": "VID2"})

    result = f.collect_and_store(db, deep=False, use_data_api=True,
                                 scrape_fallback=False)

    assert result["deadline_exceeded"] is True
    assert result["videos_updated"] == 1
    assert [r[1] for r in db.video_stats] == ["VID1"]
    assert result["deadline_stage"] == "data_api_videos"


def test_canal_persiste_analytics_aunque_falle_scrape(monkeypatch):
    f = _fetcher(monkeypatch)
    monkeypatch.setattr(f, "get_channel_stats", lambda: None)
    monkeypatch.setattr(f, "get_video_stats_batch", lambda ids, deadline=None: {})
    monkeypatch.setattr(f, "get_all_videos_analytics",
                        lambda ids, days=365, deadline=None: {})

    class EmptyScraper:
        def __init__(self, *a, **k):
            pass

        def get_channel_stats(self, channel):
            return {}  # scrape vacío / bot-check

        def get_video_stats_batch(self, ids, deadline=None):
            return {}

    import pipeline.youtube_stats_scraper as sc
    monkeypatch.setattr(sc, "YouTubeStatsScraper", EmptyScraper)

    db = FakeStatsDB()
    result = f.collect_and_store(db, deep=False, use_data_api=True,
                                 scrape_fallback=True)

    assert result["channel_updated"] is True
    assert db.channel_stats, "debe guardar snapshot con Analytics"
    snapshot = db.channel_stats[0][1]
    assert float(snapshot["estimatedMinutesWatched"]) == 600.0


# ── Paginación del watch-time diario ───────────────────────────

def test_daily_analytics_pagina_mas_de_200_dias():
    import pipeline.youtube_stats as ys

    f = ys.YouTubeStatsFetcher("canal3")
    pages = {1: 200, 201: 165}
    seen_starts = []

    class FakeQuery:
        def __init__(self, **kw):
            self.kw = kw

        def execute(self, num_retries=0):
            start = self.kw["startIndex"]
            seen_starts.append(start)
            n = pages.get(start, 0)
            base = datetime(2025, 10, 1)
            return {"rows": [
                [(base + timedelta(days=start + i)).strftime("%Y-%m-%d"),
                 10.0, 1] for i in range(n)
            ]}

    class FakeReports:
        def query(self, **kw):
            return FakeQuery(**kw)

    f._analytics_service = type("S", (), {"reports": lambda self: FakeReports()})()

    daily = f.get_channel_daily_analytics(days=365)

    assert seen_starts == [1, 201]
    assert len(daily) == 365
    assert len({d["date"] for d in daily}) == 365


# ── YPP: media de 30 días ──────────────────────────────────────

def _watchtime_db(tmp_path):
    from database.db_extended import ExtendedDatabase

    p = tmp_path / "watch.db"
    with sqlite3.connect(str(p)) as conn:
        conn.executescript("""
            CREATE TABLE channel_stats_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT, channel_id INTEGER,
                subscribers INTEGER DEFAULT 0, total_views INTEGER DEFAULT 0,
                video_count INTEGER DEFAULT 0,
                estimated_minutes_watched REAL DEFAULT 0,
                estimated_revenue_min REAL DEFAULT 0,
                estimated_revenue_max REAL DEFAULT 0,
                fetched_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP);
            CREATE TABLE channel_daily_watchtime (
                channel_id INTEGER, date TEXT,
                estimated_minutes_watched REAL DEFAULT 0,
                subscribers_gained INTEGER DEFAULT 0,
                PRIMARY KEY (channel_id, date));
            CREATE TABLE videos (
                id INTEGER PRIMARY KEY, channel_id INTEGER, titulo_final TEXT,
                yt_video_id TEXT, yt_url TEXT);
            CREATE TABLE video_stats_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT, video_id INTEGER,
                estimated_minutes_watched REAL DEFAULT 0,
                views INTEGER DEFAULT 0, likes INTEGER DEFAULT 0);
            INSERT INTO channel_stats_history
                (channel_id, estimated_minutes_watched) VALUES (10, 6000);
        """)
        today = datetime.now().date()
        for i in range(5):
            conn.execute(
                "INSERT INTO channel_daily_watchtime VALUES (?, ?, ?, 0)",
                (10, (today - timedelta(days=i)).isoformat(), 60.0),
            )
        # Histórico antiguo que NO debe entrar en la media de 30 días.
        conn.execute(
            "INSERT INTO channel_daily_watchtime VALUES (?, ?, ?, 0)",
            (10, (today - timedelta(days=200)).isoformat(), 6000.0),
        )
        conn.commit()
    return ExtendedDatabase(p)


def test_watchtime_summary_usa_ultimos_30_dias(tmp_path):
    db = _watchtime_db(tmp_path)
    summary = db.get_channel_watch_time_summary(10)
    # 5 días × 60 min / 5 = 60 min = 1 h (sin los 6000 min antiguos).
    assert summary["daily_avg_hours"] == 1.0
    assert summary["daily_latest_date"] is not None


# ── Conteo de shorts Analytics ─────────────────────────────────

def test_batch_update_short_analytics_cuenta_filas_reales(tmp_path):
    from database.db_extended import ExtendedDatabase

    p = tmp_path / "shorts.db"
    with sqlite3.connect(str(p)) as conn:
        conn.executescript("""
            CREATE TABLE short_stats (
                id INTEGER PRIMARY KEY AUTOINCREMENT, short_id INTEGER,
                yt_video_id TEXT, views INTEGER DEFAULT 0,
                likes INTEGER DEFAULT 0, comments INTEGER DEFAULT 0,
                estimated_minutes_watched REAL DEFAULT 0,
                average_view_duration REAL DEFAULT 0,
                subscribers_gained INTEGER DEFAULT 0,
                short_type TEXT, embeddable INTEGER DEFAULT 1,
                fetched_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP);
            INSERT INTO short_stats (short_id, yt_video_id) VALUES (1, 'A');
            INSERT INTO short_stats (short_id, yt_video_id) VALUES (2, 'B');
        """)
        conn.commit()

    db = ExtendedDatabase(p)
    n = db.batch_update_short_analytics(
        {"A": 1, "B": 2},
        {"A": {"estimatedMinutesWatched": "5", "averageViewDuration": "9",
               "subscribersGained": "1"},
         "B": {"estimatedMinutesWatched": "7", "averageViewDuration": "11",
               "subscribersGained": "2"}},
    )
    assert n == 2


# ── Resultado global parcial por deadline de canal ─────────────

def test_deadline_de_canal_marca_resultado_global_parcial(tmp_path, monkeypatch):
    import os
    os.environ["DATABASE_PATH"] = str(tmp_path / "wiring.db")
    with sqlite3.connect(str(tmp_path / "wiring.db")) as sq:
        sq.execute(
            """CREATE TABLE IF NOT EXISTS system_state (
                key TEXT PRIMARY KEY, value TEXT NOT NULL DEFAULT '',
                updated_at TEXT DEFAULT (datetime('now')))"""
        )
        sq.commit()

    import api.main as m

    m.TOKENS_DIR = tmp_path / "tokens"
    m.TOKENS_DIR.mkdir(exist_ok=True)
    (m.TOKENS_DIR / "canal2.pickle").write_bytes(b"stub")

    class SlowFetcher:
        def __init__(self, slug):
            pass

        def collect_and_store(self, db, deep=False, use_data_api=True,
                              deadline=None, progress=None):
            return {"videos_updated": 1, "shorts_updated": 0,
                    "deadline_exceeded": True, "deadline_stage": "bulk_analytics",
                    "authenticated": True, "analytics_available": True}

    class FakeDB:
        def get_channels(self, active_only=True):
            return [{"id": 1, "slug": "canal2", "name": "Test", "cpm_min": None}]

        def is_quota_exhausted_for_channel(self, slug):
            return False

        def record_stats_collection_run(self, *a, **k):
            pass

        def set_system_state(self, *a):
            pass

    import database.db_extended as dbe
    monkeypatch.setattr(dbe, "ExtendedDatabase", lambda *a, **k: FakeDB())
    import pipeline.youtube_stats as ys
    monkeypatch.setattr(ys, "YouTubeStatsFetcher", SlowFetcher)

    coord = m._stats_coordinator
    acquired, st = coord.try_acquire(deep=True)
    assert acquired
    m._collect_youtube_stats(deep=True, force=False, run_id=st["run_id"])

    snap = coord.snapshot()
    assert snap["result"] == "partial"
    assert snap["deadline_hit"] is True
    assert snap["channels"][0]["partial"] is True
    assert snap["channels"][0]["deadline_stage"] == "bulk_analytics"


# ── Maratones: último snapshot, no la suma histórica ───────────

def test_maraton_watchtime_no_suma_snapshots_historicos(tmp_path, monkeypatch):
    from api.routers import channels as ch_router

    p = tmp_path / "marathon.db"
    with sqlite3.connect(str(p)) as conn:
        conn.executescript("""
            CREATE TABLE videos (
                id INTEGER PRIMARY KEY, channel_id INTEGER, titulo_final TEXT,
                yt_video_id TEXT, is_marathon INTEGER DEFAULT 0,
                duracion_seg INTEGER DEFAULT 0);
            CREATE TABLE video_stats_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT, video_id INTEGER,
                estimated_minutes_watched REAL DEFAULT 0,
                views INTEGER DEFAULT 0, likes INTEGER DEFAULT 0,
                comments INTEGER DEFAULT 0);
            INSERT INTO videos (id, channel_id, titulo_final, yt_video_id, is_marathon)
                VALUES (1, 10, 'Maratón', 'M1', 1);
            INSERT INTO video_stats_history (video_id, estimated_minutes_watched, views)
                VALUES (1, 100, 10);
            INSERT INTO video_stats_history (video_id, estimated_minutes_watched, views)
                VALUES (1, 250, 20);
        """)
        conn.commit()

    class DB:
        def get_channel(self, cid):
            return {"id": cid, "slug": "canal3"}

        def _connect(self):
            conn = sqlite3.connect(str(p))
            conn.row_factory = sqlite3.Row
            return conn

    monkeypatch.setattr(ch_router, "get_db", lambda: DB())

    out = ch_router.get_channel_marathon_analytics(10)

    assert out["marathon_analytics"]["total_watch_hours"] == round(250 / 60.0, 1)


# ── Agente egress: métrica válida + shorts ─────────────────────

def test_egress_collect_stats_metrica_y_shorts(monkeypatch):
    import egress_agent.youtube_api as agent

    captured = {}

    class FakeAnalyticsQuery:
        def __init__(self, **kw):
            self.kw = kw

        def execute(self):
            if self.kw.get("dimensions") == "day":
                captured["daily"] = self.kw
                return {"rows": [["2026-10-01", 10.0, 1]]}
            if self.kw.get("dimensions") == "video":
                captured["analytics"] = self.kw
                return {"rows": [["VID1", 5, 10, 1, 50, 100, 2, 1]]}
            captured["watch"] = self.kw
            return {"rows": [[600.0]]}

    class FakeAnalytics:
        def reports(self):
            return type("R", (), {"query": lambda self, **kw: FakeAnalyticsQuery(**kw)})()

    class FakeData:
        def videos(self):
            return type("V", (), {"list": lambda self, **kw: type(
                "Q", (), {"execute": lambda s: {"items": [
                    {"id": i, "statistics": {"viewCount": "1"},
                     "contentDetails": {"duration": "PT1M"}}
                    for i in kw["id"].split(",")
                ]}})()})()

        def channels(self):
            return type("C", (), {"list": lambda self, **kw: type(
                "Q", (), {"execute": lambda s: {"items": [
                    {"statistics": {"subscriberCount": "10",
                                    "viewCount": "100", "videoCount": "1"},
                     "snippet": {"title": "Test"}}
                ]}})()})()

    monkeypatch.setattr(agent, "_service", lambda cfg, *a: (
        FakeAnalytics() if a else FakeData()
    ))

    out = agent.collect_stats(
        object(), video_ids=["VID1"], short_ids=["SHORT1"], days=365
    )
    res = out["result"]
    assert captured["watch"]["metrics"] == "estimatedMinutesWatched"
    assert "estimatedMinutesWatched" in captured["analytics"]["metrics"]
    assert "SHORT1" in res["shorts"]
    assert res["watch_time_min"] == 600.0
    assert res["daily"][0]["date"] == "2026-10-01"
    start = datetime.strptime(captured["watch"]["startDate"], "%Y-%m-%d")
    assert (datetime.utcnow() - start).days >= 364


# ── Persistencia del payload egress (shorts + daily + watch-time) ──

def test_egress_persiste_shorts_daily_y_watchtime(tmp_path):
    import pipeline.youtube_stats as ys

    dbp = tmp_path / "egress.db"
    with sqlite3.connect(str(dbp)) as conn:
        conn.executescript("""
            CREATE TABLE channels (id INTEGER PRIMARY KEY, slug TEXT);
            CREATE TABLE videos (id INTEGER PRIMARY KEY, channel_id INTEGER,
                                 yt_video_id TEXT, status TEXT DEFAULT 'published');
            CREATE TABLE shorts (id INTEGER PRIMARY KEY, channel_id INTEGER,
                                 youtube_id TEXT, status TEXT DEFAULT 'published',
                                 type TEXT DEFAULT 'clip');
            INSERT INTO channels VALUES (10, 'canal3');
            INSERT INTO videos (id, channel_id, yt_video_id) VALUES (1, 10, 'VID1');
            INSERT INTO shorts (id, channel_id, youtube_id, type)
                VALUES (2, 10, 'SHORT1', 'native');
        """)
        conn.commit()

    class DB(FakeStatsDB):
        def _connect(self):
            c = sqlite3.connect(str(dbp))
            c.row_factory = sqlite3.Row
            return c

        def get_channel_by_slug(self, slug):
            return {"id": 10, "slug": slug}

    class Egress:
        def api_call(self, op, params):
            assert op == "collect_stats"
            assert params["short_ids"] == ["SHORT1"]
            assert params["days"] == 365
            return {"ok": True, "result": {
                "videos": {"VID1": {"views": 10, "likes": 1, "comments": 0}},
                "shorts": {"SHORT1": {"views": 5, "likes": 2, "comments": 0}},
                "channel": {"subscribers": 100, "total_views": 1000,
                            "video_count": 10},
                "watch_time_min": 600,
                "analytics": {
                    "VID1": {"estimatedMinutesWatched": 30,
                             "averageViewDuration": 60,
                             "subscribersGained": 1,
                             "averageViewPercentage": 40},
                    "SHORT1": {"estimatedMinutesWatched": 12,
                               "averageViewDuration": 8,
                               "subscribersGained": 2,
                               "averageViewPercentage": 70},
                },
                "daily": [{"date": "2026-10-01",
                           "estimatedMinutesWatched": 10.0,
                           "subscribersGained": 1}],
                "errors": {},
            }}

    f = ys.YouTubeStatsFetcher("canal3")
    db = DB()
    result = f._collect_and_store_via_egress(db, Egress(), deep=True)

    assert result["videos_updated"] == 1
    assert result["shorts_updated"] == 1
    assert result["channel_updated"] is True
    assert result["analytics_updated"] == 2
    assert result["daily_watchtime_days"] == 1
    assert result["daily_watchtime_latest_date"] == "2026-10-01"

    # Short persistido con sus métricas Analytics.
    short_row = db.short_stats[0]
    assert short_row[1] == "SHORT1"
    assert short_row[2]["estimatedMinutesWatched"] == 12.0
    # Canal persistido con watch-time válido (no cero).
    ch_snapshot = db.channel_stats[0][1]
    assert float(ch_snapshot["estimatedMinutesWatched"]) == 600.0
