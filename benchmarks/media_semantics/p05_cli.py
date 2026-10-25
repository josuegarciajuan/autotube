"""P05 — integrated benchmark, shadow sample and AI fallback canary.

Offline by default: reuses the frozen P03 corpus/pools/previews and the P04
archive winners.  Nothing is published and production defaults are untouched.

Stages
------
* ``run``     — offline integrated benchmark over frozen evidence (CLIP preview
                when the local model is available).
* ``shadow``  — legacy vs P05 comparison over the same frozen scenes.
* ``report``  — metrics + human review pack.
* ``ai-canary`` — bounded live generation test (2-3 scenes, no upload).

Usage:
  python3 -m benchmarks.media_semantics.p05_cli --stage all \
      --run-id p05-20261025 --p03-run benchmarks/media_semantics/runs/p03-20261024 \
      --p04-run benchmarks/media_semantics/runs/p04-20261025c
"""

from __future__ import annotations

import argparse
import html
import json
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pipeline.media_semantics.contracts import Candidate, SceneSpec
from pipeline.media_semantics.integrated import (
    IntegratedConfig,
    IntegratedSelector,
    describe_universe,
)
from pipeline.media_semantics.source_router import AssetType, SourceRouter
from pipeline.media_semantics.unified_scoring import (
    PROFILE_FULL,
    PROFILE_PREVIEW,
    PROFILE_TEXT_ONLY,
    UnifiedWeights,
    VisualEvidence,
)
from pipeline.media_semantics.visual_evidence import P02PreviewEvidenceEvaluator

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_P03 = ROOT / "benchmarks/media_semantics/runs/p03-20261024"
DEFAULT_P04 = ROOT / "benchmarks/media_semantics/runs/p04-20261025c"
DEFAULT_OUT = ROOT / "benchmarks/media_semantics/evidence"
RELEVANCE_THRESHOLD = 0.60


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_json(path: Path, default=None):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
    )


def _resolve_preview(scene_id: str, key: str, previews: dict, p03_run: Path) -> str:
    meta = (previews.get(scene_id) or {}).get(key) or {}
    raw = str(meta.get("path") or "")
    if raw and Path(raw).is_file():
        return raw
    if raw:
        fallback = p03_run / "previews" / scene_id / Path(raw).name
        if fallback.is_file():
            return str(fallback)
    return ""


def load_labels(p03_run: Path) -> tuple[dict[str, dict[str, float]], dict[str, dict[str, str]]]:
    """Return (scene→key→relevance, scene→key→evidence_path)."""
    entries = _load_json(p03_run / "auto_eval_after.json", []) or []
    labels: dict[str, dict[str, float]] = {}
    evidence: dict[str, dict[str, str]] = {}
    for entry in entries:
        evaluation = entry.get("auto_evaluation") or {}
        if evaluation.get("status") != "OK":
            continue
        scene_id = str(entry.get("scene_id") or "")
        key = str(entry.get("candidate_key") or "")
        if not scene_id or not key:
            continue
        labels.setdefault(scene_id, {})[key] = float(
            evaluation.get("overall_relevance") or 0.0
        )
        path = str(entry.get("evidence_path") or "")
        if path:
            evidence.setdefault(scene_id, {})[key] = path
    return labels, evidence


def merge_p04_labels(
    p04_run: Path | None,
    labels: dict[str, dict[str, float]],
    evidence: dict[str, dict[str, str]],
) -> None:
    """Add P04 selected-asset auto-evaluations (archive canary evidence)."""
    if p04_run is None:
        return
    run = _load_json(p04_run / "p04_run.json", {}) or {}
    for case in run.get("cases") or []:
        scene_id = str(case.get("scene_id") or "")
        winner = (case.get("decision") or {}).get("winner")
        evaluation = case.get("auto_evaluation") or {}
        if not winner or evaluation.get("status") != "OK":
            continue
        candidate = Candidate.from_dict(winner)
        key = candidate.canonical_key
        labels.setdefault(scene_id, {})[key] = float(
            evaluation.get("overall_relevance") or 0.0
        )
        rel = str(case.get("selected_evidence") or "")
        if rel and (p04_run / rel).is_file():
            evidence.setdefault(scene_id, {})[key] = str(p04_run / rel)


