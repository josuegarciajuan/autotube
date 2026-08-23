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
