"""Windowed, impression-weighted CTR analytics."""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from database.db_extended import ExtendedDatabase


def _seed(db):
    with db._connect() as conn:
        for table in ("video_stats_history", "video_reach_daily", "videos", "shorts", "channels"):
            conn.execute(f"DROP TABLE IF EXISTS {table}")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS channels (id INTEGER PRIMARY KEY, name TEXT, "
            "slug TEXT, config_json TEXT, active INTEGER)"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS videos (id INTEGER PRIMARY KEY, channel_id INTEGER, "
            "yt_video_id TEXT, titulo_final TEXT)"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS shorts (id INTEGER PRIMARY KEY, channel_id INTEGER, "
            "youtube_id TEXT, title TEXT)"
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS video_reach_daily (
                   id INTEGER PRIMARY KEY AUTOINCREMENT, channel_id INTEGER, yt_video_id TEXT,
                   date TEXT, impressions INTEGER, impressions_ctr REAL, watch_minutes REAL,
                   subs_gained INTEGER, views INTEGER, retention_pct REAL DEFAULT 0)"""
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS video_stats_history ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, video_id INTEGER, retention_pct REAL, "
            "views INTEGER, fetched_at TEXT DEFAULT CURRENT_TIMESTAMP)"
        )
        conn.execute(
            "INSERT INTO channels (id, name, slug, config_json, active) "
            "VALUES (1, 'Test', 'canal2', '{}', 1)"
        )
        conn.execute(
            "INSERT INTO videos (id, channel_id, yt_video_id, titulo_final) "
            "VALUES (10, 1, 'vidA', 'A'), (11, 1, 'vidB', 'B')"
        )
        conn.execute(
            "INSERT INTO video_stats_history (video_id, retention_pct, views) "
            "VALUES (10, 20.0, 5), (11, 10.0, 1)"
        )
        rows = [
            # Old row outside the window: must NOT influence the result.
            ("oldvid", "2026-09-01", 10000, 10.0, 5000, 50),
            ("vidA", "2026-11-04", 1000, 2.0, 100, 5),
            ("vidA", "2026-11-05", 1000, 4.0, 100, 5),
            # Zero impressions: absence of data, not 0% CTR.
            ("vidB", "2026-11-06", 0, 0.0, 0, 1),
            ("vidB", "2026-11-06", 0, 0.0, 0, 1),
        ]
        for vid, day, imp, ctr, watch, subs in rows:
            conn.execute(
                """INSERT INTO video_reach_daily
                   (channel_id, yt_video_id, date, impressions, impressions_ctr,
                    watch_minutes, subs_gained, views)
                   VALUES (1, ?, ?, ?, ?, ?, ?, ?)""",
                (vid, day, imp, ctr, watch, subs, int(imp * ctr / 100)),
            )
        conn.commit()


def test_weighted_ctr_and_real_window(tmp_path):
    db = ExtendedDatabase(str(tmp_path / "t.db"))
    _seed(db)
    out = db.get_channel_ctr_summary(1, days=30)
    assert out["window_end"] == "2026-11-06"
    assert out["window_start"] == "2026-10-08"
    assert out["total_impressions_30d"] == 2000
    assert out["avg_ctr_30d"] == 3.0  # (1000*2 + 1000*4) / 2000
    assert out["ctr_video_count"] == 1  # only vidA has impressions
    assert out["avg_retention_30d"] == 18.33  # (20*5 + 10*1) / 6
    assert out["retention_video_count"] == 2


def test_batch_summary_uses_same_window(tmp_path):
    db = ExtendedDatabase(str(tmp_path / "t.db"))
    _seed(db)
    out = db.get_all_channels_ctr_summary(days=30)
    assert out[1]["total_impressions_30d"] == 2000
    assert out[1]["avg_ctr_30d"] == 3.0