def build_universe(
    p03_run: Path,
    p04_run: Path | None,
    labels: dict[str, dict[str, float]],
    evidence: dict[str, dict[str, str]],
    previews: dict,
) -> dict[str, list[tuple[Candidate, dict[str, Any]]]]:
    """One universe per scene, restricted to candidates with frozen labels.

    Frozen labels are the ground truth used by the benchmark.  Including an
    unlabelled candidate would make its metric unmeasurable, so it is omitted
    (documented limitation, not a selection advantage).
    """
    pools = _load_json(p03_run / "pools.json", []) or []
    by_scene: dict[str, dict[str, Candidate]] = {}
    for item in pools:
        scene_id = str(item.get("scene_id") or "")
        for raw in item.get("candidates") or []:
            candidate = Candidate.from_dict(raw)
            asset_type = candidate.asset_type or (
                AssetType.STOCK_VIDEO.value if candidate.media_type == "video"
                else AssetType.STOCK_IMAGE.value
            )
            candidate.asset_type = asset_type
            if candidate.canonical_key in labels.get(scene_id, {}):
                by_scene.setdefault(scene_id, {})[candidate.canonical_key] = candidate
    # P04 archive winners with evidence (bounded; never invent candidates).
    if p04_run is not None:
        run = _load_json(p04_run / "p04_run.json", {}) or {}
        for case in run.get("cases") or []:
            scene_id = str(case.get("scene_id") or "")
            winner = (case.get("decision") or {}).get("winner")
            if not winner:
                continue
            candidate = Candidate.from_dict(winner)
            key = candidate.canonical_key
            if key not in labels.get(scene_id, {}):
                continue
            candidate.asset_type = str(
                (case.get("decision") or {}).get("asset_type") or candidate.asset_type
            )
            rel = str(case.get("selected_evidence") or "")
            if rel:
                evidence.setdefault(scene_id, {})[key] = str(p04_run / rel)
            by_scene.setdefault(scene_id, {})[key] = candidate
    out: dict[str, list[tuple[Candidate, dict[str, Any]]]] = {}
    for scene_id, candidates in by_scene.items():
        out[scene_id] = [
            (candidate, {
                "asset_type": candidate.asset_type,
                "media_type": candidate.media_type,
            })
            for candidate in candidates.values()
        ]
    return out


