"""Perfil central de cadencia (pacing) — "strike mode" switch.

Un único perfil persistido en ``system_state`` gobierna TODAS las reglas de
frecuencia y espaciado de subidas/publicaciones. Cambiar de perfil
(``strike`` → ``recovery`` → ``normal``) = un valor, y la fábrica de
generación + la válvula de publicación se reajustan solas.

Resolución de cada clave de pacing (de mayor a menor prioridad):

    1. ``system_state["pacing_<key>"]``  → override manual puntual (kill-switch)
    2. perfil activo (``system_state["pacing_profile"]``) → el switch central
    3. perfil ``strike`` (constante en este módulo) → fallback por defecto

Relación con ``config_json`` por canal:
    Para las claves de pacing, el perfil central GANA sobre ``config_json``
    por canal. Los campos per-channel ``MAX_LONGFORM_PUBLISH_PER_DAY``,
    ``MIN_SAME_CHANNEL_UPLOAD_GAP_HOURS`` y ``ACCOUNT_DAILY_UPLOAD_CAP``
    quedan deprecados como override: el perfil es la fuente única, de modo
    que relajar los strikes es UN clic y todos los canales se adaptan.

Los valores del perfil ``strike`` coinciden EXACTAMENTE con las constantes
antiban actuales (ago 2026), de modo que activar el perfil strike NO cambia
el comportamiento de producción actual.
"""

from __future__ import annotations

import logging

logger = logging.getLogger("autotube.pacing")

# ── Perfiles (schema) ──────────────────────────────────────────
# Valores aprobados en el plan (Fase 0, ago 2026):
#   strike  → situación actual de strikes (1 short/día, 1 longform/día, ...)
#   recovery → relajación gradual
#   normal  → frecuencia máxima objetivo
PACING_PROFILES: dict[str, dict] = {
    "strike": {
        "shorts_per_channel_day": 1,
        "shorts_global_day": 6,
        "max_longform_publish_day": 1,
        "same_channel_publish_gap_h": 24,
        "same_channel_upload_gap_h": 6,
        "global_upload_spacing_min": 45,
        "account_daily_upload_cap": 4,
        "shorts_cooldown_min": 180,
        "shorts_same_type_gap_min": 240,
        "shorts_cross_type_gap_min": 20,
        "shorts_min_gap_min": 20,
        "content_safety_disabled": False,
        "marathon_backlog_per_channel": 4,
    },
    "recovery": {
        "shorts_per_channel_day": 2,
        "shorts_global_day": 8,
        "max_longform_publish_day": 1,
        "same_channel_publish_gap_h": 12,
        "same_channel_upload_gap_h": 4,
        "global_upload_spacing_min": 30,
        "account_daily_upload_cap": 6,
        "shorts_cooldown_min": 120,
        "shorts_same_type_gap_min": 180,
        "shorts_cross_type_gap_min": 20,
        "shorts_min_gap_min": 20,
        "content_safety_disabled": False,
        "marathon_backlog_per_channel": 3,
    },
    "normal": {
        "shorts_per_channel_day": 3,
        "shorts_global_day": 12,
        "max_longform_publish_day": 2,
        "same_channel_publish_gap_h": 6,
        "same_channel_upload_gap_h": 3,
        "global_upload_spacing_min": 20,
        "account_daily_upload_cap": 8,
        "shorts_cooldown_min": 90,
        "shorts_same_type_gap_min": 120,
        "shorts_cross_type_gap_min": 20,
        "shorts_min_gap_min": 20,
        "content_safety_disabled": False,
        "marathon_backlog_per_channel": 2,
    },
}

# Perfil por defecto al arrancar (persistido en system_state si no existe).
DEFAULT_PROFILE = "strike"

_STATE_KEY = "pacing_profile"
_OVERRIDE_PREFIX = "pacing_"

# ── DB lazy ────────────────────────────────────────────────────

def _get_db():
    from database.db_extended import ExtendedDatabase
    from config.settings import DATABASE_PATH
    return ExtendedDatabase(str(DATABASE_PATH))


# ── API pública ────────────────────────────────────────────────

def list_pacing_profiles() -> dict[str, dict]:
    """Devuelve el schema completo de perfiles (para UI/API)."""
    return {name: dict(values) for name, values in PACING_PROFILES.items()}


def get_active_profile_name(db=None) -> str:
    """Nombre del perfil activo. Valida contra el schema; fallback a strike."""
    if db is None:
        db = _get_db()
    try:
        raw = db.get_system_state(_STATE_KEY)
    except Exception:
        raw = None
    if raw and raw in PACING_PROFILES:
        return raw
    return DEFAULT_PROFILE


