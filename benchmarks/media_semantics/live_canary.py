"""LIVE CANARY — never collected by pytest.

Minimal real calls:
  1. Pexels / Pixabay / Mixkit / Coverr search contract.
  2. Pixabay video poster availability.
  3. CLIP model load + one text/image embedding.
  4. One OpenAI vision call (low detail) if configured.

Usage:
    python3 -m benchmarks.media_semantics.live_canary --out /tmp/p03_canary.json
"""

from __future__ import annotations

import argparse
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

from benchmarks.media_semantics import retrieval
from benchmarks.media_semantics import scoring

MODEL_ID = "openai/clip-vit-base-patch32"


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default="/tmp/p03_live_canary.json")
    parser.add_argument("--env-file", default=str(retrieval.DEFAULT_ENV))
    args = parser.parse_args()
    load_dotenv(args.env_file)
    env = retrieval.load_provider_env(args.env_file)
    payload = {"timestamp": utcnow(), "providers": {}, "model": {}, "vlm": {}}

    # 1-2. Provider contracts (one query each).
    registry = retrieval.ProviderRegistry(env)
    ledger = retrieval.RequestLedger()
    for name, provider in [
        ("pexels", registry.video.get("pexels")),
        ("pixabay", registry.video.get("pixabay")),
        ("mixkit", registry.video.get("mixkit")),
        ("coverr", registry.video.get("coverr")),
    ]:
        if provider is None:
            payload["providers"][name] = {"status": "UNAVAILABLE_NO_KEY"}
            continue
        started = time.time()
        try:
            page = provider.search_page("desert landscape", 4, 30, page=1, per_page=10)
            assets = list(getattr(page, "assets", []) or [])
            ledger.record(name, requests=1)
            sample = assets[0] if assets else None
            payload["providers"][name] = {
                "status": "OK" if assets else "EMPTY",
                "assets": len(assets),
                "sample": {
                    "duration": getattr(sample, "duration", None) if sample else None,
                    "resolution": getattr(sample, "resolution", None) if sample else None,
                    "page_url": getattr(sample, "page_url", "") if sample else "",
                    "thumbnail_url": getattr(sample, "thumbnail_url", "") if sample else "",
                    "preview_status": getattr(sample, "preview_status", "") if sample else "",
                    "title": getattr(sample, "title", "") if sample else "",
                    "tags": (getattr(sample, "tags", []) or [])[:8] if sample else [],
                },
                "latency_ms": round((time.time() - started) * 1000, 1),
            }
        except Exception as exc:
            payload["providers"][name] = {"status": "ERROR", "error": str(exc)[:300]}

    # 3. Model load + embeddings.
    try:
        from pipeline.media_semantics.config import VisualConfig
        from pipeline.media_semantics.visual_backends import build_visual_backend

        config = VisualConfig(
            mode="embedding_only", backend="clip", model=MODEL_ID,
            model_version=MODEL_ID, allow_model_download=False,
        )
        started = time.time()
        backend = build_visual_backend(config)
        texts = backend.encode_texts(["a desert landscape at sunset"])
        load_s = time.time() - started
        payload["model"] = {
            "status": "OK" if texts else "FAILED",
            "model": MODEL_ID,
            "dim": len(texts[0]) if texts else None,
            "load_plus_text_s": round(load_s, 2),
            "describe": backend.describe(),
        }
    except Exception as exc:
        payload["model"] = {"status": "ERROR", "error": str(exc)[:300]}

    # 4. One VLM call, low detail, on the first available project image.
    vlm = scoring.build_vlm_backend(env)
    image = next(
        (p for p in Path("/root/autotube/output/images").glob("*.jpg") if p.is_file()),
        None,
    )
    if vlm is None:
        payload["vlm"] = {"status": "UNAVAILABLE_NO_KEY"}
    elif image is None:
        payload["vlm"] = {"status": "UNAVAILABLE_NO_IMAGE"}
    else:
        try:
            from benchmarks.media_semantics.scoring import _auto_eval_vlm
            from benchmarks.media_semantics.corpus import build_scene_spec, Block

            block = Block(
                script_id=0, video_id=None, channel="canary", title="canary",
                block_idx=0, narration="un desierto al atardecer",
                description="Un desierto al atardecer", query_en="desert sunset",
                media_type="image", duration=5, category="desert_geology",
            )
            spec = build_scene_spec(block, {"subject": "desert", "action": "glowing", "environment": "desert", "source": "deterministic"})
            parsed, usage = _auto_eval_vlm(vlm, spec, str(image))
            from pipeline.media_semantics.visual_vlm import verify_with_backend
            from benchmarks.media_semantics.scoring import _resize_evidence

            resized = _resize_evidence(str(image), Path("/tmp/p03_live_canary_images"))
            production = verify_with_backend(
                vlm,
                spec,
                narration=spec.narration_fragment,
                frames=[resized],
                provider_metadata={"title": "canary", "tags": ["canary"], "provider": "local"},
                timeout=45.0,
            )
            payload["vlm"] = {
                "status": "OK",
                "model": vlm.model,
                "rubric_parsed": parsed,
                "usage": usage,
                "production_verifier": production.to_dict(),
            }
        except Exception as exc:
            payload["vlm"] = {"status": "ERROR", "error": str(exc)[:300]}

    payload["budget"] = ledger.to_dict()
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
