"""Tests de la cola unificada de shorts (native + clip + standalone).

Verifica el modelo "generar → cola → válvula de goteo" implementado en
ago 2026:

  1. El tope duro por canal cuenta por `published_at` (fecha de SUBIDA), no por
     `created_at` (fecha de GENERACIÓN) — shorts de cola de días previos ya no
     se escapan del cap de 1/día.
  2. `get_queued_shorts` devuelve los tres tipos (native/standalone
     'generated', clip 'ready').
  3. La válvula respeta el tope global diario y el tope duro por canal.
  4. El cooldown de la válvula se basa en la última SUBIDA, no en la última
     generación.
  5. La válvula corre aunque el tope global esté alcanzado (no lo frena el
     early-return del dispatch), pero no supera el tope.
  6. Un slot native due se despacha con generate_only=True (a cola).
  7. El clip pre-renderizado (status='ready') NO se sube al despacharse: queda
     en cola para la válvula.
"""

import json
import sqlite3
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from database.db import init_db
from database.db_extended import ExtendedDatabase, _migrate_v48
from api.services import shorts_scheduler as ss
from pipeline import shorts_cross_promote as scp


def test_only_native_short_type_is_allowed():
    """Clips and legacy standalone rows cannot enter the upload pipeline."""
    assert ss.short_type_allowed("native") is True
    assert ss.short_type_allowed("clip") is False
    assert ss.short_type_allowed("standalone") is False


def _db(tmp_path):
    path = tmp_path / "queue_unified.db"
    init_db(str(path))
    with sqlite3.connect(str(path)) as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS system_state (
                key         TEXT PRIMARY KEY,
                value       TEXT NOT NULL DEFAULT '',
                updated_at  TEXT DEFAULT (datetime('now'))
            );
            CREATE TABLE IF NOT EXISTS channels (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE, slug TEXT NOT NULL UNIQUE,
                config_json TEXT NOT NULL DEFAULT '{}',
                active BOOLEAN NOT NULL DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS shorts_planned_slots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                channel_id INTEGER NOT NULL,
                date_key TEXT NOT NULL,
                scheduled_at TIMESTAMP NOT NULL,
                target_upload_at TIMESTAMP,
                short_type TEXT NOT NULL DEFAULT 'native',
                status TEXT NOT NULL DEFAULT 'pending',
                job_id INTEGER, short_id INTEGER,
                long_slot_position INTEGER, source_video_id INTEGER,
                slot_position INTEGER DEFAULT 0, slot_rank INTEGER DEFAULT 0,
                retry_count INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS generation_jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                channel_id INTEGER, video_id INTEGER,
                action TEXT DEFAULT 'generate_native_short',
                status TEXT DEFAULT 'queued',
                progress INTEGER DEFAULT 0, phase TEXT, error_msg TEXT,
                started_at TIMESTAMP, finished_at TIMESTAMP,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS shorts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                channel_id INTEGER, type TEXT DEFAULT 'native',
                title TEXT, status TEXT DEFAULT 'pending',
                file_path TEXT DEFAULT '', published_at TIMESTAMP,
                youtube_id TEXT, youtube_url TEXT, hook_text TEXT,
                topic TEXT, has_subscribe_cta INTEGER DEFAULT 1,
                source_video_id INTEGER, hook_title TEXT,
                error_message TEXT, longform_linked INTEGER DEFAULT 0,
                longform_linked_at TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS videos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                canal TEXT, channel_id INTEGER, video_path TEXT DEFAULT '',
                status TEXT DEFAULT 'draft', progress INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)
        for ch_id, slug in ((1, "canal2"), (2, "canal3")):
            conn.execute(
                "INSERT OR REPLACE INTO channels (id, name, slug, config_json) VALUES (?, ?, ?, ?)",
                (ch_id, slug, slug, json.dumps({"videos_per_day": 1})),
            )
    # v48: añade columnas de estado real de publicación (scheduled / publish_at /
    # yt_visibility / actual_published_at). La válvula las escribe al subir.
    import logging as _logging
    with sqlite3.connect(str(path)) as conn:
        _migrate_v48(conn, _logging.getLogger("test"))
    return ExtendedDatabase(str(path)), str(path)


