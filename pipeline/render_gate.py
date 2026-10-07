"""Gate de validación del render: escenas negras, silencios y duración de audio.

Objetivo: impedir que un vídeo con tramos negros/vacíos o silencios largos se
suba a YouTube. Es **bloqueante** por defecto y **fail-open ante errores de
herramienta** (un fallo del gate en sí nunca bloquea: solo bloquea si detecta
contenido defectuoso con evidencia).

Intro/outro: el pipeline monta tarjetas de marca ("intro" al principio;
"CTA"+"outro" al final) con fondo oscuro (YAVG≈20), que ``blackdetect`` podía
clasificar como negro. Dos defensas:
  - ``RENDER_GATE_BLACK_PIX_TH`` bajo (0.03): solo cuenta como negro real
    (YAVG≲8), no el branding oscuro intencional.
  - Ventana de marca: un tramo negro que caiga dentro de los primeros/últimos
    ``RENDER_GATE_EDGE_IGNORE_SEC`` segundos se exime si su duración ≤ esa
    ventana y ≤ ``RENDER_GATE_EDGE_MAX_FRACTION`` de la duración total.

Así un vídeo completamente negro (o un tramo negro largo real en el cuerpo)
sigue bloqueando, pero las tarjetas de marca no.

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
    # blackdetect — 0.03 solo marca negro real (YAVG≲8); el branding oscuro de
    # las tarjetas de marca (YAVG≈20) deja de confundirse con negro.
    "RENDER_GATE_BLACK_PIX_TH": 0.03,
    "RENDER_GATE_BLACK_MIN_SEC": 2.0,     # trozo negro mínimo considerado
    "RENDER_GATE_MAX_BLACK_SEC": 5.0,     # total de negro permitido antes de bloquear
    # Exención de la ventana de marca (intro al principio; CTA+outro al final).
    "RENDER_GATE_EDGE_IGNORE_SEC": 30.0,  # ventana de marca a eximir en cada extremo
    "RENDER_GATE_EDGE_MAX_FRACTION": 0.15,  # y como fracción de la duración total
    # silencedetect
    "RENDER_GATE_SILENCE_DB": -40,
    "RENDER_GATE_SILENCE_MIN_SEC": 3.0,   # silencio mínimo considerado
    "RENDER_GATE_MAX_SILENCE_SEC": 8.0,   # total de silencio permitido
    "RENDER_GATE_TIMEOUT_SEC": 300,
}

_RE_BLACK_RUN = re.compile(
    r"black_start:\s*([0-9.]+)\s+black_end:\s*([0-9.]+)\s+black_duration:\s*([0-9.]+)"
)
_RE_SILENCE = re.compile(r"silence_duration:\s*([0-9.]+)")

# Tolerancia (s) para considerar que un tramo "empieza al principio" o
# "termina al final" del vídeo.
_EDGE_EPS = 0.6


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


def _probe_duration(video_path: str, timeout: int) -> float | None:
    """Duración del vídeo (s) vía ffprobe. None si no se puede determinar."""
    cmd = [
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "csv=p=0", video_path,
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return float((r.stdout or "").strip())
    except Exception:  # noqa: BLE001
        return None


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


def _parse_black_runs(stderr: str, min_sec: float) -> list[tuple[float, float, float]]:
    """Tramos negros (start, end, duration) con duración ≥ min_sec."""
    runs: list[tuple[float, float, float]] = []
    for m in _RE_BLACK_RUN.finditer(stderr or ""):
        try:
            start, end, dur = float(m.group(1)), float(m.group(2)), float(m.group(3))
        except ValueError:
            continue
        if dur >= min_sec:
            runs.append((start, end, dur))
    return runs


def _split_edge_black(
    runs: list[tuple[float, float, float]],
    duration: float | None,
    edge_sec: float,
    max_fraction: float,
) -> tuple[float, int, float, int]:
    """Separa tramos negros de cuerpo (bloqueantes) y de marca (intro/outro, exentos).

    Un tramo se exime si cae dentro de la **ventana de marca** —los primeros o
    últimos ``edge_sec`` segundos— Y es corto (``≤ edge_sec``) Y no domina el
    vídeo (``≤ max_fraction * duración``). La ventana (en vez de exigir que el
    negro toque el extremo exacto) es necesaria porque el CTA+outro pueden dejar
    una cola visible (end card) tras el negro. Si no se conoce la duración, no
    se exime nada (fail-safe: se mantiene el comportamiento bloqueante original).
    """
    counted_total = counted_count = 0.0
    ignored_total = ignored_count = 0.0
    if duration is None or duration <= 0:
        for _s, _e, dur in runs:
            counted_total += dur
            counted_count += 1
        return counted_total, counted_count, 0.0, 0.0

    for start, end, dur in runs:
        is_head = start <= edge_sec
        is_tail = (duration - end) <= edge_sec
        short_enough = dur <= edge_sec
        not_dominant = dur <= max_fraction * duration
        if (is_head or is_tail) and short_enough and not_dominant:
            ignored_total += dur
            ignored_count += 1
        else:
            counted_total += dur
            counted_count += 1
    return counted_total, counted_count, ignored_total, ignored_count


def evaluate_render_gate(video_path: str, channel_config=None) -> dict:
    """Evalúa el vídeo contra el gate.

    Returns dict:
        enabled, passed, blocking (list[str]), warnings (list[str]),
        black_sec, black_segments, black_ignored_sec, black_ignored_segments,
        silence_sec, silence_segments, error
    """
    result = {
        "enabled": bool(_cfg(channel_config, "RENDER_GATE_ENABLED")),
        "passed": True, "blocking": [], "warnings": [],
        "black_sec": 0.0, "black_segments": 0,
        "black_ignored_sec": 0.0, "black_ignored_segments": 0,
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
    edge_sec = float(_cfg(channel_config, "RENDER_GATE_EDGE_IGNORE_SEC"))
    edge_fraction = float(_cfg(channel_config, "RENDER_GATE_EDGE_MAX_FRACTION"))
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
        runs = _parse_black_runs(out, black_min)
        duration = _probe_duration(video_path, timeout)
        total, count, ign_total, ign_count = _split_edge_black(
            runs, duration, edge_sec, edge_fraction,
        )
        result["black_sec"], result["black_segments"] = total, count
        result["black_ignored_sec"], result["black_ignored_segments"] = ign_total, ign_count
        if ign_count:
            logger.info(
                "Render gate: %d tramo(s) de intro/outro ignorados (%.1fs)",
                ign_count, ign_total,
            )
        if total > black_max:
            extra = (
                f"; {ign_count} tramo(s) de intro/outro ignorados"
                if ign_count else ""
            )
            result["blocking"].append(
                f"render_gate: {total:.1f}s de negro en {count} tramo(s) "
                f"(> {black_max}s{extra})"
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
            "Render gate OK (negro=%.1fs/%d, intro/outro ignorado=%.1fs/%d, silencio=%.1fs/%d)",
            result["black_sec"], result["black_segments"],
            result["black_ignored_sec"], result["black_ignored_segments"],
            result["silence_sec"], result["silence_segments"],
        )
    return result
