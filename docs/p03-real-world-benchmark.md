# P03 — Real-world materialization, benchmark and calibration

Status: **candidate for external review**. Production defaults are unchanged:
`MEDIA_SELECTOR=legacy`, `P02_VISUAL_MODE=off`. P03 does **not** activate
semantic or visual selection.

Run: `benchmarks/media_semantics/runs/p03-20261024/` (runtime assets gitignored;
JSON/HTML evidence committed).

## 1. What was actually run

| Item | Result |
|---|---|
| Corpus | 36 real scenes from `scripts.bloques_json` (canal2/3/4/5) |
| Media types | 15 image, 21 video |
| Categories | action, animals, city, desert/geology, history, medical, nature, other, people, space/science, technology |
| Era-relevant scenes | 8 (with benchmark-injected historical `must_avoid`) |
| Candidate pools | one frozen pool per scene, shared by all three arms |
| Visual backend | CLIP ViT-B/32 (`openai/clip-vit-base-patch32`), CPU |
| Provider canary | Pexels, Pixabay, Mixkit, Coverr, Pixabay Photos — all reachable |
| VLM | OpenAI `gpt-4o-mini`, real calls (rubric + production verifier canary) |
| Network | 134 requests, 239.3 MB, 16 video downloads, 432 previews, 96 frames |
| VLM usage | 310 calls, 1.03 M prompt tokens, 28.5 k completion tokens, ≈$0.17 |

No uploads, publications, credential changes, bulk downloads or production
generation were performed.

## 2. Visual model

| Field | Value |
|---|---|
| Model | `openai/clip-vit-base-patch32` |
| Version | CLIP ViT-B/32 |
| Parameters | 151.28 M |
| Embedding dim | 512 |
| Device / dtype | CPU / float32 |
| Cache | 1.214 GB (`~/.cache/huggingface/hub/models--openai--clip-vit-base-patch32`) |
| Cold load | 10.38 s, ~861 MB RSS |
| Warm load | 0.89 s |
| Throughput | 9.15 images/s, 122.68 texts/s |
| Why selected | CPU-viable, 1.2 GB, 512-d joint text/image space, already the P02 default model id, no new dependency (`torch`/`transformers` already installed) |

No GPU is present on the host (`torch.cuda.is_available() == False`).

## 3. Provider live contracts

Minimal one-query canaries (`provider_canary.json`, `live_canary.json`):

| Provider | Status | Assets | Poster/still |
|---|---|---|---|
| Pexels video | OK | 8 | `image` + `video_pictures[0]` — AVAILABLE |
| Pixabay video | OK | 8 | `videos.<size>.thumbnail` — **AVAILABLE** |
| Mixkit | OK (scrape) | 10 | card `<img>` — AVAILABLE |
| Coverr | OK (scrape) | 10 | card `<img>` — AVAILABLE |
| Pixabay Photos | OK | 5 | `webformatURL` — AVAILABLE |

### PIXABAY_VIDEO_POSTER verdict

**AVAILABLE.** The live Pixabay `/api/videos/` response contains
`videos.large|medium|small|tiny.thumbnail` (verified: JPEG 200, 1280×720,
~89 KB for `tiny`). P02's documentation said `PREVIEW_UNAVAILABLE`; that was
wrong. The provider already scanned those fields, but selected the first entry
(`large`, often >512 KB) and the preview budget rejected it. P03 changed the
selection order to `tiny → small → medium → large`.

## 4. Corpus

Built deterministically from the production DB (read-only):

- source: `scripts.bloques_json` blocks with real narration + Spanish visual
  description + English `search_query_en`;
- excludes the `test` channel;
- round-robin across 11 categories and 4 production channels;
- one cheap text LLM call per scene extracts English filmable intent
  (`subject`, `action`, `environment`, `visual_concept`, `must_include`),
  cached in `intent_cache.json`;
- `human_label` is `null` for every scene.

Diversity: 3–4 scenes per category; 9 per channel; 8 era scenes; 8 with
historical `must_avoid` (`smartphone`, `laptop`, `modern car`, `neon sign`).

