"""P04 focused ALL_BAD benchmark and review pack.

Reuses the frozen P03 corpus/pools for stock candidates and performs a bounded
live archive canary (Wikimedia Commons + NASA) to test whether source expansion
reduces the ALL_BAD set.  No production generation, upload or global default is
touched.

Stages
------
* ``build``  — read corpus/failure set, write focused cases (offline).
* ``run``    — live archive retrieval + selected-still download + optional VLM
               rubric evaluation.  Requires ``--allow-network``.
* ``report`` — write metrics, review pack and README (offline).
* ``all``    — build + run + report.

Usage:
  python3 -m benchmarks.media_semantics.p04_cli \
    --run-id p04-20261025 --stage all --allow-network --vlm
"""

from __future__ import annotations

import argparse
import html
import json
import os
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from benchmarks.media_semantics import scoring as p03_scoring
from benchmarks.media_semantics import retrieval as p03_retrieval
from pipeline.media_semantics.contracts import Candidate, SceneSpec, SelectionDecision
from pipeline.media_semantics.source_router import SourceRouter
from pipeline.media_semantics.sourcing_pipeline import (
    SourcedMediaSelector,
    SourcingConfig,
)
from pipeline.media_semantics.visual_previews import PreviewFetcher

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_P03_RUN = ROOT / "benchmarks/media_semantics/runs/p03-20261024"
DEFAULT_OUT = ROOT / "benchmarks/media_semantics/runs"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_json(path: Path, default=None):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _p03_preview_path(scene_id: str, candidate_key: str, previews: dict, p03_run: Path) -> str:
    meta = (previews.get(scene_id) or {}).get(candidate_key) or {}
    raw = str(meta.get("path") or "")
    if raw and Path(raw).is_file():
        return raw
    if raw:
        fallback = p03_run / "previews" / scene_id / Path(raw).name
        if fallback.is_file():
            return str(fallback)
    return ""


class ArchiveBudget:
    def __init__(self, max_requests: int = 120) -> None:
        self.max_requests = max_requests
        self.requests = 0
        self.hits = 0
        self.cache: dict[tuple[str, str, str], list[dict]] = {}
        self.bytes = 0

    def exhausted(self) -> bool:
        return self.requests >= self.max_requests


def build_cases(p03_run: Path, out_dir: Path, *, control: int = 5) -> dict[str, Any]:
    corpus = _load_json(p03_run / "corpus.json", {}) or {}
    failures = _load_json(p03_run / "failure_set_before.json", {}) or {}
    entries = {e.get("scene_id"): e for e in corpus.get("entries") or []}
    all_bad = [item.get("scene_id") for item in failures.get("ALL_BAD") or []]
    all_good = [item.get("scene_id") for item in failures.get("ALL_GOOD") or []]
    selected: list[tuple[str, str]] = [(sid, "ALL_BAD") for sid in all_bad if sid in entries]
    selected += [(sid, "CONTROL") for sid in all_good[: max(0, control)] if sid in entries]
    cases = []
    for scene_id, cohort in selected:
        entry = entries[scene_id]
        cases.append({
            "scene_id": scene_id,
            "cohort": cohort,
            "category": entry.get("category"),
            "era": entry.get("era"),
            "narration_fragment": entry.get("narration_fragment"),
            "visual_description_es": entry.get("visual_description_es"),
            "scene_spec": entry.get("scene_spec") or {},
            "p03_best": _p03_best(failures, scene_id),
            "legacy_winner": _legacy_winner(p03_run, scene_id),
        })
    payload = {
        "version": 1,
        "generated_at": _now(),
        "p03_run": str(p03_run),
        "all_bad_count": len(all_bad),
        "control_count": len([c for c in cases if c["cohort"] == "CONTROL"]),
        "cases": cases,
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "cases.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
    )
    return payload


