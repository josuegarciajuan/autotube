"""Voice preview & narration-demo synthesis (runtime, cached, 0 pre-generated files).

Synthesizes short audio clips on demand using the SAME engine/prosody path as
production (``config.voice_resolver.build_tts_engine``), so what the operator
hears in the panel equals what the pipeline will generate.

Cache: ``output/voice_previews/cache/{hash}.mp3`` (served via /api/static/...).
"""

import hashlib
import logging
import threading
from pathlib import Path
from typing import Any, Optional

from config.defaults import PROSODY_PROFILES, TONO_CATALOG, TONO_DEFAULT
from config.settings import OUTPUT_DIR

logger = logging.getLogger(__name__)

CACHE_DIR = OUTPUT_DIR / "voice_previews" / "cache"

# Serialize preview synthesis (Kokoro is RAM-heavy; avoid concurrent loads).
_SYNTH_LOCK = threading.Lock()

# ── Demo phrases per tone (long enough to hear the nuance) ────
TONE_DEMO_TEXT: dict[str, str] = {
    "neutro": (
        "Los archivos guardaban silencio. Durante décadas, nadie se atrevió a "
        "consultarlos, y sin embargo la respuesta llevaba allí todo el tiempo."
    ),
    "suspense": (
        "Algo no encajaba. La puerta estaba cerrada por dentro, la ventana no "
        "tenía salida, y sobre la mesa, aún humeante, una taza que nadie había "
        "servido esperaba a su dueño."
    ),
    "misterio": (
        "Los mapas más antiguos omitían aquel lugar a propósito. Quienes lo "
        "dibujaron sabían algo, y ese algo era precisamente lo que nadie debía "
        "encontrar jamás."
    ),
    "tension": (
        "El reloj avanzaba. Cada segundo pesaba más que el anterior, y en la "
        "oscuridad, muy cerca, algo empezó a moverse sin hacer ruido."
    ),
    "revelacion": (
        "Y entonces todo encajó. Las piezas que parecían inconexas formaban una "
        "sola figura, y la verdad, por fin, salió a la luz con una claridad "
        "sobrecogedora."
    ),
    "asombro": (
        "Nadie podía creerlo. Frente a ellos se alzaba una estructura imposible, "
        "más antigua que cualquier civilización conocida, y sin embargo "
        "perfectamente intacta."
    ),
    "tristeza": (
        "Aquel fue el último invierno. La casa quedó vacía, las cartas sin "
        "responder, y el nombre de quien se marchó se fue borrando despacio del "
        "recuerdo de todos."
    ),
    "esperanza": (
        "Pero aún quedaba una posibilidad. Pequeña, frágil, casi invisible, pero "
        "real. Y a veces, basta con eso para empezar de nuevo."
    ),
    "reflexion": (
        "Con el tiempo entendemos que la historia no la escriben los vencedores, "
        "sino quienes se atreven a recordar lo que los demás decidieron olvidar."
    ),
    "enfasis": (
        "Escucha bien esto: no fue un accidente, no fue casualidad, fue una "
        "decisión. Una sola decisión cambió el destino de todos los presentes."
    ),
    "cierre": (
        "Y así termina esta historia. Quizá nunca sepamos toda la verdad, pero "
        "lo que descubrimos ya es suficiente para no volver a mirar el pasado "
        "igual que antes."
    ),
}

# ── Long demo story cycling every tone ────────────────────────
RELATO_DEMO: list[dict] = [
    {"tono": "misterio", "texto": (
        "Durante siglos, aquel expediente permaneció oculto en los sótanos del "
        "archivo. Nadie sabía quién lo había escrito, ni por qué nadie debía leerlo."
    )},
    {"tono": "suspense", "texto": (
        "La primera página advertía con una sola frase: quien lo abra, no volverá "
        "a mirar el mundo de la misma manera. Y aun así, alguien lo abrió."
    )},
    {"tono": "tension", "texto": (
        "Las luces parpadearon. Un ruido seco recorrió el pasillo vacío y, por un "
        "instante, todo el edificio pareció contener la respiración."
    )},
    {"tono": "revelacion", "texto": (
        "Fue entonces cuando lo vio. La firma al pie del documento no pertenecía a "
        "ningún historiador: era su propio nombre, escrito cien años atrás."
    )},
    {"tono": "asombro", "texto": (
        "El hallazgo era imposible de explicar. Las fechas no mentían, los sellos "
        "eran auténticos, y el tiempo, sencillamente, se negaba a tener sentido."
    )},
    {"tono": "tristeza", "texto": (
        "Recordó entonces a todos los que habían buscado la verdad antes que él. "
        "Ninguno regresó, y ahora comprendía el precio que habían pagado."
    )},
    {"tono": "esperanza", "texto": (
        "Pero también comprendió algo más: mientras alguien siga preguntando, la "
        "verdad nunca muere del todo. Y él seguía preguntando."
    )},
    {"tono": "reflexion", "texto": (
        "Porque hay historias que no se cuentan para entretener, sino para que no "
        "olvidemos lo que somos capaces de hacer cuando dejamos de preguntar."
    )},
    {"tono": "cierre", "texto": (
        "Cerró el expediente despacio. Lo devolvió a su estante, apagó la luz, y "
        "en la oscuridad, muy lejos, alguien abrió la puerta del archivo."
    )},
]


