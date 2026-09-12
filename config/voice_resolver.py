"""Voice resolver — single source of truth for channel TTS voice and engine.

Replicates the panel's voice selection logic (VoiceSelector.tsx:120-130):
  - engine = TTS_ENGINE (default "edgetts")
  - kokoro → KOKORO_VOICE
  - edgetts → VOICE_ID (user-selected), with TTS_STRATEGY.voice_primary as fallback

Provides a factory ``build_tts_engine(config)`` that returns the correct
engine instance for any given channel config (module, dict, or SimpleNamespace).
"""

import logging
import re
from typing import Optional, Union, Any

logger = logging.getLogger(__name__)

# ── Helper: normalise any config object ──────────────────────

def _get(cfg: Any, key: str, default=None) -> Any:
    """Extract a value from a dict, SimpleNamespace, or module."""
    if isinstance(cfg, dict):
        return cfg.get(key, default)
    return getattr(cfg, key, default)


# ── Expressive narration: canonical tones & emotion hints ────

# Mapa de palabras clave (substring, sin acentos) → tono canónico.
# Se usa como respaldo cuando el LLM no emite un "tono" canónico.
_TONO_KEYWORDS = [
    ("suspense", "suspense"), ("suspenso", "suspense"), ("intriga", "suspense"),
    ("mister", "misterio"), ("enigma", "misterio"), ("oculto", "misterio"),
    ("tension", "tension"), ("tension", "tension"), ("peligro", "tension"),
    ("miedo", "tension"), ("terror", "tension"), ("panico", "tension"),
    ("revela", "revelacion"), ("descubr", "revelacion"), ("giro", "revelacion"),
    ("desenlace", "revelacion"), ("sorpres", "asombro"),
    ("asombr", "asombro"), ("increib", "asombro"), ("impact", "asombro"),
    ("trist", "tristeza"), ("dolor", "tristeza"), ("melancol", "tristeza"),
    ("esperanz", "esperanza"), ("alivio", "esperanza"), ("luz", "esperanza"),
    ("reflex", "reflexion"), ("reflexi", "reflexion"), ("medita", "reflexion"),
    ("enfasis", "enfasis"), ("enfatic", "enfasis"), ("clave", "enfasis"),
    ("cierre", "cierre"), ("final", "cierre"), ("conclusion", "cierre"),
]

_TIPO_AS_TONO = {
    "hook": "suspense",
    "desarrollo": "reflexion",
    "climax": "tension",
    "reflexion": "reflexion",
    "cierre": "cierre",
    "intro": "neutro",
    "cta": "esperanza",
    "subscribe_cta": "esperanza",
    "desarrollo1": "reflexion",
    "desarrollo2": "reflexion",
    "desarrollo3": "reflexion",
}


def _strip_accents(text: str) -> str:
    """Lowercase + remove common Spanish accents for keyword matching."""
    table = str.maketrans("áéíóúüñÁÉÍÓÚÜÑ", "aeiouunAEIOUUN")
    return text.translate(table).lower()


def _tono_from_emotion(emocion: str) -> Optional[str]:
    """Map a free-text emotion to a canonical tone (best effort)."""
    if not emocion:
        return None
    low = _strip_accents(str(emocion))
    for needle, tono in _TONO_KEYWORDS:
        if needle in low:
            return tono
    return None


# ── Content-based tone inference (fallback adaptativo) ────────
# Se aplica cuando el LLM no aporta un tono ni una emoción reconocible
# (p. ej. guiones antiguos o bloques sin metadatos). Analiza el propio
# texto del bloque para decidir CÓMO debe sonar.
# Señales de alta precisión (evitamos conectores genéricos como "entonces"
# que provocarían falsos positivos). El orden define la prioridad.
_TEXT_CUES: list[tuple[str, list[str]]] = [
    ("revelacion", [
        "revel", "descubr", "en realidad", "la verdad", "resultó", "resulto",
        "giro inesperado", "desenlace", "lo que nadie sabía", "lo que nadie sabia",
    ]),
    ("asombro", [
        "increíble", "increible", "asombr", "imposible", "sorprendente",
        "extraordinari", "nunca antes",
    ]),
    ("tristeza", [
        "murió", "murio", "murieron", "perdió", "perdio", "perdieron",
        "nunca más", "nunca mas", "lágrim", "lagrim", "adiós", "adios",
        "quedó vacía", "quedo vacia",
    ]),
    ("esperanza", [
        "esperanza", "futuro", "renac", "nuevo comienzo", "una posibilidad",
    ]),
    ("tension", [
        "de repente", "de pronto", "peligro", "miedo", "terror", "sangre",
        "gritó", "grito", "dispar", "escap", "persegu", "trampa",
    ]),
    ("misterio", [
        "misterio", "enigma", "secreto", "ocult", "desaparec",
        "inexplicable", "silencio sepulcral",
    ]),
    ("suspense", [
        "suspenso", "intriga", "extraño", "extrano", "oscur", "¿", "?", "...",
    ]),
    ("enfasis", [
        "precisamente", "exactamente", "fue una decisión", "fue una decision",
        "lo más importante", "lo mas importante",
    ]),
    ("reflexion", [
        "la historia", "aprend", "comprendió", "comprendio", "con el tiempo",
        "lección", "leccion", "tal vez", "quizá", "quiza",
    ]),
]


