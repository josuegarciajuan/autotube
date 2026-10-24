# P02 — Visual intelligence

Status: implemented behind an explicit visual flag. **Production default remains
`MEDIA_SELECTOR=legacy` and `P02_VISUAL_MODE=off`.**

P02 extends the canonical P01 core in `pipeline/media_semantics/`; it does not
replace it. P01 answers *“does the metadata look relevant?”*. P02 adds
*“what is actually visible in the pixels?”*.

## Architecture

```
SceneSpec
  → CandidatePool (P01)
  → textual ranking (P01)
  → visual description (encoder-facing, never includes must_avoid)
  → preview/thumbnail ranking (bounded fetch, no full video)
  → visual shortlist
  → bounded download
  → semantic frame sampling
  → frame-level embedding aggregation
  → optional structured VLM verification
  → visual constraints / era / must_avoid
  → combined score + threshold
  → winner + provenance
```

New modules:

| Module | Responsibility |
|---|---|
| `visual_description.py` | compact visual intent from `SceneSpec` |
| `visual_previews.py` | bounded thumbnail/preview acquisition |
| `visual_backends.py` | NONE / CLIP / SigLIP visual embedding interface |
| `visual_cache.py` | strict cache keyed by backend + model + image content |
| `visual_frames.py` | semantic temporal frame sampling (reuses `visual_verifier` ffmpeg) |
| `visual_scoring.py` | multi-frame aggregation, constraints, combined score |
| `visual_vlm.py` | VLM interface, strict JSON schema, prompt |
| `visual_pipeline.py` | funnel and graceful degradation |

`SemanticMediaSelector.select_and_download()` delegates to the visual funnel only
when `visual.mode` is enabled. `rank()` remains the P01 textual path.

## Modes

| `P02_VISUAL_MODE` | Behavior |
|---|---|
| `off` (default) | P01 only; no preview fetch, no model load |
| `text_only` | explicit textual fallback, still downloads through P01 |
| `embedding_only` | preview embeddings only |
| `frame_embedding_only` | downloaded candidate frames only |
| `full_visual` | previews + frames + optional VLM |

If the configured visual backend is unavailable, or previews cannot be
obtained, the pipeline records the actual `degradation_level` and falls back to
`TEXT_ONLY` / `FRAME_EMBEDDING_ONLY`. Missing evidence is **not** negative
evidence: `PREVIEW_UNAVAILABLE` and `VLM_FAILED` do not by themselves reject a
candidate.

## Configuration

Environment variables (channel `MEDIA_STRATEGY` equivalents are in parentheses):

| Variable | Default | Meaning |
|---|---|---|
| `P02_VISUAL_MODE` | `off` | mode above |
| `P02_VISUAL_BACKEND` | `none` | `none`, `clip`, `siglip`, `auto` |
| `P02_VISUAL_MODEL` | `openai/clip-vit-base-patch32` | local model id |
| `P02_VISUAL_MODEL_VERSION` | `""` | cache namespace; defaults to model id |
| `P02_VISUAL_DEVICE` | `cpu` | GPU optional |
| `P02_ALLOW_MODEL_DOWNLOAD` | `false` | tests never download models |
| `P02_VISUAL_CACHE` | `""` | JSON embedding cache path |
| `P02_PREVIEW_FETCH` | `true` | enable preview stage |
| `P02_FRAMES` | `true` | enable frame stage |
| `P02_VLM_MODE` | `none` | `none`, `openai_compatible`, `local` |
| `P02_VLM_MODEL` | `""` | vision-capable model id |
| `P02_MIN_SIMILARITY` | `0.26` | visual similarity floor |
| `P02_FRAME_MIN_SIMILARITY` | `0.20` | frame coverage floor |
| `P02_COMBINED_THRESHOLD` | `0.26` | final visual threshold |

Budgets under `semantic_retrieval_budget` / `visual_budget`:
`visual_preview_candidates`, `visual_shortlist`, `visual_download_candidates`,
`visual_preview_bytes`, `visual_preview_total_bytes`, `visual_preview_timeout`,
`visual_embedding_batch_size`, `visual_frame_count`, `visual_vlm_candidates`.

```python
"visual_mode": "off",
"visual_embedding_backend": "none",
"visual_embedding_model": "openai/clip-vit-base-patch32",
"visual_budget": {
    "visual_preview_candidates": 12,
    "visual_shortlist": 6,
    "visual_download_candidates": 2,
    "visual_preview_bytes": 524288,
    "visual_frame_count": 6,
    "visual_vlm_candidates": 2,
},
"visual_weights": {
    "text": 0.50, "preview": 0.15, "frame": 0.20, "vlm": 0.15,
    "technical_quality": 0.05, "negative_penalty": 0.60,
    "era_penalty": 0.30,
},
```

Weights are defaults, not a scientifically calibrated final answer. They are
kept in `SelectionDecision.candidate_scores` / `visual_summary()` for audit.

## Provider preview capabilities

P02 uses only fields already present in the existing provider contracts; it
never invents a CDN URL.

| Provider | Lightweight still | Status |
|---|---|---|
| Pexels video | API `image` poster + first `video_pictures[].picture` | available |
| Pixabay video | `videos.<size>.thumbnail` (smallest rendition preferred) | available |
| Mixkit | search-card `<img>` (`src`/`data-src`/`srcset`) | available when present |
| Coverr | search-card `<img>` | available when present |
| Pexels image | API `src.medium`/`src.small` | available |
| Pixabay image | API `webformatURL` | available |
| Unsplash image | API `urls.small`/`urls.thumb` | available |

