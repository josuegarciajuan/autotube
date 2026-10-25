"""P04-R/S — unified provenance and per-video attribution manifest.

Every provider funnels into one provenance shape.  The manifest is
reproducible from stored decisions and never invents a licence: unknown stays
``UNKNOWN`` and ``public_domain`` stays ``None`` unless explicitly proven.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .contracts import Candidate, SelectionDecision


def provenance_for_candidate(candidate: Candidate | None) -> dict[str, Any]:
    if candidate is None:
        return {
            "provider": "", "source_url": "", "creator": "", "license": "UNKNOWN",
            "license_url": "", "attribution_required": None,
            "public_domain": None, "ai_generated": False,
            "generation_backend": "", "generation_prompt_hash": "",
        }
    return {
        "provider": str(candidate.provider or "UNKNOWN"),
        "source_url": str(candidate.page_url or candidate.asset_url or ""),
        "media_url": str(candidate.download_url or candidate.asset_url or ""),
        "creator": str(candidate.creator or ""),
        "license": str(candidate.license or "UNKNOWN"),
        "license_url": str(candidate.license_url or ""),
        "attribution_required": candidate.attribution_required,
        "public_domain": candidate.public_domain,
        "ai_generated": bool(candidate.ai_generated),
        "generation_backend": str(candidate.generation_backend or ""),
        "generation_prompt_hash": str(candidate.generation_prompt_hash or ""),
        "date": str(candidate.date or ""),
        "asset_type": str(candidate.asset_type or ""),
    }


def attribution_line(prov: dict[str, Any]) -> str:
    if prov.get("ai_generated"):
        backend = prov.get("generation_backend") or "AI"
        return f"AI-generated image ({backend})"
    creator = prov.get("creator") or "Unknown creator"
    license_label = prov.get("license") or "UNKNOWN"
    source = prov.get("provider") or "UNKNOWN"
    line = f"{creator} — {license_label} — via {source}"
    if prov.get("public_domain") is True:
        line += " (public domain)"
    elif prov.get("public_domain") is None:
        line += " (licence status unknown)"
    return line


def build_attribution_manifest(
    decisions: Iterable[SelectionDecision],
    *,
    video_id: int | None = None,
    channel: str = "",
    script_title: str = "",
    generated_at: str = "",
) -> dict[str, Any]:
    scenes: list[dict[str, Any]] = []
    licence_counts: dict[str, int] = {}
    provider_counts: dict[str, int] = {}
    unknown = 0
    for decision in decisions:
        spec = decision.scene_spec
        candidate = decision.winner
        prov = provenance_for_candidate(candidate)
        if prov["license"] == "UNKNOWN" and not prov["ai_generated"]:
            unknown += 1
        licence_counts[prov["license"] or "UNKNOWN"] = (
            licence_counts.get(prov["license"] or "UNKNOWN", 0) + 1
        )
        provider_counts[prov["provider"] or "UNKNOWN"] = (
            provider_counts.get(prov["provider"] or "UNKNOWN", 0) + 1
        )
        scenes.append({
            "scene_idx": int(spec.scene_idx),
            "block_idx": spec.block_idx,
            "narration_fragment": str(spec.narration_fragment or "")[:400],
            "scene_category": decision.scene_category,
            "asset_type": decision.asset_type,
            "fallback_type": decision.fallback_type,
            "status": decision.status.value,
            "score": round(float(decision.winner_score), 6),
            "asset_url": prov.get("media_url", ""),
            "provenance": prov,
            "attribution": attribution_line(prov),
        })
    return {
        "version": 1,
        "generated_at": generated_at or datetime.now(timezone.utc).isoformat(),
        "video_id": video_id,
        "channel": channel,
        "script_title": script_title,
        "scenes": scenes,
        "summary": {
            "scenes": len(scenes),
            "providers": provider_counts,
            "licenses": licence_counts,
            "unknown_license": unknown,
            "ai_generated": sum(1 for s in scenes if s["provenance"].get("ai_generated")),
            "public_domain": sum(
                1 for s in scenes if s["provenance"].get("public_domain") is True
            ),
        },
    }


def attribution_markdown(manifest: dict[str, Any]) -> str:
    lines = [
        "# Attribution manifest",
        "",
        f"- Video: {manifest.get('video_id') or 'n/a'}",
        f"- Channel: {manifest.get('channel') or 'n/a'}",
        f"- Script: {manifest.get('script_title') or 'n/a'}",
        f"- Generated: {manifest.get('generated_at')}",
        "",
        "| Scene | Asset type | Provider | Creator | Licence | Attribution |",
        "|---:|---|---|---|---|---|",
    ]
    for scene in manifest.get("scenes") or []:
        prov = scene.get("provenance") or {}
        line = (
            f"| {scene.get('scene_idx')} | {scene.get('asset_type') or '-'} | "
            f"{prov.get('provider') or '-'} | {prov.get('creator') or '-'} | "
            f"{prov.get('license') or 'UNKNOWN'} | {scene.get('attribution') or '-'} |"
        )
        lines.append(line.replace("|", "\\|"))
    summary = manifest.get("summary") or {}
    lines.extend([
        "",
        "## Summary",
        "",
        f"- Scenes: {summary.get('scenes', 0)}",
        f"- AI generated: {summary.get('ai_generated', 0)}",
        f"- Public domain (verified): {summary.get('public_domain', 0)}",
        f"- Unknown licence: {summary.get('unknown_license', 0)}",
        "",
        "Unknown licence means the provider did not clearly declare one; it is "
        "not a claim that the asset is free to use.",
        "",
    ])
    return "\n".join(lines)


def write_attribution_manifest(
    manifest: dict[str, Any],
    out_dir: str | Path,
    *,
    stem: str = "attribution_manifest",
) -> dict[str, str]:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    json_path = out / f"{stem}.json"
    md_path = out / f"{stem}.md"
    json_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
    )
    md_path.write_text(attribution_markdown(manifest), encoding="utf-8")
    return {"json": str(json_path), "markdown": str(md_path)}
