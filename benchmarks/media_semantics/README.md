# P03 media-semantics benchmark

Offline-safe harness for the P03 real-world benchmark. It reads the production
DB read-only, retrieves bounded real provider candidates, scores the same pool
with three selectors, auto-evaluates with a VLM rubric, calibrates thresholds
and writes a static review pack.

```bash
# Full run (network + bounded downloads + VLM)
python3 -m benchmarks.media_semantics.cli \
  --run-id p03-20261024 --stage all --llm-intents \
  --limit 44 --per-category 6 --scrapers-scenes 8 \
  --frame-scenes 8 --max-auto-eval 400

# Report-only from cached decisions (no network, no VLM)
python3 -m benchmarks.media_semantics.cli \
  --run-id p03-20261024 --stage report

# LIVE CANARY (never collected by pytest)
python3 -m benchmarks.media_semantics.live_canary --out /tmp/p03_canary.json
```

Stages: `preflight`, `corpus`, `retrieve`, `previews`, `score`, `frames`,
`evaluate`, `calibrate`, `report`, `all`. Stages are resumable; cached
`corpus.json`, `pools.json`, `previews.json`, `frames.json` and `auto_eval_*.json`
are reused unless `--rebuild-*` / `--refresh-*` is passed.

Runtime artifacts live in `runs/<run_id>/` and are gitignored (large previews,
videos, frames, resized evaluation images). Only JSON/HTML evidence is committed.

Offline tests: `pytest -q tests/test_p03_benchmark.py`. The live canary is a
separate module and is never collected automatically.

## P04 focused benchmark

```bash
# Build cases from the P03 ALL_BAD + control set (offline)
python3 -m benchmarks.media_semantics.p04_cli --run-id p04-20261025 --stage build

# Bounded live archive canary + optional VLM rubric (never collected by pytest)
python3 -m benchmarks.media_semantics.p04_cli \
  --run-id p04-20261025 --stage all --allow-network --vlm \
  --archive-max-requests 120

# Report only from cached run
python3 -m benchmarks.media_semantics.p04_cli --run-id p04-20261025 --stage report
```

Evidence is copied to `evidence/p04-20261025/` (JSON + review pack + compact
thumbnails). Runtime downloads stay under the gitignored `runs/` tree.
