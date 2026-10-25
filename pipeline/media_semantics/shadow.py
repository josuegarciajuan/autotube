"""P05-N/O — shadow evaluation that never changes the production winner.

A shadow record contains the legacy/LIVE winner and the P05 integrated winner
computed over the same candidate universe.  It is observational: no asset is
downloaded for production, no scene is replaced, and no upload happens.

The caller is responsible for providing the *real* universe produced by the
production path (legacy pool or frozen replay).  Visual evidence is bounded by
``IntegratedConfig.max_visual_candidates``; a project budget can also cap the
number of evidence calls per scene.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from .contracts import Candidate, SceneSpec
from .integrated import IntegratedSelector
from .source_router import SourceRoute
from .unified_scoring import RerankRow


@dataclass
class ShadowRecord:
    scene_id: str
    legacy_winner: dict[str, Any] = field(default_factory=dict)
    p05_winner: dict[str, Any] = field(default_factory=dict)
    agreement: bool = False
    legacy_score: float | None = None
    p05_score: float | None = None
    evidence_calls: int = 0
    universe: int = 0
    status: str = ""
    notes: list[str] = field(default_factory=list)
    rows: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "scene_id": self.scene_id,
            "legacy_winner": dict(self.legacy_winner),
            "p05_winner": dict(self.p05_winner),
            "agreement": bool(self.agreement),
            "legacy_score": self.legacy_score,
            "p05_score": self.p05_score,
            "evidence_calls": int(self.evidence_calls),
            "universe": int(self.universe),
            "status": self.status,
            "notes": list(self.notes),
            "rows": list(self.rows),
        }


def _winner_payload(row: RerankRow | None) -> dict[str, Any]:
    if row is None:
        return {}
    candidate = row.candidate
    return {
        "provider": candidate.provider,
        "provider_asset_id": candidate.provider_asset_id,
        "asset_type": row.asset_type,
        "media_type": row.media_type,
        "score": round(float(row.final), 6),
        "relevance": round(float(row.relevance), 6),
        "url": candidate.download_url or candidate.asset_url,
        "canonical_key": candidate.canonical_key,
        "fallback_type": row.meta.get("fallback_type", ""),
        "evidence": dict(row.meta.get("evidence") or {}),
        "components": row.score.to_dict(),
    }


def run_shadow_scene(
    *,
    scene_id: str,
    spec: SceneSpec,
    universe: Iterable[tuple[Candidate, dict[str, Any]]],
    route: SourceRoute,
    selector: IntegratedSelector,
    legacy_winner: dict[str, Any] | None = None,
    max_rows: int = 12,
) -> ShadowRecord:
    items = list(universe)
    legacy = dict(legacy_winner or {})
    if not items:
        return ShadowRecord(
            scene_id=scene_id,
            legacy_winner=legacy,
            universe=0,
            status="NO_UNIVERSE",
        )
    rows, _approved, diag = selector.rank_universe(
        spec, items, route, profile=selector.config.visual_profile,
    )
    p05 = _winner_payload(rows[0] if rows else None)
    legacy_key = str(legacy.get("canonical_key") or "")
    if not legacy_key:
        legacy_key = f"{legacy.get('provider', '')}:{legacy.get('provider_asset_id', '')}"
    p05_key = str(p05.get("canonical_key") or "")
    agreement = bool(legacy_key and legacy_key == p05_key)
    notes: list[str] = []
    if legacy and not agreement:
        notes.append("DISAGREEMENT")
    if not rows:
        notes.append("NO_P05_WINNER")
    return ShadowRecord(
        scene_id=scene_id,
        legacy_winner=legacy,
        p05_winner=p05,
        agreement=agreement,
        legacy_score=_float_or_none(legacy.get("score")),
        p05_score=float(p05["score"]) if p05 else None,
        evidence_calls=int(diag.get("evidence_calls") or 0),
        universe=len(items),
        status="OK" if rows else "NO_DECISION",
        notes=notes,
        rows=[row.to_dict() for row in rows[:max_rows]],
    )


def _float_or_none(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


class ShadowRecorder:
    """Append-only JSONL recorder; one line per scene."""

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path else None
        self.records: list[ShadowRecord] = []
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)

    def add(self, record: ShadowRecord) -> None:
        self.records.append(record)
        if self.path is not None:
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record.to_dict(), ensure_ascii=False) + "\n")

    def summary(self) -> dict[str, Any]:
        total = len(self.records)
        agreed = sum(1 for record in self.records if record.agreement)
        legacy_scores = [
            record.legacy_score for record in self.records
            if record.legacy_score is not None
        ]
        p05_scores = [
            record.p05_score for record in self.records
            if record.p05_score is not None
        ]
        return {
            "scenes": total,
            "agreement": agreed,
            "agreement_rate": round(agreed / total, 4) if total else 0.0,
            "disagreements": total - agreed,
            "legacy_mean_score": round(sum(legacy_scores) / len(legacy_scores), 4)
            if legacy_scores else None,
            "p05_mean_score": round(sum(p05_scores) / len(p05_scores), 4)
            if p05_scores else None,
            "evidence_calls": sum(r.evidence_calls for r in self.records),
            "legacy_altered": False,
            "production_assets_replaced": 0,
        }
