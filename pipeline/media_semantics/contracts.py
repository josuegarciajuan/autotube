"""Canonical P01 contracts: SceneSpec, Candidate, SelectionDecision.

The three dataclasses in this module are the only stable shapes shared by
retrieval, normalization, ranking, replay and provenance.  They are plain
Python (no pydantic) with explicit ``to_dict``/``from_dict`` so fixtures stay
readable and versionable.

Design notes
------------
* Unknown metadata is represented with ``None`` where a scalar is optional
  (``width``, ``duration``, ``tags``).  An empty string/list means the
  provider explicitly returned empty, which is a different signal.
* ``metadata_status`` distinguishes ``NO_METADATA`` from ``NO_MATCH``.  A
  candidate with no searchable metadata must never be ranked as equivalent to
  a candidate with known-but-unrelated metadata (P01-D).
* ``SelectionDecision`` is serialisable end-to-end for offline replay and
  audit; it records rejected candidates with a machine-readable reason.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


_WORD_RE = re.compile(r"[a-z0-9áéíóúüñ]+", re.IGNORECASE)


def _norm_text(value: Any) -> str:
    return " ".join(_WORD_RE.findall(str(value or "").lower()))


def _slug_from_url(url: str) -> str:
    """Extract a human-readable slug from a provider page URL.

    Pexels/Pixabay/Mixkit/Coverr all embed descriptive words in their page
    URLs (``/video/aerial-view-of-city-123/``).  Using them is not metadata
    invention: it is the provider's own slug.  Returns ``""`` when no slug is
    present.
    """
    if not url:
        return ""
    try:
        path = str(url).split("?", 1)[0].split("#", 1)[0]
        m = re.search(
            r"/(?:video|videos|photo|photos|free-video|free-stock-video|clip)/([^/]+)/?$",
            path,
        )
        if not m:
            # Generic fallback: only accept a genuinely descriptive filename.
            # CDN files like ``9.mp4`` or random hashes carry no safe metadata.
            parts = [p for p in path.rstrip("/").split("/") if p]
            if not parts:
                return ""
            last = re.sub(r"\.(?:mp4|webm|mov|m4v|jpg|jpeg|png|webp)$", "", parts[-1])
        else:
            last = m.group(1)
        last = re.sub(r"[-_]?\d{3,}$", "", last)
        last = re.sub(r"[^a-zA-Z0-9]+", " ", last).strip().lower()
        words = [
            w for w in last.split()
            if len(w) >= 3 and w not in {
                "mp4", "webm", "mov", "jpg", "jpeg", "png", "webp", "video", "clip",
            }
        ]
        if len(words) < 2:
            return ""
        return " ".join(words)
    except Exception:
        return ""


class MetadataStatus(str, Enum):
    """Why a candidate's searchable text is (not) usable for matching."""

    NO_METADATA = "NO_METADATA"
    NO_MATCH = "NO_MATCH"
    MATCH = "MATCH"


class SelectionStatus(str, Enum):
    """Terminal state of one scene selection decision."""

    SELECTED = "SELECTED"
    REJECTED = "REJECTED"
    NO_GOOD_CANDIDATE = "NO_GOOD_CANDIDATE"
    RETRIEVAL_FAILED = "RETRIEVAL_FAILED"
    DOWNLOAD_FAILED = "DOWNLOAD_FAILED"
    FALLBACK_SELECTED = "FALLBACK_SELECTED"


