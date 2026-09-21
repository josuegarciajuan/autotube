"""Tests del backfill de marcado IA: ventana, orden de cola y robustez."""

from __future__ import annotations

import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from scripts import yt_mark_pending_ia as m  # noqa: E402


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
            status TEXT DEFAULT '', yt_visibility TEXT DEFAULT '',
            manual_altered_content_done INTEGER DEFAULT 0,
            manual_altered_content_skip INTEGER DEFAULT 0,
            manual_altered_content_skip_reason TEXT,
            manual_altered_content_attempts INTEGER DEFAULT 0,
            manual_altered_content_skip_at TEXT);
        CREATE TABLE shorts (id INTEGER PRIMARY KEY, channel_id INTEGER,
            youtube_id TEXT, actual_published_at TEXT, published_at TEXT, created_at TEXT,
            yt_visibility TEXT DEFAULT '',
            manual_altered_content_done INTEGER DEFAULT 0,
            manual_altered_content_skip INTEGER DEFAULT 0,
            manual_altered_content_skip_reason TEXT,
            manual_altered_content_attempts INTEGER DEFAULT 0,
            manual_altered_content_skip_at TEXT);
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


def _args(max_attempts: int = 3) -> SimpleNamespace:
    return SimpleNamespace(
        max_items=0, max_attempts=max_attempts,
        max_consecutive_technical_failures=5,
        no_window=True, window_start=0, window_end=24,
        delay_min=0, delay_max=0, long_pause_min=0, long_pause_max=0,
    )


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


def test_get_pending_excludes_unavailable_and_skipped(tmp_path, monkeypatch):
    db_path = tmp_path / "t.db"
    _make_db(db_path)
    monkeypatch.setattr(m, "DB_PATH", db_path)
    conn = sqlite3.connect(str(db_path))
    conn.execute("INSERT INTO videos (id,channel_id,yt_video_id,uploaded_at,status)"
                 " VALUES (20,3,'DELETED','2026-09-15 10:00:00','deleted_on_yt')")
    conn.execute("INSERT INTO videos (id,channel_id,yt_video_id,uploaded_at,yt_visibility)"
                 " VALUES (21,3,'REMOVED','2026-09-15 11:00:00','removed')")
    conn.execute("INSERT INTO videos (id,channel_id,yt_video_id,uploaded_at,"
                 "manual_altered_content_skip) VALUES (22,3,'SKIPPED','2026-09-15 12:00:00',1)")
    conn.execute("INSERT INTO videos (id,channel_id,yt_video_id,uploaded_at)"
                 " VALUES (23,3,'OK_PUBLIC','2026-09-15 13:00:00')")
    conn.execute("INSERT INTO shorts (id,channel_id,youtube_id,actual_published_at,yt_visibility)"
                 " VALUES (30,3,'SH_REMOVED','2026-09-15 14:00:00','unavailable')")
    conn.commit()
    conn.close()

    ids = [it["yt_id"] for it in m.get_pending("canal2")]
    assert "DELETED" not in ids
    assert "REMOVED" not in ids
    assert "SKIPPED" not in ids
    assert "SH_REMOVED" not in ids
    assert "OK_PUBLIC" in ids


def test_mark_unavailable_items_persists_skip(tmp_path, monkeypatch):
    db_path = tmp_path / "t.db"
    _make_db(db_path)
    monkeypatch.setattr(m, "DB_PATH", db_path)
    conn = sqlite3.connect(str(db_path))
    conn.execute("INSERT INTO videos (id,channel_id,yt_video_id,uploaded_at,status)"
                 " VALUES (20,3,'DELETED','2026-09-15 10:00:00','deleted_on_yt')")
    conn.commit()
    conn.close()

    n = m.mark_unavailable_items()
    assert n == 1
    conn = sqlite3.connect(str(db_path))
    row = conn.execute("SELECT manual_altered_content_skip, manual_altered_content_skip_reason"
                       " FROM videos WHERE yt_video_id='DELETED'").fetchone()
    conn.close()
    assert row[0] == 1
    assert row[1] == "unavailable_yt"


class _FakeBrowser:
    def __init__(self, results: dict[str, str]):
        self.results = results  # yt_id -> "ok" o motivo de fallo
        self.last_mark_reason = ""

    def mark_altered_content(self, yt_id: str) -> bool:
        r = self.results.get(yt_id, "ok")
        self.last_mark_reason = r
        return r in ("ok", "already")


