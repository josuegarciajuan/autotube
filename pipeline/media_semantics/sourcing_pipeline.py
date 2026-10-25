"""P04 — source expansion, fallback ladder and global scene selection.

This is the integration point for P04.  It is **opt-in** (``P04_SOURCE_ROUTER``)
and never changes legacy, semantic-text or P02-visual selection when disabled.

Flow for one scene:

1. :class:`SourceRouter` decides the scene category and a category-specific
   ladder (archives for history, NASA for space, stock for people/city, etc.).
2. Providers are queried **stage by stage** with hard budgets.  Retrieval stops
   early when a stage yields a strong candidate; weak stages are compared
   globally with the next stage (so a very relevant image can beat a weak
   video).
3. Candidates are ranked with the unchanged P01 text ranker plus small
   type/quality/coherence signals.  There is no blanket video preference.
4. Real duplicates are vetoed (canonical, URL, dHash/pHash).
5. If no real candidate qualifies, the AI image fallback runs only when the
   route policy allows it.  Conceptual and generic fallbacks are explicitly
   labelled and never scored as literal matches.
6. Every decision keeps providers considered/queried/skipped, asset type,
   fallback type and unified provenance.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

from .ai_policy import (
    AIGenerationRequest,
    ai_generation_allowed,
    build_ai_request,
    build_conceptual_query,
    should_attempt_ai,
)
from .asset_strategy import (
    AssetStrategy,
    build_asset_strategy,
    conceptual_fallback_asset_type,
    infer_asset_type,
    media_type_for_asset_type,
)
from .attribution import provenance_for_candidate
from .coherence import CoherenceConfig, SelectionCoherence
from .config import SemanticConfig
from .contracts import (
    Candidate,
    RejectedCandidate,
    SceneSpec,
    SelectionDecision,
    SelectionStatus,
)
from .dedup import PerceptualDuplicateIndex, image_hash
from .image_motion import plan_image_motion
from .ranking import SemanticTextRanker
from .source_router import (
    AIPolicy,
    AI_PROVIDERS,
    AssetType,
    MEDIA_TYPE_BY_ASSET,
    SourceRoute,
    SourceRouter,
)

SOURCING_VERSION = "p04-source-fallback-1"

# Retrieval adapters -----------------------------------------------------------
# ``provider_search(provider, query, asset_type, limit) -> list[Candidate]``
ProviderSearch = Callable[[str, str, str, int], list[Candidate]]
DownloadFn = Callable[[Candidate], dict | None]
AIGenerator = Callable[[AIGenerationRequest, SceneSpec], dict | None]
PreviewHasher = Callable[[Candidate], str]


def p04_enabled_from_env(env: dict[str, str] | None = None) -> bool:
    """Single source of truth for the opt-in flag (default OFF)."""
    import os

    env = os.environ if env is None else env
    return str(env.get("P04_SOURCE_ROUTER", "0")).strip().lower() in (
        "1", "true", "yes", "on",
    )


@dataclass
class SourcingConfig:
    """Budgets and thresholds for the P04 pipeline.  Conservative defaults."""

    min_relevance_score: float = 0.18
    strong_relevance_score: float = 0.30
    conceptual_min_score: float = 0.10
    archive_min_score: float = 0.15
    scientific_min_score: float = 0.15
    max_queries_per_stage: int = 3
    max_candidates_per_stage: int = 18
    max_candidates_total: int = 90
    max_pages_per_stage: int = 1
    max_downloads_per_scene: int = 3
    ai_enabled: bool = True
    ai_max_per_video: int = 0  # 0 → no real generation unless explicitly raised
    ai_backend: str = "pollinations"
    allow_conceptual: bool = True
    allow_generic: bool = True
    phash_threshold: int = 6
    dhash_threshold: int = 8
    type_prior_step: float = 0.006
    type_prior_max: float = 0.05
    quality_bonus: float = 0.015
    conceptual_score_penalty: float = 0.12
    generic_score_penalty: float = 0.18

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "SourcingConfig":
        import os

        env = os.environ if env is None else env

        def _f(name: str, default: float) -> float:
            try:
                return float(env.get(name, default))
            except (TypeError, ValueError):
                return default

        def _i(name: str, default: int) -> int:
            try:
                return int(env.get(name, default))
            except (TypeError, ValueError):
                return default

        def _b(name: str, default: bool) -> bool:
            raw = env.get(name)
            if raw is None:
                return default
            return str(raw).strip().lower() in ("1", "true", "yes", "on")

        return cls(
            min_relevance_score=max(0.0, min(1.0, _f("P04_MIN_RELEVANCE_SCORE", 0.18))),
            strong_relevance_score=max(0.0, min(1.0, _f("P04_STRONG_RELEVANCE_SCORE", 0.30))),
            ai_enabled=_b("P04_AI_ENABLED", True),
            ai_max_per_video=max(0, _i("P04_AI_MAX_PER_VIDEO", 0)),
            ai_backend=str(env.get("P04_AI_BACKEND", "pollinations")),
            allow_conceptual=_b("P04_ALLOW_CONCEPTUAL", True),
            allow_generic=_b("P04_ALLOW_GENERIC", True),
        )


@dataclass
class SourcingResult:
    decision: SelectionDecision
    route: SourceRoute
    strategy: AssetStrategy
    asset: dict | None = None
    queries: list[str] = field(default_factory=list)
    routing_budget: dict[str, Any] = field(default_factory=dict)
    assets_considered: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "decision": self.decision.to_dict(),
            "route": self.route.to_dict(),
            "strategy": self.strategy.to_dict(),
            "asset": dict(self.asset or {}),
            "queries": list(self.queries),
            "routing_budget": dict(self.routing_budget),
        }


def _stage_queries(spec: SceneSpec, asset_type: str) -> list[str]:
    base = [
        " ".join(x for x in (spec.subject, spec.action, spec.environment) if x),
        " ".join(x for x in (spec.subject, spec.environment) if x),
        " ".join(x for x in (spec.subject, spec.era) if x),
        spec.subject or spec.visual_concept or spec.narration_fragment[:80],
    ]
    if asset_type in (AssetType.ARCHIVAL_IMAGE.value, AssetType.ARCHIVAL_VIDEO.value):
        # Archive metadata often uses place/era names, not modern b-roll terms.
        base = [
            " ".join(x for x in (spec.subject, spec.era) if x),
            " ".join(x for x in (spec.subject, spec.environment) if x),
            spec.subject,
        ]
    if asset_type == AssetType.SCIENTIFIC_IMAGE.value:
        base = [
            f"{spec.subject} {spec.environment}".strip(),
            " ".join(x for x in (spec.subject, spec.visual_concept) if x),
            spec.subject,
        ]
    out: list[str] = []
    seen: set[str] = set()
    for query in base:
        query = " ".join(str(query or "").split()).strip(" ,.;")
        if not query or query.lower() in seen:
            continue
        seen.add(query.lower())
        out.append(query[:120])
    return out


def _quality_bonus(candidate: Candidate, media_type: str) -> float:
    try:
        width = int(candidate.width or 0)
        height = int(candidate.height or 0)
    except (TypeError, ValueError):
        return 0.0
    if not width or not height:
        return 0.0
    short = min(width, height)
    if short >= 1080:
        return 1.0
    if short >= 720:
        return 0.6
    if short >= 480:
        return 0.25
    return 0.0


def _category_prior(route: SourceRoute, asset_type: str) -> float:
    """Small, explainable preference for the category's native source."""
    if route.historical_priority:
        if asset_type in (
            AssetType.ARCHIVAL_IMAGE.value, AssetType.ARCHIVAL_VIDEO.value,
        ):
            return 0.09
        if asset_type in (AssetType.STOCK_VIDEO.value, AssetType.STOCK_IMAGE.value):
            return -0.04
    if route.scene_type in ("SPACE", "SCIENCE"):
        if asset_type in (
            AssetType.SCIENTIFIC_IMAGE.value, AssetType.ARCHIVAL_IMAGE.value,
        ):
            return 0.08
    if route.scene_type == "MEDICAL" and asset_type == AssetType.SCIENTIFIC_IMAGE.value:
        return 0.05
    return 0.0


