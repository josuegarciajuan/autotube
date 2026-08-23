"""Tests de la Fábrica continua long-form (Fase 1).

Verifica:
  1. get_continuous_generation_candidates devuelve slots pendientes lejanos
     (sin ventana de 36h), ordenados por target_public_at ASC.
  2. continuous_generation_enabled: toggle por system_state > settings (default False).
  3. top_up_horizon extiende el horizonte hacia delante SIN borrar los slots
     existentes (incremental), hasta la cobertura objetivo.
"""

import json
import sqlite3

import pytest

from database.db import init_db
from database.db_extended import ExtendedDatabase
from api.services import planning_service as ps

_SYSTEM_STATE_DDL = """
CREATE TABLE IF NOT EXISTS system_state (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL DEFAULT '',
    updated_at  TEXT DEFAULT (datetime('now'))
)
"""


def _db(tmp_path):
    path = tmp_path / "cont_gen.db"
    init_db(str(path))
    with sqlite3.connect(str(path)) as conn:
        conn.executescript(_SYSTEM_STATE_DDL)
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS channels (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                slug TEXT NOT NULL UNIQUE,
                config_json TEXT NOT NULL DEFAULT '{}',
                active BOOLEAN NOT NULL DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS planned_slots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                channel_id INTEGER NOT NULL,
                date_key TEXT NOT NULL,
                scheduled_at TIMESTAMP NOT NULL,
                target_upload_at TIMESTAMP,
                target_public_at TIMESTAMP,
                upload_window_start INTEGER DEFAULT 9,
                upload_window_end INTEGER DEFAULT 11,
                source_mode TEXT DEFAULT 'original',
                status TEXT NOT NULL DEFAULT 'pending',
                job_id INTEGER,
                video_id INTEGER,
                slot_position INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS videos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                canal TEXT, channel_id INTEGER, video_path TEXT DEFAULT '',
                status TEXT DEFAULT 'draft', progress INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)
        # 2 canales activos con planning
        for ch_id, slug in ((1, "canal2"), (2, "canal3")):
            conn.execute(
                "INSERT INTO channels (id, name, slug, config_json) VALUES (?, ?, ?, ?)",
                (ch_id, slug, slug,
                 json.dumps({"videos_per_day": 1, "planning_enabled": True})),
            )
    return ExtendedDatabase(str(path))


def _seed_far_slot(db, channel_id: int, date_key: str, pub_at: str):
    with db._connect() as conn:
        conn.execute(
            """INSERT INTO planned_slots
               (channel_id, date_key, scheduled_at, target_upload_at, target_public_at, status)
               VALUES (?, ?, ?, ?, ?, 'pending')""",
            (channel_id, date_key, pub_at, pub_at, pub_at),
        )
        conn.commit()


def test_continuous_candidates_include_far_future_slots(tmp_path):
    db = _db(tmp_path)
    from datetime import date, timedelta
    far = (date.today() + timedelta(days=20)).isoformat()
    near = date.today().isoformat()
    _seed_far_slot(db, 1, far, f"{far}T12:00:00")
    _seed_far_slot(db, 2, near, f"{near}T09:00:00")

    candidates = db.get_continuous_generation_candidates(limit=10)
    assert len(candidates) == 2, "la selección continua NO tiene ventana temporal"
    # Orden: target_public_at ASC → el de hoy primero
    assert candidates[0]["channel_id"] == 2
    assert candidates[1]["channel_id"] == 1


def test_continuous_mode_disabled_by_default(tmp_path):
    db = _db(tmp_path)
    assert ps.continuous_generation_enabled(db) is False


def test_continuous_mode_enabled_via_system_state(tmp_path):
    db = _db(tmp_path)
    db.set_system_state("continuous_generation", "true")
    assert ps.continuous_generation_enabled(db) is True
    db.set_system_state("continuous_generation", "false")
    assert ps.continuous_generation_enabled(db) is False


def test_top_up_horizon_adds_slots_without_deleting(tmp_path):
    db = _db(tmp_path)
    from datetime import date, timedelta
    today = date.today()
    # Un slot pendiente de hace días (existente) que NO debe borrarse
    _seed_far_slot(db, 1, today.isoformat(), f"{today.isoformat()}T10:00:00")
    before = len(db.get_continuous_generation_candidates(limit=100))

    result = ps.top_up_horizon(db=db, horizon_days=7, max_ahead_days=10)
    assert result["added"] > 0, "el top-up debe añadir slots nuevos"

    after = len(db.get_continuous_generation_candidates(limit=100))
    assert after > before, "top-up incremental: no debe borrar slots existentes"
    # El slot preexistente sigue ahí
    ids_before = {s["id"] for s in db.get_continuous_generation_candidates(limit=100)}
    assert len(ids_before) == after


def test_top_up_is_idempotent_per_coverage(tmp_path):
    db = _db(tmp_path)
    r1 = ps.top_up_horizon(db=db, horizon_days=7, max_ahead_days=10)
    count1 = len(db.get_continuous_generation_candidates(limit=100))
    r2 = ps.top_up_horizon(db=db, horizon_days=7, max_ahead_days=10)
    count2 = len(db.get_continuous_generation_candidates(limit=100))
    assert r2["added"] == 0, "segunda pasada con cobertura completa: sin añadir"
    assert count1 == count2
