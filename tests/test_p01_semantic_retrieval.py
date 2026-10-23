"""P01 — semantic retrieval core tests.

All tests are offline: no provider APIs, no downloads, no embeddings model
download.  Provider metadata is represented by plain dicts identical in shape
to the ones produced by ``MediaFetcher._search_provider_page``.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path

import pytest

from pipeline.cinematic_staging import rank_candidates
from pipeline.media_semantics.benchmark import BenchmarkCorpus
from pipeline.media_semantics.config import (
    RankingWeights,
    RetrievalBudget,
    SemanticConfig,
)
from pipeline.media_semantics.contracts import (
    Candidate,
    MetadataStatus,
    SceneSpec,
    SelectionDecision,
    SelectionStatus,
)
from pipeline.media_semantics.normalization import (
    normalize_candidate,
    provider_prior,
)
from pipeline.media_semantics.pool import CandidatePool
from pipeline.media_semantics.provenance import decision_record
from pipeline.media_semantics.query_generation import (
    generate_scene_queries,
    generate_scene_query_variants,
)
from pipeline.media_semantics.ranking import (
    HashingEmbeddingBackend,
    NullEmbeddingBackend,
    SemanticTextRanker,
)
from pipeline.media_semantics.replay import ReplayHarness, legacy_decision
from pipeline.media_semantics.scene_spec import (
    attach_scene_specs,
    build_scene_spec,
)
from pipeline.media_semantics.selector import SemanticMediaSelector
from pipeline.scene_context import SceneVisualContext


# ── fixtures ──────────────────────────────────────────────────────

def _spec(**overrides) -> SceneSpec:
    values = dict(
        scene_idx=0,
        block_idx=0,
        origin="block",
        narration_fragment="an archaeologist excavates an ancient tomb",
        subject="archaeologist",
        action="excavating",
        environment="ancient tomb",
        era="ancient egypt",
        desired_shot="medium shot",
        visual_concept="ancient burial site",
        must_include=[],
        must_avoid=[],
        duration=6.0,
    )
    values.update(overrides)
    return SceneSpec(**values)


def _raw_good(asset_id: str = "1", provider: str = "pexels") -> dict:
    return {
        "id": asset_id,
        "url": f"https://cdn.example/{provider}/{asset_id}.mp4",
        "download_url": f"https://cdn.example/{provider}/{asset_id}.mp4",
        "page_url": f"https://www.pexels.com/video/archaeologist-excavating-ancient-tomb-{asset_id}/",
        "type": "video",
        "title": "Archaeologist excavating ancient tomb",
        "tags": ["archaeologist", "excavation", "ancient", "tomb"],
        "duration": 8.0,
        "width": 1920,
        "height": 1080,
        "source": provider,
    }


def _raw_bad(asset_id: str = "2", provider: str = "pixabay") -> dict:
    return {
        "id": asset_id,
        "url": f"https://cdn.example/{provider}/{asset_id}.mp4",
        "download_url": f"https://cdn.example/{provider}/{asset_id}.mp4",
        "page_url": f"https://pixabay.com/video/city-traffic-modern-cars-{asset_id}/",
        "type": "video",
        "title": "Modern city traffic",
        "tags": ["city", "traffic", "cars", "modern"],
        "duration": 6.0,
        "source": provider,
    }


def _candidate(raw: dict, *, provider: str | None = None, **kwargs) -> Candidate:
    return normalize_candidate(
        raw, provider=provider or raw.get("source", "pexels"), **kwargs
    )


def _selector(**config) -> SemanticMediaSelector:
    cfg = SemanticConfig(mode="semantic")
    for key, value in config.items():
        setattr(cfg, key, value)
    return SemanticMediaSelector(config=cfg)


# ── 1. SceneSpec per FINAL scene ─────────────────────────────────

def test_scene_spec_per_final_scene_preserves_fragment_and_origin():
    scenes = [
        {
            "fragment_text": "first sub-scene narration",
            "texto": "first sub-scene narration",
            "is_subscene": True,
            "asset_idx": 3,
            "media_request_id": "3:0",
            "duration": 5.0,
        },
        {
            "fragment_text": "second sub-scene narration",
            "texto": "second sub-scene narration",
            "is_subscene": True,
            "asset_idx": 3,
            "media_request_id": "3:1",
            "duration": 6.0,
        },
    ]

    specs = attach_scene_specs(
        scenes,
        lambda scene, idx: SceneVisualContext(
            fragment=scene["fragment_text"], subject=f"subject {idx}"
        ),
    )

    assert [s.narration_fragment for s in specs] == [
        "first sub-scene narration",
        "second sub-scene narration",
    ]
    assert [s.origin for s in specs] == ["subscene", "subscene"]
    assert [s.scene_idx for s in specs] == [0, 1]
    assert scenes[0]["scene_spec"]["narration_fragment"] == "first sub-scene narration"
    assert scenes[1]["final_scene_idx"] == 1
    json.dumps(scenes)  # media checkpoint must stay JSON-serialisable


def test_scene_spec_roundtrip_is_stable():
    spec = _spec()
    restored = SceneSpec.from_dict(spec.to_dict())
    assert restored == spec
    assert restored.identity == "s0:b0"


# ── 2. Query specific per sub-scene ──────────────────────────────

def test_query_is_specific_to_each_subscene():
    left = _spec(
        scene_idx=0,
        narration_fragment="the ship crosses a storm",
        subject="sailing ship",
        action="crossing",
        environment="stormy sea",
    )
    right = _spec(
        scene_idx=1,
        narration_fragment="the captain writes in the logbook",
        subject="captain",
        action="writing",
        environment="cabin",
    )

    left_queries = generate_scene_queries(left)
    right_queries = generate_scene_queries(right)

    assert left_queries[0] != right_queries[0]
    assert "ship" in left_queries[0].lower()
    assert "captain" in right_queries[0].lower()


def test_must_avoid_is_never_added_as_positive_keyword():
    spec = _spec(
        subject="ancient temple",
        action="ritual",
        environment="desert",
        must_avoid=["modern car", "neon"],
    )
    variants = generate_scene_query_variants(spec)
    joined = " ".join(v["query"].lower() for v in variants)
    assert "neon" not in joined
    assert "modern car" not in joined


# ── 3. Candidate normalization ───────────────────────────────────

def test_candidate_normalization_exposes_metadata_and_completeness():
    raw = _raw_good()
    candidate = _candidate(raw)

    assert candidate.provider == "pexels"
    assert candidate.provider_asset_id == "1"
    assert candidate.title
    assert candidate.tags == ["archaeologist", "excavation", "ancient", "tomb"]
    assert candidate.width == 1920
    assert candidate.duration == 8.0
    assert candidate.metadata_completeness > 0.5
    assert "archaeologist" in candidate.searchable_text
    assert candidate.raw_candidate == raw


def test_mixkit_page_url_slug_becomes_safe_metadata():
    raw = {
        "id": "",
        # Mixkit exposes only the landing URL (lazy download).
        "url": "https://mixkit.co/free-video/ancient-temple-123/",
        "type": "video",
        "source": "mixkit",
        "tags": [],
    }
    candidate = _candidate(raw, provider="mixkit")
    assert candidate.title == "ancient temple"
    assert candidate.metadata_status == MetadataStatus.NO_MATCH
    assert candidate.metadata_completeness > 0


# ── 4. NO_METADATA != NO_MATCH ───────────────────────────────────

def test_no_metadata_is_distinct_from_no_match():
    selector = _selector()
    spec = _spec()
    no_meta = _candidate(
        {"id": "9", "url": "https://cdn.example/9.mp4", "type": "video"},
        provider="coverr",
    )
    unrelated = _candidate(_raw_bad())

    decision = selector.rank(spec, [no_meta, unrelated])
    statuses = {row["provider_asset_id"]: row["metadata_status"] for row in decision.candidate_scores}

    assert statuses["9"] == MetadataStatus.NO_METADATA.value
    assert statuses["2"] == MetadataStatus.NO_MATCH.value


# ── 5/6/7. Candidate pool ────────────────────────────────────────

def test_candidate_pool_collects_multiple_queries_and_providers():
    pool = CandidatePool(budget=RetrievalBudget(max_total=20))
    pool.add(_candidate(_raw_good("1"), provider="pexels", query="q1"))
    pool.add(_candidate(_raw_good("2"), provider="pexels", query="q2"))
    pool.add(_candidate(_raw_good("3"), provider="pixabay", query="q1"))

    assert len(pool) == 3
    assert pool.providers == ["pexels", "pixabay"]
    assert {c.query for c in pool.candidates()} == {"q1", "q2"}


def test_candidate_pool_deduplicates_before_download():
    pool = CandidatePool(budget=RetrievalBudget(max_total=20))
    first = _candidate(_raw_good("7"), provider="pexels", query="q1")
    duplicate = _candidate(_raw_good("7"), provider="pexels", query="q2")

    assert pool.add(first) is True
    assert pool.add(duplicate) is False
    assert len(pool) == 1
    assert pool.stats.duplicates == 1


def test_candidate_pool_honours_provider_budget():
    pool = CandidatePool(budget=RetrievalBudget(max_total=20, max_per_provider=1))
    assert pool.add(_candidate(_raw_good("1"), provider="pexels")) is True
    assert pool.add(_candidate(_raw_good("2"), provider="pexels")) is False
    assert pool.stats.over_provider_budget == 1


# ── 8/9/10. Ranking ──────────────────────────────────────────────

def test_textual_ranking_prefers_semantically_related_candidate():
    selector = _selector()
    good = _candidate(_raw_good("1"))
    bad = _candidate(_raw_bad("2"))
    decision = selector.rank(_spec(), [bad, good])
    assert decision.winner is not None
    assert decision.winner.provider_asset_id == "1"
    assert decision.winner_score > 0.5


class _KeywordEmbeddingBackend:
    name = "mock_keyword"

    def encode(self, texts):
        return [[1.0, 0.0] if "tomb" in t.lower() else [0.0, 1.0] for t in texts]


def test_semantic_ranking_uses_mocked_embedding_backend():
    weights = RankingWeights(
        semantic=1.0, lexical=0.0, action=0.0, subject=0.0,
        environment=0.0, era=0.0, metadata_quality=0.0, provider_prior=0.0,
    )
    ranker = SemanticTextRanker(
        weights=weights, backend=_KeywordEmbeddingBackend()
    )
    spec = _spec()
    good = _candidate(_raw_good("1"))
    bad = _candidate(_raw_bad("2"))
    breakdowns = ranker.score_many(spec, [bad, good])
    assert breakdowns[0].semantic == 0.0
    assert breakdowns[1].semantic == pytest.approx(1.0)
    assert breakdowns[1].final > breakdowns[0].final


def test_fallback_without_embeddings_still_ranks_textually():
    weights = RankingWeights(semantic=0.35, lexical=0.65, action=0.0,
                             subject=0.0, environment=0.0, era=0.0,
                             metadata_quality=0.0, provider_prior=0.0)
    ranker = SemanticTextRanker(weights=weights, backend=NullEmbeddingBackend())
    spec = _spec()
    good = _candidate(_raw_good("1"))
    bad = _candidate(_raw_bad("2"))
    breakdowns = ranker.score_many(spec, [good, bad])
    assert all(b.semantic == 0.0 for b in breakdowns)
    assert breakdowns[0].final > breakdowns[1].final


def test_hashing_backend_is_deterministic_and_cached():
    backend = HashingEmbeddingBackend(dimensions=128)
    first = backend.encode(["ancient tomb excavation"])
    second = backend.encode(["ancient tomb excavation"])
    assert first == second
    assert len(first[0]) == 128


# ── 11/12. Global top-K ──────────────────────────────────────────

def test_global_top_k_limits_approved_candidates():
    cfg = SemanticConfig(mode="semantic")
    cfg.budget.top_k = 1
    selector = SemanticMediaSelector(config=cfg)
    decision = selector.rank(
        _spec(),
        [_candidate(_raw_good("1")), _candidate(_raw_good("2"))],
    )
    approved = getattr(decision, "approved", [])
    assert len(approved) == 1
    assert any(r.reason == "outside_top_k" for r in decision.rejected)


def test_later_provider_page_can_beat_first_provider():
    poor = _candidate(
        _raw_bad("10", provider="pexels"), provider="pexels", page=1, provider_rank=0
    )
    excellent = _candidate(
        _raw_good("11", provider="pixabay"), provider="pixabay", page=2, provider_rank=0
    )
    decision = _selector().rank(_spec(), [poor, excellent])
    assert decision.winner is not None
    assert decision.winner.provider == "pixabay"
    assert decision.winner.page == 2


# ── 13/14. Threshold and NO_GOOD_CANDIDATE ──────────────────────

def test_threshold_rejects_below_floor():
    selector = _selector()
    decision = selector.rank(_spec(), [_candidate(_raw_good("1"))], threshold=0.99)
    assert decision.status == SelectionStatus.NO_GOOD_CANDIDATE
    assert decision.rejection_reason == "no_candidate_above_threshold"


def test_no_good_candidate_when_only_metadata_less_candidates():
    selector = _selector()
    no_meta = _candidate(
        {"id": "3", "url": "https://cdn.example/3.mp4", "type": "video"},
        provider="coverr",
    )
    decision = selector.rank(_spec(), [no_meta])
    assert decision.status == SelectionStatus.NO_GOOD_CANDIDATE
    assert decision.winner is None


# ── 15. Download after ranking / fallback ───────────────────────

def test_download_only_after_ranking_and_survives_first_failure():
    selector = _selector()
    candidates = [
        _candidate(_raw_good("1")),
        _candidate(_raw_good("2")),
    ]
    calls: list[str] = []

    def download_fn(candidate):
        calls.append(candidate.provider_asset_id)
        if candidate.provider_asset_id == "1":
            return None
        return {"path": "/tmp/asset-2.mp4", "type": "video"}

    decision = selector.select_and_download(_spec(), candidates, download_fn)

    assert calls[:2] == ["1", "2"]
    assert decision.status == SelectionStatus.FALLBACK_SELECTED
    assert decision.fallback_used is True
    assert decision.winner is not None


def test_all_downloads_failing_reports_download_failed():
    selector = _selector()
    decision = selector.select_and_download(
        _spec(), [_candidate(_raw_good("1"))], lambda candidate: None
    )
    assert decision.status == SelectionStatus.DOWNLOAD_FAILED
    assert decision.winner is None


def test_download_never_touches_candidates_below_threshold():
    selector = _selector()
    calls: list[str] = []
    decision = selector.select_and_download(
        _spec(),
        [_candidate(_raw_bad("2"))],
        lambda candidate: calls.append(candidate.provider_asset_id) or {"path": "/tmp/x"},
        threshold=0.99,
    )
    assert calls == []
    assert decision.status == SelectionStatus.NO_GOOD_CANDIDATE


# ── 16. Provenance ──────────────────────────────────────────────

def test_decision_record_contains_replay_and_audit_fields():
    selector = _selector()
    candidate = _candidate(_raw_good("1"))
    decision = selector.rank(_spec(), [candidate, _candidate(_raw_bad("2"))])
    record = decision_record(decision, video_id=42, channel_id=7)

    assert record["video_id"] == 42
    assert record["scene_idx"] == 0
    assert record["block_idx"] == 0
    assert record["narration_fragment"] == "an archaeologist excavates an ancient tomb"
    assert record["status"] == SelectionStatus.SELECTED.value
    assert record["winner_provider"] == "pexels"
    assert record["threshold"] == pytest.approx(0.26)
    assert json.loads(record["scene_spec_json"])["subject"] == "archaeologist"
    assert json.loads(record["candidates_json"])
    assert json.loads(record["rejected_json"])


# ── 17. Scene identity mapping (DB, non-destructive) ────────────

def _make_identity_db(path: Path) -> None:
    from database.db_extended import _migrate_v72

    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE video_scenes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            video_id INTEGER, scene_order INTEGER, description TEXT,
            script_text TEXT, audio_path TEXT, image_path TEXT,
            image_url TEXT, subtitle_text TEXT, duration_ms INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        """
    )
    _migrate_v72(conn, logging.getLogger("p01-test"))
    conn.close()