def _p03_best(failures: dict, scene_id: str) -> dict[str, Any]:
    for bucket in ("ALL_BAD", "LEGACY_BAD_VISUAL_GOOD", "TEXT_BAD_VISUAL_GOOD"):
        for item in failures.get(bucket) or []:
            if item.get("scene_id") == scene_id:
                return {
                    "bucket": bucket,
                    "scores": item.get("scores") or {},
                    "legacy": item.get("legacy"),
                    "semantic_text": item.get("semantic_text"),
                    "semantic_visual": item.get("semantic_visual"),
                }
    for item in failures.get("ALL_GOOD") or []:
        if item.get("scene_id") == scene_id:
            return {
                "bucket": "ALL_GOOD",
                "scores": item.get("scores") or {},
                "legacy": item.get("legacy"),
                "semantic_text": item.get("semantic_text"),
                "semantic_visual": item.get("semantic_visual"),
            }
    return {}


def _legacy_winner(p03_run: Path, scene_id: str) -> dict | None:
    decisions = _load_json(p03_run / "decisions_summary.json", None)
    if isinstance(decisions, list):
        for item in decisions:
            if item.get("scene_id") == scene_id:
                legacy = item.get("legacy") or {}
                return legacy.get("winner") or legacy
    labels = _load_json(p03_run / "labels_template.json", {}) or {}
    for entry in labels.get("entries") or []:
        if entry.get("scene_id") == scene_id:
            return entry.get("legacy_winner")
    return None


def _load_pools(p03_run: Path) -> dict[str, list[Candidate]]:
    pools = _load_json(p03_run / "pools.json", []) or []
    out: dict[str, list[Candidate]] = {}
    for item in pools:
        scene_id = str(item.get("scene_id") or "")
        out[scene_id] = [Candidate.from_dict(c) for c in item.get("candidates") or []]
    return out


def _media_type_for_asset_type(asset_type: str) -> str:
    from pipeline.media_semantics.source_router import AssetType

    return "video" if asset_type in (
        AssetType.STOCK_VIDEO.value, AssetType.ARCHIVAL_VIDEO.value,
    ) else "image"