def infer_tono_from_text(texto: str) -> Optional[str]:
    """Infer a canonical tone from the block's own text (best effort).

    Used as an adaptive fallback so narration varies per paragraph even when
    the LLM did not annotate an explicit ``tono``.
    """
    if not texto:
        return None
    low = str(texto).lower()
    for tono, cues in _TEXT_CUES:
        for cue in cues:
            if cue in low:
                return tono
    return None


# ── Public API ───────────────────────────────────────────────

def resolve_channel_voice(config: Any) -> dict:
    """Resolve the actual voice settings for a channel.

    Returns a dict with:
      - engine: "kokoro" or "edgetts"
      - voice: voice ID string (edge voice id or kokoro voice name)
      - rate: base rate string (edge) or None (kokoro uses block_speeds)
      - pitch: base pitch string (edge) or None
      - volume: volume string
      - block_speeds: dict (kokoro) or None (edge)
      - tts_strategy: full TTS_STRATEGY dict for per-block rate/pitch
    """
    engine = _get(config, "TTS_ENGINE", "edgetts")
    tts_strategy = _get(config, "TTS_STRATEGY", {}) or {}

    expressive = bool(_get(config, "EXPRESSIVE_NARRATION", True))
    profiles = _get(config, "PROSODY_PROFILES", {}) or {}
    tono_catalog = _get(config, "TONO_CATALOG", None) or list(profiles.keys())
    tono_default = _get(config, "TONO_DEFAULT", "neutro")
    max_segments = int(_get(config, "TONO_MAX_SEGMENTS_PER_BLOCK", 3) or 3)

    if engine == "kokoro":
        voice = _get(config, "KOKORO_VOICE", "em_santa")
        # Build block_speeds from TTS_STRATEGY rate values
        block_speeds = _kokoro_block_speeds(tts_strategy)
        return {
            "engine": "kokoro",
            "voice": voice,
            "rate": None,
            "pitch": None,
            "volume": _get(config, "VOICE_VOLUME", "+0%"),
            "block_speeds": block_speeds,
            "tts_strategy": tts_strategy,
            "expressive": expressive,
            "prosody_profiles": profiles,
            "tono_catalog": tono_catalog,
            "tono_default": tono_default,
            "max_segments": max_segments,
        }
    else:
        # edge-tts: use VOICE_ID (panel selection) with voice_primary as fallback
        voice = _get(config, "VOICE_ID")
        if not voice:
            voice = tts_strategy.get("voice_primary", "es-ES-AlvaroNeural")
        rate = tts_strategy.get("rate_base", _get(config, "VOICE_RATE", "+5%"))
        pitch = tts_strategy.get("pitch_base", _get(config, "VOICE_PITCH", "+0Hz"))
        volume = _get(config, "VOICE_VOLUME", "+0%")
        return {
            "engine": "edgetts",
            "voice": voice,
            "rate": rate,
            "pitch": pitch,
            "volume": volume,
            "block_speeds": None,
            "tts_strategy": tts_strategy,
            "expressive": expressive,
            "prosody_profiles": profiles,
            "tono_catalog": tono_catalog,
            "tono_default": tono_default,
            "max_segments": max_segments,
        }


# ── Prosody resolution ───────────────────────────────────────

def normalize_tono(
    config: Any,
    tono: Optional[str] = None,
    emocion: str = "",
    tipo: str = "",
    texto: str = "",
) -> str:
    """Resolve a raw tone value to a canonical tone.

    Priority: explicit canonical ``tono`` > emotion keywords > content cues
    > block type map > configured default.
    """
    profiles = _get(config, "PROSODY_PROFILES", {}) or {}
    catalog = set(_get(config, "TONO_CATALOG", None) or profiles.keys())
    default = _get(config, "TONO_DEFAULT", "neutro") or "neutro"

    if tono:
        candidate = _strip_accents(str(tono)).strip()
        # Direct canonical match (accent-insensitive).
        for canon in catalog:
            if _strip_accents(canon) == candidate:
                return canon
        # Fuzzy: keyword inside the tone string.
        mapped = _tono_from_emotion(str(tono))
        if mapped and (not catalog or mapped in catalog):
            return mapped

    mapped = _tono_from_emotion(emocion)
    if mapped and (not catalog or mapped in catalog):
        return mapped

    # Adaptive fallback: infer the tone from what the block actually says.
    mapped = infer_tono_from_text(texto)
    if mapped and (not catalog or mapped in catalog):
        return mapped

    tkey = _strip_accents(str(tipo or ""))
    # Strip trailing digits (desarrollo1 → desarrollo).
    tkey = re.sub(r"\d+$", "", tkey) or tkey
    mapped = _TIPO_AS_TONO.get(tkey)
    if mapped and (not catalog or mapped in catalog):
        return mapped

    return default if (not catalog or default in catalog) else (next(iter(catalog), "neutro"))


