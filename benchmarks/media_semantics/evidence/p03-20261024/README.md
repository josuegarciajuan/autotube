# P03 evidence bundle — p03-20261024

Small, committed evidence from the full run. The runtime directory
(`benchmarks/media_semantics/runs/p03-20261024/`) contains the complete
decisions, preview images, downloaded videos and frames; those binaries are
gitignored.

| File | Contents |
|---|---|
| `manifest.json` | run identity, corpus/pool/decision counts |
| `preflight.json` | host, git, provider presence, model cache |
| `config_snapshot.json` | benchmark config, no secrets |
| `model_identity.json` | CLIP model identity, files, load timing |
| `provider_canary.json` | one live contract probe per provider |
| `live_canary.json` | provider + model + production VLM canary |
| `corpus.json` | 36 real scenes, SceneSpecs, `human_label: null` |
| `intent_cache.json` | cached LLM intent extraction |
| `metrics_before.json` / `metrics_after.json` | winner-level metrics |
| `metrics.json` | combined metrics + calibration + performance |
| `calibration.json` | before/after thresholds, sweep, rationale |
| `failure_set_before.json` / `failure_set_after.json` | concrete cases |
| `performance.json` | latency, throughput, network, 20-min estimate |
| `auto_eval_before.json` / `auto_eval_after.json` | auxiliary VLM labels |
| `decisions_summary.json` | compact winners/scores per scene and arm |
| `review_pack.html` | review pack with local asset paths (run dir) |
| `review_pack_remote.html` | review pack using provider thumbnail URLs |
| `labels_template.json` etc. | human labels are `null` |

`AUTO_EVALUATION` is not human ground truth. `human_label` is `null` everywhere.