def run_cases(
    cases_payload: dict[str, Any],
    out_dir: Path,
    *,
    p03_run: Path,
    allow_network: bool,
    use_vlm: bool,
    max_scenes: int = 0,
    archive_max_requests: int = 120,
    timeout: float = 20.0,
) -> dict[str, Any]:
    if not allow_network:
        raise SystemExit("run stage requires --allow-network (bounded archive canary)")
    from pipeline.media_semantics.archive_providers import provider_registry

    cases = list(cases_payload.get("cases") or [])
    if max_scenes > 0:
        cases = cases[:max_scenes]
    pools = _load_pools(p03_run)
    previews = _load_json(p03_run / "previews.json", {}) or {}
    archives = provider_registry()
    # provider_registry does not expose timeout; set the helper timeout directly.
    for provider in archives.values():
        provider.http.timeout = float(timeout)
        provider.budget.timeout = float(timeout)
    archive_budget = ArchiveBudget(archive_max_requests)
    evidence_dir = out_dir / "evidence"
    evidence_dir.mkdir(parents=True, exist_ok=True)
    preview_fetcher = PreviewFetcher(
        work_dir=out_dir / "preview_cache", allow_local_paths=True,
    )

    vlm = None
    if use_vlm:
        env = p03_retrieval.load_provider_env(str(p03_retrieval.DEFAULT_ENV))
        vlm = p03_scoring.build_vlm_backend(env)

    rng_selector = SourceRouter()
    results: list[dict[str, Any]] = []
    started_all = time.time()

    def copy_evidence(path: str, scene_id: str) -> str:
        if not path or not Path(path).is_file():
            return ""
        target = evidence_dir / scene_id / Path(path).name
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            shutil.copy2(path, target)
        return str(target.relative_to(out_dir))

    for index, case in enumerate(cases):
        scene_id = case["scene_id"]
        spec = SceneSpec.from_dict(case.get("scene_spec") or {})
        route = rng_selector.route(spec)
        pool = pools.get(scene_id, [])
        scene_started = time.time()

        def provider_search(provider: str, query: str, asset_type: str, limit: int):
            media_type = _media_type_for_asset_type(asset_type)
            from pipeline.media_semantics.source_router import AssetType

            is_archive = provider in archives
            if is_archive:
                key = (provider, media_type, query)
                if key in archive_budget.cache:
                    archive_budget.hits += 1
                    return list(archive_budget.cache[key])
                if archive_budget.exhausted():
                    return []
                archive_budget.requests += 1
                raw = archives[provider].search(query, media_type=media_type, limit=limit)
                archive_budget.bytes += archives[provider].http.bytes_received
                from pipeline.media_semantics.normalization import normalize_candidates

                candidates = normalize_candidates(
                    raw, provider=provider, query=query, media_type=media_type,
                )
                for candidate, item in zip(candidates, raw):
                    candidate.asset_type = str(item.get("asset_type") or "")
                archive_budget.cache[key] = candidates
                return list(candidates)
            # Stock: reuse the frozen P03 pool (no new stock requests).
            matches = [
                c for c in pool
                if c.provider == provider and str(c.media_type) == media_type
            ]
            return matches[:limit]

        def download_fn(candidate: Candidate) -> dict | None:
            archives_provider = archives.get(candidate.provider)
            if archives_provider is not None:
                raw = dict(candidate.raw_candidate or candidate.to_dict())
                out = evidence_dir / scene_id
                path = archives_provider.download(raw, out)
                archive_budget.bytes += archives_provider.http.bytes_received
                if not path:
                    return None
                return {"path": str(path), "type": candidate.media_type, "source": candidate.provider}
            preview_path = _p03_preview_path(scene_id, candidate.canonical_key, previews, p03_run)
            if not preview_path and candidate.has_preview:
                result = preview_fetcher.fetch(candidate)
                if result.available:
                    preview_path = result.path
            if not preview_path:
                return None
            return {"path": preview_path, "type": "image", "source": candidate.provider}

        selector = SourcedMediaSelector(
            config=SourcingConfig(
                ai_enabled=False,
                ai_max_per_video=0,
                max_queries_per_stage=1,
                max_candidates_per_stage=12,
                max_downloads_per_scene=2,
                # Benchmark: explore the whole ladder instead of stopping at
                # the first strong text match (P03 showed text strength does
                # not imply visual relevance).
                strong_relevance_score=2.0,
            ),
            provider_search=provider_search,
            download_fn=download_fn,
            available_providers=set(
                {c.provider for c in pool} | set(archives.keys())
            ),
            available_ai_backends=[],
        )
        result = selector.select(spec, scene_idx=spec.scene_idx)
        decision = result.decision
        selected_path = str((result.asset or {}).get("path") or "")
        copied = copy_evidence(selected_path, scene_id) if selected_path else ""
        auto_eval = None
        if vlm is not None and selected_path and Path(selected_path).is_file():
            try:
                resized = p03_scoring._resize_evidence(selected_path, out_dir / "eval_images")
                parsed, usage = p03_scoring._auto_eval_vlm(vlm, spec, resized)
                auto_eval = {"status": "OK", **parsed, "usage": usage}
            except Exception as exc:
                auto_eval = {"status": "VLM_FAILED", "reason": str(exc)[:300]}
        results.append({
            "scene_id": scene_id,
            "cohort": case.get("cohort"),
            "category": case.get("category"),
            "era": case.get("era"),
            "scene_spec": spec.to_dict(),
            "route": route.to_dict(),
            "decision": decision.to_dict(),
            "status": decision.status.value,
            "asset_type": decision.asset_type,
            "fallback_type": decision.fallback_type,
            "literal_match": decision.literal_match,
            "score": decision.winner_score,
            "provider": (decision.winner.provider if decision.winner else ""),
            "provenance": decision.provenance,
            "routing_budget": result.routing_budget,
            "selected_evidence": copied,
            "auto_evaluation": auto_eval,
            "timing_ms": round((time.time() - scene_started) * 1000, 2),
        })
        print(
            f"[{index + 1}/{len(cases)}] {scene_id} {case.get('cohort')} -> "
            f"{decision.asset_type or decision.status.value} / {decision.fallback_type or 'literal'} "
            f"eval={(auto_eval or {}).get('overall_relevance')}",
            flush=True,
        )

    payload = {
        "version": 1,
        "generated_at": _now(),
        "p03_run": str(p03_run),
        "cases": results,
        "network": {
            "archive_requests": archive_budget.requests,
            "archive_bytes": archive_budget.bytes,
            "archive_cache_entries": len(archive_budget.cache),
            "archive_cache_hits": archive_budget.hits,
            "total_timing_ms": round((time.time() - started_all) * 1000, 2),
        },
        "vlm": {"enabled": vlm is not None},
    }
    (out_dir / "p04_run.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
    )
    return payload


