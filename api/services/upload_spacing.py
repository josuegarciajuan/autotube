"""Espaciado global de subidas entre canales (anti-ráfaga / anti-spam).

Contexto (ago 2026): YouTube eliminó 4 shorts en 4 días y aplicó strikes a 3
canales. Los 3 strikes ocurrieron justo cuando varios canales subían casi a la
vez desde la misma IP (ráfagas cruzadas de 3-4 subidas en <7 min). YouTube
detecta "redes de spam" (misma IP + plantillas idénticas + ráfagas) y elimina
los vídeos a los ~20 segundos de la subida.

Este módulo impone un espaciado mínimo ENTRE CANALES DISTINTOS antes de cada
subida real a YouTube. El cooldown por canal (shorts: 180 min) ya existe; esto
cubre el hueco de las ráfagas entre canales distintos.

Estado persistido en ``system_state`` (sobrevive reinicios de la API):
  - ``last_upload_any_ts``      : epoch del último upload completado (cualquier canal)
  - ``last_upload_any_channel`` : slug del canal de ese upload

El espaciado se aplica SOLO si el último upload fue de OTRO canal: subir dos
veces el mismo canal con rapidez lo gobierna el cooldown por canal existente.
"""

from __future__ import annotations

import logging
import time

logger = logging.getLogger("autotube.upload_spacing")

# Mínimo de minutos entre subidas de CANALES DISTINTOS. Sobrescribible en
# runtime vía system_state["global_upload_spacing_min"] (para relajarlo o
# endurecerlo sin tocar código).
GLOBAL_UPLOAD_SPACING_MIN = 45

_TS_KEY = "last_upload_any_ts"
_CHANNEL_KEY = "last_upload_any_channel"
_MIN_KEY = "global_upload_spacing_min"


def _get_db():
    from database.db_extended import ExtendedDatabase
    from config.settings import DATABASE_PATH
    return ExtendedDatabase(str(DATABASE_PATH))


def _spacing_min_minutes(db) -> int:
    try:
        raw = db.get_system_state(_MIN_KEY)
        if raw:
            return int(float(raw))
    except (TypeError, ValueError):
        pass
    return GLOBAL_UPLOAD_SPACING_MIN


def get_last_upload(db=None) -> tuple[float, str] | None:
    """Return (ts_epoch, channel_slug) of the last upload, or None."""
    try:
        if db is None:
            db = _get_db()
        raw_ts = db.get_system_state(_TS_KEY)
        if not raw_ts:
            return None
        ts = float(raw_ts)
        channel = db.get_system_state(_CHANNEL_KEY) or ""
        return ts, channel
    except (TypeError, ValueError):
        return None


def record_upload(channel_slug: str, db=None) -> None:
    """Mark that a YouTube upload just completed (for the spacing window)."""
    try:
        if not channel_slug:
            return
        if db is None:
            db = _get_db()
        db.set_system_state(_TS_KEY, str(time.time()))
        db.set_system_state(_CHANNEL_KEY, channel_slug)
    except Exception as exc:  # noqa: BLE001 — non-critical bookkeeping
        logger.debug("record_upload failed: %s", exc)


def remaining_spacing_seconds(channel_slug: str, db=None) -> int:
    """Seconds to wait before uploading ``channel_slug`` (0 = can upload now).

    Returns 0 when: no prior upload recorded, the prior upload was the SAME
    channel (per-channel cooldown governs that case), or the gap already
    elapsed.
    """
    try:
        if db is None:
            db = _get_db()
        last = get_last_upload(db)
        if not last:
            return 0
        last_ts, last_channel = last
        if last_channel and channel_slug and last_channel == channel_slug:
            return 0  # same channel: per-channel cooldown handles it
        min_min = _spacing_min_minutes(db)
        elapsed = time.time() - last_ts
        remaining = (min_min * 60) - elapsed
        return int(max(0, remaining))
    except Exception as exc:  # noqa: BLE001 — fail open on errors
        logger.debug("remaining_spacing_seconds failed (fail-open): %s", exc)
        return 0


def spacing_ok(channel_slug: str, db=None) -> bool:
    """True if ``channel_slug`` may upload now (no cross-channel gap violation)."""
    return remaining_spacing_seconds(channel_slug, db) == 0
