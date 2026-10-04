"""Gate de validación del render: escenas negras, silencios y duración de audio.

Objetivo: impedir que un vídeo con tramos negros/vacíos o silencios largos se
suba a YouTube. Es **bloqueante** por defecto y **fail-open ante errores de
herramienta** (un fallo del gate en sí nunca bloquea: solo bloquea si detecta
contenido defectuoso con evidencia).

Kill-switch: ``RENDER_GATE_ENABLED=False`` (config por canal o global).

Uso:
    from pipeline.render_gate import evaluate_render_gate
    res = evaluate_render_gate(video_path, channel_config=cfg)
    if res["blocking"]:
        ...
"""
from __future__ import annotations

import logging
import re
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULTS = {
    "RENDER_GATE_ENABLED": True,
    # blackdetect
    "RENDER_GATE_BLACK_PIX_TH": 0.10,
    "RENDER_GATE_BLACK_MIN_SEC": 2.0,     # trozo negro mínimo considerado
    "RENDER_GATE_MAX_BLACK_SEC": 5.0,     # total de negro permitido antes de bloquear
    # silencedetect
    "RENDER_GATE_SILENCE_DB": -40,
    "RENDER_GATE_SILENCE_MIN_SEC": 3.0,   # silencio mínimo considerado
    "RENDER_GATE_MAX_SILENCE_SEC": 8.0,   # total de silencio permitido
    "RENDER_GATE_TIMEOUT_SEC": 300,
}

_RE_BLACK = re.compile(r"black_duration:([0-9.]+)")
_RE_SILENCE = re.compile(r"silence_duration:\s*([0-9.]+)")


def _cfg(channel_config, key):
    if channel_config is not None:
        v = getattr(channel_config, key, None)
        if v is not None:
            return v
    return DEFAULTS[key]


def _run_ffmpeg_detect(video_path: str, vf: str, af: str, timeout: int) -> tuple[str, bool]:
    """Devuelve (stderr, ok). ok=False si ffmpeg no pudo ejecutarse/timeout."""
    cmd = ["ffmpeg", "-hide_banner", "-nostats", "-i", video_path]
    if vf:
        cmd += ["-vf", vf]
    if af:
        cmd += ["-af", af]
    cmd += ["-f", "null", "-"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        # blackdetect/silencedetect escriben en stderr; returncode suele ser 0.
        return (r.stderr or ""), True
    except subprocess.TimeoutExpired:
        return f"timeout tras {timeout}s", False
    except Exception as exc:  # noqa: BLE001
        return f"error ejecutando ffmpeg: {exc}", False


def _sum_durations(stderr: str, regex: re.Pattern, min_sec: float) -> tuple[float, int]:
    total = 0.0
    count = 0
    for m in regex.finditer(stderr or ""):
        try:
            d = float(m.group(1))
        except ValueError:
            continue
        if d >= min_sec:
            total += d
            count += 1
    return total, count


def evaluate_render_gate(video_path: str, channel_config=None) -> dict:
    """Evalúa el vídeo contra el gate.

    Returns dict:
        enabled, passed, blocking (list[str]), warnings (list[str]),
        black_sec, black_segments, silence_sec, silence_segments, error
    """
    result = {
        "enabled": bool(_cfg(channel_config, "RENDER_GATE_ENABLED")),
        "passed": True, "blocking": [], "warnings": [],
        "black_sec": 0.0, "black_segments": 0,
        "silence_sec": 0.0, "silence_segments": 0,
        "error": None,
    }
    if not result["enabled"]:
        result["warnings"].append("render_gate desactivado (RENDER_GATE_ENABLED=False)")
        return result
    if not video_path or not Path(video_path).exists():
        result["passed"] = False
        result["blocking"].append("render_gate: fichero inexistente")
        return result

    timeout = int(_cfg(channel_config, "RENDER_GATE_TIMEOUT_SEC"))
    pix_th = float(_cfg(channel_config, "RENDER_GATE_BLACK_PIX_TH"))
    black_min = float(_cfg(channel_config, "RENDER_GATE_BLACK_MIN_SEC"))
    black_max = float(_cfg(channel_config, "RENDER_GATE_MAX_BLACK_SEC"))
    sil_min = float(_cfg(channel_config, "RENDER_GATE_SILENCE_MIN_SEC"))
    sil_max = float(_cfg(channel_config, "RENDER_GATE_MAX_SILENCE_SEC"))
    sil_db = int(_cfg(channel_config, "RENDER_GATE_SILENCE_DB"))

    # ── blackdetect (vídeo) ──
    out, ok = _run_ffmpeg_detect(
        video_path, f"blackdetect=d={black_min}:pix_th={pix_th}", None, timeout,
    )
    if not ok:
        result["error"] = out
        result["warnings"].append(f"render_gate: blackdetect no ejecutó ({out}); no se bloquea por esto")
    else:
        total, count = _sum_durations(out, _RE_BLACK, black_min)
        result["black_sec"], result["black_segments"] = total, count
        if total > black_max:
            result["blocking"].append(
                f"render_gate: {total:.1f}s de negro en {count} tramo(s) (> {black_max}s)"
            )

    # ── silencedetect (audio) ──
    out, ok = _run_ffmpeg_detect(
        video_path, None, f"silencedetect=noise={sil_db}dB:d={sil_min}", timeout,
    )
    if not ok:
        result["warnings"].append(f"render_gate: silencedetect no ejecutó ({out}); no se bloquea por esto")
    else:
        total, count = _sum_durations(out, _RE_SILENCE, sil_min)
        result["silence_sec"], result["silence_segments"] = total, count
        if total > sil_max:
            result["blocking"].append(
                f"render_gate: {total:.1f}s de silencio en {count} tramo(s) (> {sil_max}s)"
            )

    result["passed"] = not result["blocking"]
    if result["blocking"]:
        logger.error("Render gate: %s", "; ".join(result["blocking"]))
    else:
        logger.info(
            "Render gate OK (negro=%.1fs/%d, silencio=%.1fs/%d)",
            result["black_sec"], result["black_segments"],
            result["silence_sec"], result["silence_segments"],
        )
    return result
