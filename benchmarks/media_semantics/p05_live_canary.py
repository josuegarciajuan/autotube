"""P05 live canary — one real script, a few real scenes, no upload.

This is the *scope mechanism* for the controlled canary: each arm runs in its
own process with an explicit environment, its own ``OUTPUT_DIR`` and a job id
matched against ``P05_CANARY_JOB_IDS``.  It never touches the production
service, the database (read-only) or YouTube.

Arms
----
* ``legacy`` — production legacy media path (P04/P05 off).
* ``p05``    — P04 SourceRouter + P05 integrated selection, PREVIEW_VISUAL,
               adaptive budget, visual cache, AI fallback disabled.

Usage
-----
    python3 -m benchmarks.media_semantics.p05_live_canary --arm all \
        --script-id 1714 --scenes 0,2,10 --run-id p05-canary-20261025

Artifacts under ``benchmarks/media_semantics/evidence/<run-id>/``:
``arm_legacy.json``, ``arm_p05.json``, ``comparison.json``,
``review_pack.html`` and ``review_thumbs/``.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DB = ROOT / "autotube.db"
DEFAULT_EVIDENCE = ROOT / "benchmarks/media_semantics/evidence"

ENV_PREFIXES = ("P04_", "P05_", "P02_VISUAL", "MEDIA_SELECTOR", "AUTOTUBE_JOB_ID")


# ── helpers ────────────────────────────────────────────────────────────────
def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(v) for v in value]
    dump = getattr(value, "to_dict", None)
    if callable(dump):
        try:
            return _json_safe(dump())
        except Exception:
            pass
    return str(value)


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(_json_safe(payload), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _read_profile(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()
    return values


def _env_for_arm(arm: str, run_dir: Path, token: str) -> dict[str, str]:
    base = {
        "P02_VISUAL_CACHE_ENABLED": "1",
        "P02_VISUAL_CACHE": str(run_dir / "visual_cache"),
        "P03_VISUAL_CACHE": str(run_dir / "visual_cache"),
        "MEDIA_SELECTOR": "legacy",
        "MEDIA_SELECTOR_SHADOW": "0",
        "P02_VISUAL_MODE": "off",
        "P04_SOURCE_ROUTER": "0",
        "P05_INTEGRATED": "0",
        "P04_ATTRIBUTION_MANIFEST": "0",
        "AUTOTUBE_JOB_ID": token,
        "P05_CANARY_JOB_IDS": token,
    }
    if arm == "p05":
        base.update(_read_profile(ROOT / "config/canary/p05-preview.profile"))
        # The profile ships a placeholder token; the run token always wins.
        base["AUTOTUBE_JOB_ID"] = token
        base["P05_CANARY_JOB_IDS"] = token
        base.update({
            "P04_SOURCE_ROUTER": "1",
            "P05_INTEGRATED": "1",
            "P05_VISUAL_PROFILE": "PREVIEW_VISUAL",
            "P05_VISUAL_TOP_N": "8",
            "P05_MAX_VISUAL_CANDIDATES": "12",
            "P05_ADAPTIVE_BUDGET": "1",
            "P05_REAL_THRESHOLD": "0.26",
            "P05_AI_THRESHOLD": "0.26",
            "P05_AI_VALIDATION_REQUIRED": "1",
            "P04_AI_ENABLED": "0",
            "P04_AI_MAX_PER_VIDEO": "0",
            "P02_VISUAL_MODE": "embedding_only",
        })
    return base


def _apply_env(env: dict[str, str]) -> None:
    for key in list(os.environ):
        if key.startswith(ENV_PREFIXES):
            os.environ.pop(key, None)
    os.environ.update(env)
    os.environ.setdefault("PYTHONUNBUFFERED", "1")


def _clean_child_env() -> dict[str, str]:
    env = {
        key: value for key, value in os.environ.items()
        if not key.startswith(ENV_PREFIXES)
    }
    env["PYTHONUNBUFFERED"] = "1"
    return env


def _load_scenes(db_path: Path, script_id: int, indices: list[int]) -> tuple[str, list[dict]]:
    connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    row = connection.execute(
        "SELECT id, canal, bloques_json FROM scripts WHERE id = ?", (script_id,)
    ).fetchone()
    connection.close()
    if row is None:
        raise SystemExit(f"script {script_id} not found in {db_path}")
    blocks = json.loads(row["bloques_json"] or "[]")
    if not blocks:
        raise SystemExit(f"script {script_id} has no blocks")
    scenes: list[dict] = []
    for index in indices:
        if index < 0 or index >= len(blocks):
            raise SystemExit(f"scene index {index} out of range (0..{len(blocks) - 1})")
        block = dict(blocks[index])
        block["asset_idx"] = index
        block["_canary_scene_index"] = index
        block.setdefault("media_duracion", block.get("duration", 5))
        scenes.append(block)
    return str(row["canal"]), scenes


def _build_ranges(scenes: list[dict]) -> list[dict]:
    ranges: list[dict] = []
    for i, block in enumerate(scenes):
        duration = float(block.get("media_duracion") or block.get("duration") or 5)
        ranges.append({
            **block,
            "start": 0,
            "end": duration,
            "duration": duration,
            "asset_idx": i,
            "source_scene_index": block.get("_canary_scene_index", i),
        })
    return ranges


def _still(asset: dict, out_path: Path) -> str | None:
    path = str((asset or {}).get("path") or "")
    if not path:
        return None
    source = Path(path)
    if not source.is_absolute():
        source = ROOT / source
    if not source.is_file():
        return None
    out_path.parent.mkdir(parents=True, exist_ok=True)
    media_type = str(asset.get("type") or "").lower()
    if media_type == "image" or source.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp"):
        try:
            from PIL import Image

            with Image.open(source) as image:
                image = image.convert("RGB")
                scale = min(1.0, 640.0 / max(image.size))
                if scale < 1.0:
                    image = image.resize(
                        (max(1, int(image.width * scale)), max(1, int(image.height * scale))),
                        Image.LANCZOS,
                    )
                image.save(out_path, "JPEG", quality=86)
            return str(out_path)
        except Exception:
            return None
    try:
        result = subprocess.run(
            [
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                "-ss", "1", "-i", str(source), "-frames:v", "1",
                "-vf", "scale=640:-2", str(out_path),
            ],
            capture_output=True, timeout=60,
        )
        if result.returncode == 0 and out_path.is_file():
            return str(out_path)
    except Exception:
        pass
    return None


# ── arm execution ──────────────────────────────────────────────────────────
def run_arm(args: argparse.Namespace) -> int:
    arm = args.arm
    run_dir = Path(args.out_dir).resolve()
    arm_dir = run_dir / arm
    arm_dir.mkdir(parents=True, exist_ok=True)
    media_root = Path(args.media_dir or (Path("/tmp/autotube_p05_canary") / args.run_id))
    out_dir = media_root / arm
    out_dir.mkdir(parents=True, exist_ok=True)

    token = f"canary-{args.run_id}-{arm}"
    env = _env_for_arm(arm, run_dir, token)
    env["OUTPUT_DIR"] = str(out_dir)
    env["DATABASE_PATH"] = str(Path(args.db).resolve())
    _apply_env(env)

    # Provider credentials + DB path come from the main checkout's .env (the
    # worktree is git-only and does not carry secrets). override=False so the
    # arm/profile variables set above always win.
    try:
        from dotenv import load_dotenv

        load_dotenv(args.env_file, override=False)
    except Exception:
        pass

    # Imports happen AFTER the arm environment is pinned (settings/media_fetcher
    # read env at import/constructor time).
    from config.config_bridge import get_channel_config
    from config import settings
    from pipeline.media_fetcher import MediaFetcher

    canal, scenes = _load_scenes(
        Path(args.db).resolve(), args.script_id,
        [int(x) for x in args.scenes.split(",") if x.strip()],
    )
    ranges = _build_ranges(scenes)
    started = time.time()
    errors: list[dict[str, Any]] = []
    assets: list[dict] = []
    decisions: list[dict] = []

    try:
        config = get_channel_config(canal)
        fetcher = MediaFetcher(config=config)
        assets = fetcher.fetch_for_script(bloques=scenes, scene_ranges=ranges) or []
        decisions = list(getattr(fetcher, "_pending_selection_decisions", []) or [])
    except Exception as exc:  # record, never publish
        errors.append({
            "phase": "fetch_for_script",
            "error": f"{type(exc).__name__}: {exc}",
        })

    elapsed = time.time() - started
    cache_stats = {}
    evaluator = getattr(fetcher, "_p05_visual_evaluator", None) if "fetcher" in locals() else None
    try:
        cache_stats = evaluator.cache.stats() if evaluator is not None else {}
    except Exception:
        cache_stats = {}
    payload = {
        "arm": arm,
        "run_id": args.run_id,
        "script_id": args.script_id,
        "channel": canal,
        "scene_indices": [int(x) for x in args.scenes.split(",") if x.strip()],
        "started_at": utcnow(),
        "elapsed_s": round(elapsed, 2),
        "env": {
            key: value for key, value in sorted(env.items())
            if not re.search(r"(KEY|TOKEN|SECRET|PASSWORD)", key, re.IGNORECASE)
        },
        "output_dir": str(out_dir),
        "settings_output_dir": str(settings.OUTPUT_DIR),
        "visual_cache": cache_stats,
        "scenes": [
            {
                "index": i,
                "source_scene_index": scenes[i].get("_canary_scene_index", i),
                "tipo": scenes[i].get("tipo"),
                "declared_media_type": scenes[i].get("media_tipo"),
                "narration": (scenes[i].get("texto") or "")[:1200],
                "query_en": scenes[i].get("search_query_en"),
                "description": scenes[i].get("escena_descripcion"),
                "asset": _json_safe(assets[i]) if i < len(assets) else None,
            }
            for i in range(len(scenes))
        ],
        "decisions": _json_safe(decisions),
        "errors": errors,
    }
    _write_json(arm_dir / "arm.json", payload)
    print(json.dumps({
        "arm": arm,
        "elapsed_s": round(elapsed, 2),
        "scenes": len(scenes),
        "assets": sum(1 for a in assets if a and a.get("path")),
        "decisions": len(decisions),
        "errors": len(errors),
    }))
    return 0 if not errors else 2


# ── comparison / report ────────────────────────────────────────────────────
def _asset_key(asset: dict | None) -> str:
    asset = asset or {}
    candidate = asset.get("candidate") or {}
    provider = candidate.get("provider") or asset.get("source") or ""
    pid = candidate.get("provider_asset_id") or ""
    if provider and pid:
        return f"{provider}:{pid}"
    path = asset.get("path") or candidate.get("download_url") or candidate.get("asset_url") or ""
    return str(path)


def _decision_for(arm: dict, scene_index: int) -> dict | None:
    for decision in arm.get("decisions") or []:
        if int(decision.get("scene_idx", -1)) == int(scene_index):
            return decision
    return None


def _winner_row(decision: dict | None, asset: dict | None) -> dict:
    """Match the P05 winner metadata inside the bounded candidate list."""
    if not decision:
        return {}
    try:
        rows = json.loads(decision.get("candidates_json") or "[]")
    except Exception:
        return {}
    try:
        winner = json.loads(decision.get("winner_json") or "{}")
    except Exception:
        winner = {}
    provider = str(winner.get("provider") or "")
    pid = str(winner.get("provider_asset_id") or "")
    for row in rows:
        if provider and str(row.get("provider") or "") != provider:
            continue
        if pid and str(row.get("provider_asset_id") or "") not in (pid, ""):
            continue
        return row
    return rows[0] if rows else {}


def _visual_score(decision: dict | None, asset: dict | None) -> float | None:
    row = _winner_row(decision, asset)
    for key in ("visual_preview_relevance", "frame_relevance", "vlm_relevance"):
        value = row.get(key)
        if isinstance(value, (int, float)):
            return float(value)
    return None


def _decision_meta(decision: dict | None) -> dict:
    if not decision:
        return {}
    cost = {}
    try:
        cost = json.loads(decision.get("cost_json") or "{}")
    except Exception:
        pass
    p04 = cost.get("p04") or {}
    routing = p04.get("routing_budget") or {}
    return {
        "scene_category": p04.get("scene_category") or "",
        "asset_type": p04.get("asset_type") or "",
        "ai_policy": p04.get("ai_policy") or "",
        "fallback_type": p04.get("fallback_type") or decision.get("fallback_reason") or "",
        "candidate_count": int(decision.get("candidate_count") or 0),
        "providers_queried": list(routing.get("providers_queried") or []),
        "providers_considered": list(routing.get("providers_considered") or []),
        "routing_queries": int(routing.get("queries") or 0),
        "routing_requests": int(routing.get("requests") or 0),
        "duplicate_rejections": int(routing.get("duplicate_rejections") or 0),
        "ai_used": int(routing.get("ai_used") or 0),
        "latency_ms": float(decision.get("timing_ms") or 0.0),
        "status": decision.get("status") or "",
    }


def _auto_eval(asset: dict, scene: dict, env: dict[str, str], out_dir: Path) -> dict:
    path = _still(asset, out_dir / f"{scene['index']:03d}.jpg")
    if not path:
        return {"status": "NO_IMAGE"}
    try:
        from benchmarks.media_semantics.scoring import build_vlm_backend, _auto_eval_vlm
        from benchmarks.media_semantics.corpus import Block, build_scene_spec
        from benchmarks.media_semantics.scoring import _resize_evidence

        vlm = build_vlm_backend(env)
        if vlm is None:
            return {"status": "UNAVAILABLE_NO_KEY", "evidence_path": path}
        block = Block(
            script_id=0, video_id=None, channel="canary", title="canary",
            block_idx=int(scene["index"]), narration=scene.get("narration") or "",
            description=scene.get("description") or "",
            query_en=scene.get("query_en") or "",
            media_type=scene.get("declared_media_type") or "image",
            duration=5, category="canary",
        )
        spec = build_scene_spec(block, {
            "subject": scene.get("query_en") or "",
            "action": "", "environment": "", "source": "canary",
        })
        resized = _resize_evidence(path, out_dir / "eval_images")
        parsed, usage = _auto_eval_vlm(vlm, spec, resized)
        return {
            "status": "OK",
            "overall_relevance": parsed.get("overall_relevance"),
            "subject_match": parsed.get("subject_match"),
            "action_match": parsed.get("action_match"),
            "environment_match": parsed.get("environment_match"),
            "reason": parsed.get("reason"),
            "usage": usage,
            "evidence_path": path,
        }
    except Exception as exc:
        return {"status": "ERROR", "error": f"{type(exc).__name__}: {exc}", "evidence_path": path}


def build_report(args: argparse.Namespace) -> int:
    run_dir = Path(args.out_dir).resolve()
    legacy = json.loads((run_dir / "legacy" / "arm.json").read_text(encoding="utf-8"))
    p05 = json.loads((run_dir / "p05" / "arm.json").read_text(encoding="utf-8"))
    thumbs = run_dir / "review_thumbs"
    thumbs.mkdir(parents=True, exist_ok=True)
    previous_eval: dict[tuple[int, str], dict] = {}
    if args.reuse_eval and (run_dir / "comparison.json").is_file():
        try:
            old = json.loads((run_dir / "comparison.json").read_text(encoding="utf-8"))
            for old_row in old.get("scenes") or []:
                for arm_name in ("legacy", "p05"):
                    value = (old_row.get(arm_name) or {}).get("auto_evaluation")
                    if value:
                        previous_eval[(int(old_row["scene_index"]), arm_name)] = value
        except Exception:
            previous_eval = {}

    from dotenv import load_dotenv

    load_dotenv(args.env_file, override=False)
    env = {**os.environ}
    do_eval = bool(env.get("OPENAI_API_KEY")) and not args.no_auto_eval

    rows: list[dict] = []
    for scene in legacy.get("scenes") or []:
        index = int(scene["index"])
        p_scene = next(
            (s for s in (p05.get("scenes") or []) if int(s["index"]) == index), {}
        )
        legacy_asset = scene.get("asset") or {}
        p05_asset = p_scene.get("asset") or {}
        legacy_decision = _decision_for(legacy, index)
        p05_decision = _decision_for(p05, index)
        legacy_thumb = _still(legacy_asset, thumbs / f"s{index:03d}-legacy.jpg")
        p05_thumb = _still(p05_asset, thumbs / f"s{index:03d}-p05.jpg")
        legacy_eval = previous_eval.get((index, "legacy")) or (
            _auto_eval(legacy_asset, scene, env, thumbs / f"s{index:03d}-legacy-eval")
            if do_eval and legacy_asset else {"status": "SKIPPED"}
        )
        p05_eval = previous_eval.get((index, "p05")) or (
            _auto_eval(p05_asset, p_scene, env, thumbs / f"s{index:03d}-p05-eval")
            if do_eval and p05_asset else {"status": "SKIPPED"}
        )
        p05_row = _winner_row(p05_decision, p05_asset)
        p05_meta = _decision_meta(p05_decision)
        p05_prov = p05_asset.get("provenance") or {}
        archive_used = str(p05_meta.get("asset_type") or "").startswith("ARCHIVAL") or (
            str(p05_meta.get("asset_type") or "") == "SCIENTIFIC_IMAGE"
        ) or str(p05_asset.get("source") or "") in (
            "wikimedia_commons", "nasa_image_library",
        )
        ai_used = str(p05_meta.get("asset_type") or "") == "AI_IMAGE" or str(
            p05_asset.get("source") or ""
        ).startswith("ai_")
        rows.append({
            "scene_index": index,
            "source_scene_index": scene.get("source_scene_index"),
            "narration": scene.get("narration"),
            "query_en": scene.get("query_en"),
            "legacy": {
                "asset": legacy_asset,
                "asset_key": _asset_key(legacy_asset),
                "provider": legacy_asset.get("source") or "",
                "type": legacy_asset.get("type") or "",
                "path": legacy_asset.get("path") or "",
                "thumb": legacy_thumb,
                "score": None,
                "decision": legacy_decision,
                "auto_evaluation": legacy_eval,
                "human_label": None,
            },
            "p05": {
                "asset": p05_asset,
                "asset_key": _asset_key(p05_asset),
                "provider": p05_asset.get("source") or "",
                "type": p05_asset.get("type") or "",
                "path": p05_asset.get("path") or "",
                "thumb": p05_thumb,
                "score": (p05_decision or {}).get("final_score"),
                "text_score": p05_row.get("text_score"),
                "preview_score": p05_row.get("visual_preview_relevance"),
                "frame_score": p05_row.get("frame_relevance"),
                "vlm_score": p05_row.get("vlm_relevance"),
                "visual_score": _visual_score(p05_decision, p05_asset),
                "route": p05_meta.get("scene_category"),
                "asset_type": p05_meta.get("asset_type"),
                "candidate_count": p05_meta.get("candidate_count"),
                "providers_queried": p05_meta.get("providers_queried"),
                "latency_ms": p05_meta.get("latency_ms"),
                "status": p05_meta.get("status"),
                "ai_used": ai_used,
                "archive_used": archive_used,
                "license": p05_prov.get("license") or "",
                "decision": p05_decision,
                "auto_evaluation": p05_eval,
                "human_label": None,
            },
            "agreement": _asset_key(legacy_asset) == _asset_key(p05_asset),
            "fallback": (p05_decision or {}).get("fallback_reason") or "",
        })

    def _rel(row, arm):
        value = (row[arm]["auto_evaluation"] or {}).get("overall_relevance")
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    def _mean(values):
        values = [v for v in values if v is not None]
        return round(sum(values) / len(values), 4) if values else None

    regressions = 0
    improvements = 0
    for row in rows:
        legacy_rel, p05_rel = _rel(row, "legacy"), _rel(row, "p05")
        if legacy_rel is not None and p05_rel is not None:
            delta = p05_rel - legacy_rel
            row["auto_delta"] = round(delta, 4)
            if delta <= -0.15:
                regressions += 1
            elif delta >= 0.15:
                improvements += 1
        else:
            row["auto_delta"] = None

    p05_assets = [r["p05"]["asset"] for r in rows if r["p05"]["asset"]]
    legacy_assets = [r["legacy"]["asset"] for r in rows if r["legacy"]["asset"]]
    def _count(assets, predicate):
        return sum(1 for a in assets if predicate(a))

    summary = {
        "run_id": args.run_id,
        "generated_at": utcnow(),
        "scenes": len(rows),
        "agreement": sum(1 for r in rows if r["agreement"]),
        "agreement_rate": round(
            sum(1 for r in rows if r["agreement"]) / len(rows), 4
        ) if rows else 0.0,
        "legacy_mean_auto_relevance": _mean([_rel(r, "legacy") for r in rows]),
        "p05_mean_auto_relevance": _mean([_rel(r, "p05") for r in rows]),
        "auto_regressions": regressions,
        "auto_improvements": improvements,
        "p05_no_good": sum(
            1 for r in rows if not (r["p05"]["asset"] or {}).get("path")
            or (r["p05"]["decision"] or {}).get("status") not in (None, "OK", "SELECTED")
        ),
        "p05_auto_no_good": sum(
            1 for r in rows if (_rel(r, "p05") is not None and _rel(r, "p05") < 0.3)
        ),
        "p05_fallbacks": sum(1 for r in rows if r["fallback"]),
        "legacy_ai": _count(legacy_assets, lambda a: str(a.get("source", "")).startswith("ai_")),
        "p05_ai": sum(1 for r in rows if r["p05"].get("ai_used")),
        "legacy_archive": _count(legacy_assets, lambda a: str(a.get("source", "")) in ("wikimedia_commons", "nasa_image_library")),
        "p05_archive": sum(1 for r in rows if r["p05"].get("archive_used")),
        "legacy_elapsed_s": legacy.get("elapsed_s"),
        "p05_elapsed_s": p05.get("elapsed_s"),
        "legacy_errors": legacy.get("errors") or [],
        "p05_errors": p05.get("errors") or [],
        "human_labels": None,
        "auto_evaluation_used": do_eval,
    }

    # Canary attribution manifest (the production manifest flag stays off).
    try:
        from pipeline.media_semantics.attribution import (
            attribution_markdown,
            build_attribution_manifest,
        )
        from pipeline.media_semantics.contracts import (
            Candidate,
            SceneSpec,
            SelectionDecision,
            SelectionStatus,
        )

        decisions = []
        for record in p05.get("decisions") or []:
            winner_raw = json.loads(record.get("winner_json") or "null")
            if not winner_raw:
                continue
            decisions.append(SelectionDecision(
                scene_spec=SceneSpec.from_dict(
                    json.loads(record.get("scene_spec_json") or "{}")
                ),
                status=SelectionStatus(record.get("status") or "NO_GOOD_CANDIDATE"),
                selector_version=record.get("selector_version") or "",
                winner=Candidate.from_dict(winner_raw),
                winner_score=float(record.get("final_score") or 0.0),
                threshold=float(record.get("threshold") or 0.0),
            ))
        manifest = build_attribution_manifest(
            decisions,
            video_id=None,
            channel=p05.get("channel") or "",
            script_title=f"script {p05.get('script_id')}",
        )
        _write_json(run_dir / "attribution_manifest.json", manifest)
        (run_dir / "attribution_manifest.md").write_text(
            attribution_markdown(manifest), encoding="utf-8",
        )
        summary["attribution_manifest"] = {
            "scenes": manifest["summary"]["scenes"],
            "providers": manifest["summary"]["providers"],
            "licenses": manifest["summary"]["licenses"],
            "unknown_license": manifest["summary"]["unknown_license"],
        }
    except Exception as exc:
        summary["attribution_manifest"] = {"error": f"{type(exc).__name__}: {exc}"}

    _write_json(run_dir / "comparison.json", {"summary": summary, "scenes": rows})
    (run_dir / "review_pack.html").write_text(
        _review_html(args.run_id, summary, rows), encoding="utf-8"
    )
    print(json.dumps(summary, ensure_ascii=False))
    return 0


def _review_html(run_id: str, summary: dict, rows: list[dict]) -> str:
    def esc(value):
        import html

        return html.escape("" if value is None else str(value))

    def rel(value):
        return "—" if value is None else f"{value:.2f}"

    cards = []
    for row in rows:
        cards.append(f"""
        <section class="scene">
          <h2>Scene {row['scene_index']} <span class="muted">(script scene {row.get('source_scene_index')})</span></h2>
          <p class="narration">{esc((row.get('narration') or '')[:700])}</p>
          <p class="query">query: <code>{esc(row.get('query_en'))}</code></p>
          <div class="grid">
            <div class="arm">
              <h3>LEGACY</h3>
              <img src="{esc('review_thumbs/' + Path(row['legacy']['thumb']).name if row['legacy']['thumb'] else '')}" alt="legacy">
              <p>{esc(row['legacy']['provider'])} · {esc(row['legacy']['type'])}</p>
              <p>auto relevance: {rel((row['legacy']['auto_evaluation'] or {}).get('overall_relevance'))}</p>
              <p class="muted">{esc(row['legacy']['asset_key'])[:160]}</p>
            </div>
            <div class="arm">
              <h3>P05</h3>
              <img src="{esc('review_thumbs/' + Path(row['p05']['thumb']).name if row['p05']['thumb'] else '')}" alt="p05">
              <p>{esc(row['p05']['provider'])} · {esc(row['p05']['type'])}</p>
              <p>final: {rel(row['p05']['score'])} · text: {rel(row['p05']['text_score'])} · preview: {rel(row['p05']['preview_score'])}</p>
              <p>route: {esc(row['p05']['route'] or '—')} · type: {esc(row['p05']['asset_type'] or '—')} · candidates: {esc(row['p05']['candidate_count'])}</p>
              <p>auto relevance: {rel((row['p05']['auto_evaluation'] or {}).get('overall_relevance'))}</p>
              <p class="muted">{esc(row['p05']['asset_key'])[:160]}</p>
            </div>
          </div>
          <p>agreement: <b>{esc(row['agreement'])}</b> · auto delta: {rel(row.get('auto_delta'))} ·
             fallback: {esc(row['fallback'] or '—')} · archive: {esc(row['p05']['archive_used'])} ·
             AI: {esc(row['p05']['ai_used'])} · latency: {esc(row['p05']['latency_ms'] or '—')} ms · license: {esc(row['p05']['license'] or '—')}</p>
        </section>""")
    cards_html = "\n".join(cards)
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<title>P05 canary review — {esc(run_id)}</title>
<style>
body{{font-family:system-ui,-apple-system,Segoe UI,sans-serif;margin:0;background:#0b0d12;color:#e7eaf0}}
header{{padding:24px 32px;border-bottom:1px solid #232838}}
h1{{margin:0 0 8px;font-size:22px}} h2{{font-size:17px;margin:0 0 8px}}
h3{{font-size:13px;letter-spacing:.08em;color:#9aa4bb;margin:0 0 8px}}
main{{padding:24px 32px;max-width:1180px;margin:0 auto}}
.muted{{color:#8b93a7}} .scene{{border:1px solid #232838;border-radius:12px;padding:18px;margin:0 0 20px;background:#11151d}}
.narration{{color:#c9d2e3;line-height:1.5}} .query code{{color:#8fd3ff}}
.grid{{display:grid;grid-template-columns:1fr 1fr;gap:16px;margin:12px 0}}
.arm{{background:#0b0d12;border:1px solid #1c2130;border-radius:10px;padding:12px}}
.arm img{{width:100%;height:auto;border-radius:8px;background:#000;aspect-ratio:16/9;object-fit:cover}}
</style></head>
<body><header>
<h1>AUTOTUBE — P05 controlled canary</h1>
<div class="muted">run <b>{esc(run_id)}</b> · scenes {summary['scenes']} · agreement {summary['agreement_rate']} ·
legacy↔P05 auto relevance {rel(summary['legacy_mean_auto_relevance'])} → {rel(summary['p05_mean_auto_relevance'])} ·
human_label = null</div>
</header><main>{cards_html}</main></body></html>
"""


