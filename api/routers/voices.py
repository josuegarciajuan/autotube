"""Voice listing endpoint — returns available TTS voices with preview URLs."""
from fastapi import APIRouter

router = APIRouter(prefix="/voices", tags=["voices"])

# ── Static voice catalog ──────────────────────────────────────
# Preview audio files live in output/voice_previews/{engine}/{key}.mp3
# and are served via the existing /api/static/… endpoint.

VOICES_CATALOG = [
    {
        "key": "kokoro:ef_dora",
        "name": "Dora",
        "engine": "kokoro",
        "engine_label": "Kokoro (local)",
        "gender": "female",
        "preview_url": "api/static/voice_previews/kokoro/ef_dora.mp3",
    },
    {
        "key": "kokoro:em_alex",
        "name": "Alex",
        "engine": "kokoro",
        "engine_label": "Kokoro (local)",
        "gender": "male",
        "preview_url": "api/static/voice_previews/kokoro/em_alex.mp3",
    },
    {
        "key": "kokoro:em_santa",
        "name": "Santa",
        "engine": "kokoro",
        "engine_label": "Kokoro (local)",
        "gender": "male",
        "preview_url": "api/static/voice_previews/kokoro/em_santa.mp3",
    },
    {
        "key": "edgetts:es-MX-DaliaNeural",
        "name": "Dalia",
        "engine": "edgetts",
        "engine_label": "Edge-TTS (cloud)",
        "gender": "female",
        "preview_url": "api/static/voice_previews/edgetts/es-MX-DaliaNeural.mp3",
    },
    {
        "key": "edgetts:es-MX-JorgeNeural",
        "name": "Jorge",
        "engine": "edgetts",
        "engine_label": "Edge-TTS (cloud)",
        "gender": "male",
        "preview_url": "api/static/voice_previews/edgetts/es-MX-JorgeNeural.mp3",
    },
    {
        "key": "edgetts:es-CO-GonzaloNeural",
        "name": "Gonzalo",
        "engine": "edgetts",
        "engine_label": "Edge-TTS (cloud)",
        "gender": "male",
        "preview_url": "api/static/voice_previews/edgetts/es-CO-GonzaloNeural.mp3",
    },
]


@router.get("")
async def list_voices():
    """Return all available TTS voices with metadata and preview audio URLs."""
    return {"voices": VOICES_CATALOG, "total": len(VOICES_CATALOG)}
