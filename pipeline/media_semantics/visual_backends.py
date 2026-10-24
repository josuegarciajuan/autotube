"""P02-C — pluggable VISUAL_TEXT embedding backends (CPU-first, lazy).

This is intentionally separate from P01's text ``HASH_VECTOR`` backend.  A
hashed text vector is not a visual semantic embedding, and neither is a cheap
pixel heuristic.  The default backend is ``none`` so no model is loaded or
downloaded unless a channel explicitly opts in.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Protocol, Sequence, runtime_checkable

from .config import VisualConfig

logger = logging.getLogger(__name__)


@runtime_checkable
class VisualEmbeddingBackend(Protocol):
    name: str
    model_version: str

    def available(self) -> bool:
        ...

    def encode_texts(self, texts: Sequence[str]) -> list[list[float]] | None:
        ...

    def encode_images(self, paths: Sequence[str | Path]) -> list[list[float]] | None:
        ...


def _normalise(vector: Any) -> list[float]:
    values = [float(x) for x in list(vector)]
    norm = sum(v * v for v in values) ** 0.5
    if norm > 0:
        values = [v / norm for v in values]
    return values


def _feature_tensor(features: Any) -> Any:
    """Return the projected embedding tensor from a Transformers output.

    Transformers <5 returned a plain tensor from ``get_text_features`` /
    ``get_image_features``.  Transformers 5 wraps it in a
    ``BaseModelOutputWithPooling`` whose ``pooler_output`` already holds the
    projected (e.g. 512-d) embedding.  Accept both shapes so a library upgrade
    does not silently disable the visual backend.
    """
    if hasattr(features, "pooler_output"):
        pooled = features.pooler_output
        if pooled is not None:
            return pooled
    return features


class NullVisualEmbeddingBackend:
    name = "none"
    model_version = "none"

    def available(self) -> bool:
        return False

    def encode_texts(self, texts: Sequence[str]) -> list[list[float]] | None:
        return None

    def encode_images(self, paths: Sequence[str | Path]) -> list[list[float]] | None:
        return None


class _TransformerVisualBackend:
    """Lazy Transformers implementation shared by CLIP/SigLIP.

    No model is downloaded during tests: ``local_files_only`` is True unless
    ``visual_allow_model_download`` is explicitly enabled.  Any failure yields
    ``None`` and the pipeline degrades instead of crashing.
    """

    name = "clip"
    _model_cls_name = "CLIPModel"
    _processor_cls_name = "CLIPProcessor"

    def __init__(self, config: VisualConfig) -> None:
        self.config = config
        self.model_version = config.model_version or config.model or self.name
        self._model = None
        self._processor = None
        self._device = "cpu"
        self._load_error = ""

    def available(self) -> bool:
        return True  # capability exists; load may still fail safely

    def _load(self) -> bool:
        if self._model is not None and self._processor is not None:
            return True
        try:
            import torch
            import transformers

            model_cls = getattr(transformers, self._model_cls_name)
            processor_cls = getattr(transformers, self._processor_cls_name)
            local_only = not bool(self.config.allow_model_download)
            kwargs = {
                "local_files_only": local_only,
            }
            device = str(self.config.device or "cpu").lower()
            if device.startswith("cuda") and not torch.cuda.is_available():
                device = "cpu"
            self._device = device
            self._processor = processor_cls.from_pretrained(self.config.model, **kwargs)
            self._model = model_cls.from_pretrained(self.config.model, **kwargs)
            self._model.to(device)
            self._model.eval()
            return True
        except Exception as exc:  # noqa: BLE001
            self._load_error = f"{type(exc).__name__}: {str(exc)[:200]}"
            logger.info("P02 visual backend %s unavailable: %s", self.name, self._load_error)
            return False

    def _torch(self):
        import torch

        return torch

    def encode_texts(self, texts: Sequence[str]) -> list[list[float]] | None:
        if not self._load():
            return None
        try:
            torch = self._torch()
            inputs = self._processor(
                text=[str(t or "") for t in texts], return_tensors="pt", padding=True,
                truncation=True, max_length=77,
            )
            inputs = {k: v.to(self._device) for k, v in inputs.items()}
            with torch.no_grad():
                features = _feature_tensor(self._model.get_text_features(**inputs))
            features = features / features.norm(p=2, dim=-1, keepdim=True).clamp(min=1e-12)
            return [_normalise(v) for v in features.cpu().tolist()]
        except Exception as exc:  # noqa: BLE001
            logger.info("P02 visual text encode failed (%s): %s", self.name, exc)
            return None

    def encode_images(self, paths: Sequence[str | Path]) -> list[list[float]] | None:
        if not self._load():
            return None
        try:
            from PIL import Image
            import torch

            images = []
            for path in paths:
                with Image.open(path) as img:
                    images.append(img.convert("RGB"))
            if not images:
                return []
            inputs = self._processor(images=images, return_tensors="pt")
            inputs = {k: v.to(self._device) for k, v in inputs.items()}
            with torch.no_grad():
                features = _feature_tensor(self._model.get_image_features(**inputs))
            features = features / features.norm(p=2, dim=-1, keepdim=True).clamp(min=1e-12)
            return [_normalise(v) for v in features.cpu().tolist()]
        except Exception as exc:  # noqa: BLE001
            logger.info("P02 visual image encode failed (%s): %s", self.name, exc)
            return None

    def describe(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "model": self.config.model,
            "model_version": self.model_version,
            "device": self._device,
            "loaded": self._model is not None,
            "load_error": self._load_error,
        }


class CLIPVisualEmbeddingBackend(_TransformerVisualBackend):
    name = "clip"
    _model_cls_name = "CLIPModel"
    _processor_cls_name = "CLIPProcessor"


class SigLIPVisualEmbeddingBackend(_TransformerVisualBackend):
    name = "siglip"
    _model_cls_name = "AutoModel"
    _processor_cls_name = "AutoProcessor"


def build_visual_backend(config: VisualConfig | None = None) -> VisualEmbeddingBackend:
    """Resolve the configured backend.  Never downloads implicitly."""
    config = config or VisualConfig()
    mode = str(config.backend or "none").strip().lower()
    if mode == "clip":
        return CLIPVisualEmbeddingBackend(config)
    if mode == "siglip":
        return SigLIPVisualEmbeddingBackend(config)
    # ``auto`` deliberately does not silently download a large model.  An
    # operator must select clip/siglip explicitly.
    return NullVisualEmbeddingBackend()