This is a hard corpus: many scenes are symbolic (`grief`, `inner light`,
`synchronistic coincidence`), not literal stock-search subjects. That is
intentional; it exposes where retrieval fails.

## 5. Benchmark method

The same frozen candidate pool is passed unchanged to:

- **A LEGACY** — `replay.legacy_decision` (production Fase-4a `rank_candidates`
  order; no threshold, first ranked candidate wins);
- **B SEMANTIC_TEXT** — P01 `SemanticMediaSelector.rank` (text/semantic only);
- **C SEMANTIC_VISUAL** — P02 preview CLIP embeddings + text, and real frame
  evidence on a bounded subset (8 scenes, 2 candidates/scene, 6 frames each).

Arm C never uses the VLM. Frames are only downloaded for C's top shortlist, with
a 30 MB/file cap and a 300 MB total cap.

Auto-evaluation uses a **benchmark-specific relevance rubric** (separate from
the production strict verifier): generic-but-plausible stills get partial
credit. It is explicitly **not human ground truth**.

## 6. Results (winner-level, AUTO_EVALUATION)

`top1_relevance_mean` now means the mean auto-evaluation of the candidate each
arm actually selected. NO_GOOD arms have no winner and count as failures.

### Before calibration

| Arm | Winners evaluated | Coverage | top1 relevance | relevant rate | NO_GOOD | era violations |
|---|---:|---:|---:|---:|---:|---:|
| LEGACY | 36 | 100% | 0.2694 | 13.9% | 0.0% | 3 |
| SEMANTIC_TEXT | 19 | 52.8% | 0.3605 | 26.3% | 47.2% | 1 |
| SEMANTIC_VISUAL | 34 | 94.4% | 0.3412 | 20.6% | 13.9% | 2 |

Candidate-level (top-3) relevance, used only for threshold sweeps:

| Arm | top-3 relevance |
|---|---:|
| LEGACY | 0.2889 |
| SEMANTIC_TEXT | 0.3306 |
| SEMANTIC_VISUAL | 0.3761 |

### After calibration

| Arm | Winners evaluated | Coverage | top1 relevance | relevant rate | NO_GOOD | era violations |
|---|---:|---:|---:|---:|---:|---:|
| LEGACY | 36 | 100% | 0.2694 | 16.7% | 0.0% | 3 |
| SEMANTIC_TEXT | 31 | 86.1% | 0.3500 | 19.4% | 13.9% | 1 |
| SEMANTIC_VISUAL | 34 | 94.4% | 0.3544 | 20.6% | 13.9% | 2 |

Paired deltas after calibration (only scenes where both arms have a winner and
auto-evidence):

| Pair | n | Mean delta | Wins / losses / ties |
|---|---:|---:|---:|
| legacy → semantic_text | 31 | **+0.0661** | 7 / 1 / 23 |
| legacy → semantic_visual | 34 | **+0.0956** | 9 / 1 / 24 |
| semantic_text → semantic_visual | 29 | +0.0103 | 1 / 0 / 28 |

Interpretation: semantic text and visual both beat legacy, clearly. Visual is
only marginally better than text on this corpus (one win, no loss, 28 ties).

Provider distribution (after): legacy `pexels 13 / pixabay 8 / pixabay_photo 15`;
text `9 / 10 / 12`; visual `11 / 10 / 15`.

## 7. Failure set

Concrete classes after calibration (`failure_set_after.json`):

| Class | Count |
|---|---:|
| ALL_BAD | 28 |
| ALL_GOOD | 5 |
| LEGACY_BAD_VISUAL_GOOD | 2 |
| LEGACY_BAD_TEXT_GOOD | 1 |
| TEXT_BAD_VISUAL_GOOD | 1 |
| VISUAL_BAD_TEXT_GOOD | 0 |
| INCONCLUSIVE | 1 |

Representative cases:

- **LEGACY_BAD_VISUAL_GOOD** `s0379-b03` (mummy/amulet): legacy picked a
  Pixabay clip whose tags matched “mummy” but the visual was a costume/child
  scene; the visual arm picked “ancient artifact”. Auto overall 0.0 vs 0.85.