def test_scene_identity_mapping_and_decision_persistence(tmp_path):
    from database.db_extended import ExtendedDatabase

    db_path = tmp_path / "p01.db"
    _make_identity_db(db_path)
    db = ExtendedDatabase(str(db_path))

    db.insert_scenes_batch(1, [{
        "description": "scene",
        "script_text": "",
        "final_scene_idx": 0,
        "block_idx": 3,
        "parent_scene_idx": None,
        "media_request_id": "3:0",
        "narration_fragment": "the fragment",
        "selector_version": "p01-semantic-1",
        "selection_status": "SELECTED",
    }])
    scenes = db.get_scenes(1)

    assert scenes[0]["final_scene_idx"] == 0
    assert scenes[0]["block_idx"] == 3
    assert scenes[0]["narration_fragment"] == "the fragment"

    selector = _selector()
    decision = selector.rank(_spec(), [_candidate(_raw_good("1"))])
    record = decision_record(decision, video_id=1, channel_id=2)
    assert db.insert_media_selection_decisions(1, [record]) == 1
    decisions = db.get_media_selection_decisions(1)
    assert len(decisions) == 1
    assert decisions[0]["status"] == SelectionStatus.SELECTED.value
    assert decisions[0]["scene_idx"] == 0


# ── 18. Replay offline ──────────────────────────────────────────