def resolve_prosody(
    config: Any,
    tono: Optional[str] = None,
    emocion: str = "",
    tipo: str = "",
    texto: str = "",
) -> dict:
    """Return the prosody settings to apply to a segment.

    Returns ``{tono, rate, pitch, volume, pause_after_ms}``. When expressive
    narration is disabled, falls back to the legacy per-``tipo`` rate/pitch
    from ``TTS_STRATEGY`` with no expressive pause.
    """
    expressive = bool(_get(config, "EXPRESSIVE_NARRATION", True))
    profiles = _get(config, "PROSODY_PROFILES", {}) or {}
    strategy = _get(config, "TTS_STRATEGY", {}) or {}
    base_rate = _get(config, "VOICE_RATE", "+0%")
    base_pitch = _get(config, "VOICE_PITCH", "+0Hz")
    base_volume = _get(config, "VOICE_VOLUME", "+0%")

    if not expressive or not profiles:
        tkey = re.sub(r"\d+$", "", _strip_accents(str(tipo or ""))) or str(tipo or "")
        rate = strategy.get(f"rate_{tkey}", strategy.get("rate_base", base_rate))
        pitch = strategy.get(f"pitch_{tkey}", strategy.get("pitch_base", base_pitch))
        return {
            "tono": None,
            "rate": rate,
            "pitch": pitch,
            "volume": base_volume,
            "pause_after_ms": 0,
        }

    resolved_tono = normalize_tono(config, tono=tono, emocion=emocion, tipo=tipo, texto=texto)
    profile = profiles.get(resolved_tono, {}) or {}
    return {
        "tono": resolved_tono,
        "rate": profile.get("rate", base_rate),
        "pitch": profile.get("pitch", base_pitch),
        "volume": profile.get("volume", base_volume),
        "pause_after_ms": int(profile.get("pause_after_ms", 0) or 0),
    }


def split_block_segments(
    bloque: dict,
    config: Any,
    max_segments: int = 3,
    expressive: bool = True,
) -> list[dict]:
    """Split a block into toned, prosody-resolved segments.

    Returns a list of dicts with: ``texto, tono, emocion, tipo, rate, pitch,
    volume, pause_after_ms``. Consecutive segments that resolve to the same
    tone are merged, and the result is capped at ``max_segments`` (extra tail
    segments are merged into the last one).
    """
    tipo = bloque.get("tipo", "desarrollo")
    emocion = bloque.get("emocion", "")
    raw = bloque.get("segmentos") if expressive else None

    candidates: list[dict] = []
    if isinstance(raw, list) and raw:
        for seg in raw:
            if not isinstance(seg, dict):
                continue
            stext = str(seg.get("texto") or "").strip()
            if not stext:
                continue
            candidates.append({
                "texto": stext,
                "tono": seg.get("tono"),
                "emocion": seg.get("emocion", emocion),
                "tipo": tipo,
            })
    if not candidates:
        candidates = [{
            "texto": str(bloque.get("texto") or "").strip(),
            "tono": bloque.get("tono"),
            "emocion": emocion,
            "tipo": tipo,
        }]

    merged: list[dict] = []
    for seg in candidates:
        if not seg["texto"]:
            continue
        pros = resolve_prosody(config, tono=seg["tono"],
                               emocion=seg["emocion"], tipo=seg["tipo"],
                               texto=seg["texto"])
        if merged and merged[-1]["tono"] == pros["tono"]:
            merged[-1]["texto"] = f"{merged[-1]['texto']} {seg['texto']}".strip()
        else:
            merged.append({
                "texto": seg["texto"],
                "tono": pros["tono"],
                "emocion": seg["emocion"],
                "tipo": seg["tipo"],
                "rate": pros["rate"],
                "pitch": pros["pitch"],
                "volume": pros["volume"],
                "pause_after_ms": pros["pause_after_ms"],
            })

    if max_segments and max_segments > 0 and len(merged) > max_segments:
        keep = merged[: max_segments - 1]
        tail = " ".join(s["texto"] for s in merged[max_segments - 1:]).strip()
        last = dict(merged[max_segments - 1])
        last["texto"] = tail
        keep.append(last)
        merged = keep

    return merged


