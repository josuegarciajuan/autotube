"""Voice endpoints — catalog + on-demand preview synthesis with per-tone demos.

Previews are synthesized at runtime using the production TTS path (cached in
``output/voice_previews/cache``), so they always reflect the actual engine,
voice and prosody. No pre-generated static files required.
"""
import logging
from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel

from config.defaults import TONO_CATALOG
from api.services import voice_preview_service as vps

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/voices", tags=["voices"])


# ── Static voice catalog ──────────────────────────────────────
def _edge(key: str, name: str, gender: str, tag: str) -> dict:
    return {
        "key": f"edgetts:{key}",
        "name": name,
        "engine": "edgetts",
        "engine_label": "Edge-TTS (cloud)",
        "gender": gender,
        "tag": tag,
    }


def _kokoro(key: str, name: str, gender: str, tag: str) -> dict:
    return {
        "key": f"kokoro:{key}",
        "name": name,
        "engine": "kokoro",
        "engine_label": "Kokoro (local)",
        "gender": gender,
        "tag": tag,
    }


VOICES_CATALOG = [
    # ── Kokoro (local, 3 voces ES) ────────────────────────────
    _kokoro("ef_dora", "Dora", "female", "cálida"),
    _kokoro("em_alex", "Alex", "male", "neutra"),
    _kokoro("em_santa", "Santa", "male", "grave"),
    # ── Edge-TTS (cloud, voces ES) ────────────────────────────
    _edge("es-ES-AlvaroNeural", "Álvaro", "male", "grave · España"),
    _edge("es-ES-ElviraNeural", "Elvira", "female", "documental · España"),
    _edge("es-ES-AbrilNeural", "Abril", "female", "joven · España"),
    _edge("es-MX-DaliaNeural", "Dalia", "female", "cálida · México"),
    _edge("es-MX-JorgeNeural", "Jorge", "male", "neutra · México"),
    _edge("es-AR-TomasNeural", "Tomás", "male", "intensa · Argentina"),
    _edge("es-AR-ElenaNeural", "Elena", "female", "serena · Argentina"),
    _edge("es-CO-GonzaloNeural", "Gonzalo", "male", "cercana · Colombia"),
    _edge("es-CO-SalomeNeural", "Salomé", "female", "expresiva · Colombia"),
    _edge("es-CL-LorenzoNeural", "Lorenzo", "male", "sobria · Chile"),
    _edge("es-CL-CatalinaNeural", "Catalina", "female", "clara · Chile"),
    _edge("es-PE-AlexNeural", "Álex", "male", "joven · Perú"),
    _edge("es-PE-CamilaNeural", "Camila", "female", "dulce · Perú"),
    _edge("es-VE-SebastianNeural", "Sebastián", "male", "teatral · Venezuela"),
    _edge("es-VE-PaolaNeural", "Paola", "female", "enérgica · Venezuela"),
    _edge("es-US-PalomaNeural", "Paloma", "female", "neutra · EE.UU."),
    _edge("es-US-AlonsoNeural", "Alonso", "male", "profunda · EE.UU."),
    _edge("es-CU-ManuelNeural", "Manuel", "male", "cálida · Cuba"),
    _edge("es-CU-BelkysNeural", "Belkys", "female", "narradora · Cuba"),
]

_VALID_ENGINES = {"kokoro", "edgetts"}
_VOICE_KEYS = {v["key"] for v in VOICES_CATALOG}


def _validate_voice(engine: str, voice: str) -> None:
    if engine not in _VALID_ENGINES:
        raise HTTPException(400, f"Engine no válido: {engine}")
    # Allow any voice string (edge voices not in catalog should still work),
    # but warn-level reject empty.
    if not voice:
        raise HTTPException(400, "Falta 'voice'")


def _channel_cfg(slug: Optional[str]):
    if not slug:
        return None
    try:
        from config.config_bridge import get_channel_config
        return get_channel_config(slug)
    except Exception as exc:  # noqa: BLE001
        logger.warning("No se pudo cargar config del canal '%s': %s", slug, exc)
        return None


@router.get("")
async def list_voices():
    """Return all available TTS voices plus the supported tone vocabulary."""
    return {
        "voices": VOICES_CATALOG,
        "tones": list(TONO_CATALOG),
        "total": len(VOICES_CATALOG),
    }


@router.get("/clip")
def voice_clip(
    engine: str = Query(...),
    voice: str = Query(...),
    tone: str = Query("neutro"),
    slug: Optional[str] = Query(None),
):
    """Synthesize (and cache) a short preview clip for one voice/tone."""
    _validate_voice(engine, voice)
    try:
        path = vps.synthesize_clip(engine, voice, tone=tone, channel_cfg=_channel_cfg(slug))
    except Exception as exc:  # noqa: BLE001
        logger.error("voice_clip failed (%s/%s/%s): %s", engine, voice, tone, exc)
        raise HTTPException(500, f"Error sintetizando preview: {exc}")
    return FileResponse(
        path, media_type="audio/mpeg",
        headers={"Cache-Control": "public, max-age=86400"},
    )


@router.get("/relato")
def voice_relato(
    engine: str = Query(...),
    voice: str = Query(...),
    slug: Optional[str] = Query(None),
):
    """Synthesize (and cache) the long multi-tone demo story."""
    _validate_voice(engine, voice)
    try:
        path = vps.synthesize_relato(engine, voice, channel_cfg=_channel_cfg(slug))
    except Exception as exc:  # noqa: BLE001
        logger.error("voice_relato failed (%s/%s): %s", engine, voice, exc)
        raise HTTPException(500, f"Error sintetizando relato: {exc}")
    return FileResponse(
        path, media_type="audio/mpeg",
        headers={"Cache-Control": "public, max-age=86400"},
    )


class PreviewRequest(BaseModel):
    engine: str
    voice: str
    text: str
    tone: str = "neutro"
    slug: Optional[str] = None


@router.post("/preview")
def voice_preview(body: PreviewRequest):
    """Synthesize an arbitrary text sample with a given voice/tone."""
    _validate_voice(body.engine, body.voice)
    if not body.text.strip():
        raise HTTPException(400, "Falta 'text'")
    try:
        path = vps.synthesize_clip(
            body.engine, body.voice, tone=body.tone,
            text=body.text, channel_cfg=_channel_cfg(body.slug),
        )
    except Exception as exc:  # noqa: BLE001
        logger.error("voice_preview failed (%s/%s): %s", body.engine, body.voice, exc)
        raise HTTPException(500, f"Error sintetizando preview: {exc}")
    return FileResponse(
        path, media_type="audio/mpeg",
        headers={"Cache-Control": "public, max-age=86400"},
    )
