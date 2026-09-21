"""Tests del backfill de marcado IA (Parte B): ventana diurna y orden de cola."""

from __future__ import annotations

import sqlite3
import sys
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from scripts import yt_mark_pending_ia as m  # noqa: E402


class _AlertDB:
    """DB mínima con pipeline_alerts para probar la auto-resolución."""

    def __init__(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript("""
            CREATE TABLE pipeline_alerts (
                id INTEGER PRIMARY KEY, entity_id INTEGER, channel_id INTEGER,
                alert_type TEXT, message TEXT, resolved INTEGER DEFAULT 0,
                resolved_at TEXT, acknowledged INTEGER DEFAULT 0);
        """)
        self.conn.commit()

    def _connect(self):
        import contextlib

        @contextlib.contextmanager
        def _cm():
            yield self.conn
        return _cm()


def test_resolve_backfill_aborted_is_channel_scoped():
    db = _AlertDB()
    db.conn.execute(
        "INSERT INTO pipeline_alerts(entity_id, channel_id, alert_type, message) "
        "VALUES (3, 3, 'ia_backfill_aborted', 'fallos canal2')"
    )
    db.conn.execute(
        "INSERT INTO pipeline_alerts(entity_id, channel_id, alert_type, message) "
        "VALUES (4, 4, 'ia_backfill_aborted', 'fallos canal3')"
    )
    db.conn.commit()

    assert m.resolve_backfill_aborted(db, "canal2", 3) == 1
    rows = {
        r["channel_id"]: r["resolved"]
        for r in db.conn.execute(
            "SELECT channel_id, resolved FROM pipeline_alerts"
        ).fetchall()
    }
    assert rows[3] == 1
    assert rows[4] == 0


def test_in_window_boundaries():
    assert m.in_window(datetime(2026, 9, 18, 9, 0)) is True
    assert m.in_window(datetime(2026, 9, 18, 14, 30)) is True
    assert m.in_window(datetime(2026, 9, 18, 22, 59)) is True
    assert m.in_window(datetime(2026, 9, 18, 23, 0)) is False
    assert m.in_window(datetime(2026, 9, 18, 8, 59)) is False
    assert m.in_window(datetime(2026, 9, 18, 3, 0)) is False


def _make_db(path: Path) -> None:
    conn = sqlite3.connect(str(path))
    conn.executescript("""
        CREATE TABLE channels (id INTEGER PRIMARY KEY, slug TEXT);
        CREATE TABLE videos (id INTEGER PRIMARY KEY, channel_id INTEGER,
            yt_video_id TEXT, uploaded_at TEXT, published_at TEXT, created_at TEXT,
            manual_altered_content_done INTEGER DEFAULT 0);
        CREATE TABLE shorts (id INTEGER PRIMARY KEY, channel_id INTEGER,
            youtube_id TEXT, actual_published_at TEXT, published_at TEXT, created_at TEXT,
            manual_altered_content_done INTEGER DEFAULT 0);
    """)
    conn.execute("INSERT INTO channels (id, slug) VALUES (3,'canal2')")
    conn.execute("INSERT INTO channels (id, slug) VALUES (4,'canal3')")
    # dos vídeos (canal3, id 4): uno antiguo y uno nuevo sin marcar; uno ya marcado
    conn.execute("INSERT INTO videos (id, channel_id, yt_video_id, uploaded_at,"
                 " manual_altered_content_done) VALUES (1,4,'OLD','2026-08-01 10:00:00',0)")
    conn.execute("INSERT INTO videos (id, channel_id, yt_video_id, uploaded_at,"
                 " manual_altered_content_done) VALUES (2,4,'NEW','2026-09-16 10:00:00',0)")
    conn.execute("INSERT INTO videos (id, channel_id, yt_video_id, uploaded_at,"
                 " manual_altered_content_done) VALUES (3,4,'DONE','2026-09-17 10:00:00',1)")
    conn.execute("INSERT INTO shorts (id, channel_id, youtube_id, actual_published_at,"
                 " manual_altered_content_done) VALUES (10,3,'SH','2026-09-10 10:00:00',0)")
    conn.commit()
    conn.close()


def test_get_pending_newest_first_and_excludes_done(tmp_path, monkeypatch):
    db_path = tmp_path / "t.db"
    _make_db(db_path)
    monkeypatch.setattr(m, "DB_PATH", db_path)

    items = m.get_pending()
    ids = [it["yt_id"] for it in items]
    assert ids == ["NEW", "SH", "OLD"], "debe ordenar por fecha desc y excluir marcados"

    only_c3 = m.get_pending("canal3")
    assert [it["yt_id"] for it in only_c3] == ["NEW", "OLD"]
    assert all(it["canal"] == "canal3" for it in only_c3)