@dataclass
class SceneSpec:
    """Structured visual intent for ONE final scene.

    It is derived deterministically from the final scene range (post
    merge/split) plus ``SceneVisualContext``/visual bible, so sub-scenes no
    longer inherit the parent block's single query blindly.
    """

    scene_idx: int = 0
    block_idx: int | None = None
    parent_scene_idx: int | None = None
    media_request_id: str = ""
    origin: str = "block"  # block | subscene | merged
    narration_fragment: str = ""
    subject: str = ""
    action: str = ""
    environment: str = ""
    era: str = ""
    desired_shot: str = ""
    visual_concept: str = ""
    mood_style: str = ""
    must_include: list[str] = field(default_factory=list)
    must_avoid: list[str] = field(default_factory=list)
    previous_context: str = ""
    next_context: str = ""
    duration: float = 0.0
    media_type: str = "image"
    depiction_mode: str = "documentary"
    source: str = "deterministic"
    confidence: float = 0.6
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def identity(self) -> str:
        """Stable, human-readable scene identity used in logs."""
        block = "?" if self.block_idx is None else str(self.block_idx)
        return f"s{self.scene_idx}:b{block}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "scene_idx": int(self.scene_idx),
            "block_idx": self.block_idx,
            "parent_scene_idx": self.parent_scene_idx,
            "media_request_id": self.media_request_id,
            "origin": self.origin,
            "narration_fragment": self.narration_fragment,
            "subject": self.subject,
            "action": self.action,
            "environment": self.environment,
            "era": self.era,
            "desired_shot": self.desired_shot,
            "visual_concept": self.visual_concept,
            "mood_style": self.mood_style,
            "must_include": list(self.must_include),
            "must_avoid": list(self.must_avoid),
            "previous_context": self.previous_context,
            "next_context": self.next_context,
            "duration": float(self.duration or 0.0),
            "media_type": self.media_type,
            "depiction_mode": self.depiction_mode,
            "source": self.source,
            "confidence": float(self.confidence),
            "extra": dict(self.extra),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SceneSpec":
        data = data or {}
        known = {
            "scene_idx", "block_idx", "parent_scene_idx", "media_request_id",
            "origin", "narration_fragment", "subject", "action", "environment", "era",
            "desired_shot", "visual_concept", "mood_style", "must_include",
            "must_avoid", "previous_context", "next_context", "duration",
            "media_type", "depiction_mode", "source", "confidence", "extra",
        }
        kwargs = {k: v for k, v in data.items() if k in known}
        for key in ("must_include", "must_avoid"):
            kwargs[key] = list(kwargs.get(key) or [])
        kwargs.setdefault("scene_idx", 0)
        return cls(**kwargs)


def _as_tags(value: Any) -> list[str] | None:
    """Normalise provider tags. ``None`` means the provider had no tags field."""
    if value is None:
        return None
    if isinstance(value, str):
        items = [t.strip() for t in value.split(",")]
    elif isinstance(value, (list, tuple, set)):
        items = [str(t).strip() for t in value]
    else:
        return None
    return [t for t in dict.fromkeys(items) if t]


def _positive_int(value: Any) -> int | None:
    try:
        ivalue = int(value)
    except (TypeError, ValueError):
        return None
    return ivalue if ivalue > 0 else None


def _positive_float(value: Any) -> float | None:
    try:
        fvalue = float(value)
    except (TypeError, ValueError):
        return None
    return fvalue if fvalue > 0 else None


def _first_str(data: dict[str, Any], *keys: str) -> str:
    """Return the first non-empty string field (provider response order)."""
    for key in keys:
        value = data.get(key)
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return ""


def _infer_preview_type(url: str, declared: Any = "") -> str:
    declared = str(declared or "").strip().lower()
    if declared in ("image", "video"):
        return declared
    path = str(url or "").split("?", 1)[0].lower()
    if path.endswith((".mp4", ".webm", ".mov", ".m4v")):
        return "video"
    if path.endswith((".jpg", ".jpeg", ".png", ".webp", ".gif", ".avif")):
        return "image"
    return ""


def _preview_from_raw(raw: dict[str, Any]) -> dict[str, Any]:
    """Extract a lightweight, provider-declared visual representation.

    This only reads fields already present in the provider response.  It never
    constructs a CDN URL.  Pexels exposes ``image`` plus ``video_pictures``;
    Pixabay images expose ``webformatURL``; scraped providers may expose a
    card ``<img>``.  Everything else is explicitly ``PREVIEW_UNAVAILABLE``.
    """
    thumbnail = _first_str(
        raw, "thumbnail_url", "thumbnail", "image", "poster", "picture",
        "preview_image",
    )
    explicit_preview = _first_str(
        raw, "preview_url", "preview", "preview_video", "preview_video_url",
        "video_preview", "preview_image",
    )
    preview = explicit_preview or _first_str(raw, "image")
    # Pexels: video_pictures = [{"picture": "...", "nr": 0}, ...]
    pictures = raw.get("video_pictures")
    if isinstance(pictures, list) and pictures:
        first = pictures[0]
        if isinstance(first, dict):
            picture = _first_str(first, "picture", "url", "src")
            if picture:
                preview = explicit_preview or picture
                thumbnail = thumbnail or picture
    if not thumbnail and preview:
        ptype = _infer_preview_type(preview)
        if ptype == "image":
            thumbnail = preview
    if not preview and thumbnail:
        preview = thumbnail
    ptype = _infer_preview_type(preview or thumbnail, raw.get("preview_type"))
    # The P02 visual stage consumes still images.  A video-only preview is
    # recorded (preview_type=video) but is not a fetchable still.
    has_still = bool(thumbnail) or (bool(preview) and ptype == "image")
    status = "PREVIEW_AVAILABLE" if has_still else "PREVIEW_UNAVAILABLE"
    return {
        "thumbnail_url": thumbnail,
        "preview_url": preview,
        "preview_type": ptype,
        "preview_width": _positive_int(
            raw.get("preview_width", raw.get("thumbnail_width"))
        ),
        "preview_height": _positive_int(
            raw.get("preview_height", raw.get("thumbnail_height"))
        ),
        "preview_duration": _positive_float(
            raw.get("preview_duration", raw.get("preview_duration_sec"))
        ),
        "visual_source": _first_str(raw, "visual_source") or "",
        "preview_status": str(raw.get("preview_status") or status).upper(),
    }


