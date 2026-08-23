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
