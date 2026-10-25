"""P05 — production/offline adapter from P02 previews to unified evidence.

The integrated selector is deliberately provider- and model-agnostic; this
adapter is the *only* place that knows how to obtain a still and run the P02
visual embedding backend.  It is fail-open: unavailable previews/models return
evidence with ``None`` values (missing), never zero.

Deviations from a full P02 ``select_and_download`` run are explicit:

* preview-only profile → one still per candidate;
* full profile → the still is still the preview; frame sampling is only used
  when the candidate already points to a local video file.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from .config import VisualConfig
from .contracts import Candidate, SceneSpec
from .unified_scoring import VisualEvidence
from .visual_backends import build_visual_backend
from .visual_description import build_visual_description
from .visual_previews import PreviewFetcher
from .visual_scoring import cosine_similarity, technical_quality_score

PathResolver = Callable[[Candidate, SceneSpec], str]


class P02PreviewEvidenceEvaluator:
    """Preview/image embedding evidence using the existing P02 backend."""

    def __init__(
        self,
        *,
        config: VisualConfig | None = None,
        preview_fetcher: PreviewFetcher | None = None,
        backend: Any = None,
        path_resolver: PathResolver | None = None,
        work_dir: str | Path | None = None,
    ) -> None:
        self.config = config or VisualConfig(mode="embedding_only", backend="clip")
        self.preview_fetcher = preview_fetcher or PreviewFetcher(
            self.config.budget,
            work_dir=work_dir,
            allow_local_paths=work_dir is not None,
        )
        self.backend = backend if backend is not None else build_visual_backend(self.config)
        self.path_resolver = path_resolver
        self._text_cache: dict[str, list[float] | None] = {}
        self._image_cache: dict[str, list[float] | None] = {}
        self.calls = 0
        self.preview_hits = 0
        self.preview_misses = 0
        self.errors = 0

    def _still_path(self, candidate: Candidate, spec: SceneSpec) -> str:
        # Generated assets are local files; validating them must not depend on
        # a provider preview URL (P05-J).
        if candidate.ai_generated:
            local = candidate.asset_url or candidate.download_url
            if local and Path(local).is_file():
                return str(local)
        if self.path_resolver is not None:
            try:
                resolved = self.path_resolver(candidate, spec)
            except Exception:
                resolved = ""
            if resolved and Path(resolved).is_file():
                return str(resolved)
        if not candidate.has_preview:
            return ""
        try:
            result = self.preview_fetcher.fetch(candidate)
        except Exception:
            return ""
        return result.path if result.available else ""

    def _text_vector(self, spec: SceneSpec) -> list[float] | None:
        text = build_visual_description(spec)
        key = text[:400]
        if key in self._text_cache:
            return self._text_cache[key]
        try:
            vectors = self.backend.encode_texts([text])
        except Exception:
            vectors = None
        vector = vectors[0] if vectors else None
        self._text_cache[key] = vector
        return vector

    def _image_vector(self, path: str) -> list[float] | None:
        if path in self._image_cache:
            return self._image_cache[path]
        try:
            vectors = self.backend.encode_images([path])
        except Exception:
            vectors = None
        vector = vectors[0] if vectors else None
        self._image_cache[path] = vector
        return vector

    def __call__(self, candidate: Candidate, spec: SceneSpec) -> VisualEvidence:
        self.calls += 1
        path = self._still_path(candidate, spec)
        if not path:
            self.preview_misses += 1
            return VisualEvidence(status="PREVIEW_UNAVAILABLE")
        self.preview_hits += 1
        text_vector = self._text_vector(spec)
        image_vector = self._image_vector(path)
        if text_vector is None or image_vector is None:
            self.errors += 1
            return VisualEvidence(status="VISUAL_BACKEND_UNAVAILABLE")
        relevance = cosine_similarity(text_vector, image_vector)
        quality = technical_quality_score(
            width=candidate.width, height=candidate.height,
        )
        return VisualEvidence(
            preview_relevance=relevance,
            technical_quality=quality,
            status="PREVIEW_AVAILABLE",
            detail={
                "path": path,
                "visual_source": candidate.visual_source,
                "backend": str(getattr(self.backend, "name", "")),
                "model": str(getattr(self.backend, "model_version", "")),
            },
        )

    def stats(self) -> dict[str, Any]:
        return {
            "calls": self.calls,
            "preview_hits": self.preview_hits,
            "preview_misses": self.preview_misses,
            "errors": self.errors,
            "backend": str(getattr(self.backend, "name", "")),
            "model": str(getattr(self.backend, "model_version", "")),
        }
