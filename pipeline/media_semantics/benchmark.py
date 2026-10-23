"""P01-N — benchmark corpus capture infrastructure.

Captures real scene decisions into a versioned JSON corpus so a later phase can
label relevance and calibrate ranking weights.  No labels are invented: every
human field defaults to ``None`` and is explicitly pending.

The corpus extends the replay fixture shape with provenance/labels, and can be
loaded back into a :class:`~pipeline.media_semantics.replay.ReplayHarness`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .contracts import Candidate, SceneSpec, SelectionDecision
from .replay import ReplayHarness, ReplayScene

CORPUS_VERSION = 1


@dataclass
class BenchmarkEntry:
    scene_id: str
    scene_spec: SceneSpec
    queries: list[str] = field(default_factory=list)
    candidates: list[Candidate] = field(default_factory=list)
    legacy_winner: dict[str, Any] | None = None
    semantic_winner: dict[str, Any] | None = None
    expected_visual_description: str = ""
    human_relevance_label: str | None = None
    notes: str = ""
    captured_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "scene_id": self.scene_id,
            "scene_spec": self.scene_spec.to_dict(),
            "queries": list(self.queries),
            "candidates": [c.to_dict() for c in self.candidates],
            "legacy_winner": self.legacy_winner,
            "semantic_winner": self.semantic_winner,
            "expected_visual_description": self.expected_visual_description,
            "human_relevance_label": self.human_relevance_label,
            "notes": self.notes,
            "captured_at": self.captured_at or datetime.now(timezone.utc).isoformat(),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "BenchmarkEntry":
        return cls(
            scene_id=str(data.get("scene_id") or ""),
            scene_spec=SceneSpec.from_dict(data.get("scene_spec") or {}),
            queries=list(data.get("queries") or []),
            candidates=[Candidate.from_dict(c) for c in data.get("candidates") or []],
            legacy_winner=data.get("legacy_winner"),
            semantic_winner=data.get("semantic_winner"),
            expected_visual_description=str(
                data.get("expected_visual_description") or ""
            ),
            human_relevance_label=data.get("human_relevance_label"),
            notes=str(data.get("notes") or ""),
            captured_at=str(data.get("captured_at") or ""),
        )


@dataclass
class BenchmarkCorpus:
    entries: list[BenchmarkEntry] = field(default_factory=list)

    def add_decision(
        self,
        scene_id: str,
        scene_spec: SceneSpec,
        queries: list[str],
        candidates: list[Candidate],
        *,
        legacy_decision: SelectionDecision | None = None,
        semantic_decision: SelectionDecision | None = None,
        expected_visual_description: str = "",
        human_relevance_label: str | None = None,
        notes: str = "",
    ) -> BenchmarkEntry:
        entry = BenchmarkEntry(
            scene_id=scene_id,
            scene_spec=scene_spec,
            queries=list(queries),
            candidates=list(candidates),
            legacy_winner=(
                legacy_decision.winner.to_dict()
                if legacy_decision and legacy_decision.winner else None
            ),
            semantic_winner=(
                semantic_decision.winner.to_dict()
                if semantic_decision and semantic_decision.winner else None
            ),
            expected_visual_description=expected_visual_description,
            human_relevance_label=human_relevance_label,
            notes=notes,
            captured_at=datetime.now(timezone.utc).isoformat(),
        )
        self.entries.append(entry)
        return entry

    @property
    def pending_labels(self) -> int:
        return sum(1 for e in self.entries if e.human_relevance_label is None)

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": CORPUS_VERSION,
            "pending_human_labels": self.pending_labels,
            "entries": [entry.to_dict() for entry in self.entries],
        }

    def save(self, path: str | Path) -> Path:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(self.to_dict(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return target

    @classmethod
    def load(cls, path: str | Path) -> "BenchmarkCorpus":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        version = int(data.get("version") or 0)
        if version != CORPUS_VERSION:
            raise ValueError(
                f"Unsupported benchmark corpus version {version}; "
                f"expected {CORPUS_VERSION}"
            )
        corpus = cls()
        for item in data.get("entries") or []:
            corpus.entries.append(BenchmarkEntry.from_dict(item))
        return corpus

    def to_replay_harness(self) -> ReplayHarness:
        harness = ReplayHarness()
        for entry in self.entries:
            harness.scenes.append(
                ReplayScene(
                    scene_id=entry.scene_id,
                    scene_spec=entry.scene_spec,
                    queries=list(entry.queries),
                    candidates=list(entry.candidates),
                    legacy_winner=entry.legacy_winner,
                    semantic_winner=entry.semantic_winner,
                    expected_visual_description=entry.expected_visual_description,
                    human_relevance_label=entry.human_relevance_label,
                    notes=entry.notes,
                )
            )
        return harness
