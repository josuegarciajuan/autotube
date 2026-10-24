"""P03-F/H/I/L — three-way scoring, auto-evaluation, metrics and calibration.

Arms share the exact same frozen candidate pool:

* A ``LEGACY``      — ``replay.legacy_decision`` (production Fase-4a order).
* B ``SEMANTIC_TEXT`` — P01 ``SemanticMediaSelector.rank``.
* C ``SEMANTIC_VISUAL`` — P02 preview embeddings + optional frame evidence.

No VLM is used inside arm C.  That keeps auto-evaluation independent from the
arm under test.
"""

from __future__ import annotations

import json
import math
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Sequence
from urllib.parse import urlparse

from pipeline.media_semantics.config import (
    RankingWeights,
    SemanticConfig,
    VisualBudget,
    VisualConfig,
    VisualWeights,
)
from pipeline.media_semantics.contracts import (
    Candidate,
    SceneSpec,
    SelectionStatus,
)
from pipeline.media_semantics.ranking import SemanticTextRanker
from pipeline.media_semantics.replay import legacy_decision
from pipeline.media_semantics.selector import SemanticMediaSelector
from pipeline.media_semantics.visual_description import build_visual_description
from pipeline.media_semantics.visual_frames import sample_semantic_frames
from pipeline.media_semantics.visual_scoring import (
    aggregate_frame_similarities,
    compute_combined_score,
    cosine_similarity,
    evaluate_visual_constraints,
    technical_quality_score,
)
from pipeline.media_semantics.visual_vlm import (
    OpenAICompatibleVLMBackend,
    VisualVerificationResult,
    verify_with_backend,
)

ARMS = ("legacy", "semantic_text", "semantic_visual")

DEFAULT_WEIGHTS = {
    "semantic": 0.35,
    "lexical": 0.15,
    "action": 0.18,
    "subject": 0.12,
    "environment": 0.08,
    "era": 0.05,
    "metadata_quality": 0.04,
    "provider_prior": 0.03,
    "negative_constraint": 0.50,
    "no_metadata_ceiling": 0.12,
}

DEFAULT_VISUAL_WEIGHTS = {
    "text": 0.50,
    "preview": 0.15,
    "frame": 0.20,
    "vlm": 0.15,
    "technical_quality": 0.05,
    "negative_penalty": 0.60,
    "era_penalty": 0.30,
    "must_include_penalty": 0.25,
}


def build_semantic_config(overrides: dict[str, Any] | None = None) -> SemanticConfig:
    overrides = dict(overrides or {})
    config = SemanticConfig(mode="semantic", shadow=False)
    if "p01_min_relevance_score" in overrides:
        config.min_relevance_score = float(overrides["p01_min_relevance_score"])
    if overrides.get("ranking_weights"):
        config.weights = RankingWeights.from_mapping(overrides["ranking_weights"])
    visual = config.visual
    visual.mode = "full_visual"
    visual.backend = "clip"
    visual.model = str(overrides.get("visual_model") or visual.model)
    visual.model_version = str(overrides.get("visual_model_version") or visual.model_version)
    visual.device = "cpu"
    visual.allow_model_download = False
    visual.preview_fetch_enabled = False  # previews are fetched by the harness
    visual.frames_enabled = True
    visual.vlm_mode = "none"
    if "visual_combined_threshold" in overrides:
        visual.combined_threshold = float(overrides["visual_combined_threshold"])
    if "visual_weights" in overrides:
        visual.weights = VisualWeights.from_mapping(overrides["visual_weights"])
    if "visual_budget" in overrides:
        visual.budget = VisualBudget.from_mapping(overrides["visual_budget"])
    if "visual_must_avoid_threshold" in overrides:
        visual.must_avoid_similarity_threshold = float(
            overrides["visual_must_avoid_threshold"]
        )
    return config


def _spec(entry: dict[str, Any]) -> SceneSpec:
    return SceneSpec.from_dict(entry.get("scene_spec") or {})


def _candidate_rows(pool: dict[str, Any]) -> list[Candidate]:
    return [Candidate.from_dict(item) for item in pool.get("candidates") or []]


def _legacy_context(spec: SceneSpec) -> Any:
    from pipeline.media_semantics.replay import _LegacyContext

    return _LegacyContext(spec)


def preview_priority_candidates(
    entry: dict[str, Any],
    pool: dict[str, Any],
    config: SemanticConfig,
    *,
    limit: int = 16,
) -> list[Candidate]:
    """Union of candidates that each arm could plausibly select.

    Fetching previews only for the first N pool entries would leave the
    legacy/text winners without visual evidence.  This keeps the same bounded
    preview budget while covering every arm's top candidates.
    """
    spec = _spec(entry)
    candidates = _candidate_rows(pool)
    by_key = {c.canonical_key: c for c in candidates}
    ordered: list[Candidate] = []

    def add(candidate: Candidate | None) -> None:
        if candidate is not None and candidate.canonical_key not in {
            c.canonical_key for c in ordered
        }:
            ordered.append(candidate)

    legacy = legacy_decision(spec, candidates)
    for key in list((legacy.extra or {}).get("legacy_order") or [])[:4]:
        add(by_key.get(key))

    selector = SemanticMediaSelector(config=config)
    text = selector.rank(spec, candidates, threshold=config.min_relevance_score)
    rows = _candidate_rows_for_decision(text, candidates)
    for row in rows[:4]:
        add(by_key.get(row.get("candidate_key")))

    # Visual candidates need provider stills too: keep a spread of the pool.
    for candidate in candidates[: max(8, limit - len(ordered))]:
        add(candidate)

    for candidate in candidates:
        if len(ordered) >= limit:
            break
        add(candidate)
    return ordered[:limit]


def _winner_summary(decision: Any) -> dict[str, Any]:
    winner = getattr(decision, "winner", None)
    return {
        "winner": winner.to_dict() if winner is not None else None,
        "winner_score": round(float(getattr(decision, "winner_score", 0.0) or 0.0), 6),
        "status": getattr(decision.status, "value", str(decision.status)),
        "rejection_reason": getattr(decision, "rejection_reason", "") or "",
        "timing_ms": round(float(getattr(decision, "timing_ms", 0.0) or 0.0), 2),
        "candidates_considered": int(getattr(decision, "candidates_considered", 0) or 0),
        "candidate_scores": list(getattr(decision, "candidate_scores", []) or []),
        "rejected": [
            {"candidate": r.candidate.to_dict(), "score": r.score, "reason": r.reason}
            for r in (getattr(decision, "rejected", []) or [])
        ],
    }


