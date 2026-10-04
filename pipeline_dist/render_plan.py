"""Plan por escena para el render distribuido identity-preserving.

Congela en el control plane las decisiones NO deterministas/estatales que el
render local acumula, para que un nodo pueda reproducir EXACTAMENTE el mismo
segmento ejecutando el mismo código (`VideoEditor._render_scene_segment`) con:

  - semilla RNG por escena (`_seed_scene_rng`, ya en video_editor),
  - `color_grade` congelado (se calcula una vez, en la 1ª escena no-placeholder),
  - `start_offset` congelado por vídeo (acumulación normal),
  - `_last_ken_burns_profile` de entrada de la escena.

Escenas no reproducibles con seguridad (vídeo que dispara hybrid-fill con
fetch on-demand, asset inexistente, duración desconocida) se marcan
`reproducible=False` → el orquestador las renderiza en local (fallback).
"""
from __future__ import annotations

import random
import subprocess
from pathlib import Path


def _probe_duration(path: str) -> float:
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "csv=p=0", str(path)],
            capture_output=True, text=True, timeout=15,
        )
        return float((r.stdout or "0").strip() or 0.0)
    except Exception:  # noqa: BLE001
        return 0.0


def build_scene_plans(ve, block_ranges: list, media_assets: list) -> list[dict]:
    """Devuelve un plan por escena (lista alineada 1:1 con media_assets)."""
    ve._video_color_grade = None
    ve._last_ken_burns_profile = None
    offsets: dict[str, float] = {}
    grade = None
    grade_scene: int | None = None
    plans: list[dict] = []
    poisoned = False  # una vez no-reproducible, todas las siguientes también

    for i, (br, asset) in enumerate(zip(block_ranges, media_assets)):
        ve._seed_scene_rng(i)
        mt = str(asset.get("type", "placeholder"))
        path = str(asset.get("path") or "")
        block_dur = float(br.get("duration", 0) or 0)

        sets_grade = False
        if grade is None and mt not in ("placeholder", "duplicate"):
            # La PRIMERA escena no-placeholder define el grade (consumiendo RNG
            # en el mismo orden que el render local). El nodo debe computarlo
            # también para no desincronizar el RNG de Ken Burns.
            grade = {
                "contrast": random.uniform(1.02, 1.04),
                "brightness": random.uniform(0.99, 1.01),
                "saturation": random.uniform(0.97, 1.03),
            }
            grade_scene = i
            sets_grade = True

        last_kb = ve._last_ken_burns_profile
        start_offset = offsets.get(path, 0.0)
        reproducible = not poisoned

        if mt == "video":
            if not (path and Path(path).exists()):
                reproducible = False
            else:
                dur = _probe_duration(path)
                if dur <= 0:
                    reproducible = False
                else:
                    eff = start_offset % dur
                    remaining = (eff + block_dur) - dur
                    if remaining > block_dur * 0.5:
                        reproducible = False  # hybrid-fill (on-demand) → local
                    offsets[path] = start_offset + block_dur

        if mt == "image" and path and Path(path).exists():
            # Avanza el perfil Ken Burns igual que haría el render local.
            ve._select_ken_burns_profile()

        if not reproducible:
            poisoned = True

        plans.append({
            "index": i,
            "start_offset": start_offset,
            "color_grade": grade,
            "sets_color_grade": sets_grade,
            "last_kb": last_kb,
            "reproducible": reproducible,
        })

    return plans