- **LEGACY_BAD_VISUAL_GOOD** `s1406-b11` (doctor/clinical records): legacy
  0.3, visual 0.6.
- **TEXT_BAD_VISUAL_GOOD** `s1406-b11`: text 0.3, visual 0.6.
- **ALL_GOOD** `s1073-b14` (group sharing ideas): all three arms 1.0.
- **ALL_BAD** `s0777-b12` (Palmyra ruins) and `s0526-b18` (radiating human
  silhouette): stock never reaches the requested specificity; visual slightly
  improves the choice but not enough to cross 0.6.

The high `ALL_BAD` count is a finding about **corpus difficulty**, not only
selector quality. The 0.6 rubric is “generic but usable”; many symbolic scenes
have no adequate stock asset.

## 8. Calibration

Only one parameter changed: **`P01_MIN_RELEVANCE_SCORE` 0.26 → 0.18**.

- Sweep over 160 auto-evaluated candidates: 0.20 maximised F1; 0.18 was within
  0.02 F1 while raising recall from 0.76 to 0.85 and lowering NO_GOOD from
  47.2% to 13.9%.
- **`P02_COMBINED_THRESHOLD` kept at 0.26.** After the denominator fix below,
  0.26 is the F1 optimum (precision 0.28 / recall 0.76 / coverage 0.57).
- **Ranking weights kept.** Candidate-level correlations: score 0.358,
  semantic 0.276, lexical 0.121, subject 0.214, action 0.052. No large,
  stable component advantage justified reweighting.
- **Visual weights kept.** Preview correlation 0.516 vs text 0.377, but
  winner-level evidence did not justify a weight shift; defaults kept to avoid
  overfitting.
- `must_avoid`/`era` penalties unchanged. The VLM reported zero must-avoid
  violations in the benchmark; there was no evidence to recalibrate the
  threshold.

## 9. P02 bugs found and fixed

1. **Transformers 5 compatibility.** `CLIPModel.get_text_features()` /
   `get_image_features()` now return `BaseModelOutputWithPooling`, not a tensor.
   P02 silently returned `None`, disabling the visual backend with transformers
   5.12.1. Fixed with `_feature_tensor()` (uses `pooler_output` when present).
2. **AVIF preview failure.** `PreviewFetcher` advertised `image/avif`; Pexels
   returned AVIF; the MIME detector rejected it as `not_an_image`, so **all
   Pexels previews failed**. Fixed: stop advertising AVIF and report
   `avif_unsupported` explicitly if a server still returns it.
3. **Pixabay video poster.** Live responses expose `videos.<size>.thumbnail`.
   P02 documentation said unavailable; the provider picked the 4K `large`
   thumbnail (often >512 KB) and the preview budget rejected it. Fixed to prefer
   `tiny → small → medium → large`, and docs corrected.
4. **Combined-score denominator.** `compute_combined_score()` divided by the
   full weight sum even when frame/VLM stages did not run, capping preview-only
   scores and producing 77.8% NO_GOOD. Fixed with
   `available_components` renormalization (ratios unchanged, ranking unchanged,
   score scale comparable across modes). Preview-only NO_GOOD dropped to 13.9%
   with the same 0.26 threshold.

## 10. Performance (measured)

| Metric | Value |
|---|---|
| Model load (warm) | 0.89 s |
| Preview fetch mean / p90 | 349.9 ms / 506.6 ms |
| Preview mean bytes | 65.0 KB |
| Frame extraction | 240.5 ms/frame (6 frames ≈ 1.44 s) |
| Visual scoring (CLIP + preview/text + constraints) | ~4.4 s/scene |
| VLM | 1.66 s/call |
| Peak RSS | ~1.33 GB |
| Network | 134 requests, 239.3 MB, 16 downloads, 432 previews, 96 frames |

### 20-minute video estimate

Typical scenes per video (DB): **37.75**.

Formula: `scenes × (12×preview + 2×6×frame + visual_scoring + 2×VLM)`;
bytes = `scenes × (12×avg_preview_bytes + 2×avg_video_bytes)`.