def _preview_score_map(
    spec: SceneSpec,
    candidates: list[Candidate],
    previews: dict[str, dict[str, Any]],
    backend: Any,
    *,
    text_encoder: Any = None,
) -> tuple[dict[str, float], dict[str, Any]]:
    """CLIP similarity for every candidate with a usable provider still."""
    description = build_visual_description(spec)
    usable: list[tuple[Candidate, str]] = []
    for candidate in candidates:
        preview = previews.get(candidate.canonical_key) or {}
        path = str(preview.get("path") or "")
        if preview.get("status") != "PREVIEW_AVAILABLE" or not path:
            continue
        if not Path(path).is_file():
            continue
        usable.append((candidate, path))
    if not usable:
        return {}, {}, {"visual_description": description, "previews_used": 0, "error": "no_previews"}

    encoder = text_encoder or backend
    text_vectors = encoder.encode_texts([description])
    if not text_vectors:
        return {}, {}, {"visual_description": description, "previews_used": 0, "error": "text_encode_failed"}
    image_vectors = backend.encode_images([path for _, path in usable])
    if not image_vectors or len(image_vectors) != len(usable):
        return {}, {}, {"visual_description": description, "previews_used": 0, "error": "image_encode_failed"}
    scores = {
        candidate.canonical_key: cosine_similarity(text_vectors[0], vector)
        for (candidate, _), vector in zip(usable, image_vectors)
    }
    vectors_by_key = {
        candidate.canonical_key: vector
        for (candidate, _), vector in zip(usable, image_vectors)
    }
    return scores, vectors_by_key, {
        "visual_description": description,
        "previews_used": len(usable),
        "previews_available": len(usable),
        "similarity_min": round(min(scores.values()), 6) if scores else 0.0,
        "similarity_max": round(max(scores.values()), 6) if scores else 0.0,
    }