def rate_to_speed(rate: Any, default: float = 1.0) -> float:
    """Convert an edge-tts rate value to a float speed multiplier.

    Accepts strings like ``"-18%"``, ``"+5%"`` or numbers (``0.8``).
    Approx: speed = 1.0 + pct/100  (clamped to [0.5, 2.0]).
    """
    if rate is None:
        return default
    if isinstance(rate, (int, float)):
        val = float(rate)
        # Already a multiplier (0.5..2.0) rather than a percentage.
        if 0.4 <= val <= 2.5:
            return max(0.5, min(2.0, val))
        return max(0.5, min(2.0, 1.0 + val / 100.0))
    match = re.match(r"([+-]?\d+(?:\.\d+)?)\s*%?", str(rate).strip())
    if not match:
        return default
    return max(0.5, min(2.0, 1.0 + float(match.group(1)) / 100.0))


def build_tts_engine(config: Any) -> Any:
    """Build the TTS engine (KokoroTTSEngine or TTSEngine) for a channel config.

    Uses resolve_channel_voice internally and returns a fully configured engine.
    """
    resolved = resolve_channel_voice(config)
    engine_type = resolved["engine"]

    if engine_type == "kokoro":
        from pipeline.kokoro_tts import KokoroTTSEngine
        # Resolve pause_between_blocks — try top-level key first,
        # then fall back to TTS_STRATEGY dict.
        pause_between = _get(config, "KOKORO_PAUSE_BETWEEN_BLOCKS", None)
        if pause_between is None:
            tts_strategy = _get(config, "TTS_STRATEGY", {})
            if isinstance(tts_strategy, dict):
                pause_between = tts_strategy.get("pause_between_blocks", 0.7)
            else:
                pause_between = 0.7
        voice_config = {
            "kokoro_voice": resolved["voice"],
            "block_speeds": resolved["block_speeds"],
            "pause_between_blocks": pause_between,
            # Batch unload: reload Kokoro every N blocks to keep RAM low.
            # 0 = disabled (legacy: model stays loaded for all blocks).
            "unload_every_n_blocks": _get(config, "KOKORO_UNLOAD_EVERY_N_BLOCKS", 0),
            # Expressive narration
            "expressive": resolved["expressive"],
            "prosody_profiles": resolved["prosody_profiles"],
            "tono_default": resolved["tono_default"],
            "max_segments": resolved["max_segments"],
            "rate_base": resolved["block_speeds"].get("base", 0.9),
        }
        logger.info(
            "🔊 TTS engine: Kokoro (voice=%s, expressive=%s)",
            resolved["voice"], resolved["expressive"],
        )
        return KokoroTTSEngine(voice_config)
    else:
        from pipeline.tts_engine import TTSEngine
        voice_config = {
            "voice": resolved["voice"],
            "rate": resolved["rate"],
            "pitch": resolved["pitch"],
            "volume": resolved["volume"],
            "tts_strategy": resolved["tts_strategy"],
            # Expressive narration
            "expressive": resolved["expressive"],
            "prosody_profiles": resolved["prosody_profiles"],
            "tono_default": resolved["tono_default"],
            "max_segments": resolved["max_segments"],
        }
        logger.info(
            "🔊 TTS engine: edge-tts (voice=%s, rate=%s, expressive=%s)",
            resolved["voice"], resolved["rate"], resolved["expressive"],
        )
        return TTSEngine(voice_config)


# ── Kokoro block_speeds from edge-tts rate strings ───────────

def _kokoro_block_speeds(tts_strategy: dict) -> dict:
    """Convert edge-tts rate percentages to Kokoro float speed multipliers.

    edge-tts "-10%" → slower speech (165 wpm)
    Kokoro  0.90  → slower speech (90% of neutral)

    Approximation: speed = 1.0 + (rate_pct / 100.0)
    -10% → 0.90,  -5% → 0.95,  +0% → 1.0
    """
    import re
    speeds: dict[str, float] = {}
    for key, val in tts_strategy.items():
        if key.startswith("rate_") and isinstance(val, str):
            block_type = key[5:]  # strip "rate_" prefix
            match = re.match(r'([+-]?\d+)%?', val)
            if match:
                pct = float(match.group(1))
                speeds[block_type] = max(0.5, min(2.0, 1.0 + pct / 100.0))
    # Ensure base speed exists
    if "base" not in speeds:
        speeds["base"] = 0.90  # slightly slower than neutral
    return speeds