PREVIEW_AVAILABLE = "PREVIEW_AVAILABLE"
PREVIEW_UNAVAILABLE = "PREVIEW_UNAVAILABLE"


@dataclass
class Candidate:
    """Provider-independent normalised candidate (metadata + preview refs)."""

    provider: str
    provider_asset_id: str = ""
    asset_url: str = ""
    download_url: str = ""
    page_url: str = ""
    preview_url: str = ""
    thumbnail_url: str = ""
    title: str = ""
    description: str = ""
    tags: list[str] | None = None
    width: int | None = None
    height: int | None = None
    duration: float | None = None
    license: str = ""
    provenance: str = ""
    query: str = ""
    query_tier: str = ""
    query_index: int = 0
    provider_rank: int = 0
    page: int = 1
    media_type: str = "video"
    metadata_status: MetadataStatus = MetadataStatus.NO_METADATA
    metadata_completeness: float = 0.0
    searchable_text: str = ""
    preview_type: str = ""
    preview_width: int | None = None
    preview_height: int | None = None
    preview_duration: float | None = None
    visual_source: str = ""
    preview_status: str = PREVIEW_UNAVAILABLE
    raw_candidate: dict[str, Any] | None = field(default=None, repr=False, compare=False)

    # ── construction ──────────────────────────────────────────
    @classmethod
    def from_provider_dict(
        cls,
        raw: dict[str, Any],
        *,
        provider: str,
        query: str = "",
        query_tier: str = "",
        query_index: int = 0,
        provider_rank: int = 0,
        page: int = 1,
        media_type: str | None = None,
    ) -> "Candidate":
        """Build a Candidate from the dicts emitted by ``MediaFetcher``."""
        raw = dict(raw or {})
        page_url = str(raw.get("page_url") or "")
        # Mixkit/Coverr expose the landing page as ``url`` (lazy download);
        # normal video providers expose a CDN URL plus a separate page_url.
        slug = _slug_from_url(page_url or str(raw.get("url") or ""))
        tags = _as_tags(raw.get("tags"))
        title = str(raw.get("title") or "").strip()
        description = str(raw.get("description") or "").strip()
        # Mixkit/Coverr expose only the page URL; the slug is safe metadata.
        if not title and slug:
            title = slug
        parts = [title, description]
        if tags:
            parts.extend(tags)
        searchable = _norm_text(" ".join(parts))
        mtype = media_type or str(raw.get("type") or "video")
        preview = _preview_from_raw(raw)
        visual_source = (
            preview.get("visual_source")
            or (str(provider or raw.get("provider") or raw.get("source") or "unknown"))
        )
        candidate = cls(
            provider=str(provider or raw.get("provider") or raw.get("source") or "unknown"),
            provider_asset_id=str(raw.get("id") or ""),
            asset_url=str(raw.get("url") or raw.get("download_url") or ""),
            download_url=str(raw.get("download_url") or raw.get("url") or ""),
            page_url=page_url,
            preview_url=preview["preview_url"],
            thumbnail_url=preview["thumbnail_url"],
            title=title,
            description=description,
            tags=tags,
            width=_positive_int(raw.get("width")),
            height=_positive_int(raw.get("height")),
            duration=_positive_float(raw.get("duration")),
            license=str(raw.get("license") or ""),
            provenance=str(raw.get("provenance") or provider or ""),
            query=query or "",
            query_tier=query_tier or "",
            query_index=int(query_index),
            provider_rank=int(provider_rank),
            page=int(page),
            media_type=mtype,
            preview_type=preview["preview_type"],
            preview_width=preview["preview_width"],
            preview_height=preview["preview_height"],
            preview_duration=preview["preview_duration"],
            visual_source=visual_source,
            preview_status=preview["preview_status"],
            raw_candidate=raw,
        )
        candidate.metadata_completeness = candidate.compute_completeness()
        candidate.searchable_text = searchable
        candidate.metadata_status = (
            MetadataStatus.NO_METADATA if not searchable else MetadataStatus.NO_MATCH
        )
        return candidate

    def compute_completeness(self) -> float:
        """Fraction of useful metadata fields present (0.0–1.0)."""
        checks = [
            bool(self.title),
            bool(self.description),
            bool(self.tags),
            bool(self.duration),
            bool(self.width and self.height),
            bool(self.page_url),
        ]
        return round(sum(1 for c in checks if c) / len(checks), 4)

    # ── identity / serialisation ──────────────────────────────
    @property
    def canonical_key(self) -> str:
        """Deterministic dedup key independent of provider CDN URL quirks."""
        if self.provider_asset_id:
            return f"{self.provider}:id:{self.provider_asset_id}"
        url = self.download_url or self.asset_url or self.page_url
        digest = hashlib.sha1(str(url).encode("utf-8")).hexdigest()[:16]
        return f"{self.provider}:url:{digest}"

    @property
    def has_metadata(self) -> bool:
        status = (
            self.metadata_status.value
            if isinstance(self.metadata_status, MetadataStatus)
            else str(self.metadata_status or "")
        )
        return status != MetadataStatus.NO_METADATA.value and bool(self.searchable_text)

    @property
    def has_preview(self) -> bool:
        """True only when a provider-declared lightweight still exists."""
        return bool(
            self.visual_representation_url
            and str(self.preview_status or "").upper() != PREVIEW_UNAVAILABLE
        )

    @property
    def visual_representation_url(self) -> str:
        """Best still image for visual inspection (never the full video)."""
        if self.thumbnail_url:
            ttype = _infer_preview_type(self.thumbnail_url)
            if ttype in ("", "image"):
                return self.thumbnail_url
        if self.preview_url and (
            self.preview_type or _infer_preview_type(self.preview_url)
        ) == "image":
            return self.preview_url
        return ""

    def to_dict(self) -> dict[str, Any]:
        status = (
            self.metadata_status.value
            if isinstance(self.metadata_status, MetadataStatus)
            else str(self.metadata_status or MetadataStatus.NO_METADATA.value)
        )
        return {
            "provider": self.provider,
            "provider_asset_id": self.provider_asset_id,
            "asset_url": self.asset_url,
            "download_url": self.download_url,
            "page_url": self.page_url,
            "preview_url": self.preview_url,
            "thumbnail_url": self.thumbnail_url,
            "title": self.title,
            "description": self.description,
            "tags": None if self.tags is None else list(self.tags),
            "width": self.width,
            "height": self.height,
            "duration": self.duration,
            "license": self.license,
            "provenance": self.provenance,
            "query": self.query,
            "query_tier": self.query_tier,
            "query_index": self.query_index,
            "provider_rank": self.provider_rank,
            "page": self.page,
            "media_type": self.media_type,
            "metadata_status": status,
            "metadata_completeness": self.metadata_completeness,
            "searchable_text": self.searchable_text,
            "preview_type": self.preview_type,
            "preview_width": self.preview_width,
            "preview_height": self.preview_height,
            "preview_duration": self.preview_duration,
            "visual_source": self.visual_source,
            "preview_status": self.preview_status,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Candidate":
        data = dict(data or {})
        status = data.get("metadata_status", MetadataStatus.NO_METADATA)
        try:
            status = MetadataStatus(status)
        except ValueError:
            status = MetadataStatus.NO_METADATA
        tags = data.get("tags")
        return cls(
            provider=str(data.get("provider") or "unknown"),
            provider_asset_id=str(data.get("provider_asset_id") or ""),
            asset_url=str(data.get("asset_url") or ""),
            download_url=str(data.get("download_url") or ""),
            page_url=str(data.get("page_url") or ""),
            preview_url=str(data.get("preview_url") or ""),
            thumbnail_url=str(data.get("thumbnail_url") or ""),
            title=str(data.get("title") or ""),
            description=str(data.get("description") or ""),
            tags=None if tags is None else list(tags),
            width=data.get("width"),
            height=data.get("height"),
            duration=data.get("duration"),
            license=str(data.get("license") or ""),
            provenance=str(data.get("provenance") or ""),
            query=str(data.get("query") or ""),
            query_tier=str(data.get("query_tier") or ""),
            query_index=int(data.get("query_index") or 0),
            provider_rank=int(data.get("provider_rank") or 0),
            page=int(data.get("page") or 1),
            media_type=str(data.get("media_type") or "video"),
            metadata_status=status,
            metadata_completeness=float(data.get("metadata_completeness") or 0.0),
            searchable_text=str(data.get("searchable_text") or ""),
            preview_type=str(data.get("preview_type") or ""),
            preview_width=data.get("preview_width"),
            preview_height=data.get("preview_height"),
            preview_duration=data.get("preview_duration"),
            visual_source=str(data.get("visual_source") or ""),
            preview_status=str(
                data.get("preview_status")
                or (
                    PREVIEW_AVAILABLE
                    if (data.get("thumbnail_url") or data.get("preview_type") == "image")
                    else PREVIEW_UNAVAILABLE
                )
            ),
            raw_candidate=dict(data.get("raw_candidate") or {}) or None,
        )


@dataclass
class RejectedCandidate:
    """One rejected candidate plus the reason and the score that produced it."""

    candidate: Candidate
    score: float = 0.0
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate": self.candidate.to_dict(),
            "score": round(float(self.score), 6),
            "reason": self.reason,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RejectedCandidate":
        return cls(
            candidate=Candidate.from_dict(data.get("candidate") or {}),
            score=float(data.get("score") or 0.0),
            reason=str(data.get("reason") or ""),
        )


@dataclass
class SelectionDecision:
    """Complete, serialisable record of why a scene got (or did not get) an asset."""

    scene_spec: SceneSpec
    status: SelectionStatus = SelectionStatus.NO_GOOD_CANDIDATE
    selector_version: str = "p01-semantic-1"
    queries: list[str] = field(default_factory=list)
    providers_consulted: list[str] = field(default_factory=list)
    candidates_considered: int = 0
    candidate_scores: list[dict[str, Any]] = field(default_factory=list)
    rejected: list[RejectedCandidate] = field(default_factory=list)
    winner: Candidate | None = None
    winner_score: float = 0.0
    threshold: float = 0.0
    rejection_reason: str = ""
    fallback_used: bool = False
    fallback_reason: str = ""
    download_attempts: list[dict[str, Any]] = field(default_factory=list)
    timing_ms: float = 0.0
    cost: dict[str, Any] = field(default_factory=dict)
    extra: dict[str, Any] = field(default_factory=dict)

    # ── P02 visual intelligence provenance (additive, replayable) ──
    visual_mode: str = ""
    visual_backend: str = ""
    visual_model: str = ""
    visual_similarity: float | None = None
    frame_scores: list[dict[str, Any]] = field(default_factory=list)
    visual_constraints: dict[str, Any] = field(default_factory=dict)
    visual_rejection_reason: str = ""
    frames_sampled: int = 0
    visual_verification_status: str = ""
    visual_timing_ms: float = 0.0
    visual_cost: dict[str, Any] = field(default_factory=dict)
    degradation_level: str = ""
    text_score: float = 0.0
    visual_preview_relevance: float = 0.0
    frame_relevance: float = 0.0
    vlm_relevance: float = 0.0
    negative_penalty: float = 0.0
    era_penalty: float = 0.0
    combined_score: float = 0.0

    @property
    def selected(self) -> bool:
        return self.status in (
            SelectionStatus.SELECTED, SelectionStatus.FALLBACK_SELECTED,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "selector_version": self.selector_version,
            "status": self.status.value,
            "scene_spec": self.scene_spec.to_dict(),
            "queries": list(self.queries),
            "providers_consulted": list(self.providers_consulted),
            "candidates_considered": int(self.candidates_considered),
            "candidate_scores": list(self.candidate_scores),
            "rejected": [r.to_dict() for r in self.rejected],
            "winner": None if self.winner is None else self.winner.to_dict(),
            "winner_score": round(float(self.winner_score), 6),
            "threshold": round(float(self.threshold), 6),
            "rejection_reason": self.rejection_reason,
            "fallback_used": bool(self.fallback_used),
            "fallback_reason": self.fallback_reason,
            "download_attempts": list(self.download_attempts),
            "timing_ms": round(float(self.timing_ms), 3),
            "cost": dict(self.cost),
            "extra": dict(self.extra),
            "visual_mode": self.visual_mode,
            "visual_backend": self.visual_backend,
            "visual_model": self.visual_model,
            "visual_similarity": (
                None if self.visual_similarity is None
                else round(float(self.visual_similarity), 6)
            ),
            "frame_scores": list(self.frame_scores),
            "visual_constraints": dict(self.visual_constraints),
            "visual_rejection_reason": self.visual_rejection_reason,
            "frames_sampled": int(self.frames_sampled),
            "visual_verification_status": self.visual_verification_status,
            "visual_timing_ms": round(float(self.visual_timing_ms), 3),
            "visual_cost": dict(self.visual_cost),
            "degradation_level": self.degradation_level,
            "text_score": round(float(self.text_score), 6),
            "visual_preview_relevance": round(float(self.visual_preview_relevance), 6),
            "frame_relevance": round(float(self.frame_relevance), 6),
            "vlm_relevance": round(float(self.vlm_relevance), 6),
            "negative_penalty": round(float(self.negative_penalty), 6),
            "era_penalty": round(float(self.era_penalty), 6),
            "combined_score": round(float(self.combined_score), 6),
        }

    def visual_summary(self) -> dict[str, Any]:
        """Compact JSON-safe P02 summary for assets/logs (no blobs)."""
        return {
            "visual_mode": self.visual_mode,
            "degradation_level": self.degradation_level,
            "visual_backend": self.visual_backend,
            "visual_model": self.visual_model,
            "visual_similarity": self.visual_similarity,
            "frame_scores": list(self.frame_scores),
            "visual_constraints": dict(self.visual_constraints),
            "visual_rejection_reason": self.visual_rejection_reason,
            "frames_sampled": self.frames_sampled,
            "visual_verification_status": self.visual_verification_status,
            "visual_timing_ms": self.visual_timing_ms,
            "visual_cost": dict(self.visual_cost),
            "text_score": self.text_score,
            "combined_score": self.combined_score,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SelectionDecision":
        data = dict(data or {})
        try:
            status = SelectionStatus(data.get("status") or "NO_GOOD_CANDIDATE")
        except ValueError:
            status = SelectionStatus.NO_GOOD_CANDIDATE
        winner = data.get("winner")
        return cls(
            scene_spec=SceneSpec.from_dict(data.get("scene_spec") or {}),
            status=status,
            selector_version=str(data.get("selector_version") or "p01-semantic-1"),
            queries=list(data.get("queries") or []),
            providers_consulted=list(data.get("providers_consulted") or []),
            candidates_considered=int(data.get("candidates_considered") or 0),
            candidate_scores=list(data.get("candidate_scores") or []),
            rejected=[RejectedCandidate.from_dict(r) for r in data.get("rejected") or []],
            winner=None if not winner else Candidate.from_dict(winner),
            winner_score=float(data.get("winner_score") or 0.0),
            threshold=float(data.get("threshold") or 0.0),
            rejection_reason=str(data.get("rejection_reason") or ""),
            fallback_used=bool(data.get("fallback_used")),
            fallback_reason=str(data.get("fallback_reason") or ""),
            download_attempts=list(data.get("download_attempts") or []),
            timing_ms=float(data.get("timing_ms") or 0.0),
            cost=dict(data.get("cost") or {}),
            extra=dict(data.get("extra") or {}),
            visual_mode=str(data.get("visual_mode") or ""),
            visual_backend=str(data.get("visual_backend") or ""),
            visual_model=str(data.get("visual_model") or ""),
            visual_similarity=(
                None if data.get("visual_similarity") is None
                else float(data.get("visual_similarity"))
            ),
            frame_scores=list(data.get("frame_scores") or []),
            visual_constraints=dict(data.get("visual_constraints") or {}),
            visual_rejection_reason=str(data.get("visual_rejection_reason") or ""),
            frames_sampled=int(data.get("frames_sampled") or 0),
            visual_verification_status=str(data.get("visual_verification_status") or ""),
            visual_timing_ms=float(data.get("visual_timing_ms") or 0.0),
            visual_cost=dict(data.get("visual_cost") or {}),
            degradation_level=str(data.get("degradation_level") or ""),
            text_score=float(data.get("text_score") or 0.0),
            visual_preview_relevance=float(data.get("visual_preview_relevance") or 0.0),
            frame_relevance=float(data.get("frame_relevance") or 0.0),
            vlm_relevance=float(data.get("vlm_relevance") or 0.0),
            negative_penalty=float(data.get("negative_penalty") or 0.0),
            era_penalty=float(data.get("era_penalty") or 0.0),
            combined_score=float(data.get("combined_score") or 0.0),
        )
