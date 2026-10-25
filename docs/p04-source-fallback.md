# P04 — source expansion, fallback intelligence and global coherence

Status: **implemented, opt-in, candidate for external review.**
Production defaults remain unchanged:

```
MEDIA_SELECTOR=legacy
P02_VISUAL_MODE=off
P04_SOURCE_ROUTER=0        # default: off
```

P04 does not change the legacy selector, P01 semantic text ranking or P02 visual
ranking when `P04_SOURCE_ROUTER` is unset. The benchmark evidence is in
`benchmarks/media_semantics/evidence/p04-20261025/`.

## 1. Why

P03 showed the remaining bottleneck is **material availability/specificity**, not
only ranking: 28/36 scenes were ALL_BAD after calibration, and stock video cannot
represent historical figures, specific ruins, NASA subjects or abstract concepts.
P04 therefore routes each scene to the kind of source that can actually represent
it, rather than searching only stock video/stock images.

## 2. Architecture

| Layer | Module | Responsibility |
|---|---|---|
| Scene taxonomy + route | `pipeline/media_semantics/source_router.py` | 13 categories, deterministic heuristics, optional LLM refinement, category-specific ladders, provider budgets |
| Asset strategy | `pipeline/media_semantics/asset_strategy.py` | allowed/preferred/forbidden asset types, AI policy, conceptual permission |
| Archive providers | `pipeline/media_semantics/archive_providers.py` | Wikimedia Commons + NASA official JSON APIs, provenance, rate/cache/retry |
| Sourcing pipeline | `pipeline/media_semantics/sourcing_pipeline.py` | stage budgets → global type-aware ranking → download → fallback ladder |
| Duplicate detection | `pipeline/media_semantics/dedup.py` | canonical key, normalised URL, dHash, pHash, embedding near-duplicate |
| Coherence | `pipeline/media_semantics/coherence.py` | recent assets/providers/types/subjects/shots, style/era signals |
| AI policy | `pipeline/media_semantics/ai_policy.py` | `AI_IMAGE_ALLOWED/PREFERRED/FORBIDDEN`, prompt from SceneSpec |
| Image motion | `pipeline/media_semantics/image_motion.py` | deterministic Ken Burns plan, clamped zoom |
| Attribution | `pipeline/media_semantics/attribution.py` | unified provenance + JSON/Markdown manifest |
| Opt-in wiring | `pipeline/media_fetcher.py`, `pipeline/video_editor.py` | `P04_SOURCE_ROUTER=1`; motion plan consumed by the existing Ken Burns renderer |

No parallel media stack is introduced: stock retrieval/download, P01 ranker,
P01 contracts, P02 preview fetcher and the renderer are reused.

## 3. Scene categories and ladders

Categories: `PEOPLE, NATURE, ANIMALS, CITY, TECHNOLOGY, HISTORY, SPACE,
SCIENCE, MEDICAL, GEOGRAPHY, ABSTRACT, GENERIC, OTHER`.

Ladders differ by category (examples):

| Category | Ladder |
|---|---|
| HISTORY | archive image → archive video → stock image → stock video → AI → conceptual → generic |
| SPACE / SCIENCE | scientific image (NASA/Commons) → archive → stock video → stock image → AI → conceptual → generic |
| MEDICAL | scientific image → stock image → stock video → AI → conceptual → generic |
| PEOPLE / CITY / NATURE / ANIMALS / TECHNOLOGY / GEOGRAPHY | stock video → stock image → (archive where useful) → AI → conceptual → generic |
| ABSTRACT | stock image → AI → conceptual → stock video → generic |
| GENERIC | stock video → stock image → archive → AI → conceptual → generic |

Historical routing (`historical_priority=true`) adds an archive prior and a stock
anachronism prior. A named public/historical figure sets `AI_IMAGE_FORBIDDEN`
(authentic material preferred).

## 4. Asset types

`STOCK_VIDEO`, `STOCK_IMAGE`, `ARCHIVAL_IMAGE`, `ARCHIVAL_VIDEO`,
`SCIENTIFIC_IMAGE`, `AI_IMAGE`, `CONCEPTUAL_STOCK`, `GENERIC_FALLBACK`.

`Candidate.asset_type` and `SelectionDecision.asset_type` make images first-class.
There is no blanket video preference in the P04 score: a highly relevant image
can beat a weakly relevant video. Relevance is the primary signal; type and
coherence are small priors/penalties.

## 5. Providers

### ADDED

* **Wikimedia Commons** — MediaWiki API (`commons.wikimedia.org/w/api.php`),
  `generator=search`, `prop=imageinfo`, images and video, creator/licence/
  attribution/date/categories. Images download a 1920px thumbnail (safe for Ken
  Burns); video uses the original file.
* **NASA Image and Video Library** — `images-api.nasa.gov/search`; space/science
  routes only; asset manifest resolved on download. NASA media terms are **not**
  blanket public domain, so `public_domain` stays `None`.

### REJECTED

* **Dareful** — no stable public API documented for programmatic search; a
  scraper would be fragile and would not add metadata/provenance. Not added.

### DEFERRED

* **Openverse / Internet Archive / Europeana** — plausible future additions, but
  they need their own rate/budget/licence review. Not added in P04.
* **Additional scrapers** — rejected by design unless no API exists and terms
  permit automation.

## 6. Fallback and AI policy

