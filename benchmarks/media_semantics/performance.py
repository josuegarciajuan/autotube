"""P03-M — measured latency / throughput / cost and 20-minute extrapolation."""

from __future__ import annotations

import json
import resource
import time
from pathlib import Path
from typing import Any

from pipeline.media_semantics.visual_frames import sample_semantic_frames


def measure_embedding_throughput(
    backend: Any,
    image_paths: list[str],
    texts: list[str],
    *,
    repeats: int = 3,
) -> dict[str, Any]:
    image_paths = [p for p in image_paths if Path(p).is_file()][:32]
    texts = texts[:32]
    text_times: list[float] = []
    image_times: list[float] = []
    for _ in range(max(1, repeats)):
        started = time.time()
        backend.encode_texts(texts)
        text_times.append(time.time() - started)
        if image_paths:
            started = time.time()
            backend.encode_images(image_paths)
            image_times.append(time.time() - started)
    best_text = min(text_times) if text_times else 0.0
    best_image = min(image_times) if image_times else 0.0
    return {
        "texts": len(texts),
        "images": len(image_paths),
        "text_seconds_best": round(best_text, 3),
        "image_seconds_best": round(best_image, 3),
        "texts_per_second": round(len(texts) / best_text, 2) if best_text > 0 else None,
        "images_per_second": round(len(image_paths) / best_image, 2) if best_image > 0 else None,
        "maxrss_mb": round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 1),
    }


def measure_frame_latency(frames: dict[str, dict[str, Any]], *, repeats: int = 2) -> dict[str, Any]:
    asset = None
    for items in (frames or {}).values():
        for value in items.values():
            if value.get("type") == "video" and Path(value.get("path", "")).is_file():
                asset = value
                break
        if asset:
            break
    if not asset:
        return {"status": "NO_VIDEO", "frame_count": 0}
    samples: list[float] = []
    frame_counts: list[int] = []
    for _ in range(max(1, repeats)):
        started = time.time()
        result = sample_semantic_frames(asset, max_frames=6)
        samples.append(time.time() - started)
        frame_counts.append(len(result.frames))
        result.cleanup()
    best = min(samples)
    best_count = frame_counts[samples.index(best)] if frame_counts else 0
    return {
        "status": "OK",
        "frame_count": best_count,
        "best_seconds": round(best, 3),
        "per_frame_ms": round((best / best_count) * 1000, 2) if best_count else None,
        "maxrss_mb": round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 1),
    }


def typical_scenes_per_video(db_path: str | Path) -> dict[str, Any]:
    import sqlite3

    path = Path(db_path)
    if not path.is_file():
        return {"status": "NO_DB"}
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        row = con.execute(
            """
            SELECT AVG(cnt), MIN(cnt), MAX(cnt), COUNT(*)
              FROM (SELECT video_id, COUNT(*) AS cnt
                      FROM video_scenes
                     GROUP BY video_id)
            """
        ).fetchone()
    finally:
        con.close()
    if not row or row[0] is None:
        return {"status": "NO_DATA"}
    return {
        "average_scenes_per_video": round(float(row[0]), 2),
        "min": int(row[1]),
        "max": int(row[2]),
        "videos_with_scenes": int(row[3]),
    }


