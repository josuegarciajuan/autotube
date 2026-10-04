"""Plan visual congelado por escena (coordinador -> nodos).

Aquí se toman TODAS las decisiones (qué asset, duración exacta en frames, zoom,
color, encoding) antes de repartir. Los nodos solo ejecutan. Se aplica el
invariante del proyecto: **un asset único por escena**; si se detecta una
repetición, la planificación distribuida se rechaza y el llamador cae al render
local (nunca se reutiliza un asset para "acelerar").
"""
from __future__ import annotations

import os
import random
import shutil
from pathlib import Path
from typing import Any, Optional

from .model import (
    DEFAULT_FPS,
    DEFAULT_HEIGHT,
    DEFAULT_WIDTH,
    SCHEMA_VERSION,
    AssetRef,
    ColorGrade,
    Motion,
    SceneManifest,
    atomic_write_json,
)


class ScenePlanError(RuntimeError):
    """La escena no se puede planificar para render distribuido."""


class MissingAssetError(ScenePlanError):
    """Una escena no tiene asset real (placeholder / sin path)."""


class DuplicateAssetError(ScenePlanError):
    """Dos escenas comparten el mismo asset (viola el invariante del proyecto)."""


def _asset_identity(asset: dict) -> Optional[str]:
    kind = str(asset.get("type", "")).lower()
    if kind not in ("image", "video"):
        return None
    path = asset.get("path")
    if not path:
        return None
    content_hash = asset.get("content_hash") or asset.get("sha256") or ""
    return f"{kind}:{content_hash or path}"


def build_manifests(
    scene_ranges: list[dict],
    media_assets: list[dict],
    *,
    fps: float = DEFAULT_FPS,
    width: int = DEFAULT_WIDTH,
    height: int = DEFAULT_HEIGHT,
    seed: int = 0,
    zoom_min: float = 1.0,
    zoom_max: float = 1.08,
    encoding: Optional[dict] = None,
) -> list[dict]:
    """Devuelve la lista de manifiestos (dict) 1:1 con las escenas.

    Lanza ScenePlanError/MissingAssetError/DuplicateAssetError si el reparto no
    es seguro; el llamador debe entonces renderizar en local.
    """
    if not scene_ranges:
        raise ScenePlanError("sin scene_ranges")
    if len(scene_ranges) != len(media_assets):
        raise ScenePlanError(
            f"scene_ranges({len(scene_ranges)}) y media_assets({len(media_assets)}) no son 1:1"
        )

    rng_video = random.Random(int(seed) & 0x7FFFFFFF)
    color = ColorGrade(
        contrast=round(rng_video.uniform(1.02, 1.04), 4),
        brightness=round(rng_video.uniform(0.99, 1.01), 4),
        saturation=round(rng_video.uniform(0.97, 1.03), 4),
    )

    seen: dict[str, int] = {}
    manifests: list[dict] = []
    for i, (rng, asset) in enumerate(zip(scene_ranges, media_assets)):
        identity = _asset_identity(asset)
        if identity is None:
            raise MissingAssetError(
                f"escena {i} sin asset real (type={asset.get('type')!r}, path={asset.get('path')!r})"
            )
        if identity in seen:
            raise DuplicateAssetError(
                f"asset repetido en escenas {seen[identity]} y {i}: {identity}"
            )
        seen[identity] = i

        duration = float(rng.get("duration") or 0.0)
        if duration <= 0:
            raise ScenePlanError(f"escena {i} con duración inválida ({duration})")
        frames = max(1, int(round(duration * fps)))
        real_duration = frames / float(fps)

        local_rng = random.Random((int(seed) ^ (i * 2654435761)) & 0x7FFFFFFF)
        zoom_end = round(local_rng.uniform(float(zoom_min), float(zoom_max)), 4)
        motion = Motion(zoom_start=1.0, zoom_end=zoom_end, focus="center")

        kind = str(asset.get("type", "")).lower()
        manifest = SceneManifest(
            scene_key=f"{i:04d}",
            index=i,
            frames=frames,
            fps=float(fps),
            width=int(width),
            height=int(height),
            duration=real_duration,
            asset=AssetRef(
                kind=kind,
                path=Path(str(asset.get("path"))).name,
                sha256=str(asset.get("content_hash") or asset.get("sha256") or ""),
                trim_start=0.0,
            ),
            motion=motion,
            color=color,
            encoding=dict(encoding or {
                "codec": "libx264", "crf": 16, "preset": "veryfast", "pix_fmt": "yuv420p",
            }),
            schema_version=SCHEMA_VERSION,
        )
        manifests.append(manifest.to_dict())

    return manifests


def stage_scene(staging_dir: str, manifest: dict, source_path: str) -> dict:
    """Prepara el directorio de una escena: manifiesto + asset (hardlink/copia).

    Devuelve `{key, dir, manifest}` apto para `autotube-render-scene`.
    """
    idx = int(manifest["index"])
    scene_dir_name = f"scene_{idx:04d}"
    scene_dir = os.path.join(staging_dir, scene_dir_name)
    os.makedirs(scene_dir, exist_ok=True)

    asset = manifest.get("asset") or {}
    kind = str(asset.get("kind", ""))
    dest_name = Path(str(asset.get("path") or "")).name
    if kind in ("image", "video"):
        if not source_path or not os.path.exists(source_path):
            raise MissingAssetError(f"asset inexistente para escena {manifest['scene_key']}: {source_path}")
        dest = os.path.join(scene_dir, dest_name)
        if os.path.exists(dest):
            os.unlink(dest)
        try:
            os.link(source_path, dest)  # mismo FS: sin copia ni doble disco
        except OSError:
            shutil.copy2(source_path, dest)

    atomic_write_json(os.path.join(scene_dir, "manifest.json"), manifest)
    return {"key": manifest["scene_key"], "dir": scene_dir_name, "manifest": "manifest.json"}