def test_replay_fixture_roundtrip_and_legacy_vs_semantic(tmp_path):
    harness = ReplayHarness()
    harness.add_scene(
        "video1:s0",
        _spec(),
        queries=["archaeologist excavating ancient tomb"],
        candidates=[_candidate(_raw_bad("2")), _candidate(_raw_good("1"))],
    )
    path = harness.save(tmp_path / "replay.json")
    loaded = ReplayHarness.load(path)
    results = loaded.replay()

    assert len(results) == 1
    assert results[0]["same_candidates"] is True
    assert results[0]["legacy"]["status"] == SelectionStatus.SELECTED.value
    assert results[0]["semantic"]["status"] == SelectionStatus.SELECTED.value
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["version"] == 1
    assert "scene_spec" in payload["scenes"][0]
    assert "candidates" in payload["scenes"][0]


def test_legacy_replay_accepts_zero_score_candidate():
    spec = _spec()
    unrelated = _candidate(_raw_bad("2"))
    decision = legacy_decision(spec, [unrelated])
    # Current production behaviour: a candidate wins because it is the only one.
    assert decision.status == SelectionStatus.SELECTED
    assert decision.winner is unrelated


# ── 19/20/21. Flags, legacy availability and shadow ────────────

def test_feature_flag_defaults_to_legacy_and_semantic_is_opt_in():
    assert SemanticConfig.from_media_strategy({}, env={}).mode == "legacy"
    assert SemanticConfig.from_media_strategy(
        {}, env={"MEDIA_SELECTOR": "semantic"}
    ).mode == "semantic"
    assert SemanticConfig.from_media_strategy(
        {}, env={"MEDIA_SELECTOR": "semantic", "MEDIA_SELECTOR_SHADOW": "true"}
    ).shadow is False


