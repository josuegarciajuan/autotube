# ADR-20261024 — Semantic media retrieval core (P01)

## Context

P00 established that stock media selection was mostly lexical, page-local,
first-success-wins, and unable to explain scene→asset decisions. Sub-scenes
inherited the parent block's query. Candidates with no metadata were treated
like candidates with a known zero score, and there was no relevance floor.

## Decision

Introduce a provider-agnostic semantic retrieval core in
`pipeline/media_semantics/` and keep the existing selector as the default
(`MEDIA_SELECTOR=legacy`). The new path (`MEDIA_SELECTOR=semantic`) builds a
`SceneSpec` per final scene, retrieves metadata across queries/providers/pages
into one bounded pool, ranks globally, applies a threshold, then downloads only
the approved top-N.

Text embeddings are optional and local. The default backend is deterministic
hashing (**HASH_VECTOR**, not a neural embedding); a sentence-transformers
backend (**SEMANTIC_EMBEDDING**) can be enabled later without changing the
contracts. Visual embeddings are explicitly deferred to P02.

## Reconciliation note (P01-R)

Two independent P01 implementations existed (`pipeline/media_semantics` in main
and `pipeline/media_selection` in a supervised worktree). This ADR keeps
`pipeline/media_semantics` as the single canonical namespace because it is the
integrated implementation (worker/generation-service scene identity, DB v72,
provenance flush, docs, 37 tests). The useful supervised-worktree pieces were
ported into it: final-fragment action derivation for sub-scenes, a
narration-derived query variant, forbidden-term stripping, a pre-rank
era/anachronism veto, explicit `media_request_id` mapping, deterministic
offline replay, the replay CLI and a benchmark fixture. The parallel
`pipeline/media_selection` namespace is not present in main.

## Consequences

Positive:
- A provider/page boundary no longer decides the winner.
- Score-0 and metadata-less candidates can no longer win silently.
- Every decision is replayable offline and persisted with partial scores.
- Legacy remains deployable and comparable via shadow mode.

Negative / risks:
- Default threshold and weights are not yet calibrated; a labelled benchmark is
  required before production activation.
- The hashing backend is weaker than a transformer model.
- Semantic mode performs more metadata requests than first-success-wins
  (bounded by configurable budgets), though fewer downloads.

## Reversibility

Set `MEDIA_SELECTOR=legacy` (default) to restore the previous selector. DB
migration v72 is additive/nullable and does not modify existing rows.
