"""Tests del sistema de cobertura de publicación (fixes ago 2026).

Cubre los 4 cambios clave del plan "cumplimiento forzoso de la programación":
  1. repack_channel_publish_times: el floor del primer slot NO aplica warmup a
     candidatos ya subidos → el slot de hoy no se pierde cuando now+warmup cruza
     el pico por segundos (bug canal2).
  2. Idempotencia: targets futuros que caen en el día que les toca se PRESERVAN
     (no se re-snappean a HH:00) → el repack no mueve cada minuto lo ya bueno.
  3. _channels_need_repack: un vídeo pending con target_public_at NULL marca el
     canal (síntoma 4) — antes quedaba invisible (filtro IS NOT NULL).
  4. _resolve_videos_per_day: el techo diario sigue al perfil de pacing
     (max_longform_publish_day), no a la constante fija.

Run: python3 -m pytest tests/test_publish_coverage.py -v
"""

import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest


# ── Control de tiempo: la función usa `from datetime import datetime as _dt`
# dentro del cuerpo, así que parchear datetime.datetime (atributo del módulo)
# hace que el import local resuelva a la clase congelada. ──

@pytest.fixture(autouse=True)
def _reset_pacing_cache():
    """Evita polución entre tests: el caché TTL de _publish_cap_per_day persiste
    5 min y, al correr la suite en orden alfabético, test_publish_coverage.py
    va antes que test_quota_aware_planning.py → un cap=2 cacheado rompería los
    tests de planificación con cuota."""
    import api.services.planning_service as _ps
    _ps._PUBLISH_CAP_CACHE.update(ts=0.0, value=1)
    yield
    _ps._PUBLISH_CAP_CACHE.update(ts=0.0, value=1)


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
    """Congela datetime.datetime a un instante UTC dado."""
    def _freeze(iso_utc):
        import datetime as _dtmod
        monkeypatch.setattr(_dtmod, "datetime", FixedDateTime)
        FixedDateTime.set(datetime.fromisoformat(iso_utc))
        return FixedDateTime
    return _freeze


# ── FakeDB SQLite en memoria (suficiente para repack_channel_publish_times) ──

class RepackDB:
    def __init__(self, videos=None, channel_cfg=None, state=None):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute(
            """CREATE TABLE videos (
                id INTEGER PRIMARY KEY, channel_id INTEGER, status TEXT,
                yt_video_id TEXT, target_public_at TEXT, scheduled_upload_at TEXT,
                uploaded_at TEXT, created_at TEXT, publish_mode TEXT)"""
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
                    created_at, publish_mode)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
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


CANAL2_CFG = {
    3: {
        "slug": "canal2",
        "config_json": (
            '{"PUBLISH_TIMEZONE": "Europe/Madrid", "PUBLISH_WARMUP_MIN": 30, '
            '"PUBLISH_TARGET_HOUR": 21}'
        ),
    },
}


# ── 1. Warmup floor: no pierde el slot de hoy ──────────────────

class TestWarmupFloorKeepsTodaySlot:
    """Regresión del bug canal2: repack dentro de la ventana de warmup empujaba
    toda la cola a mañana. Con el fix, un candidato YA SUBIDO mantiene el pico
    de hoy aunque now+warmup > pico."""

    def test_uploaded_first_candidate_keeps_today(self, frozen_now):
        # 18:30:05 UTC = 20:30:05 CEST; warmup 30min → floor 21:00:05 CEST, que
        # cruza el pico de 21:00 por 5 segundos. Antes: slot → mañana.
        frozen_now("2026-08-24T18:30:05+00:00")

        db = RepackDB(
            videos=[
                (1, 3, "uploaded_private", "YT1", "2026-08-24 21:00:00",
                 None, "2026-08-18 09:00:00", "2026-08-17 08:00:00", "scheduled"),
                (2, 3, "uploaded_private", "YT2", "2026-08-25 21:00:00",
                 None, "2026-08-19 09:00:00", "2026-08-18 08:00:00", "scheduled"),
            ],
            channel_cfg=CANAL2_CFG,
        )
        from pipeline.publish_scheduler import repack_channel_publish_times

        plan = repack_channel_publish_times(db, 3, "canal2", timezone_str="Europe/Madrid")

        assert plan, "debe haber plan"
        first_local_date = (
            datetime.fromisoformat(plan[0]["new_target"]).astimezone(
                timezone(timedelta(hours=2))).date()
        )
        # El primer vídeo debe publicar HOY (24/08), no mañana.
        assert first_local_date.isoformat() == "2026-08-24", (
            f"el primer slot debería ser hoy, es {first_local_date}"
        )
        assert plan[0]["preserved"] is True
        assert plan[0]["requires_yt_update"] is False  # target intacto → no re-set