def _seed_queued(db, channel_id: int, n: int, short_type: str = "native",
                 status: str = "generated", file_path: str | None = None):
    from pathlib import Path
    with db._connect() as conn:
        for i in range(n):
            fp = file_path or f"/tmp/queued_unified_{channel_id}_{i}.mp4"
            Path(fp).write_bytes(b"fake")
            conn.execute(
                """INSERT INTO shorts (channel_id, type, title, status, file_path)
                   VALUES (?, ?, ?, ?, ?)""",
                (channel_id, short_type, f"{short_type}-{i}", status, fp),
            )
        conn.commit()


def _set_published(db, channel_id: int, n: int, hours_ago: float = 0,
                   created_hours_ago: float | None = None):
    """Inserta shorts 'published' con youtube_id y published_at controlados."""
    with db._connect() as conn:
        pub = (datetime.now() - timedelta(hours=hours_ago)).strftime("%Y-%m-%d %H:%M:%S")
        for i in range(n):
            created = pub
            if created_hours_ago is not None:
                created = (datetime.now() - timedelta(hours=created_hours_ago)).strftime("%Y-%m-%d %H:%M:%S")
            conn.execute(
                """INSERT INTO shorts (channel_id, type, title, status, file_path,
                                       youtube_id, published_at, created_at)
                   VALUES (?, 'native', ?, 'published', '/tmp/y.mp4', ?, ?, ?)""",
                (channel_id, f"pub-{i}", f"yt{i}", pub, created),
            )
        conn.commit()


def test_clip_shorts_are_globally_disabled():
    assert ss.CLIP_SHORTS_ENABLED is False
    assert ss.short_type_allowed("native") is True
    assert ss.short_type_allowed("standalone") is False
    assert ss.short_type_allowed("clip") is False


def test_zero_clip_config_value_is_preserved():
    assert ss.configured_clip_count({"shorts_clips_per_long": 0}) == 0
    assert ss.configured_clip_count({"shorts_clips_per_long": 2}) == 2


def test_clip_shorts_are_not_returned_from_upload_queue(tmp_path):
    db, _ = _db(tmp_path)
    _seed_queued(db, 1, 1, short_type="clip", status="ready")
    _seed_queued(db, 1, 1, short_type="native", status="generated")

    queued = db.get_queued_shorts(1, limit=10)

    assert [row["type"] for row in queued] == ["native"]


@pytest.fixture
def patch_database_path(tmp_path, monkeypatch):
    """Crea un DB de test y apunta config.settings.DATABASE_PATH a él (las
    funciones de cap usan esa constante global, no el parámetro db)."""
    db, path = _db(tmp_path)
    import config.settings as settings
    monkeypatch.setattr(settings, "DATABASE_PATH", __import__("pathlib").Path(path))
    return db, path


@pytest.fixture
def fake_uploader(monkeypatch):
    """Fake YouTubeUploader que registra subidas sin tocar la red."""
    calls = {"uploads": [], "authenticated": False}

    class _FakeUploader:
        def __init__(self, account_name=None, channel_slug=None):
            pass

        def authenticate(self):
            calls["authenticated"] = True
            return True

        def upload(self, **kwargs):
            calls["uploads"].append(kwargs)
            return {"video_id": f"fake-{len(calls['uploads'])}", "url": "https://youtu.be/x"}

    monkeypatch.setattr("pipeline.youtube_uploader.YouTubeUploader", _FakeUploader)
    # Evitar cross-promote/descripciones reales
    monkeypatch.setattr(scp, "run_post_publish_promotion", lambda **kw: None)
    monkeypatch.setattr(scp, "build_short_description", lambda **kw: "#Shorts")
    monkeypatch.setattr(scp, "get_best_longform_link", lambda *a, **kw: None)
    # Evitar gates externos
    monkeypatch.setattr(ss, "_youtube_quota_blocked", lambda *a, **kw: False)
    monkeypatch.setattr(ss, "_safe_publish_at", lambda *a, **kw: None)
    import config.config_bridge as cbr
    monkeypatch.setattr(
        cbr, "get_channel_config",
        lambda slug: SimpleNamespace(
            SHORTS_HASHTAGS=["#Shorts"], YOUTUBE_CHANNEL_URL="",
            SHORTS_DESCRIPTION_LINK_ENABLED=False, YT_CATEGORY_ID="24",
            AUTO_MARK_ALTERED_CONTENT=False,
        ),
    )
    return calls


# ── 1. Tope duro por published_at ────────────────────────────────

