"""P01-M — offline replay harness.

A fixture freezes ``SceneSpec`` + queries + normalised candidate metadata, so
both the legacy ordering and the semantic selector can be replayed with no
network, no provider APIs and no downloads.

Fixture format (version 1, JSON, stable/readable)::

    {
      "version": 1,
      "selector_version": "p01-semantic-1",
      "scenes": [
        {
          "scene_id": "video12:s3",
          "scene_spec": {...},
          "queries": [...],
          "candidates": [...],
          "legacy_winner": {...} | null,
          "semantic_winner": {...} | null,
          "expected_visual_description": "",
          "human_relevance_label": null,
          "notes": ""
        }
      ]
    }
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .config import SemanticConfig
from .contracts import Candidate, SceneSpec, SelectionDecision, SelectionStatus
from .selector import SELECTOR_VERSION, SemanticMediaSelector


FIXTURE_VERSION = 1


class _LegacyContext:
    """Duck-typed SceneVisualContext for ``cinematic_staging.rank_candidates``."""

    def __init__(self, spec: SceneSpec) -> None:
        self.subject = spec.subject
        self.action = spec.action
        self.object = ""
        self.setting = spec.environment
        self.fragment = spec.narration_fragment
        self.visual_concept = spec.visual_concept
        self.bridge_from_prev = ""
        self.central_entity = ""
        self.recurring_elements: list[str] = []
        self.must_show = list(spec.must_include)
        self.must_avoid = list(spec.must_avoid)
        self.era = spec.era
        self.phase_id = ""


def legacy_decision(spec: SceneSpec, candidates: list[Candidate]) -> SelectionDecision:
    """Replay the current deterministic legacy ordering on a frozen pool.

    This mirrors the production Fase-4a ordering (``rank_candidates``) used by
    ``MediaFetcher._try_download_best_candidate``.  It intentionally accepts a
    candidate with score 0 when it is the only option — that is exactly the
    behaviour P01 replaces behind the semantic flag.
    """
    decision = SelectionDecision(
        scene_spec=spec,
        selector_version="legacy-replay",
        threshold=0.0,
        candidates_considered=len(candidates),
        providers_consulted=sorted({c.provider for c in candidates}),
        queries=list(dict.fromkeys(c.query for c in candidates if c.query)),
    )
    if not candidates:
        decision.status = SelectionStatus.RETRIEVAL_FAILED
        decision.rejection_reason = "no_candidates_retrieved"
        return decision

    try:
        from pipeline.cinematic_staging import rank_candidates

        raw = [c.to_dict() for c in candidates]
        ordered_raw = rank_candidates(
            raw,
            spec.narration_fragment or spec.subject or "",
            None,
            scene_context=_LegacyContext(spec),
        )
    except Exception:
        ordered_raw = [c.to_dict() for c in candidates]

    by_key = {c.canonical_key: c for c in candidates}
    ordered: list[Candidate] = []
    for item in ordered_raw:
        probe = Candidate.from_dict(item)
        key = probe.canonical_key
        if key in by_key:
            ordered.append(by_key[key])
    if not ordered:
        ordered = list(candidates)

    decision.winner = ordered[0]
    decision.status = SelectionStatus.SELECTED
    decision.winner_score = 0.0
    decision.extra["legacy_order"] = [c.canonical_key for c in ordered[:10]]
    return decision


@dataclass
class ReplayScene:
    scene_id: str
    scene_spec: SceneSpec
    queries: list[str] = field(default_factory=list)
    candidates: list[Candidate] = field(default_factory=list)
    legacy_winner: dict[str, Any] | None = None
    semantic_winner: dict[str, Any] | None = None
    expected_visual_description: str = ""
    human_relevance_label: str | None = None
    notes: str = ""

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
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ReplayScene":
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
        )


@dataclass
class ReplayHarness:
    scenes: list[ReplayScene] = field(default_factory=list)

    def add_scene(
        self,
        scene_id: str,
        scene_spec: SceneSpec,
        queries: list[str] | None = None,
        candidates: list[Candidate] | None = None,
        *,
        expected_visual_description: str = "",
        human_relevance_label: str | None = None,
        notes: str = "",
    ) -> ReplayScene:
        scene = ReplayScene(
            scene_id=scene_id,
            scene_spec=scene_spec,
            queries=list(queries or []),
            candidates=list(candidates or []),
            expected_visual_description=expected_visual_description,
            human_relevance_label=human_relevance_label,
            notes=notes,
        )
        self.scenes.append(scene)
        return scene

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": FIXTURE_VERSION,
            "selector_version": SELECTOR_VERSION,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "scenes": [scene.to_dict() for scene in self.scenes],
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
    def load(cls, path: str | Path) -> "ReplayHarness":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        version = int(data.get("version") or 0)
        if version != FIXTURE_VERSION:
            raise ValueError(
                f"Unsupported replay fixture version {version}; expected {FIXTURE_VERSION}"
            )
        harness = cls()
        for item in data.get("scenes") or []:
            harness.scenes.append(ReplayScene.from_dict(item))
        return harness

    def replay(
        self,
        *,
        config: SemanticConfig | None = None,
        threshold: float | None = None,
    ) -> list[dict[str, Any]]:
        """Run both selectors over the frozen pools; no network/downloads."""
        config = config or SemanticConfig(mode="semantic")
        selector = SemanticMediaSelector(config=config)
        results: list[dict[str, Any]] = []
        for scene in self.scenes:
            legacy = legacy_decision(scene.scene_spec, scene.candidates)
            semantic = selector.rank(
                scene.scene_spec, scene.candidates, threshold=threshold
            )
            # Offline replay must be byte-for-byte deterministic: wall-clock
            # timing is metadata of the live run, not part of the decision.
            semantic.timing_ms = 0.0
            scene.legacy_winner = (
                legacy.winner.to_dict() if legacy.winner is not None else None
            )
            scene.semantic_winner = (
                semantic.winner.to_dict() if semantic.winner is not None else None
            )
            results.append({
                "scene_id": scene.scene_id,
                "same_candidates": True,
                "candidate_count": len(scene.candidates),
                "legacy": {
                    "status": legacy.status.value,
                    "winner": scene.legacy_winner,
                },
                "semantic": {
                    "status": semantic.status.value,
                    "winner": scene.semantic_winner,
                    "score": semantic.winner_score,
                    "threshold": semantic.threshold,
                },
                "agreement": (
                    legacy.winner.canonical_key if legacy.winner else None
                ) == (
                    semantic.winner.canonical_key if semantic.winner else None
                ),
                "detail": {
                    "legacy": legacy.to_dict(),
                    "semantic": semantic.to_dict(),
                },
            })
        return results

    def replay_visual(
        self,
        *,
        config: SemanticConfig | None = None,
        visual_backend: Any = None,
        preview_fetcher: Any = None,
        frame_sampler: Any = None,
        vlm_backend: Any = None,
        source_loader: Callable[[Candidate], dict | None] | None = None,
        threshold: float | None = None,
    ) -> list[dict[str, Any]]:
        """Replay the P02 visual funnel with local fixtures only.

        ``source_loader`` maps a frozen candidate to a local asset dict (e.g.
        ``{"path": "/fixtures/clip.mp4", "type": "video"}``).  Preview URLs in
        the fixture may be local paths.  Nothing in this method touches the
        network.
        """
        from .config import VisualConfig
        from .selector import SemanticMediaSelector
        from .visual_pipeline import VisualIntelligencePipeline

        if config is None:
            config = SemanticConfig(
                mode="semantic",
                visual=VisualConfig(mode="full_visual"),
            )
        elif config.visual.mode in ("", "off"):
            config.visual = VisualConfig(mode="full_visual")
        selector = SemanticMediaSelector(config=config)

        def download_fn(candidate: Candidate) -> dict | None:
            if source_loader is None:
                return None
            try:
                return source_loader(candidate)
            except Exception:
                return None

        pipeline = VisualIntelligencePipeline(
            config=config,
            textual_selector=selector,
            visual_backend=visual_backend,
            vlm_backend=vlm_backend,
            preview_fetcher=preview_fetcher,
            frame_sampler=frame_sampler,
            download_fn=download_fn,
        )
        results: list[dict[str, Any]] = []
        for scene in self.scenes:
            decision = pipeline.select_and_download(
                scene.scene_spec, scene.candidates, download_fn,
                threshold=threshold,
            )
            decision.timing_ms = 0.0
            decision.visual_timing_ms = 0.0
            payload = decision.to_dict()
            results.append({
                "scene_id": scene.scene_id,
                "candidate_count": len(scene.candidates),
                "status": decision.status.value,
                "winner": (
                    decision.winner.to_dict() if decision.winner is not None else None
                ),
                "visual": decision.visual_summary(),
                "detail": payload,
            })
        return results

    def summary(self, results: list[dict[str, Any]]) -> dict[str, Any]:
        total = len(results)
        agreements = sum(1 for r in results if r.get("agreement"))
        semantic_selected = sum(
            1 for r in results if r["semantic"]["status"] in (
                SelectionStatus.SELECTED.value,
                SelectionStatus.FALLBACK_SELECTED.value,
            )
        )
        no_good = sum(
            1 for r in results
            if r["semantic"]["status"] == SelectionStatus.NO_GOOD_CANDIDATE.value
        )
        return {
            "scenes": total,
            "agreements": agreements,
            "disagreements": total - agreements,
            "semantic_selected": semantic_selected,
            "semantic_no_good_candidate": no_good,
        }


def replay_file(
    path: str | Path,
    *,
    config: SemanticConfig | None = None,
    threshold: float | None = None,
) -> dict[str, Any]:
    """Load a fixture, replay it and return results + summary."""
    harness = ReplayHarness.load(path)
    results = harness.replay(config=config, threshold=threshold)
    return {
        "fixture": str(path),
        "results": results,
        "summary": harness.summary(results),
    }