def get_pacing(db=None) -> dict:
    """Perfil activo resuelto: perfil base + overrides manuales ``pacing_<key>``."""
    if db is None:
        db = _get_db()
    profile = PACING_PROFILES[get_active_profile_name(db)]
    resolved = dict(profile)
    # Aplicar overrides manuales puntuales (kill-switch por clave)
    for key in profile:
        override_key = f"{_OVERRIDE_PREFIX}{key}"
        try:
            raw = db.get_system_state(override_key)
        except Exception:
            raw = None
        if raw is not None and raw != "":
            resolved[key] = _coerce(key, raw)
    return resolved


def get_pacing_value(key: str, default=None, db=None):
    """Valor resuelto de una clave de pacing (override manual > perfil > default)."""
    try:
        pacing = get_pacing(db=db)
        if key in pacing:
            return pacing[key]
    except Exception:
        pass
    return default


def set_pacing_profile(name: str, db=None) -> dict:
    """Activa un perfil. Persiste en system_state y devuelve el pacing resuelto.

    Raises:
        ValueError: si el nombre del perfil no existe en el schema.
    """
    if name not in PACING_PROFILES:
        raise ValueError(
            f"Perfil desconocido: {name!r}. Disponibles: {list(PACING_PROFILES)}"
        )
    if db is None:
        db = _get_db()
    previous = get_active_profile_name(db)
    db.set_system_state(_STATE_KEY, name)
    resolved = get_pacing(db=db)
    logger.info(
        "Pacing: perfil %s → %s. shorts/día=%s longform/día=%s spacing=%smin "
        "gap_pub=%sh gap_upload=%sh account_cap=%s",
        previous, name,
        resolved.get("shorts_per_channel_day"),
        resolved.get("max_longform_publish_day"),
        resolved.get("global_upload_spacing_min"),
        resolved.get("same_channel_publish_gap_h"),
        resolved.get("same_channel_upload_gap_h"),
        resolved.get("account_daily_upload_cap"),
    )
    return resolved


def get_pacing_summary(db=None) -> dict:
    """Resumen para UI: perfil activo + valores resueltos + canales activos."""
    if db is None:
        db = _get_db()
    active = get_active_profile_name(db)
    resolved = get_pacing(db=db)
    channels = []
    try:
        for ch in db.get_channels(active_only=True) or []:
            channels.append({
                "id": ch.get("id"),
                "slug": ch.get("slug"),
                "name": ch.get("name"),
                "google_account": ch.get("google_account") or "",
            })
    except Exception:
        pass
    return {
        "active_profile": active,
        "available_profiles": list(PACING_PROFILES),
        "pacing": resolved,
        "active_channels": channels,
    }


# ── Helpers ────────────────────────────────────────────────────

def _coerce(key: str, raw: str):
    """Convierte el string de system_state al tipo de la clave del schema."""
    if key in ("content_safety_disabled",):
        return str(raw).strip().lower() in ("1", "true", "yes", "on")
    try:
        profile_value = PACING_PROFILES[DEFAULT_PROFILE].get(key)
    except Exception:
        profile_value = None
    if isinstance(profile_value, bool):
        return str(raw).strip().lower() in ("1", "true", "yes", "on")
    if isinstance(profile_value, int):
        try:
            return int(float(str(raw)))
        except (TypeError, ValueError):
            return profile_value
    if isinstance(profile_value, float):
        try:
            return float(str(raw))
        except (TypeError, ValueError):
            return profile_value
    return str(raw)


# ═══════════════════════════════════════════════════════════════
# Transición automática de perfil (Fase 4 bis)
# ═══════════════════════════════════════════════════════════════
# Tras N días SIN actividad de strike (bloqueos, remociones, eliminaciones
# silenciosas), el sistema escala solo: strike → recovery → normal. Kill-switch:
# settings.AUTO_PACING_TRANSITION=False o system_state["auto_pacing_transition"]
# = "false" desactiva la transición automática.

def _auto_transition_recovery_days() -> int:
    try:
        from config.settings import AUTO_TRANSITION_RECOVERY_DAYS
        return max(1, int(AUTO_TRANSITION_RECOVERY_DAYS or 7))
    except Exception:
        return 7


def _auto_transition_normal_days() -> int:
    try:
        from config.settings import AUTO_TRANSITION_NORMAL_DAYS
        return max(1, int(AUTO_TRANSITION_NORMAL_DAYS or 21))
    except Exception:
        return 21