def test_hard_cap_counts_by_published_at(patch_database_path):
    """Short de cola subido hoy (published_at hoy, created_at ayer) SÍ cuenta."""
    db, path = patch_database_path
    _set_published(db, 1, 1, hours_ago=0, created_hours_ago=30)
    assert ss._channel_hard_daily_short_cap_reached(1, db) is True


def test_hard_cap_ignores_created_at_today_not_published(patch_database_path):
    """Short con created_at hoy pero publicado AYER no cuenta para hoy."""
    db, path = patch_database_path
    _set_published(db, 1, 1, hours_ago=30, created_hours_ago=1)
    assert ss._channel_hard_daily_short_cap_reached(1, db) is False


def test_hard_cap_zero_when_no_uploads(patch_database_path):
    db, path = patch_database_path
    _seed_queued(db, 1, 1)
    assert ss._channel_hard_daily_short_cap_reached(1, db) is False


# ── 2. get_queued_shorts unificado ───────────────────────────────

def test_queued_shorts_returns_only_allowed_types(tmp_path):
    db, _ = _db(tmp_path)
    _seed_queued(db, 1, 1, short_type="native", status="generated")
    _seed_queued(db, 1, 1, short_type="native", status="generated")
    _seed_queued(db, 1, 1, short_type="clip", status="ready")
    queued = db.get_queued_shorts(1, limit=10)
    types = {q["type"] for q in queued}
    assert types == {"native"}
    # La variante antigua (compat) delega en la unificada.
    assert len(db.get_queued_native_shorts(1, limit=10)) == 2


def test_queued_shorts_excludes_published(tmp_path):
    db, _ = _db(tmp_path)
    _seed_queued(db, 1, 1, short_type="native", status="generated")
    _set_published(db, 1, 1)
    queued = db.get_queued_shorts(1, limit=10)
    assert len(queued) == 1
    assert queued[0]["status"] == "generated"


# ── 3. Válvula: respeta topes ────────────────────────────────────

def test_valve_respects_global_cap(patch_database_path, fake_uploader):
    db, path = patch_database_path
    # Tope global = 6 (strike). Publicar 6 hoy → la válvula no sube nada.
    for ch in (1, 2):
        _set_published(db, ch, 3)
    _seed_queued(db, 1, 2)
    uploaded = ss._upload_queued_shorts(db, max_per_pass=10)
    assert uploaded == 0
    assert fake_uploader["uploads"] == []


def test_valve_respects_hard_cap_per_channel(patch_database_path, fake_uploader):
    db, path = patch_database_path
    # Tope duro = 1/día. Ya se subió 1 hoy → la válvula no sube más.
    _set_published(db, 1, 1)
    _seed_queued(db, 1, 2)
    uploaded = ss._upload_queued_shorts(db, max_per_pass=10)
    assert uploaded == 0


def test_valve_uploads_queued_when_caps_allow(patch_database_path, fake_uploader):
    db, path = patch_database_path
    _seed_queued(db, 1, 2, short_type="native", status="generated")
    uploaded = ss._upload_queued_shorts(db, max_per_pass=1)
    assert uploaded == 1
    assert len(fake_uploader["uploads"]) == 1
    with db._connect() as conn:
        row = conn.execute(
            "SELECT status, youtube_id FROM shorts WHERE channel_id = 1 ORDER BY id LIMIT 1",
        ).fetchone()
        assert row["status"] == "published"
    assert row["youtube_id"] == "fake-1"


def test_valve_uploads_native_immediately_without_publish_at(patch_database_path, fake_uploader):
    db, path = patch_database_path
    _seed_queued(db, 1, 1, short_type="native", status="generated")
    assert ss._upload_queued_shorts(db, max_per_pass=1) == 1
    assert fake_uploader["uploads"][0]["privacy"] == "public"
    assert fake_uploader["uploads"][0]["publish_at"] is None


# ── 4. Cooldown por última subida ────────────────────────────────

def test_valve_cooldown_blocks_after_recent_upload(patch_database_path, fake_uploader):
    db, path = patch_database_path
    # Última subida hace 10 min (cooldown 180 min) → la válvula no sube.
    _set_published(db, 1, 1, hours_ago=10 / 60)
    _seed_queued(db, 1, 1)
    uploaded = ss._upload_queued_shorts(db, max_per_pass=10)
    assert uploaded == 0


def test_valve_cooldown_passes_after_gap(patch_database_path, fake_uploader):
    db, path = patch_database_path
    # Última subida hace 26h: pasa el cooldown (> 180 min) Y es de ayer, por lo que
    # NO consume el tope duro de 1 subida/día → la válvula puede subir hoy.
    _set_published(db, 1, 1, hours_ago=26)
    _seed_queued(db, 1, 1)
    uploaded = ss._upload_queued_shorts(db, max_per_pass=10)
    assert uploaded == 1


