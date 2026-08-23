"""Pacing router — perfil central de cadencia ("strike mode" switch).

GET  /api/pacing/profile     → perfil activo + valores resueltos + canales
GET  /api/pacing/profiles    → schema completo de perfiles
PUT  /api/pacing/profile     → activar un perfil (strike | recovery | normal)

El perfil central gobierna TODAS las reglas de frecuencia y espaciado
(shorts/día, longform/día, gaps, spacing, caps de cuenta). Cambiar de perfil
relaja/endurece el sistema de golpe sin tocar código.
"""

import logging

from fastapi import APIRouter
from pydantic import BaseModel

from api.services import pacing_profile

logger = logging.getLogger("autotube.pacing.api")

router = APIRouter(prefix="/api/pacing", tags=["Pacing"])


class ProfileUpdate(BaseModel):
    profile: str


@router.get("/profile")
def get_profile():
    """Perfil activo: nombre, valores resueltos y canales activos."""
    return pacing_profile.get_pacing_summary()


@router.get("/profiles")
def get_profiles():
    """Schema completo de perfiles disponibles."""
    return {
        "active_profile": pacing_profile.get_active_profile_name(),
        "profiles": pacing_profile.list_pacing_profiles(),
    }


@router.put("/profile")
def set_profile(body: ProfileUpdate):
    """Activar un perfil de pacing (strike | recovery | normal)."""
    try:
        resolved = pacing_profile.set_pacing_profile(body.profile)
    except ValueError as exc:
        from fastapi import HTTPException
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "ok": True,
        "active_profile": body.profile,
        "pacing": resolved,
    }


@router.get("/factory-status")
def factory_status():
    """Estado de la fábrica continua: disco, créditos LLM y profundidad de cola.

    Backlog: awaiting_upload + warming (long-form) + shorts en cola ('generated').
    ETA (días) = backlog / capacidad diaria de publicación (perfil de pacing).
    """
    from database.db_extended import ExtendedDatabase
    from api.services import factory_governor
    from api.services.planning_service import continuous_generation_enabled

    db = ExtendedDatabase()

    # Profundidad de cola
    awaiting = 0
    warming = 0
    queued_shorts = 0
    try:
        awaiting = int(db.count_all_awaiting_upload() or 0)
        warming = int(db.count_all_warming() or 0)
        for ch in db.get_channels(active_only=True) or []:
            try:
                queued_shorts += int(db.count_queued_native_shorts(ch["id"]) or 0)
            except Exception:
                pass
    except Exception:
        pass

    # Capacidad diaria según perfil
    pacing = pacing_profile.get_pacing(db=db)
    channels = 0
    try:
        channels = len(db.get_channels(active_only=True) or [])
    except Exception:
        pass
    per_day = max(
        1, int(pacing.get("shorts_per_channel_day", 1) or 1)
        + int(pacing.get("max_longform_publish_day", 1) or 1)
    )
    daily_capacity = per_day * max(1, channels)
    backlog_items = awaiting + warming + queued_shorts
    eta_days = round(backlog_items / daily_capacity, 1) if daily_capacity else None

    # Créditos LLM
    credits = {}
    try:
        from api.services.llm_credit_checker import get_llm_credit_status
        st = get_llm_credit_status(db) or {}
        for prov in ("deepseek", "openai"):
            p = st.get(prov) or {}
            credits[prov] = {
                "status": p.get("status"),
                "balance_usd": p.get("balance_usd"),
                "has_quota": p.get("has_quota"),
            }
    except Exception:
        pass

    disk_free_mb = factory_governor.free_disk_mb()
    disk_min = factory_governor._default_min_free_disk_mb()
    return {
        "factory_ok": factory_governor.factory_ok(db),
        "disk": {
            "free_mb": disk_free_mb,
            "min_mb": disk_min,
            "ok": factory_governor.disk_ok(),
        },
        "credits": credits,
        "credits_ok": factory_governor.credits_ok(db),
        "backlog": {
            "awaiting_upload": awaiting,
            "warming": warming,
            "queued_shorts": queued_shorts,
            "total_items": backlog_items,
            "daily_capacity": daily_capacity,
            "eta_days": eta_days,
        },
        "continuous_generation": continuous_generation_enabled(db),
    }
