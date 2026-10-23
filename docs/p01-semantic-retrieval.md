# P01 — Semantic retrieval core

Status: implemented behind a feature flag. Production default remains `legacy`.

## What it solves

Before P01, media selection ranked one provider page at a time with a mostly
lexical score, downloaded the first usable candidate, accepted score-0
candidates, and let sub-scenes inherit the parent block's query. P01 replaces
that path with:

1. a structured `SceneSpec` per **final** scene (post merge/split),
2. multiple queries derived from that scene's real narration slice,
3. a provider-independent `Candidate` with explicit metadata status,
4. one global candidate pool across queries/providers/pages,
5. textual ranking with an optional embedding backend and partial scores,
6. a global top-K and a configurable relevance threshold,
7. download only after ranking, with ordered fallback across approved candidates,
8. a serialisable `SelectionDecision` persisted for audit and replay.

Visual embeddings (CLIP/SigLIP), frame analysis and new providers are **out of
scope** for P01. The embedding backend is pluggable so P02 can add them.

## Feature flags

| Variable | Values | Default | Meaning |
|---|---|---|---|
| `MEDIA_SELECTOR` | `legacy`, `semantic`, `shadow` | `legacy` | Selector implementation (`shadow` = legacy runtime + semantic scoring) |
| `MEDIA_SELECTOR_SHADOW` | `true`, `false` | `false` | In legacy mode, score the pages retrieved by the legacy loop without changing the asset |
| `P01_MIN_RELEVANCE_SCORE` | 0.0–1.0 | `0.26` | Global relevance threshold |
| `P01_EMBEDDING_BACKEND` | `auto`, `hashing`, `sentence_transformers`, `none` | `auto` | Text embedding backend |
| `P01_EMBEDDING_MODEL` | model id | `sentence-transformers/all-MiniLM-L6-v2` | Only used by the optional ST backend |

Channel `MEDIA_STRATEGY` equivalents:

```python
"media_selector": "legacy",
"media_selector_shadow": False,
"semantic_min_relevance_score": 0.26,
"semantic_embedding_backend": "auto",
"semantic_retrieval_budget": {
    "max_total": 80, "max_per_provider": 30, "max_per_query": 24,
    "max_pages_per_provider": 2, "max_queries": 10,
    "top_k": 10, "top_n_download": 3,
},
"semantic_ranking_weights": {
    "semantic": 0.35, "lexical": 0.15, "action": 0.18, "subject": 0.12,
    "environment": 0.08, "era": 0.05, "metadata_quality": 0.04,
    "provider_prior": 0.03, "negative_constraint": 0.50,
    "no_metadata_ceiling": 0.12,
},
```

Ranking backends are explicitly classified:

| Backend | Class | Default | Notes |
|---|---|---|---|
| `hashing` | **HASH_VECTOR** (lexical/distributional) | yes | tokens + bigrams; deterministic; no network/model download |
| `none` | **LEXICAL** | no | semantic component disabled; lexical/action/etc. remain |
| `sentence_transformers` | **SEMANTIC_EMBEDDING** (optional) | no | only attempted when explicitly requested and the local model is already available; falls back to hashing |

`HashingEmbeddingBackend` is **not** a transformer/neural embedding. It is a
lightweight local similarity fallback. The optional `sentence_transformers`
backend is the real semantic-embedding extension point for P02/P03.

## Decision lifecycle

```
final scene
  → SceneSpec (narration fragment + visual bible/intent)
  → query variants (subject/action/environment/era/shot/concept/narration/simplified)
  → metadata retrieval (multi-query × multi-provider × bounded pages)
  → CandidatePool (dedup + budgets, no download)
  → SemanticTextRanker (partial scores → final 0..1; pre-rank era veto)
  → global top-K → threshold → approved list
  → download top-N in order → validate → winner / fallback / NO_GOOD_CANDIDATE
```

Statuses: `SELECTED`, `REJECTED`, `NO_GOOD_CANDIDATE`, `RETRIEVAL_FAILED`,
`DOWNLOAD_FAILED`, `FALLBACK_SELECTED`.

`NO_METADATA` is distinct from `NO_MATCH`; a candidate without usable metadata
can never win, even when every candidate is bad. In that case the scene returns
`NO_GOOD_CANDIDATE` and the normal tier fallback runs.

## Provenance

Every semantic decision emits:

* one `media_selection_decision` JSONL event (`logs/obs/obs.log`), and
* one row in `media_selection_decisions` (migration v72) flushed once
  `video_id` exists.

The row stores the scene identity (`video_id`, `scene_idx`, `block_idx`,
`parent_scene_idx`, `media_request_id`, `origin`, `narration_fragment`), the
`SceneSpec`, queries,
providers, bounded candidate scores, rejected candidates/reasons, winner,
threshold, status, fallback and timing/cost. This answers: *why did this asset
appear over this narration fragment?*

`video_scenes` also gains nullable identity columns (`final_scene_idx`,
`block_idx`, `parent_scene_idx`, `media_request_id`, `narration_fragment`,
`selector_version`, `selection_status`) so existing consumers can join the
final scene to the asset and the decision. The same `media_request_id` is
persisted on `media_selection_decisions` for direct joins.

## Offline replay and benchmark

```python
from pipeline.media_semantics.replay import ReplayHarness, replay_file

harness = ReplayHarness.load("fixtures/replay.json")
results = harness.replay()
print(harness.summary(results))
```

Fixtures contain `SceneSpec`, queries and normalised candidate metadata only.
Replay never touches the network or downloads. Both the current legacy ordering
and the semantic selector run on exactly the same candidate set, and replay
output is deterministic (wall-clock timing is zeroed).

CLI:

```bash
python3 scripts/media_semantics_replay.py \
  --fixture tests/fixtures/media_semantics/example.json
```

`pipeline.media_semantics.benchmark.BenchmarkCorpus` captures the same data plus
`expected_visual_description`, `human_relevance_label` (initially `None`) and
notes. Labels are never invented; `pending_labels` reports what still needs
human review. Target corpus: 20–50 real scenes.

## Known limitations (deferred)

* No visual embeddings / frame understanding yet (P02).
* The default `hashing` backend is HASH_VECTOR (lexical/distributional), not a
  neural semantic embedding. `sentence_transformers` is optional and isolated.
* Default weights/threshold are uncalibrated until the benchmark is labelled.
* Shadow mode only scores pages the legacy loop already fetched (by design: no
  extra downloads). It is an approximation, not a full semantic retrieval.
* Mixkit/Coverr expose only page URLs; their slug is used as safe metadata.