def build_report(
    cases_payload: dict[str, Any],
    run_payload: dict[str, Any],
    out_dir: Path,
    *,
    relevance_threshold: float = 0.60,
) -> dict[str, Any]:
    cases = {c["scene_id"]: c for c in cases_payload.get("cases") or []}
    rows = list(run_payload.get("cases") or [])
    all_bad = [r for r in rows if r.get("cohort") == "ALL_BAD"]
    control = [r for r in rows if r.get("cohort") == "CONTROL"]

    def evaluated(rows_):
        return [
            r for r in rows_
            if (r.get("auto_evaluation") or {}).get("status") == "OK"
            and (r.get("auto_evaluation") or {}).get("overall_relevance") is not None
        ]

    def good(rows_):
        return [
            r for r in evaluated(rows_)
            if float(r["auto_evaluation"]["overall_relevance"]) >= relevance_threshold
        ]

    before_scores = []
    for row in all_bad:
        scores = (cases.get(row["scene_id"], {}).get("p03_best") or {}).get("scores") or {}
        values = [float(v) for v in scores.values() if isinstance(v, (int, float))]
        if values:
            before_scores.append(max(values))
    after_all = evaluated(all_bad)
    after_scores = [float(r["auto_evaluation"]["overall_relevance"]) for r in after_all]
    archive_good = [
        r for r in good(all_bad)
        if str(r.get("asset_type") or "") in ("ARCHIVAL_IMAGE", "ARCHIVAL_VIDEO", "SCIENTIFIC_IMAGE")
    ]
    ai_good = [
        r for r in good(all_bad) if str(r.get("asset_type") or "") == "AI_IMAGE"
    ]
    conceptual = [r for r in all_bad if r.get("fallback_type") == "CONCEPTUAL"]
    control_good_before = 0
    for row in control:
        scores = (cases.get(row["scene_id"], {}).get("p03_best") or {}).get("scores") or {}
        values = [float(v) for v in scores.values() if isinstance(v, (int, float))]
        control_good_before += 1 if values and max(values) >= relevance_threshold else 0
    by_category: dict[str, dict[str, Any]] = {}
    for row in rows:
        key = str((row.get("route") or {}).get("scene_type") or "UNKNOWN")
        bucket = by_category.setdefault(key, {
            "scenes": 0, "requests": 0, "candidates": 0,
            "providers_queried": 0, "providers_considered": 0,
            "timing_ms": 0.0, "archive_assets": 0,
        })
        budget = row.get("routing_budget") or {}
        bucket["scenes"] += 1
        bucket["requests"] += int(budget.get("requests") or 0)
        bucket["candidates"] += int(budget.get("candidates") or 0)
        bucket["providers_queried"] += len(budget.get("providers_queried") or [])
        bucket["providers_considered"] += len(budget.get("providers_considered") or [])
        bucket["timing_ms"] += float(row.get("timing_ms") or 0.0)
        if str(row.get("asset_type") or "") in (
            "ARCHIVAL_IMAGE", "ARCHIVAL_VIDEO", "SCIENTIFIC_IMAGE",
        ):
            bucket["archive_assets"] += 1
    for bucket in by_category.values():
        bucket["timing_ms"] = round(bucket["timing_ms"], 2)
        bucket["mean_timing_ms"] = round(
            bucket["timing_ms"] / max(bucket["scenes"], 1), 2,
        )
    network = run_payload.get("network") or {}
    archive_requests = int(network.get("archive_requests") or 0)
    performance = {
        "by_scene_type": by_category,
        "archive": {
            "requests": archive_requests,
            "bytes": int(network.get("archive_bytes") or 0),
            "cache_entries": int(network.get("archive_cache_entries") or 0),
            "cache_hits": int(network.get("archive_cache_hits") or 0),
            "mean_latency_ms": round(
                float(network.get("total_timing_ms") or 0.0) / max(archive_requests, 1), 2,
            ),
        },
        "routing": {
            "scenes": len(rows),
            "providers_considered": sum(
                len((r.get("routing_budget") or {}).get("providers_considered") or [])
                for r in rows
            ),
            "providers_queried": sum(
                len((r.get("routing_budget") or {}).get("providers_queried") or [])
                for r in rows
            ),
            "candidates": sum(
                int((r.get("routing_budget") or {}).get("candidates") or 0)
                for r in rows
            ),
            "requests": sum(
                int((r.get("routing_budget") or {}).get("requests") or 0)
                for r in rows
            ),
        },
    }
    (out_dir / "performance.json").write_text(
        json.dumps(performance, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    metrics = {
        "relevance_threshold": relevance_threshold,
        "all_bad_before": len(all_bad),
        "all_bad_after": max(0, len(all_bad) - len(good(all_bad))),
        "all_bad_after_good": len(good(all_bad)),
        "all_bad_evaluated_after": len(after_all),
        "all_bad_before_mean_score": round(sum(before_scores) / max(len(before_scores), 1), 4),
        "all_bad_after_mean_score": round(sum(after_scores) / max(len(after_scores), 1), 4),
        "all_bad_reduction": max(0, len(all_bad) - len(good(all_bad))),
        "archive_good": len(archive_good),
        "ai_fallback_good": len(ai_good),
        "conceptual_fallback": len(conceptual),
        "unresolved": len([r for r in all_bad if r not in good(all_bad)]),
        "control_before_good": control_good_before,
        "control_after_good": len(good(control)),
        "control_total": len(control),
        "network": run_payload.get("network") or {},
        "vlm_enabled": bool((run_payload.get("vlm") or {}).get("enabled")),
        "performance": performance,
    }
    # Compact review thumbnails (the selected evidence files themselves can be
    # large archive stills; the pack only needs a readable preview).
    thumb_dir = out_dir / "review_thumbs"
    thumb_dir.mkdir(parents=True, exist_ok=True)
    for row in rows:
        source = str(row.get("selected_evidence") or "")
        source_path = Path(source)
        if source and not source_path.is_absolute():
            source_path = out_dir / source_path
        target = thumb_dir / f"{row.get('scene_id')}.jpg"
        if source and source_path.is_file():
            try:
                from PIL import Image
                with Image.open(source_path) as image:
                    image = image.convert("RGB")
                    w, h = image.size
                    scale = min(1.0, 420.0 / max(w, h))
                    if scale < 1.0:
                        image = image.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.LANCZOS)
                    image.save(target, "JPEG", quality=82)
                row["review_thumb"] = str(target.relative_to(out_dir))
            except Exception:
                row["review_thumb"] = ""
        else:
            row["review_thumb"] = ""
    report = {
        "generated_at": _now(),
        "metrics": metrics,
        "cases": rows,
        "cases_source": cases_payload.get("cases") or [],
    }
    (out_dir / "metrics.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
    )
    html_doc = _review_pack_html(report)
    (out_dir / "review_pack.html").write_text(html_doc, encoding="utf-8")
    (out_dir / "README.md").write_text(_report_markdown(report), encoding="utf-8")
    return report


def _review_pack_html(report: dict[str, Any]) -> str:
    rows = []
    cases = {c["scene_id"]: c for c in report.get("cases_source") or []}
    for row in report.get("cases") or []:
        case = cases.get(row["scene_id"], {})
        auto = row.get("auto_evaluation") or {}
        spec = row.get("scene_spec") or {}
        route = row.get("route") or {}
        prov = row.get("provenance") or {}
        p03 = case.get("p03_best") or {}
        p03_scores = p03.get("scores") or {}
        image = row.get("review_thumb") or row.get("selected_evidence") or ""
        rows.append(f"""
        <tr>
          <td><strong>{html.escape(str(row.get('scene_id')))}</strong><br>
            <span class="muted">{html.escape(str(case.get('cohort')))} · {html.escape(str(case.get('category')))}</span></td>
          <td class="narration">{html.escape(str(spec.get('narration_fragment') or case.get('narration_fragment') or ''))}</td>
          <td><code>{html.escape(str(route.get('scene_type')))}</code><br>
            {html.escape(str(row.get('asset_type')))}<br>
            <span class="muted">fallback={html.escape(str(row.get('fallback_type') or 'literal'))}</span><br>
            <span class="muted">score={row.get('score')}</span></td>
          <td>{html.escape(str(row.get('provider')))}<br>
            <span class="muted">{html.escape(str(prov.get('license') or 'UNKNOWN'))}</span><br>
            <span class="muted">{html.escape(str(prov.get('creator') or ''))}</span></td>
          <td>{html.escape(str(p03_scores))}</td>
          <td>{html.escape(str(auto.get('overall_relevance')))}<br>
            <span class="muted">{html.escape(str(auto.get('reason') or auto.get('status') or ''))}</span></td>
          <td>{html.escape(str(auto.get('human_label', 'null')))}</td>
          <td>{f'<a href="{html.escape(image)}"><img src="{html.escape(image)}" loading="lazy"></a>' if image else '<em>no still</em>'}</td>
        </tr>""")
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<title>P04 review pack</title>
<style>
body{{font-family:system-ui,sans-serif;margin:24px;background:#0f1115;color:#e8eaf0}}
h1,h2{{font-weight:600}} table{{border-collapse:collapse;width:100%}}
th,td{{border:1px solid #2a2f3a;padding:8px;vertical-align:top;font-size:13px}}
th{{background:#171b23;text-align:left}} img{{max-width:190px;border-radius:6px}}
.muted{{color:#98a2b3;font-size:11px}} code{{color:#8bd5ca}} tr:nth-child(even){{background:#131720}}
.narration{{max-width:420px}} .metrics{{display:flex;gap:16px;flex-wrap:wrap;margin:16px 0}}
.card{{background:#171b23;border:1px solid #2a2f3a;padding:12px 16px;border-radius:8px}}
</style></head><body>
<h1>P04 review pack — source expansion & fallback</h1>
<p class="muted">human_label is always null unless a real human intervention is recorded. Auto-evaluation uses the P03 benchmark rubric, not human ground truth.</p>
<div class="metrics">
  <div class="card">ALL_BAD before: <strong>{report['metrics']['all_bad_before']}</strong></div>
  <div class="card">ALL_BAD after: <strong>{report['metrics']['all_bad_after']}</strong></div>
  <div class="card">After LLM rubric mean: <strong>{report['metrics']['all_bad_after_mean_score']}</strong></div>
  <div class="card">Archive good: <strong>{report['metrics']['archive_good']}</strong></div>
  <div class="card">Conceptual fallback: <strong>{report['metrics']['conceptual_fallback']}</strong></div>
</div>
<table><thead><tr>
<th>Scene</th><th>Narration</th><th>Route / asset</th><th>Source / licence</th>
<th>P03 scores</th><th>P04 auto-eval</th><th>human_label</th><th>Still</th>
</tr></thead><tbody>{''.join(rows)}</tbody></table>
</body></html>"""


def _report_markdown(report: dict[str, Any]) -> str:
    m = report["metrics"]
    lines = [
        "# P04 — source expansion / fallback focused benchmark",
        "",
        f"Generated: {report['generated_at']}",
        "",
        "## Before / after (ALL_BAD)",
        "",
        "| Metric | Value |",
        "|---|---:|",
        f"| ALL_BAD before | {m['all_bad_before']} |",
        f"| ALL_BAD after | {m['all_bad_after']} |",
        f"| After scenes graded good (>= {m['relevance_threshold']}) | {m['all_bad_after_good']} |",
        f"| Evaluated after | {m['all_bad_evaluated_after']} |",
        f"| Mean P03 best score | {m['all_bad_before_mean_score']} |",
        f"| Mean P04 score | {m['all_bad_after_mean_score']} |",
        f"| Archive good | {m['archive_good']} |",
        f"| AI fallback good | {m['ai_fallback_good']} |",
        f"| Conceptual fallback | {m['conceptual_fallback']} |",
        f"| Unresolved | {m['unresolved']} |",
        f"| Control good before | {m['control_before_good']}/{m['control_total']} |",
        f"| Control good after | {m['control_after_good']}/{m['control_total']} |",
        "",
        "## Network",
        "",
        f"- Archive requests: {m['network'].get('archive_requests', 0)}",
        f"- Archive bytes: {m['network'].get('archive_bytes', 0)}",
        f"- Cache entries: {m['network'].get('archive_cache_hits', 0)}",
        f"- VLM enabled: {m['vlm_enabled']}",
        "",
        "Auto-evaluation is the P03 benchmark rubric, not human ground truth.",
        "human_label remains null. Production defaults are unchanged.",
    ]
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", default="p04-20261025")
    parser.add_argument("--stage", default="all", choices=("build", "run", "report", "all"))
    parser.add_argument("--p03-run", default=str(DEFAULT_P03_RUN))
    parser.add_argument("--out-root", default=str(DEFAULT_OUT))
    parser.add_argument("--control", type=int, default=5)
    parser.add_argument("--max-scenes", type=int, default=0)
    parser.add_argument("--archive-max-requests", type=int, default=120)
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument("--allow-network", action="store_true")
    parser.add_argument("--vlm", action="store_true")
    args = parser.parse_args()

    load_dotenv(str(p03_retrieval.DEFAULT_ENV))
    p03_run = Path(args.p03_run)
    out_dir = Path(args.out_root) / args.run_id
    out_dir.mkdir(parents=True, exist_ok=True)
    cases_payload = _load_json(out_dir / "cases.json")
    if args.stage in ("build", "all") or not cases_payload:
        cases_payload = build_cases(p03_run, out_dir, control=args.control)
    run_payload = _load_json(out_dir / "p04_run.json")
    if args.stage in ("run", "all"):
        run_payload = run_cases(
            cases_payload, out_dir, p03_run=p03_run,
            allow_network=args.allow_network, use_vlm=args.vlm,
            max_scenes=args.max_scenes,
            archive_max_requests=args.archive_max_requests,
            timeout=args.timeout,
        )
    if args.stage in ("report", "all"):
        if not run_payload:
            raise SystemExit("report stage needs p04_run.json (run it first)")
        report = build_report(cases_payload, run_payload, out_dir)
        print(json.dumps(report["metrics"], ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
