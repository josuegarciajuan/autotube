"""P03-J/K — manual review pack and reproducible artifact writers."""

from __future__ import annotations

import html
import json
import shutil
from pathlib import Path
from typing import Any

from .scoring import ARMS


def atomic_write_json(path: str | Path, payload: Any) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    tmp.replace(path)
    return path


def _rel(path: str, base: Path) -> str:
    if not path:
        return ""
    try:
        return str(Path(path).resolve().relative_to(base.resolve()))
    except Exception:
        return ""


def _fmt(value: Any, digits: int = 3) -> str:
    if value is None:
        return "—"
    if isinstance(value, float):
        return f"{value:.{digits}f}"
    return str(value)


def _esc(value: Any) -> str:
    return html.escape(str(value if value is not None else ""))


def _winner_card(
    arm: str,
    decision: dict[str, Any],
    previews: dict[str, dict[str, Any]],
    frames: dict[str, dict[str, Any]],
    base: Path,
    auto_by_key: dict[tuple[str, str], dict[str, Any]],
    scene_id: str,
    *,
    prefer_remote: bool = False,
) -> str:
    arm_data = decision.get(arm) or {}
    winner = arm_data.get("winner")
    if not winner:
        return f"<div class='card'><h3>{_esc(arm)}</h3><p class='bad'>NO WINNER</p></div>"
    key = _candidate_key(winner)
    preview = previews.get(key) or {}
    frame = frames.get(key) or {}
    image_path = preview.get("path") or frame.get("path") or ""
    image_src = _rel(image_path, base) if image_path else ""
    if prefer_remote and winner.get("thumbnail_url"):
        image_src = str(winner.get("thumbnail_url"))
    page_url = winner.get("page_url") or winner.get("asset_url") or winner.get("download_url") or ""
    auto = auto_by_key.get((scene_id, arm)) or (auto_by_key.get((scene_id, key)) or {})
    auto_eval = auto.get("auto_evaluation") or {}
    tags = ", ".join(str(t) for t in (winner.get("tags") or [])[:8])
    img = (
        f"<img loading='lazy' src='{_esc(image_src)}' alt='winner evidence'>"
        if image_src
        else "<div class='noevi'>no preview evidence</div>"
    )
    return f"""
    <div class='card'>
      <h3>{_esc(arm)}</h3>
      {img}
      <p><b>{_esc(winner.get('title') or '(no title)')}</b></p>
      <p class='meta'>provider={_esc(winner.get('provider'))} · score={_fmt(arm_data.get('winner_score'))}
         · status={_esc(arm_data.get('status'))}</p>
      <p class='meta'>tags: {_esc(tags)}</p>
      <p class='meta'><a href='{_esc(page_url)}' target='_blank' rel='noopener'>provider page</a></p>
      <div class='auto'>
        <b>AUTO_EVALUATION</b> (auxiliary, not human ground truth)<br>
        status={_esc(auto_eval.get('status'))} · overall={_fmt(auto_eval.get('overall_relevance'))}
        · subject={_fmt(auto_eval.get('subject_match'))} · action={_fmt(auto_eval.get('action_match'))}
        · env={_fmt(auto_eval.get('environment_match'))} · era={_fmt(auto_eval.get('era_match'))}
        · confidence={_fmt(auto_eval.get('confidence'))}<br>
        must_avoid: {_esc(', '.join(auto_eval.get('must_avoid') or []))}<br>
        <span class='meta'>{_esc(auto_eval.get('reason'))}</span>
      </div>
      <div class='human'>
        <b>HUMAN LABEL:</b> <span class='pending'>null (pending external review)</span>
      </div>
    </div>
    """


def _candidate_key(candidate: dict[str, Any]) -> str:
    from pipeline.media_semantics.contracts import Candidate

    try:
        return Candidate.from_dict(candidate).canonical_key
    except Exception:
        return ""


def _spec_summary(spec: dict[str, Any]) -> str:
    return (
        f"subject={_esc(spec.get('subject'))} · action={_esc(spec.get('action'))} · "
        f"environment={_esc(spec.get('environment'))} · era={_esc(spec.get('era') or '—')} · "
        f"must_avoid={_esc(', '.join(spec.get('must_avoid') or []) or '—')}"
    )


