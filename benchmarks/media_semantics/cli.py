"""P03 orchestration CLI.

Usage::

    python3 -m benchmarks.media_semantics.cli --run-id 20261024-p03 --stage all

Stages are resumable: ``corpus``, ``retrieve``, ``previews``, ``score``,
``frames``, ``evaluate``, ``calibrate``, ``report``.  ``all`` runs every stage.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# Load production credentials before importing modules that read settings at
# import time (config.settings / config.llm_client).  Values are never logged.
try:
    from dotenv import load_dotenv

    for candidate in (Path("/root/autotube/.env"), REPO_ROOT / ".env"):
        if candidate.is_file():
            load_dotenv(candidate)
            break
except Exception:
    pass

from benchmarks.media_semantics import corpus as corpus_mod
from benchmarks.media_semantics import retrieval as retrieval_mod
from benchmarks.media_semantics import scoring as scoring_mod
from benchmarks.media_semantics import report as report_mod
from pipeline.media_semantics.visual_backends import build_visual_backend

DEFAULT_RUN_ROOT = REPO_ROOT / "benchmarks" / "media_semantics" / "runs"
MODEL_ID = "openai/clip-vit-base-patch32"


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _candidate_key(candidate: dict[str, Any]) -> str:
    from pipeline.media_semantics.contracts import Candidate

    try:
        return Candidate.from_dict(candidate).canonical_key
    except Exception:
        return ""


def _load_json(path: Path, default: Any = None) -> Any:
    if not path.is_file():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _log(message: str) -> None:
    print(f"[p03] {message}", flush=True)


def _asset_sample(asset: Any) -> dict[str, Any] | None:
    if asset is None:
        return None
    return {
        "url": str(getattr(asset, "url", "") or ""),
        "duration": getattr(asset, "duration", None),
        "resolution": list(getattr(asset, "resolution", ()) or ()),
        "page_url": str(getattr(asset, "page_url", "") or ""),
        "title": str(getattr(asset, "title", "") or ""),
        "tags": list(getattr(asset, "tags", []) or []),
        "thumbnail_url": str(getattr(asset, "thumbnail_url", "") or ""),
        "preview_url": str(getattr(asset, "preview_url", "") or ""),
        "preview_status": str(getattr(asset, "preview_status", "") or ""),
        "visual_source": str(getattr(asset, "visual_source", "") or ""),
    }


def preflight(run_dir: Path, env: dict[str, str]) -> dict[str, Any]:
    import resource

    disk = shutil.disk_usage(REPO_ROOT)
    try:
        git_head = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=REPO_ROOT, text=True
        ).strip()
        git_branch = subprocess.check_output(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=REPO_ROOT, text=True
        ).strip()
        git_status = subprocess.check_output(
            ["git", "status", "--short"], cwd=REPO_ROOT, text=True
        ).strip()
    except Exception:
        git_head = git_branch = git_status = ""
    try:
        import torch

        torch_version = torch.__version__
        cuda = bool(torch.cuda.is_available())
    except Exception:
        torch_version = "missing"
        cuda = False
    try:
        import transformers

        transformers_version = transformers.__version__
    except Exception:
        transformers_version = "missing"
    cache_root = Path.home() / ".cache" / "huggingface" / "hub" / "models--openai--clip-vit-base-patch32"
    model_bytes = 0
    if cache_root.is_dir():
        for path in cache_root.rglob("*"):
            if path.is_file():
                try:
                    model_bytes += path.stat().st_size
                except OSError:
                    pass
    payload = {
        "timestamp": utcnow(),
        "python": platform.python_version(),
        "platform": platform.platform(),
        "cpu_count": os.cpu_count(),
        "git": {"branch": git_branch, "head": git_head, "status_short": git_status},
        "torch": torch_version,
        "torch_cuda": cuda,
        "transformers": transformers_version,
        "ram_total_mb": round(
            (os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")) / (1024 * 1024), 1
        ),
        "ram_maxrss_mb": round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 1),
        "disk_free_gb": round(disk.free / (1024**3), 1),
        "disk_total_gb": round(disk.total / (1024**3), 1),
        "providers": retrieval_mod.provider_presence(env),
        "clip_cache": {
            "path": str(cache_root),
            "present": cache_root.is_dir(),
            "bytes": model_bytes,
        },
        "defaults": {
            "MEDIA_SELECTOR": os.getenv("MEDIA_SELECTOR", "legacy"),
            "P02_VISUAL_MODE": os.getenv("P02_VISUAL_MODE", "off"),
        },
    }
    report_mod.atomic_write_json(run_dir / "preflight.json", payload)
    return payload


def model_identity(run_dir: Path) -> dict[str, Any]:
    from transformers import CLIPModel, CLIPProcessor

    cache_root = Path.home() / ".cache" / "huggingface" / "hub" / "models--openai--clip-vit-base-patch32"
    files = []
    if cache_root.is_dir():
        for path in sorted(cache_root.rglob("*")):
            if path.is_file() and not path.is_symlink():
                files.append(
                    {"path": str(path.relative_to(cache_root)), "bytes": path.stat().st_size}
                )
    started = time.time()
    processor = CLIPProcessor.from_pretrained(MODEL_ID, local_files_only=True)
    loaded = time.time()
    model = CLIPModel.from_pretrained(MODEL_ID, local_files_only=True)
    finished = time.time()
    model.eval()
    payload = {
        "model": MODEL_ID,
        "version": "openai/clip-vit-base-patch32",
        "parameters_m": round(sum(p.numel() for p in model.parameters()) / 1e6, 2),
        "embedding_dim": int(model.config.projection_dim),
        "dtype": str(next(model.parameters()).dtype),
        "device": "cpu",
        "processor_load_s": round(loaded - started, 2),
        "model_load_s": round(finished - loaded, 2),
        "total_load_s": round(finished - started, 2),
        "why_selected": (
            "CLIP ViT-B/32: 151M params, 512-d joint text/image space, CPU-viable, "
            "already the P02 default model id, 1.2 GB on disk, ~10 s load and "
            "~1 GB RSS measured on this host (no GPU)."
        ),
        "cache_files": files,
        "cache_bytes": sum(item["bytes"] for item in files),
    }
    report_mod.atomic_write_json(run_dir / "model_identity.json", payload)
    return payload


def config_snapshot(run_dir: Path, env: dict[str, str], preflight_data: dict[str, Any]) -> dict[str, Any]:
    payload = {
        "timestamp": utcnow(),
        "production_defaults_unchanged": {
            "MEDIA_SELECTOR": "legacy",
            "P02_VISUAL_MODE": "off",
        },
        "benchmark_only": {
            "selector_arm": "semantic",
            "visual_mode": "full_visual (in-memory, benchmark harness only)",
            "visual_model": MODEL_ID,
            "visual_device": "cpu",
            "allow_model_download": False,
        },
        "p01": {
            "min_relevance_score": 0.26,
            "ranking_weights": scoring_mod.DEFAULT_WEIGHTS,
            "budget": {
                "max_total": 80,
                "max_per_provider": 30,
                "max_per_query": 24,
                "max_pages_per_provider": 2,
                "top_k": 10,
                "top_n_download": 3,
            },
        },
        "p02": {
            "combined_threshold": 0.26,
            "visual_weights": scoring_mod.DEFAULT_VISUAL_WEIGHTS,
            "visual_budget": {
                "preview_candidates": 12,
                "shortlist": 6,
                "download_candidates": 2,
                "preview_bytes": 512 * 1024,
                "preview_total_bytes": 4 * 1024 * 1024,
                "frame_count": 6,
                "vlm_candidates": 0,
            },
        },
        "providers": preflight_data.get("providers"),
        "secrets": {
            "pexels": "PRESENT" if env.get("PEXELS_API_KEY") else "ABSENT",
            "pixabay": "PRESENT" if env.get("PIXABAY_API_KEY") else "ABSENT",
            "openai_vision": "PRESENT" if env.get("OPENAI_API_KEY") else "ABSENT",
        },
    }
    report_mod.atomic_write_json(run_dir / "config_snapshot.json", payload)
    return payload


def provider_canary(run_dir: Path, env: dict[str, str], allow_network: bool) -> dict[str, Any]:
    """Minimal live contract probe (1 query / provider) with real shapes."""
    path = run_dir / "provider_canary.json"
    if path.is_file():
        return _load_json(path, {})
    payload: dict[str, Any] = {"timestamp": utcnow(), "calls": []}
    if not allow_network:
        payload["status"] = "SKIPPED_OFFLINE"
        report_mod.atomic_write_json(path, payload)
        return payload
    registry = retrieval_mod.ProviderRegistry(env)
    ledger = retrieval_mod.RequestLedger()
    probes = [
        ("pexels", registry.video.get("pexels"), "video"),
        ("pixabay", registry.video.get("pixabay"), "video"),
        ("mixkit", registry.video.get("mixkit"), "video"),
        ("coverr", registry.video.get("coverr"), "video"),
        ("pixabay_photo", registry.images.get("pixabay_photo"), "image"),
    ]
    for name, provider, media_type in probes:
        if provider is None:
            payload["calls"].append({"provider": name, "status": "UNAVAILABLE_NO_KEY"})
            continue
        started = time.time()
        try:
            if media_type == "video":
                page = provider.search_page("desert landscape", 4, 30, page=1, per_page=10)
                assets = list(getattr(page, "assets", []) or [])
                ledger.record(name, requests=1)
                sample = assets[0] if assets else None
                payload["calls"].append(
                    {
                        "provider": name,
                        "status": "OK" if assets else "EMPTY",
                        "assets": len(assets),
                        "total_available": getattr(page, "total_available", None),
                        "sample": _asset_sample(sample),
                        "sample_thumbnail_status": (
                            "AVAILABLE"
                            if sample is not None and getattr(sample, "thumbnail_url", "")
                            else "UNAVAILABLE"
                        ),
                        "latency_ms": round((time.time() - started) * 1000, 1),
                    }
                )
            else:
                results, total = provider.search_paginated("desert landscape", n=5, page=1)
                ledger.record(name, requests=1)
                payload["calls"].append(
                    {
                        "provider": name,
                        "status": "OK" if results else "EMPTY",
                        "assets": len(results),
                        "total_available": total,
                        "sample": results[0] if results else None,
                        "sample_thumbnail_status": (
                            "AVAILABLE" if results and results[0].get("thumbnail_url") else "UNAVAILABLE"
                        ),
                        "latency_ms": round((time.time() - started) * 1000, 1),
                    }
                )
        except Exception as exc:
            payload["calls"].append(
                {"provider": name, "status": "ERROR", "error": str(exc)[:300]}
            )
    payload["budget"] = ledger.to_dict()
    report_mod.atomic_write_json(path, payload)
    return payload


def build_or_load_corpus(args: argparse.Namespace, run_dir: Path, env: dict[str, str]) -> dict[str, Any]:
    path = run_dir / "corpus.json"
    if path.is_file() and not args.rebuild_corpus:
        return _load_json(path, {})
    client = None
    if args.llm_intents:
        from config.llm_client import create_llm_client

        client = create_llm_client(enable_thinking=False)
    _log("building corpus from production database")
    return corpus_mod.build_corpus(
        args.db,
        path,
        run_dir / "intent_cache.json",
        limit=args.limit,
        per_category=args.per_category,
        seed=args.seed,
        allow_llm=args.llm_intents,
        llm_client=client,
        llm_model=env.get("LLM_MODEL", "deepseek-chat"),
        log_fn=_log,
    )


def retrieve_all(
    corpus: dict[str, Any],
    run_dir: Path,
    env: dict[str, str],
    args: argparse.Namespace,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    path = run_dir / "pools.json"
    if path.is_file() and not args.rebuild_pools:
        pools = _load_json(path, [])
        ledger = _load_json(run_dir / "ledger.json", {})
        return pools, ledger or {}
    registry = retrieval_mod.ProviderRegistry(env)
    ledger = retrieval_mod.RequestLedger()
    pools: list[dict[str, Any]] = []
    for index, entry in enumerate(corpus.get("entries") or []):
        spec = scoring_mod._spec(entry)
        include_scrapers = index < args.scrapers_scenes and entry.get("media_type") == "video"
        _log(f"retrieving {entry['scene_id']} ({entry['category']}, {entry['media_type']})")
        pool = retrieval_mod.retrieve_scene(
            entry,
            spec,
            registry,
            ledger,
            include_scrapers=include_scrapers,
            log_fn=_log,
        )
        pools.append(pool)
    report_mod.atomic_write_json(path, pools)
    report_mod.atomic_write_json(run_dir / "ledger.json", ledger.to_dict())
    return pools, ledger.to_dict()


def fetch_all_previews(
    pools: list[dict[str, Any]],
    corpus: dict[str, Any],
    run_dir: Path,
    args: argparse.Namespace,
) -> dict[str, dict[str, Any]]:
    path = run_dir / "previews.json"
    if path.is_file() and not args.refresh_previews:
        return _load_json(path, {})
    from pipeline.media_semantics.contracts import Candidate

    entries = {entry["scene_id"]: entry for entry in corpus.get("entries") or []}
    config = scoring_mod.build_semantic_config({})
    ledger = retrieval_mod.RequestLedger()
    all_previews: dict[str, dict[str, Any]] = {}
    for pool in pools:
        scene_id = pool["scene_id"]
        entry = entries.get(scene_id) or {}
        try:
            candidates = scoring_mod.preview_priority_candidates(
                entry, pool, config, limit=max(args.preview_candidates, 12)
            )
        except Exception as exc:
            _log(f"preview priority failed for {scene_id}: {exc}")
            candidates = [Candidate.from_dict(item) for item in pool.get("candidates") or []]
        _log(f"fetching previews {scene_id} ({len(candidates)} priority candidates)")
        all_previews[scene_id] = retrieval_mod.fetch_previews(
            scene_id,
            candidates,
            run_dir,
            ledger,
            max_candidates=len(candidates),
        )
    report_mod.atomic_write_json(path, all_previews)
    _merge_ledger(run_dir, ledger.to_dict())
    return all_previews


def _merge_ledger(run_dir: Path, extra: dict[str, Any]) -> None:
    path = run_dir / "ledger.json"
    base = _load_json(path, {}) or {}
    merged = {
        "requests": int(base.get("requests", 0)) + int(extra.get("requests", 0)),
        "bytes": int(base.get("bytes", 0)) + int(extra.get("bytes", 0)),
        "downloads": int(base.get("downloads", 0)) + int(extra.get("downloads", 0)),
        "previews": int(base.get("previews", 0)) + int(extra.get("previews", 0)),
        "frames": int(base.get("frames", 0)) + int(extra.get("frames", 0)),
        "per_provider": {},
        "errors": list(base.get("errors") or []) + list(extra.get("errors") or []),
    }
    for source in (base.get("per_provider") or {}, extra.get("per_provider") or {}):
        for provider, stats in source.items():
            target = merged["per_provider"].setdefault(
                provider,
                {"requests": 0, "bytes": 0, "downloads": 0, "previews": 0, "frames": 0},
            )
            for key in target:
                target[key] += int(stats.get(key, 0) or 0)
    merged["per_provider"] = dict(sorted(merged["per_provider"].items()))
    report_mod.atomic_write_json(path, merged)


def load_backend():
    from pipeline.media_semantics.config import VisualConfig

    config = VisualConfig(
        mode="embedding_only",
        backend="clip",
        model=MODEL_ID,
        model_version="openai/clip-vit-base-patch32",
        device="cpu",
        allow_model_download=False,
    )
    backend = build_visual_backend(config)
    if not backend.available():
        raise RuntimeError("CLIP backend unavailable")
    return backend


def score_all(
    corpus: dict[str, Any],
    pools: list[dict[str, Any]],
    previews: dict[str, dict[str, Any]],
    frames: dict[str, dict[str, Any]],
    backend: Any,
    *,
    overrides: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    config = scoring_mod.build_semantic_config(overrides)
    by_id = {entry["scene_id"]: entry for entry in corpus.get("entries") or []}
    decisions = []
    for pool in pools:
        scene_id = pool["scene_id"]
        entry = by_id.get(scene_id)
        if entry is None:
            continue
        decisions.append(
            scoring_mod.score_scene(
                entry,
                pool,
                previews.get(scene_id) or {},
                backend=backend,
                config=config,
                frame_assets=frames.get(scene_id) or {},
            )
        )
    return decisions


def download_frames(
    decisions_pre: list[dict[str, Any]],
    corpus: dict[str, Any],
    run_dir: Path,
    args: argparse.Namespace,
) -> dict[str, dict[str, Any]]:
    """Bounded full-video download for the frame-evidence subset."""
    path = run_dir / "frames.json"
    if path.is_file() and not args.refresh_frames:
        return _load_json(path, {})
    from pipeline.media_semantics.contracts import Candidate

    entry_by_id = {entry["scene_id"]: entry for entry in corpus.get("entries") or []}
    ledger = retrieval_mod.RequestLedger()
    frames: dict[str, dict[str, Any]] = {}
    selected = 0
    total_bytes = 0
    for decision in decisions_pre:
        if selected >= args.frame_scenes:
            break
        entry = entry_by_id.get(decision["scene_id"]) or {}
        if entry.get("media_type") != "video":
            continue
        rows = list((decision.get("semantic_visual") or {}).get("rows") or [])
        picked = 0
        attempts = 0
        for row in rows:
            if picked >= args.frame_candidates or attempts >= args.frame_candidates + 2:
                break
            if total_bytes >= args.max_download_bytes:
                _log("frame download byte budget exhausted")
                break
            candidate_dict = row.get("candidate") or {}
            provider = str(candidate_dict.get("provider") or "")
            if provider not in ("pexels", "pixabay"):
                continue
            candidate = Candidate.from_dict(candidate_dict)
            url = candidate.download_url or candidate.asset_url
            if not url or not str(url).startswith("http"):
                continue
            attempts += 1
            _log(f"downloading frame candidate {decision['scene_id']} {provider}")
            asset = retrieval_mod.download_video_bounded(
                candidate, run_dir, ledger, max_bytes=args.max_video_bytes
            )
            if asset:
                frames.setdefault(decision["scene_id"], {})[candidate.canonical_key] = asset
                total_bytes += int(asset.get("bytes", 0) or 0)
                picked += 1
        if picked:
            selected += 1
    report_mod.atomic_write_json(path, frames)
    _merge_ledger(run_dir, ledger.to_dict())
    return frames


def evaluate_all(
    decisions: list[dict[str, Any]],
    corpus: dict[str, Any],
    previews: dict[str, dict[str, Any]],
    frames: dict[str, dict[str, Any]],
    run_dir: Path,
    env: dict[str, str],
    *,
    label: str,
    max_items: int,
    no_vlm: bool,
) -> list[dict[str, Any]]:
    path = run_dir / f"auto_eval_{label}.json"
    existing = _load_json(path, []) if path.is_file() else []
    covered = {
        (record.get("scene_id"), record.get("arm"), record.get("candidate_key"))
        for record in existing
    }
    vlm = None if no_vlm else scoring_mod.build_vlm_backend(env)
    entries = {entry["scene_id"]: entry for entry in corpus.get("entries") or []}
    items = scoring_mod.auto_eval_items(decisions, per_arm=3)
    missing = []
    for item in items:
        arms = [entry["arm"] for entry in item.get("arms") or [{"arm": item["arm"]}]]
        if any((item["scene_id"], arm, item["candidate_key"]) not in covered for arm in arms):
            missing.append(item)
    if not missing:
        _log(f"auto-evaluation already complete ({label}, {len(existing)} records)")
        return existing
    _log(
        f"auto-evaluating {len(missing)} new candidate groups "
        f"({label}; {len(existing)} cached records)"
    )
    results = scoring_mod.run_auto_evaluation(
        missing,
        entries,
        previews,
        frames,
        vlm=vlm,
        out_dir=run_dir,
        max_items=max_items,
        log_fn=_log,
    )
    merged = existing + [
        record for record in results
        if (record.get("scene_id"), record.get("arm"), record.get("candidate_key")) not in covered
    ]
    allowed = set()
    for decision in decisions:
        for arm in scoring_mod.ARMS:
            arm_data = decision.get(arm) or {}
            for candidate in arm_data.get("top_candidates") or []:
                allowed.add((decision["scene_id"], arm, _candidate_key(candidate)))
            for row in arm_data.get("candidate_rows") or []:
                if row.get("candidate_key"):
                    allowed.add((decision["scene_id"], arm, row["candidate_key"]))
    merged = [
        record for record in merged
        if (record.get("scene_id"), record.get("arm"), record.get("candidate_key")) in allowed
    ]
    report_mod.atomic_write_json(path, merged)
    return merged


def write_metrics(run_dir: Path, label: str, decisions: list[dict[str, Any]], auto_eval: list[dict[str, Any]]) -> dict[str, Any]:
    ledger = _load_json(run_dir / "ledger.json", {}) or {}
    metrics = scoring_mod.aggregate_metrics(decisions, auto_eval, ledger)
    metrics["label"] = label
    metrics["timestamp"] = utcnow()
    report_mod.atomic_write_json(run_dir / f"metrics_{label}.json", metrics)
    failures = scoring_mod.failure_set(decisions, auto_eval)
    report_mod.atomic_write_json(run_dir / f"failure_set_{label}.json", failures)
    return metrics


def checksums(run_dir: Path) -> dict[str, str]:
    out = {}
    for path in sorted(run_dir.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(run_dir)
        if rel.parts and rel.parts[0] in ("videos", "previews", "frames", "eval_images"):
            continue
        if path.name in ("checksums.json",):
            continue
        out[str(rel)] = sha256_file(path)
    report_mod.atomic_write_json(run_dir / "checksums.json", out)
    return out


def run(args: argparse.Namespace) -> int:
    env = retrieval_mod.load_provider_env(args.env_file)
    run_id = args.run_id or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_dir = Path(args.out_root) / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    _log(f"run_id={run_id} dir={run_dir}")

    if args.stage in ("all", "preflight"):
        pre = preflight(run_dir, env)
        model_identity(run_dir)
        config_snapshot(run_dir, env, pre)
        provider_canary(run_dir, env, allow_network=not args.skip_network)

    corpus_data = build_or_load_corpus(args, run_dir, env) if args.stage in (
        "all", "corpus", "retrieve", "previews", "score", "frames", "evaluate", "calibrate", "report"
    ) else _load_json(run_dir / "corpus.json", {})
    _log(
        f"corpus scenes={len(corpus_data.get('entries') or [])} "
        f"diversity={json.dumps(corpus_mod.corpus_diversity(corpus_data))}"
    )

    if args.stage in ("all", "retrieve", "previews", "score", "frames", "evaluate", "calibrate", "report"):
        if args.skip_network and not (run_dir / "pools.json").is_file():
            _log("offline mode with no cached pools: stopping after corpus")
            return 0
        pools, _ = retrieve_all(corpus_data, run_dir, env, args)
    else:
        pools = _load_json(run_dir / "pools.json", []) or []

    if args.stage in ("all", "previews", "score", "frames", "evaluate", "calibrate", "report"):
        if args.skip_network and not (run_dir / "previews.json").is_file():
            previews = {}
        else:
            previews = fetch_all_previews(pools, corpus_data, run_dir, args)
    else:
        previews = _load_json(run_dir / "previews.json", {}) or {}

    heavy_score = args.stage in ("all", "score", "frames", "evaluate", "calibrate") or (
        args.stage == "report" and args.rebuild_scores
    )
    if heavy_score:
        backend = load_backend()
        decisions_pre = score_all(corpus_data, pools, previews, {}, backend)
        report_mod.atomic_write_json(run_dir / "decisions_pre_frame.json", decisions_pre)
    else:
        decisions_pre = _load_json(run_dir / "decisions_pre_frame.json", []) or []

    if args.stage in ("all", "frames", "evaluate", "calibrate", "report"):
        frames = download_frames(decisions_pre, corpus_data, run_dir, args)
    else:
        frames = _load_json(run_dir / "frames.json", {}) or {}

    if heavy_score:
        backend = load_backend()
        decisions_before = score_all(corpus_data, pools, previews, frames, backend)
        report_mod.atomic_write_json(run_dir / "decisions_before.json", decisions_before)
    else:
        decisions_before = _load_json(run_dir / "decisions_before.json", []) or []

    if args.stage in ("all", "evaluate", "calibrate") or (
        args.stage == "report" and args.rebuild_scores
    ):
        auto_before = evaluate_all(
            decisions_before,
            corpus_data,
            previews,
            frames,
            run_dir,
            env,
            label="before",
            max_items=args.max_auto_eval,
            no_vlm=args.no_vlm,
        )
        metrics_before = write_metrics(run_dir, "before", decisions_before, auto_before)
    else:
        auto_before = _load_json(run_dir / "auto_eval_before.json", []) or []
        metrics_before = (
            write_metrics(run_dir, "before", decisions_before, auto_before)
            if decisions_before and auto_before
            else _load_json(run_dir / "metrics_before.json", {}) or {}
        )

    calibration = _load_json(run_dir / "calibration.json", {}) or {}
    if args.stage in ("all", "calibrate", "report"):
        _log("calibrating from auto-evaluation")
        calibration = scoring_mod.propose_calibration(
            decisions_before,
            auto_before,
            current_p01_threshold=0.26,
            current_visual_threshold=0.26,
        )
        calibration["timestamp"] = utcnow()
        report_mod.atomic_write_json(run_dir / "calibration.json", calibration)
    elif not calibration:
        calibration = _load_json(run_dir / "calibration.json", {}) or {}

    decisions_after = _load_json(run_dir / "decisions_after.json", []) or []
    auto_after = _load_json(run_dir / "auto_eval_after.json", []) or []
    metrics_after = _load_json(run_dir / "metrics_after.json", {}) or {}
    if args.stage in ("all", "calibrate") or (args.stage == "report" and args.rebuild_scores):
        overrides = {
            "p01_min_relevance_score": (calibration.get("p01_min_relevance_score") or {}).get("after", 0.26),
            "ranking_weights": (calibration.get("ranking_weights") or {}).get("after") or scoring_mod.DEFAULT_WEIGHTS,
            "visual_combined_threshold": (calibration.get("visual_combined_threshold") or {}).get("after", 0.26),
            "visual_weights": (calibration.get("visual_weights") or {}).get("after") or scoring_mod.DEFAULT_VISUAL_WEIGHTS,
        }
        backend = load_backend()
        decisions_after = score_all(corpus_data, pools, previews, frames, backend, overrides=overrides)
        report_mod.atomic_write_json(run_dir / "decisions_after.json", decisions_after)
        auto_after = evaluate_all(
            decisions_after,
            corpus_data,
            previews,
            frames,
            run_dir,
            env,
            label="after",
            max_items=args.max_auto_eval,
            no_vlm=args.no_vlm,
        )
        metrics_after = write_metrics(run_dir, "after", decisions_after, auto_after)
    elif args.stage == "report" and decisions_after and auto_after:
        metrics_after = write_metrics(run_dir, "after", decisions_after, auto_after)

    if args.stage in ("all", "report"):
        from benchmarks.media_semantics import performance as performance_mod

        backend = load_backend()
        ledger = _load_json(run_dir / "ledger.json", {}) or {}
        performance = performance_mod.build_performance_report(
            run_dir=run_dir,
            db_path=args.db,
            decisions=decisions_after or decisions_before,
            auto_eval=auto_after or auto_before,
            backend=backend,
            previews=previews,
            frames=frames,
            ledger=ledger,
        )
        report_mod.atomic_write_json(run_dir / "performance.json", performance)
        metrics = {
            "run_id": run_id,
            "before": metrics_before,
            "after": metrics_after,
            "calibration": calibration,
            "comparison": _comparison(metrics_before, metrics_after),
            "performance": performance,
        }
        report_mod.atomic_write_json(run_dir / "metrics.json", metrics)
        html_before, labels_before = report_mod.write_review_pack(
            corpus_data,
            decisions_before,
            auto_before,
            previews,
            frames,
            metrics_before,
            run_dir,
            title=f"P03 review pack — BEFORE calibration ({run_id})",
            filename="review_pack_before.html",
            labels_filename="labels_template_before.json",
        )
        if decisions_after:
            html_after, labels_after = report_mod.write_review_pack(
                corpus_data,
                decisions_after,
                auto_after,
                previews,
                frames,
                metrics_after,
                run_dir,
                title=f"P03 review pack — AFTER calibration ({run_id})",
                filename="review_pack_after.html",
                labels_filename="labels_template_after.json",
            )
            # Stable copies used by the committed artifact / repo index.
            shutil.copyfile(html_after, run_dir / "review_pack.html")
            shutil.copyfile(labels_after, run_dir / "labels_template.json")
        report_mod.copy_review_pack_to_repo(run_dir, REPO_ROOT, run_id)
        manifest = _manifest(run_dir, run_id, corpus_data, pools, decisions_before, decisions_after)
        report_mod.atomic_write_json(run_dir / "manifest.json", manifest)
        report_mod.write_artifact_index(run_dir, run_id, manifest)
        checksums(run_dir)
        _log(f"artifacts written to {run_dir}")
    return 0


def _comparison(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    out = {}
    for key in ("top1_relevance_mean", "relevant_rate", "subject_match_mean", "action_match_mean", "environment_match_mean", "no_good_candidate_rate"):
        for arm in scoring_mod.ARMS:
            b = ((before.get("arms") or {}).get(arm) or {}).get(key)
            a = ((after.get("arms") or {}).get(arm) or {}).get(key)
            out[f"{arm}.{key}"] = {"before": b, "after": a}
    return out


def _manifest(
    run_dir: Path,
    run_id: str,
    corpus: dict[str, Any],
    pools: list[dict[str, Any]],
    decisions_before: list[dict[str, Any]],
    decisions_after: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "run_id": run_id,
        "created_at": utcnow(),
        "corpus_scenes": len(corpus.get("entries") or []),
        "corpus_diversity": corpus_mod.corpus_diversity(corpus),
        "pool_candidates_total": sum(len(p.get("candidates") or []) for p in pools),
        "decisions_before": len(decisions_before),
        "decisions_after": len(decisions_after),
        "model": MODEL_ID,
        "artifacts": [
            "preflight.json",
            "model_identity.json",
            "config_snapshot.json",
            "provider_canary.json",
            "live_canary.json",
            "corpus.json",
            "intent_cache.json",
            "pools.json",
            "previews.json",
            "frames.json",
            "decisions_before.json",
            "decisions_after.json",
            "auto_eval_before.json",
            "auto_eval_after.json",
            "metrics.json",
            "failure_set_before.json",
            "failure_set_after.json",
            "calibration.json",
            "performance.json",
            "review_pack.html",
            "labels_template.json",
        ],
        "large_assets_note": (
            "previews/, videos/, frames/ and eval_images/ are runtime artifacts "
            "and are gitignored. Only JSON/HTML evidence is committed."
        ),
    }


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="P03 real-world media semantics benchmark")
    p.add_argument("--run-id", default="")
    p.add_argument("--out-root", default=str(DEFAULT_RUN_ROOT))
    p.add_argument("--db", default=str(corpus_mod.DEFAULT_DB))
    p.add_argument("--env-file", default=str(retrieval_mod.DEFAULT_ENV))
    p.add_argument("--stage", default="all", choices=[
        "all", "preflight", "corpus", "retrieve", "previews", "score",
        "frames", "evaluate", "calibrate", "report",
    ])
    p.add_argument("--limit", type=int, default=44)
    p.add_argument("--per-category", type=int, default=6)
    p.add_argument("--seed", type=int, default=corpus_mod.DEFAULT_SEED)
    p.add_argument("--preview-candidates", type=int, default=12)
    p.add_argument("--scrapers-scenes", type=int, default=8)
    p.add_argument("--frame-scenes", type=int, default=8)
    p.add_argument("--frame-candidates", type=int, default=2)
    p.add_argument("--max-video-bytes", type=int, default=retrieval_mod.MAX_VIDEO_BYTES)
    p.add_argument("--max-download-bytes", type=int, default=300 * 1024 * 1024)
    p.add_argument("--max-auto-eval", type=int, default=320)
    p.add_argument("--llm-intents", action="store_true")
    p.add_argument("--skip-network", action="store_true")
    p.add_argument("--no-vlm", action="store_true")
    p.add_argument("--rebuild-corpus", action="store_true")
    p.add_argument("--rebuild-pools", action="store_true")
    p.add_argument("--rebuild-scores", action="store_true")
    p.add_argument("--refresh-previews", action="store_true")
    p.add_argument("--refresh-frames", action="store_true")
    return p


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
