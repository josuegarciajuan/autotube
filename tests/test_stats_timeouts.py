"""Tests de deadlines cooperativos y transporte acotado (oct 2026).

Fase 2 anti-cuelgues:
  - build() de Google usa httplib2 con timeout (no socket infinito);
  - los batches comprueban el deadline ENTRE lotes y abortan ordenado;
  - yt-dlp lleva socket_timeout + reintentos acotados y la espera del batch
    es limitada (los no completados → is_mock, nunca métrica fabricada);
  - reach sync respeta el deadline entre informes descargados.
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

import pipeline.youtube_stats as ys
import pipeline.youtube_stats_scraper as sc
import pipeline.youtube_reach as rc


# ── Transporte Google acotado ────────────────────────────────

class _FakeCreds:
    # expired=False cortocircuita la rama de refresh (no se necesita el atributo)
    expired = False
    valid = True


def test_build_usa_http_acotado(tmp_path, monkeypatch):
    f = ys.YouTubeStatsFetcher("canal3")
    token_file = tmp_path / "canal3.pickle"
    token_file.write_bytes(b"stub")
    f._token_path = token_file

    built = {}
    seen_timeouts = []

    class FakeAuthorizedHttp:
        def __init__(self, creds, http=None):
            # google-auth 2.54: el wrapper canónico es AuthorizedHttp(creds, http)
            assert http is not None, "debe recibir transporte httplib2"
            seen_timeouts.append(http.timeout)
            self.timeout = http.timeout

    def fake_build(svc, ver, http=None, credentials=None, cache_discovery=False):
        # `http=` acotado es excluyente con `credentials=` (socket infinito)
        assert http is not None and credentials is None
        built[svc] = http
        return object()

    monkeypatch.setattr(ys, "AuthorizedHttp", FakeAuthorizedHttp)
    monkeypatch.setattr(ys, "build", fake_build)
    monkeypatch.setattr(ys.pickle, "load", lambda fp: _FakeCreds())
    assert f.authenticate()
    assert "youtube" in built and "youtubeAnalytics" in built
    assert seen_timeouts and all(t == ys.GOOGLE_HTTP_TIMEOUT_S for t in seen_timeouts)


def test_timeout_session_inyecta_timeout(monkeypatch):
    """El refresh OAuth (Request(session=...)) queda acotado por la sesión."""
    import requests
    seen = {}

    def fake_request(self, method, url, **kw):
        seen.update(kw)
        return "ok"

    monkeypatch.setattr(requests.Session, "request", fake_request)
    s = ys._TimeoutSession()
    assert s.request("GET", "http://example.invalid") == "ok"
    assert seen.get("timeout") == ys.GOOGLE_HTTP_TIMEOUT_S
    # un timeout explícito del llamador manda
    seen.clear()
    s.request("GET", "http://example.invalid", timeout=7)
    assert seen.get("timeout") == 7


# ── Deadline cooperativo en batches ──────────────────────────

def test_data_api_batch_deadline_entre_lotes():
    f = ys.YouTubeStatsFetcher("canal3")
    calls = []

    class FakeList:
        def __init__(self, **kw):
            self.kw = kw

        def execute(self, num_retries=0):
            calls.append(num_retries)
            time.sleep(0.05)
            return {"items": []}

    class FakeVideos:
        def list(self, **kw):
            return FakeList(**kw)

    f._service = type("S", (), {"videos": lambda self: FakeVideos()})()
    ids = [f"VID{i:03d}" for i in range(200)]  # 4 lotes de 50

    deadline = time.time() + 0.12  # vence tras ~2-3 lotes
    r = f.get_video_stats_batch(ids, deadline=deadline)

    assert r.get("_deadline_exceeded") is True
    assert len(calls) < 4, "debe abortar antes de procesar todos los lotes"
    assert all(v in r for v in ids), "todos los IDs deben aparecer"
    assert all(r[v].get("is_mock") for v in ids), "sin datos → is_mock, nunca fabricado"


def test_bulk_analytics_deadline_devuelve_parcial():
    f = ys.YouTubeStatsFetcher("canal3")

    class FakeQuery:
        def __init__(self, **kw):
            time.sleep(0.05)

        def execute(self, num_retries=0):
            return {"rows": []}

    class FakeReports:
        def query(self, **kw):
            return FakeQuery(**kw)

    f._analytics_service = type("S", (), {"reports": lambda self: FakeReports()})()
    deadline = time.time() + 0.12
    r = f.get_all_videos_analytics([f"V{i}" for i in range(600)], deadline=deadline)
    assert isinstance(r, dict)  # parcial explícito, sin excepción


# ── yt-dlp: opciones acotadas + deadline de espera ───────────

def test_scraper_opts_con_socket_timeout(tmp_path, monkeypatch):
    s = sc.YouTubeStatsScraper("canal3", max_concurrency=2)
    captured = {}

    class FakeYDL:
        def __init__(self, opts):
            captured.update(opts)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def extract_info(self, url, download=False):
            raise RuntimeError("no network in test")

    monkeypatch.setattr("yt_dlp.YoutubeDL", FakeYDL)
    # _scrape_one_video captura los fallos del extractor → is_mock (nunca lanza)
    out = s._scrape_one_video("ABC")
    assert out.get("is_mock") is True
    assert captured.get("socket_timeout") == sc.YTDLP_SOCKET_TIMEOUT_S
    assert captured.get("retries") == 1
    assert captured.get("extractor_retries") == 1


def test_scraper_batch_deadline_aborta_espera():
    """Trabajos lentos con deadline corto → aborta pronto; los completados
    conservan datos reales, el resto is_mock."""
    s = sc.YouTubeStatsScraper("canal3", max_concurrency=2)

    class SlowYDL:
        def __init__(self, opts):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def extract_info(self, url, download=False):
            time.sleep(0.4)
            return {"view_count": 7, "like_count": 0, "comment_count": 0}

    import yt_dlp
    orig = yt_dlp.YoutubeDL
    yt_dlp.YoutubeDL = SlowYDL
    try:
        ids = [f"V{i}" for i in range(10)]
        t0 = time.time()
        r = s.get_video_stats_batch(ids, deadline=time.time() + 1.2)
        dt = time.time() - t0
        assert r.get("_deadline_exceeded") is True
        assert all(v in r for v in ids)
        real_n = sum(1 for v in ids if not r[v].get("is_mock"))
        assert real_n >= 1, "los ya completados conservan datos reales"
        assert dt < 5, f"la espera debe ser acotada, tardó {dt:.1f}s"
    finally:
        yt_dlp.YoutubeDL = orig


def test_scraper_batch_deadline_prevencido_no_trabaja():
    s = sc.YouTubeStatsScraper("canal3", max_concurrency=2)

    class BoomYDL:
        def __init__(self, opts):
            raise AssertionError("no debe abrir conexiones con deadline vencido")

    import yt_dlp
    orig = yt_dlp.YoutubeDL
    yt_dlp.YoutubeDL = BoomYDL
    try:
        r = s.get_video_stats_batch(["A", "B"], deadline=time.time() - 1)
        assert r.get("_deadline_exceeded") is True
        assert all(r[v].get("is_mock") for v in ("A", "B"))
    finally:
        yt_dlp.YoutubeDL = orig


# ── Reach: deadline entre informes ───────────────────────────

class _FakeReachDB:
    def get_channel_by_slug(self, slug):
        return {"id": 1}

    def reach_report_seen(self, rid):
        return False

    def mark_reach_report_seen(self, *a):
        pass

    def upsert_video_reach_daily(self, *a, **k):
        pass

    def upsert_video_reach_traffic_daily(self, *a, **k):
        pass


def test_reach_sync_respeta_deadline_entre_informes():
    c = rc.ReachReportClient.__new__(rc.ReachReportClient)
    c.slug = "canal3"
    c._service = object()
    c.api_disabled = False
    c.disabled_hint = ""
    c._report_types = {}

    n_downloads = [0]

    def fake_download(url):
        n_downloads[0] += 1
        time.sleep(0.4)  # descarga 'lenta'
        return "yt_video_id,date\nABC,2026-01-01\n"

    c.download_report = fake_download
    c.list_reports = lambda job_id, limit=200: [
        {"id": f"r{i}", "downloadUrl": f"url{i}"} for i in range(20)
    ]
    c.ensure_jobs = lambda: {"channel_reach_basic_a1": "job1"}

    deadline = time.time() + 1.0  # permite ~2-3 descargas
    summary = c.sync(_FakeReachDB(), max_reports_per_job=10, deadline=deadline)
    assert summary.get("deadline_exceeded") is True
    assert n_downloads[0] < 20, "debe dejar el resto para la siguiente pasada"


def test_scraper_deadline_no_espera_a_los_stragglers():
    """Regresión oct 2026: el `with ThreadPoolExecutor` esperaba (shutdown
    wait=True) a TODOS los trabajos ya lanzados, anulando el deadline. Un
    vídeo lento podía retener la recolección mucho más allá del límite."""
    s = sc.YouTubeStatsScraper("canal3", max_concurrency=1)

    class SlowYDL:
        def __init__(self, opts):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def extract_info(self, url, download=False):
            time.sleep(3.0)
            return {"view_count": 7, "like_count": 0, "comment_count": 0}

    import yt_dlp
    orig = yt_dlp.YoutubeDL
    yt_dlp.YoutubeDL = SlowYDL
    try:
        t0 = time.time()
        r = s.get_video_stats_batch(["A", "B", "C"], deadline=time.time() + 0.2)
        dt = time.time() - t0
        assert r.get("_deadline_exceeded") is True
        assert all(v in r for v in ("A", "B", "C"))
        assert dt < 2.0, f"debe retornar al vencer el deadline, tardó {dt:.1f}s"
    finally:
        yt_dlp.YoutubeDL = orig
