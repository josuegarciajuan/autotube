"""P01-G/H/I — global top-K, threshold and download-after-ranking.

The selector never knows which provider or page a candidate came from when
deciding the winner: every candidate is ranked together.  Provider/page is
retained only for explanation (provenance).

Download flow (P01-I)::

    rank metadata → global top-K → threshold → download top-N in order
    → validate → winner / fallback winner / NO_GOOD_CANDIDATE
"""

from __future__ import annotations

import time
from typing import Any, Callable

from .config import SemanticConfig
from .contracts import (
    Candidate,
    MetadataStatus,
    RejectedCandidate,
    SceneSpec,
    SelectionDecision,
    SelectionStatus,
)
from .ranking import ScoreBreakdown, SemanticTextRanker

SELECTOR_VERSION = "p01-semantic-1"


class SemanticMediaSelector:
    """Provider-agnostic selector over a metadata-only candidate set."""

    def __init__(
        self,
        config: SemanticConfig | None = None,
        ranker: SemanticTextRanker | None = None,
    ) -> None:
        self.config = config or SemanticConfig()
        self.ranker = ranker or SemanticTextRanker(weights=self.config.weights)

    # ── ranking only (metadata) ───────────────────────────────
    def rank(
        self,
        scene_spec: SceneSpec,
        candidates: list[Candidate],
        threshold: float | None = None,
        pre_filter: Callable[[Candidate], str | None] | None = None,
    ) -> SelectionDecision:
        started = time.time()
        threshold = (
            self.config.min_relevance_score if threshold is None else float(threshold)
        )
        candidates = list(candidates or [])
        decision = SelectionDecision(
            scene_spec=scene_spec,
            selector_version=SELECTOR_VERSION,
            threshold=threshold,
            candidates_considered=len(candidates),
            providers_consulted=sorted({c.provider for c in candidates}),
            queries=list(dict.fromkeys(c.query for c in candidates if c.query)),
            extra={"config": self.config.describe()},
        )
        if not candidates:
            decision.status = SelectionStatus.RETRIEVAL_FAILED
            decision.rejection_reason = "no_candidates_retrieved"
            decision.timing_ms = (time.time() - started) * 1000
            return decision

        # P01: era/anachronism (and any other hard veto) is applied BEFORE
        # ranking, so a forbidden candidate can never win on text similarity.
        if pre_filter is not None:
            filtered: list[Candidate] = []
            early_rejected: list[RejectedCandidate] = []
            for candidate in candidates:
                try:
                    reason = pre_filter(candidate)
                except Exception:
                    reason = None
                if reason:
                    early_rejected.append(
                        RejectedCandidate(candidate=candidate, score=0.0, reason=str(reason))
                    )
                else:
                    filtered.append(candidate)
            candidates = filtered
            decision.rejected.extend(early_rejected)
        if not candidates:
            decision.status = SelectionStatus.NO_GOOD_CANDIDATE
            decision.rejection_reason = "all_candidates_prefiltered"
            decision.timing_ms = (time.time() - started) * 1000
            return decision

        breakdowns = self.ranker.score_many(scene_spec, candidates)
        pairs = list(zip(candidates, breakdowns))
        # Deterministic global order: score, metadata class, provider prior,
        # provider's own rank, stable key.
        pairs.sort(
            key=lambda pair: (
                -float(pair[1].final),
                -int(pair[1].metadata_status == MetadataStatus.MATCH.value),
                -float(pair[1].provider_prior),
                int(pair[0].provider_rank),
                pair[0].canonical_key,
            )
        )

        approved: list[tuple[Candidate, ScoreBreakdown]] = []
        rejected: list[RejectedCandidate] = []
        score_rows: list[dict[str, Any]] = []
        for position, (candidate, breakdown) in enumerate(pairs):
            row = {
                "position": position,
                "provider": candidate.provider,
                "provider_asset_id": candidate.provider_asset_id,
                "query": candidate.query,
                "query_tier": candidate.query_tier,
                "page": candidate.page,
                "provider_rank": candidate.provider_rank,
                "url": candidate.download_url or candidate.asset_url,
                **breakdown.to_dict(),
            }
            if len(score_rows) < self.config.budget.provenance_candidate_limit:
                score_rows.append(row)

            reason = ""
            if breakdown.metadata_status == MetadataStatus.NO_METADATA.value:
                reason = "no_metadata"
            elif breakdown.negative_hits:
                reason = "negative_constraint"
            elif breakdown.metadata_status == MetadataStatus.NO_MATCH.value:
                reason = "no_match"
            elif breakdown.final < threshold:
                reason = "below_threshold"
            elif len(approved) >= self.config.budget.top_k:
                reason = "outside_top_k"

            if reason:
                rejected.append(
                    RejectedCandidate(candidate=candidate, score=breakdown.final, reason=reason)
                )
                continue
            approved.append((candidate, breakdown))

        decision.candidate_scores = score_rows
        decision.rejected = (
            decision.rejected + rejected
        )[: self.config.budget.provenance_rejected_limit]
        decision.approved = [c for c, _ in approved]  # type: ignore[attr-defined]
        if approved:
            winner, winner_breakdown = approved[0]
            decision.winner = winner
            decision.winner_score = winner_breakdown.final
            decision.status = SelectionStatus.SELECTED
        else:
            decision.status = SelectionStatus.NO_GOOD_CANDIDATE
            decision.rejection_reason = "no_candidate_above_threshold"
        decision.timing_ms = (time.time() - started) * 1000
        return decision

    # ── ranking + download (P01-I) ────────────────────────────
    def select_and_download(
        self,
        scene_spec: SceneSpec,
        candidates: list[Candidate],
        download_fn: Callable[[Candidate], dict | None],
        *,
        threshold: float | None = None,
        pre_filter: Callable[[Candidate], str | None] | None = None,
        pre_download_filter: Callable[[Candidate], bool] | None = None,
    ) -> SelectionDecision:
        """Download only the approved top-N, in rank order.

        ``download_fn`` returns an asset dict (with a truthy ``path``) on
        success or ``None`` on failure.  Failures are recorded and the next
        approved candidate is tried without re-running retrieval.
        """
        started = time.time()
        decision = self.rank(
            scene_spec, candidates, threshold=threshold, pre_filter=pre_filter
        )
        approved: list[Candidate] = list(getattr(decision, "approved", []) or [])

        if not approved:
            decision.timing_ms = (time.time() - started) * 1000
            return decision

        limit = self.config.budget.top_n_download
        success: tuple[Candidate, int] | None = None
        for index, candidate in enumerate(approved[:limit]):
            if pre_download_filter is not None:
                try:
                    allowed = bool(pre_download_filter(candidate))
                except Exception:
                    allowed = True
                if not allowed:
                    decision.download_attempts.append({
                        "provider": candidate.provider,
                        "url": candidate.download_url or candidate.asset_url,
                        "query": candidate.query,
                        "result": "skipped_duplicate",
                    })
                    continue
            decision.download_attempts.append({
                "provider": candidate.provider,
                "url": candidate.download_url or candidate.asset_url,
                "query": candidate.query,
                "rank": index,
                "result": "attempted",
            })
            try:
                asset = download_fn(candidate)
            except Exception as exc:  # fail-open: record and continue
                asset = None
                decision.download_attempts[-1]["error"] = str(exc)[:200]
            if asset and asset.get("path"):
                success = (candidate, index)
                decision.download_attempts[-1]["result"] = "downloaded"
                decision.extra["downloaded_asset"] = {
                    "path": str(asset.get("path")),
                    "type": asset.get("type"),
                    "source": asset.get("source"),
                }
                break
            decision.download_attempts[-1]["result"] = "failed"

        if success is None:
            decision.winner = None
            decision.winner_score = 0.0
            decision.status = SelectionStatus.DOWNLOAD_FAILED
            decision.rejection_reason = "all_approved_downloads_failed"
        else:
            winner, index = success
            decision.winner = winner
            decision.winner_score = next(
                (
                    float(row.get("final") or 0.0)
                    for row in decision.candidate_scores
                    if row.get("url") == (winner.download_url or winner.asset_url)
                ),
                decision.winner_score,
            )
            # Recover the exact score from the ranking pass.
            rescored = self.ranker.score(decision.scene_spec, winner)
            decision.winner_score = rescored.final
            if index == 0:
                decision.status = SelectionStatus.SELECTED
            else:
                decision.status = SelectionStatus.FALLBACK_SELECTED
                decision.fallback_used = True
                decision.fallback_reason = (
                    f"rank_{index + 1}_selected_after_previous_download_failure"
                )
        decision.timing_ms = (time.time() - started) * 1000
        decision.cost = {
            "candidates": decision.candidates_considered,
            "downloads_attempted": len(decision.download_attempts),
        }
        return decision
