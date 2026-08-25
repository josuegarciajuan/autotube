"""Tests del perfil central de cadencia (pacing_profile) — "strike mode" switch.

Verifica:
  1. El perfil por defecto es "strike" con los valores antiban actuales.
  2. set_pacing_profile persiste y cambia los valores resueltos.
  3. get_pacing_value aplica override manual (pacing_<key>) por encima del perfil.
  4. Perfiles inválidos lanzan ValueError.
  5. El toggle strike ↔ normal relaja todas las claves de golpe.
"""

import sqlite3

import pytest

from database.db import init_db
from database.db_extended import ExtendedDatabase
from api.services import pacing_profile

_SYSTEM_STATE_DDL = """
CREATE TABLE IF NOT EXISTS system_state (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL DEFAULT '',
    updated_at  TEXT DEFAULT (datetime('now'))
)
"""


def _db(tmp_path):
    path = tmp_path / "pacing.db"
    init_db(str(path))
    with sqlite3.connect(str(path)) as conn:
        conn.executescript(_SYSTEM_STATE_DDL)
    return ExtendedDatabase(str(path))


def _reset_state(db):
    db.set_system_state("pacing_profile", "")
    for key in pacing_profile.PACING_PROFILES["strike"]:
        db.set_system_state(f"pacing_{key}", "")


def test_default_profile_is_strike_with_antiban_values(tmp_path):
    db = _db(tmp_path)
    _reset_state(db)
    assert pacing_profile.get_active_profile_name(db) == "strike"
    pacing = pacing_profile.get_pacing(db)
    # Valores antiban actuales (ago 2026) — NO deben cambiar el comportamiento.
    assert pacing["shorts_per_channel_day"] == 1
    assert pacing["max_longform_publish_day"] == 1
    assert pacing["same_channel_publish_gap_h"] == 24
    assert pacing["same_channel_upload_gap_h"] == 6
    assert pacing["global_upload_spacing_min"] == 45
    assert pacing["account_daily_upload_cap"] == 4
    assert pacing["shorts_cooldown_min"] == 180
    assert pacing["content_safety_disabled"] is False


def test_set_profile_persists_and_resolves(tmp_path):
    db = _db(tmp_path)
    _reset_state(db)
    resolved = pacing_profile.set_pacing_profile("normal", db)
    assert pacing_profile.get_active_profile_name(db) == "normal"
    assert resolved["shorts_per_channel_day"] == 3
    assert resolved["max_longform_publish_day"] == 1
    assert resolved["same_channel_publish_gap_h"] == 6
    assert resolved["global_upload_spacing_min"] == 20
    assert resolved["account_daily_upload_cap"] == 8
    # Persistido en system_state → sobrevive un reinicio (nuevo get_pacing)
    pacing = pacing_profile.get_pacing(db)
    assert pacing["shorts_per_channel_day"] == 3


def test_profile_relaxes_all_keys_at_once(tmp_path):
    db = _db(tmp_path)
    _reset_state(db)
    strike = pacing_profile.get_pacing(db)
    pacing_profile.set_pacing_profile("normal", db)
    normal = pacing_profile.get_pacing(db)
    # El máximo de long-form normal permanece fijado por anti-spam.
    assert normal["shorts_per_channel_day"] > strike["shorts_per_channel_day"]
    assert normal["max_longform_publish_day"] == strike["max_longform_publish_day"] == 1
    for key in ("same_channel_publish_gap_h", "same_channel_upload_gap_h",
                "global_upload_spacing_min", "shorts_cooldown_min",
                "shorts_same_type_gap_min"):
        assert normal[key] < strike[key], f"{key} debería acortarse"


def test_manual_override_wins_over_profile(tmp_path):
    db = _db(tmp_path)
    _reset_state(db)
    pacing_profile.set_pacing_profile("normal", db)
    # Override manual puntual (kill-switch): endurecer un canal a 1 short/día
    db.set_system_state("pacing_shorts_per_channel_day", "1")
    assert pacing_profile.get_pacing_value("shorts_per_channel_day", db=db) == 1
    # El resto del perfil normal sigue aplicando
    assert pacing_profile.get_pacing_value("max_longform_publish_day", db=db) == 1


def test_unknown_profile_raises(tmp_path):
    db = _db(tmp_path)
    with pytest.raises(ValueError):
        pacing_profile.set_pacing_profile("nuclear", db)


def test_get_pacing_value_fallback(tmp_path):
    db = _db(tmp_path)
    _reset_state(db)
    # Clave inexistente → default
    assert pacing_profile.get_pacing_value("clave_inexistente", default=42, db=db) == 42
    # Perfil strike → valor real
    assert pacing_profile.get_pacing_value("shorts_per_channel_day", default=99, db=db) == 1