def build_performance_report(
    *,
    run_dir: Path,
    db_path: str | Path,
    decisions: list[dict[str, Any]],
    auto_eval: list[dict[str, Any]],
    backend: Any,
    previews: dict[str, dict[str, Any]],
    frames: dict[str, dict[str, Any]],
    ledger: dict[str, Any],
) -> dict[str, Any]:
    model = _load(run_dir / "model_identity.json")
    preview_times: list[float] = []
    preview_bytes: list[int] = []
    preview_ok = 0
    for scene in previews.values():
        for value in scene.values():
            if value.get("status") == "PREVIEW_AVAILABLE":
                preview_ok += 1
                preview_bytes.append(int(value.get("bytes", 0) or 0))
                preview_times.append(float(value.get("timing_ms", 0) or 0))
    image_paths = [
        v.get("path") for scene in previews.values() for v in scene.values()
        if v.get("status") == "PREVIEW_AVAILABLE" and v.get("path")
    ][:32]
    texts = [
        f"{d.get('category')} documentary scene {i}" for i, d in enumerate(decisions[:32])
    ]
    throughput = measure_embedding_throughput(backend, image_paths, texts)
    frame_latency = measure_frame_latency(frames)
    visual_latencies = [
        float((d.get("semantic_visual") or {}).get("latency_ms", 0) or 0)
        for d in decisions
    ]
    vlm_latencies = [
        float(r.get("timing_ms", 0) or 0)
        for r in auto_eval
        if (r.get("auto_evaluation") or {}).get("status") == "OK"
    ]
    vlm_prompt = sum((r.get("usage") or {}).get("prompt_tokens", 0) for r in auto_eval)
    vlm_completion = sum((r.get("usage") or {}).get("completion_tokens", 0) for r in auto_eval)
    frame_total = sum(
        int(((d.get("semantic_visual") or {}).get("frame_meta") or {}).get("frames", 0) or 0)
        for d in decisions
    )
    ledger = dict(ledger or {})
    ledger["frames"] = frame_total
    downloads = int(ledger.get("downloads", 0) or 0)
    video_bytes = int(ledger.get("bytes", 0) or 0) - sum(preview_bytes)
    avg_video_bytes = video_bytes / downloads if downloads else 0.0
    avg_preview_bytes = sum(preview_bytes) / len(preview_bytes) if preview_bytes else 0.0
    scenes_stats = typical_scenes_per_video(db_path)
    scenes_per_video = scenes_stats.get("average_scenes_per_video") or 60.0

    # Per-scene production P02 defaults.
    preview_candidates = 12
    download_candidates = 2
    frames_per_candidate = 6
    vlm_candidates = 2
    preview_ms = sum(preview_times) / len(preview_times) if preview_times else 0.0
    frame_per_frame_ms = frame_latency.get("per_frame_ms") or 0.0
    visual_ms = sum(visual_latencies) / len(visual_latencies) if visual_latencies else 0.0
    vlm_ms = sum(vlm_latencies) / len(vlm_latencies) if vlm_latencies else 0.0

    # Formula: scenes × (preview fetches + frame extraction + scoring + VLM),
    # plus model load once and network bytes.
    est_preview_s = (preview_ms / 1000.0) * preview_candidates * scenes_per_video
    est_frame_s = (
        (frame_per_frame_ms / 1000.0) * frames_per_candidate * download_candidates
        * scenes_per_video
    )
    est_scoring_s = (visual_ms / 1000.0) * scenes_per_video
    est_vlm_s = (vlm_ms / 1000.0) * vlm_candidates * scenes_per_video
    est_total_s = est_preview_s + est_frame_s + est_scoring_s + est_vlm_s
    est_preview_bytes = avg_preview_bytes * preview_candidates * scenes_per_video
    est_video_bytes = avg_video_bytes * download_candidates * scenes_per_video
    vlm_input_cost = (vlm_prompt / 1_000_000.0) * 0.15 if vlm_prompt else 0.0
    vlm_output_cost = (vlm_completion / 1_000_000.0) * 0.60 if vlm_completion else 0.0

    return {
        "model_load": {
            "processor_s": model.get("processor_load_s"),
            "model_s": model.get("model_load_s"),
            "total_s": model.get("total_load_s"),
            "parameters_m": model.get("parameters_m"),
            "cache_bytes": model.get("cache_bytes"),
        },
        "embedding_throughput": throughput,
        "preview": {
            "count_ok": preview_ok,
            "bytes_mean": round(avg_preview_bytes, 1),
            "latency_mean_ms": round(preview_ms, 2),
            "latency_p90_ms": round(_percentile(preview_times, 90), 2) if preview_times else None,
        },
        "frame_extraction": frame_latency,
        "visual_scoring": {
            "mean_ms": round(visual_ms, 2),
            "total_ms": round(sum(visual_latencies), 2),
        },
        "vlm": {
            "calls": len(vlm_latencies),
            "latency_mean_ms": round(vlm_ms, 2),
            "prompt_tokens": vlm_prompt,
            "completion_tokens": vlm_completion,
            "approx_cost_usd": round(vlm_input_cost + vlm_output_cost, 4),
            "pricing_note": "gpt-4o-mini public list price at measurement time; approximate",
        },
        "peak_rss_mb": round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 1),
        "network": ledger,
        "typical_video": scenes_stats,
        "estimate_20min_video": {
            "scenes_per_video": scenes_per_video,
            "formula": (
                "scenes × (previews×preview_latency + downloads×frames×frame_latency "
                "+ visual_scoring_latency + vlm_candidates×vlm_latency); "
                "bytes = scenes × (previews×avg_preview_bytes + downloads×avg_video_bytes)"
            ),
            "preview_seconds": round(est_preview_s, 1),
            "frame_extraction_seconds": round(est_frame_s, 1),
            "visual_scoring_seconds": round(est_scoring_s, 1),
            "vlm_seconds": round(est_vlm_s, 1),
            "total_seconds_excluding_downloads": round(est_total_s, 1),
            "preview_bytes": int(est_preview_bytes),
            "video_bytes": int(est_video_bytes),
            "total_bytes": int(est_preview_bytes + est_video_bytes),
        },
    }


def _load(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, int(round((pct / 100.0) * (len(ordered) - 1))))
    return ordered[index]
