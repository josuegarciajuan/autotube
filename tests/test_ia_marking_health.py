"""Tests de A4: salud del marcado IA (reconciliación + heartbeat + estancamiento)."""

from __future__ import annotations

import contextlib
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from api.services import ia_marking_health as h  # noqa: E402


class FakeDB:
    def __init__(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript("""
            CREATE TABLE channels (id INTEGER PRIMARY KEY, slug TEXT);
            CREATE TABLE videos (id INTEGER PRIMARY KEY, channel_id INTEGER,
                yt_video_id TEXT, uploaded_at TEXT, published_at TEXT,
                status TEXT DEFAULT '', yt_visibility TEXT DEFAULT '',
                manual_altered_content_done INTEGER DEFAULT 0,
                manual_altered_content_skip INTEGER DEFAULT 0);
            CREATE TABLE shorts (id INTEGER PRIMARY KEY, channel_id INTEGER,
                youtube_id TEXT, actual_published_at TEXT, published_at TEXT,
                yt_visibility TEXT DEFAULT '',
                manual_altered_content_done INTEGER DEFAULT 0,
                manual_altered_content_skip INTEGER DEFAULT 0);
            CREATE TABLE system_state (key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE pipeline_alerts (id INTEGER PRIMARY KEY, entity_type TEXT,
                entity_id INTEGER, channel_id INTEGER, alert_type TEXT,
                severity TEXT, title TEXT, message TEXT, metadata_json TEXT,
                acknowledged INTEGER DEFAULT 0, resolved INTEGER DEFAULT 0,
                resolved_at TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
        """)
        self.conn.execute("INSERT INTO channels (id, slug) VALUES (3,'canal2')")
        # recientes: 2 sin marcar, 1 marcado; 1 antiguo sin marcar (fuera de gracia)
        self.conn.execute("INSERT INTO videos (id,channel_id,yt_video_id,uploaded_at,"
                          "manual_altered_content_done) VALUES (1,3,'V_NEW_A',datetime('now','-1 hours'),0)")
        self.conn.execute("INSERT INTO videos (id,channel_id,yt_video_id,uploaded_at,"
                          "manual_altered_content_done) VALUES (2,3,'V_OLD',datetime('now','-10 days'),0)")
        self.conn.execute("INSERT INTO shorts (id,channel_id,youtube_id,actual_published_at,"
                          "manual_altered_content_done) VALUES (10,3,'S_NEW',datetime('now','-1 hours'),0)")
        self.conn.execute("INSERT INTO shorts (id,channel_id,youtube_id,actual_published_at,"
                          "manual_altered_content_done) VALUES (11,3,'S_OK',datetime('now','-1 hours'),1)")
        self.conn.commit()

    @contextlib.contextmanager
    def _connect(self):
        yield self.conn

    def get_system_state(self, key):
        row = self.conn.execute("SELECT value FROM system_state WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None


def _capture(monkeypatch):
    """Captura todas las alertas emitidas (puede haber más de una)."""
    calls: list[dict] = []
    import api.services.lifecycle_monitor as lm
    monkeypatch.setattr(lm, "emit_alert", lambda *a, **k: calls.append(k) or 1)
    return calls


def test_collect_status_counts():
    db = FakeDB()
    status = h.collect_ia_marking_status(db, grace_hours=72)
    assert status["recent_total"] >= 3
    assert "V_NEW_A" in status["unmarked_ids"]
    assert "S_NEW" in status["unmarked_ids"]
    assert "S_OK" not in status["unmarked_ids"]
    # pendientes en vivo: V_NEW_A + V_OLD + S_NEW
    assert status["backfill_pending"] == 3


def test_skipped_items_excluded():
    db = FakeDB()
    db.conn.execute("UPDATE videos SET manual_altered_content_skip=1 WHERE yt_video_id='V_NEW_A'")
    db.conn.execute("UPDATE shorts SET manual_altered_content_skip=1 WHERE youtube_id='S_NEW'")
    db.conn.commit()
    status = h.collect_ia_marking_status(db, grace_hours=72)
    assert "V_NEW_A" not in status["unmarked_ids"]
    assert "S_NEW" not in status["unmarked_ids"]
    # solo queda V_OLD como pendiente (S_OK marcado, V_NEW_A/S_NEW skipeados)
    assert status["backfill_pending"] == 1


def test_check_emits_critical_when_unmarked(monkeypatch):
    db = FakeDB()
    calls = _capture(monkeypatch)
    h.check_ia_marking_health(db, grace_hours=72)
    types = [c.get("alert_type") for c in calls]
    assert "altered_mark_missing" in types


def test_check_emits_info_heartbeat_when_clean(monkeypatch):
    db = FakeDB()
    # Marca todo lo reciente
    db.conn.execute("UPDATE videos SET manual_altered_content_done=1")
    db.conn.execute("UPDATE shorts SET manual_altered_content_done=1")
    # Progreso reciente del backfill para no disparar la alerta de estancamiento
    db.conn.execute("INSERT INTO system_state (key,value) VALUES ('ia_backfill_last_progress',?)",
                    (datetime.now().isoformat(timespec="seconds"),))
    # Alertas críticas previas que ya no aplican (incl. falso positivo de estancado)
    db.conn.execute(
        "INSERT INTO pipeline_alerts(id, entity_type, entity_id, alert_type, severity, title, resolved) "
        "VALUES (1, 'system', 0, 'altered_mark_missing', 'critical', 'sin marcar', 0)"
    )
    db.conn.execute(
        "INSERT INTO pipeline_alerts(id, entity_type, entity_id, alert_type, severity, title, resolved) "
        "VALUES (2, 'system', 0, 'ia_backfill_stalled', 'critical', 'estancado', 0)"
    )
    db.conn.commit()
    calls = _capture(monkeypatch)
    h.check_ia_marking_health(db, grace_hours=72)
    types = [c.get("alert_type") for c in calls]
    assert "ia_mark_health_ok" in types
    # Las críticas anteriores deben quedar cerradas
    rows = {
        r["alert_type"]: r["resolved"]
        for r in db.conn.execute(
            "SELECT alert_type, resolved FROM pipeline_alerts"
        ).fetchall()
    }
    assert rows["altered_mark_missing"] == 1
    assert rows["ia_backfill_stalled"] == 1


def test_check_emits_stalled_when_no_progress(monkeypatch):
    db = FakeDB()
    # Hay pendientes (V_OLD) y ningún progreso registrado → estancado.
    calls = _capture(monkeypatch)
    h.check_ia_marking_health(db, grace_hours=72)
    types = [c.get("alert_type") for c in calls]
    assert "ia_backfill_stalled" in types


def test_reconcile_recent_marks_marks_and_persists(monkeypatch):
    """La reconciliación activa marca los recientes sin marcar y lo persiste."""
    db = FakeDB()
    import pipeline.youtube_browser as yb
    import api.services.egress_delegation as ed

    marked_ids: list[str] = []

    class _FakeBrowser:
        last_mark_reason = "ok"

    def fake_robust(browser, vid, attempts=3):
        marked_ids.append(vid)
        return True

    monkeypatch.setattr(yb, "get_browser", lambda acct: _FakeBrowser())
    monkeypatch.setattr(yb, "get_account_for_channel", lambda slug: "acct")
    monkeypatch.setattr(yb, "mark_altered_content_robust", fake_robust)
    monkeypatch.setattr(ed, "egress_client_for", lambda slug: None)

    result = h.reconcile_recent_ia_marks(db, grace_hours=72, limit=10)
    assert result["marked"] == 2
    assert result["failed"] == 0
    assert set(marked_ids) == {"V_NEW_A", "S_NEW"}
    # Persistidos en BD
    assert db.conn.execute(
        "SELECT manual_altered_content_done FROM videos WHERE yt_video_id='V_NEW_A'"
    ).fetchone()[0] == 1
    assert db.conn.execute(
        "SELECT manual_altered_content_done FROM shorts WHERE youtube_id='S_NEW'"
    ).fetchone()[0] == 1


def test_reconcile_reports_session_expired(monkeypatch):
    """Si el marcado detecta sesión caducada, se refleja en el resumen."""
    db = FakeDB()
    import pipeline.youtube_browser as yb
    import api.services.egress_delegation as ed

    class _FakeBrowser:
        last_mark_reason = "session_expired"

    monkeypatch.setattr(yb, "get_browser", lambda acct: _FakeBrowser())
    monkeypatch.setattr(yb, "get_account_for_channel", lambda slug: "acct")
    monkeypatch.setattr(yb, "mark_altered_content_robust", lambda b, v, attempts=3: False)
    monkeypatch.setattr(ed, "egress_client_for", lambda slug: None)

    result = h.reconcile_recent_ia_marks(db, grace_hours=72, limit=10)
    assert result["failed"] == 2
    assert result["session_expired"] is True


def test_reconcile_yields_while_backfill_active(monkeypatch):
    """Con el backfill progresando, la reconciliación cede (no compite por Chromium)."""
    db = FakeDB()
    db.conn.execute(
        "INSERT INTO system_state (key,value) VALUES ('ia_backfill_last_progress',?)",
        (datetime.now().isoformat(timespec="seconds"),))
    db.conn.commit()
    result = h.reconcile_recent_ia_marks(db, grace_hours=72)
    assert result["attempted"] == 0


def test_reconcile_runs_when_backfill_stale(monkeypatch):
    db = FakeDB()
    db.conn.execute(
        "INSERT INTO system_state (key,value) VALUES ('ia_backfill_last_progress',?)",
        ("2026-08-01T00:00:00",))
    db.conn.commit()
    import pipeline.youtube_browser as yb
    import api.services.egress_delegation as ed
    monkeypatch.setattr(yb, "get_browser", lambda acct: SimpleNamespace(last_mark_reason="ok"))
    monkeypatch.setattr(yb, "get_account_for_channel", lambda slug: "acct")
    monkeypatch.setattr(yb, "mark_altered_content_robust", lambda b, v, attempts=3: True)
    monkeypatch.setattr(ed, "egress_client_for", lambda slug: None)
    result = h.reconcile_recent_ia_marks(db, grace_hours=72)
    assert result["marked"] == 2


def test_reconcile_noop_when_nothing_pending(monkeypatch):
    db = FakeDB()
    db.conn.execute("UPDATE videos SET manual_altered_content_done=1")
    db.conn.execute("UPDATE shorts SET manual_altered_content_done=1")
    db.conn.commit()
    result = h.reconcile_recent_ia_marks(db, grace_hours=72)
    assert result == {"attempted": 0, "marked": 0, "failed": 0, "session_expired": False}


def test_watchdog_alerts_when_stale_in_window(monkeypatch):
    db = FakeDB()  # tiene pendientes vivos (V_NEW_A, V_OLD, S_NEW)
    db.conn.execute("INSERT INTO system_state (key,value) VALUES ('ia_backfill_heartbeat',?)",
                    ("2020-01-01T00:00:00",))
    db.conn.commit()
    calls = _capture(monkeypatch)

    res = h.check_backfill_running(db, now=datetime(2026, 9, 24, 12, 0, 0))

    assert res["in_window"] is True
    assert res["alerted"] is True
    assert any(c.get("alert_type") == "ia_backfill_not_running" for c in calls)


def test_watchdog_resolves_when_heartbeat_fresh(monkeypatch):
    db = FakeDB()
    db.conn.execute("INSERT INTO system_state (key,value) VALUES ('ia_backfill_heartbeat',?)",
                    (datetime(2026, 9, 24, 11, 55, 0).isoformat(),))
    db.conn.execute(
        "INSERT INTO pipeline_alerts(id, entity_type, entity_id, alert_type, severity, title, resolved) "
        "VALUES (9, 'system', 0, 'ia_backfill_not_running', 'critical', 'parado', 0)")
    db.conn.commit()
    _capture(monkeypatch)

    res = h.check_backfill_running(db, now=datetime(2026, 9, 24, 12, 0, 0))

    assert res["alerted"] is False
    assert res["resolved"] >= 1
    row = db.conn.execute(
        "SELECT resolved FROM pipeline_alerts WHERE alert_type='ia_backfill_not_running'"
    ).fetchone()
    assert row["resolved"] == 1


def test_watchdog_no_alert_outside_window(monkeypatch):
    db = FakeDB()
    db.conn.execute("INSERT INTO system_state (key,value) VALUES ('ia_backfill_heartbeat',?)",
                    ("2020-01-01T00:00:00",))
    db.conn.commit()
    calls = _capture(monkeypatch)

    res = h.check_backfill_running(db, now=datetime(2026, 9, 24, 3, 0, 0))

    assert res["in_window"] is False
    assert res["alerted"] is False
    assert not any(c.get("alert_type") == "ia_backfill_not_running" for c in calls)


def test_watchdog_no_alert_without_pending(monkeypatch):
    db = FakeDB()
    db.conn.execute("UPDATE videos SET manual_altered_content_done=1")
    db.conn.execute("UPDATE shorts SET manual_altered_content_done=1")
    db.conn.execute("INSERT INTO system_state (key,value) VALUES ('ia_backfill_heartbeat',?)",
                    ("2020-01-01T00:00:00",))
    db.conn.commit()
    calls = _capture(monkeypatch)

    res = h.check_backfill_running(db, now=datetime(2026, 9, 24, 12, 0, 0))

    assert res["pending"] == 0
    assert res["alerted"] is False


def test_backfill_stalled_check():
    assert h.backfill_stalled_check(None) is True
    assert h.backfill_stalled_check("2020-01-01T00:00:00") is True
    assert h.backfill_stalled_check(datetime.now().isoformat(timespec="seconds")) is False
    # El estado real se guarda con tz (Europe/Madrid): no debe dar estancado.
    from datetime import timezone, timedelta as _td
    aware_now = datetime.now(timezone(_td(hours=2))).isoformat(timespec="seconds")
    assert h.backfill_stalled_check(aware_now) is False
    assert h.backfill_stalled_check("2020-01-01T00:00:00+02:00") is True
