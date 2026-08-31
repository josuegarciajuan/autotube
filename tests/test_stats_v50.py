"""Tests v50: analytics coverage por canal, reminders que solo alertan y seo_summary.

Verifica que:
  - record_stats_collection_run persiste el estado real por canal.
  - los reminders vencidos se marcan 'alerted' una sola vez (no re-emiten).
  - _build_seo_summary lee el snapshot fiable (video_stats_history) + cobertura
    del ledger, en vez de los ceros de video_analytics_detailed.
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import sqlite3
import pytest


@pytest.fixture
def v50_db(tmp_path):
    """DB mínima con channels, videos, video_stats_history, ledger y reminders."""
    db_path = tmp_path / "v50.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript("""
        CREATE TABLE channels (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT, slug TEXT, active INTEGER DEFAULT 1
        );
        CREATE TABLE videos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            canal TEXT, yt_video_id TEXT, status TEXT DEFAULT 'published',
            channel_id INTEGER
        );
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
        CREATE TABLE stats_collection_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            channel_id INTEGER NOT NULL,
            started_at TIMESTAMP, finished_at TIMESTAMP,
            status TEXT NOT NULL DEFAULT 'collected',
            deep INTEGER DEFAULT 0, use_data_api INTEGER DEFAULT 1,
            result_json TEXT DEFAULT '{}',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE scheduled_reminders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            entity_type TEXT NOT NULL DEFAULT 'system',
            entity_id INTEGER NOT NULL,
            title TEXT NOT NULL, message TEXT,
            alert_type TEXT NOT NULL DEFAULT 'scheduled_reminder_due',
            due_at TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            alert_id INTEGER,
            metadata_json TEXT DEFAULT '{}',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            resolved_at TIMESTAMP
        );
    """)
    conn.execute("INSERT INTO channels (id, name, slug) VALUES (1, 'Civilizaciones Olvidadas', 'canal3')")
    conn.execute("INSERT INTO videos (id, canal, yt_video_id, status, channel_id) VALUES (1, 'canal3', 'ABC123', 'published', 1)")
    conn.commit()
    conn.close()
    return db_path


def _get_db(db_path):
    from database.db_extended import ExtendedDatabase
    return ExtendedDatabase(db_path)


# ── Ledger de recolección ───────────────────────────────────────

def test_record_stats_collection_run_clasifica_canal(v50_db):
    db = _get_db(v50_db)
    run_id = db.record_stats_collection_run(1, "requires_authorization", deep=True,
                                            use_data_api=True, result={"reason": "no token"})
    assert run_id is not None
    latest = db.get_latest_stats_collection_run(1)
    assert latest["status"] == "requires_authorization"
    assert latest["deep"] == 1


# ── Reminders: solo alertan, una vez ────────────────────────────

def test_reminder_due_se_marca_alerted_una_vez(v50_db):
    db = _get_db(v50_db)
    rid = db.create_scheduled_reminder(
        "Revisión", "Verificar impresiones", due_at="2000-01-01T00:00:00",
        entity_id=42, metadata={"scope": "test"},
    )
    assert rid is not None
    due = db.get_due_scheduled_reminders(now_iso="2000-01-02T00:00:00")
    assert len(due) == 1
    assert due[0]["id"] == rid

    # Se marca alerted tras emitir la alerta…
    assert db.mark_scheduled_reminder_alerted(rid, alert_id=7) is True
    # …y ya NO vuelve a aparecer como due (no re-emite en silencio).
    assert db.get_due_scheduled_reminders(now_iso="2000-01-02T00:00:00") == []
    row = db.list_scheduled_reminders(status="alerted")
    assert len(row) == 1
    assert row[0]["alert_id"] == 7


def test_reminder_no_vencido_no_dispara(v50_db):
    db = _get_db(v50_db)
    db.create_scheduled_reminder("Futuro", "Aún no toca", due_at="2999-01-01T00:00:00", entity_id=43)
    assert db.get_due_scheduled_reminders(now_iso="2000-01-01T00:00:00") == []


def test_reminder_resuelto_por_operador(v50_db):
    db = _get_db(v50_db)
    rid = db.create_scheduled_reminder("Revisión", "msg", due_at="2000-01-01T00:00:00", entity_id=44)
    db.mark_scheduled_reminder_alerted(rid, alert_id=1)
    assert db.resolve_scheduled_reminder(rid) is True
    rows = db.list_scheduled_reminders(status="resolved")
    assert len(rows) == 1


# ── seo_summary: snapshot fiable + cobertura ────────────────────

def test_seo_summary_lee_snapshot_y_cobertura(v50_db):
    db = _get_db(v50_db)
    # Snapshot con datos reales (impresiones, CTR, retención, duración)
    db.insert_video_stats(1, "ABC123", {
        "viewCount": 100, "impressions": 1500, "ctr": 4.2,
        "averageViewPercentage": 19.3, "averageViewDuration": 120,
        "analytics_window_days": 365,
    })
    # Ledger: recolección completada
    db.record_stats_collection_run(1, "collected", deep=True, result={})

    with db._connect() as conn:
        summary = db._build_seo_summary(conn, [{"id": 1, "name": "Civilizaciones Olvidadas", "slug": "canal3"}])

    s = summary[1]
    assert s["total_impressions_30d"] == 1500
    assert abs(s["avg_ctr_30d"] - 4.2) < 0.001
    assert abs(s["avg_retention_30d"] - 19.3) < 0.001
    assert abs(s["avg_view_duration_30d"] - 120.0) < 0.001
    assert s["analytics_status"] == "collected"
    assert s["has_analytics_data"] is True


def test_seo_summary_sin_datos_estado_no_data(v50_db):
    db = _get_db(v50_db)
    with db._connect() as conn:
        summary = db._build_seo_summary(conn, [{"id": 1, "name": "Civilizaciones Olvidadas", "slug": "canal3"}])
    s = summary[1]
    assert s["has_analytics_data"] is False
    assert s["total_impressions_30d"] == 0
    assert s["analytics_status"] == "no_data"