def compute_arm_metrics(
    rows: list[dict[str, Any]],
    labels: dict[str, dict[str, float]],
    *,
    relevance_threshold: float = RELEVANCE_THRESHOLD,
) -> dict[str, Any]:
    """Pure metric function; deterministic and used by tests."""
    evaluated = []
    for row in rows:
        scene_id = str(row.get("scene_id") or "")
        key = str(row.get("selected_key") or "")
        label = (labels.get(scene_id) or {}).get(key)
        if label is None:
            continue
        evaluated.append((row, float(label)))
    if not evaluated:
        return {
            "scenes": 0, "top1_relevance_mean": None, "relevant_rate": None,
            "no_good_candidate_rate": None, "all_bad_scenes": 0,
            "all_bad_good": 0, "control_scenes": 0, "control_good": 0,
            "control_retention": None,
        }
    scores = [label for _, label in evaluated]
    relevant = [row for row, label in evaluated if label >= relevance_threshold]
    no_good = [row for row, label in evaluated if label < 0.30]
    all_bad = [(row, label) for row, label in evaluated if row.get("cohort") == "ALL_BAD"]
    control = [(row, label) for row, label in evaluated if row.get("cohort") == "CONTROL"]
    control_good = [row for row, label in control if label >= relevance_threshold]
    return {
        "scenes": len(evaluated),
        "top1_relevance_mean": round(sum(scores) / len(scores), 4),
        "top1_relevance_median": round(sorted(scores)[len(scores) // 2], 4),
        "relevant_rate": round(len(relevant) / len(evaluated), 4),
        "no_good_candidate_rate": round(len(no_good) / len(evaluated), 4),
        "all_bad_scenes": len(all_bad),
        "all_bad_good": len([row for row, label in all_bad if label >= relevance_threshold]),
        "all_bad_mean": round(
            sum(label for _, label in all_bad) / len(all_bad), 4,
        ) if all_bad else None,
        "control_scenes": len(control),
        "control_good": len(control_good),
        "control_retention": round(len(control_good) / len(control), 4) if control else None,
    }


def _build_selector(profile: str, evaluator, *, real_threshold: float = 0.26) -> IntegratedSelector:
    return IntegratedSelector(
        config=IntegratedConfig(
            visual_profile=profile,
            visual_top_n=8,
            max_visual_candidates=14,
            real_threshold=real_threshold,
            stage2_threshold=0.18,
            weights=UnifiedWeights(),
        ),
        visual_evaluator=evaluator,
    )


def _evaluator(p03_run: Path, previews: dict, evidence: dict[str, dict[str, str]], *, backend: str):
    path_map = {
        (scene_id, key): path
        for scene_id, items in evidence.items() for key, path in items.items()
    }

    def resolver(candidate: Candidate, spec: SceneSpec) -> str:
        scene_id = _scene_id_for_spec(spec)
        path = path_map.get((scene_id, candidate.canonical_key), "")
        if path and Path(path).is_file():
            return path
        return _resolve_preview(scene_id, candidate.canonical_key, previews, p03_run)

    from pipeline.media_semantics.config import VisualConfig

    config = VisualConfig(
        mode="embedding_only", backend=backend,
        model="openai/clip-vit-base-patch32", allow_model_download=False,
    )
    return P02PreviewEvidenceEvaluator(config=config, path_resolver=resolver)


_SPEC_SCENE: dict[int, str] = {}


def _scene_id_for_spec(spec: SceneSpec) -> str:
    return _SPEC_SCENE.get(id(spec), "")


def run_offline(
    out_dir: Path,
    *,
    p03_run: Path,
    p04_run: Path | None,
    profiles: tuple[str, ...] = (PROFILE_TEXT_ONLY, PROFILE_PREVIEW),
    max_scenes: int = 0,
    backend: str = "clip",
) -> dict[str, Any]:
    corpus = _load_json(p03_run / "corpus.json", {}) or {}
    cases = _load_json(p04_run / "cases.json", {}) if p04_run else {}
    cohorts = {
        str(case.get("scene_id")): str(case.get("cohort") or "P03")
        for case in (cases or {}).get("cases") or []
    }
    labels, evidence = load_labels(p03_run)
    merge_p04_labels(p04_run, labels, evidence)
    previews = _load_json(p03_run / "previews.json", {}) or {}
    universe = build_universe(p03_run, p04_run, labels, evidence, previews)
    entries = [
        entry for entry in corpus.get("entries") or []
        if str(entry.get("scene_id")) in universe
    ]
    if max_scenes > 0:
        entries = entries[:max_scenes]
    evaluator = _evaluator(p03_run, previews, evidence, backend=backend)
    router = SourceRouter()

    runs: dict[str, dict[str, Any]] = {}
    for profile in profiles:
        selector = _build_selector(profile, evaluator)
        rows: list[dict[str, Any]] = []
        started = time.time()
        for entry in entries:
            scene_id = str(entry.get("scene_id") or "")
            spec = SceneSpec.from_dict(entry.get("scene_spec") or {})
            _SPEC_SCENE[id(spec)] = scene_id
            route = router.route(spec)
            scene_started = time.time()
            ranked, approved, diagnostic = selector.rank_universe(
                spec, universe[scene_id], route, profile=profile,
            )
            winner = ranked[0] if ranked else None
            selected_key = winner.candidate.canonical_key if winner else ""
            row = {
                "scene_id": scene_id,
                "cohort": cohorts.get(scene_id, "P03"),
                "selected_key": selected_key,
                "score": round(winner.final, 6) if winner else 0.0,
                "text_score": round(winner.score.text_relevance, 6) if winner else 0.0,
                "asset_type": winner.asset_type if winner else "",
                "provider": winner.provider if winner else "",
                "route": route.to_dict(),
                "diagnostic": diagnostic,
                "timing_ms": round((time.time() - scene_started) * 1000, 2),
                "top5": [
                    {
                        "key": item.candidate.canonical_key,
                        "provider": item.provider,
                        "asset_type": item.asset_type,
                        "final": round(item.final, 6),
                        "score": item.score.to_dict(),
                    }
                    for item in ranked[:5]
                ],
            }
            rows.append(row)
        selector.reset()
        runs[profile] = {
            "profile": profile,
            "rows": rows,
            "metrics": compute_arm_metrics(rows, labels),
            "total_timing_ms": round((time.time() - started) * 1000, 2),
            "evidence_stats": evaluator.stats(),
        }
        evaluator.calls = evaluator.preview_hits = evaluator.preview_misses = evaluator.errors = 0
        evaluator._text_cache.clear()
        evaluator._image_cache.clear()

    payload = {
        "version": 1,
        "generated_at": _now(),
        "p03_run": str(p03_run),
        "p04_run": str(p04_run) if p04_run else "",
        "relevance_threshold": RELEVANCE_THRESHOLD,
        "scenes": len(entries),
        "profiles": runs,
        "labels": labels,
        "universe": {
            scene_id: describe_universe(items)
            for scene_id, items in universe.items()
        },
    }
    _write_json(out_dir / "p05_offline.json", payload)
    return payload


def run_shadow_sample(
    out_dir: Path,
    *,
    p03_run: Path,
    p04_run: Path | None,
    profile: str = PROFILE_PREVIEW,
    max_scenes: int = 0,
    backend: str = "clip",
) -> dict[str, Any]:
    from pipeline.media_semantics.shadow import ShadowRecorder, run_shadow_scene

    corpus = _load_json(p03_run / "corpus.json", {}) or {}
    cases = _load_json(p04_run / "cases.json", {}) if p04_run else {}
    cohorts = {
        str(case.get("scene_id")): str(case.get("cohort") or "P03")
        for case in (cases or {}).get("cases") or []
    }
    labels, evidence = load_labels(p03_run)
    merge_p04_labels(p04_run, labels, evidence)
    previews = _load_json(p03_run / "previews.json", {}) or {}
    universe = build_universe(p03_run, p04_run, labels, evidence, previews)
    decisions = _load_json(p03_run / "decisions_after.json", []) or []
    legacy_by_scene = {str(item.get("scene_id")): item for item in decisions}
    entries = [
        entry for entry in corpus.get("entries") or []
        if str(entry.get("scene_id")) in universe
    ]
    if max_scenes > 0:
        entries = entries[:max_scenes]
    evaluator = _evaluator(p03_run, previews, evidence, backend=backend)
    selector = _build_selector(profile, evaluator)
    router = SourceRouter()
    recorder = ShadowRecorder(out_dir / "shadow.jsonl")
    for entry in entries:
        scene_id = str(entry.get("scene_id") or "")
        spec = SceneSpec.from_dict(entry.get("scene_spec") or {})
        _SPEC_SCENE[id(spec)] = scene_id
        route = router.route(spec)
        legacy_decision = (legacy_by_scene.get(scene_id) or {}).get("legacy") or {}
        legacy_winner = dict((legacy_decision or {}).get("winner") or {})
        if legacy_winner:
            legacy_candidate = Candidate.from_dict(legacy_winner)
            legacy_winner["canonical_key"] = legacy_candidate.canonical_key
            legacy_score = legacy_decision.get("winner_score")
            try:
                legacy_score = float(legacy_score)
            except (TypeError, ValueError):
                legacy_score = None
            # P03 legacy winner_score is sometimes a quality score (e.g. 7.25),
            # not a 0..1 relevance; keep the field empty rather than misleading.
            legacy_winner["score"] = (
                legacy_score if legacy_score is not None and 0.0 <= legacy_score <= 1.0
                else None
            )
            label = (labels.get(scene_id) or {}).get(legacy_candidate.canonical_key)
            legacy_winner["auto_relevance"] = label
        record = run_shadow_scene(
            scene_id=scene_id,
            spec=spec,
            universe=universe[scene_id],
            route=route,
            selector=selector,
            legacy_winner=legacy_winner,
        )
        record.notes.append(f"cohort:{cohorts.get(scene_id, 'P03')}")
        winner_label = (labels.get(scene_id) or {}).get(
            (record.p05_winner or {}).get("canonical_key", "")
        )
        record.p05_winner["auto_relevance"] = winner_label
        recorder.add(record)
    summary = recorder.summary()
    _write_json(out_dir / "shadow_summary.json", summary)
    return summary


def run_ai_canary(
    out_dir: Path,
    *,
    p03_run: Path,
    p04_run: Path | None,
    max_scenes: int = 3,
    timeout: float = 90.0,
) -> dict[str, Any]:
    """Bounded live generation test. Never publishes or replaces assets."""
    from pipeline.media_semantics.ai_policy import build_ai_request
    from pipeline.providers.pollinations_provider import PollinationsProvider

    labels, _evidence = load_labels(p03_run)
    cases = (_load_json(p04_run / "cases.json", {}) if p04_run else {}) or {}
    chosen = [
        case for case in (cases.get("cases") or [])
        if case.get("cohort") == "ALL_BAD"
    ][: max_scenes]
    router = SourceRouter()
    out = out_dir / "ai_canary"
    out.mkdir(parents=True, exist_ok=True)
    provider = PollinationsProvider()
    rows: list[dict[str, Any]] = []
    for case in chosen:
        scene_id = str(case.get("scene_id") or "")
        spec = SceneSpec.from_dict(case.get("scene_spec") or {})
        route = router.route(spec)
        request = build_ai_request(spec, route)
        started = time.time()
        status = "OK"
        error = ""
        path = ""
        try:
            path = provider.generate(
                prompt=request.prompt,
                output_path=out / f"{scene_id}.jpg",
                seed=request.seed,
                negative_prompt=request.negative_prompt,
            ) or ""
        except Exception as exc:
            status = "AI_UNAVAILABLE"
            error = f"{type(exc).__name__}: {str(exc)[:200]}"
        if status == "OK" and not path:
            status = "AI_BAD_OUTPUT"
            error = "empty_output"
        elif status == "OK" and not Path(path).is_file():
            status = "AI_BAD_OUTPUT"
            error = f"missing_file:{path}"
        latency = round((time.time() - started) * 1000, 2)
        visual = None
        if path and Path(path).is_file():
            try:
                from pipeline.media_semantics.config import VisualConfig
                evaluator = P02PreviewEvidenceEvaluator(
                    config=VisualConfig(mode="embedding_only", backend="clip"),
                    path_resolver=lambda candidate, scene_spec: path,
                )
                candidate = Candidate(
                    provider="ai_pollinations",
                    provider_asset_id=request.prompt_hash,
                    asset_url=path, download_url=path,
                    title=spec.subject, description=request.prompt,
                    media_type="image", asset_type=AssetType.AI_IMAGE.value,
                    ai_generated=True, searchable_text=" ".join(
                        [spec.subject, spec.action, spec.environment, request.prompt]
                    ).lower(),
                )
                visual = evaluator(candidate, spec).to_dict()
            except Exception as exc:
                visual = {"status": "EVALUATION_FAILED", "error": str(exc)[:200]}
        rows.append({
            "scene_id": scene_id,
            "category": case.get("category"),
            "route": route.to_dict(),
            "backend": "pollinations",
            "status": status,
            "error": error,
            "latency_ms": latency,
            "prompt": request.prompt,
            "prompt_hash": request.prompt_hash,
            "output": str(path),
            "visual_evaluation": visual,
            "would_beat_existing_fallback": bool(
                visual and (visual.get("preview_relevance") or 0.0) >= 0.18
            ),
        })
    payload = {
        "version": 1,
        "generated_at": _now(),
        "rows": rows,
        "generated": sum(
            1 for row in rows
            if row["status"] == "OK" and row.get("output")
            and Path(str(row["output"])).is_file()
        ),
        "attempted": len(rows),
        "published": 0,
        "note": "Bounded canary; images are evidence only and are never published.",
    }
    _write_json(out_dir / "ai_canary.json", payload)
    return payload


def build_report(out_dir: Path, *, offline: dict, p03_run: Path, p04_run: Path) -> dict[str, Any]:
    p03_metrics = _load_json(p03_run / "metrics_after.json", {}) or {}
    p04_metrics = (_load_json(p04_run / "metrics.json", {}) or {}).get("metrics", {})
    labels = offline.get("labels") or {}
    profiles = offline.get("profiles") or {}
    preview_arm = profiles.get(PROFILE_PREVIEW) or {}
    text_arm = profiles.get(PROFILE_TEXT_ONLY) or {}
    legacy_keys = _legacy_top1_keys(p03_run)
    regression = _regression_rate(preview_arm.get("rows") or [], legacy_keys, labels)
    report = {
        "version": 1,
        "generated_at": _now(),
        "scenes": offline.get("scenes"),
        "arms": {
            "legacy": (p03_metrics.get("arms") or {}).get("legacy"),
            "semantic_text": (p03_metrics.get("arms") or {}).get("semantic_text"),
            "semantic_visual": (p03_metrics.get("arms") or {}).get("semantic_visual"),
            "p04": {
                "all_bad_after": p04_metrics.get("all_bad_after"),
                "all_bad_before": p04_metrics.get("all_bad_before"),
                "control_after_good": p04_metrics.get("control_after_good"),
                "control_before_good": p04_metrics.get("control_before_good"),
                "all_bad_after_mean_score": p04_metrics.get("all_bad_after_mean_score"),
            },
            "p05_text_only": text_arm.get("metrics"),
            "p05_preview_visual": preview_arm.get("metrics"),
        },
        "regression_rate": regression,
        "latency": {
            "p05_text_only_total_ms": text_arm.get("total_timing_ms"),
            "p05_preview_total_ms": preview_arm.get("total_timing_ms"),
        },
        "universe": offline.get("universe"),
    }
    _write_json(out_dir / "integrated_metrics.json", report)
    return report


def _legacy_top1_keys(p03_run: Path) -> dict[str, str]:
    decisions = _load_json(p03_run / "decisions_after.json", []) or []
    out = {}
    for item in decisions:
        winner = ((item.get("legacy") or {}).get("winner") or {})
        if winner:
            out[str(item.get("scene_id"))] = Candidate.from_dict(winner).canonical_key
    return out


def _regression_rate(
    rows: list[dict[str, Any]],
    legacy_keys: dict[str, str],
    labels: dict[str, dict[str, float]],
) -> dict[str, Any]:
    lost = 0
    total = 0
    for row in rows:
        scene_id = str(row.get("scene_id") or "")
        legacy_key = legacy_keys.get(scene_id, "")
        legacy_label = (labels.get(scene_id) or {}).get(legacy_key)
        p05_label = (labels.get(scene_id) or {}).get(str(row.get("selected_key") or ""))
        if legacy_label is None or p05_label is None:
            continue
        if legacy_label >= RELEVANCE_THRESHOLD:
            total += 1
            if p05_label < RELEVANCE_THRESHOLD:
                lost += 1
    return {
        "legacy_good_scenes": total,
        "regressed": lost,
        "rate": round(lost / total, 4) if total else None,
    }


def build_review_pack(
    out_dir: Path, *, offline: dict, p03_run: Path, previews: dict,
    p04_run: Path | None = None,
) -> Path:
    rows = (offline.get("profiles") or {}).get(PROFILE_PREVIEW, {}).get("rows") or []
    labels = offline.get("labels") or {}
    _labels, evidence = load_labels(p03_run)
    merge_p04_labels(p04_run, _labels, evidence)
    path_map = {
        (scene_id, key): path
        for scene_id, items in evidence.items() for key, path in items.items()
    }
    decisions = {
        str(item.get("scene_id")): item
        for item in (_load_json(p03_run / "decisions_after.json", []) or [])
    }
    parts = [
        "<!doctype html><html><head><meta charset='utf-8'><title>P05 review</title>",
        "<style>body{font-family:system-ui;margin:24px;background:#111;color:#eee}"
        ".card{border:1px solid #333;border-radius:10px;padding:14px;margin:14px 0}"
        ".row{display:flex;gap:14px;flex-wrap:wrap}img{max-width:360px;max-height:240px;"
        "background:#222}.bad{color:#ff7b7b}.good{color:#7bff9b}code{color:#9ad}"
        "table{border-collapse:collapse}td,th{border:1px solid #333;padding:3px 6px;font-size:12px}</style>",
        "</head><body><h1>P05 — legacy vs P05</h1>",
        f"<p>Generated {html.escape(str(offline.get('generated_at')))} · "
        f"scenes={offline.get('scenes')} · human_label=null (not invented)</p>",
    ]
    for row in rows:
        scene_id = str(row.get("scene_id") or "")
        spec = (decisions.get(scene_id) or {})
        legacy_winner = ((spec.get("legacy") or {}).get("winner") or {})
        legacy_key = Candidate.from_dict(legacy_winner).canonical_key if legacy_winner else ""
        legacy_path = path_map.get((scene_id, legacy_key)) or _resolve_preview(
            scene_id, legacy_key, previews, p03_run,
        )
        p05_key = str(row.get("selected_key") or "")
        p05_path = path_map.get((scene_id, p05_key)) or _resolve_preview(
            scene_id, p05_key, previews, p03_run,
        )
        legacy_label = (labels.get(scene_id) or {}).get(legacy_key)
        p05_label = (labels.get(scene_id) or {}).get(p05_key)
        parts.append("<div class='card'>")
        parts.append(f"<h3>{html.escape(scene_id)} · {html.escape(str(row.get('cohort')))} "
                     f"· {html.escape(str(row.get('asset_type')))}</h3>")
        parts.append("<div class='row'>")
        parts.append(
            f"<div><b>LEGACY</b><br><img src='{html.escape(legacy_path)}'>"
            f"<br>auto={legacy_label}</div>"
        )
        parts.append(
            f"<div><b>P05</b><br><img src='{html.escape(p05_path)}'>"
            f"<br>auto={p05_label} score={row.get('score')}</div>"
        )
        parts.append("</div>")
        parts.append(
            f"<p>provider={html.escape(str(row.get('provider')))} · "
            f"route={html.escape(str((row.get('route') or {}).get('scene_type')))} · "
            f"fallback={html.escape(str((row.get('diagnostic') or {}).get('fallback_type', '')))}"
            f" · human_label=<code>null</code></p>"
        )
        parts.append("<table><tr><th>component</th><th>value</th></tr>")
        winner = (row.get("top5") or [{}])[0]
        for key, value in (winner.get("score") or {}).items():
            if isinstance(value, (int, float, str)):
                parts.append(f"<tr><td>{html.escape(key)}</td><td>{value}</td></tr>")
        parts.append("</table></div>")
    parts.append("</body></html>")
    path = out_dir / "review_pack.html"
    path.write_text("\n".join(parts), encoding="utf-8")
    return path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--stage", default="all", choices=("run", "shadow", "report", "ai-canary", "all"))
    parser.add_argument("--run-id", default="p05-20261025")
    parser.add_argument("--p03-run", default=str(DEFAULT_P03))
    parser.add_argument("--p04-run", default=str(DEFAULT_P04))
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--max-scenes", type=int, default=0)
    parser.add_argument("--backend", default="clip")
    parser.add_argument("--allow-network", action="store_true")
    args = parser.parse_args()
    out_dir = Path(args.out) / args.run_id
    out_dir.mkdir(parents=True, exist_ok=True)
    p03_run = Path(args.p03_run)
    p04_run = Path(args.p04_run) if args.p04_run else None
    offline = None
    if args.stage in ("run", "all", "report"):
        offline_path = out_dir / "p05_offline.json"
        if offline_path.is_file() and args.stage == "report":
            offline = _load_json(offline_path, {})
        else:
            offline = run_offline(
                out_dir, p03_run=p03_run, p04_run=p04_run,
                profiles=(PROFILE_TEXT_ONLY, PROFILE_PREVIEW),
                max_scenes=args.max_scenes, backend=args.backend,
            )
        print(json.dumps({k: v.get("metrics") for k, v in (offline.get("profiles") or {}).items()}, indent=2))
    if args.stage in ("shadow", "all"):
        summary = run_shadow_sample(
            out_dir, p03_run=p03_run, p04_run=p04_run,
            max_scenes=args.max_scenes, backend=args.backend,
        )
        print(json.dumps(summary, indent=2))
    if args.stage == "ai-canary":
        if not args.allow_network:
            raise SystemExit("ai-canary requires --allow-network")
        summary = run_ai_canary(out_dir, p03_run=p03_run, p04_run=p04_run, max_scenes=3)
        print(json.dumps(summary, indent=2))
    if args.stage in ("report", "all"):
        if offline is None:
            offline = _load_json(out_dir / "p05_offline.json", {}) or {}
        report = build_report(out_dir, offline=offline, p03_run=p03_run, p04_run=p04_run or DEFAULT_P04)
        previews = _load_json(p03_run / "previews.json", {}) or {}
        build_review_pack(
            out_dir, offline=offline, p03_run=p03_run, previews=previews,
            p04_run=p04_run or DEFAULT_P04,
        )
        print(json.dumps(report, indent=2)[:4000])


if __name__ == "__main__":
    main()