def test_legacy_selector_path_still_available_and_semantic_dispatch_works(monkeypatch):
    from pipeline.media_fetcher import MediaFetcher

    fetcher = object.__new__(MediaFetcher)
    fetcher._semantic_config = SemanticConfig(mode="legacy", shadow=False)
    fetcher._interleaved_providers = lambda *a, **k: ([object()], [])
    fetcher._search_provider_page = lambda *a, **k: [_raw_good("1")]
    fetcher._record_fallback_tier = lambda query: None
    fetcher._try_download_best_candidate = lambda *a, **k: {
        "path": "/tmp/legacy.mp4", "type": "video"
    }

    legacy_result = fetcher._fetch_asset_exhaustive(
        {"scene_idx": 0, "texto": "x"}, ["q"], False, 5.0, None
    )
    assert legacy_result["path"] == "/tmp/legacy.mp4"

    sentinel = {"path": "/tmp/semantic.mp4", "type": "video"}
    fetcher._semantic_config = SemanticConfig(mode="semantic", shadow=False)
    fetcher._fetch_asset_semantic = lambda *a, **k: sentinel
    semantic_result = fetcher._fetch_asset_exhaustive(
        {"scene_idx": 0, "texto": "x"}, ["q"], False, 5.0, None
    )
    assert semantic_result is sentinel


