"""Circular rotating publication strategy (4 channels × 4 phases).

The strategy is the **planning authority** for the daily public targets:
every participating channel publishes its configured program plus the bonus of
its phase for that Madrid calendar day:

    normal      → base program
    shorts+1    → base + 1 native short
    largos+1    → base + 1 long-form
    random+1    → base + 1 long-form OR 1 native short (decided once per day)

The initial assignment is a random permutation (no repeated phase) persisted in
``system_state``.  Each Madrid day every channel advances one position, so the
four channels always hold four distinct phases and every channel visits every
phase exactly once per four-day cycle.

Safety is never bypassed: confirmed strikes, spam holds, YouTube quota,
upload gaps and the cross-channel spacing keep blocking at their own
boundaries.  The rotation only removes the *planning* caps that would otherwise
clip the extra publication.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import secrets
import threading
import time
from datetime import date, datetime, timedelta
from typing import Iterable

from api.time_utils import MADRID

logger = logging.getLogger("autotube.rotation")

PHASES: tuple[str, ...] = ("normal", "shorts+1", "largos+1", "random+1")
STRATEGY_KEY = "rotation_strategy_v1"
DAY_KEY_PREFIX = "rotation_day_"
_CACHE_TTL_SECONDS = 15

_cache_lock = threading.Lock()
_cache: dict = {"ts": 0.0, "db_path": "", "value": None}
_revision = 0


def _today() -> date:
    return datetime.now(MADRID).date()


def _as_date(day: date | str | None) -> date:
    if day is None:
        return _today()
    if isinstance(day, date):
        return day
    return date.fromisoformat(str(day)[:10])


def _db_path(db) -> str:
    path = str(getattr(db, "db_path", "") or "")
    # Legacy/test adapters may not expose db_path; never share the cache
    # between different DB objects in that case.
    return path or f"obj:{id(db)}"


def _env_disabled() -> bool:
    raw = str(os.environ.get("ROTATION_STRATEGY_DISABLED", "")).strip().lower()
    return raw in ("1", "true", "yes", "on")


def invalidate_cache() -> None:
    global _revision
    with _cache_lock:
        _cache["ts"] = 0.0
        _cache["value"] = None
        _revision += 1


def _new_permutation(count: int) -> list[int]:
    values = list(range(count))
    secrets.SystemRandom().shuffle(values)
    return values


def _build_strategy(db) -> dict:
    channels = [
        c for c in (db.get_channels(active_only=True) or [])
        if c.get("slug") != "test" and c.get("id")
    ]
    participants = [int(c["id"]) for c in channels]
    initial = _new_permutation(len(participants))
    strategy = {
        "version": 1,
        "enabled": str(os.environ.get("ROTATION_STRATEGY_ENABLED", "true")).strip().lower()
        not in ("0", "false", "no", "off"),
        "anchor_date": _today().isoformat(),
        "participants": participants,
        "initial_phase_index": {
            str(cid): int(initial[idx]) for idx, cid in enumerate(participants)
        },
        "allow_marathons": False,
        "seed": secrets.token_hex(8),
        "created_at": datetime.utcnow().isoformat() + "Z",
    }
    db.set_system_state(STRATEGY_KEY, json.dumps(strategy, ensure_ascii=False))
    invalidate_cache()
    logger.info(
        "Rotation strategy created: anchor=%s participants=%s initial=%s",
        strategy["anchor_date"], participants, strategy["initial_phase_index"],
    )
    return strategy


def _read_strategy(db) -> dict | None:
    try:
        raw = db.get_system_state(STRATEGY_KEY)
        if not raw:
            return None
        value = json.loads(raw) if isinstance(raw, str) else raw
        if not isinstance(value, dict):
            return None
        if value.get("version") != 1:
            return None
        if not isinstance(value.get("participants"), list):
            return None
        if not isinstance(value.get("initial_phase_index"), dict):
            return None
        return value
    except (TypeError, ValueError, json.JSONDecodeError):
        logger.warning("Rotation strategy: invalid persisted JSON", exc_info=True)
        return None


def get_strategy(db, *, create: bool = False) -> dict | None:
    """Return the persisted strategy, optionally creating it on first use."""
    if db is None:
        return None
    now = time.time()
    path = _db_path(db)
    with _cache_lock:
        if (
            _cache["value"] is not None
            and _cache["db_path"] == path
            and (now - _cache["ts"]) < _CACHE_TTL_SECONDS
        ):
            return dict(_cache["value"])
    value = _read_strategy(db)
    if value is None and create:
        value = _build_strategy(db)
    with _cache_lock:
        _cache["ts"] = now
        _cache["db_path"] = path
        _cache["value"] = dict(value) if value else None
    return value


def set_enabled(db, enabled: bool) -> dict:
    strategy = get_strategy(db, create=True)
    strategy["enabled"] = bool(enabled)
    db.set_system_state(STRATEGY_KEY, json.dumps(strategy, ensure_ascii=False))
    invalidate_cache()
    return strategy


def reset_strategy(db, *, enabled: bool = True) -> dict:
    """Create a fresh random permutation anchored to today."""
    strategy = _build_strategy(db)
    strategy["enabled"] = bool(enabled)
    db.set_system_state(STRATEGY_KEY, json.dumps(strategy, ensure_ascii=False))
    invalidate_cache()
    return strategy


def is_enabled(db) -> bool:
    if _env_disabled():
        return False
    strategy = get_strategy(db)
    return bool(strategy and strategy.get("enabled"))


def _days_since_anchor(strategy: dict, day: date) -> int:
    try:
        anchor = date.fromisoformat(str(strategy.get("anchor_date"))[:10])
    except (TypeError, ValueError):
        anchor = _today()
    return (day - anchor).days


def _phase_index(strategy: dict, channel_id: int, day: date) -> int | None:
    try:
        initial = int(strategy["initial_phase_index"][str(int(channel_id))])
    except (KeyError, TypeError, ValueError):
        return None
    return (initial + _days_since_anchor(strategy, day)) % len(PHASES)


def _random_bonus_type(strategy: dict, channel_id: int, day: date) -> str:
    """Deterministic, balanced long/short choice for ``random+1``.

    Each four-day block gets exactly two ``long`` and two ``short`` extras.  The
    assignment inside the block is shuffled with a stable hash of the anchor and
    the block, so replans, restarts and different processes agree.
    """
    offset = _days_since_anchor(strategy, day)
    block = offset // 4
    position = offset % 4
    seed = str(strategy.get("seed") or strategy.get("anchor_date") or "rotation")
    # The channel is intentionally excluded: one channel is in ``random+1``
    # each day, so the block balance is defined over calendar days (2 long /
    # 2 short per four-day block), not per channel.
    ordered = sorted(
        range(4),
        key=lambda idx: hashlib.sha256(
            f"{seed}|{block}|{idx}".encode()
        ).hexdigest(),
    )
    rank = ordered.index(position)
    return "long" if rank % 2 == 0 else "short"


def phase_for(channel_id: int, db, day: date | str | None = None) -> dict | None:
    """Return ``{phase, bonus_type}`` for a channel/day, or None if inactive."""
    strategy = get_strategy(db)
    if not strategy or not strategy.get("enabled") or _env_disabled():
        return None
    target = _as_date(day)
    try:
        participants = {int(value) for value in strategy.get("participants") or []}
    except (TypeError, ValueError):
        return None
    if int(channel_id) not in participants:
        return None
    idx = _phase_index(strategy, channel_id, target)
    if idx is None:
        return None
    phase = PHASES[idx]
    bonus_type = None
    if phase == "random+1":
        bonus_type = _random_bonus_type(strategy, int(channel_id), target)
    return {"phase": phase, "bonus_type": bonus_type}


def bonus_for(channel_id: int, db, day: date | str | None = None) -> dict:
    """Return additive long/short bonuses for a channel/day.

    The result is always safe to call: rotation disabled, missing strategy or a
    non-participating channel yields zeros.
    """
    empty = {"enabled": False, "phase": None, "bonus_type": None, "longs": 0, "shorts": 0}
    info = phase_for(channel_id, db, day)
    if info is None:
        return empty
    phase = info["phase"]
    longs = 1 if phase == "largos+1" else 0
    shorts = 1 if phase == "shorts+1" else 0
    if phase == "random+1":
        if info.get("bonus_type") == "long":
            longs = 1
        else:
            shorts = 1
    return {
        "enabled": True,
        "phase": phase,
        "bonus_type": info.get("bonus_type"),
        "longs": longs,
        "shorts": shorts,
    }


def apply_bonus(channel_id: int, db, day, *, longs: int, shorts: int) -> tuple[int, int, dict]:
    """Add the rotation bonus to already-resolved base caps."""
    bonus = bonus_for(channel_id, db, day)
    return (
        max(0, int(longs)) + int(bonus["longs"]),
        max(0, int(shorts)) + int(bonus["shorts"]),
        bonus,
    )


def day_assignment(db, day: date | str | None = None) -> dict:
    """Materialize (idempotently) and return the assignment for a Madrid day."""
    target = _as_date(day)
    strategy = get_strategy(db, create=True)
    assignment: dict[str, dict] = {}
    if strategy and strategy.get("enabled") and not _env_disabled():
        for raw_cid in strategy.get("participants") or []:
            try:
                cid = int(raw_cid)
            except (TypeError, ValueError):
                continue
            info = phase_for(cid, db, target)
            if info:
                assignment[str(cid)] = info
    key = f"{DAY_KEY_PREFIX}{target.isoformat()}"
    if not assignment:
        return {}
    try:
        raw = db.get_system_state(key)
        serialized = json.dumps(assignment, ensure_ascii=False, sort_keys=True)
        if not raw:
            db.set_system_state(key, serialized)
        else:
            try:
                current = json.loads(raw)
            except (TypeError, ValueError, json.JSONDecodeError):
                current = None
            if current != assignment:
                # Operator reset / new permutation: keep the audit row aligned
                # with what the resolver actually applies.
                db.set_system_state(key, serialized)
    except Exception:
        logger.warning("Rotation: could not persist day assignment %s", target, exc_info=True)
    return assignment


def ensure_daily_rotation(db, *, apply: bool = False, force: bool = False) -> dict:
    """Materialize today's phases and (optionally) replan the horizon.

    Idempotent: the persisted day key makes repeated calls on the same Madrid
    day a no-op.  ``apply`` triggers one coherent long-form + shorts replan so
    the new phase takes effect immediately; ``force`` re-applies even if the
    day was already materialized (operator reset/enable).
    """
    if db is None:
        return {"ok": False, "reason": "no_db"}
    if _env_disabled():
        return {"ok": True, "enabled": False, "reason": "kill_switch"}
    strategy = get_strategy(db, create=True)
    if not strategy:
        return {"ok": False, "reason": "no_strategy"}

    today = _today()
    if not strategy.get("enabled"):
        result = {
            "ok": True,
            "enabled": False,
            "day": today.isoformat(),
            "newly_materialized": False,
            "assignment": {},
        }
        # Al DESACTIVAR desde el panel se fuerza un replan para retirar el
        # bonus que ya estuviera planificado. En pasadas normales no hace nada.
        if not (apply and force):
            return result
        assignment = {}
    else:
        key = f"{DAY_KEY_PREFIX}{today.isoformat()}"
        already = False
        try:
            already = bool(db.get_system_state(key))
        except Exception:
            already = False
        assignment = day_assignment(db, today)
        result = {
            "ok": True,
            "enabled": True,
            "day": today.isoformat(),
            "newly_materialized": bool(assignment) and not already,
            "assignment": assignment,
        }
        if not assignment or not apply or (already and not force):
            return result

    try:
        from api.services.planning_service import (
            compute_and_store_horizon, invalidate_policy_caches,
        )
        invalidate_policy_caches()
        result["horizon"] = compute_and_store_horizon(
            horizon_days=7, db=db, force_replan=True,
        )
    except Exception as exc:
        logger.warning("Rotation: horizon replan failed: %s", exc, exc_info=True)
        result["horizon_error"] = str(exc)
    try:
        from api.services.shorts_scheduler import (
            cleanup_excessive_shorts_slots, generate_upcoming_shorts,
        )
        result["shorts"] = generate_upcoming_shorts(days=7, db=db)
        # Si el canal rota FUERA de "shorts+1", el planificador idempotente no
        # recorta los slots ya pendientes (solo añade). El cleanup los ajusta al
        # objetivo del nuevo día para no publicar el bonus de ayer.
        result["shorts_cleanup"] = cleanup_excessive_shorts_slots(db)
    except Exception as exc:
        logger.warning("Rotation: shorts replan failed: %s", exc, exc_info=True)
        result["shorts_error"] = str(exc)
    logger.info(
        "Rotation day %s applied: %s",
        today.isoformat(),
        {cid: info.get("phase") for cid, info in assignment.items()},
    )
    return result


def preview(db, days: int = 4, start: date | None = None) -> list[dict]:
    """Preview assignments for the next ``days`` Madrid days (read-only)."""
    start = start or _today()
    strategy = get_strategy(db, create=True) or {}
    participants = [int(c) for c in (strategy.get("participants") or [])]
    rows = []
    for offset in range(max(1, int(days))):
        day = start + timedelta(days=offset)
        phases = {}
        for cid in participants:
            info = phase_for(cid, db, day)
            phases[str(cid)] = info or {}
        rows.append({"day": day.isoformat(), "channels": phases})
    return rows


def summary(db) -> dict:
    strategy = get_strategy(db, create=True) or {}
    return {
        "enabled": bool(strategy.get("enabled")) and not _env_disabled(),
        "kill_switch": _env_disabled(),
        "anchor_date": strategy.get("anchor_date"),
        "participants": strategy.get("participants") or [],
        "phases": list(PHASES),
        "allow_marathons": bool(strategy.get("allow_marathons", False)),
        "today": day_assignment(db),
        "preview": preview(db, days=4),
    }
