"""Caché de imágenes IA pre-generadas por la flota (SuperServer).

`_try_ai_image_chain` pide a ``local_sd`` una imagen por escena. Cuando la
pre-generación distribuida está activa, este proxy sirve la imagen ya generada
en un nodo (indexada por hash del prompt) y delega en el proveedor local
únicamente si falta. Sin acoplar el orquestador pesado: solo stdlib.
"""
from __future__ import annotations

import hashlib
import os
import shutil
from pathlib import Path


def prompt_cache_key(prompt: str) -> str:
    """Clave estable de caché: md5(prompt)[:10] (mismo que el nombre canónico)."""
    return hashlib.md5(str(prompt).encode("utf-8")).hexdigest()[:10]


class CachedLocalSDProvider:
    """Proxy de ``LocalSDProvider`` que sirve de una caché por hash de prompt."""

    def __init__(self, inner, cache_by_hash: dict):
        self._inner = inner
        self._cache = dict(cache_by_hash or {})

    def __getattr__(self, name):
        # Delega atributos no envueltos en el proveedor real (name, metadata...).
        return getattr(self._inner, name)

    def generate(self, prompt, output_path, seed=None, negative_prompt=None,
                 width=None, height=None):
        src = self._cache.get(prompt_cache_key(prompt))
        if src and os.path.exists(src):
            output_path = Path(output_path)
            output_path.parent.mkdir(parents=True, exist_ok=True)
            if os.path.abspath(str(output_path)) != os.path.abspath(str(src)):
                shutil.copy2(src, output_path)
            return output_path
        if self._inner is None:
            return None
        return self._inner.generate(
            prompt, output_path, seed=seed, negative_prompt=negative_prompt,
            width=width, height=height,
        )
