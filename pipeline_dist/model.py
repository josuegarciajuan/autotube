"""Contratos de datos del pipeline distribuido.

Todo lo que cruza la frontera coordinador <-> nodo está aquí: manifiesto de
escena, resultado de render y utilidades de hash/escritura atómica. Sin imports
del proyecto (el worker lo carga dentro del contenedor).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Optional

SCHEMA_VERSION = 1

# Dimensiones y encoding por defecto (el coordinador las sobrescribe por canal).
DEFAULT_WIDTH = 1920
DEFAULT_HEIGHT = 1080
DEFAULT_FPS = 30


def atomic_write_json(path: str, obj: Any) -> None:
    """Escribe JSON de forma atómica (tmp + rename)."""
    tmp = f"{path}.tmp"
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False, sort_keys=True)
    os.replace(tmp, path)


def sha256_file(path: str, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(chunk), b""):
            h.update(block)
    return h.hexdigest()


def sanitize_id(value: Any, max_len: int = 80) -> str:
    """Id válido para el motor distribuido (mismo alfabeto que `validId`)."""
    s = re.sub(r"[^A-Za-z0-9._-]+", "-", str(value or "")).strip(".-")
    return s[:max_len] or "dx"


@dataclass
class AssetRef:
    """Recurso visual asignado a una escena (nunca se decide en el nodo)."""

    kind: str = "test"  # image | video | test
    path: str = ""       # relativo al directorio de la escena (dentro de /work/in)
    sha256: str = ""
    trim_start: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ColorGrade:
    contrast: float = 1.0
    brightness: float = 1.0
    saturation: float = 1.0

    def is_neutral(self) -> bool:
        return (
            abs(self.contrast - 1.0) < 1e-6
            and abs(self.brightness - 1.0) < 1e-6
            and abs(self.saturation - 1.0) < 1e-6
        )


@dataclass
class Motion:
    """Zoom lento tipo Ken Burns (determinista, fijado por el coordinador)."""

    zoom_start: float = 1.0
    zoom_end: float = 1.0
    focus: str = "center"  # center | left | right | up | down

    @property
    def animated(self) -> bool:
        return abs(self.zoom_end - self.zoom_start) > 1e-4


@dataclass
class SceneManifest:
    """Entrada congelada de UNA escena. El nodo no añade decisiones."""

    scene_key: str
    index: int
    frames: int
    fps: float
    width: int
    height: int
    duration: float
    asset: AssetRef
    motion: Motion = field(default_factory=Motion)
    color: ColorGrade = field(default_factory=ColorGrade)
    encoding: dict = field(default_factory=lambda: {
        "codec": "libx264", "crf": 16, "preset": "veryfast", "pix_fmt": "yuv420p",
    })
    schema_version: int = SCHEMA_VERSION

    def to_dict(self) -> dict:
        d = asdict(self)
        return d

    @staticmethod
    def from_dict(d: dict) -> "SceneManifest":
        a = d.get("asset") or {}
        m = d.get("motion") or {}
        c = d.get("color") or {}
        return SceneManifest(
            scene_key=str(d.get("scene_key", "")),
            index=int(d.get("index", 0)),
            frames=int(d.get("frames", 1)),
            fps=float(d.get("fps", DEFAULT_FPS)),
            width=int(d.get("width", DEFAULT_WIDTH)),
            height=int(d.get("height", DEFAULT_HEIGHT)),
            duration=float(d.get("duration", 0.0)),
            asset=AssetRef(
                kind=str(a.get("kind", "test")),
                path=str(a.get("path", "")),
                sha256=str(a.get("sha256", "")),
                trim_start=float(a.get("trim_start", 0.0)),
            ),
            motion=Motion(
                zoom_start=float(m.get("zoom_start", 1.0)),
                zoom_end=float(m.get("zoom_end", 1.0)),
                focus=str(m.get("focus", "center")),
            ),
            color=ColorGrade(
                contrast=float(c.get("contrast", 1.0)),
                brightness=float(c.get("brightness", 1.0)),
                saturation=float(c.get("saturation", 1.0)),
            ),
            encoding=dict(d.get("encoding") or {}),
            schema_version=int(d.get("schema_version", SCHEMA_VERSION)),
        )


@dataclass
class SceneResult:
    """Salida verificable de UNA escena renderizada."""

    ok: bool
    scene_key: str = ""
    filename: str = "scene.mp4"
    sha256: str = ""
    frames: Optional[int] = None
    duration: Optional[float] = None
    fps: Optional[float] = None
    width: Optional[int] = None
    height: Optional[int] = None
    manifest_hash: str = ""
    render_ms: int = 0
    error: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict) -> "SceneResult":
        return SceneResult(
            ok=bool(d.get("ok", False)),
            scene_key=str(d.get("scene_key", "")),
            filename=str(d.get("filename", "scene.mp4")),
            sha256=str(d.get("sha256", "")),
            frames=d.get("frames"),
            duration=d.get("duration"),
            fps=d.get("fps"),
            width=d.get("width"),
            height=d.get("height"),
            manifest_hash=str(d.get("manifest_hash", "")),
            render_ms=int(d.get("render_ms", 0) or 0),
            error=str(d.get("error", "")),
        )


def manifest_hash(manifest: dict) -> str:
    """Hash estable del manifiesto (para detectar artefactos de un plan viejo)."""
    blob = json.dumps(manifest, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()
