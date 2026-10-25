# P05 evidence — integrated selection (2026-10-25)

Source: frozen P03 corpus (`p03-20261024`, 36 scenes) plus P04 archive winners
with selected evidence (`p04-20261025c`). Ground truth = P03/P04 auto-evaluation
labels (VLM rubric), threshold 0.60. No human labels were invented.

| Arm | Scenes | Top-1 mean | Relevant rate | ALL_BAD good | Control good |
|---|---:|---:|---:|---:|---:|
| LEGACY | 36 | 0.2694 | 0.1667 | — | — |
| SEMANTIC_TEXT (P01) | 36 | 0.3500 | 0.1935 | — | — |
| SEMANTIC_VISUAL (P02) | 36 | 0.3544 | 0.2059 | — | — |
| P04 | 34 | 0.2534 (ALL_BAD mean) | — | 4/29 | 3/5 |
| P05 TEXT_ONLY | 36 | 0.3292 | 0.2222 | 2/29 | 4/5 |
| P05 PREVIEW_VISUAL | 36 | **0.3472** | **0.25** | **4/29** | **4/5** |

* P05 ALL_BAD mean = **0.2759** vs P04 **0.2534** (improved).
* Legacy → P05 preview mean delta = **+0.0778**.
* Regression rate vs legacy-good scenes = **1/6 (0.1667)**.
* Control retention = **4/5**, better than P04 (3/5) and one below legacy (5/5).
* Latency: TEXT_ONLY 154 ms total; PREVIEW_VISUAL 112.7 s total (3.1 s/scene).
* Adaptive budget skipped visual inspection on 2/36 scenes without metric change.
* Shadow: 36 scenes, agreement 0.3333, legacy winner unchanged, 0 assets replaced.
* AI canary: 2/3 generated; CLIP visual relevance 0.286 and 0.303; one empty
  output (timeout). Nothing published.

Artifacts: `integrated_metrics.json`, `p05_offline.json`, `shadow.jsonl`,
`shadow_summary.json`, `review_pack.html`, `ai_canary.json`, `checksums.json`.

Limitations: the offline universe only includes candidates with frozen labels;
archive coverage is limited to P04 selected winners; FULL_VISUAL was not run
offline because the frozen corpus contains previews, not local video files.
