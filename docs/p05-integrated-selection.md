# P05 — integrated selection, global rerank and two-stage fallback

Status: **implemented, opt-in, candidate for controlled canary.**
Production defaults remain unchanged:

```
MEDIA_SELECTOR=legacy
P02_VISUAL_MODE=off
P04_SOURCE_ROUTER=0
P05_INTEGRATED=0
P04_ATTRIBUTION_MANIFEST=0
```

This phase does not start a parallel media stack: it joins P01 text, P02
visual evidence and P04 routing/fallback into one candidate universe and one
global ordering.

## 1. Decision flow

```
scene spec
  → SourceRouter (WHERE to search)
  → real sources: stock video/image, archive, scientific
  → P01 text score per candidate
  → bounded P02 preview/frame/VLM evidence
  → unified score (text + pixels + bounded routing priors − real penalties)
  → global rerank (coherence only near the relevance frontier)
  → real winner
       else stage 2: AI / conceptual / generic
            AI must pass the same visual gate before selection
```

Modules:

| Module | Responsibility |
|---|---|
| `source_router.py` | category/route (P04) |
| `sourcing_pipeline.py` | retrieval, stage budgets, download, integrated hook |
| `integrated.py` | unified universe, global rerank, two-stage fallback, AI validation |
| `unified_scoring.py` | explicit components, normalisation, prior/coherence caps, profiles |
| `entity_queries.py` | entity/era-aware archive queries |
| `visual_evidence.py` | P02 preview/CLIP adapter (fail-open) |
| `shadow.py` | observational legacy-vs-P05 comparison |
| `attribution.py` | per-video manifest |

## 2. Unified score

```
available = {text} ∪ observed{preview, frame, vlm, technical_quality}
base      = Σ wᵢ·componentᵢ / Σ wᵢ      (i ∈ available)
priors    = clamp(source + historical + asset_type, −cap, +cap)   cap=0.04
coherence = min(0.05, provider + style + shot)
final     = clamp(base + priors − coherence − real_constraints − duplicate − fallback)
```

* `missing evidence != negative evidence`: an unobserved stage is `None` and is
  excluded from the denominator. An observed mismatch is an explicit penalty.
* P01's internal provider prior is **not** added again; routing priors are a
  separate, capped signal. No double counting.
* A clearly more relevant asset cannot be overturned by priors/coherence.
* Coherence is applied only to candidates within `0.08` relevance of the best.

## 3. Two-stage fallback and AI validation

1. Real assets are ranked together. If one clears `P05_REAL_THRESHOLD`, AI and
   conceptual candidates are never considered.
2. Only after a real failure do stage-2 candidates enter. An AI image must pass
   the same visual evidence gate (`preview/frame/VLM`) and the score threshold;
   a prompt alone is never accepted as proof of pixels.

## 4. Profiles and adaptive budget (measured)

| Profile | What runs | Frozen P03 corpus (36 scenes) |
|---|---|---|
| `TEXT_ONLY` | P01 only | 0.154 s total (4 ms/scene) |
| `PREVIEW_VISUAL` | P01 + one still + CLIP | 112.7 s total (3.1 s/scene) |
| `FULL_VISUAL` | P01 + preview + frames + optional VLM | not run offline (no local video files). P03 measured 1.44 s for 6 frames and 1.65 s per VLM call. |

Adaptive budget skips visual inspection when text is strong and has a clear
margin (`text ≥ 0.28`, margin ≥ 0.08). On the frozen corpus that skipped 2/36
scenes with no metric change. Profile choice and thresholds are configurable;
`FULL_VISUAL` is not recommended as a default.

## 5. Entity-aware archive queries

When the SceneSpec carries a subject/entity, location, person or era, archive
queries use those instead of generic b-roll phrasing, e.g.
`Palmyra ruins 2nd century`, `Palmyra Syrian desert`. This is a deterministic
phrase builder, not a knowledge graph; explicit `scene_spec.extra` hints win.

## 6. Failure handling

`NO_CANDIDATES`, `NO_GOOD_REAL_ASSET`, `ARCHIVE_FAILURE`, `AI_UNAVAILABLE`,
`AI_BAD_OUTPUT`, `PREVIEW_FAILURE`, `FRAME_FAILURE`, `VLM_FAILURE`,
`DUPLICATE_ONLY`, `COHERENCE_CONFLICT`, `LICENSE_UNKNOWN`,
`DOWNLOAD_FAILURE` degrade to the next stage or fallback; none aborts the video
by themselves.

## 7. Configuration

| Variable | Default | Meaning |
|---|---|---|
| `P05_INTEGRATED` | `0` | opt-in integrated selection |
| `P05_VISUAL_PROFILE` | `PREVIEW_VISUAL` | `TEXT_ONLY` / `PREVIEW_VISUAL` / `FULL_VISUAL` |
| `P05_VISUAL_TOP_N` | `8` | text-top candidates inspected visually |
| `P05_MAX_VISUAL_CANDIDATES` | `14` | hard visual budget per scene |
| `P05_REAL_THRESHOLD` | `0.26` | real-asset eligibility |
| `P05_AI_THRESHOLD` | `0.26` | generated-asset eligibility |
| `P05_AI_VALIDATION_REQUIRED` | `1` | AI must pass visual evidence |
| `P05_ADAPTIVE_BUDGET` | `1` | skip visual when text is unambiguous |
| `P05_ENTITY_ARCHIVE_QUERIES` | `1` | entity/era archive queries |
| `P04_ATTRIBUTION_MANIFEST` | `0` | write per-video attribution manifest |

## 8. Evidence and tests

* Integrated benchmark (frozen P03 corpus + P04 archive winners):
  `benchmarks/media_semantics/evidence/p05-20261025/`.
* Shadow sample: 36 real scenes, `shadow.jsonl` / `shadow_summary.json`.
* AI canary: 2/3 generations, visual scores 0.286 and 0.303 (no publication).
* Tests: `pytest -q tests/test_p05_integrated_selection.py` (30 offline tests).
