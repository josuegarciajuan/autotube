"""Contrato de distribución de publicaciones — regresiones de la Fase 0.

Este archivo fija el comportamiento CORRECTO descrito en
``specs/distribucion-publicaciones.md``. Los tests marcados con
``xfail(strict=True)`` reproducen defectos conocidos del código actual: se
mantienen verdes (como "expected failure") hasta que la fase correspondiente
los arregle. Al arreglarse, ``strict=True`` los convierte en fallo y obliga a
quitar el marcador.

Fases:
  1/2 — asignador único, rollover al día siguiente, histórico real.
  3   — shorts por ``slot_rank``.
  4   — replan por canal y persistencia acotada.
"""

import datetime as dt
import json
import sqlite3
from collections import Counter

import pytest
import pytz

# ── Helpers ─────────────────────────────────────────────────────


def _full_db(tmp_path, name: str = "distribution.db"):
    """DB con el schema real (init + migraciones) y canales controlados."""
    from database.db import init_db
    from database.db_extended import ExtendedDatabase, migrate_v2

    path = tmp_path / name
    init_db(str(path))
    migrate_v2(str(path))
    db = ExtendedDatabase(str(path))
    with db._connect() as conn:
        conn.execute("DELETE FROM channels")
        for cid, slug in ((1, "one"), (2, "two")):
            conn.execute(
                "INSERT INTO channels (id, name, slug, config_json, active) "
                "VALUES (?, ?, ?, ?, 1)",
                (cid, slug, slug, json.dumps({"PUBLISH_TIMEZONE": "Europe/Madrid"})),
            )
        conn.commit()
    return db


def _local_date(value: str) -> dt.date:
    tz = pytz.timezone("Europe/Madrid")
    parsed = dt.datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = tz.localize(parsed)
    return parsed.astimezone(tz).date()


# ── Fase 1/2: asignador único y rollover ────────────────────────


def test_longform_overflow_rolls_to_next_day(monkeypatch):
    """Con un gap de 24 h y dos slots, el segundo debe caer en otro día.

    Las ventanas se fuerzan al final del día para provocar el overflow. El
    asignador debe mover el segundo slot al día siguiente (antes se recortaba
    a las 23:xx, dejando <24 h entre ambos).
    """
    import api.services.planning_service as ps

    monkeypatch.setattr(
        ps, "_optimal_slot_windows",
        lambda db, channel_id: [
            (21, 23, 3, "optimal_rank1"),
            (22, 23, 2, "optimal_rank2"),
        ],
    )
    monkeypatch.setattr(ps, "_publish_gap_minutes", lambda *a, **k: 24 * 60)
    monkeypatch.setattr(ps, "_publish_cap_per_day", lambda *a, **k: 1)

    slots = ps._distribute_slots(
        2,
        day_seed=20261018,
        channel_id=1,
        is_scheduled=True,
        scheduled_cfg={"publish_mode": "scheduled"},
        db=None,
        date_str="2026-10-18",
    )

    assert len(slots) == 2
    first, second = slots
    assert second > first
    assert (second - first) >= dt.timedelta(hours=24)
    assert second.date() > first.date()


@pytest.mark.xfail(
    strict=True,
    reason="Fase 4: _replan_long_form ignora el gap por canal y apila slots el mismo día",
)
def test_optimize_franjas_replan_respects_daily_cap(monkeypatch):
    """'Optimizar franjas' no puede meter 2 long-forms el mismo día (cap=1)."""
    import random

    import api.services.optimal_slots_calculator as osc

    monkeypatch.setattr(random, "randint", lambda a, b: 0)

    pending = [
        {"id": 1, "channel_id": 1, "scheduled_at": "2026-10-18 01:00:00",
         "target_public_at": None},
        {"id": 2, "channel_id": 1, "scheduled_at": "2026-10-18 02:00:00",
         "target_public_at": None},
        {"id": 3, "channel_id": 1, "scheduled_at": "2026-10-18 03:00:00",
         "target_public_at": None},
    ]

    class _Cursor:
        def __init__(self, rows):
            self._rows = rows

        def fetchall(self):
            return self._rows

        def fetchone(self):
            return self._rows[0] if self._rows else None

    class _Conn:
        def __init__(self, rows):
            self.rows = rows

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def execute(self, query, params=()):
            norm = " ".join(query.split())
            if norm.startswith("SELECT * FROM planned_slots"):
                return _Cursor([dict(r) for r in self.rows])
            if norm.startswith("SELECT 1 FROM planned_slots"):
                _cid, ts, slot_id = params
                hit = any(
                    r["target_public_at"] == ts and r["id"] != slot_id
                    for r in self.rows
                )
                return _Cursor([{"1": 1}] if hit else [])
            if norm.startswith("UPDATE planned_slots SET target_public_at"):
                ts, slot_id = params
                for row in self.rows:
                    if row["id"] == slot_id:
                        row["target_public_at"] = ts
                return _Cursor([])
            return _Cursor([])

        def commit(self):
            pass

    class _DB:
        def __init__(self, rows):
            self.rows = rows

        def get_optimal_slots(self, channel_id, content_type):
            return [
                {"slot_rank": 1, "target_hour": 20, "timezone": "Europe/Madrid"},
                {"slot_rank": 2, "target_hour": 6, "timezone": "Europe/Madrid"},
            ]

        def _connect(self):
            return _Conn(self.rows)

    calc = osc.OptimalSlotsCalculator(_DB(pending))
    updated = calc._replan_long_form(1)
    assert updated == 3

    per_day: Counter = Counter()
    times = []
    for row in pending:
        assert row["target_public_at"] is not None
        per_day[_local_date(row["target_public_at"])] += 1
        times.append(row["target_public_at"])

    assert max(per_day.values()) <= 1, per_day
    assert len(set(times)) == 3