def _patch_browser(monkeypatch, fake):
    import pipeline.youtube_browser as yb
    monkeypatch.setattr(yb, "get_browser", lambda acct: fake)
    monkeypatch.setattr(yb, "get_account_for_channel", lambda slug: "acct")
    monkeypatch.setattr(m, "set_state", lambda *a, **k: None)
    monkeypatch.setattr(m, "resolve_backfill_aborted", lambda *a, **k: 0)


def test_process_channel_does_not_abort_on_item_failure(tmp_path, monkeypatch):
    """Un ítem no marcable no debe abortar la sesión ni bloquear los siguientes."""
    db_path = tmp_path / "t.db"
    _make_db(db_path)
    monkeypatch.setattr(m, "DB_PATH", db_path)
    conn = sqlite3.connect(str(db_path))
    conn.execute("DELETE FROM shorts")
    conn.execute("INSERT INTO videos (id,channel_id,yt_video_id,uploaded_at)"
                 " VALUES (20,3,'BAD','2026-09-15 10:00:00')")
    conn.execute("INSERT INTO videos (id,channel_id,yt_video_id,uploaded_at)"
                 " VALUES (21,3,'GOOD','2026-09-15 09:00:00')")
    conn.commit()
    conn.close()

    fake = _FakeBrowser({"BAD": "radio_disabled", "GOOD": "ok"})
    _patch_browser(monkeypatch, fake)

    done, finished = m.process_channel(object(), "canal2", _args(), 0)
    assert finished is True
    assert done == 1  # GOOD marcado, BAD falló pero no abortó
    conn = sqlite3.connect(str(db_path))
    bad = conn.execute("SELECT manual_altered_content_done, manual_altered_content_attempts,"
                       " manual_altered_content_skip FROM videos WHERE yt_video_id='BAD'").fetchone()
    good = conn.execute("SELECT manual_altered_content_done FROM videos WHERE yt_video_id='GOOD'").fetchone()
    conn.close()
    assert good[0] == 1
    assert bad[1] == 1  # intento contabilizado
    assert bad[2] == 0  # aún no descartado


def test_process_channel_skips_after_max_attempts(tmp_path, monkeypatch):
    db_path = tmp_path / "t.db"
    _make_db(db_path)
    monkeypatch.setattr(m, "DB_PATH", db_path)
    conn = sqlite3.connect(str(db_path))
    conn.execute("INSERT INTO videos (id,channel_id,yt_video_id,uploaded_at)"
                 " VALUES (20,3,'BAD','2026-09-15 10:00:00')")
    conn.commit()
    conn.close()

    fake = _FakeBrowser({"BAD": "radio_disabled"})
    _patch_browser(monkeypatch, fake)

    for _ in range(3):
        m.process_channel(object(), "canal2", _args(max_attempts=3), 0)

    conn = sqlite3.connect(str(db_path))
    row = conn.execute("SELECT manual_altered_content_skip, manual_altered_content_skip_reason,"
                       " manual_altered_content_attempts FROM videos WHERE yt_video_id='BAD'").fetchone()
    conn.close()
    assert row[0] == 1
    assert row[1] == "radio_disabled"
    assert row[2] == 3


def test_process_channel_aborts_on_consecutive_technical_failures(tmp_path, monkeypatch):
    db_path = tmp_path / "m.db"
    _make_db(db_path)
    monkeypatch.setattr(m, "DB_PATH", db_path)
    conn = sqlite3.connect(str(db_path))
    for i in range(6):
        conn.execute("INSERT INTO videos (id,channel_id,yt_video_id,uploaded_at)"
                     f" VALUES ({100+i},3,'T{i}','2026-09-15 1{i}:00:00')")
    conn.commit()
    conn.close()

    fake = _FakeBrowser({f"T{i}": "navigation_failed" for i in range(6)})
    _patch_browser(monkeypatch, fake)
    alerts = []
    monkeypatch.setattr(m, "alert", lambda db, *a, **k: alerts.append(a))

    done, finished = m.process_channel(object(), "canal2", _args(), 0)
    assert finished is False
    assert alerts, "debe emitir alerta de aborto técnico"
    assert alerts[0][0] == "ia_backfill_aborted"