def write_review_pack(
    corpus: dict[str, Any],
    decisions: list[dict[str, Any]],
    auto_eval: list[dict[str, Any]],
    previews_by_scene: dict[str, dict[str, dict[str, Any]]],
    frames_by_scene: dict[str, dict[str, dict[str, Any]]],
    metrics: dict[str, Any],
    run_dir: Path,
    *,
    title: str = "P03 media semantics review pack",
    filename: str = "review_pack.html",
    labels_filename: str = "labels_template.json",
    prefer_remote: bool = False,
) -> tuple[Path, Path]:
    """Write the static HTML review pack and the labels template."""
    entries = {entry["scene_id"]: entry for entry in corpus.get("entries") or []}
    decisions_by_id = {d["scene_id"]: d for d in decisions}
    auto_by_key: dict[tuple[str, str], dict[str, Any]] = {}
    for record in auto_eval:
        auto_by_key[(record["scene_id"], record["arm"])] = record
        auto_by_key[(record["scene_id"], record.get("candidate_key", ""))] = record

    cards = []
    labels = []
    for scene_id, entry in entries.items():
        decision = decisions_by_id.get(scene_id, {})
        previews = previews_by_scene.get(scene_id) or {}
        frames = frames_by_scene.get(scene_id) or {}
        spec = entry.get("scene_spec") or {}
        arm_cards = "".join(
            _winner_card(
                arm, decision, previews, frames, run_dir, auto_by_key, scene_id,
                prefer_remote=prefer_remote,
            )
            for arm in ARMS
        )
        cards.append(
            f"""
            <section class='scene'>
              <h2>{_esc(scene_id)} <span class='tag'>{_esc(entry.get('category'))}</span>
                  <span class='tag'>{_esc(entry.get('channel'))}</span>
                  <span class='tag'>{_esc(entry.get('media_type'))}</span></h2>
              <p class='title'>{_esc(entry.get('script_title'))}</p>
              <details open>
                <summary>Narration &amp; SceneSpec</summary>
                <p class='narration'>{_esc(entry.get('narration_fragment'))}</p>
                <p class='meta'>visual description (ES): {_esc(entry.get('visual_description_es'))}</p>
                <p class='meta'>query: {_esc(entry.get('query_en'))}</p>
                <p class='meta'>{_spec_summary(spec)}</p>
              </details>
              <div class='cards'>{arm_cards}</div>
            </section>
            """
        )
        labels.append(
            {
                "scene_id": scene_id,
                "category": entry.get("category"),
                "era": entry.get("era"),
                "narration_fragment": entry.get("narration_fragment"),
                "legacy_winner": (decision.get("legacy") or {}).get("winner"),
                "semantic_text_winner": (decision.get("semantic_text") or {}).get("winner"),
                "semantic_visual_winner": (decision.get("semantic_visual") or {}).get("winner"),
                "human_label": None,
                "human_scores": {
                    "subject_match": None,
                    "action_match": None,
                    "environment_match": None,
                    "era_match": None,
                    "must_avoid": None,
                    "overall_relevance": None,
                    "notes": None,
                },
            }
        )

    summary_rows = "".join(
        f"<tr><td>{_esc(arm)}</td>"
        f"<td>{_fmt((metrics['arms'].get(arm) or {}).get('top1_relevance_mean'))}</td>"
        f"<td>{_fmt((metrics['arms'].get(arm) or {}).get('relevant_rate'))}</td>"
        f"<td>{_fmt((metrics['arms'].get(arm) or {}).get('subject_match_mean'))}</td>"
        f"<td>{_fmt((metrics['arms'].get(arm) or {}).get('action_match_mean'))}</td>"
        f"<td>{_fmt((metrics['arms'].get(arm) or {}).get('environment_match_mean'))}</td>"
        f"<td>{_fmt((metrics['arms'].get(arm) or {}).get('no_good_candidate_rate'))}</td>"
        f"<td>{_fmt((metrics['arms'].get(arm) or {}).get('latency_mean_ms'))}</td></tr>"
        for arm in ARMS
    )
    html_doc = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_esc(title)}</title>
