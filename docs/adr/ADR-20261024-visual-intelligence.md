# ADR-20261024 — Visual intelligence extension (P02)

## Context

P01 added a provider-agnostic textual/semantic retrieval core. It cannot tell
whether the chosen pixels actually show the requested subject, action or era.
P02 adds visual evidence without replacing P01 and without turning the visual
path on by default.

## Decision

Extend `pipeline/media_semantics/` with a bounded visual funnel:

1. a `SceneSpec`-derived encoder description,
2. provider-declared thumbnail/preview references on `Candidate`,
3. optional CPU-first CLIP/SigLIP visual embedding backends (lazy, no implicit
   model download),
4. a visual cache keyed by asset + representation + backend + model version,
5. a preview-only rerank before any full download,
6. bounded download of the visual shortlist,
7. semantic temporal frame sampling using the existing ffmpeg helpers,
8. multi-frame aggregation with coverage/consistency,
9. an optional structured VLM verifier with strict JSON validation,
10. explicit visual constraints / era penalties and a combined score,
11. provenance and offline replay.

Graceful degradation is mandatory: `FULL_VISUAL`, `EMBEDDING_ONLY`,
`FRAME_EMBEDDING_ONLY`, `TEXT_ONLY`, and the untouched `LEGACY` path. Missing
previews or a failed VLM are unavailable evidence, not rejection.

## Consequences

Positive:
- The selector can reject a textually plausible but visually contradictory asset.
- The funnel avoids `N candidates × full video × VLM` by design.
- Every visual signal is inspectable and replayable offline.
- No production behavior changes unless a channel/operator opts in.

Negative / risks:
- Model weights and thresholds are not calibrated yet.
- Provider preview availability differs; Pixabay video may degrade.
- A VLM/CLIP stage adds latency/cost when enabled; budgets are configurable.
- The local VLM backend is a configurable placeholder until a model is provisioned.

## Reversibility

`P02_VISUAL_MODE=off` (default) restores P01 exactly. `MEDIA_SELECTOR=legacy`
restores the pre-P01 selector. No database migration is required; visual
provenance is JSON in existing columns.