def _cache_path(key: str) -> Path:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return CACHE_DIR / f"{key}.mp3"


def _make_key(*parts: Any) -> str:
    raw = "|".join(str(p) for p in parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def _get(cfg: Any, key: str, default=None):
    if cfg is None:
        return default
    if isinstance(cfg, dict):
        return cfg.get(key, default)
    return getattr(cfg, key, default)


def _build_preview_config(
    engine: str,
    voice: str,
    channel_cfg: Any = None,
    max_segments: int = 3,
) -> dict:
    """Build a channel-like config dict for preview synthesis."""
    profiles = _get(channel_cfg, "PROSODY_PROFILES", None) or PROSODY_PROFILES
    cfg: dict[str, Any] = {
        "TTS_ENGINE": engine,
        "VOICE_VOLUME": _get(channel_cfg, "VOICE_VOLUME", "+0%"),
        "VOICE_RATE": _get(channel_cfg, "VOICE_RATE", "+0%"),
        "VOICE_PITCH": _get(channel_cfg, "VOICE_PITCH", "+0Hz"),
        "TTS_STRATEGY": _get(channel_cfg, "TTS_STRATEGY", {}) or {},
        "EXPRESSIVE_NARRATION": True,
        "PROSODY_PROFILES": profiles,
        "TONO_CATALOG": list(profiles.keys()),
        "TONO_DEFAULT": _get(channel_cfg, "TONO_DEFAULT", TONO_DEFAULT),
        "TONO_MAX_SEGMENTS_PER_BLOCK": max_segments,
        "KOKORO_PAUSE_BETWEEN_BLOCKS": _get(
            channel_cfg, "KOKORO_PAUSE_BETWEEN_BLOCKS", 0.4
        ),
        "KOKORO_UNLOAD_EVERY_N_BLOCKS": 0,
    }
    if engine == "kokoro":
        cfg["KOKORO_VOICE"] = voice
    else:
        cfg["VOICE_ID"] = voice
    return cfg


def _synthesize(
    engine: str,
    voice: str,
    bloques: list[dict],
    cache_key: str,
    channel_cfg: Any = None,
    max_segments: int = 3,
) -> Path:
    out = _cache_path(cache_key)
    if out.exists() and out.stat().st_size > 0:
        return out

    from config.voice_resolver import build_tts_engine

    with _SYNTH_LOCK:
        # Re-check inside the lock (another request may have produced it).
        if out.exists() and out.stat().st_size > 0:
            return out
        cfg = _build_preview_config(engine, voice, channel_cfg, max_segments=max_segments)
        tts = build_tts_engine(cfg)
        tmp_base = out.with_suffix("")
        try:
            audio_path, _ts = tts.generate_segmented(bloques, output_path=str(tmp_base))
            produced = Path(audio_path)
            if produced != out:
                if out.exists():
                    out.unlink()
                produced.replace(out)
        finally:
            unload = getattr(tts, "unload", None)
            if callable(unload):
                try:
                    unload()
                except Exception:
                    pass
    return out


def synthesize_clip(
    engine: str,
    voice: str,
    tone: str = "neutro",
    text: Optional[str] = None,
    channel_cfg: Any = None,
) -> Path:
    """Synthesize a short single-tone preview clip (cached)."""
    tone = tone if tone in TONO_CATALOG else TONO_DEFAULT
    sample = (text or TONE_DEMO_TEXT.get(tone) or TONE_DEMO_TEXT[TONO_DEFAULT]).strip()
    profiles = _get(channel_cfg, "PROSODY_PROFILES", None) or PROSODY_PROFILES
    prof = profiles.get(tone, {}) or {}
    profile_tag = "{}|{}|{}|{}".format(
        prof.get("rate"), prof.get("pitch"), prof.get("volume"),
        prof.get("pause_after_ms"),
    )
    key = _make_key("clip", engine, voice, tone, sample, profile_tag)
    bloques = [{"tipo": "desarrollo", "tono": tone, "texto": sample}]
    return _synthesize(engine, voice, bloques, key, channel_cfg=channel_cfg, max_segments=1)


def synthesize_relato(
    engine: str,
    voice: str,
    channel_cfg: Any = None,
) -> Path:
    """Synthesize the long multi-tone demo story (cached)."""
    profiles = _get(channel_cfg, "PROSODY_PROFILES", None) or PROSODY_PROFILES
    profile_tag = "|".join(
        f"{t}:{p.get('rate')},{p.get('pitch')},{p.get('volume')}"
        for t, p in sorted(profiles.items())
    )
    key = _make_key("relato", engine, voice, "v1", profile_tag)
    bloques = [
        {"tipo": "desarrollo", "tono": seg["tono"], "texto": seg["texto"]}
        for seg in RELATO_DEMO
    ]
    return _synthesize(
        engine, voice, bloques, key,
        channel_cfg=channel_cfg, max_segments=len(RELATO_DEMO),
    )