<style>
:root {{ color-scheme: dark; }}
body {{ font-family: -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
       background:#0f1115; color:#e6e6e6; margin:0; padding:24px; }}
h1,h2,h3 {{ margin:0 0 8px 0; }}
a {{ color:#7cc7ff; }}
.scene {{ border:1px solid #2a2f3a; border-radius:12px; padding:18px; margin:0 0 22px 0;
          background:#151922; }}
.tag {{ font-size:11px; background:#243042; color:#9fc7ff; border-radius:999px;
        padding:2px 8px; margin-left:6px; vertical-align:middle; }}
.title {{ color:#b9c2d0; margin:0 0 6px 0; }}
.narration {{ background:#0c0f14; padding:10px; border-radius:8px; line-height:1.45; }}
.meta {{ color:#9aa4b2; font-size:12px; margin:4px 0; }}
.cards {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(280px,1fr)); gap:14px; }}
.card {{ background:#10141b; border:1px solid #2a2f3a; border-radius:10px; padding:12px; }}
.card img {{ width:100%; max-height:190px; object-fit:cover; border-radius:8px;
             background:#000; margin-bottom:8px; }}
.noevi {{ height:120px; display:flex; align-items:center; justify-content:center;
          border:1px dashed #333; border-radius:8px; color:#666; margin-bottom:8px; }}
.auto {{ border-top:1px solid #2a2f3a; margin-top:8px; padding-top:8px; font-size:12px;
         color:#c7d0db; }}
.human {{ margin-top:8px; font-size:12px; color:#ffd479; }}
.pending {{ color:#ffd479; }}
.bad {{ color:#ff7b7b; }}
table {{ border-collapse:collapse; margin:10px 0 20px 0; font-size:13px; }}
td,th {{ border:1px solid #2a2f3a; padding:6px 10px; text-align:left; }}
th {{ background:#1a1f29; }}
</style></head><body>
<h1>{_esc(title)}</h1>
<p class='meta'>AUTO_EVALUATION is a VLM auxiliary signal, NOT human ground truth.
<code>human_label</code> is null for every scene.</p>
<table>
<tr><th>arm</th><th>top1 relevance</th><th>relevant rate</th><th>subject</th>
<th>action</th><th>environment</th><th>NO_GOOD</th><th>latency ms</th></tr>
{summary_rows}
</table>
{''.join(cards)}
</body></html>
"""
    html_path = run_dir / filename
    html_path.write_text(html_doc, encoding="utf-8")
    labels_path = run_dir / labels_filename
    atomic_write_json(labels_path, {"version": 1, "entries": labels})
    return html_path, labels_path


def write_artifact_index(run_dir: Path, run_id: str, manifest: dict[str, Any]) -> Path:
    """Small, reproducible index (no secrets, no large binaries)."""
    files = []
    for path in sorted(run_dir.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(run_dir)
        if rel.parts and rel.parts[0] in ("videos", "previews", "frames", "eval_images"):
            continue
        files.append({"path": str(rel), "bytes": path.stat().st_size})
    index = {
        "run_id": run_id,
        "manifest": manifest,
        "files": files,
    }
    return atomic_write_json(run_dir / "artifact_index.json", index)


def copy_review_pack_to_repo(run_dir: Path, repo_root: Path, run_id: str) -> Path | None:
    """Copy the HTML to a stable, committed location with adjusted asset root."""
    source = run_dir / "review_pack.html"
    if not source.is_file():
        return None
    target_dir = repo_root / "benchmarks" / "media_semantics"
    target_dir.mkdir(parents=True, exist_ok=True)
    text = source.read_text(encoding="utf-8")
    # The stable copy lives one directory above the runs/ folder.
    prefix = f"runs/{run_id}/"
    text = text.replace('src="', f'src="{prefix}')
    target = target_dir / "review_pack.html"
    target.write_text(text, encoding="utf-8")
    return target