def test_semantic_fetcher_path_builds_pool_ranks_and_downloads(monkeypatch):
    """End-to-end (offline) exercise of MediaFetcher._fetch_asset_semantic."""
    from pipeline.media_fetcher import MediaFetcher

    fetcher = object.__new__(MediaFetcher)
    fetcher._semantic_config = SemanticConfig(mode="semantic")
    fetcher._semantic_selector = None
    fetcher._pending_selection_decisions = []
    fetcher._last_selection_decision = None
    fetcher._current_scene_idx = 0
    fetcher._media_strategy = {}
    fetcher._interleaved_providers = lambda *a, **k: ([object()], [])
    fetcher._search_provider_page = lambda *a, **k: [
        _raw_bad("2"), _raw_good("1"),
    ]
    fetcher._download_candidate = lambda provider, raw: {
        "path": f"/tmp/{raw.get('id')}.mp4",
        "type": raw.get("type", "video"),
        "source": raw.get("source", "pexels"),
    }
    fetcher._is_asset_duplicate = lambda raw: False
    fetcher._record_asset_used = lambda raw: None
    fetcher._record_asset_for_history = lambda asset: None
    fetcher._visual_candidate_ok = lambda asset, scene, duration: True
    fetcher._scene_context = lambda scene, scene_idx=0: SceneVisualContext(
        fragment=scene.get("fragment_text", ""),
        subject="archaeologist",
        action="excavating",
        setting="ancient tomb",
        era="ancient egypt",
    )

    scene = {
        "scene_idx": 0,
        "fragment_text": "an archaeologist excavates an ancient tomb",
        "duration": 5.0,
        "asset_idx": 2,
    }
    asset = fetcher._fetch_asset_semantic(
        scene, ["archaeologist excavating ancient tomb"], False, 5.0, None,
    )

    assert asset is not None
    assert asset["selector"] == "semantic"
    assert asset["scene_spec"]["narration_fragment"].startswith("an archaeologist")
    assert asset["candidate"]["provider_asset_id"] == "1"
    assert len(fetcher._pending_selection_decisions) == 1
    record = fetcher._pending_selection_decisions[0]
    assert record["status"] == SelectionStatus.SELECTED.value
    assert record["candidate_count"] >= 2


