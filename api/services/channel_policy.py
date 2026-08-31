"""Single read-only resolver for channel safety state.

This module deliberately separates historical evidence from the currently
enforced block.  Old strikes remain visible for audit and pacing decisions,
but an expired block is not an active strike.
"""

from __future__ import annotations

import time
import json
from collections import Counter
from threading import Lock

_telemetry = Counter()
_telemetry_lock = Lock()


def _count(event: str) -> None:
    with _telemetry_lock:
        _telemetry[event] += 1


def get_policy_telemetry() -> dict[str, int]:
    """Process-local legacy diagnostics, avoiding frequent DB writes."""
    with _telemetry_lock:
        return dict(_telemetry)

REMOVAL_VISIBILITY = "removed"
NON_REMOVAL_VISIBILITIES = frozenset({
    "private", "scheduled", "login_required", "unknown", "error",
    "unavailable", "age_restricted", "public", "available",
})


def _pacing(db) -> dict:
    """Read the global safety profile without creating a live DB in callers."""
    try:
        from api.services.pacing_profile import get_pacing
        return get_pacing(db=db)
    except Exception:
        return {
            "max_longform_publish_day": 1,
            "same_channel_publish_gap_h": 24,
            "same_channel_upload_gap_h": 6,
            "generation_start_gap_min": 90,
            "global_generation_gap_min": 30,
        }


def _channel_config(channel_id: int, db) -> dict:
    try:
        row = db.get_channel(channel_id) or {}
        raw = row.get("config_json")
        config = json.loads(raw or "{}") if isinstance(raw, str) else (raw or {})
    except Exception:
        config = {}
    try:
        # Planning config is the authoritative DB representation when present.
        config.update(db.get_channel_planning_config(channel_id) or {})
    except Exception:
        pass
    return config


def get_channel_delivery_policy(channel_id: int, db) -> dict | None:
    """Read the explicit delivery policy once, preserving an explicit zero."""
    try:
        raw = db.get_system_state(f"channel_delivery_policy_{channel_id}")
        if not raw:
            return None
        data = json.loads(raw) if isinstance(raw, str) else raw
        if not isinstance(data, dict) or data.get("mode") != "explicit":
            return None
        def nonnegative_int(key: str, default: int) -> int:
            value = data.get(key, default)
            try:
                return max(0, int(value))
            except (TypeError, ValueError):
                _count("invalid_values")
                return default
        def boolean(key: str, default: bool) -> bool:
            value = data.get(key, default)
            if isinstance(value, bool):
                return value
            normalized = str(value).strip().lower()
            if normalized in ("1", "true", "yes", "on"):
                return True
            if normalized in ("0", "false", "no", "off"):
                return False
            _count("invalid_values")
            return default
        return {
            "mode": "explicit",
            "longs_per_day": nonnegative_int("longs_per_day", 1),
            "native_shorts_per_day": nonnegative_int("native_shorts_per_day", 1),
            "shorts_enabled": boolean("shorts_enabled", True),
            "clips_enabled": boolean("clips_enabled", False),
        }
    except (TypeError, ValueError, json.JSONDecodeError):
        _count("invalid_values")
        return None


def get_channel_strike_state(channel_id: int, db, now: float | None = None) -> dict:
    """Return strike history separately from whether the block is active."""
    now = time.time() if now is None else float(now)
    blocked_until = _float_state(db, f"shorts_spam_blocked_until_{channel_id}")
    return {
        "historical_strikes": _int_state(db, f"shorts_spam_strikes_{channel_id}"),
        "blocked_until": blocked_until,
        "strike_active": blocked_until is not None and blocked_until > now,
    }


def get_historical_strikes(channel_id: int, db) -> int:
    """Central accessor for the durable strike counter."""
    return _int_state(db, f"shorts_spam_strikes_{channel_id}")