# ── 5. La válvula corre aunque el tope global esté alcanzado ─────

def test_valve_runs_even_when_global_cap_reached(tmp_path, monkeypatch):
    db, path = _db(tmp_path)
    import config.settings as settings
    monkeypatch.setattr(settings, "DATABASE_PATH", __import__("pathlib").Path(path))
    # Tope global alcanzado (6 publicados hoy)
    for ch in (1, 2):
        _set_published(db, ch, 3)
    calls = {"n": 0}

    def _spy(*a, **kw):
        calls["n"] += 1
        return 0

    monkeypatch.setattr(ss, "_upload_queued_shorts", _spy)
    monkeypatch.setattr(ss, "_youtube_quota_blocked", lambda *a, **kw: False)
    monkeypatch.setattr(ss, "_fill_native_short_queue", lambda *a, **kw: None)
    ss.dispatch_next_due_shorts_slot(db=db)
    assert calls["n"] == 1, "la válvula debe ejecutarse aunque el tope global esté alcanzado"


# ── 6. Slot native due → generate_only ───────────────────────────

def test_native_slot_dispatches_generate_only(tmp_path, monkeypatch):
    import asyncio
    db, path = _db(tmp_path)
    import config.settings as settings
    monkeypatch.setattr(settings, "DATABASE_PATH", __import__("pathlib").Path(path))

    # Slot native due (scheduled_at en el pasado)
    with db._connect() as conn:
        conn.execute(
            """INSERT INTO shorts_planned_slots
               (channel_id, date_key, scheduled_at, target_upload_at, short_type, status)
               VALUES (1, '2026-08-24', datetime('now','-1 hour'), datetime('now','+2 hours'), 'native', 'pending')""",
        )
        conn.commit()

    captured = {}

    async def _spy_dispatch(**kwargs):
        captured.update(kwargs)
        return 1

    monkeypatch.setattr(ss, "_dispatch_short_async", _spy_dispatch)
    monkeypatch.setattr(ss, "_upload_queued_shorts", lambda *a, **kw: 0)
    monkeypatch.setattr(ss, "_youtube_quota_blocked", lambda *a, **kw: False)
    monkeypatch.setattr(ss, "_fill_native_short_queue", lambda *a, **kw: None)
    # Evitar topes que bloqueen la generación
    monkeypatch.setattr(ss, "_channel_hard_daily_short_cap_reached", lambda *a, **kw: False)
    monkeypatch.setattr(ss, "_global_shorts_daily_cap_reached", lambda *a, **kw: False)

    loop = asyncio.new_event_loop()
    try:
        ss.dispatch_next_due_shorts_slot(db=db, loop=loop)
        # Ejecutar el loop para que el coroutine spy (programado con
        # run_coroutine_threadsafe) capture los kwargs.
        loop.run_until_complete(asyncio.sleep(0.05))
    finally:
        loop.close()
    assert captured.get("short_type") == "native"
    assert captured.get("generate_only") is True, "el native debe despacharse a cola (generate_only)"


# ── 7. Clip pre-renderizado NO se sube al despacharse ────────────

def test_clip_generation_is_rejected_even_for_pre_rendered_input(tmp_path, monkeypatch):
    from pathlib import Path
    import config.settings as settings
    db, path = _db(tmp_path)
    monkeypatch.setattr(settings, "DATABASE_PATH", __import__("pathlib").Path(path))
    video_path = tmp_path / "preclip.mp4"
    video_path.write_bytes(b"fake")
    with db._connect() as conn:
        conn.execute(
            """INSERT INTO shorts (channel_id, type, title, status, file_path, source_video_id)
               VALUES (1, 'clip', 'clip-ready', 'ready', ?, 99)""",
            (str(video_path),),
        )
        short_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
        conn.commit()
    short_id = int(short_id)
    result = ss._dispatch_clip_short(
        1, "canal2", source_video_id=99, pre_rendered_short_id=short_id,
    )
    assert result is None
    with db._connect() as conn:
        row = conn.execute("SELECT status, youtube_id FROM shorts WHERE id = ?", (short_id,)).fetchone()
        assert row["status"] == "ready", "el clip no debe procesarse"
        assert row["youtube_id"] is None
