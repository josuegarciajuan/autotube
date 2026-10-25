# ADR-20261025 — P00–P05 engineering closure (canary approved, no default)

* Status: **Accepted — engineering closed** (`AUTOTUBE_SEMANTIC_VISUAL_SELECTION_ENGINEERING_COMPLETE`)
* Date: 2026-10-25
* Authority: external review, P00–P05 = APPROVED / CLOSED
* Readiness: **READY_FOR_CONTROLLED_CANARY** (not `READY_FOR_DEFAULT`)

## Context

P00–P05 delivered a complete, opt-in media-selection stack: semantic retrieval,
visual evidence, calibrated benchmarks, source routing/fallback, and integrated
global selection with two-stage fallback. External review approved every phase.
The next action is operational: a very small production canary, not a default
activation, and **not P06**.

## Decision

1. Close P00–P05 at this baseline. No new feature work (`P06` is not started).
2. Keep every production default unchanged:
   `MEDIA_SELECTOR=legacy`, `P02_VISUAL_MODE=off`, `P04_SOURCE_ROUTER=0`,
   `P05_INTEGRATED=0`, `P04_ATTRIBUTION_MANIFEST=0`.
3. Approve a **controlled canary** of the combined profile
   (`P04_SOURCE_ROUTER=1`, `P05_INTEGRATED=1`, `PREVIEW_VISUAL`, adaptive budget,
   `P05_VISUAL_TOP_N<=8`, visual cache), scoped to one real script / a few real
   scenes, no YouTube upload.
4. Never enable `FULL_VISUAL` globally. It may only be used selectively for
   ambiguous/high-risk scenes through the adaptive path.
5. Require a job scope for any activation that runs inside the shared service:
   `P05_CANARY_JOB_IDS` must contain `AUTOTUBE_JOB_ID` (see
   `pipeline/media_semantics/scoping.py`). An empty allowlist preserves historic
   behavior for offline tools.

## Final architecture

```
SceneSpec
  → SourceRouter (P04: WHERE, category ladder)
  → real sources: stock video/image, Wikimedia, NASA, scientific
  → P01 text score
  → bounded P02 preview/CLIP evidence (PREVIEW_VISUAL)
  → unified score (text + pixels + capped priors − real penalties)
  → global rerank (coherence only near the relevance frontier)
  → real winner
       else stage 2: AI / conceptual / generic (AI must pass the visual gate)
```

Modules: `pipeline/media_semantics/{source_router,sourcing_pipeline,integrated,
unified_scoring,entity_queries,visual_evidence,shadow,attribution,scoping}.py`.

## Benchmark baseline (P03/P05 evidence)

* Frozen P03 corpus: 36 real scenes, auto-evaluation rubric (no invented labels).
* Legacy top-1 mean 0.2694 → P05 `PREVIEW_VISUAL` 0.3472 (+0.0778).
* Relevant rate 0.1667 → 0.25; ALL_BAD mean 0.2759 vs P04 0.2534.
* Regressions vs legacy-good scenes: 1/6; control retention 4/5.
* Latency: TEXT_ONLY 154 ms total; PREVIEW_VISUAL 112.7 s / 36 scenes (3.1 s/scene).
* Adaptive budget skipped 2/36 scenes with no metric change.
* Evidence: `benchmarks/media_semantics/evidence/p05-20261025/`.

## Commits

| Phase | Commit | Note |
|---|---|---|
| P00 | — | discovery/baseline; no code commit (referenced by P01 ADR) |
| P01 | `581755cb` (merge `bfbaf0e8`) | semantic retrieval core |
| P02 | `cb5044a6` (merge `8de3bfe7`) | visual intelligence |
| P03 | `3a06c5a9` (merge `fbd50ed5`) | real benchmark + calibration |
| P04 | `5aa9701b` | source routing + fallback |
| P05 | `8be11698` | integrated selection, rerank, shadow |
| Closure | this ADR + canary runbook | docs/scope only |

## Readiness

* `READY_FOR_CONTROLLED_CANARY`: **yes**.
* `READY_FOR_DEFAULT`: **no**.

## Known debt / limitations

* `FULL_VISUAL` not measured on the frozen corpus (previews only, no local video
  files); do not default it.
* Archive coverage is limited to P04 selected winners; entity queries are
  deterministic phrases, not a knowledge graph.
* Human visual labels are still absent for the P05 winners
  (`human_label = null`); auto-evaluation is an auxiliary signal only.
* Text-only routing can lose metadata-less good assets (P04 control 2/5).
* The canary harness exercises the media-selection path, not a full render.

## Reversibility

No code default changed. Rollback is: stop passing the canary env file /
unset `P04_SOURCE_ROUTER`, `P05_INTEGRATED`, `P05_CANARY_JOB_IDS`; restart only
if the shared service environment was modified. Production stays on legacy.