def resolve_channel_policy_values(channel_id: int, db=None, config: dict | None = None) -> dict:
    """Resolve channel pacing while making the global profile a safety ceiling.

    A channel can be *more* conservative than the active profile.  It cannot
    raise caps or lower safety gaps, which keeps all scheduling entry points in
    agreement when the profile changes.
    """
    if db is None:
        from database.db_extended import ExtendedDatabase
        db = ExtendedDatabase()
    cfg = _channel_config(channel_id, db)
    if config:
        cfg.update(config)
    pacing = _pacing(db)
    def number(canonical, *legacy, default=None):
        if cfg.get(canonical) not in (None, ""):
            try:
                return int(float(cfg[canonical]))
            except (TypeError, ValueError):
                _count("invalid_values")
                return default
        for key in legacy:
            if cfg.get(key) not in (None, ""):
                _count("legacy_fallbacks")
                try:
                    return int(float(cfg[key]))
                except (TypeError, ValueError):
                    _count("invalid_values")
                    return default
        return default

    global_cap = max(0, int(pacing.get("max_longform_publish_day", 1) or 0))
    # La política explícita vive en system_state y tiene prioridad sobre los
    # campos de planificación legacy. El perfil continúa siendo el techo de
    # seguridad; un contador histórico de strikes no modifica este valor.
    explicit = get_channel_delivery_policy(channel_id, db)
    if explicit is not None:
        channel_cap = explicit["longs_per_day"]
    elif cfg.get("longs_per_day") not in (None, ""):
        channel_cap = number("longs_per_day", default=global_cap)
    else:
        channel_cap = number("max_longform_publish_day", "MAX_LONGFORM_PUBLISH_PER_DAY",
                             default=global_cap)
    publish_gap = number("same_channel_publish_gap_h",
                         "MIN_SAME_CHANNEL_PUBLISH_GAP_HOURS",
                         default=int(pacing.get("same_channel_publish_gap_h", 24)))
    upload_gap = number("same_channel_upload_gap_h",
                        "MIN_SAME_CHANNEL_UPLOAD_GAP_HOURS",
                        default=int(pacing.get("same_channel_upload_gap_h", 6)))
    spread = number("publish_window_spread_min", "PUBLISH_WINDOW_SPREAD_MIN",
                    "PUBLISH_JITTER_MIN", "publish_jitter_min", default=0)
    target = number("publish_target_hour", "PUBLISH_TARGET_HOUR", default=None)
    return {
        "channel_id": int(channel_id),
        "longform_publish_cap": max(0, min(channel_cap, global_cap)) if channel_cap is not None else global_cap,
        "same_channel_publish_gap_h": max(publish_gap, int(pacing.get("same_channel_publish_gap_h", 24))),
        "same_channel_upload_gap_h": max(upload_gap, int(pacing.get("same_channel_upload_gap_h", 6))),
        "publish_target_hour": target,
        "publish_window_spread_min": max(0, spread),
        "generation_start_gap_min": max(0, number("MIN_GAP_MINUTES", default=90)),
        "global_generation_gap_min": max(0, number("GLOBAL_GAP_MINUTES", default=30)),
    }


def _int_state(db, key: str) -> int:
    try:
        return max(0, int(db.get_system_state(key) or 0))
    except (TypeError, ValueError):
        _count("invalid_values")
        return 0


def _float_state(db, key: str):
    try:
        value = db.get_system_state(key)
        return float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        _count("invalid_values")
        return None


def resolve_channel_policy(channel_id: int, db=None, now: float | None = None) -> dict:
    """Return the effective, non-mutating policy state for one channel."""
    if db is None:
        from database.db_extended import ExtendedDatabase
        db = ExtendedDatabase()
    now = time.time() if now is None else float(now)
    strike = get_channel_strike_state(channel_id, db, now=now)
    active = strike["strike_active"]
    policy = {
        "channel_id": int(channel_id),
        "historical_strikes": strike["historical_strikes"],
        "blocked_until": strike["blocked_until"],
        "blocked": active,
        "strike_active": active,
        "scope": "all" if active else "none",
    }
    policy.update(resolve_channel_policy_values(channel_id, db=db))
    return policy


def policy_value(channel_id: int, key: str, db=None, default=None):
    """Small common accessor used by schedulers (never reads config directly)."""
    try:
        return resolve_channel_policy_values(channel_id, db=db).get(key, default)
    except Exception:
        return default


def resolve_policy_for_config(channel_id: int, config: dict, db=None) -> dict:
    """Resolve an in-memory planning row using the same global safety rules."""
    return resolve_channel_policy_values(channel_id, db=db, config=config)


def collect_channel_policy_snapshot(db, now: float | None = None) -> list[dict]:
    """Read current policies for all channels without changing the database."""
    snapshot = []
    for channel in db.get_channels(active_only=False) or []:
        channel_id = channel.get("id")
        if not channel_id:
            continue
        state = resolve_channel_policy(channel_id, db=db, now=now)
        state.update({
            "slug": channel.get("slug", ""),
            "name": channel.get("name", ""),
        })
        snapshot.append(state)
    return snapshot


def removal_is_confirmed(visibility: str, confirmations: int = 0) -> bool:
    """Only explicit removal evidence from at least two checks is actionable."""
    return visibility == REMOVAL_VISIBILITY and int(confirmations or 0) >= 2


def should_create_removal_alert(visibility: str, confirmations: int = 0) -> bool:
    """Guard alert/strike consumers against ambiguous visibility failures."""
    return removal_is_confirmed(visibility, confirmations)