def auto_transition_enabled(db=None) -> bool:
    """Kill-switch de la transición automática (settings > system_state)."""
    try:
        if db is None:
            db = _get_db()
        raw = db.get_system_state("auto_pacing_transition")
        if raw is not None and raw != "":
            return str(raw).strip().lower() in ("1", "true", "yes", "on")
    except Exception:
        pass
    try:
        from config.settings import AUTO_PACING_TRANSITION
        return bool(AUTO_PACING_TRANSITION)
    except Exception:
        return True


def last_strike_activity_epoch(db=None) -> float:
    """Epoch del último evento de strike/remoción (global, todos los canales).

    Fuentes: bloqueos activos/vencidos (shorts_spam_blocked_until_*), última
    remoción registrada (shorts_spam_last_removal_*.detected_at) y alertas
    spam_strike / silent_removal en pipeline_alerts. 0.0 si nunca hubo.
    """
    import json as _json
    from datetime import datetime as _dt

    if db is None:
        db = _get_db()
    last = 0.0
    now_epoch = __import__("time").time()
    try:
        for ch in db.get_channels(active_only=False) or []:
            cid = ch.get("id")
            if not cid:
                continue
            try:
                raw_block = db.get_system_state(f"shorts_spam_blocked_until_{cid}")
                if raw_block:
                    ts = float(raw_block)
                    # Un bloqueo ACTIVO cuenta como actividad 'ahora mismo'
                    last = max(last, now_epoch if ts > now_epoch else ts)
            except (TypeError, ValueError):
                pass
            try:
                raw_rem = db.get_system_state(f"shorts_spam_last_removal_{cid}")
                if raw_rem:
                    d = _json.loads(raw_rem)
                    detected = d.get("detected_at") if isinstance(d, dict) else None
                    if detected:
                        ts = _dt.fromisoformat(str(detected).replace("Z", "+00:00")).timestamp()
                        last = max(last, ts)
            except Exception:
                pass
    except Exception:
        pass
    # Alertas recientes de strike / eliminación silenciosa
    try:
        with db._connect() as conn:
            rows = conn.execute(
                """SELECT created_at FROM pipeline_alerts
                   WHERE alert_type IN ('spam_strike', 'silent_removal')
                   ORDER BY created_at DESC LIMIT 1""",
            ).fetchone()
        if rows and rows["created_at"]:
            try:
                last = max(last, _dt.strptime(rows["created_at"], "%Y-%m-%d %H:%M:%S").timestamp())
            except (ValueError, TypeError):
                pass
    except Exception:
        pass
    return last


def clean_days_since_strike(db=None) -> float:
    """Días consecutivos SIN actividad de strike (0 si hay bloqueo activo)."""
    import time as _time
    if db is None:
        db = _get_db()
    last = last_strike_activity_epoch(db)
    if last <= 0:
        return float("inf")  # nunca hubo strike → limpio desde siempre
    return max(0.0, (_time.time() - last) / 86400.0)


def auto_transition_profile(db=None) -> dict:
    """Escala el perfil automáticamente si hay suficientes días limpios.

    strike → recovery tras AUTO_TRANSITION_RECOVERY_DAYS (7) días limpios.
    recovery → normal tras AUTO_TRANSITION_NORMAL_DAYS (21) días limpios.
    normal → no-op (ya es el máximo).

    Returns:
        {"transitioned": bool, "from": str, "to": str, "clean_days": float}
    """
    if db is None:
        db = _get_db()
    if not auto_transition_enabled(db):
        return {"transitioned": False, "from": get_active_profile_name(db),
                "to": get_active_profile_name(db), "clean_days": clean_days_since_strike(db),
                "reason": "kill-switch"}
    current = get_active_profile_name(db)
    clean = clean_days_since_strike(db)
    if current == "strike" and clean >= _auto_transition_recovery_days():
        set_pacing_profile("recovery", db)
        logger.info(
            "Pacing: transición automática strike → recovery tras %.0f días sin strikes",
            clean,
        )
        return {"transitioned": True, "from": "strike", "to": "recovery",
                "clean_days": round(clean, 1), "reason": "clean_days"}
    if current == "recovery" and clean >= _auto_transition_normal_days():
        set_pacing_profile("normal", db)
        logger.info(
            "Pacing: transición automática recovery → normal tras %.0f días sin strikes",
            clean,
        )
        return {"transitioned": True, "from": "recovery", "to": "normal",
                "clean_days": round(clean, 1), "reason": "clean_days"}
    return {"transitioned": False, "from": current, "to": current,
            "clean_days": round(clean, 1) if clean != float("inf") else None,
            "reason": "not_clean"}
