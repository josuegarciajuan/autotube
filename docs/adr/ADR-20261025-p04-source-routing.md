# ADR-20261025 — P04 source routing and fallback intelligence

* Status: proposed (external review pending)
* Context: P01–P03 complete; production still `MEDIA_SELECTOR=legacy`,
  `P02_VISUAL_MODE=off`. P03 evidence shows availability/specificity is the main
  remaining bottleneck (most ALL_BAD scenes have no adequate stock material).
* Decision:
  1. Introduce a deterministic `SourceRouter` that classifies a `SceneSpec` into
     one of 13 categories and returns a category-specific asset ladder.
  2. Add official Wikimedia Commons and NASA providers behind the router.
  3. Treat images as first-class assets; use the existing Ken Burns renderer with
     a deterministic, clamped motion plan.
  4. Keep AI image generation as an explicit, policy-controlled fallback after
     real sources fail; never a universal first choice.
  5. Add perceptual dedup and bounded recent-scene coherence as soft signals
     (only real duplicates are hard vetoes).
  6. Unify provenance and emit a JSON/Markdown attribution manifest per video.
  7. Wire everything behind `P04_SOURCE_ROUTER=1`, default **off**.
* Consequences:
  * Positive: more authentic material for history/space/science, explicit
    fallbacks, auditable licences, no production change by default.
  * Negative: text-only ranking can regress scenes whose good stock asset has no
    metadata; benchmark control 5/5 → 2/5. Must be combined with P02 visual
    re-ranking before a production canary.
  * Neutral: no new mandatory dependency (`Pillow` + `numpy` already used).
* Alternatives considered: adding more stock scrapers (rejected: fragile, poor
  provenance); making AI the default fallback (rejected: cost/authenticity);
  replacing the legacy selector (rejected: P04 is opt-in).
* Validation: 42 offline tests + focused 29 ALL_BAD live archive canary.