Order for real material is controlled by the route. AI is evaluated only after
the real ladder fails (`NO_GOOD_CANDIDATE`), via `should_attempt_ai`, and only
when the policy permits. Prompt construction preserves subject, action,
environment, era, shot, style continuity and `must_avoid`.

* `AI_IMAGE_ALLOWED`: after appropriate real sources fail.
* `AI_IMAGE_PREFERRED`: symbolic/abstract routes may place AI before generic stock.
* `AI_IMAGE_FORBIDDEN`: never generate (named historical figures, route forbids it).

AI is disabled in production even when P04 routing is enabled unless
`P04_AI_MAX_PER_VIDEO>0` and a backend is configured. Generated assets carry
`AI_GENERATED`, backend and prompt hash.

Conceptual fallback is always explicit: `fallback_type=CONCEPTUAL`,
`conceptual_used=true`, `literal_match=false`, lower score. Generic fallback is
`fallback_type=GENERIC`. Neither is scored as a literal match.

## 7. Duplicates, coherence and shots

* Hard veto only for real duplicates: canonical provider key, normalised URL,
  dHash/pHash near-duplicate, sampled-frame/embedding near-duplicate.
* Soft penalties: same clip recent, subject repetition, provider overuse,
  consecutive stills, era/style jump, portrait crop.
* Shot alternation is a small bonus; relevance remains dominant
  (`RELEVANCE > DIVERSITY`).
* Style coherence penalises generated assets in documentary scenes and
  illustration-after-documentary jumps, without a universal veto.

## 8. Provenance and attribution

Every decision carries provider, source URL, creator, licence, licence URL,
attribution flag, `public_domain` (`True/False/None`), AI flags/backend/prompt
hash and asset type. Unknown licence stays `UNKNOWN`; `public_domain` stays
`None` unless explicitly proven.

`build_attribution_manifest` / `write_attribution_manifest` produce JSON and
Markdown for a video. It is not inserted into YouTube descriptions yet.

## 9. Configuration

All P04 knobs are environment variables; defaults are conservative:

| Variable | Default | Meaning |
|---|---|---|
| `P04_SOURCE_ROUTER` | `0` | **opt-in** wiring in MediaFetcher |
| `P04_MIN_RELEVANCE_SCORE` | `0.18` | literal threshold |
| `P04_STRONG_RELEVANCE_SCORE` | `0.30` | early-stop threshold |
| `P04_AI_ENABLED` | `1` | policy gate (still needs a budget/backend) |
| `P04_AI_MAX_PER_VIDEO` | `0` | 0 = no real generation |
| `P04_AI_BACKEND` | `pollinations` | preferred backend |
| `P04_ALLOW_CONCEPTUAL` | `1` | conceptual fallback |
| `P04_ALLOW_GENERIC` | `1` | generic fallback |

## 10. Benchmark (focused ALL_BAD)

Evidence: `benchmarks/media_semantics/evidence/p04-20261025/`.

* P03 ALL_BAD set: **29** scenes (P03 report rounded 28/36; the frozen failure set
  contains 29 ids).
* Control sample: 5 ALL_GOOD scenes.
* P04 arm: frozen P03 stock pool (no new stock requests) + live Wikimedia/NASA
  canary, AI disabled.
* Auto-evaluation: the P03 benchmark VLM rubric (not human truth).
* Network: 39 archive requests, ~11.6 MB, 39 cache entries, mean archive latency ~1.47 s.

| Metric | Value |
|---|---:|
| ALL_BAD before | 29 |
| ALL_BAD after (still not good) | 25 |
| Good after (>=0.60) | 4 |
| Mean P03 best score | 0.2379 |
| Mean P04 score | 0.2534 |
| Archive good | 2 |
| Control good before | 5/5 |
| Control good after | 3/5 |

The reduction is real but modest: primary wins are authentic archival stills for
history/space subjects (e.g. Palmyra, Egyptian cartonnage mask). Most remaining
failures are symbolic/abstract scenes or subjects with no matching archive asset.

**Control note:** P03's control winners were often visual-only (no usable text
metadata). The P04 text ranker cannot rank them, so it chose a different asset in
2/5 controls. This is a text-only limitation; P04 should be combined with the P02
visual re-ranker before production canary. It is not a change to P02/P01.

## 11. Tests

`pytest -q tests/test_p04_source_fallback.py` — 42 offline tests covering routing,
determinism, provider skip, Wikimedia/NASA normalization and licences, image vs
video competition, ladders, AI policy/prompt, conceptual/generic labels,
perceptual/URL/embedding duplicates, coherence, shot/style, provenance,
attribution manifest, replay, offline guard and non-regression of legacy/P01/P02.

Live canary: `benchmarks/media_semantics/p04_cli.py` (separate module, not
collected by pytest).

## 12. Known limitations / P05 recommendations

1. Combine P04 routing with P02 preview/frame visual scoring so visual-only stock
   candidates (no text metadata) are not discarded.
2. Archive queries are simple keyword strings; category-aware entity extraction
   or Wikidata/Wikimedia category search could improve precision.
3. AI fallback was not exercised live (Pollinations returned 503; local SD too
   slow for a 29-scene canary). Test a controlled 2–3 scene canary with a working
   free/local backend.
4. Wikimedia original videos can be large; keep thumbnail/download byte budgets.
5. Add Openverse/Internet Archive only after a licence + API sustainability review.
6. No production activation, no `MEDIA_SELECTOR`/`P02_VISUAL_MODE` change.