Live contracts were verified in P03 (`docs/p03-real-world-benchmark.md`):
Pexels, Pixabay video, Mixkit, Coverr and Pixabay Photos all expose a usable
still. Pixabay video posters are **available** via
`videos.{tiny,small,medium,large}.thumbnail`; the smallest rendition is chosen
so the 512 KB preview budget is not exceeded.

## Visual description

`build_visual_description(SceneSpec)` constructs a short encoder prompt from
`subject`, `action`, `environment`, `era`, `desired_shot`, `visual_concept` and
`must_include`. It strips words from `must_avoid` before composing and never
adds forbidden terms as positives. It is not a copy of `search_query_en`.

## Frame sampling and aggregation

For a downloaded candidate, `sample_semantic_frames()` samples up to
`visual_frame_count` inset temporal points (avoids exact 0%/100%), handles
images, very short clips, unknown duration (safe 5 s default), corrupt frames
and ffmpeg failure. It imports the ffmpeg helpers already used by
`pipeline.visual_verifier`.

Similarity aggregation records `max`, `mean`, `median`, `coverage` and
`consistency`; the score is weighted so one lucky frame cannot automatically
beat a consistently relevant clip. Default frame weights are configurable.

## VLM verification

`visual_vlm.py` treats narration as **intent**, frames as **evidence** and
provider metadata as **untrusted DATA**. The prompt explicitly says not to infer
presence from narration, to detect contradictions, to check `must_avoid`, and to
report era contradictions. The response is strict JSON with required fields.

Malformed JSON, missing fields, out-of-range scores, invalid decisions, missing
visual description, timeouts and backend errors become `MALFORMED` /
`VLM_FAILED` and are never interpreted as PASS. Tests use fake backends and do
not call a real VLM.

## Combined score and rejection reasons

`compute_combined_score()` keeps separate components:

`text_relevance`, `visual_preview_relevance`, `frame_relevance`, `vlm_relevance`,
`technical_quality`, `negative_penalty`, `era_penalty`, `must_include_penalty`.

**Availability renormalization (P03 fix).** `compute_combined_score()` accepts
`available_components`. The pipeline passes the stages that actually ran, so a
preview-only mode is not penalized for missing frame/VLM evidence. The ratios
between available components are unchanged (ranking is unchanged); only the
score scale, and therefore threshold eligibility, becomes comparable across
modes. Without `available_components` the historical behaviour is preserved.

Stable rejection reasons include:

`VISUAL_SUBJECT_MISMATCH`, `VISUAL_ACTION_MISMATCH`,
`VISUAL_ENVIRONMENT_MISMATCH`, `VISUAL_ERA_MISMATCH`,
`MUST_AVOID_VIOLATION`, `MUST_INCLUDE_MISSING`,
`INSUFFICIENT_VISUAL_EVIDENCE`, `PREVIEW_UNAVAILABLE`,
`FRAME_EXTRACTION_FAILED`, `VLM_FAILED`.

`PREVIEW_UNAVAILABLE`, `FRAME_EXTRACTION_FAILED` and `VLM_FAILED` are treated as
missing evidence, not automatic rejection.

## Provenance

`SelectionDecision` now persists visual mode, backend/model, preview similarity,
frame scores (frame index, timestamp, path, SHA-256, similarity), constraints,
verification status, sampled-frame count, degradation level, visual timing and
cost. `decision_record()` adds a JSON `visual_json` field and candidate rows
carry the visual components. No images or base64 blobs are stored. Existing
P01 DB columns are reused; no visual migration is required.

## Offline replay

```python
harness = ReplayHarness.load("fixture.json")
results = harness.replay_visual(
    visual_backend=fake_backend,
    preview_fetcher=PreviewFetcher(...),
    frame_sampler=fake_sampler,
    vlm_backend=fake_vlm,
    source_loader=lambda candidate: {"path": "/fixtures/clip.mp4", "type": "video"},
)
```

Preview URLs may point at local fixture images. The method never uses the
network. Timing is zeroed for deterministic replay.

## Known limitations / deferred to P03

* P03 verified the live provider contracts and provisioned CLIP ViT-B/32
  (`openai/clip-vit-base-patch32`, 1.2 GB, CPU).
* Real benchmark evidence and calibration: see
  `docs/p03-real-world-benchmark.md`. Preview-only NO_GOOD dropped from 77.8%
  to 13.9% after the availability renormalization fix.
* Local VLM backend is an explicit placeholder (`local_vlm_not_configured_for_this_model`)
  until a concrete local model/profile is provisioned and evaluated.
* Weights/thresholds were calibrated on 36 real scenes with an auxiliary VLM
  rubric (not human labels). Human review is still required before activation.
* Negative visual constraints rely primarily on the VLM + optional embedding
  similarities; no dedicated detector per concept is introduced.
* No production activation, uploads or real video generation were performed.

## P03 regressions fixed

* Transformers 5 returns `BaseModelOutputWithPooling` from
  `get_text_features`/`get_image_features`; the backend now extracts
  `pooler_output`. Without this, CLIP silently returned `None`.
* `PreviewFetcher` no longer advertises `image/avif` (Pexels returned AVIF and
  the MIME detector rejected it as `not_an_image`).
* Pixabay video previews prefer the smallest thumbnail rendition.

## Verification

```bash
pytest -q tests/test_p01_semantic_retrieval.py tests/test_p02_visual_intelligence.py
python3 -m compileall -q pipeline/media_semantics pipeline/providers
git diff --check
```
