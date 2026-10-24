"""P02-F — persistent cache for visual embeddings.

The key is deliberately strict: provider/asset identity + representation
identity (URL or content hash) + backend name + model/version.  Changing the
model can never silently reuse an embedding produced by a different model.
The cache stores JSON vectors only (no images, no base64 blobs).
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: str | Path, *, limit: int | None = None) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        remaining = limit
        while True:
            chunk = handle.read(1024 * 1024 if remaining is None else min(1024 * 1024, remaining))
            if not chunk:
                break
            digest.update(chunk)
            if remaining is not None:
                remaining -= len(chunk)
                if remaining <= 0:
                    break
    return digest.hexdigest()


def visual_cache_key(
    *,
    provider: str,
    asset_id: str,
    representation: str,
    backend: str,
    model_version: str,
) -> str:
    """Stable cache key. ``representation`` should be a URL or content hash."""
    raw = "|".join(
        [
            str(provider or "").strip().lower(),
            str(asset_id or "").strip(),
            str(representation or "").strip(),
            str(backend or "").strip().lower(),
            str(model_version or "").strip(),
        ]
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


@dataclass
class VisualEmbeddingCache:
    path: str | Path | None = None
    enabled: bool = True
    _data: dict[str, list[float]] = field(default_factory=dict, repr=False)
    hits: int = 0
    misses: int = 0
    writes: int = 0

    def __post_init__(self) -> None:
        self._load()

    def _load(self) -> None:
        if not self.enabled or not self.path:
            return
        try:
            p = Path(self.path)
            if not p.exists():
                return
            data = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                self._data = {
                    str(k): [float(x) for x in v]
                    for k, v in data.items()
                    if isinstance(v, list) and v and all(isinstance(x, (int, float)) for x in v)
                }
        except Exception:
            self._data = {}

    def get(self, key: str) -> list[float] | None:
        if not self.enabled:
            return None
        value = self._data.get(str(key))
        if value is None:
            self.misses += 1
            return None
        self.hits += 1
        return list(value)

    def set(self, key: str, vector: list[float]) -> None:
        if not self.enabled:
            return
        self._data[str(key)] = [float(x) for x in vector]
        self.writes += 1
        self._persist()

    def _persist(self) -> None:
        if not self.path:
            return
        try:
            p = Path(self.path)
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp = p.with_suffix(p.suffix + ".tmp")
            tmp.write_text(json.dumps(self._data, separators=(",", ":")), encoding="utf-8")
            os.replace(tmp, p)
        except Exception:
            pass

    def stats(self) -> dict[str, Any]:
        return {
            "enabled": bool(self.enabled),
            "entries": len(self._data),
            "hits": self.hits,
            "misses": self.misses,
            "writes": self.writes,
        }
