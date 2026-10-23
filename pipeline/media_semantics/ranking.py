"""P01-F — textual/semantic candidate ranking with explicit partial scores.

The ranker combines several normalised signals (semantic similarity, lexical
overlap, action/subject/environment/era matches, negative constraints, metadata
quality and provider prior) into a single 0..1 score.  Weights are
configurable.

Semantic backend policy
-----------------------
* ``auto`` (default) → deterministic local hashed n-gram embeddings.  No
  network, no model download, no new mandatory dependency.
* ``sentence_transformers`` → optional local model if the package and model are
  already available; any failure degrades to the hashing backend.
* ``none`` → disables the semantic component; all other signals still work.

The backend interface is intentionally small so P02 can plug in a better
text model without touching the selector or provenance contracts.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Protocol

from .config import RankingWeights
from .contracts import Candidate, MetadataStatus, SceneSpec
from .normalization import provider_prior

_WORD_RE = re.compile(r"[a-z0-9áéíóúüñ]+", re.IGNORECASE)

_STOPWORDS = frozenset({
    "a", "an", "the", "of", "in", "on", "at", "to", "for", "with", "and",
    "or", "is", "are", "be", "as", "by", "from", "into", "that", "this",
    "de", "del", "la", "el", "los", "las", "un", "una", "y", "o", "en",
    "con", "por", "para", "al", "se", "su", "que",
})

_GENERIC_METADATA_WORDS = frozenset({
    "video", "footage", "clip", "stock", "free", "hd", "4k", "cinematic",
    "shot", "scene", "broll", "b-roll",
})


def _tokens(text: Any) -> list[str]:
    return [
        w.lower() for w in _WORD_RE.findall(str(text or ""))
        if len(w) > 1 and w.lower() not in _STOPWORDS
    ]


def _token_set(text: Any) -> set[str]:
    return set(_tokens(text))


class EmbeddingBackend(Protocol):
    name: str

    def encode(self, texts: list[str]) -> list[list[float]] | None:
        """Return one embedding per text, or ``None`` when unavailable."""


class HashingEmbeddingBackend:
    """HASH_VECTOR backend: deterministic hashed n-gram similarity.

    This is a real vector-space representation (word unigrams + bigrams hashed
    into a fixed space and L2-normalised), but it is **not** a neural semantic
    embedding.  It is local and stable across processes (uses BLAKE2b, never
    Python's randomised ``hash``).  The optional ``SentenceTransformerBackend``
    is the SEMANTIC_EMBEDDING extension point.
    """

    name = "hashing"

    def __init__(self, dimensions: int = 512, cache_path: str = "") -> None:
        self.dimensions = max(64, int(dimensions))
        self._cache: dict[str, list[float]] = {}
        self._cache_path = Path(cache_path) if cache_path else None
        if self._cache_path is not None:
            self._load_cache()

    def _load_cache(self) -> None:
        try:
            if self._cache_path and self._cache_path.exists():
                data = json.loads(self._cache_path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    self._cache = {
                        str(k): [float(x) for x in v]
                        for k, v in data.items()
                        if isinstance(v, list)
                    }
        except Exception:
            self._cache = {}

    def _persist_cache(self) -> None:
        if self._cache_path is None:
            return
        try:
            self._cache_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._cache_path.with_suffix(self._cache_path.suffix + ".tmp")
            tmp.write_text(json.dumps(self._cache), encoding="utf-8")
            os.replace(tmp, self._cache_path)
        except Exception:
            pass

    @staticmethod
    def _key(text: str) -> str:
        return hashlib.sha256(text.encode("utf-8")).hexdigest()[:32]

    def _bucket(self, feature: str) -> tuple[int, float]:
        digest = hashlib.blake2b(feature.encode("utf-8"), digest_size=8).digest()
        value = int.from_bytes(digest, "big")
        sign = 1.0 if (value >> 63) & 1 else -1.0
        return value % self.dimensions, sign

    def _embed_one(self, text: str) -> list[float]:
        words = _tokens(text)
        vector = [0.0] * self.dimensions
        features: list[tuple[str, float]] = []
        for word in words:
            features.append((f"w:{word}", 1.0))
        for left, right in zip(words, words[1:]):
            features.append((f"b:{left}_{right}", 0.7))
        for feature, weight in features:
            index, sign = self._bucket(feature)
            vector[index] += sign * weight
        norm = math.sqrt(sum(v * v for v in vector))
        if norm > 0:
            vector = [v / norm for v in vector]
        return vector

    def encode(self, texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        dirty = False
        for text in texts:
            key = self._key(text or "")
            vector = self._cache.get(key)
            if vector is None:
                vector = self._embed_one(text or "")
                self._cache[key] = vector
                dirty = True
            out.append(vector)
        if dirty:
            self._persist_cache()
        return out


class NullEmbeddingBackend:
    name = "none"

    def encode(self, texts: list[str]) -> list[list[float]] | None:
        return None


class SentenceTransformerBackend:
    """Optional local sentence-transformers backend (never downloaded implicitly)."""

    name = "sentence_transformers"

    def __init__(self, model_name: str) -> None:
        from sentence_transformers import SentenceTransformer  # type: ignore

        self._model = SentenceTransformer(model_name)
        self._cache: dict[str, list[float]] = {}

    def encode(self, texts: list[str]) -> list[list[float]] | None:
        try:
            vectors = self._model.encode(
                texts, batch_size=32, convert_to_numpy=False,
                normalize_embeddings=True, show_progress_bar=False,
            )
            return [list(map(float, v)) for v in vectors]
        except Exception:
            return None


def build_embedding_backend(
    mode: str = "auto", model: str = "", cache_path: str = ""
) -> EmbeddingBackend:
    """Resolve the configured backend, always returning a usable object.

    Heavy backends are only attempted when explicitly requested; any failure
    falls back to the local hashing backend so the pipeline still starts.
    """
    mode = str(mode or "auto").strip().lower()
    if mode == "none":
        return NullEmbeddingBackend()
    if mode in ("sentence_transformers", "sentence-transformers", "st"):
        try:
            return SentenceTransformerBackend(model or "all-MiniLM-L6-v2")
        except Exception:
            pass
    return HashingEmbeddingBackend(cache_path=cache_path)


def _cosine(left: list[float], right: list[float]) -> float:
    if not left or not right:
        return 0.0
    n = min(len(left), len(right))
    dot = sum(left[i] * right[i] for i in range(n))
    lnorm = math.sqrt(sum(left[i] * left[i] for i in range(n)))
    rnorm = math.sqrt(sum(right[i] * right[i] for i in range(n)))
    if lnorm <= 0 or rnorm <= 0:
        return 0.0
    return max(0.0, min(1.0, dot / (lnorm * rnorm)))


@dataclass
class ScoreBreakdown:
    lexical: float = 0.0
    semantic: float = 0.0
    action_match: float = 0.0
    subject_match: float = 0.0
    environment_match: float = 0.0
    era_match: float = 0.5
    negative_constraint_penalty: float = 0.0
    metadata_quality: float = 0.0
    provider_prior: float = 0.5
    final: float = 0.0
    metadata_status: str = MetadataStatus.NO_METADATA.value
    matched_terms: list[str] = field(default_factory=list)
    negative_hits: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "lexical": round(self.lexical, 6),
            "semantic": round(self.semantic, 6),
            "action_match": round(self.action_match, 6),
            "subject_match": round(self.subject_match, 6),
            "environment_match": round(self.environment_match, 6),
            "era_match": round(self.era_match, 6),
            "negative_constraint_penalty": round(self.negative_constraint_penalty, 6),
            "metadata_quality": round(self.metadata_quality, 6),
            "provider_prior": round(self.provider_prior, 6),
            "final": round(self.final, 6),
            "metadata_status": self.metadata_status,
            "matched_terms": list(self.matched_terms[:12]),
            "negative_hits": list(self.negative_hits[:8]),
        }


def _phrase_match(text_tokens: set[str], phrase: str) -> float:
    words = _token_set(phrase)
    if not words:
        return 0.5  # neutral: nothing to match against
    return len(text_tokens & words) / len(words)


def _era_match(text_tokens: set[str], text: str, era: str) -> float:
    era = str(era or "").strip()
    if not era or era.lower() in ("atemporal", "presente", "present", "timeless"):
        return 0.5
    words = _token_set(era)
    if not words:
        return 0.5
    if words.issubset(text_tokens):
        return 1.0
    # "17th century" → "17th" alone is already a strong period signal.
    return 1.0 if any(w.isdigit() and w in text_tokens for w in words) else 0.0


class SemanticTextRanker:
    """Deterministic partial-signal ranker with configurable weights."""

    def __init__(
        self,
        weights: RankingWeights | None = None,
        backend: EmbeddingBackend | None = None,
    ) -> None:
        self.weights = weights or RankingWeights()
        self.backend = backend or build_embedding_backend("auto")

    def scene_text(self, spec: SceneSpec) -> str:
        parts = [
            spec.subject, spec.action, spec.environment, spec.visual_concept,
            spec.narration_fragment, " ".join(spec.must_include),
        ]
        return " ".join(p for p in parts if p)

    def score_many(
        self, spec: SceneSpec, candidates: Iterable[Candidate]
    ) -> list[ScoreBreakdown]:
        candidates = list(candidates)
        if not candidates:
            return []
        scene = self.scene_text(spec)
        candidate_texts = [c.searchable_text for c in candidates]
        vectors = None
        try:
            vectors = self.backend.encode([scene] + candidate_texts)
        except Exception:
            vectors = None
        scene_vec = vectors[0] if vectors else None

        scene_tokens = _token_set(scene)
        action_tokens = _token_set(spec.action)
        subject_tokens = _token_set(spec.subject)
        env_tokens = _token_set(spec.environment) | _token_set(spec.era)
        avoid_terms = [str(t).strip().lower() for t in spec.must_avoid if str(t).strip()]

        out: list[ScoreBreakdown] = []
        for index, candidate in enumerate(candidates):
            breakdown = ScoreBreakdown()
            breakdown.provider_prior = provider_prior(candidate.provider)
            breakdown.metadata_quality = float(candidate.metadata_completeness or 0.0)

            text = candidate.searchable_text or ""
            text_tokens = _token_set(text)
            if not text or not text_tokens:
                breakdown.metadata_status = MetadataStatus.NO_METADATA.value
                candidate.metadata_status = MetadataStatus.NO_METADATA
                # Explicitly NOT equivalent to a known zero: the status carries
                # the "unknown" signal and the score stays below the ceiling.
                breakdown.final = min(
                    self.weights.no_metadata_ceiling,
                    0.5 * breakdown.provider_prior,
                )
                out.append(breakdown)
                continue

            overlap = scene_tokens & text_tokens
            meaningful = overlap - _GENERIC_METADATA_WORDS
            breakdown.lexical = len(overlap) / max(1, len(scene_tokens))
            breakdown.matched_terms = sorted(meaningful)

            if vectors and scene_vec is not None:
                breakdown.semantic = _cosine(scene_vec, vectors[index + 1])

            breakdown.action_match = _phrase_match(text_tokens, spec.action)
            breakdown.subject_match = _phrase_match(text_tokens, spec.subject)
            breakdown.environment_match = _phrase_match(
                text_tokens, f"{spec.environment} {spec.era}"
            )
            breakdown.era_match = _era_match(text_tokens, text, spec.era)

            negative_hits: list[str] = []
            for term in avoid_terms:
                if not term:
                    continue
                if (" " in term and term in text) or (
                    " " not in term and term in text_tokens
                ):
                    negative_hits.append(term)
            breakdown.negative_hits = negative_hits
            if negative_hits:
                penalty = min(1.0, 0.5 * len(negative_hits))
                breakdown.negative_constraint_penalty = (
                    penalty * self.weights.negative_constraint
                )

            w = self.weights
            weighted = (
                w.semantic * breakdown.semantic
                + w.lexical * breakdown.lexical
                + w.action * breakdown.action_match
                + w.subject * breakdown.subject_match
                + w.environment * breakdown.environment_match
                + w.era * breakdown.era_match
                + w.metadata_quality * breakdown.metadata_quality
                + w.provider_prior * breakdown.provider_prior
            )
            final = weighted / w.positive_total() - breakdown.negative_constraint_penalty
            breakdown.final = max(0.0, min(1.0, final))

            has_overlap = bool(meaningful) or breakdown.semantic >= 0.18
            if not has_overlap:
                breakdown.metadata_status = MetadataStatus.NO_MATCH.value
                candidate.metadata_status = MetadataStatus.NO_MATCH
            else:
                breakdown.metadata_status = MetadataStatus.MATCH.value
                candidate.metadata_status = MetadataStatus.MATCH
            out.append(breakdown)

        return out

    def score(self, spec: SceneSpec, candidate: Candidate) -> ScoreBreakdown:
        results = self.score_many(spec, [candidate])
        return results[0] if results else ScoreBreakdown()