def test_download_candidate_preserves_lazy_provider_asset_object():
    """Mixkit/Coverr need their original VideoAsset to resolve the MP4 URL."""
    from pipeline.media_fetcher import MediaFetcher
    from pipeline.providers.base import VideoAsset

    fetcher = object.__new__(MediaFetcher)
    captured = {}

    def fake_download(asset, provider):
        captured["asset"] = asset
        return Path("/tmp/mixkit.mp4")

    fetcher._download_video_asset = fake_download
    original = VideoAsset(
        url="https://mixkit.co/free-video/ancient-temple-123/",
        file_path=Path(), duration=8.0, resolution=(1920, 1080),
        provider="mixkit",
    )
    original._mixkit_page_url = original.url  # type: ignore[attr-defined]

    result = fetcher._download_candidate(object(), {
        "type": "video",
        "url": original.url,
        "source": "mixkit",
        "_asset_obj": original,
    })

    assert captured["asset"] is original
    assert captured["asset"]._mixkit_page_url == original.url
    assert result["path"] == "/tmp/mixkit.mp4"


def test_shadow_mode_scores_without_replacing_legacy_asset():
    from pipeline.media_fetcher import MediaFetcher

    fetcher = object.__new__(MediaFetcher)
    fetcher._semantic_config = SemanticConfig(mode="legacy", shadow=True)
    shadow_calls = {"accumulate": 0, "finalize": 0}

    fetcher._interleaved_providers = lambda *a, **k: ([object()], [])
    fetcher._search_provider_page = lambda *a, **k: [_raw_good("1")]
    fetcher._record_fallback_tier = lambda query: None
    fetcher._try_download_best_candidate = lambda *a, **k: {
        "path": "/tmp/legacy.mp4", "type": "video"
    }

    def accumulate(*args, **kwargs):
        shadow_calls["accumulate"] += 1

    def finalize(*args, **kwargs):
        shadow_calls["finalize"] += 1

    fetcher._shadow_accumulate = accumulate
    fetcher._shadow_finalize = finalize

    result = fetcher._fetch_asset_exhaustive(
        {"scene_idx": 0, "texto": "x"}, ["q"], False, 5.0, None
    )

    assert result["path"] == "/tmp/legacy.mp4"
    assert shadow_calls == {"accumulate": 1, "finalize": 1}


# ── Benchmark capture ──────────────────────────────────────────

def test_benchmark_corpus_captures_unlabelled_entries(tmp_path):
    selector = _selector()
    spec = _spec()
    candidates = [_candidate(_raw_good("1")), _candidate(_raw_bad("2"))]
    legacy = legacy_decision(spec, candidates)
    semantic = selector.rank(spec, candidates)

    corpus = BenchmarkCorpus()
    corpus.add_decision(
        "video1:s0", spec, ["archaeologist excavating ancient tomb"],
        candidates, legacy_decision=legacy, semantic_decision=semantic,
    )
    path = corpus.save(tmp_path / "benchmark.json")
    loaded = BenchmarkCorpus.load(path)

    assert len(loaded.entries) == 1
    assert loaded.entries[0].human_relevance_label is None
    assert loaded.pending_labels == 1
    assert loaded.to_replay_harness().scenes[0].scene_id == "video1:s0"