# ── Fase 2: histórico por publicación real ─────────────────────


@pytest.mark.xfail(
    strict=True,
    reason="Fase 2: el histórico agrupa por created_at (generación), no por publicación",
)
def test_historical_performance_uses_publication_time(tmp_path):
    """Un vídeo creado a las 02:00 y publicado a las 20:10 debe contar a las 20."""
    from api.services.optimal_slots_calculator import OptimalSlotsCalculator

    db = _full_db(tmp_path, "history.db")
    with db._connect() as conn:
        cur = conn.execute(
            """INSERT INTO videos
               (channel_id, canal, video_path, status, yt_video_id,
                created_at, actual_published_at)
               VALUES (1, 'one', '/tmp/v.mp4', 'published', 'yt-1',
                       '2026-10-10 02:00:00', '2026-10-10T18:10:00+00:00')"""
        )
        video_id = cur.lastrowid
        conn.execute(
            """INSERT INTO video_stats_history
               (video_id, yt_video_id, views, fetched_at)
               VALUES (?, 'yt-1', 5000, '2026-10-11 00:00:00')""",
            (video_id,),
        )
        conn.commit()

    hist = OptimalSlotsCalculator(db)._get_historical_by_publish_hour(1, "long")

    # 18:10 UTC en octubre = 20:10 Europe/Madrid.
    assert 20 in hist, hist
    assert 2 not in hist, hist


# ── Fase 3: shorts por ranking de franja ───────────────────────


@pytest.mark.xfail(
    strict=True,
    reason="Fase 3: los shorts se ordenan por reloj y eligen la franja más temprana",
)
def test_short_uses_best_ranked_franja_not_earliest(monkeypatch):
    """Con rank 1 a las 19:00 y rank 3 a las 03:00, el short debe ir a las 19:00."""
    import database.db_extended as db_ext
    import api.services.shorts_scheduler as ss

    class _FakeDB:
        def get_optimal_slots(self, channel_id, content_type):
            return [
                {"slot_rank": 1, "target_hour": 19, "target_minute": 0},
                {"slot_rank": 2, "target_hour": 13, "target_minute": 0},
                {"slot_rank": 3, "target_hour": 3, "target_minute": 0},
            ]

        def record_slot_usage(self, *a, **k):
            return True

    monkeypatch.setattr(db_ext, "ExtendedDatabase", lambda *a, **k: _FakeDB())

    def _no_config(*_a, **_k):
        raise RuntimeError("sin config para el test")

    monkeypatch.setattr("config.config_bridge.get_channel_config", _no_config)

    channel = {"id": 1, "slug": "one", "name": "One"}
    slots, _pos = ss._build_shorts_slots_for_channel(
        channel, "2026-10-18", native_count=1,
        clips_per_long=0, long_video_count=0,
        long_target_hours=[], global_start_pos=0,
    )

    assert len(slots) == 1
    # 19:00 Madrid (CEST) = 17:00 UTC.
    assert slots[0]["target_upload_at"] == "2026-10-18 17:00:00"
    assert slots[0]["slot_rank"] == 1


# ── Fase 4: replan acotado por canal ───────────────────────────


@pytest.mark.xfail(
    strict=True,
    reason="Fase 4: persist_daily_shorts_slots borra los pendientes de TODOS los canales",
)
def test_persist_shorts_does_not_delete_other_channels(tmp_path):
    """Persistir el canal 1 no debe borrar los slots pendientes del canal 2."""
    from api.services.shorts_scheduler import persist_daily_shorts_slots

    db = _full_db(tmp_path, "shorts-scope.db")
    date_key = "2026-10-18"
    with db._connect() as conn:
        for cid in (1, 2):
            conn.execute(
                """INSERT INTO shorts_planned_slots
                   (channel_id, date_key, scheduled_at, target_upload_at,
                    short_type, status, slot_position, slot_rank)
                   VALUES (?, ?, ?, ?, 'native', 'pending', 1, 1)""",
                (cid, date_key, f"{date_key} 10:00:00", f"{date_key} 10:15:00"),
            )
        conn.commit()

    new_slot = {
        "channel_id": 1,
        "date_key": date_key,
        "scheduled_at": f"{date_key} 12:00:00",
        "target_upload_at": f"{date_key} 12:15:00",
        "short_type": "native",
        "slot_position": 1,
        "long_slot_position": None,
        "source_video_id": None,
        "slot_rank": 1,
    }
    persist_daily_shorts_slots(date_key, [new_slot], db)

    with db._connect() as conn:
        ch2 = conn.execute(
            """SELECT COUNT(*) FROM shorts_planned_slots
               WHERE channel_id = 2 AND date_key = ? AND status = 'pending'""",
            (date_key,),
        ).fetchone()[0]
    assert ch2 == 1
