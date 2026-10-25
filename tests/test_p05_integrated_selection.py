"""P05 — offline tests for the integrated selector (no network).

Every test uses injected retrieval/download/evidence functions.  Network
clients are never instantiated; the one file-writing test uses a temp dir.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from PIL import Image

from pipeline.media_semantics.attribution import (
    build_attribution_manifest,
    write_attribution_manifest,
)
from pipeline.media_semantics.contracts import (
    Candidate,
    MetadataStatus,
    SceneSpec,
    SelectionDecision,
)
from pipeline.media_semantics.entity_queries import (
    archive_queries,
    build_archive_query_plan,
)
from pipeline.media_semantics.integrated import (
    IntegratedConfig,
    IntegratedSelector,
    describe_universe,
    is_generated_asset,
    is_real_asset,
    routing_priors,
    stage1_universe,
    stage2_universe,
)
from pipeline.media_semantics.source_router import AssetType, SourceRouter
from pipeline.media_semantics.sourcing_pipeline import (
    SourcedMediaSelector,
    SourcingConfig,
    p04_enabled_from_env,
)
from pipeline.media_semantics.shadow import ShadowRecorder, run_shadow_scene
from pipeline.media_semantics.unified_scoring import (
    PROFILE_FULL,
    PROFILE_PREVIEW,
    PROFILE_TEXT_ONLY,
    AdaptiveBudgetConfig,
    RerankRow,
    UnifiedWeights,
    VisualEvidence,
    bounded_coherence,
    choose_visual_budget,
    global_rerank,
    profile_enabled_stages,
    score_candidate,
)
from pipeline.media_semantics.visual_evidence import P02PreviewEvidenceEvaluator


# ── helpers ────────────────────────────────────────────────────────────────
def _candidate(
    provider: str = "pexels",
    asset_type: str = AssetType.STOCK_IMAGE.value,
    *,
    pid: str = "",
    text: str = "ancient ruins in the desert",
    media: str = "image",
    title: str = "ancient ruins",
) -> Candidate:
    candidate = Candidate(
        provider=provider,
        provider_asset_id=pid or f"{provider}-{abs(hash(text)) % 100000}",
        asset_url=f"https://example.test/{provider}/asset",
        download_url=f"https://example.test/{provider}/asset.mp4",
        page_url=f"https://example.test/{provider}/page",
        title=title,
        description=text,
        tags=text.split(),
        width=1920,
        height=1080,
        media_type=media,
        asset_type=asset_type,
        license="CC-BY-4.0",
        creator="Test Creator",
        metadata_completeness=0.9,
        searchable_text=text.lower(),
    )
    candidate.metadata_status = MetadataStatus.MATCH
    return candidate


def _spec(**overrides) -> SceneSpec:
    spec = SceneSpec(
        scene_idx=1,
        narration_fragment="The ancient city of Palmyra rose from the desert.",
        subject="ancient ruins of Palmyra",
        action="rising from the desert",
        environment="Syrian desert at dusk",
        era="2nd century",
        visual_concept="lost civilization",
        must_include=["columns"],
        duration=8.0,
    )
    for key, value in overrides.items():
        setattr(spec, key, value)
    return spec


def _route(spec: SceneSpec | None = None):
    return SourceRouter().route(spec or _spec())


def _evidence(preview: float | None, **kwargs) -> VisualEvidence:
    kwargs.setdefault("technical_quality", 0.8)
    return VisualEvidence(preview_relevance=preview, **kwargs)


def _write_image(path: Path) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (64, 64), color=(120, 80, 40)).save(path, format="JPEG")
    return str(path)


# 1 / 2 — unified universe + coexist
def test_unified_universe_contains_stock_archive_and_generated():
    universe = [
        (_candidate("pexels", AssetType.STOCK_VIDEO.value, media="video"), {"asset_type": AssetType.STOCK_VIDEO.value}),
        (_candidate("pixabay_photo", AssetType.STOCK_IMAGE.value), {"asset_type": AssetType.STOCK_IMAGE.value}),
        (_candidate("wikimedia_commons", AssetType.ARCHIVAL_IMAGE.value), {"asset_type": AssetType.ARCHIVAL_IMAGE.value}),
        (_candidate("nasa_image_library", AssetType.SCIENTIFIC_IMAGE.value), {"asset_type": AssetType.SCIENTIFIC_IMAGE.value}),
        (_candidate("ai_pollinations", AssetType.AI_IMAGE.value), {"asset_type": AssetType.AI_IMAGE.value}),
    ]
    described = describe_universe(universe)
    assert described["total"] == 5
    assert described["real"] == 4
    assert described["generated"] == 1
    assert described["by_asset_type"][AssetType.ARCHIVAL_IMAGE.value] == 1
    assert len(stage1_universe(universe)) == 4
    assert len(stage2_universe(universe)) == 1


def test_image_video_archive_compete_in_one_ranking():
    spec = _spec()
    route = _route(spec)
    universe = [
        (_candidate("pexels", AssetType.STOCK_VIDEO.value, media="video", pid="v1", text="generic desert b roll"), {"asset_type": AssetType.STOCK_VIDEO.value}),
        (_candidate("pixabay_photo", AssetType.STOCK_IMAGE.value, pid="i1", text="ancient ruins palmyra desert columns"), {"asset_type": AssetType.STOCK_IMAGE.value}),
        (_candidate("wikimedia_commons", AssetType.ARCHIVAL_IMAGE.value, pid="a1", text="palmyra ruins 2nd century"), {"asset_type": AssetType.ARCHIVAL_IMAGE.value}),
    ]
    selector = IntegratedSelector(
        config=IntegratedConfig(visual_profile=PROFILE_TEXT_ONLY, real_threshold=0.01),
    )
    rows, approved, _ = selector.rank_universe(spec, universe, route)
    assert len(rows) == 3
    assert {row.asset_type for row in rows} == {
        AssetType.STOCK_VIDEO.value, AssetType.STOCK_IMAGE.value, AssetType.ARCHIVAL_IMAGE.value,
    }
    assert approved


# 3 — unified scoring components
def test_unified_scoring_separates_components_and_penalties():
    score = score_candidate(
        text_relevance=0.6,
        evidence=_evidence(0.5, frame_relevance=0.4, vlm_relevance=0.7, negative_penalty=0.5, era_penalty=0.5),
        source_prior=0.03,
        historical_prior=0.05,
        asset_type_prior=0.02,
        coherence_penalty=0.02,
        style_penalty=0.01,
        fallback_type="",
    )
    payload = score.to_dict()
    assert payload["text_relevance"] == 0.6
    assert payload["preview_visual_relevance"] == 0.5
    assert payload["frame_relevance"] == 0.4
    assert payload["vlm_relevance"] == 0.7
    assert payload["negative_penalty"] == 0.5
    assert payload["era_penalty"] == 0.5
    assert payload["routing_prior_bounded"] == pytest.approx(UnifiedWeights().routing_prior_cap)
    assert 0.0 <= payload["final_score"] <= 1.0


# 4 — missing evidence renormalisation
def test_missing_evidence_is_not_negative_evidence():
    text_only = score_candidate(text_relevance=0.6, evidence=None, profile=PROFILE_FULL)
    equal_evidence = score_candidate(
        text_relevance=0.6,
        evidence=_evidence(0.6, technical_quality=None),
        profile=PROFILE_FULL,
    )
    assert text_only.available_components == ["text"]
    assert equal_evidence.available_components == ["text", "preview"]
    # Same observed relevance → same score despite different denominators.
    assert text_only.final_score == pytest.approx(equal_evidence.final_score)
    # A genuinely observed zero DOES lower the score.
    observed_zero = score_candidate(
        text_relevance=0.6,
        evidence=_evidence(0.0, technical_quality=0.6),
        profile=PROFILE_PREVIEW,
    )
    assert observed_zero.final_score < text_only.final_score


# 5 — negative evidence penalised
def test_negative_evidence_penalised_but_missing_is_not():
    clean = score_candidate(text_relevance=0.6, evidence=None)
    bad = score_candidate(
        text_relevance=0.6,
        evidence=VisualEvidence(negative_penalty=1.0),
    )
    assert bad.final_score < clean.final_score
    assert bad.negative_penalty == 1.0


# 6 / 7 — routing prior vs relevance
def test_source_prior_cannot_dominate_a_clear_relevance_gap():
    archive = score_candidate(
        text_relevance=0.40, evidence=_evidence(0.40), source_prior=0.03,
        historical_prior=0.05, asset_type_prior=0.02,
    )
    stock = score_candidate(text_relevance=0.55, evidence=_evidence(0.55))
    assert stock.final_score > archive.final_score


def test_relevant_archive_can_beat_generic_modern_stock():
    archive = score_candidate(
        text_relevance=0.55, evidence=_evidence(0.55), source_prior=0.03,
        historical_prior=0.05, asset_type_prior=0.02,
    )
    stock = score_candidate(text_relevance=0.55, evidence=_evidence(0.55))
    assert archive.final_score > stock.final_score
    route = _route(_spec(era="ancient", subject="Palmyra ruins"))
    priors = routing_priors(route, AssetType.ARCHIVAL_IMAGE.value)
    assert archive.routing_prior_bounded <= UnifiedWeights().routing_prior_cap
    assert priors != (0.0, 0.0, 0.0)


# 8 — global rerank
def test_global_rerank_orders_all_providers_together():
    rows = [
        RerankRow(candidate=_candidate("a"), score=score_candidate(text_relevance=0.3), provider="a"),
        RerankRow(candidate=_candidate("b", pid="b2"), score=score_candidate(text_relevance=0.6), provider="b"),
        RerankRow(candidate=_candidate("c", pid="c3"), score=score_candidate(text_relevance=0.5), provider="c"),
    ]
    ranked = global_rerank(rows)
    assert [row.provider for row in ranked] == ["b", "c", "a"]


# 15 — duplicate veto
def test_duplicate_veto_removes_candidate_from_global_ranking():
    good = RerankRow(candidate=_candidate("a"), score=score_candidate(text_relevance=0.6), provider="a")
    duplicate = RerankRow(
        candidate=_candidate("b", pid="dup"),
        score=score_candidate(
            text_relevance=0.9,
            evidence=VisualEvidence(hard_reject_reason="DUPLICATE"),
        ),
        provider="b",
    )
    ranked = global_rerank([duplicate, good])
    assert [row.provider for row in ranked] == ["a"]


# 14 / 16 / 17 — coherence after relevance and caps
def test_coherence_never_promotes_a_clearly_worse_asset():
    best = RerankRow(candidate=_candidate("a"), score=score_candidate(text_relevance=0.70), provider="a")
    worse = RerankRow(candidate=_candidate("a", pid="b2"), score=score_candidate(text_relevance=0.30), provider="a")
    ranked = bounded_coherence([best, worse])
    assert ranked[0] is best
    # Outside the relevance band, no penalty is applied.
    assert worse.score.final_score == pytest.approx(0.30)


def test_provider_and_still_penalties_are_capped():
    rows = [
        RerankRow(candidate=_candidate("same", pid=f"p{i}"), score=score_candidate(text_relevance=0.60 - i * 0.01), provider="same")
        for i in range(5)
    ]
    ranked = bounded_coherence(rows, weights=UnifiedWeights())
    for row in ranked:
        assert row.score.final_score >= row.score.base_score - UnifiedWeights().coherence_cap - 1e-9
    # Re-ranking is deterministic.
    assert [r.candidate.provider_asset_id for r in ranked] == sorted(
        [r.candidate.provider_asset_id for r in ranked],
        key=lambda pid: -dict((r.candidate.provider_asset_id, r.final) for r in rows)[pid],
    )


# 9 / 10 / 11 / 12 / 26 — two-stage fallback + AI validation
def _selector_with_sources(tmp_path, *, real_text="generic desert b roll", ai_preview=0.7):
    calls = {"ai": 0}

    def provider_search(provider, query, asset_type, limit):
        if asset_type in (AssetType.STOCK_IMAGE.value, AssetType.STOCK_VIDEO.value):
            return [_candidate(provider, asset_type, pid=f"{provider}-real", text=real_text, media="video" if "VIDEO" in asset_type else "image")]
        return []

    def download_fn(candidate):
        return {
            "path": _write_image(tmp_path / f"{candidate.canonical_key.replace(':', '_')}.jpg"),
            "type": "image",
            "source": candidate.provider,
        }

    def visual_evaluator(candidate, spec):
        if candidate.provider.startswith("ai_"):
            return _evidence(ai_preview)
        return VisualEvidence(preview_relevance=0.05, status="PREVIEW_AVAILABLE")

    def ai_generator(request, spec):
        calls["ai"] += 1
        path = _write_image(tmp_path / "ai.jpg")
        return {"path": path, "type": "image"}

    selector = SourcedMediaSelector(
        config=SourcingConfig(
            integrated=True,
            visual_profile=PROFILE_PREVIEW,
            ai_enabled=True,
            ai_max_per_video=1,
            min_relevance_score=0.05,
            real_threshold=0.60,
            ai_threshold=0.05,
            max_downloads_per_scene=2,
            max_queries_per_stage=1,
            max_candidates_per_stage=4,
            entity_archive_queries=True,
        ),
        provider_search=provider_search,
        download_fn=download_fn,
        ai_generator=ai_generator,
        visual_evaluator=visual_evaluator,
        available_providers={"pexels", "pixabay_photo"},
        available_ai_backends=["pollinations"],
    )
    return selector, calls


def test_two_stage_fallback_uses_ai_only_after_real_failure(tmp_path):
    selector, calls = _selector_with_sources(tmp_path)
    result = selector.select(_spec(depiction_mode="symbolic"), scene_idx=1)
    assert calls["ai"] == 1
    assert result.decision.fallback_type == "AI_IMAGE"
    assert result.asset is not None


def test_real_asset_prevents_ai_generation(tmp_path):
    selector, calls = _selector_with_sources(tmp_path, real_text="ancient ruins palmyra desert columns")
    # Lower the real threshold for this test: the real candidate is genuine.
    selector.config.real_threshold = 0.10
    result = selector.select(_spec(), scene_idx=1)
    assert calls["ai"] == 0
    assert result.decision.fallback_type == ""
    assert result.decision.asset_type in (AssetType.STOCK_IMAGE.value, AssetType.STOCK_VIDEO.value)


def test_ai_candidate_requires_visual_validation(tmp_path):
    selector, calls = _selector_with_sources(tmp_path, ai_preview=0.01)
    result = selector.select(_spec(depiction_mode="symbolic"), scene_idx=1)
    assert calls["ai"] == 1
    assert result.decision.fallback_type != "AI_IMAGE"
    validation = result.decision.extra.get("ai_validation") or {}
    assert validation.get("accepted") is False
    assert validation.get("reason") in ("AI_BAD_OUTPUT", "ai_visual_evidence_unavailable")


def test_graceful_ai_unavailable(tmp_path):
    selector, _ = _selector_with_sources(tmp_path)
    selector.ai_generator = None
    result = selector.select(_spec(depiction_mode="symbolic"), scene_idx=1)
    assert result.decision.fallback_type != "AI_IMAGE"
    assert result.decision.extra.get("ai_skipped") == "ai_backend_not_configured"


# 27 — VLM unavailable
def test_vlm_unavailable_is_not_negative_evidence():
    no_vlm = score_candidate(
        text_relevance=0.6,
        evidence=_evidence(0.6, vlm_relevance=None, technical_quality=0.6),
    )
    with_vlm_equal = score_candidate(
        text_relevance=0.6,
        evidence=_evidence(0.6, vlm_relevance=0.6, technical_quality=0.6),
    )
    assert no_vlm.final_score == pytest.approx(with_vlm_equal.final_score)
    assert "vlm" not in no_vlm.available_components


# 13 — entity-aware archive query
def test_entity_aware_archive_queries_use_entity_and_era():
    spec = _spec(
        subject="Palmyra ruins",
        era="2nd century",
        environment="Syrian desert",
        extra={"location": "Palmyra", "entity": "Palmyra"},
    )
    plan = build_archive_query_plan(spec, max_queries=4)
    assert plan.entity == "Palmyra"
    assert "Palmyra 2nd century" in plan.queries
    assert any("Palmyra" in query for query in plan.queries)
    assert plan.queries == archive_queries(spec, max_queries=4)
    assert len(plan.queries) <= 4


# 18 — attribution pipeline
def test_attribution_manifest_is_written_next_to_outputs(tmp_path):
    decision = SelectionDecision(scene_spec=_spec(), winner=_candidate())
    decision.asset_type = AssetType.ARCHIVAL_IMAGE.value
    decision.winner_score = 0.42
    manifest = build_attribution_manifest([decision], video_id=7, channel="canal3")
    paths = write_attribution_manifest(manifest, tmp_path / "attribution", stem="video_7")
    assert Path(paths["json"]).is_file()
    assert "licence status unknown" in Path(paths["markdown"]).read_text(encoding="utf-8")
    payload = json.loads(Path(paths["json"]).read_text(encoding="utf-8"))
    assert payload["video_id"] == 7
    assert payload["scenes"][0]["provenance"]["provider"] == "pexels"


# 19 / 20 — shadow does not alter legacy winner; provenance retained
def test_shadow_keeps_legacy_winner_and_records_provenance():
    spec = _spec()
    route = _route(spec)
    legacy = _candidate("pexels", AssetType.STOCK_IMAGE.value, pid="legacy", text="generic b roll")
    better = _candidate("wikimedia_commons", AssetType.ARCHIVAL_IMAGE.value, pid="archive", text="palmyra ruins 2nd century")
    selector = IntegratedSelector(
        config=IntegratedConfig(visual_profile=PROFILE_TEXT_ONLY, real_threshold=0.01),
    )
    record = run_shadow_scene(
        scene_id="s1",
        spec=spec,
        universe=[
            (legacy, {"asset_type": AssetType.STOCK_IMAGE.value}),
            (better, {"asset_type": AssetType.ARCHIVAL_IMAGE.value}),
        ],
        route=route,
        selector=selector,
        legacy_winner={"provider": "pexels", "provider_asset_id": "legacy", "canonical_key": legacy.canonical_key, "score": 0.2},
    )
    assert record.legacy_winner["canonical_key"] == legacy.canonical_key
    assert record.p05_winner["canonical_key"] == better.canonical_key
    assert record.agreement is False
    assert record.universe == 2
    assert record.rows


def test_shadow_recorder_writes_jsonl(tmp_path):
    recorder = ShadowRecorder(tmp_path / "shadow.jsonl")
    spec = _spec()
    selector = IntegratedSelector(
        config=IntegratedConfig(visual_profile=PROFILE_TEXT_ONLY, real_threshold=0.01),
    )
    recorder.add(run_shadow_scene(
        scene_id="s1", spec=spec,
        universe=[(_candidate("a", pid="1"), {"asset_type": AssetType.STOCK_IMAGE.value})],
        route=_route(spec), selector=selector,
        legacy_winner={"provider": "a", "provider_asset_id": "1"},
    ))
    summary = recorder.summary()
    assert summary["scenes"] == 1
    assert summary["legacy_altered"] is False
    assert summary["production_assets_replaced"] == 0
    assert len((tmp_path / "shadow.jsonl").read_text().splitlines()) == 1


# 22 / 23 / 24 — profiles
def test_text_only_profile_ignores_visual_components():
    score = score_candidate(
        text_relevance=0.6, evidence=_evidence(0.9, frame_relevance=0.9, vlm_relevance=0.9),
        profile=PROFILE_TEXT_ONLY,
    )
    assert score.available_components == ["text"]
    assert set(score.available_components).isdisjoint({"preview", "frame", "vlm"})


def test_preview_visual_profile_uses_preview_only():
    score = score_candidate(
        text_relevance=0.6,
        evidence=_evidence(0.7, frame_relevance=0.9, vlm_relevance=0.9),
        profile=PROFILE_PREVIEW,
    )
    assert set(score.available_components) == {"text", "preview", "technical_quality"}
    assert "frame" not in score.available_components
    assert "vlm" not in score.available_components


def test_full_visual_profile_uses_all_observed_components():
    score = score_candidate(
        text_relevance=0.6,
        evidence=_evidence(0.7, frame_relevance=0.8, vlm_relevance=0.9),
        profile=PROFILE_FULL,
    )
    assert set(score.available_components) == {
        "text", "preview", "frame", "vlm", "technical_quality",
    }
    assert score.final_score > 0


def test_profile_enabled_stages_matrix():
    assert profile_enabled_stages(PROFILE_TEXT_ONLY) == {
        "text": True, "preview": False, "frames": False, "vlm": False,
    }
    assert profile_enabled_stages(PROFILE_PREVIEW)["preview"] is True
    assert profile_enabled_stages(PROFILE_PREVIEW)["frames"] is False
    assert profile_enabled_stages(PROFILE_FULL)["vlm"] is True


# 25 — adaptive budget
def test_adaptive_budget_chooses_cheapest_useful_stage():
    cfg = AdaptiveBudgetConfig()
    assert choose_visual_budget(text_score=0.7, text_margin=0.2, config=cfg) == "text"
    assert choose_visual_budget(text_score=0.3, text_margin=0.01, config=cfg) == "preview"
    assert choose_visual_budget(text_score=0.3, text_margin=0.2, negative_hits=1, config=cfg) == "frames"
    assert choose_visual_budget(
        text_score=0.20, text_margin=0.2, preview_margin=0.01, config=cfg,
    ) == "vlm"


# 28 — no network in standard tests
def test_core_scoring_does_not_touch_network(monkeypatch):
    import socket

    def _fail(*args, **kwargs):
        raise AssertionError("network attempted in offline test")

    monkeypatch.setattr(socket, "create_connection", _fail)
    score = score_candidate(text_relevance=0.5, evidence=_evidence(0.5))
    assert score.final_score > 0


# 21 — deterministic benchmark metrics (small, frozen fixture)
def test_benchmark_metric_computation_is_deterministic(tmp_path):
    from benchmarks.media_semantics.p05_cli import compute_arm_metrics

    rows = [
        {"scene_id": "a", "cohort": "ALL_BAD", "selected_key": "k1", "score": 0.6, "asset_type": "STOCK_IMAGE"},
        {"scene_id": "b", "cohort": "CONTROL", "selected_key": "k2", "score": 0.3, "asset_type": "STOCK_IMAGE"},
    ]
    labels = {
        "a": {"k1": 0.8, "k2": 0.1},
        "b": {"k1": 0.2, "k2": 0.7},
    }
    first = compute_arm_metrics(rows, labels, relevance_threshold=0.6)
    second = compute_arm_metrics(rows, labels, relevance_threshold=0.6)
    assert first == second
    assert first["top1_relevance_mean"] == pytest.approx(0.75)


# 26/27 handled above; 28 source-level non-regression smoke
def test_p01_p04_modules_still_import_and_p04_remains_opt_in():
    assert p04_enabled_from_env({}) is False
    assert p04_enabled_from_env({"P04_SOURCE_ROUTER": "1"}) is True
    assert SourcingConfig().integrated is False


# 30 — production defaults unchanged
def test_production_defaults_unchanged(monkeypatch):
    for key in (
        "P05_INTEGRATED", "P04_SOURCE_ROUTER", "MEDIA_SELECTOR", "P02_VISUAL_MODE",
    ):
        monkeypatch.delenv(key, raising=False)
    config = SourcingConfig.from_env({})
    assert config.integrated is False
    assert config.visual_profile == PROFILE_PREVIEW
    assert p04_enabled_from_env({}) is False


# extra: visual evaluator fail-open without model
def test_visual_evidence_evaluator_fails_open_without_backend():
    from pipeline.media_semantics.config import VisualConfig

    evaluator = P02PreviewEvidenceEvaluator(
        config=VisualConfig(mode="embedding_only", backend="none"),
    )
    candidate = _candidate(pid="no-preview", text="x")
    evidence = evaluator(candidate, _spec())
    assert evidence.has_visual is False
    assert evidence.status in ("PREVIEW_UNAVAILABLE", "VISUAL_BACKEND_UNAVAILABLE")
    assert evaluator.stats()["calls"] == 1


def test_integrated_generic_fallback_is_labelled_and_selectable(tmp_path):
    def provider_search(provider, query, asset_type, limit):
        return [_candidate(provider, asset_type, pid="generic-1", text="atmospheric documentary b roll")]

    def download_fn(candidate):
        return {"path": _write_image(tmp_path / "generic.jpg"), "type": "image", "source": candidate.provider}

    selector = SourcedMediaSelector(
        config=SourcingConfig(
            integrated=True,
            visual_profile=PROFILE_TEXT_ONLY,
            ai_enabled=False,
            ai_max_per_video=0,
            allow_conceptual=False,
            allow_generic=True,
            real_threshold=0.99,
            min_relevance_score=0.01,
            max_queries_per_stage=1,
            max_candidates_per_stage=2,
        ),
        provider_search=provider_search,
        download_fn=download_fn,
        available_providers={"pixabay_photo", "pexels_photo", "unsplash"},
    )
    result = selector.select(_spec(), scene_idx=1)
    assert result.decision.fallback_type == "GENERIC"
    assert result.decision.asset_type == AssetType.GENERIC_FALLBACK.value
    assert result.decision.literal_match is False


def test_visual_evaluator_scores_local_ai_file(tmp_path):
    from pipeline.media_semantics.config import VisualConfig

    path = _write_image(tmp_path / "ai.jpg")
    candidate = _candidate("ai_pollinations", AssetType.AI_IMAGE.value, pid="ai1", text="ruins")
    candidate.ai_generated = True
    candidate.asset_url = path
    candidate.download_url = path
    evaluator = P02PreviewEvidenceEvaluator(
        config=VisualConfig(mode="embedding_only", backend="none"),
    )
    evidence = evaluator(candidate, _spec())
    # With no model, evidence is missing (not zero) but the local file was used.
    assert evidence.status in ("VISUAL_BACKEND_UNAVAILABLE", "PREVIEW_AVAILABLE")
    assert evaluator.stats()["preview_hits"] == 1
