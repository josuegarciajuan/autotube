# ADR-20261024 — P03 calibration and availability renormalization

Status: accepted for P03 (inactive in production; `MEDIA_SELECTOR=legacy`,
`P02_VISUAL_MODE=off`).

## Context

P02 combined text, preview, frame and VLM components by dividing by the full
weight sum even when frame/VLM stages did not run. In preview-only mode the
maximum attainable score was therefore 0.65, and the default 0.26 threshold
produced 77.8% NO_GOOD on a 36-scene real benchmark. P01's 0.26 threshold also
rejected 47.2% of scenes that a real benchmark found usable.

Two P02 implementation defects were also found: CLIP silently returned `None`
with transformers 5 (output type change), and Pexels previews failed because
the fetcher advertised AVIF but the MIME detector rejected it.

## Decision

1. **Renormalize by available components.** `compute_combined_score()` accepts
   `available_components`; the visual pipeline passes only the stages that ran.
   The relative weights of available components are unchanged, so ranking is
   unchanged; only the score scale (and threshold eligibility) changes.
2. **Calibrate P01 threshold 0.26 → 0.18.** The sweep maximised F1 at 0.20; a
   0.02-F1 robustness band prefers 0.18 because it raises recall 0.76 → 0.85
   and lowers NO_GOOD 47.2% → 13.9%.
3. **Keep P02 threshold and weights unchanged.** After renormalization, 0.26 is
   the F1 optimum. Candidate-level preview correlation was positive but
   winner-level evidence did not justify reweighting.
4. **Fix the backend/provider regressions** (transformers 5 output tensor,
   AVIF Accept header, Pixabay thumbnail rendition selection).

## Consequences

- Preview-only visual mode is no longer structurally penalized; NO_GOOD drops
  to 13.9% at the existing 0.26 threshold.
- P01 selects more scenes (NO_GOOD 47.2% → 13.9%) with a small precision
  trade-off; semantic mode remains opt-in.
- Scores are not directly comparable to pre-P03 preview-only scores; the
  denominator changed. Full-five-component scores are unchanged.
- No production activation. External human review is still required before a
  controlled canary.

## Evidence

`benchmarks/media_semantics/runs/p03-20261024/` and
`docs/p03-real-world-benchmark.md`.
