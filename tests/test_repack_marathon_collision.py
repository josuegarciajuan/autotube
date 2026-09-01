"""Tests de regresión: colisión maratón + normales en el repack (ESR/canal4, 1/9).

Escenario raíz: una maratón NO consume el presupuesto diario de normales, pero
el viejo walk la dejaba fuera de `day_used`, así que la maratón y la siguiente
normal elegían el MISMO rank → mismo `target_public_at` (3 vídeos a 1/9 20:00Z).
Además `_channels_need_repack` ignoraba colisiones exactas (diff_h == 0).

Estos tests verifican:
  1. repack_channel_publish_times asigna slots ÚNICOS y con gap >= gap_hours
     a una cola maratón + 2 normales que llegaron con el mismo target.
  2. La preservación no congela una colisión (el vídeo colisionante marca
     requires_yt_update=True y recibe un target nuevo único).
  3. _channels_need_repack detecta la colisión EXACTA (misma hora) y marca el canal.

Run: python3 -m pytest tests/test_repack_marathon_collision.py -v
"""

import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest


class FixedDateTime(datetime):
    _fixed = None

    @classmethod
    def set(cls, dt):
        cls._fixed = dt

    @classmethod
    def now(cls, tz=None):
        base = cls._fixed if cls._fixed is not None else datetime.now(tz)
        if tz is not None:
            if base.tzinfo is None:
                base = base.replace(tzinfo=timezone.utc)
            return base.astimezone(tz)
        return base


@pytest.fixture
def frozen_now(monkeypatch):
    def _freeze(iso_utc):
        import datetime as _dtmod
        monkeypatch.setattr(_dtmod, "datetime", FixedDateTime)
        FixedDateTime.set(datetime.fromisoformat(iso_utc))
        return FixedDateTime
    return _freeze


