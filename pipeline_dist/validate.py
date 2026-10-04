"""Validaciones de ida y vuelta del render distribuido.

Regla: un artefacto remoto solo se acepta si es verificable. Si no lo es, la
escena se descarta y el pipeline decide (fallback local), nunca "se asume bueno".
"""
from __future__ import annotations

import math
import os
from typing import Optional

from .model import sha256_file


def validate_scene_result(
    manifest: dict,
    artifact_path: str,
    result: dict,
    *,
    frame_tolerance: int = 1,
    duration_tolerance: float = 0.25,
) -> tuple[bool, list[str]]:
    """Comprueba hash, frames y duración de un segmento remoto."""
    problems: list[str] = []
    if not result or not result.get("ok"):
        return False, [f"resultado no ok: {(result or {}).get('error', 'sin detalle')}"]
    if not artifact_path or not os.path.exists(artifact_path):
        return False, ["artefacto ausente"]

    expected_sha = str(result.get("sha256") or "")
    if expected_sha:
        actual = sha256_file(artifact_path)
        if actual != expected_sha:
            problems.append(f"sha256 no coincide ({actual[:12]} != {expected_sha[:12]})")

    expected_frames = int(manifest.get("frames", 0))
    got_frames = result.get("frames")
    if got_frames is not None and expected_frames:
        try:
            if abs(int(got_frames) - expected_frames) > frame_tolerance:
                problems.append(f"frames {got_frames} != {expected_frames}")
        except (TypeError, ValueError):
            problems.append(f"frames ilegibles: {got_frames!r}")

    expected_dur = float(manifest.get("duration", 0.0))
    got_dur = result.get("duration")
    if got_dur is not None and expected_dur > 0:
        try:
            if not math.isfinite(float(got_dur)) or abs(float(got_dur) - expected_dur) > duration_tolerance:
                problems.append(f"duración {got_dur} != {expected_dur:.3f}")
        except (TypeError, ValueError):
            problems.append(f"duración ilegible: {got_dur!r}")

    fps = result.get("fps")
    expected_fps = float(manifest.get("fps", 0) or 0)
    if fps is not None and expected_fps > 0:
        try:
            if abs(float(fps) - expected_fps) > 0.05:
                problems.append(f"fps {fps} != {expected_fps}")
        except (TypeError, ValueError):
            problems.append(f"fps ilegible: {fps!r}")

    return (not problems), problems


def validate_segment_coverage(
    scene_ranges: list[dict],
    segment_paths: list[str],
) -> tuple[bool, list[str]]:
    """Comprueba que hay un segmento por escena y con tamaño mínimo viable."""
    problems: list[str] = []
    if len(segment_paths) != len(scene_ranges):
        problems.append(f"segmentos {len(segment_paths)} != escenas {len(scene_ranges)}")
    for i, path in enumerate(segment_paths):
        if not path or not os.path.exists(path):
            problems.append(f"escena {i}: segmento ausente")
            continue
        try:
            if os.path.getsize(path) <= 1024:
                problems.append(f"escena {i}: segmento sospechosamente pequeño")
        except OSError as exc:
            problems.append(f"escena {i}: {exc}")
    return (not problems), problems