# ── CLI ────────────────────────────────────────────────────────────────────
def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--arm", choices=["legacy", "p05", "all", "report"], required=True)
    parser.add_argument("--script-id", type=int, default=1714)
    parser.add_argument("--scenes", default="0,2,10")
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--env-file", default="/root/autotube/.env")
    parser.add_argument("--run-id", default="")
    parser.add_argument("--out-dir", default="")
    parser.add_argument("--media-dir", default="",
                        help="temporary download dir (default /tmp/autotube_p05_canary/<run-id>)")
    parser.add_argument("--no-auto-eval", action="store_true")
    parser.add_argument("--reuse-eval", action="store_true",
                        help="reuse auto-evaluations already present in comparison.json")
    args = parser.parse_args()

    if not args.run_id:
        args.run_id = f"p05-canary-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    if not args.out_dir:
        args.out_dir = str(DEFAULT_EVIDENCE / args.run_id)
    args.out_dir = str(Path(args.out_dir).resolve())
    Path(args.out_dir).mkdir(parents=True, exist_ok=True)

    if args.arm == "report":
        return build_report(args)

    if args.arm == "all":
        child_env = _clean_child_env()
        for arm in ("legacy", "p05"):
            cmd = [
                sys.executable, "-m", "benchmarks.media_semantics.p05_live_canary",
                "--arm", arm,
                "--script-id", str(args.script_id),
                "--scenes", args.scenes,
                "--db", args.db,
                "--env-file", args.env_file,
                "--run-id", args.run_id,
                "--out-dir", args.out_dir,
                "--media-dir", args.media_dir,
            ]
            if args.no_auto_eval:
                cmd.append("--no-auto-eval")
            print(f"[canary] running arm={arm}", flush=True)
            result = subprocess.run(cmd, cwd=str(ROOT), env=child_env)
            if result.returncode != 0:
                print(f"[canary] arm {arm} failed rc={result.returncode}", file=sys.stderr)
                return result.returncode
        return build_report(args)

    return run_arm(args)


if __name__ == "__main__":
    raise SystemExit(main())
