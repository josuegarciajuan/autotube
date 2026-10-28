"""Tests del sync de reach reports acotado y con escrituras en lote (oct 2026).

Causa raíz de la recolección de stats "eterna":
  - cada fila de un informe hacía su propia conexión + 8 PRAGMAs + commit
    (un informe de 200 filas = 200 transacciones contra SQLite bajo contención);
  - `reach_report_seen` se consultaba con una conexión por informe;
  - no había tope global: 3 jobs × 90 informes podían monopolizar la fase.

Estos tests garantizan:
  - upserts en lote con la MISMA semántica COALESCE que el upsert individual;
  - una sola consulta de IDs ya vistos por job;
  - tope global por pasada (deja pendientes para la siguiente);
  - el sync no usa el upsert fila-a-fila cuando existe el de lote.
"""
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

import pipeline.youtube_reach as rc


_SCHEMA = """
CREATE TABLE video_reach_daily (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id INTEGER NOT NULL,
    yt_video_id TEXT NOT NULL,
    date TEXT NOT NULL,
    impressions INTEGER DEFAULT 0,
    impressions_ctr REAL DEFAULT 0,
    retention_pct REAL DEFAULT 0,
    watch_minutes REAL DEFAULT 0,
    subs_gained INTEGER DEFAULT 0,
    subs_lost INTEGER DEFAULT 0,
    engaged_views INTEGER DEFAULT 0,
    views INTEGER DEFAULT 0,
    traffic_source TEXT,
    source TEXT DEFAULT 'reporting_api',
    fetched_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(channel_id, yt_video_id, date)
);
CREATE TABLE video_reach_traffic_daily (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id INTEGER NOT NULL,
    yt_video_id TEXT NOT NULL,
    date TEXT NOT NULL,
    traffic_source TEXT NOT NULL,
    views INTEGER DEFAULT 0,
    watch_minutes REAL DEFAULT 0,
    avg_view_duration_pct REAL DEFAULT 0,
    fetched_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(channel_id, yt_video_id, date, traffic_source)
);
CREATE TABLE reach_reports_seen (
    report_id TEXT PRIMARY KEY,
    job_id TEXT,
    report_type_id TEXT,
    downloaded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = tmp_path / "reach.db"
    conn = sqlite3.connect(str(path))
    conn.executescript(_SCHEMA)
    conn.commit()
    conn.close()

    from database.db_extended import ExtendedDatabase
    return ExtendedDatabase(db_path=str(path))


def _row(channel_id=1, vid="ABC", date="2026-10-01", **kw):
    base = {"channel_id": channel_id, "yt_video_id": vid, "date": date}
    base.update(kw)
    return base


# ── Upserts en lote ──────────────────────────────────────────

def test_batch_upsert_conserva_campos_no_aportados(db):
    """reach y basic escriben campos distintos en pasadas distintas: el lote
    debe comportarse como el upsert individual (COALESCE, no ceros)."""
    db.upsert_video_reach_daily_batch([
        _row(impressions=1000, impressions_ctr=4.5, source="reporting_reach"),
    ])
    db.upsert_video_reach_daily_batch([
        _row(retention_pct=42.0, watch_minutes=90.0, views=120,
             subs_gained=3, subs_lost=1, engaged_views=50,
             source="reporting_basic"),
    ])

    with db._connect() as conn:
        row = conn.execute(
            "SELECT impressions, impressions_ctr, retention_pct, watch_minutes, "
            "views, subs_gained, source FROM video_reach_daily"
        ).fetchone()
    assert row["impressions"] == 1000, "el basic report no debe pisar impresiones"
    assert row["impressions_ctr"] == 4.5
    assert row["retention_pct"] == 42.0
    assert row["views"] == 120
    assert row["source"] == "reporting_basic"


def test_batch_upsert_normaliza_fecha(db):
    db.upsert_video_reach_daily_batch([_row(date="20261001", impressions=10)])
    with db._connect() as conn:
        got = conn.execute("SELECT date FROM video_reach_daily").fetchone()[0]
    assert got == "2026-10-01"


def test_batch_upsert_traffic_es_idempotente(db):
    rows = [
        {"channel_id": 1, "yt_video_id": "ABC", "date": "2026-10-01",
         "traffic_source": "YT_SEARCH", "views": 10, "watch_minutes": 5,
         "avg_view_duration_pct": 33.0},
        {"channel_id": 1, "yt_video_id": "ABC", "date": "2026-10-01",
         "traffic_source": "SUGGESTED", "views": 7, "watch_minutes": 2,
         "avg_view_duration_pct": 20.0},
    ]
    assert db.upsert_video_reach_traffic_daily_batch(rows) == 2
    # reprocesar reemplaza (no acumula)
    db.upsert_video_reach_traffic_daily_batch([
        {**rows[0], "views": 99},
    ])
    with db._connect() as conn:
        got = dict(conn.execute(
            "SELECT traffic_source, views FROM video_reach_traffic_daily"
        ).fetchall())
    assert got == {"YT_SEARCH": 99, "SUGGESTED": 7}


def test_seen_ids_en_una_sola_consulta(db):
    with db._connect() as conn:
        conn.executemany(
            "INSERT INTO reach_reports_seen(report_id, job_id, report_type_id) "
            "VALUES (?, ?, ?)",
            [("r1", "job1", rc.REPORT_TYPE_REACH),
             ("r2", "job2", rc.REPORT_TYPE_BASIC)],
        )
        conn.commit()
    assert db.get_seen_reach_report_ids("job1") == {"r1"}
    assert db.get_seen_reach_report_ids() == {"r1", "r2"}


# ── Sync: lote, tope por pasada y no N+1 ─────────────────────

class _CountingDB:
    """Envuelve la DB contando llamadas a los upserts individuales."""

    def __init__(self, real):
        self.real = real
        self.single_calls = 0
        self.batch_calls = 0

    def get_channel_by_slug(self, slug):
        return {"id": 1, "slug": slug}

    def get_seen_reach_report_ids(self, job_id=None):
        return self.real.get_seen_reach_report_ids(job_id)

    def reach_report_seen(self, report_id):
        raise AssertionError("no debe consultarse fila a fila con el set disponible")

    def mark_reach_report_seen(self, *a, **k):
        return self.real.mark_reach_report_seen(*a, **k)

    def upsert_video_reach_daily_batch(self, rows):
        self.batch_calls += 1
        return self.real.upsert_video_reach_daily_batch(rows)

    def upsert_video_reach_daily(self, *a, **k):
        self.single_calls += 1
        return self.real.upsert_video_reach_daily(*a, **k)

    def upsert_video_reach_traffic_daily_batch(self, rows):
        self.batch_calls += 1
        return self.real.upsert_video_reach_traffic_daily_batch(rows)

    def upsert_video_reach_traffic_daily(self, *a, **k):
        self.single_calls += 1


class _FakeClient(rc.ReachReportClient):
    """Cliente sin red: hereda `sync` real y sustituye solo las llamadas API."""

    def __init__(self, reports_by_job, csv_text):
        self.slug = "canal3"
        self._service = object()
        self.api_disabled = False
        self.disabled_hint = ""
        self._reports = reports_by_job
        self._csv = csv_text
        self.downloads = 0

    def ensure_jobs(self):
        return {rid: f"job-{rid}" for rid in self._reports}

    def list_reports(self, job_id, limit=200):
        rid = job_id.removeprefix("job-")
        return self._reports.get(rid, [])

    def download_report(self, url):
        self.downloads += 1
        return self._csv

    @staticmethod
    def parse_reach(text):
        return [{"yt_video_id": f"V{i}", "date": "20261001",
                 "impressions": 100 + i, "impressions_ctr": 4.0}
                for i in range(5)]

    @staticmethod
    def parse_basic(text):
        return [{"yt_video_id": "V1", "date": "20261001", "retention_pct": 40.0,
                 "watch_minutes": 10.0, "views": 5, "subs_gained": 1,
                 "subs_lost": 0, "engaged_views": 2}]

    @staticmethod
    def parse_traffic(text):
        return [{"yt_video_id": "V1", "date": "20261001",
                 "traffic_source": "YT_SEARCH", "views": 3,
                 "watch_minutes": 1.0, "avg_view_duration_pct": 30.0}]


def _client(reports_by_job, csv_text="x"):
    c = _FakeClient(reports_by_job, csv_text)
    # `sync` usa self.slug/_service/api_disabled/disabled_hint y los métodos.
    return c


def test_sync_escribe_en_lote_y_no_fila_a_fila(db):
    cdb = _CountingDB(db)
    reports = {"channel_reach_basic_a1": [
        {"id": "r1", "downloadUrl": "u"},
        {"id": "r2", "downloadUrl": "u"},
    ]}
    client = _client(reports)
    out = client.sync(cdb, max_reports_per_run=0)

    assert out["reports_downloaded"] == 2
    assert out["reach_rows"] == 10  # 2 informes × 5 filas
    assert cdb.batch_calls == 2, "un lote por informe"
    assert cdb.single_calls == 0, "nunca upsert fila a fila con batch disponible"
    with db._connect() as conn:
        n = conn.execute("SELECT COUNT(*) FROM video_reach_daily").fetchone()[0]
    # Los dos informes del fake traen los mismos vídeos/día → upsert idempotente.
    assert n == 5


def test_sync_tope_global_por_pasada(db):
    """10 informes nuevos, tope 3 → descarga 3 y deja 7 pendientes."""
    cdb = _CountingDB(db)
    reports = {"channel_reach_basic_a1": [
        {"id": f"r{i}", "downloadUrl": "u"} for i in range(10)
    ]}
    client = _client(reports)
    out = client.sync(cdb, max_reports_per_run=3)

    assert out["reports_downloaded"] == 3
    assert out["reports_pending"] == 7
    assert client.downloads == 3


def test_sync_tope_cuenta_todos_los_jobs(db):
    """El tope es global (suma de los 3 jobs), no por job."""
    cdb = _CountingDB(db)
    reports = {
        rc.REPORT_TYPE_REACH: [{"id": "r1", "downloadUrl": "u"}],
        rc.REPORT_TYPE_BASIC: [{"id": "b1", "downloadUrl": "u"}],
        rc.REPORT_TYPE_TRAFFIC: [{"id": "t1", "downloadUrl": "u"}],
    }
    client = _client(reports)
    out = client.sync(cdb, max_reports_per_run=2)

    assert out["reports_downloaded"] == 2
    assert client.downloads == 2
    assert out["reports_pending"] == 1