# ── 2. Idempotencia: preserva targets válidos ──────────────────

class TestRepackPreservesValidTarget:
    """Un target futuro que cae en el día que le toca se conserva tal cual
    (no se re-snappea a HH:00 ni se mueve a otro día)."""

    def test_off_peak_minute_preserved(self, frozen_now):
        # 20:00 UTC = 22:00 CEST → el pico de hoy (21:00) ya pasó → el walk
        # asigna al primer vídeo el pico de mañana (25/08 21:00 CEST).
        frozen_now("2026-08-24T20:00:00+00:00")

        db = RepackDB(
            videos=[
                (1, 3, "uploaded_private", "YT1", "2026-08-25 21:07:00",
                 None, "2026-08-18 09:00:00", "2026-08-17 08:00:00", "scheduled"),
                (2, 3, "uploaded_private", "YT2", "2026-08-26 21:00:00",
                 None, "2026-08-19 09:00:00", "2026-08-18 08:00:00", "scheduled"),
            ],
            channel_cfg=CANAL2_CFG,
        )
        from pipeline.publish_scheduler import repack_channel_publish_times

        plan = repack_channel_publish_times(db, 3, "canal2", timezone_str="Europe/Madrid")

        # El target 21:07 CEST (mismo día que el walk) se preserva con su
        # minuto. new_target va en UTC (21:07 CEST = 19:07 UTC).
        assert plan[0]["preserved"] is True
        assert "19:07" in plan[0]["new_target"], (
            f"el target válido debería preservarse, es {plan[0]['new_target']}"
        )


# ── 3. _channels_need_repack: target NULL marca el canal ───────

class TestNullTargetFlagsChannel:
    """Un vídeo pending SIN target_public_at (NULL) debe marcar el canal para
    repack (antes quedaba invisible por el filtro IS NOT NULL)."""

    def test_null_target_flags(self, monkeypatch):
        monkeypatch.setattr(
            "api.services.quota_tracker.get_channel_project",
            lambda slug: "test_project",
        )
        db = RepackDB(
            videos=[
                (1, 7, "awaiting_upload", None, None,
                 None, None, "2026-08-24 10:00:00", "scheduled"),
            ],
            channel_cfg={7: {"slug": "canal5", "config_json": "{}"}},
        )
        from api.services.upload_scheduler import _channels_need_repack
        from datetime import datetime as _dt

        affected = _channels_need_repack(db, _dt.now(timezone.utc))
        assert 7 in affected, (
            f"canal con target NULL debe entrar al repack, affected={affected}"
        )


# ── 4. _resolve_videos_per_day sigue al perfil de pacing ───────

class TestResolveVideosPerDayFollowsPacing:
    """El techo diario de planificación = max_longform_publish_day del perfil
    (strike=1, normal=2), no la constante fija."""

    def _reset_cache(self):
        import api.services.planning_service as ps
        ps._PUBLISH_CAP_CACHE.update(ts=0.0, value=1)

    def test_cap_1_in_strike(self, monkeypatch):
        self._reset_cache()
        import api.services.pacing_profile as pp
        monkeypatch.setattr(pp, "get_pacing_value", lambda key, default=None, db=None: 1)
        from api.services.planning_service import _resolve_videos_per_day

        ch = {"videos_per_day": 2, "channel_id": 3, "videos_day_boost_weight": 0.0}
        assert _resolve_videos_per_day(ch, "2026-08-24") == 1

    def test_cap_2_in_normal(self, monkeypatch):
        self._reset_cache()
        import api.services.pacing_profile as pp
        monkeypatch.setattr(pp, "get_pacing_value", lambda key, default=None, db=None: 2)
        from api.services.planning_service import _resolve_videos_per_day

        ch = {"videos_per_day": 2, "channel_id": 3, "videos_day_boost_weight": 0.0}
        assert _resolve_videos_per_day(ch, "2026-08-24") == 2