# ── provider priors are bounded ─────────────────────────────────

def test_provider_priors_are_bounded_and_provider_ordering_is_not_deciding():
    assert 0.0 <= provider_prior("pexels") <= 1.0
    assert provider_prior("pexels") < 1.1
    # A weak prior cannot compensate for a clearly unrelated candidate.
    selector = _selector()
    decision = selector.rank(
        _spec(),
        [
            _candidate(_raw_bad("2", provider="pexels"), provider="pexels"),
            _candidate(_raw_good("1", provider="coverr"), provider="coverr"),
        ],
    )
    assert decision.winner is not None
    assert decision.winner.provider == "coverr"


# ── P01 reconciliation additions ────────────────────────────────

def test_subscene_without_bible_does_not_inherit_parent_action():
    """A sub-scene derives its observable action from its own fragment."""
    parent_query = "wooden sailing ship ocean expedition"
    specs = attach_scene_specs(
        [
            {
                "search_query_en": parent_query,
                "fragment_text": "El barco navega entre la tormenta",
                "is_subscene": True, "asset_idx": 0,
                "media_request_id": "0:0", "duration": 5,
            },
            {
                "search_query_en": parent_query,
                "fragment_text": "El capitán escribe el diario en cubierta",
                "is_subscene": True, "asset_idx": 0,
                "media_request_id": "0:1", "duration": 5,
            },
        ],
        lambda scene, idx: SceneVisualContext(fragment=scene["fragment_text"]),
    )
    assert specs[0].action and specs[1].action
    assert specs[0].action != specs[1].action
    assert "navega" in specs[0].action.lower()
    assert "escribe" in specs[1].action.lower()
    left = generate_scene_queries(specs[0], base_queries=[parent_query])
    right = generate_scene_queries(specs[1], base_queries=[parent_query])
    assert left[0] != right[0]
    assert any("navega" in q.lower() or "barco" in q.lower() for q in left)
    assert any("escribe" in q.lower() or "capitán" in q.lower() for q in right)


def test_pre_rank_filter_vetoes_anachronism_before_scoring():
    selector = _selector()
    modern = _candidate(_raw_good("1"), provider="pexels")
    modern.title = "modern city excavation"
    modern.searchable_text = "modern city excavation"
    decision = selector.rank(
        _spec(), [modern],
        pre_filter=lambda candidate: "anachronism" if "modern" in candidate.searchable_text else None,
    )
    assert decision.status == SelectionStatus.NO_GOOD_CANDIDATE
    assert any(r.reason == "anachronism" for r in decision.rejected)


def test_semantic_retrieval_respects_pagination_and_budget(monkeypatch):
    from pipeline.media_fetcher import MediaFetcher

    fetcher = object.__new__(MediaFetcher)
    fetcher._semantic_config = SemanticConfig(mode="semantic")
    fetcher._semantic_config.budget.max_pages_per_provider = 2
    fetcher._semantic_config.budget.max_queries = 1
    fetcher._semantic_config.budget.max_total = 20
    fetcher._semantic_selector = None
    fetcher._pending_selection_decisions = []
    fetcher._last_selection_decision = None
    fetcher._current_scene_idx = 0
    fetcher._media_strategy = {}
    fetcher._interleaved_providers = lambda *a, **k: ([object()], [])
    pages: list[int] = []

    def fake_search(provider, query, target_dur, page, want_video):
        pages.append(page)
        return [{
            "id": f"p{page}", "title": "archaeologist excavating ancient tomb",
            "tags": ["archaeologist", "ancient", "tomb"], "type": "video",
            "download_url": f"https://cdn.example/p{page}.mp4",
            "source": "pexels", "page_url": (
                f"https://pexels.com/video/archaeologist-excavating-ancient-tomb-{page}/"
            ),
            "_total_available": 80, "_per_page": 20,
        }]

    fetcher._search_provider_page = fake_search
    fetcher._download_candidate = lambda provider, raw: {
        "path": f"/tmp/{raw['id']}.mp4", "type": "video", "source": "pexels",
    }
    fetcher._is_asset_duplicate = lambda raw: False
    fetcher._is_anachronistic_for_scene = lambda raw, ctx, scene_ctx: False
    fetcher._record_asset_used = lambda raw: None
    fetcher._record_asset_for_history = lambda asset: None
    fetcher._visual_candidate_ok = lambda asset, scene, duration: True
    fetcher._scene_context = lambda scene, scene_idx=0: SceneVisualContext(
        fragment=scene.get("fragment_text", ""),
        subject="archaeologist", action="excavating", setting="ancient tomb",
    )

    asset = fetcher._fetch_asset_semantic(
        {
            "scene_idx": 0,
            "fragment_text": "an archaeologist excavates an ancient tomb",
            "search_query_en": "archaeologist ancient tomb",
            "duration": 5.0, "asset_idx": 0, "media_request_id": "0:0",
        },
        ["archaeologist ancient tomb"], False, 5.0, None,
    )
    assert asset is not None
    assert pages[:2] == [1, 2]       # paginated
    assert 3 not in pages            # budget respected
    assert fetcher._last_selection_decision is not None