class RepackDB:
    """FakeDB con `is_marathon` (ausente en test_publish_coverage.RepackDB) y sin
    optimal_publish_slots → el repack usa la heurística de pico determinista."""

    def __init__(self, videos=None, channel_cfg=None, state=None):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute(
            """CREATE TABLE videos (
                id INTEGER PRIMARY KEY, channel_id INTEGER, status TEXT,
                yt_video_id TEXT, target_public_at TEXT, scheduled_upload_at TEXT,
                uploaded_at TEXT, created_at TEXT, publish_mode TEXT,
                is_marathon INTEGER DEFAULT 0)"""
        )
        self.conn.execute(
            "CREATE TABLE channels (id INTEGER PRIMARY KEY, slug TEXT, config_json TEXT)"
        )
        self._channel_cfg = channel_cfg or {}
        self._state = dict(state or {})
        for ch_id, cfg in self._channel_cfg.items():
            self.conn.execute(
                "INSERT INTO channels (id, slug, config_json) VALUES (?,?,?)",
                (ch_id, cfg.get("slug", ""), cfg.get("config_json", "{}")),
            )
        if videos:
            self.conn.executemany(
                """INSERT INTO videos (id, channel_id, status, yt_video_id,
                    target_public_at, scheduled_upload_at, uploaded_at,
                    created_at, publish_mode, is_marathon)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                videos,
            )
            self.conn.commit()

    def _connect(self):
        return self.conn

    def get_system_state(self, key):
        return self._state.get(key)

    def get_channel(self, channel_id):
        cfg = self._channel_cfg.get(channel_id)
        if cfg is None:
            return None
        return {
            "id": channel_id,
            "slug": cfg.get("slug", ""),
            "config_json": cfg.get("config_json", "{}"),
        }

    def get_channels(self, active_only=False):
        return [
            {"id": ch_id, "slug": cfg.get("slug", "")}
            for ch_id, cfg in self._channel_cfg.items()
        ]

    def is_channel_spam_blocked(self, channel_id):
        return False


CANAL4_CFG = {
    5: {
        "slug": "canal4",
        "config_json": (
            '{"PUBLISH_TIMEZONE": "Europe/Madrid", "PUBLISH_WARMUP_MIN": 30, '
            '"PUBLISH_TARGET_HOUR": 20, "MAX_LONGFORM_PUBLISH_PER_DAY": 2}'
        ),
    },
}

# (id, channel_id, status, yt_id, target, sched_upload, uploaded, created, mode, is_marathon)
COLLIDING = [
    (2259, 5, "uploaded_private", "YT2259", "2026-09-01T20:00:00+00:00",
     None, "2026-08-31 10:13:41", "2026-08-25 13:52:13", "scheduled", 0),
    (2279, 5, "uploaded_private", "YT2279", "2026-09-01T20:00:00+00:00",
     None, "2026-09-01 00:48:18", "2026-08-28 14:08:09", "scheduled", 1),
    (2288, 5, "uploaded_private", "YT2288", "2026-09-01T20:00:00+00:00",
     None, "2026-09-01 00:53:38", "2026-08-31 05:51:35", "scheduled", 0),
]


class TestRepackMarathonNoCollision:
    """El repack debe separar maratón + normales que llegaron a la misma hora."""

    def test_unique_slots_and_gap(self, frozen_now):
        frozen_now("2026-09-01T12:00:00+00:00")
        db = RepackDB(videos=COLLIDING, channel_cfg=CANAL4_CFG)

        from pipeline.publish_scheduler import repack_channel_publish_times

        plan = repack_channel_publish_times(
            db, 5, "canal4", timezone_str="Europe/Madrid", warmup_min=30,
            gap_hours=6,
        )

        assert plan, "debe haber plan"
        # 1) Todos los targets nuevos deben ser únicos
        targets = [datetime.fromisoformat(p["new_target"]) for p in plan]
        assert len(set(t.isoformat() for t in targets)) == len(targets), (
            f"hay targets duplicados: {[t.isoformat() for t in targets]}"
        )
        # 2) Espaciado >= gap_hours (6h) entre consecutivos ordenados
        targets.sort()
        for a, b in zip(targets, targets[1:]):
            diff_h = (b - a).total_seconds() / 3600
            assert diff_h >= 6, f"gap insuficiente entre {a} y {b} ({diff_h:.1f}h)"

    def test_colliding_marathon_must_reprogram(self, frozen_now):
        frozen_now("2026-09-01T12:00:00+00:00")
        db = RepackDB(videos=COLLIDING, channel_cfg=CANAL4_CFG)

        from pipeline.publish_scheduler import repack_channel_publish_times

        plan = repack_channel_publish_times(
            db, 5, "canal4", timezone_str="Europe/Madrid", warmup_min=30,
            gap_hours=6,
        )
        by_id = {p["video_id"]: p for p in plan}
        # El primer normal (2259) puede preservarse; la maratón (2279) y el 2º
        # normal (2288) NO deben quedar preservados a la misma hora que 2259.
        assert by_id[2259]["preserved"] is True
        assert by_id[2279]["preserved"] is False, (
            "la maratón colisionante no debe preservarse"
        )
        assert by_id[2288]["preserved"] is False, (
            "el 2º normal colisionante no debe preservarse"
        )
        assert by_id[2279]["requires_yt_update"] is True
        assert by_id[2288]["requires_yt_update"] is True
        # El target nuevo de la maratón debe ser DISTINTO al de 2259
        assert by_id[2279]["new_target"] != by_id[2259]["new_target"]


class TestRepackNonCollidingStillPreserved:
    """Regresión: colas ya bien espaciadas siguen preservándose (idempotencia)."""

    def test_spread_queue_preserved(self, frozen_now):
        frozen_now("2026-09-01T12:00:00+00:00")
        db = RepackDB(
            videos=[
                (1001, 5, "uploaded_private", "YT1001", "2026-09-01T20:00:00+00:00",
                 None, "2026-08-31 10:00:00", "2026-08-30 08:00:00", "scheduled", 0),
                (1002, 5, "uploaded_private", "YT1002", "2026-09-02T18:00:00+00:00",
                 None, "2026-08-31 11:00:00", "2026-08-30 09:00:00", "scheduled", 0),
            ],
            channel_cfg=CANAL4_CFG,
        )
        from pipeline.publish_scheduler import repack_channel_publish_times

        plan = repack_channel_publish_times(
            db, 5, "canal4", timezone_str="Europe/Madrid", warmup_min=30,
            gap_hours=6,
        )
        by_id = {p["video_id"]: p for p in plan}
        assert by_id[1001]["preserved"] is True
        assert by_id[1002]["preserved"] is True
        assert by_id[1002]["requires_yt_update"] is False


class TestChannelsNeedRepackDetectsExactCollision:
    """_channels_need_repack debe marcar el canal cuando 2+ vídeos tienen la
    MISMA hora (diff == 0), que el viejo `0 < diff_h < gap` ignoraba."""

    def test_exact_same_time_flags_channel(self, monkeypatch):
        monkeypatch.setattr(
            "api.services.quota_tracker.get_channel_project",
            lambda slug: "test_project",
        )
        db = RepackDB(
            videos=[
                (1, 5, "uploaded_private", "YT1", "2026-09-01T20:00:00+00:00",
                 None, "2026-08-31 10:00:00", "2026-08-30 08:00:00", "scheduled", 0),
                (2, 5, "uploaded_private", "YT2", "2026-09-01T20:00:00+00:00",
                 None, "2026-09-01 00:48:00", "2026-08-30 09:00:00", "scheduled", 1),
            ],
            channel_cfg=CANAL4_CFG,
        )
        from api.services.upload_scheduler import _channels_need_repack
        from datetime import datetime as _dt

        affected = _channels_need_repack(db, _dt.now(timezone.utc))
        assert 5 in affected, (
            f"la colisión exacta (misma hora) debe marcar el canal, affected={affected}"
        )
