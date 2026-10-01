"""Tests de F6 — A/B en el flujo F1/F2 (ventana post-cambio e idempotencia)."""
import sqlite3

from api.services.ab_test_worker import ABTestWorker


class _DB:
    """DB mínima con _get_conn() para el worker A/B."""

    def __init__(self, path):
        self.path = str(path)

    def _get_conn(self):
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn


def _mk(path):
    conn = sqlite3.connect(str(path))
    conn.executescript(
        """
        CREATE TABLE video_reach_daily (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            channel_id INTEGER, yt_video_id TEXT, date TEXT,
            impressions INTEGER DEFAULT 0, impressions_ctr REAL DEFAULT 0,
            retention_pct REAL DEFAULT 0, watch_minutes REAL DEFAULT 0,
            subs_gained INTEGER DEFAULT 0, views INTEGER DEFAULT 0
        );
        """
    )
    conn.commit()
    conn.close()


def test_post_change_ctr_only_counts_window(tmp_path):
    path = tmp_path / "abflow.db"
    _mk(path)
    conn = sqlite3.connect(str(path))
    conn.execute("INSERT INTO video_reach_daily(channel_id,yt_video_id,date,impressions,impressions_ctr)"
                 " VALUES (3,'V1','2026-09-01',100,10.0)")   # antes (CTR 10%)
    conn.execute("INSERT INTO video_reach_daily(channel_id,yt_video_id,date,impressions,impressions_ctr)"
                 " VALUES (3,'V1','2026-09-20',100,5.0)")    # después (CTR 5%)
    conn.commit()
    conn.close()

    worker = ABTestWorker(_DB(path))
    row = {"video_id": 1, "yt_video_id": "V1", "channel_slug": "",
           "thumbnail_rotated_at": "2026-09-15 00:00:00",
           "title_rotated_at": None, "first_checked_at": None}

    post = worker._fetch_ctr(row, post_change=True)
    assert post["impressions"] == 100        # solo la ventana posterior
    assert post["ctr"] == 5.0

    pre = worker._fetch_ctr(row, post_change=False)
    assert pre["impressions"] == 200         # acumulado
    assert pre["ctr"] == 7.5


def test_ensure_ab_row_is_idempotent(tmp_path):
    pytest = __import__("pytest")
    fpm = pytest.importorskip("api.services.full_pipeline_worker")

    path = tmp_path / "ab.db"
    conn = sqlite3.connect(str(path))
    conn.executescript(
        """
        CREATE TABLE video_ab_tests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            video_id INTEGER, yt_video_id TEXT, channel_id INTEGER,
            phase TEXT DEFAULT 'pending', title_v1 TEXT,
            thumbnail_variant_paths TEXT, thumbnail_variant_strategies TEXT,
            thumbnail_variant_active INTEGER DEFAULT 1,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        """
    )
    conn.commit()
    conn.close()

    class DB:
        def __init__(self, p):
            self.path = str(p)

        def _connect(self):
            return sqlite3.connect(self.path)

    db = DB(path)
    fpm._ensure_ab_row(db, 42, 3, "Título", ["a.jpg", "b.jpg"], ["face"])
    fpm._ensure_ab_row(db, 42, 3, "Título", ["a.jpg", "b.jpg", "c.jpg"], ["face", "map"])

    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM video_ab_tests WHERE video_id=42").fetchall()
    assert len(rows) == 1  # sin duplicados entre generación y subida
    assert "c.jpg" in rows[0]["thumbnail_variant_paths"]
    conn.close()
