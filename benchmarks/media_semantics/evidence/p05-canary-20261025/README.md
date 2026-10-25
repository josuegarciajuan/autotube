# P05 controlled canary — 2026-10-25

Real script `1714` (canal2), 3 real scenes (`0,2,10`), same content in both arms.
No YouTube upload, no `videos`/`generation_jobs` rows, no production env change.

| Arm | Selector | Assets | AI used | Archive used | Elapsed |
|---|---|---|---:|---:|---:|
| legacy | `MEDIA_SELECTOR=legacy`, P04/P05 off | 3/3 | 3 (local SD) | 0 | 803 s |
| p05 | P04 + P05 integrated, `PREVIEW_VISUAL`, adaptive, top-N 8 | 3/3 | 0 | 1 (Wikimedia) | 18.2 s (warm cache) |

## Decision summary

* Agreement legacy↔P05: **0/3** (expected: legacy fell to AI-first for canal2 medical scenes).
* Auto-evaluation (auxiliary, VLM; `human_label = null`):
  legacy mean **0.0**, P05 mean **0.483**; improvements 2, regressions 0,
  P05 auto-NO_GOOD 1 (scene 0: headphones for "fetus hearing").
* Per scene:
  * `0` — legacy `ai_local_sd` → P05 `pixabay_photo` STOCK_IMAGE, final 0.3078,
    text 0.2580, preview 0.2201, route MEDICAL. Auto 0.0 → 0.0 (NO_GOOD).
    Top-ranked Pexels video failed to download; the next approved stock image won.
  * `1` — legacy `ai_local_sd` → P05 `pixabay` STOCK_VIDEO, final 0.2740,
    text 0.2326, preview 0.2433, route GENERIC. Auto 0.0 → 0.6.
  * `2` — legacy `ai_local_sd` → P05 `wikimedia_commons` ARCHIVAL_IMAGE,
    final 0.2995, text 0.3295, preview unavailable, route SCIENCE.
    Auto 0.0 → 0.85; licence CC BY-SA 3.0.
* Fallbacks: 0. AI in P05: 0. Duplicate rejections: 0.
* Per-scene latency: 35.3 s / 15.8 s / 4.5 s (cold). Warm cache: 14/14 hits,
  0 writes, 18.2 s total.
* Attribution: 3 scenes, providers pixabay_photo/pixabay/wikimedia_commons,
  2 licences UNKNOWN, 1 CC BY-SA 3.0. Unknown licence is not a claim of free use.

## Files

* `legacy/arm.json`, `p05/arm.json` — per-arm scene + decision evidence.
* `comparison.json` — legacy vs P05 rows, auto-evaluation, human_label=null.
* `review_pack.html` — side-by-side review (open locally).
* `review_thumbs/` — legacy vs P05 stills.
* `attribution_manifest.json` / `.md` — canary attribution manifest.
* `visual_cache` — persistent CLIP embeddings (JSON vectors only).

## Limitations

* Media-selection canary, not a full render/upload canary.
* Auto-evaluation is noisy (scene 1 scored 0.85 and 0.6 in two runs) and is not
  a human label.
* Legacy arm is AI-first because canal2 medical scenes classify as `ai_image`;
  this is the production legacy behavior, not a forced baseline.
* `FULL_VISUAL` was not used and remains globally disabled.
