"""P02-E/O/P — funnel that adds visual evidence to the P01 selector.

Flow::

    textual rank → preview top-N → visual rerank → shortlist
    → download shortlist (bounded) → sample frames → frame score
    → optional VLM → constraints → combined score → winner

Every stage is fail-open.  A missing model/preview/VLM degrades to a less
ambitious level instead of breaking the pipeline, and the level actually used
is recorded in ``SelectionDecision.degradation_level``.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Callable, Sequence

from .config import SemanticConfig, VisualConfig, VisualWeights
from .contracts import (
    Candidate,
    RejectedCandidate,
    SelectionDecision,
    SelectionStatus,
)
from .visual_backends import NullVisualEmbeddingBackend, VisualEmbeddingBackend, build_visual_backend
from .visual_cache import VisualEmbeddingCache, sha256_file, visual_cache_key
from .visual_description import build_visual_description
from .visual_frames import FrameSamplingResult, sample_semantic_frames
from .visual_previews import PreviewFetcher, PreviewResult
from .visual_scoring import (
    FRAME_EXTRACTION_FAILED,
    PREVIEW_UNAVAILABLE,
    aggregate_frame_similarities,
    compute_combined_score,
    cosine_similarity,
    evaluate_visual_constraints,
    technical_quality_score,
)
from .visual_vlm import (
    NullVLMBackend,
    VLMBackend,
    VisualVerificationResult,
    build_vlm_backend,
    verify_with_backend,
)

logger = logging.getLogger(__name__)

VISUAL_SELECTOR_VERSION = "p02-visual-1"


def _backend_available(backend: Any) -> bool:
    if backend is None or isinstance(backend, NullVisualEmbeddingBackend):
        return False
    try:
        available = getattr(backend, "available", None)
        return bool(available()) if callable(available) else True
    except Exception:
        return False


def _vlm_available(backend: Any) -> bool:
    if backend is None or isinstance(backend, NullVLMBackend):
        return False
    try:
        available = getattr(backend, "available", None)
        return bool(available()) if callable(available) else True
    except Exception:
        return False


class VisualIntelligencePipeline:
    """Visual extension of :class:`SemanticMediaSelector` (never a parallel core)."""

    def __init__(
        self,
        config: SemanticConfig | VisualConfig | None = None,
        *,
        textual_selector: Any = None,
        visual_backend: VisualEmbeddingBackend | None = None,
        vlm_backend: VLMBackend | None = None,
        preview_fetcher: PreviewFetcher | None = None,
        cache: VisualEmbeddingCache | None = None,
        frame_sampler: Callable[..., FrameSamplingResult] | None = None,
        download_fn: Callable[[Candidate], dict | None] | None = None,
    ) -> None:
        if isinstance(config, SemanticConfig):
            self.semantic_config = config
            self.config = config.visual
        else:
            self.config = config or VisualConfig()
            self.semantic_config = SemanticConfig(visual=self.config)
        self.textual_selector = textual_selector
        self.backend = visual_backend if visual_backend is not None else build_visual_backend(self.config)
        self._vlm_injected = vlm_backend is not None
        self.vlm = vlm_backend if vlm_backend is not None else build_vlm_backend(self.config)
        self.preview_fetcher = preview_fetcher or PreviewFetcher(self.config.budget)
        self.cache = cache if cache is not None else VisualEmbeddingCache(
            path=self.config.cache_path or None,
            enabled=self.config.cache_enabled,
        )
        self._frame_sampler = frame_sampler
        self.download_fn = download_fn

    # ── helpers ────────────────────────────────────────────────
    @property
    def weights(self) -> VisualWeights:
        return self.config.weights

    def _textual_rank(self, spec, candidates, threshold, pre_filter):
        if self.textual_selector is not None:
            return self.textual_selector.rank(
                spec, candidates, threshold=threshold, pre_filter=pre_filter,
            )
        from .selector import SemanticMediaSelector

        return SemanticMediaSelector(config=SemanticConfig(visual=VisualConfig(mode="off"))).rank(
            spec, candidates, threshold=threshold, pre_filter=pre_filter,
        )

    def _encode_texts(self, texts: Sequence[str]) -> list[list[float]] | None:
        try:
            return self.backend.encode_texts(list(texts))
        except Exception as exc:  # noqa: BLE001
            logger.info("P02 backend text encoding failed: %s", exc)
            return None

    def _encode_images_cached(
        self, paths: Sequence[str], candidate_keys: Sequence[str]
    ) -> list[list[float]] | None:
        """Encode images with a strict backend/model/representation cache key."""
        if not paths:
            return []
        versions = (
            str(getattr(self.backend, "name", "unknown")),
            str(getattr(self.backend, "model_version", "")),
        )
        keys: list[str] = []
        for path, candidate_key in zip(paths, candidate_keys):
            try:
                digest = sha256_file(path)
            except Exception:
                digest = str(path)
            keys.append(
                visual_cache_key(
                    provider=candidate_key,
                    asset_id="",
                    representation=digest,
                    backend=versions[0],
                    model_version=versions[1],
                )
            )
        cached: list[list[float] | None] = [self.cache.get(key) for key in keys]
        missing = [i for i, value in enumerate(cached) if value is None]
        if missing:
            encoded = None
            try:
                encoded = self.backend.encode_images([paths[i] for i in missing])
            except Exception as exc:  # noqa: BLE001
                logger.info("P02 backend image encoding failed: %s", exc)
            if not encoded or len(encoded) != len(missing):
                return None
            for index, vector in zip(missing, encoded):
                cached[index] = vector
                self.cache.set(keys[index], vector)
        return [list(v) for v in cached if v is not None]

    def _negative_embedding_hits(
        self, spec, image_vectors: Sequence[Sequence[float]]
    ) -> list[tuple[str, float]]:
        terms = [str(t).strip() for t in (spec.must_avoid or []) if str(t).strip()]
        if not terms or not image_vectors or not _backend_available(self.backend):
            return []
        vectors = self._encode_texts([f"a photo of {term}" for term in terms])
        if not vectors or len(vectors) != len(terms):
            return []
        hits: list[tuple[str, float]] = []
        for term, vec in zip(terms, vectors):
            best = max((cosine_similarity(vec, img) for img in image_vectors), default=0.0)
            if best >= self.config.must_avoid_similarity_threshold:
                hits.append((term, best))
        return hits

    def _frame_rows(self, sampling: FrameSamplingResult, values: Sequence[float]) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for index, frame in enumerate(sampling.frames):
            value = float(values[index]) if index < len(values) else 0.0
            try:
                from .visual_cache import sha256_file as _sha
                digest = _sha(frame)
            except Exception:
                digest = ""
            rows.append({
                "frame_index": index,
                "timestamp": (
                    round(float(sampling.timestamps[index]), 3)
                    if index < len(sampling.timestamps) else None
                ),
                "path": str(frame),
                "sha256": digest,
                "similarity": round(value, 6),
            })
        return rows

    def _match_score_row(
        self, decision: SelectionDecision, candidate: Candidate
    ) -> dict[str, Any] | None:
        url = candidate.download_url or candidate.asset_url
        for row in decision.candidate_scores:
            if row.get("url") == url:
                return row
            if (
                row.get("provider") == candidate.provider
                and row.get("provider_asset_id") == candidate.provider_asset_id
                and candidate.provider_asset_id
            ):
                return row
        return None

    # ── main entry point ───────────────────────────────────────
    def select_and_download(
        self,
        scene_spec,
        candidates: list[Candidate],
        download_fn: Callable[[Candidate], dict | None] | None = None,
        *,
        threshold: float | None = None,
        pre_filter: Callable[[Candidate], str | None] | None = None,
        pre_download_filter: Callable[[Candidate], bool] | None = None,
    ) -> SelectionDecision:
        started = time.time()
        download = download_fn or self.download_fn
        if download is None:
            raise ValueError("VisualIntelligencePipeline requires a download_fn")
        decision = self._textual_rank(
            scene_spec,
            list(candidates or []),
            threshold=threshold,
            pre_filter=pre_filter,
        )
        decision.visual_mode = self.config.mode
        decision.visual_backend = str(getattr(self.backend, "name", "none"))
        decision.visual_model = str(getattr(self.backend, "model_version", "") or "")
        decision.visual_cost = {
            "preview_candidates": 0,
            "previews_available": 0,
            "downloads": 0,
            "frames": 0,
            "vlm_calls": 0,
            "embeddings": 0,
            "cache": self.cache.stats(),
        }
        decision.degradation_level = "TEXT_ONLY"
        decision.threshold = (
            self.config.combined_threshold if threshold is None else float(threshold)
        )
        if not decision.selected or not candidates:
            decision.visual_mode = self.config.mode
            decision.extra["visual_summary"] = decision.visual_summary()
            decision.timing_ms = (time.time() - started) * 1000
            return decision

        backend_ok = _backend_available(self.backend)
        mode = self.config.mode
        if mode in ("off", "text_only") or not backend_ok:
            decision.degradation_level = "TEXT_ONLY"
            decision.visual_verification_status = "NOT_RUN"
            decision.extra["visual_summary"] = decision.visual_summary()
            decision.timing_ms = (time.time() - started) * 1000
            # The textual winner still needs to be downloaded by the caller.
            return self._download_textual_only(
                decision, download, pre_download_filter=pre_download_filter,
                started=started,
            )

        approved: list[Candidate] = list(getattr(decision, "approved", []) or [])
        if not approved:
            decision.degradation_level = "TEXT_ONLY"
            decision.timing_ms = (time.time() - started) * 1000
            return decision

        visual_description = build_visual_description(scene_spec)
        preview_scores: dict[str, float] = {}
        preview_results: dict[str, PreviewResult] = {}
        preview_paths: list[str] = []
        preview_keys: list[str] = []
        preview_candidates = approved[: self.config.budget.preview_candidates]

        # ── preview / thumbnail stage ──────────────────────────
        if self.config.uses_previews and self.config.preview_fetch_enabled:
            for candidate in preview_candidates:
                try:
                    result = self.preview_fetcher.fetch(candidate)
                except Exception as exc:  # fail-open: preview is optional
                    result = PreviewResult(
                        candidate_key=candidate.canonical_key,
                        status=PREVIEW_UNAVAILABLE, error=str(exc)[:200],
                        visual_source=candidate.visual_source,
                    )
                preview_results[candidate.canonical_key] = result
                if result.available:
                    preview_paths.append(result.path)
                    preview_keys.append(candidate.canonical_key)
            decision.visual_cost["preview_candidates"] = len(preview_candidates)
            decision.visual_cost["previews_available"] = len(preview_paths)
            if preview_paths:
                text_vectors = self._encode_texts([visual_description])
                image_vectors = (
                    self._encode_images_cached(preview_paths, preview_keys)
                    if text_vectors else None
                )
                if text_vectors and image_vectors and len(image_vectors) == len(preview_paths):
                    decision.visual_cost["embeddings"] += 1 + len(image_vectors)
                    for key, image_vector in zip(preview_keys, image_vectors):
                        preview_scores[key] = cosine_similarity(text_vectors[0], image_vector)
                else:
                    preview_scores = {}
            if not preview_paths:
                decision.visual_constraints["preview"] = PREVIEW_UNAVAILABLE
            decision.extra["preview_results"] = [
                result.to_dict() for result in preview_results.values()
            ][: self.semantic_config.budget.provenance_candidate_limit]
        else:
            decision.visual_constraints["preview"] = PREVIEW_UNAVAILABLE

        if mode == "embedding_only":
            level = "EMBEDDING_ONLY" if preview_scores else "TEXT_ONLY"
        elif mode == "frame_embedding_only":
            level = "FRAME_EMBEDDING_ONLY"
        elif mode == "full_visual":
            level = "FULL_VISUAL" if preview_scores else "FRAME_EMBEDDING_ONLY"
        else:
            level = "TEXT_ONLY"
        if level == "TEXT_ONLY":
            decision.degradation_level = level
            decision.extra["visual_summary"] = decision.visual_summary()
            return self._download_textual_only(
                decision, download, pre_download_filter=pre_download_filter,
                started=started,
            )
        decision.degradation_level = level

        # ── visual shortlist (or textual order when previews unavailable) ──
        if preview_scores:
            def preview_sort_key(candidate: Candidate):
                text_row = self._match_score_row(decision, candidate)
                text_score = float((text_row or {}).get("final") or 0.0)
                return (
                    -(0.70 * text_score + 0.30 * preview_scores.get(candidate.canonical_key, 0.0)),
                    candidate.canonical_key,
                )

            shortlist = sorted(preview_candidates, key=preview_sort_key)[
                : self.config.budget.shortlist
            ]
        else:
            shortlist = list(preview_candidates[: self.config.budget.shortlist])

        # ── bounded download + frame/VLM verification ──────────
        downloaded: list[dict[str, Any]] = []
        attempts = 0
        for rank, candidate in enumerate(shortlist):
            if attempts >= self.config.budget.download_candidates:
                break
            if pre_download_filter is not None:
                try:
                    if not bool(pre_download_filter(candidate)):
                        decision.download_attempts.append({
                            "provider": candidate.provider,
                            "url": candidate.download_url or candidate.asset_url,
                            "query": candidate.query,
                            "rank": rank,
                            "result": "skipped_duplicate",
                        })
                        continue
                except Exception:
                    pass
            attempts += 1
            decision.download_attempts.append({
                "provider": candidate.provider,
                "url": candidate.download_url or candidate.asset_url,
                "query": candidate.query,
                "rank": rank,
                "result": "attempted",
            })
            try:
                asset = download(candidate)
            except Exception as exc:  # noqa: BLE001
                asset = None
                decision.download_attempts[-1]["error"] = str(exc)[:200]
            if not asset or not asset.get("path"):
                decision.download_attempts[-1]["result"] = "failed"
                continue
            decision.download_attempts[-1]["result"] = "downloaded"
            decision.visual_cost["downloads"] += 1
            row: dict[str, Any] = {
                "candidate": candidate,
                "asset": asset,
                "text_score": float((self._match_score_row(decision, candidate) or {}).get("final") or 0.0),
                "preview_relevance": float(preview_scores.get(candidate.canonical_key, 0.0)),
                "frame_relevance": 0.0,
                "vlm_relevance": 0.0,
                "technical_quality": technical_quality_score(
                    width=asset.get("width") or candidate.width,
                    height=asset.get("height") or candidate.height,
                ),
                "frame_scores": [],
                "constraints": None,
                "verification": None,
                "combined": None,
            }
            # Frame-level evidence
            if self.config.uses_frames and self.config.frames_enabled and level in (
                "FRAME_EMBEDDING_ONLY", "FULL_VISUAL",
            ):
                try:
                    sampling = (
                        self._frame_sampler(asset, max_frames=self.config.budget.frame_count)
                        if self._frame_sampler is not None
                        else sample_semantic_frames(
                            asset, max_frames=self.config.budget.frame_count,
                        )
                    )
                except Exception as exc:  # fail-open: frame evidence is optional
                    sampling = FrameSamplingResult(
                        error=f"frame_sampler_failed:{str(exc)[:160]}",
                    )
                row["frame_sampling"] = sampling
                if sampling.ok:
                    text_vectors = self._encode_texts([visual_description])
                    image_vectors = (
                        self._encode_images_cached(sampling.frames, [candidate.canonical_key] * len(sampling.frames))
                        if text_vectors else None
                    )
                    if text_vectors and image_vectors and len(image_vectors) == len(sampling.frames):
                        values = [cosine_similarity(text_vectors[0], vec) for vec in image_vectors]
                        aggregate = aggregate_frame_similarities(
                            values,
                            min_similarity=self.config.frame_min_similarity,
                            weights=self.weights,
                        )
                        row["frame_relevance"] = aggregate["score"]
                        row["frame_scores"] = self._frame_rows(sampling, values)
                        row["frame_aggregate"] = aggregate
                        decision.visual_cost["frames"] += len(sampling.frames)
                        decision.visual_cost["embeddings"] += 1 + len(image_vectors)
                    else:
                        row["frame_error"] = FRAME_EXTRACTION_FAILED
                else:
                    row["frame_error"] = sampling.error or FRAME_EXTRACTION_FAILED
            downloaded.append(row)

        if not downloaded:
            decision.status = SelectionStatus.DOWNLOAD_FAILED
            decision.winner = None
            decision.winner_score = 0.0
            decision.rejection_reason = "all_visual_downloads_failed"
            decision.extra["visual_summary"] = decision.visual_summary()
            decision.timing_ms = (time.time() - started) * 1000
            return decision

        # ── pre-VLM combined scoring to choose the VLM budget ──
        for row in downloaded:
            combined = compute_combined_score(
                text_relevance=row["text_score"],
                preview_relevance=row["preview_relevance"],
                frame_relevance=row["frame_relevance"],
                technical_quality=row["technical_quality"],
                weights=self.weights,
                available_components={
                    "text": True,
                    "preview": bool(preview_scores),
                    "frame": bool(row.get("frame_scores")),
                    "vlm": False,
                    "technical_quality": True,
                },
            )
            row["combined"] = combined
        vlm_available = _vlm_available(self.vlm)
        pre_sorted = sorted(
            downloaded,
            key=lambda r: (-float(r["combined"]["combined_score"]), r["candidate"].canonical_key),
        )
        vlm_rows = (
            pre_sorted[: self.config.budget.vlm_candidates]
            if (self.config.uses_vlm or self._vlm_injected) and vlm_available and level == "FULL_VISUAL"
            else []
        )

        # ── optional VLM + constraints ─────────────────────────
        for row in downloaded:
            verification: VisualVerificationResult | None = None
            if row in vlm_rows:
                sampling = row.get("frame_sampling")
                frames = sampling.frames if sampling and sampling.ok else []
                if frames:
                    verification = verify_with_backend(
                        self.vlm,
                        scene_spec,
                        narration=scene_spec.narration_fragment,
                        frames=frames,
                        provider_metadata={
                            "title": row["candidate"].title,
                            "tags": row["candidate"].tags or [],
                            "provider": row["candidate"].provider,
                        },
                        timeout=self.config.budget.vlm_timeout,
                    )
                    decision.visual_cost["vlm_calls"] += 1
                else:
                    verification = VisualVerificationResult(
                        status="VLM_FAILED", decision="uncertain",
                        backend=str(getattr(self.vlm, "name", "")),
                        error="no_frames_for_vlm",
                    )
            row["verification"] = verification
            image_vectors: list[Sequence[float]] = []
            if row.get("frame_sampling") and row["frame_sampling"].ok:
                try:
                    # Reuse the cached frame embeddings (no duplicate model work).
                    vectors = self._encode_images_cached(
                        row["frame_sampling"].frames,
                        [row["candidate"].canonical_key] * len(row["frame_sampling"].frames),
                    )
                    image_vectors = list(vectors or [])
                except Exception:
                    image_vectors = []
            hits = self._negative_embedding_hits(scene_spec, image_vectors) if self.config.constraints_enabled else []
            constraints = evaluate_visual_constraints(
                scene_spec,
                verification,
                negative_embedding_hits=hits,
                min_confidence=self.config.min_vlm_confidence,
                must_avoid_threshold=self.config.must_avoid_similarity_threshold,
                reject_on_negative=self.config.visual_reject_on_negative,
                reject_on_era=self.config.visual_reject_on_era and self.config.era_check_enabled,
            )
            row["constraints"] = constraints
            vlm_relevance = 0.0
            if verification is not None and verification.usable:
                vlm_relevance = float(verification.overall_semantic_match or 0.0) * float(
                    verification.confidence or 0.0
                )
            row["vlm_relevance"] = vlm_relevance
            row["combined"] = compute_combined_score(
                text_relevance=row["text_score"],
                preview_relevance=row["preview_relevance"],
                frame_relevance=row["frame_relevance"],
                vlm_relevance=vlm_relevance,
                technical_quality=row["technical_quality"],
                negative_penalty=constraints.negative_penalty,
                era_penalty=constraints.era_penalty,
                must_include_penalty=constraints.must_include_penalty,
                weights=self.weights,
                available_components={
                    "text": True,
                    "preview": bool(preview_scores),
                    "frame": bool(row.get("frame_scores")),
                    "vlm": bool(verification is not None and verification.usable),
                    "technical_quality": True,
                },
            )

        eligible: list[dict[str, Any]] = []
        rejected_visual: list[RejectedCandidate] = []
        for row in downloaded:
            constraints = row["constraints"]
            reason = constraints.rejection_reason if constraints is not None else ""
            if reason:
                rejected_visual.append(RejectedCandidate(
                    candidate=row["candidate"],
                    score=float(row["combined"]["combined_score"]),
                    reason=reason,
                ))
                continue
            if float(row["combined"]["combined_score"]) < float(decision.threshold):
                rejected_visual.append(RejectedCandidate(
                    candidate=row["candidate"],
                    score=float(row["combined"]["combined_score"]),
                    reason="below_visual_threshold",
                ))
                continue
            eligible.append(row)

        decision.rejected.extend(rejected_visual)
        decision.rejected = decision.rejected[: self.semantic_config.budget.provenance_rejected_limit]
        if not eligible:
            decision.status = SelectionStatus.NO_GOOD_CANDIDATE
            decision.winner = None
            decision.winner_score = 0.0
            decision.rejection_reason = "no_candidate_above_visual_threshold"
            reasons = [r.reason for r in rejected_visual]
            decision.visual_rejection_reason = reasons[0] if reasons else ""
            decision.extra["visual_summary"] = decision.visual_summary()
            decision.timing_ms = (time.time() - started) * 1000
            self._record_visual_rows(decision, downloaded)
            return decision

        eligible.sort(
            key=lambda r: (
                -float(r["combined"]["combined_score"]),
                -float(r["text_score"]),
                r["candidate"].canonical_key,
            )
        )
        winner = eligible[0]
        candidate: Candidate = winner["candidate"]
        asset: dict[str, Any] = winner["asset"]
        constraints = winner["constraints"]
        verification = winner["verification"]
        sampling = winner.get("frame_sampling")
        decision.winner = candidate
        decision.winner_score = float(winner["combined"]["combined_score"])
        decision.combined_score = decision.winner_score
        decision.text_score = float(winner["text_score"])
        decision.visual_preview_relevance = float(winner["preview_relevance"])
        decision.frame_relevance = float(winner["frame_relevance"])
        decision.vlm_relevance = float(winner["vlm_relevance"])
        decision.negative_penalty = float(constraints.negative_penalty if constraints else 0.0)
        decision.era_penalty = float(constraints.era_penalty if constraints else 0.0)
        decision.visual_similarity = float(winner["preview_relevance"])
        decision.frame_scores = list(winner.get("frame_scores") or [])
        decision.visual_constraints = constraints.to_dict() if constraints else {}
        decision.frames_sampled = int(len(sampling.frames)) if sampling and sampling.ok else 0
        decision.visual_verification_status = (
            verification.status if verification is not None else "NOT_RUN"
        )
        decision.visual_rejection_reason = ""
        decision.selector_version = VISUAL_SELECTOR_VERSION
        decision.extra["downloaded_asset"] = {
            "path": str(asset.get("path")),
            "type": asset.get("type"),
            "source": asset.get("source"),
        }
        decision.extra["visual_description"] = visual_description
        decision.extra["preview_results"] = [
            result.to_dict() for result in preview_results.values()
        ][: self.semantic_config.budget.provenance_candidate_limit]
        if sampling is not None and not sampling.ok:
            decision.extra["winner_frame_error"] = sampling.error or FRAME_EXTRACTION_FAILED
        decision.extra["visual_summary"] = decision.visual_summary()
        # Fallback is positional among the *downloaded* shortlist, not the global
        # pool: the first shortlisted candidate may have failed to download.
        first_downloaded = downloaded[0]["candidate"].canonical_key
        if candidate.canonical_key != first_downloaded:
            decision.status = SelectionStatus.FALLBACK_SELECTED
            decision.fallback_used = True
            decision.fallback_reason = "visual_rank_preferred_later_downloaded_candidate"
        else:
            decision.status = SelectionStatus.SELECTED
        self._record_visual_rows(decision, downloaded)
        decision.visual_cost["cache"] = self.cache.stats()
        decision.timing_ms = (time.time() - started) * 1000
        decision.visual_timing_ms = decision.timing_ms
        decision.cost = {
            "candidates": decision.candidates_considered,
            "downloads_attempted": len(decision.download_attempts),
            **decision.visual_cost,
        }
        # Clean frame temp dirs only after the decision is fully recorded.
        for row in downloaded:
            sampling = row.get("frame_sampling")
            if sampling is not None:
                try:
                    sampling.cleanup()
                except Exception:
                    pass
        return decision

    # ── text-only fallback (still selects/downloads like P01) ──
    def _download_textual_only(
        self,
        decision: SelectionDecision,
        download: Callable[[Candidate], dict | None],
        *,
        pre_download_filter: Callable[[Candidate], bool] | None,
        started: float,
    ) -> SelectionDecision:
        approved = list(getattr(decision, "approved", []) or [])
        if not approved:
            decision.timing_ms = (time.time() - started) * 1000
            return decision
        limit = self.semantic_config.budget.top_n_download
        for index, candidate in enumerate(approved[:limit]):
            if pre_download_filter is not None:
                try:
                    if not bool(pre_download_filter(candidate)):
                        decision.download_attempts.append({
                            "provider": candidate.provider,
                            "url": candidate.download_url or candidate.asset_url,
                            "query": candidate.query,
                            "result": "skipped_duplicate",
                        })
                        continue
                except Exception:
                    pass
            decision.download_attempts.append({
                "provider": candidate.provider,
                "url": candidate.download_url or candidate.asset_url,
                "query": candidate.query,
                "rank": index,
                "result": "attempted",
            })
            try:
                asset = download(candidate)
            except Exception as exc:  # noqa: BLE001
                asset = None
                decision.download_attempts[-1]["error"] = str(exc)[:200]
            if asset and asset.get("path"):
                decision.download_attempts[-1]["result"] = "downloaded"
                decision.winner = candidate
                decision.text_score = float(
                    (self._match_score_row(decision, candidate) or {}).get("final")
                    or decision.winner_score or 0.0
                )
                decision.winner_score = decision.text_score
                decision.combined_score = decision.text_score
                decision.visual_verification_status = "NOT_RUN"
                if index == 0:
                    decision.status = SelectionStatus.SELECTED
                else:
                    decision.status = SelectionStatus.FALLBACK_SELECTED
                    decision.fallback_used = True
                    decision.fallback_reason = "text_only_fallback_download"
                decision.extra["downloaded_asset"] = {
                    "path": str(asset.get("path")),
                    "type": asset.get("type"),
                    "source": asset.get("source"),
                }
                break
            decision.download_attempts[-1]["result"] = "failed"
        else:
            decision.winner = None
            decision.winner_score = 0.0
            decision.status = SelectionStatus.DOWNLOAD_FAILED
            decision.rejection_reason = "all_approved_downloads_failed"
        decision.visual_cost["cache"] = self.cache.stats()
        decision.cost = {
            "candidates": decision.candidates_considered,
            "downloads_attempted": len(decision.download_attempts),
            **decision.visual_cost,
        }
        decision.timing_ms = (time.time() - started) * 1000
        decision.visual_timing_ms = decision.timing_ms
        decision.extra["visual_summary"] = decision.visual_summary()
        return decision

    def _record_visual_rows(
        self, decision: SelectionDecision, rows: list[dict[str, Any]]
    ) -> None:
        """Attach compact per-candidate visual components to provenance rows."""
        by_key = {row["candidate"].canonical_key: row for row in rows}
        for score_row in decision.candidate_scores:
            candidate = next(
                (
                    row["candidate"] for row in rows
                    if (
                        row["candidate"].download_url or row["candidate"].asset_url
                    ) == score_row.get("url")
                ),
                None,
            )
            if candidate is None:
                continue
            visual = by_key.get(candidate.canonical_key)
            if visual is None:
                continue
            verification = visual.get("verification")
            score_row.update({
                "visual_preview_relevance": round(float(visual["preview_relevance"]), 6),
                "frame_relevance": round(float(visual["frame_relevance"]), 6),
                "vlm_relevance": round(float(visual["vlm_relevance"]), 6),
                "technical_quality": round(float(visual["technical_quality"]), 6),
                "combined_score": round(float(visual["combined"]["combined_score"]), 6),
                "visual_rejection_reason": (
                    visual["constraints"].rejection_reason if visual.get("constraints") else ""
                ),
                "visual_verification_status": verification.status if verification else "NOT_RUN",
                "frames_sampled": len(visual.get("frame_scores") or []),
                "frame_error": visual.get("frame_error", ""),
                "frame_aggregate": visual.get("frame_aggregate") or {},
                "degradation_level": decision.degradation_level,
                "visual_backend": decision.visual_backend,
                "visual_model": decision.visual_model,
            })