def test_replay_is_deterministic_and_fixture_roundtrips(tmp_path):
    harness = ReplayHarness()
    harness.add_scene(
        "video1:s0",
        _spec(media_request_id="0:0"),
        queries=["archaeologist excavating ancient tomb"],
        candidates=[_candidate(_raw_bad("2")), _candidate(_raw_good("1"))],
        expected_visual_description="archaeologist in a tomb",
    )
    path = harness.save(tmp_path / "replay.json")
    loaded = ReplayHarness.load(path)
    first = loaded.replay()
    second = loaded.replay()
    assert first == second
    assert first[0]["same_candidates"] is True
    assert first[0]["semantic"]["winner"] is not None
    assert first[0]["semantic"]["winner"]["provider_asset_id"] == "1"


def test_media_request_id_is_persisted_in_scene_and_decision(tmp_path):
    from database.db_extended import ExtendedDatabase

    db_path = tmp_path / "p01.db"
    _make_identity_db(db_path)
    db = ExtendedDatabase(str(db_path))
    db.insert_scenes_batch(1, [{
        "description": "scene", "final_scene_idx": 0, "block_idx": 3,
        "media_request_id": "3:1",
        "narration_fragment": "the fragment",
    }])
    scenes = db.get_scenes(1)
    assert scenes[0]["media_request_id"] == "3:1"

    selector = _selector()
    decision = selector.rank(_spec(media_request_id="3:1"), [_candidate(_raw_good("1"))])
    record = decision_record(decision, video_id=1)
    assert record["media_request_id"] == "3:1"
    assert db.insert_media_selection_decisions(1, [record]) == 1
    rows = db.get_media_selection_decisions(1)
    assert rows[0]["media_request_id"] == "3:1"


def test_shadow_alias_and_default_legacy():
    assert SemanticConfig.from_media_strategy({}, env={}).mode == "legacy"
    shadow = SemanticConfig.from_media_strategy(
        {}, env={"MEDIA_SELECTOR": "shadow"}
    )
    assert shadow.mode == "legacy" and shadow.shadow is True
    semantic = SemanticConfig.from_media_strategy(
        {}, env={"MEDIA_SELECTOR": "semantic"}
    )
    assert semantic.mode == "semantic" and semantic.shadow is False


def test_v72_reconciliation_adds_column_to_preexisting_table(tmp_path):
    """DBs that already ran the first v72 revision get the new column safely."""
    from database.db_extended import _migrate_v72

    db_path = tmp_path / "old-v72.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE video_scenes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            video_id INTEGER, scene_order INTEGER, description TEXT,
            script_text TEXT, audio_path TEXT, image_path TEXT,
            image_url TEXT, subtitle_text TEXT, duration_ms INTEGER,
            block_idx INTEGER, final_scene_idx INTEGER, parent_scene_idx INTEGER,
            narration_fragment TEXT, selector_version TEXT, selection_status TEXT
        );
        CREATE TABLE media_selection_decisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            video_id INTEGER, scene_idx INTEGER, block_idx INTEGER,
            status TEXT, selector_version TEXT, created_at TIMESTAMP
        );
        """
    )
    _migrate_v72(conn, logging.getLogger("p01-test"))
    _migrate_v72(conn, logging.getLogger("p01-test"))
    cols = {
        row[1] for row in conn.execute(
            "PRAGMA table_info('media_selection_decisions')"
        )
    }
    assert "media_request_id" in cols
    # Existing data survives the additive migration.
    assert conn.execute(
        "SELECT COUNT(*) FROM media_selection_decisions"
    ).fetchone()[0] == 0
    conn.close()
