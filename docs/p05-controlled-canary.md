# P05 controlled canary — runbook

Status: approved by the P00–P05 closure (`ADR-20261025-p00-p05-engineering-closure`).
This is **not** a default activation and **not** P06.

## 1. Profile

`config/canary/p05-preview.profile` is the only approved profile:

```
MEDIA_SELECTOR=legacy
P04_SOURCE_ROUTER=1
P05_INTEGRATED=1
P05_VISUAL_PROFILE=PREVIEW_VISUAL
P05_VISUAL_TOP_N=8
P05_MAX_VISUAL_CANDIDATES=12
P05_ADAPTIVE_BUDGET=1
P04_AI_ENABLED=0
P04_AI_MAX_PER_VIDEO=0
P02_VISUAL_CACHE_ENABLED=1
P05_CANARY_JOB_IDS=<scope-token>
```

`FULL_VISUAL` is **never** enabled globally.

## 2. Scope mechanism

`pipeline/media_semantics/scoping.py` narrows P04/P05 to the jobs listed in
`P05_CANARY_JOB_IDS` when that variable is non-empty. The API injects
`AUTOTUBE_JOB_ID=<job_id>` into every long-form worker subprocess
(`api/services/generation_service.py`), so an in-service canary is:

```
P04_SOURCE_ROUTER=1
P05_INTEGRATED=1
P05_CANARY_JOB_IDS=15374
```

Any other job of the same service stays legacy. An empty allowlist does not
narrow (backwards compatible for offline tools).

## 3. Isolated media-selection canary (used for the closure)

```bash
python3 -m benchmarks.media_semantics.p05_live_canary --arm all \
  --script-id 1714 --scenes 0,2,10 --run-id p05-canary-20261025
```

* Reads a real script from `autotube.db` (read-only).
* Runs `legacy` first (control), then `p05`, each in its own process,
  `OUTPUT_DIR` and scope token.
* Downloads media to a temporary run directory, never uploads, never writes a
  `generation_jobs`/`videos` row.
* Emits `arm_legacy.json`, `arm_p05.json`, `comparison.json`,
  `review_pack.html`, `review_thumbs/` under
  `benchmarks/media_semantics/evidence/<run-id>/`.
* `human_label` stays `null`; auto-evaluation is auxiliary and requires
  `OPENAI_API_KEY` (disable with `--no-auto-eval`).

## 4. Rollback

* Isolated harness: nothing to roll back; the process exits and production is
  untouched.
* In-service scope: unset `P04_SOURCE_ROUTER`, `P05_INTEGRATED` and
  `P05_CANARY_JOB_IDS` (or remove the drop-in) and `systemctl daemon-reload`.
  Never restart other services. Running workers survive a panel restart
  (`KillMode=process`).
* Verify defaults afterwards:

```bash
python3 - <<'PY'
import os
from pipeline.media_semantics.sourcing_pipeline import (
    SourcingConfig, p04_enabled_from_env)
print("P04=", p04_enabled_from_env(), "integrated=", SourcingConfig.from_env().integrated)
PY
```

Expected: `P04= False integrated= False`.