def _threshold_for(asset_type: str, config: SourcingConfig) -> float:
    if asset_type in (AssetType.ARCHIVAL_IMAGE.value, AssetType.ARCHIVAL_VIDEO.value):
        return min(config.min_relevance_score, config.archive_min_score)
    if asset_type == AssetType.SCIENTIFIC_IMAGE.value:
        return min(config.min_relevance_score, config.scientific_min_score)
    return config.min_relevance_score


class SourcedMediaSelector:
    """Provider-agnostic P04 selection over injected retrieval/download fns."""

    def __init__(
        self,
        *,
        config: SourcingConfig | None = None,
        semantic_config: SemanticConfig | None = None,
        router: SourceRouter | None = None,
        provider_search: ProviderSearch | None = None,
        download_fn: DownloadFn | None = None,
        ai_generator: AIGenerator | None = None,
        preview_hasher: PreviewHasher | None = None,
        available_providers: Iterable[str] | None = None,
        available_ai_backends: Iterable[str] | None = None,
        ranker: SemanticTextRanker | None = None,
    ) -> None:
        self.config = config or SourcingConfig()
        self.semantic_config = semantic_config or SemanticConfig()
        self.router = router or SourceRouter(
            generation_allowed=self.config.ai_enabled and self.config.ai_max_per_video > 0,
        )
        self.provider_search = provider_search
        self.download_fn = download_fn
        self.ai_generator = ai_generator
        self.preview_hasher = preview_hasher
        self.available_providers = (
            set(available_providers) if available_providers is not None else None
        )
        self.available_ai_backends = (
            set(available_ai_backends) if available_ai_backends is not None else None
        )
        self.ranker = ranker or SemanticTextRanker()
        self.duplicates = PerceptualDuplicateIndex(
            phash_threshold=self.config.phash_threshold,
            dhash_threshold=self.config.dhash_threshold,
        )
        self.coherence = SelectionCoherence(CoherenceConfig())
        self.ai_used = 0

    # ── public API ────────────────────────────────────────────
    def select(
        self,
        spec: SceneSpec,
        *,
        scene_idx: int | None = None,
        allow_video: bool = True,
    ) -> SourcingResult:
        started = time.time()
        route = self.router.route(spec)
        strategy = build_asset_strategy(spec, route)
        queries: list[str] = []
        queried: list[str] = []
        skipped: dict[str, str] = dict(route.providers_skipped)
        considered = list(route.providers_considered)
        accumulated: list[tuple[Candidate, dict[str, Any]]] = []
        per_provider: dict[str, int] = {}
        pages = 0
        requests = 0
        strong_found = False

        decision = SelectionDecision(
            scene_spec=spec,
            selector_version=SOURCING_VERSION,
            threshold=self.config.min_relevance_score,
            scene_category=route.scene_type,
            source_route=route.to_dict(),
            ai_policy=route.ai_policy,
        )

        for level_index, asset_type in enumerate(strategy.fallback_order):
            if not allow_video and asset_type in (
                AssetType.STOCK_VIDEO.value, AssetType.ARCHIVAL_VIDEO.value,
            ):
                skipped[asset_type] = "video_disallowed_for_scene"
                continue
            if asset_type in (
                AssetType.AI_IMAGE.value,
                AssetType.CONCEPTUAL_STOCK.value,
                AssetType.GENERIC_FALLBACK.value,
            ):
                # AI/conceptual/generic are handled explicitly AFTER the real
                # literal ladder, so they can never be scored as a literal win.
                skipped[asset_type] = "deferred_to_explicit_fallback"
                continue
            if not strategy.allows(asset_type):
                skipped[asset_type] = "forbidden_by_strategy"
                continue
            if asset_type == AssetType.CONCEPTUAL_STOCK.value and not self.config.allow_conceptual:
                skipped[asset_type] = "conceptual_disabled"
                continue
            if asset_type == AssetType.GENERIC_FALLBACK.value and not self.config.allow_generic:
                skipped[asset_type] = "generic_disabled"
                continue
            providers = self._providers_for(asset_type)
            if not providers:
                skipped[asset_type] = "no_provider_for_asset_type"
                continue
            stage_added = 0
            for provider in providers:
                if self.available_providers is not None and provider not in self.available_providers:
                    skipped[provider] = "provider_unavailable"
                    continue
                if self.provider_search is None:
                    skipped[provider] = "retrieval_not_configured"
                    continue
                if provider not in queried:
                    queried.append(provider)
                for query in _stage_queries(spec, asset_type)[: self.config.max_queries_per_stage]:
                    if query not in queries:
                        queries.append(query)
                    if len(accumulated) >= self.config.max_candidates_total:
                        break
                    try:
                        raw_candidates = self.provider_search(
                            provider, query, asset_type,
                            self.config.max_candidates_per_stage,
                        ) or []
                    except Exception:
                        skipped[provider] = "provider_error"
                        raw_candidates = []
                    requests += 1
                    stage_added += len(raw_candidates)
                    for candidate in raw_candidates:
                        if not isinstance(candidate, Candidate):
                            continue
                        if not candidate.query:
                            candidate.query = query
                        candidate.asset_type = candidate.asset_type or infer_asset_type(
                            candidate, route,
                        )
                        if len(accumulated) >= self.config.max_candidates_total:
                            continue
                        per_provider[candidate.provider] = (
                            per_provider.get(candidate.provider, 0) + 1
                        )
                        accumulated.append((candidate, {"level": level_index, "asset_type": asset_type}))
            pages += 1
            if stage_added:
                # Re-rank the accumulated pool; stop early only on a strong
                # candidate, otherwise compare with the next stage.
                _, best, _, _ = self._rank_pool(spec, route, accumulated)
                if best and best["final"] >= self.config.strong_relevance_score:
                    strong_found = True
                    break
            # NASA/archive budgets are tiny; a missing stage is cheap.
        ranked = self._rank_pool(spec, route, accumulated)
        decision.candidates_considered = len(accumulated)
        decision.candidate_scores = ranked[0][: self.semantic_config.budget.provenance_candidate_limit]
        decision.rejected = ranked[3][: self.semantic_config.budget.provenance_rejected_limit]
        decision.queries = queries

        winner: Candidate | None = None
        winner_row: dict[str, Any] | None = None
        downloaded: dict | None = None
        attempts = 0
        for candidate, row in ranked[2]:
            if attempts >= self.config.max_downloads_per_scene:
                break
            attempts += 1
            asset = self._download(candidate)
            if asset is None:
                decision.download_attempts.append({
                    "provider": candidate.provider,
                    "url": candidate.download_url or candidate.asset_url,
                    "result": "failed",
                })
                continue
            duplicate = self._duplicate_check(candidate, asset)
            if duplicate:
                decision.duplicate_rejections += 1
                decision.download_attempts.append({
                    "provider": candidate.provider,
                    "url": candidate.download_url or candidate.asset_url,
                    "result": "skipped_duplicate",
                    "duplicate": duplicate.to_dict(),
                })
                continue
            winner, winner_row, downloaded = candidate, row, asset
            decision.download_attempts.append({
                "provider": candidate.provider,
                "url": candidate.download_url or candidate.asset_url,
                "result": "downloaded" if asset.get("path") else "selected",
            })
            break

        # If no downloader is configured the metadata winner is still selected;
        # this keeps offline benchmarks and shadow scoring pure-metadata.
        if winner is None and self.download_fn is None:
            approved = [item for item in ranked[2]]
            if approved:
                candidate, row = approved[0]
                dup = self._duplicate_check(candidate, None)
                if not dup:
                    winner, winner_row = candidate, row
                    downloaded = {
                        "path": None, "type": media_type_for_asset_type(row["asset_type"], candidate),
                        "source": candidate.provider,
                    }

        if winner is not None:
            self._finalize_selected(
                decision, spec, route, strategy, winner, winner_row, downloaded,
            )
            self._record(winner, spec, winner_row["asset_type"], decision.asset_type)
        else:
            ai_result = self._try_ai(spec, route, strategy, decision)
            if ai_result is not None:
                winner, downloaded, winner_row = ai_result
                self._finalize_selected(
                    decision, spec, route, strategy, winner, winner_row, downloaded,
                    fallback_type="AI_IMAGE",
                )
                self._record(winner, spec, AssetType.AI_IMAGE.value, "image")
            else:
                conceptual = self._try_conceptual(spec, route, strategy, decision)
                if conceptual is not None:
                    winner, downloaded, winner_row = conceptual
                    self._finalize_selected(
                        decision, spec, route, strategy, winner, winner_row, downloaded,
                        fallback_type="CONCEPTUAL", literal=False,
                    )
                    self._record(winner, spec, winner_row["asset_type"], decision.asset_type)
                else:
                    generic = self._try_generic(spec, route, strategy, decision)
                    if generic is not None:
                        winner, downloaded, winner_row = generic
                        self._finalize_selected(
                            decision, spec, route, strategy, winner, winner_row, downloaded,
                            fallback_type="GENERIC", literal=False,
                        )
                        self._record(winner, spec, winner_row["asset_type"], decision.asset_type)
                    else:
                        decision.status = SelectionStatus.NO_GOOD_CANDIDATE
                        decision.rejection_reason = "no_candidate_above_threshold_after_ladder"

        decision.timing_ms = (time.time() - started) * 1000
        decision.routing_budget = {
            "providers_considered": considered,
            "providers_queried": queried,
            "providers_skipped": skipped,
            "queries": len(queries),
            "requests": requests,
            "stages": pages,
            "candidates": len(accumulated),
            "strong_candidate_short_circuit": strong_found,
            "duplicate_rejections": decision.duplicate_rejections,
            "ai_used": self.ai_used,
        }
        decision.cost = {
            "candidates": len(accumulated),
            "requests": requests,
            "downloads_attempted": len(decision.download_attempts),
        }
        return SourcingResult(
            decision=decision,
            route=route,
            strategy=strategy,
            asset=downloaded,
            queries=queries,
            routing_budget=dict(decision.routing_budget),
        )

    def reset(self) -> None:
        self.duplicates = PerceptualDuplicateIndex(
            phash_threshold=self.config.phash_threshold,
            dhash_threshold=self.config.dhash_threshold,
        )
        self.coherence.reset()
        self.ai_used = 0

    # ── internal ──────────────────────────────────────────────
    def _providers_for(self, asset_type: str) -> list[str]:
        from .source_router import _PROVIDERS_BY_ASSET

        return list(_PROVIDERS_BY_ASSET.get(asset_type, ()))

    def _rank_pool(
        self,
        spec: SceneSpec,
        route: SourceRoute,
        pool: list[tuple[Candidate, dict[str, Any]]],
    ) -> tuple[
        list[dict[str, Any]], dict[str, Any] | None,
        list[tuple[Candidate, dict[str, Any]]], list[RejectedCandidate],
    ]:
        rows: list[dict[str, Any]] = []
        rejected: list[RejectedCandidate] = []
        approved: list[tuple[Candidate, dict[str, Any]]] = []
        if not pool:
            return rows, None, approved, rejected
        candidates = [c for c, _ in pool]
        breakdowns = self.ranker.score_many(spec, candidates)
        level_of = {id(c): meta for c, meta in pool}
        scored: list[tuple[float, Candidate, dict[str, Any]]] = []
        for candidate, breakdown in zip(candidates, breakdowns):
            meta = level_of[id(candidate)]
            asset_type = str(meta["asset_type"])
            level = int(meta["level"])
            media_type = media_type_for_asset_type(asset_type, candidate)
            type_prior = max(
                0.0,
                self.config.type_prior_max - level * self.config.type_prior_step,
            )
            quality = self.config.quality_bonus * _quality_bonus(candidate, media_type)
            category_prior = _category_prior(route, asset_type)
            signals = self.coherence.evaluate(
                candidate, spec=spec, asset_type=asset_type, media_type=media_type,
            )
            final = max(
                0.0, min(1.0, breakdown.final + type_prior + quality
                        - signals.total_penalty + signals.total_bonus),
            )
            row = {
                "provider": candidate.provider,
                "provider_asset_id": candidate.provider_asset_id,
                "query": candidate.query,
                "asset_type": asset_type,
                "media_type": media_type,
                "level": level,
                "url": candidate.download_url or candidate.asset_url,
                "text_score": round(float(breakdown.final), 6),
                "type_prior": round(type_prior, 6),
                "quality_bonus": round(quality, 6),
                "category_prior": round(category_prior, 6),
                "coherence_penalty": signals.total_penalty,
                "coherence_bonus": signals.total_bonus,
                "coherence": signals.to_dict(),
                "final": round(final, 6),
                **breakdown.to_dict(),
            }
            scored.append((final, candidate, {**meta, **row, "media_type": media_type}))
            rows.append(row)
            if breakdown.metadata_status == "NO_METADATA":
                rejected.append(RejectedCandidate(candidate=candidate, score=final, reason="no_metadata"))
                continue
            if breakdown.negative_hits:
                rejected.append(RejectedCandidate(candidate=candidate, score=final, reason="negative_constraint"))
                continue
            threshold = _threshold_for(asset_type, self.config)
            if final < threshold:
                rejected.append(RejectedCandidate(candidate=candidate, score=final, reason="below_threshold"))
                continue
            approved.append((candidate, {**meta, **row, "media_type": media_type}))
        approved.sort(key=lambda item: (-item[1]["final"], item[0].canonical_key))
        scored.sort(key=lambda item: (-item[0], item[1].canonical_key))
        best = scored[0][2] if scored else None
        return rows, best, approved, rejected

    def _duplicate_check(self, candidate: Candidate, asset: dict | None):
        hash_value = candidate.perceptual_hash
        path = (asset or {}).get("path") if asset else None
        if not hash_value and path:
            hash_value = image_hash(path)
            candidate.perceptual_hash = hash_value
        if not hash_value and self.preview_hasher is not None:
            try:
                hash_value = self.preview_hasher(candidate) or ""
            except Exception:
                hash_value = ""
        match = self.duplicates.check(
            key=candidate.canonical_key,
            urls=[candidate.download_url, candidate.asset_url, candidate.page_url],
            hash_value=hash_value,
        )
        if not match.is_duplicate:
            self.duplicates.add(
                key=candidate.canonical_key,
                urls=[candidate.download_url, candidate.asset_url, candidate.page_url],
                hash_value=hash_value,
            )
        return None if not match.is_duplicate else match

    def _download(self, candidate: Candidate) -> dict | None:
        if self.download_fn is None:
            return {"path": None, "type": media_type_for_asset_type(candidate.asset_type, candidate),
                    "source": candidate.provider}
        try:
            asset = self.download_fn(candidate)
        except Exception:
            return None
        if not asset:
            return None
        asset.setdefault("type", media_type_for_asset_type(candidate.asset_type, candidate))
        asset.setdefault("source", candidate.provider)
        return asset

    def _finalize_selected(
        self,
        decision: SelectionDecision,
        spec: SceneSpec,
        route: SourceRoute,
        strategy: AssetStrategy,
        winner: Candidate,
        row: dict[str, Any],
        asset: dict | None,
        *,
        fallback_type: str = "",
        literal: bool = True,
    ) -> None:
        asset_type = str(row.get("asset_type") or infer_asset_type(winner, route))
        media_type = media_type_for_asset_type(asset_type, winner)
        decision.status = (
            SelectionStatus.SELECTED if not fallback_type
            else SelectionStatus.FALLBACK_SELECTED
        )
        decision.winner = winner
        decision.winner_score = float(row.get("final") or 0.0)
        decision.asset_type = asset_type
        decision.fallback_type = fallback_type
        decision.fallback_used = bool(fallback_type)
        decision.fallback_reason = fallback_type.lower() if fallback_type else ""
        decision.literal_match = literal
        decision.conceptual_used = fallback_type == "CONCEPTUAL"
        decision.provenance = provenance_for_candidate(winner)
        decision.provenance["asset_type"] = asset_type
        decision.provenance["fallback_type"] = fallback_type
        if asset and asset.get("path") and not decision.provenance.get("media_url"):
            decision.provenance["media_url"] = str(asset.get("path"))
        decision.coherence_signals = dict(row.get("coherence") or {})
        if asset is not None:
            asset["asset_type"] = asset_type
            asset["scene_category"] = route.scene_type
            asset["fallback_type"] = fallback_type
            asset["selection_score"] = decision.winner_score
            asset["selector"] = "p04_source_router"
            asset["selector_version"] = SOURCING_VERSION
            asset["scene_spec"] = spec.to_dict()
            asset["route"] = route.to_dict()
            asset["strategy"] = strategy.to_dict()
            asset["provenance"] = dict(decision.provenance)
            asset["candidate"] = winner.to_dict()
            if media_type == "image":
                asset["motion_plan"] = plan_image_motion(
                    spec, scene_idx=spec.scene_idx, duration=spec.duration,
                ).to_dict()
            else:
                asset["motion_plan"] = None
            asset.setdefault("type", media_type)
            asset.setdefault("source", winner.provider)
            if asset_type in (AssetType.AI_IMAGE.value,):
                asset.setdefault("ai_generated", True)

    def _record(self, candidate: Candidate, spec: SceneSpec, asset_type: str, media_type: str) -> None:
        self.coherence.record(
            candidate, spec=spec, asset_type=asset_type, media_type=media_type,
        )

    def _retrieve_first(
        self,
        spec: SceneSpec,
        route: SourceRoute,
        asset_type: str,
        *,
        queries: list[str] | None = None,
        limit_stages: int = 1,
    ) -> tuple[list[tuple[Candidate, dict[str, Any]]], list[str], list[str]]:
        providers = self._providers_for(asset_type)
        pool: list[tuple[Candidate, dict[str, Any]]] = []
        queried: list[str] = []
        queries_used: list[str] = []
        for provider in providers[:1]:
            if self.available_providers is not None and provider not in self.available_providers:
                continue
            if self.provider_search is None:
                continue
            if provider not in queried:
                queried.append(provider)
            for query in (queries or _stage_queries(spec, asset_type))[: self.config.max_queries_per_stage]:
                queries_used.append(query)
                try:
                    found = self.provider_search(
                        provider, query, asset_type, self.config.max_candidates_per_stage,
                    ) or []
                except Exception:
                    found = []
                for candidate in found:
                    if not isinstance(candidate, Candidate):
                        continue
                    candidate.query = candidate.query or query
                    pool.append((candidate, {"level": 0, "asset_type": asset_type}))
        return pool, queried, queries_used

    def _try_ai(
        self, spec: SceneSpec, route: SourceRoute, strategy: AssetStrategy,
        decision: SelectionDecision,
    ) -> tuple[Candidate, dict, dict] | None:
        ok, reason = should_attempt_ai(route, spec, no_good_real_candidate=True)
        if not ok or not self.config.ai_enabled:
            decision.extra["ai_skipped"] = reason
            return None
        if self.ai_used >= self.config.ai_max_per_video:
            decision.extra["ai_skipped"] = "ai_budget_exhausted"
            return None
        if self.ai_generator is None:
            decision.extra["ai_skipped"] = "ai_backend_not_configured"
            return None
        backends = [self.config.ai_backend]
        if self.available_ai_backends:
            backends = [b for b in backends if b in self.available_ai_backends]
        if not backends:
            decision.extra["ai_skipped"] = "ai_backend_unavailable"
            return None
        request = build_ai_request(
            spec, route, backends=backends,
            visual_bible=(spec.extra or {}).get("visual_bible") or None,
            style_suffix=str((spec.extra or {}).get("visual_style") or ""),
        )
        try:
            asset = self.ai_generator(request, spec)
        except Exception:
            asset = None
        if not asset or not asset.get("path"):
            decision.extra["ai_skipped"] = "ai_generation_failed"
            return None
        asset["ai_generated"] = True
        asset["generation_backend"] = backends[0]
        asset["generation_prompt_hash"] = request.prompt_hash
        asset["asset_type"] = AssetType.AI_IMAGE.value
        asset["source"] = f"ai_{backends[0]}"
        asset["type"] = "image"
        asset["license"] = "AI_GENERATED"
        asset["creator"] = f"AI ({backends[0]})"
        candidate = Candidate(
            provider=f"ai_{backends[0]}",
            provider_asset_id=request.prompt_hash,
            asset_url=str(asset["path"]),
            download_url=str(asset["path"]),
            page_url="",
            title=spec.subject or spec.visual_concept,
            description=request.prompt,
            tags=[t for t in (spec.subject, spec.action, spec.environment) if t],
            license="AI_GENERATED",
            provenance="AI_GENERATED",
            media_type="image",
            asset_type=AssetType.AI_IMAGE.value,
            ai_generated=True,
            generation_backend=backends[0],
            generation_prompt_hash=request.prompt_hash,
            creator=f"AI ({backends[0]})",
            public_domain=False,
            attribution_required=False,
            metadata_status="MATCH",
            metadata_completeness=0.6,
            searchable_text=" ".join([spec.subject, spec.action, spec.environment, request.prompt]).lower(),
        )
        self.ai_used += 1
        self.duplicates.add(
            key=candidate.canonical_key, urls=[str(asset["path"])],
            hash_value=image_hash(asset["path"]),
        )
        row = {
            "asset_type": AssetType.AI_IMAGE.value,
            "final": max(0.0, self.config.min_relevance_score),
            "media_type": "image",
            "coherence": {},
        }
        decision.extra["ai_request"] = request.to_dict()
        return candidate, asset, row

    def _try_conceptual(
        self, spec: SceneSpec, route: SourceRoute, strategy: AssetStrategy,
        decision: SelectionDecision,
    ) -> tuple[Candidate, dict, dict] | None:
        if not self.config.allow_conceptual:
            return None
        pool, queried, queries = self._retrieve_first(
            spec, route, AssetType.CONCEPTUAL_STOCK.value,
            queries=[build_conceptual_query(spec)],
        )
        if not pool:
            return None
        rows, best, approved, rejected = self._rank_pool(spec, route, pool)
        if not approved:
            return None
        candidate, row = approved[0]
        adjusted = max(0.0, float(row["final"]) - self.config.conceptual_score_penalty)
        if adjusted < self.config.conceptual_min_score:
            return None
        row = dict(row)
        row["final"] = adjusted
        row["asset_type"] = AssetType.CONCEPTUAL_STOCK.value
        asset = self._download(candidate)
        if asset is None:
            return None
        dup = self._duplicate_check(candidate, asset)
        if dup:
            decision.duplicate_rejections += 1
            return None
        decision.queries.extend(q for q in queries if q not in decision.queries)
        if queried:
            decision.routing_budget["conceptual_providers_queried"] = queried
        return candidate, asset, row

    def _try_generic(
        self, spec: SceneSpec, route: SourceRoute, strategy: AssetStrategy,
        decision: SelectionDecision,
    ) -> tuple[Candidate, dict, dict] | None:
        if not self.config.allow_generic:
            return None
        query = f"{route.scene_type.lower()} atmospheric documentary b-roll"
        pool, queried, queries = self._retrieve_first(
            spec, route, AssetType.STOCK_IMAGE.value, queries=[query],
        )
        if not pool:
            return None
        rows, best, approved, rejected = self._rank_pool(spec, route, pool)
        if not approved:
            # Generic fallback intentionally accepts the provider's best even
            # with weak metadata, but labels the confidence accordingly.
            approved = sorted(
                [(c, m) for c, m in pool],
                key=lambda item: (item[0].provider_rank, item[0].canonical_key),
            )
        if not approved:
            return None
        candidate, row = approved[0]
        row = dict(row)
        row["final"] = max(
            0.0, float(row.get("final") or 0.0) - self.config.generic_score_penalty,
        )
        row["asset_type"] = AssetType.GENERIC_FALLBACK.value
        asset = self._download(candidate)
        if asset is None:
            return None
        dup = self._duplicate_check(candidate, asset)
        if dup:
            decision.duplicate_rejections += 1
            return None
        decision.queries.extend(q for q in queries if q not in decision.queries)
        return candidate, asset, row