def _frame_score_map(
    candidates: list[Candidate],
    frame_assets: dict[str, dict[str, Any]],
    spec: SceneSpec,
    backend: Any,
    visual_config: VisualConfig,
    *,
    frame_sampler: Any = None,
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    """Score real downloaded frames for the bounded frame subset."""
    if not frame_assets:
        return {}, {"frame_candidates": 0, "frames": 0}
    description = build_visual_description(spec)
    text_vectors = backend.encode_texts([description])
    if not text_vectors:
        return {}, {"frame_candidates": 0, "frames": 0, "error": "text_encode_failed"}
    by_key = {c.canonical_key: c for c in candidates}
    rows: dict[str, dict[str, Any]] = {}
    total_frames = 0
    sampler = frame_sampler or sample_semantic_frames
    for key, asset in frame_assets.items():
        if key not in by_key:
            continue
        sampling = sampler(
            asset,
            max_frames=int(visual_config.budget.frame_count),
            tmp_dir=str(Path(asset["path"]).parent / "frames" / Path(asset["path"]).stem),
        )
        if not sampling.ok:
            rows[key] = {"error": sampling.error or "frame_extraction_failed"}
            continue
        vectors = backend.encode_images(sampling.frames)
        if not vectors or len(vectors) != len(sampling.frames):
            rows[key] = {"error": "frame_encode_failed"}
            continue
        values = [cosine_similarity(text_vectors[0], vector) for vector in vectors]
        aggregate = aggregate_frame_similarities(
            values,
            min_similarity=visual_config.frame_min_similarity,
            weights=visual_config.weights,
        )
        total_frames += len(values)
        rows[key] = {
            "frames": len(values),
            "timestamps": [round(t, 3) for t in sampling.timestamps],
            "values": [round(v, 6) for v in values],
            "aggregate": aggregate,
            "score": float(aggregate["score"]),
        }
    return rows, {"frame_candidates": len(rows), "frames": total_frames}


def score_scene(
    entry: dict[str, Any],
    pool: dict[str, Any],
    previews: dict[str, dict[str, Any]],
    *,
    backend: Any,
    config: SemanticConfig,
    frame_assets: dict[str, dict[str, Any]] | None = None,
    frame_weights_cache: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run the three arms over one frozen pool."""
    spec = _spec(entry)
    candidates = _candidate_rows(pool)
    queries = list(pool.get("queries") or [])
    started = time.time()

    # ── A. legacy ──────────────────────────────────────────────
    started_a = time.time()
    legacy = legacy_decision(spec, candidates)
    legacy_summary = _winner_summary(legacy)
    legacy_top = _legacy_top_candidates(spec, legacy, candidates, limit=3)
    legacy_summary["top_candidates"] = legacy_top
    if legacy_top:
        try:
            from pipeline.cinematic_staging import score_candidate

            legacy_summary["winner_score"] = round(
                float(
                    score_candidate(
                        legacy_top[0],
                        spec.narration_fragment or spec.subject or "",
                        None,
                        scene_context=_legacy_context(spec),
                    )
                ),
                6,
            )
        except Exception:
            pass

    # ── B. semantic text ───────────────────────────────────────
    started_b = time.time()
    text_selector = SemanticMediaSelector(config=config)
    text_decision = text_selector.rank(
        spec, candidates, threshold=config.min_relevance_score
    )
    text_summary = _winner_summary(text_decision)

    # ── C. semantic visual ─────────────────────────────────────
    started_c = time.time()
    visual_config = config.visual
    text_ranker = text_selector.ranker
    breakdowns = text_ranker.score_many(spec, candidates)
    text_by_key = {c.canonical_key: b for c, b in zip(candidates, breakdowns)}
    preview_scores, preview_vectors, preview_meta = _preview_score_map(
        spec, candidates, previews, backend
    )
    frame_scores, frame_meta = _frame_score_map(
        candidates,
        frame_assets or {},
        spec,
        backend,
        visual_config,
        frame_sampler=(frame_weights_cache or {}).get("frame_sampler"),
    )

    # Negative constraints via CLIP: available must_avoid terms vs the
    # candidate still (and frames when downloaded).  Missing evidence is not
    # negative evidence.
    avoid_terms = [str(t).strip() for t in spec.must_avoid if str(t).strip()]
    avoid_vectors = None
    if avoid_terms and backend is not None:
        try:
            avoid_vectors = backend.encode_texts([f"a photo of {t}" for t in avoid_terms])
        except Exception:
            avoid_vectors = None

    visual_rows: list[dict[str, Any]] = []
    for candidate in candidates:
        key = candidate.canonical_key
        breakdown = text_by_key.get(key)
        text_score = float(getattr(breakdown, "final", 0.0) or 0.0)
        preview_score = float(preview_scores.get(key, 0.0))
        tech = technical_quality_score(width=candidate.width, height=candidate.height)
        negative_hits: list[tuple[str, float]] = []
        if avoid_vectors:
            try:
                image_vector = preview_vectors.get(key)
                if image_vector:
                    for term, vector in zip(avoid_terms, avoid_vectors):
                        best = cosine_similarity(vector, image_vector)
                        if best >= float(
                            getattr(visual_config, "must_avoid_similarity_threshold", 0.28)
                        ):
                            negative_hits.append((term, best))
            except Exception:
                negative_hits = []
        constraints = evaluate_visual_constraints(
            spec,
            None,
            negative_embedding_hits=negative_hits,
            must_avoid_threshold=float(
                getattr(visual_config, "must_avoid_similarity_threshold", 0.28)
            ),
            reject_on_negative=visual_config.visual_reject_on_negative,
            reject_on_era=False,
        )
        frame_score = float((frame_scores.get(key) or {}).get("score", 0.0) or 0.0)
        combined = compute_combined_score(
            text_relevance=text_score,
            preview_relevance=preview_score,
            frame_relevance=frame_score,
            technical_quality=tech,
            negative_penalty=constraints.negative_penalty,
            era_penalty=constraints.era_penalty,
            weights=visual_config.weights,
            available_components={
                "text": True,
                "preview": key in preview_scores,
                "frame": key in frame_scores,
                "vlm": False,
                "technical_quality": True,
            },
        )
        visual_rows.append(
            {
                "candidate": candidate,
                "key": key,
                "provider": candidate.provider,
                "text_score": round(text_score, 6),
                "preview_relevance": round(preview_score, 6),
                "frame_relevance": round(frame_score, 6),
                "technical_quality": round(tech, 6),
                "negative_hits": [
                    {"term": term, "similarity": round(score, 6)}
                    for term, score in negative_hits
                ],
                "constraints": constraints.to_dict(),
                "combined": combined,
                "metadata_status": getattr(breakdown, "metadata_status", ""),
                "provider_rank": int(candidate.provider_rank or 0),
                "has_preview": key in preview_scores,
                "has_frames": key in frame_scores,
            }
        )

    # Text rank list (for shortlist), then visual shortlist.
    shortlist_size = int(visual_config.budget.shortlist)
    shortlist = sorted(
        visual_rows,
        key=lambda row: (
            -(0.70 * row["text_score"] + 0.30 * row["preview_relevance"]),
            row["key"],
        ),
    )[:shortlist_size]
    eligible = [
        row for row in shortlist
        if row["has_preview"]
        and not row["constraints"].get("rejection_reason")
        and float(row["combined"]["combined_score"]) >= float(visual_config.combined_threshold)
    ]
    eligible.sort(
        key=lambda row: (
            -float(row["combined"]["combined_score"]),
            -float(row["text_score"]),
            row["key"],
        )
    )
    visual_winner = eligible[0] if eligible else None
    if visual_winner is None:
        # P02 fail-open: fall back to the best text-scored candidate, but mark
        # the arm as NO_GOOD so the metrics never hide the degradation.
        best_text = max(visual_rows, key=lambda row: (row["text_score"], row["key"])) if visual_rows else None
        visual_status = SelectionStatus.NO_GOOD_CANDIDATE.value
    else:
        best_text = visual_winner
        visual_status = SelectionStatus.SELECTED.value

    top_visual = sorted(
        visual_rows,
        key=lambda row: (-float(row["combined"]["combined_score"]), row["key"]),
    )

    return {
        "scene_id": entry["scene_id"],
        "category": entry.get("category"),
        "era": entry.get("era"),
        "media_type": entry.get("media_type"),
        "pool_size": len(candidates),
        "queries": queries,
        "legacy": {
            **legacy_summary,
            "candidate_rows": _candidate_rows_for_decision(legacy, candidates),
            "latency_ms": round((time.time() - started_a) * 1000, 2),
        },
        "semantic_text": {
            **text_summary,
            "top_candidates": _top_rows(text_summary, candidates, limit=3),
            "candidate_rows": _candidate_rows_for_decision(text_decision, candidates),
            "latency_ms": round((time.time() - started_b) * 1000, 2),
            "threshold": config.min_relevance_score,
            "weights": {
                "semantic": config.weights.semantic,
                "lexical": config.weights.lexical,
                "action": config.weights.action,
                "subject": config.weights.subject,
                "environment": config.weights.environment,
                "era": config.weights.era,
                "metadata_quality": config.weights.metadata_quality,
                "provider_prior": config.weights.provider_prior,
                "negative_constraint": config.weights.negative_constraint,
                "no_metadata_ceiling": config.weights.no_metadata_ceiling,
            },
        },
        "semantic_visual": {
            "winner": best_text["candidate"].to_dict() if best_text else None,
            "winner_score": round(float(best_text["combined"]["combined_score"]), 6) if best_text else 0.0,
            "status": visual_status,
            "rejection_reason": "" if visual_winner else "no_candidate_above_visual_threshold",
            "combined_threshold": visual_config.combined_threshold,
            "weights": {
                "text": visual_config.weights.text,
                "preview": visual_config.weights.preview,
                "frame": visual_config.weights.frame,
                "vlm": visual_config.weights.vlm,
                "technical_quality": visual_config.weights.technical_quality,
                "negative_penalty": visual_config.weights.negative_penalty,
                "era_penalty": visual_config.weights.era_penalty,
            },
            "preview_meta": preview_meta,
            "frame_meta": frame_meta,
            "rows": [
                {
                    "candidate": row["candidate"].to_dict(),
                    "key": row["key"],
                    "provider": row["provider"],
                    "text_score": row["text_score"],
                    "preview_relevance": row["preview_relevance"],
                    "frame_relevance": row["frame_relevance"],
                    "technical_quality": row["technical_quality"],
                    "combined": row["combined"],
                    "negative_hits": row["negative_hits"],
                    "constraints": row["constraints"],
                    "metadata_status": row["metadata_status"],
                    "has_preview": row["has_preview"],
                    "has_frames": row["has_frames"],
                }
                for row in top_visual[: max(12, shortlist_size)]
            ],
            "top_candidates": [
                row["candidate"].to_dict() for row in top_visual[:3]
            ],
            "latency_ms": round((time.time() - started_c) * 1000, 2),
        },
        "scene_latency_ms": round((time.time() - started) * 1000, 2),
    }


def _candidate_rows_for_decision(decision: Any, candidates: list[Candidate]) -> list[dict[str, Any]]:
    """Full per-candidate score rows keyed by canonical key (for sweeps)."""
    by_url = {((c.download_url or c.asset_url)): c for c in candidates}
    rows: list[dict[str, Any]] = []
    for row in getattr(decision, "candidate_scores", []) or []:
        candidate = by_url.get(row.get("url"))
        if candidate is None:
            continue
        rows.append(
            {
                "candidate_key": candidate.canonical_key,
                "candidate": candidate.to_dict(),
                "provider": candidate.provider,
                "score": row.get("final"),
                "subject_match": row.get("subject_match"),
                "action_match": row.get("action_match"),
                "environment_match": row.get("environment_match"),
                "semantic": row.get("semantic"),
                "lexical": row.get("lexical"),
                "metadata_status": row.get("metadata_status"),
            }
        )
    return rows


def _legacy_top_candidates(
    spec: SceneSpec, decision: Any, candidates: list[Candidate], limit: int = 3
) -> list[dict[str, Any]]:
    by_key = {c.canonical_key: c for c in candidates}
    out: list[dict[str, Any]] = []
    for key in list((decision.extra or {}).get("legacy_order") or [])[:limit]:
        candidate = by_key.get(key)
        if candidate is not None:
            out.append(candidate.to_dict())
    return out


def _top_rows(summary: dict[str, Any], candidates: list[Candidate], limit: int = 3) -> list[dict[str, Any]]:
    by_key = {c.canonical_key: c for c in candidates}
    rows = []
    for row in (summary.get("candidate_scores") or [])[:limit]:
        candidate = None
        for value in by_key.values():
            if (value.download_url or value.asset_url) == row.get("url"):
                candidate = value
                break
        if candidate is None:
            continue
        rows.append(candidate.to_dict())
    return rows


# ── auto evaluation (auxiliary, never ground truth) ────────────────

def auto_eval_items(
    decisions: list[dict[str, Any]],
    *,
    per_arm: int = 3,
) -> list[dict[str, Any]]:
    """Candidate-level evidence items for the auxiliary VLM evaluation."""
    items: list[dict[str, Any]] = []
    seen: dict[tuple[str, str], dict[str, Any]] = {}
    for decision in decisions:
        for arm in ARMS:
            arm_data = decision.get(arm) or {}
            rows = list(arm_data.get("top_candidates") or [])[:per_arm]
            for index, candidate in enumerate(rows):
                key = Candidate.from_dict(candidate).canonical_key
                unique = (decision["scene_id"], key)
                if unique in seen:
                    seen[unique]["arms"].append({"arm": arm, "rank": index})
                    continue
                item = {
                    "id": f"{decision['scene_id']}::{key}",
                    "scene_id": decision["scene_id"],
                    "arm": arm,
                    "arms": [{"arm": arm, "rank": index}],
                    "rank": index,
                    "candidate_key": key,
                    "candidate": candidate,
                }
                seen[unique] = item
                items.append(item)
    return items


def build_vlm_backend(env: dict[str, str], model: str = "gpt-4o-mini") -> OpenAICompatibleVLMBackend | None:
    api_key = env.get("OPENAI_API_KEY", "")
    if not api_key:
        return None
    base_url = env.get("VISION_BASE_URL") or "https://api.openai.com/v1"
    config = VisualConfig(
        vlm_mode="openai_compatible",
        vlm_model=env.get("VISION_MODEL") or model,
        vlm_api_key=api_key,
        vlm_base_url=base_url,
        budget=VisualBudget(vlm_max_frames=1, vlm_max_image_bytes=3 * 1024 * 1024),
    )
    return OpenAICompatibleVLMBackend(config, model=config.vlm_model)


def _resize_evidence(path: str, out_dir: Path, max_side: int = 768) -> str:
    from PIL import Image

    target = out_dir / "eval_images" / Path(path).name
    if target.is_file():
        return str(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    with Image.open(path) as image:
        image = image.convert("RGB")
        width, height = image.size
        scale = min(1.0, float(max_side) / max(width, height))
        if scale < 1.0:
            image = image.resize(
                (max(1, int(width * scale)), max(1, int(height * scale))),
                Image.LANCZOS,
            )
        image.save(target, "JPEG", quality=85)
    return str(target)


def auto_eval_prompt(spec: SceneSpec) -> str:
    """Benchmark-specific rubric: relevance, not exact-entity verification."""
    avoid = ", ".join(str(x) for x in (spec.must_avoid or [])[:8]) or "(none)"
    return f"""You are grading whether a stock media still can illustrate one documentary scene.
This is a RELEVANCE rubric, not identity verification: a generic but plausible
illustration gets partial credit (e.g. generic ancient ruins for a specific
archaeological site).

REQUESTED VISUAL INTENT
- subject: {spec.subject}
- action: {spec.action}
- environment: {spec.environment}
- era: {spec.era or "(timeless)"}
- must_avoid: {avoid}
NARRATION (intent only, not evidence): {spec.narration_fragment[:700]}

Look ONLY at the visible pixels. Return JSON with exactly these keys:
{{
  "subject_match": 0.0,      // 0=wrong/absent subject, 0.5=generic substitute, 1=clearly the subject
  "action_match": 0.0,       // 0=action absent/contradicted, 0.5=implied, 1=clearly visible
  "environment_match": 0.0,  // 0=wrong setting, 0.5=generic compatible, 1=clearly the setting
  "era_match": 0.0,          // 0..1, or null when no historical era is requested
  "must_avoid": [],          // requested forbidden elements visibly present
  "overall_relevance": 0.0,  // 0=unrelated, 0.3=related theme/wrong subject, 0.6=generic but usable, 0.85=clearly illustrates intent, 1=ideal
  "confidence": 0.0,         // 0..1
  "reason": "one short sentence"
}}"""


def _clamp(value: Any, default: float | None = 0.0) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if number != number:
        return default
    return max(0.0, min(1.0, number))


def _auto_eval_vlm(
    vlm: OpenAICompatibleVLMBackend,
    spec: SceneSpec,
    image_path: str,
    *,
    timeout: float = 45.0,
    detail: str = "low",
) -> tuple[dict[str, Any], dict[str, int]]:
    """Call the VLM with the benchmark rubric and return parsed scores + usage."""
    import base64

    client = vlm._get_client()  # noqa: SLF001 - explicit benchmark integration
    data = Path(image_path).read_bytes()
    mime = "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        mime = "image/png"
    b64 = base64.b64encode(data).decode("ascii")
    response = client.chat.completions.create(
        model=vlm.model,
        timeout=timeout,
        temperature=0.0,
        max_tokens=400,
        messages=[
            {"role": "system", "content": "Return only valid JSON."},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": auto_eval_prompt(spec)},
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:{mime};base64,{b64}",
                            "detail": detail,
                        },
                    },
                ],
            },
        ],
    )
    usage = {
        "prompt_tokens": int(getattr(response.usage, "prompt_tokens", 0) or 0),
        "completion_tokens": int(getattr(response.usage, "completion_tokens", 0) or 0),
    }
    raw = str(response.choices[0].message.content or "")
    import re

    match = re.search(r"\{.*\}", raw, re.DOTALL)
    payload = json.loads(match.group(0)) if match else {}
    if not isinstance(payload, dict):
        raise ValueError("auto-eval payload is not an object")
    avoid = payload.get("must_avoid") or []
    if isinstance(avoid, str):
        avoid = [avoid]
    return (
        {
            "subject_match": _clamp(payload.get("subject_match")),
            "action_match": _clamp(payload.get("action_match")),
            "environment_match": _clamp(payload.get("environment_match")),
            "era_match": (
                None if payload.get("era_match") is None else _clamp(payload.get("era_match"))
            ),
            "must_avoid": [str(x)[:80] for x in avoid if str(x).strip()],
            "overall_relevance": _clamp(payload.get("overall_relevance")),
            "confidence": _clamp(payload.get("confidence")),
            "reason": str(payload.get("reason") or "")[:400],
            "decision": "benchmark_rubric",
            "model": vlm.model,
        },
        usage,
    )


def _expand_arms(record: dict[str, Any]) -> list[dict[str, Any]]:
    """One auto-evaluation per unique candidate, expanded per arm."""
    arms = list(record.get("arms") or [])
    if len(arms) <= 1:
        return [record]
    out = []
    for arm_info in arms:
        copy = dict(record)
        copy["arm"] = arm_info["arm"]
        copy["rank"] = arm_info["rank"]
        out.append(copy)
    return out


def run_auto_evaluation(
    items: list[dict[str, Any]],
    entries_by_id: dict[str, dict[str, Any]],
    previews_by_scene: dict[str, dict[str, dict[str, Any]]],
    frames_by_scene: dict[str, dict[str, dict[str, Any]]],
    *,
    vlm: OpenAICompatibleVLMBackend | None,
    out_dir: Path,
    max_items: int = 400,
    log_fn: Any = None,
) -> list[dict[str, Any]]:
    """Run the auxiliary VLM evaluation.  ``human_label`` is always null."""
    results: list[dict[str, Any]] = []
    if vlm is None:
        return results
    for item in items[:max_items]:
        entry = entries_by_id.get(item["scene_id"]) or {}
        spec = _spec(entry)
        candidate_key = item["candidate_key"]
        evidence = ""
        preview = (previews_by_scene.get(item["scene_id"]) or {}).get(candidate_key) or {}
        if preview.get("status") == "PREVIEW_AVAILABLE" and preview.get("path"):
            evidence = str(preview["path"])
        frame = (frames_by_scene.get(item["scene_id"]) or {}).get(candidate_key) or {}
        if not evidence and frame.get("path"):
            evidence = str(frame["path"])
        record: dict[str, Any] = {
            "id": item["id"],
            "scene_id": item["scene_id"],
            "arm": item["arm"],
            "arms": list(item.get("arms") or [{"arm": item["arm"], "rank": item["rank"]}]),
            "rank": item["rank"],
            "candidate_key": candidate_key,
            "provider": (item["candidate"] or {}).get("provider", ""),
            "evidence_path": evidence,
            "human_label": None,
            "auto_evaluation": {
                "status": "NO_EVIDENCE",
                "subject_match": None,
                "action_match": None,
                "environment_match": None,
                "era_match": None,
                "must_avoid": [],
                "overall_relevance": None,
                "confidence": 0.0,
                "reason": "",
            },
        }
        if not evidence or not Path(evidence).is_file():
            results.extend(_expand_arms(record))
            continue
        started = time.time()
        try:
            resized = _resize_evidence(evidence, out_dir)
            parsed, usage = _auto_eval_vlm(vlm, spec, resized)
            record["usage"] = usage
            record["timing_ms"] = round((time.time() - started) * 1000, 2)
            auto = record["auto_evaluation"]
            auto.update({"status": "OK", **parsed})
        except Exception as exc:  # fail-open
            record["timing_ms"] = round((time.time() - started) * 1000, 2)
            auto = record["auto_evaluation"]
            auto.update({"status": "VLM_FAILED", "reason": str(exc)[:300]})
        results.extend(_expand_arms(record))
        if log_fn:
            log_fn(f"auto-eval {item['id']}: {auto['status']}")
    return results


# ── metrics ───────────────────────────────────────────────────────

def _usable_scores(records: Iterable[dict[str, Any]]) -> list[float]:
    values = []
    for record in records:
        auto = record.get("auto_evaluation") or {}
        value = auto.get("overall_relevance")
        if auto.get("status") == "OK" and value is not None:
            values.append(float(value))
    return values


def filter_auto_eval(
    decisions: list[dict[str, Any]], auto_eval: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Keep only records that belong to the current decision top candidates.

    Auto-evaluation files are resumable; a re-score (e.g. after calibration)
    can retire candidates.  Stale records must never leak into metrics.
    """
    allowed = set()
    for decision in decisions:
        for arm in ARMS:
            arm_data = decision.get(arm) or {}
            for candidate in arm_data.get("top_candidates") or []:
                allowed.add((decision["scene_id"], arm, _candidate_key_from_dict(candidate)))
            for row in arm_data.get("candidate_rows") or []:
                if row.get("candidate_key"):
                    allowed.add((decision["scene_id"], arm, row["candidate_key"]))
    return [
        record for record in auto_eval
        if (record.get("scene_id"), record.get("arm"), record.get("candidate_key")) in allowed
    ]


def winner_records(
    decisions: list[dict[str, Any]], auto_eval: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Auto-evaluation records matching the candidate each arm actually selected.

    NO_GOOD arms intentionally have no record here: a good top-3 candidate that
    was rejected by the threshold must not be counted as a success.
    """
    index = {
        (r.get("scene_id"), r.get("arm"), r.get("candidate_key")): r
        for r in filter_auto_eval(decisions, auto_eval)
    }
    out = []
    for decision in decisions:
        for arm in ARMS:
            winner = (decision.get(arm) or {}).get("winner")
            if not winner:
                continue
            key = _candidate_key_from_dict(winner)
            record = index.get((decision["scene_id"], arm, key))
            if record is not None:
                out.append(record)
    return out


def aggregate_metrics(
    decisions: list[dict[str, Any]],
    auto_eval: list[dict[str, Any]],
    ledger: dict[str, Any],
    *,
    relevance_threshold: float = 0.60,
) -> dict[str, Any]:
    candidate_auto_eval = filter_auto_eval(decisions, auto_eval)
    auto_eval = winner_records(decisions, candidate_auto_eval)
    by_arm: dict[str, list[dict[str, Any]]] = {arm: [] for arm in ARMS}
    for record in auto_eval:
        by_arm.setdefault(record["arm"], []).append(record)

    metrics: dict[str, Any] = {"arms": {}, "paired": {}, "overall": {}}
    for arm in ARMS:
        records = by_arm.get(arm, [])
        ok = [r for r in records if (r.get("auto_evaluation") or {}).get("status") == "OK"]
        scores = _usable_scores(records)
        era_records = [
            r for r in ok
            if (r.get("auto_evaluation") or {}).get("era_match") is not None
            and (r.get("scene_era") or _scene_era(decisions, r["scene_id"]))
        ]
        era_violations = [
            r for r in era_records
            if float(r["auto_evaluation"]["era_match"]) < 0.5
        ]
        must_avoid = [r for r in ok if r["auto_evaluation"].get("must_avoid")]
        decisions_for_arm = [d for d in decisions if d.get(arm)]
        no_good = [
            d for d in decisions_for_arm
            if str(d[arm].get("status")) == SelectionStatus.NO_GOOD_CANDIDATE.value
            or not d[arm].get("winner")
        ]
        winners = [d[arm].get("winner") for d in decisions_for_arm if d[arm].get("winner")]
        provider_dist: dict[str, int] = {}
        for winner in winners:
            provider_dist[winner.get("provider", "?")] = provider_dist.get(winner.get("provider", "?"), 0) + 1
        generic = [
            w for w in winners
            if w.get("metadata_status") == "NO_METADATA"
            or (not w.get("title") and not (w.get("tags") or []))
        ]
        latency = sum(float(d[arm].get("latency_ms", 0.0) or 0.0) for d in decisions_for_arm)
        metrics["arms"][arm] = {
            "scenes": len(decisions_for_arm),
            "auto_evaluated": len(ok),
            "auto_coverage": round(len(ok) / max(1, len(decisions_for_arm)), 4),
            "candidate_auto_evaluated": sum(
                1 for r in candidate_auto_eval if r.get("arm") == arm
            ),
            "top1_relevance_mean": round(sum(scores) / len(scores), 4) if scores else None,
            "top1_relevance_median": round(_median(scores), 4) if scores else None,
            "relevant_rate": round(
                sum(1 for value in scores if value >= relevance_threshold) / max(1, len(scores)), 4
            ) if scores else None,
            "subject_match_mean": _mean_field(ok, "subject_match"),
            "action_match_mean": _mean_field(ok, "action_match"),
            "environment_match_mean": _mean_field(ok, "environment_match"),
            "era_violations": len(era_violations),
            "era_scenes": len(era_records),
            "must_avoid_violations": len(must_avoid),
            "no_good_candidate_rate": round(len(no_good) / max(1, len(decisions_for_arm)), 4),
            "generic_fallback_rate": round(len(generic) / max(1, len(winners)), 4),
            "provider_distribution": dict(sorted(provider_dist.items())),
            "latency_mean_ms": round(latency / max(1, len(decisions_for_arm)), 2),
            "latency_total_ms": round(latency, 2),
            "candidate_top3_relevance_mean": _mean_overall(
                [r for r in candidate_auto_eval if r.get("arm") == arm]
            ),
        }

    # Paired comparisons on scenes with auto evaluation for both arms.
    scored = {}
    for record in auto_eval:
        auto = record.get("auto_evaluation") or {}
        if auto.get("status") == "OK" and auto.get("overall_relevance") is not None:
            scored[(record["scene_id"], record["arm"])] = float(auto["overall_relevance"])
    pairs = {
        "legacy_vs_semantic_text": ("legacy", "semantic_text"),
        "legacy_vs_semantic_visual": ("legacy", "semantic_visual"),
        "semantic_text_vs_semantic_visual": ("semantic_text", "semantic_visual"),
    }
    for label, (left, right) in pairs.items():
        deltas = []
        for scene_id in {key[0] for key in scored}:
            if (scene_id, left) in scored and (scene_id, right) in scored:
                deltas.append(scored[(scene_id, right)] - scored[(scene_id, left)])
        metrics["paired"][label] = {
            "n": len(deltas),
            "mean_delta": round(sum(deltas) / len(deltas), 4) if deltas else None,
            "wins": sum(1 for d in deltas if d > 0.05),
            "losses": sum(1 for d in deltas if d < -0.05),
            "ties": sum(1 for d in deltas if abs(d) <= 0.05),
        }
    frames_sampled = sum(
        int(((d.get("semantic_visual") or {}).get("frame_meta") or {}).get("frames", 0) or 0)
        for d in decisions
    )
    vlm_records = [
        r for r in auto_eval
        if (r.get("auto_evaluation") or {}).get("status") == "OK"
    ]
    metrics["overall"] = {
        "network": ledger,
        "scenes": len(decisions),
        "auto_eval_records": len(candidate_auto_eval),
        "auto_eval_ok": len(vlm_records),
        "frames_sampled": frames_sampled,
        "vlm_calls": len(vlm_records),
        "vlm_prompt_tokens": sum((r.get("usage") or {}).get("prompt_tokens", 0) for r in vlm_records),
        "vlm_completion_tokens": sum((r.get("usage") or {}).get("completion_tokens", 0) for r in vlm_records),
        "vlm_latency_mean_ms": round(
            sum(float(r.get("timing_ms", 0) or 0) for r in vlm_records) / max(1, len(vlm_records)), 2
        ),
    }
    return metrics


def _candidate_key_from_dict(candidate: dict[str, Any]) -> str:
    try:
        return Candidate.from_dict(candidate).canonical_key
    except Exception:
        return ""


def _scene_era(decisions: list[dict[str, Any]], scene_id: str) -> str:
    for decision in decisions:
        if decision["scene_id"] == scene_id:
            return str(decision.get("era") or "")
    return ""


def _median(values: Sequence[float]) -> float:
    ordered = sorted(values)
    n = len(ordered)
    if not n:
        return 0.0
    mid = n // 2
    return ordered[mid] if n % 2 else (ordered[mid - 1] + ordered[mid]) / 2.0


def _mean_overall(records: Iterable[dict[str, Any]]) -> float | None:
    values = [
        float((r.get("auto_evaluation") or {})["overall_relevance"])
        for r in records
        if (r.get("auto_evaluation") or {}).get("status") == "OK"
        and (r.get("auto_evaluation") or {}).get("overall_relevance") is not None
    ]
    return round(sum(values) / len(values), 4) if values else None


def _mean_field(records: Iterable[dict[str, Any]], field: str) -> float | None:
    values = [
        float((r.get("auto_evaluation") or {})[field])
        for r in records
        if (r.get("auto_evaluation") or {}).get(field) is not None
    ]
    return round(sum(values) / len(values), 4) if values else None


def failure_set(
    decisions: list[dict[str, Any]],
    auto_eval: list[dict[str, Any]],
    *,
    relevance_threshold: float = 0.60,
) -> dict[str, list[dict[str, Any]]]:
    """Concrete LEGACY/TEXT/VISUAL failure classes (no aggregate-only output)."""
    auto_eval = winner_records(decisions, auto_eval)
    scores: dict[tuple[str, str], float] = {}
    for record in auto_eval:
        auto = record.get("auto_evaluation") or {}
        if auto.get("status") == "OK" and auto.get("overall_relevance") is not None:
            scores[(record["scene_id"], record["arm"])] = float(auto["overall_relevance"])

    classes = {
        "LEGACY_BAD_VISUAL_GOOD": [],
        "LEGACY_BAD_TEXT_GOOD": [],
        "TEXT_BAD_VISUAL_GOOD": [],
        "VISUAL_BAD_TEXT_GOOD": [],
        "ALL_BAD": [],
        "ALL_GOOD": [],
        "INCONCLUSIVE": [],
    }
    for decision in decisions:
        scene_id = decision["scene_id"]
        legacy = scores.get((scene_id, "legacy"))
        text = scores.get((scene_id, "semantic_text"))
        visual = scores.get((scene_id, "semantic_visual"))
        bad_legacy = legacy is None or legacy < relevance_threshold
        bad_text = text is None or text < relevance_threshold
        bad_visual = visual is None or visual < relevance_threshold
        case = _failure_case(decision, legacy, text, visual)
        matched = []
        if bad_legacy and not bad_visual:
            matched.append("LEGACY_BAD_VISUAL_GOOD")
        if bad_legacy and not bad_text:
            matched.append("LEGACY_BAD_TEXT_GOOD")
        if bad_text and not bad_visual:
            matched.append("TEXT_BAD_VISUAL_GOOD")
        if bad_visual and not bad_text:
            matched.append("VISUAL_BAD_TEXT_GOOD")
        if bad_legacy and bad_text and bad_visual:
            matched.append("ALL_BAD")
        if not bad_legacy and not bad_text and not bad_visual:
            matched.append("ALL_GOOD")
        if not matched:
            matched.append("INCONCLUSIVE")
        for key in matched:
            classes[key].append(case)
    return classes


def _failure_case(
    decision: dict[str, Any],
    legacy: float | None,
    text: float | None,
    visual: float | None,
) -> dict[str, Any]:
    def winner(arm: str) -> dict[str, Any] | None:
        candidate = (decision.get(arm) or {}).get("winner")
        if not candidate:
            return None
        return {
            "provider": candidate.get("provider"),
            "title": candidate.get("title"),
            "tags": candidate.get("tags"),
            "page_url": candidate.get("page_url"),
            "score": (decision.get(arm) or {}).get("winner_score"),
        }

    return {
        "scene_id": decision["scene_id"],
        "category": decision.get("category"),
        "era": decision.get("era"),
        "scores": {"legacy": legacy, "semantic_text": text, "semantic_visual": visual},
        "legacy": winner("legacy"),
        "semantic_text": winner("semantic_text"),
        "semantic_visual": winner("semantic_visual"),
    }


def _candidate_score_rows(decisions: list[dict[str, Any]], arm: str) -> list[dict[str, Any]]:
    rows = []
    for decision in decisions:
        arm_data = decision.get(arm) or {}
        if arm != "semantic_visual":
            for row in arm_data.get("candidate_rows") or []:
                rows.append(
                    {
                        "scene_id": decision["scene_id"],
                        "arm": arm,
                        "score": row.get("score"),
                        "provider": row.get("provider"),
                        "subject_match": row.get("subject_match"),
                        "action_match": row.get("action_match"),
                        "environment_match": row.get("environment_match"),
                        "semantic": row.get("semantic"),
                        "lexical": row.get("lexical"),
                        "metadata_status": row.get("metadata_status"),
                        "candidate_key": row.get("candidate_key"),
                        "url": (row.get("candidate") or {}).get("page_url"),
                    }
                )
        if arm == "semantic_visual":
            for row in (arm_data.get("rows") or []):
                combined = row.get("combined") or {}
                rows.append(
                    {
                        "scene_id": decision["scene_id"],
                        "arm": arm,
                        "score": combined.get("combined_score"),
                        "provider": row.get("provider"),
                        "text": (combined.get("components") or {}).get("text"),
                        "preview": (combined.get("components") or {}).get("preview"),
                        "frame": (combined.get("components") or {}).get("frame"),
                        "technical_quality": (combined.get("components") or {}).get("technical_quality"),
                        "metadata_status": row.get("metadata_status"),
                        "url": (row.get("candidate") or {}).get("page_url"),
                        "candidate_key": row.get("key"),
                    }
                )
    return rows


def sweep_threshold(
    decisions: list[dict[str, Any]],
    auto_eval: list[dict[str, Any]],
    arm: str,
    *,
    grid: Sequence[float],
    relevance_threshold: float = 0.60,
) -> list[dict[str, Any]]:
    """Precision/recall/coverage of a score threshold against AUTO_EVALUATION."""
    labels: dict[tuple[str, str], bool] = {}
    for record in auto_eval:
        auto = record.get("auto_evaluation") or {}
        if auto.get("status") == "OK" and auto.get("overall_relevance") is not None:
            key = (record["scene_id"], record.get("candidate_key", ""))
            labels[key] = float(auto["overall_relevance"]) >= relevance_threshold

    rows = _candidate_score_rows(decisions, arm)
    evaluated = []
    for row in rows:
        key = (row["scene_id"], row.get("candidate_key", ""))
        if key in labels and row.get("score") is not None:
            evaluated.append((float(row["score"]), labels[key]))
    out = []
    for threshold in grid:
        accepted = [label for score, label in evaluated if score >= threshold]
        relevant_accepted = sum(1 for label in accepted if label)
        total_relevant = sum(1 for _, label in evaluated if label)
        out.append(
            {
                "threshold": round(float(threshold), 4),
                "evaluated_candidates": len(evaluated),
                "accepted": len(accepted),
                "precision": round(relevant_accepted / len(accepted), 4) if accepted else 0.0,
                "recall": round(relevant_accepted / total_relevant, 4) if total_relevant else 0.0,
                "coverage": round(len(accepted) / max(1, len(evaluated)), 4),
            }
        )
    return out


def correlational_stats(
    decisions: list[dict[str, Any]],
    auto_eval: list[dict[str, Any]],
    arm: str,
) -> dict[str, Any]:
    """Pearson correlation of score components with AUTO_EVALUATION."""
    by_key = {}
    for record in auto_eval:
        auto = record.get("auto_evaluation") or {}
        if auto.get("status") == "OK" and auto.get("overall_relevance") is not None:
            by_key[(record["scene_id"], record.get("candidate_key", ""))] = float(auto["overall_relevance"])
    rows = [r for r in _candidate_score_rows(decisions, arm) if r.get("score") is not None]
    keys = ("score", "semantic", "lexical", "subject_match", "action_match", "environment_match", "text", "preview", "frame")
    result: dict[str, Any] = {"n": 0}
    pairs: dict[str, list[tuple[float, float]]] = {key: [] for key in keys}
    for row in rows:
        key = (row["scene_id"], row.get("candidate_key", ""))
        if key not in by_key:
            continue
        result["n"] += 1
        for field in keys:
            value = row.get(field)
            if value is None:
                continue
            pairs[field].append((float(value), by_key[key]))
    for field, values in pairs.items():
        result[field] = round(_pearson(values), 4) if len(values) >= 3 else None
    return result


def _pearson(pairs: Sequence[tuple[float, float]]) -> float:
    n = len(pairs)
    if n < 2:
        return 0.0
    xs = [p[0] for p in pairs]
    ys = [p[1] for p in pairs]
    mx = sum(xs) / n
    my = sum(ys) / n
    num = sum((x - mx) * (y - my) for x, y in pairs)
    denx = math.sqrt(sum((x - mx) ** 2 for x in xs))
    deny = math.sqrt(sum((y - my) ** 2 for y in ys))
    if denx <= 0 or deny <= 0:
        return 0.0
    return num / (denx * deny)


def propose_calibration(
    decisions_before: list[dict[str, Any]],
    auto_eval: list[dict[str, Any]],
    *,
    current_p01_threshold: float = 0.26,
    current_visual_threshold: float = 0.26,
    relevance_threshold: float = 0.60,
) -> dict[str, Any]:
    """Conservative, explainable calibration using the auxiliary evaluation."""
    auto_eval = filter_auto_eval(decisions_before, auto_eval)
    p01_grid = [0.10, 0.12, 0.14, 0.16, 0.18, 0.20, 0.22, 0.24, 0.26, 0.30]
    visual_grid = [0.06, 0.08, 0.10, 0.12, 0.14, 0.16, 0.18, 0.20, 0.24, 0.26]
    p01_sweep = sweep_threshold(
        decisions_before, auto_eval, "semantic_text", grid=p01_grid,
        relevance_threshold=relevance_threshold,
    )
    visual_sweep = sweep_threshold(
        decisions_before, auto_eval, "semantic_visual", grid=visual_grid,
        relevance_threshold=relevance_threshold,
    )

    def choose(sweep: list[dict[str, Any]], current: float) -> tuple[float, str]:
        usable = [row for row in sweep if row["accepted"] > 0 and row["evaluated_candidates"] >= 6]
        if not usable:
            return current, "insufficient auto-evaluated candidates; keep current value"
        # Prefer the highest F1, then the highest coverage.  Require at least
        # 50% coverage so a threshold cannot win by rejecting everything.
        candidates = [row for row in usable if row["coverage"] >= 0.50] or usable
        for row in candidates:
            row["_f1"] = (2 * row["precision"] * row["recall"]) / max(
                1e-9, row["precision"] + row["recall"]
            )
        best_f1 = max(row["_f1"] for row in candidates)
        # Robustness rule: if a threshold is effectively tied on F1 (within
        # 0.02), prefer the one with higher recall.  This avoids discarding
        # usable scenes for a negligible precision gain.
        near = [row for row in candidates if row["_f1"] >= best_f1 - 0.02]
        best = max(
            near,
            key=lambda row: (row["recall"], row["precision"], -abs(row["threshold"] - current)),
        )
        rationale = (
            f"sweep P={best['precision']:.2f} R={best['recall']:.2f} "
            f"coverage={best['coverage']:.2f} at {best['threshold']}"
        )
        return float(best["threshold"]), rationale

    p01_after, p01_reason = choose(p01_sweep, current_p01_threshold)
    visual_after, visual_reason = choose(visual_sweep, current_visual_threshold)
    for row in p01_sweep + visual_sweep:
        row.pop("_f1", None)

    text_corr = correlational_stats(decisions_before, auto_eval, "semantic_text")
    visual_corr = correlational_stats(decisions_before, auto_eval, "semantic_visual")

    # One large, explainable weight change per ranker: if subject/action carry
    # more signal than hashed "semantic", move weight there; otherwise keep.
    weights_after = dict(DEFAULT_WEIGHTS)
    weights_reason = "kept: auto-evaluation did not show a large component advantage"
    semantic_corr = text_corr.get("semantic")
    subject_corr = text_corr.get("subject_match")
    action_corr = text_corr.get("action_match")
    if (
        semantic_corr is not None
        and action_corr is not None
        and subject_corr is not None
        and max(action_corr, subject_corr) - semantic_corr >= 0.15
    ):
        weights_after["semantic"] = 0.22
        weights_after["action"] = round(DEFAULT_WEIGHTS["action"] + 0.05, 4)
        weights_after["subject"] = round(DEFAULT_WEIGHTS["subject"] + 0.05, 4)
        weights_after["lexical"] = round(DEFAULT_WEIGHTS["lexical"] + 0.03, 4)
        weights_reason = (
            "action/subject correlate more with auto relevance than hashed "
            "semantic; move 0.13 of weight from semantic to action/subject/lexical"
        )

    visual_weights_after = dict(DEFAULT_VISUAL_WEIGHTS)
    visual_weights_reason = "kept: preview/frame signal not strong enough to reweight"
    preview_corr = visual_corr.get("preview")
    frame_corr = visual_corr.get("frame")
    text_component_corr = visual_corr.get("text")
    if preview_corr is not None and preview_corr >= 0.30:
        visual_weights_reason = (
            "preview embeddings correlate positively with candidate-level auto "
            "relevance, but winner-level evidence was insufficient to justify "
            "reweighting; defaults kept to avoid overfitting"
        )

    return {
        "p01_min_relevance_score": {
            "before": current_p01_threshold,
            "after": p01_after,
            "rationale": p01_reason,
            "sweep": p01_sweep,
        },
        "visual_combined_threshold": {
            "before": current_visual_threshold,
            "after": visual_after,
            "rationale": visual_reason,
            "sweep": visual_sweep,
        },
        "ranking_weights": {
            "before": dict(DEFAULT_WEIGHTS),
            "after": weights_after,
            "rationale": weights_reason,
            "correlations": text_corr,
        },
        "visual_weights": {
            "before": dict(DEFAULT_VISUAL_WEIGHTS),
            "after": visual_weights_after,
            "rationale": visual_weights_reason,
            "correlations": visual_corr,
        },
        "notes": [
            "AUTO_EVALUATION is a VLM auxiliary signal, not human ground truth.",
            "Only threshold + one weight family were changed to limit overfitting.",
            "human_label remains null for every scene.",
        ],
    }