| Component | Estimate |
|---|---:|
| Previews | 158.5 s |
| Frame extraction | 108.9 s |
| Visual scoring | 165.9 s |
| VLM (2 candidates/scene) | 124.9 s |
| **Total CPU, excluding downloads** | **≈558 s (9.3 min)** |
| Preview bytes | 29.4 MB |
| Video bytes | 996.7 MB |
| Total bytes | ≈1.03 GB |

This is a significant cost. For production, preview-only (`embedding_only`) or
a reduced download/frame budget is likely the right first step.

## 11. Manual review pack

`benchmarks/media_semantics/review_pack.html` (stable copy; full run copies:
`review_pack_before.html`, `review_pack_after.html`).

Each scene shows narration, SceneSpec summary, the three winners with provider
page links and local preview references, scores, AUTO_EVALUATION, and a
`human_label` placeholder. `labels_template.json` has `human_label: null` and
empty human score fields.

## 12. Artifacts

`benchmarks/media_semantics/runs/p03-20261024/`:

`preflight.json`, `model_identity.json`, `config_snapshot.json`,
`provider_canary.json`, `live_canary.json`, `corpus.json`, `intent_cache.json`,
`pools.json`, `previews.json`, `frames.json`, `decisions_before.json`,
`decisions_after.json`, `auto_eval_before.json`, `auto_eval_after.json`,
`metrics_before.json`, `metrics_after.json`, `metrics.json`,
`failure_set_*.json`, `calibration.json`, `performance.json`, `review_pack*.html`,
`labels_template*.json`, `manifest.json`, `artifact_index.json`, `checksums.json`.

Large runtime assets (`previews/`, `videos/`, `frames/`, `eval_images/`) are
gitignored. Only JSON/HTML evidence is committed.

## 13. VLM status

**VLM_REAL_VALIDATED.** OpenAI-compatible `gpt-4o-mini` was called for:
- 310 benchmark auto-evaluations (benchmark rubric, low-detail images);
- one production-path canary through `verify_with_backend` +
  `build_vlm_prompt` (`status=OK`, `model=gpt-4o-mini`, `decision=accept`).

The production verifier remains intentionally strict; the benchmark rubric is
separate and is not used to select media.

## 14. Known limitations

- AUTO_EVALUATION is a VLM auxiliary signal, not human ground truth; 36 scenes
  and 160 evaluated candidates are a small calibration sample.
- The corpus is Spanish narration + English queries; other languages/styles
  are not covered.
- Only 8 scenes received real frame evidence; the rest of arm C is
  preview-only.
- `must_avoid` violations were not observed, so negative-penalty calibration
  is not empirically validated.
- The 20-minute estimate uses measured per-unit costs but assumes the P02
  default funnel (12 previews, 2 downloads, 6 frames, 2 VLM candidates).
- P02 `full_visual` with VLM in the selection loop was not benchmarked
  end-to-end (VLM was kept out of arm C deliberately).
- No production activation, no uploads, no E2E video generation.

## 15. Recommendation

**READY_FOR_SHADOW** (not default). Recommended next steps for P04:

1. Run shadow mode on production scenes (no asset change) to collect human
   labels and validate the +0.09 visual gain over legacy.
2. Prefer preview-only visual scoring first; the full download+frames+VLM path
   costs ~9 CPU-minutes and ~1 GB per 20-minute video.
3. Improve symbolic-scene handling (AI image fallback or intent rewriting);
   `ALL_BAD` is dominated by abstract/symbolic scenes.
4. Add a dedicated must-avoid detector/validation set before relying on the
   negative penalty.
5. Keep `MEDIA_SELECTOR=legacy` and `P02_VISUAL_MODE=off` until the external
   supervisor approves a canary.

## Reproduction

```bash
python3 -m benchmarks.media_semantics.live_canary --out /tmp/p03_canary.json
python3 -m benchmarks.media_semantics.cli \
  --run-id p03-repro --stage all --llm-intents \
  --limit 44 --per-category 6 --scrapers-scenes 8 \
  --frame-scenes 8 --max-auto-eval 400
```
