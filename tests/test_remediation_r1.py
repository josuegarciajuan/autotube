"""R1 — post-closure remediation regression tests (offline, no network)."""

from __future__ import annotations

import json
import os
import re
import time
from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image

from pipeline.json_repair import parse_json_lenient
from pipeline.media_semantics.contracts import Candidate, MetadataStatus, SceneSpec
from pipeline.media_semantics.failure_cache import FailedAssetCache
from pipeline.media_semantics.integrated import IntegratedConfig, IntegratedSelector
from pipeline.media_semantics.ranking import ScoreBreakdown
from pipeline.media_semantics.source_router import SourceRouter
from pipeline.media_semantics.sourcing_pipeline import (
    SourcedMediaSelector,
    SourcingConfig,
)
from pipeline.media_semantics.unified_scoring import PROFILE_TEXT_ONLY
from pipeline.visual_bible import VisualBible, VisualBibleGenerator


# ── helpers ────────────────────────────────────────────────────────────────
def _candidate(provider="pexels", pid="c1", *, text="ancient ruins in the desert",
               title="ancient ruins", media="image", asset_type="STOCK_IMAGE"):
    candidate = Candidate(
        provider=provider,
        provider_asset_id=pid,
        asset_url=f"https://example.test/{provider}/{pid}.jpg",
        download_url=f"https://example.test/{provider}/{pid}.jpg",
        page_url=f"https://example.test/{provider}/{pid}",
        title=title,
        description=text,
        tags=text.split(),
        width=1920,
        height=1080,
        media_type=media,
        asset_type=asset_type,
    )
    candidate.metadata_status = MetadataStatus.MATCH
    candidate.metadata_completeness = 0.9
    candidate.searchable_text = text.lower()
    return candidate


def _spec(scene_idx=1, **overrides):
    spec = SceneSpec(
        scene_idx=scene_idx,
        narration_fragment="The ancient city rose from the desert.",
        subject="ancient ruins of Palmyra",
        action="rising from the desert",
        environment="Syrian desert at dusk",
        era="2nd century",
        visual_concept="lost civilization",
        desired_shot="wide",
        duration=8.0,
    )
    for key, value in overrides.items():
        setattr(spec, key, value)
    return spec


def _route(spec=None):
    return SourceRouter().route(spec or _spec())


class _FakeRanker:
    """Deterministic text ranker: provider_asset_id → pre-coherence score."""

    def __init__(self, finals):
        self.finals = dict(finals)

    def score_many(self, spec, candidates):
        out = []
        for candidate in candidates:
            breakdown = ScoreBreakdown(
                final=float(self.finals.get(candidate.provider_asset_id, 0.0)),
                metadata_status=MetadataStatus.MATCH.value,
            )
            out.append(breakdown)
        return out


def _integrated(final_map, threshold=0.0):
    selector = IntegratedSelector(
        config=IntegratedConfig(
            visual_profile=PROFILE_TEXT_ONLY,
            real_threshold=threshold,
            stage2_threshold=threshold,
            adaptive_budget=False,
        ),
        ranker=_FakeRanker(final_map),
    )
    return selector


# 1 — integrated rerank invokes coherence and exposes signals
def test_integrated_rerank_invokes_coherence():
    spec = _spec()
    selector = _integrated({"c1": 0.50})
    seed = _candidate(provider="pexels", pid="seed", text="ancient ruins in the desert")
    selector.coherence.record(seed, spec=spec, asset_type="STOCK_IMAGE", media_type="image")
    universe = [(_candidate(provider="pexels", pid="c1"), {"asset_type": "STOCK_IMAGE"})]
    rows, approved, diag = selector.rank_universe(spec, universe, _route(spec))
    assert diag["coherence_window"] == 1
    assert diag["coherence_in_band"] == 1
    assert rows[0].meta["coherence"]["reasons"]
    assert "provider_repeated:pexels:1/1" in rows[0].meta["coherence"]["reasons"]


# 2 — the signal reaches unified scoring (not just metadata)
def test_coherence_signal_reaches_unified_scoring():
    spec = _spec()
    selector = _integrated({"c1": 0.50})
    seed = _candidate(provider="pexels", pid="seed", text="ancient ruins in the desert")
    selector.coherence.record(seed, spec=spec, asset_type="STOCK_IMAGE", media_type="image")
    rows, _, _ = selector.rank_universe(
        spec, [(_candidate(provider="pexels", pid="c1"), {"asset_type": "STOCK_IMAGE"})],
        _route(spec),
    )
    assert rows[0].score.coherence_penalty > 0.0
    assert rows[0].meta["coherence_applied_penalty"] > 0.0
    # Capped: relevance is never buried by coherence.
    assert rows[0].score.final_score >= rows[0].score.base_score - 0.05 - 1e-9


# 3 — strong relevance beats coherence
def test_strong_relevance_beats_coherence():
    spec = _spec()
    selector = _integrated({"a": 0.70, "b": 0.30})
    seed = _candidate(provider="pa", pid="seed", text="ancient ruins in the desert")
    selector.coherence.record(seed, spec=spec, asset_type="STOCK_IMAGE", media_type="image")
    universe = [
        (_candidate(provider="pa", pid="a"), {"asset_type": "STOCK_IMAGE"}),
        (_candidate(provider="pb", pid="b"), {"asset_type": "STOCK_IMAGE"}),
    ]
    rows, _, _ = selector.rank_universe(spec, universe, _route(spec))
    assert rows[0].provider == "pa"
    # The clearly worse asset was outside the relevance band: no penalty.
    low = next(row for row in rows if row.provider == "pb")
    assert low.meta["coherence_in_band"] is False
    assert low.meta["coherence_applied_penalty"] == 0.0


# 4 — coherence can break a near tie
def test_coherence_breaks_near_tie():
    spec = _spec()
    selector = _integrated({"a1": 0.50, "b1": 0.50})
    # Seed only the provider context: no shared subject/shot/era signal, so
    # the provider repetition is the signal that can break the tie.
    seed_spec = _spec(subject="volcano eruption", desired_shot="close")
    seed = _candidate(provider="pa", pid="seed", text="volcano eruption footage")
    selector.coherence.record(
        seed, spec=seed_spec, asset_type="STOCK_IMAGE", media_type="image",
    )
    universe = [
        (_candidate(provider="pa", pid="a1"), {"asset_type": "STOCK_IMAGE"}),
        (_candidate(provider="pb", pid="b1"), {"asset_type": "STOCK_IMAGE"}),
    ]
    rows, _, diag = selector.rank_universe(spec, universe, _route(spec))
    assert rows[0].provider == "pb"
    assert diag["coherence_ordering_changed"] is True


# ── selector failover helpers ─────────────────────────────────────────────
def _failover_selector(final_map, failing_ids, *, allow_fallbacks=False):
    calls = {"downloads": 0, "ids": []}

    def provider_search(provider, query, asset_type, limit):
        if asset_type != "STOCK_IMAGE" or provider != "pixabay_photo":
            return []
        return [
            _candidate(provider="pexels", pid=pid, text=f"candidate {pid}")
            for pid in final_map
        ]

    def download_fn(candidate):
        calls["downloads"] += 1
        calls["ids"].append(candidate.provider_asset_id)
        if candidate.provider_asset_id in failing_ids:
            return None
        path = Path(os.environ.get("TMPDIR", "/tmp")) / f"r1-{candidate.provider_asset_id}.jpg"
        Image.new("RGB", (64, 64), (10, 20, 30)).save(path, "JPEG")
        return {"path": str(path), "type": "image", "source": candidate.provider}

    selector = SourcedMediaSelector(
        config=SourcingConfig(
            integrated=True,
            visual_profile=PROFILE_TEXT_ONLY,
            ai_enabled=False,
            ai_max_per_video=0,
            allow_conceptual=allow_fallbacks,
            allow_generic=allow_fallbacks,
            min_relevance_score=0.18,
            real_threshold=0.0,
            max_downloads_per_scene=2,
            max_queries_per_stage=1,
            max_candidates_per_stage=4,
        ),
        provider_search=provider_search,
        download_fn=download_fn,
        ranker=_FakeRanker(final_map),
        available_providers={"pixabay_photo"},
    )
    return selector, calls


# 5 — the actual downloaded winner advances the coherence context
def test_actual_downloaded_winner_updates_coherence_context():
    selector, calls = _failover_selector({"fail": 0.40, "ok": 0.32}, {"fail"})
    result = selector.select(_spec(scene_idx=1), scene_idx=1)
    assert result.decision.selected
    assert result.decision.winner.provider_asset_id == "ok"
    assert len(selector.coherence) == 1
    assert selector.coherence.recent()[0]["key"] == "pexels:id:ok"


# 6 — a failed top candidate cannot silently win at a catastrophic score
def test_failed_top_candidate_cannot_win_catastrophically():
    selector, calls = _failover_selector({"top": 0.375, "bad": 0.05}, {"top"})
    result = selector.select(_spec(scene_idx=1), scene_idx=1)
    assert result.decision.winner is None
    assert result.decision.status.value == "NO_GOOD_CANDIDATE"
    failover = result.decision.extra["failover"]
    assert failover["quality_floor_rejections"] == 1
    assert failover["quality_floor"] >= 0.255 - 1e-9
    assert any(a["result"] == "rejected_quality_floor"
               for a in result.decision.download_attempts)
    assert len(selector.coherence) == 0


# 7 — an acceptable second candidate can still win after failover
def test_acceptable_second_candidate_still_wins():
    selector, _ = _failover_selector({"top": 0.375, "second": 0.32}, {"top"})
    result = selector.select(_spec(scene_idx=1), scene_idx=1)
    assert result.decision.selected
    assert result.decision.winner.provider_asset_id == "second"
    assert result.decision.extra["failover"]["active"] is True
    assert result.decision.extra["failover"]["quality_floor_rejections"] == 0


# 8 — a failed URL is skipped on a subsequent scene (no repeated retries)
def test_failed_url_skipped_on_subsequent_scene():
    selector, calls = _failover_selector({"broken": 0.40}, {"broken"})
    first = selector.select(_spec(scene_idx=1), scene_idx=1)
    assert not first.decision.selected
    downloads_after_first = calls["downloads"]
    second = selector.select(_spec(scene_idx=2), scene_idx=2)
    assert second.decision.winner is None
    assert calls["downloads"] == downloads_after_first  # cache skipped, no retry
    assert any(a["result"] == "skipped_cached_failure"
               for a in second.decision.download_attempts)
    assert second.decision.extra["failover"]["failed_cache_hits"] == 1


# 9 — the failure cache is TTL-scoped and bounded (not a global blacklist)
def test_failure_cache_is_scoped_and_bounded():
    now = [1000.0]
    cache = FailedAssetCache(ttl_s=10, max_entries=2, clock=lambda: now[0])
    for pid in ("a", "b", "c"):
        cache.record(_candidate(pid=pid), failure_category="download_failed")
    stats = cache.stats()
    assert stats["entries"] == 2
    assert stats["evictions"] == 1
    current = _candidate(pid="c")
    assert cache.is_failed(current) is True
    now[0] += 11
    assert cache.is_failed(current) is False
    assert cache.stats()["entries"] == 0


# 10 — valid PNG bytes behind a .jpg URL are accepted and normalised
def test_valid_png_behind_jpg_url_is_accepted(tmp_path, monkeypatch):
    import pipeline.media_fetcher as mf

    raw = BytesIO()
    Image.frombytes("RGB", (1200, 800), os.urandom(1200 * 800 * 3)).save(raw, "PNG")
    payload = raw.getvalue()
    assert payload.startswith(b"\x89PNG")

    class _Resp:
        content = payload
        def raise_for_status(self):
            return None

    monkeypatch.setattr(mf.requests, "get", lambda *a, **k: _Resp())
    monkeypatch.setattr(mf.settings, "IMAGES_DIR", tmp_path)

    fetcher = object.__new__(mf.MediaFetcher)
    fetcher._bad_image_urls = set()
    fetcher._bad_image_urls_ts = 0.0
    path = fetcher._download_image("https://pixabay.com/x_1280.jpg", "pixabay_x.jpg")
    assert path is not None and path.exists()
    assert mf.MediaFetcher._image_magic_format(path.read_bytes()) == "JPEG"
    with Image.open(path) as image:
        assert image.size == (1200, 800)


# 11 — invalid payloads are still rejected
def test_invalid_image_payload_is_rejected(tmp_path):
    import pipeline.media_fetcher as mf

    assert mf.MediaFetcher._image_magic_format(b"<html><body>403</body></html>") == ""
    assert mf.MediaFetcher._image_magic_format(b"PK\x03\x04zip") == ""
    fake = tmp_path / "not-image.jpg"
    fake.write_bytes(b"<html>" + b"x" * 60000)
    assert mf.MediaFetcher._is_valid_image(fake) is False


# 12 — motion plan adopts the FINAL split duration
def test_motion_plan_uses_final_split_duration():
    import pipeline.media_fetcher as mf

    asset = {
        "path": "x.jpg",
        "type": "image",
        "motion_plan": {"duration": 25.479, "zoom_percent": 5.0, "profile": "horizontal_out"},
    }
    mf.MediaFetcher._sync_asset_motion_duration(asset, 6.764)
    assert asset["motion_plan"]["duration"] == 6.764
    assert asset["motion_plan"]["duration_source"] == "final_scene_split"
    assert asset["motion_plan"]["zoom_percent"] == 5.0  # no excessive zoom change


# ── visual bible helpers ──────────────────────────────────────────────────
def _entries(indices):
    return [
        {
            "scene": int(i),
            "shot_type": "mood",
            "visual_concept": f"concept {i}",
            "mood": "neutral",
            "has_protagonist": False,
            "bridge_from_prev": None,
            "visual_density": "balanced",
        }
        for i in indices
    ]


class _ModelEntry:
    def __init__(self, model_id):
        self.model_id = model_id
        self.display_name = model_id


class _ModelPool:
    def __init__(self, names):
        self.names = names

    def iter_models(self):
        for name in self.names:
            yield _ModelEntry(name), object()


class _FakeScriptGen:
    def __init__(self, fn, names=("primary", "fallback")):
        self.model_pool = _ModelPool(names)
        self.fn = fn
        self.calls = 0

    def _llm_json_call(self, **kwargs):
        self.calls += 1
        return self.fn(kwargs)


def _batch_from_kwargs(kwargs):
    prompt = kwargs["messages"][1]["content"]
    match = re.search(r"\[([0-9, ]+)\]", prompt)
    if not match:
        return []
    return [int(x) for x in match.group(1).split(",") if x.strip()]


def _fake_gen(fn, names=("primary", "fallback")):
    gen = VisualBibleGenerator(_FakeScriptGen(fn, names), channel_cfg={})
    return gen


# 13 — malformed JSON is recovered (fenced, trailing comma, truncation)
def test_visual_bible_malformed_json_recovery():
    assert parse_json_lenient('```json\n{"scene_visual_map": [\n```') == {
        "scene_visual_map": [],
    }
    recovered = parse_json_lenient(
        '```json\n{"scene_visual_map": [{"scene": 0, "visual_concept": "a"},]}\n```'
    )
    assert recovered["scene_visual_map"][0]["scene"] == 0
    truncated = parse_json_lenient(
        '{"scene_visual_map": [{"scene": 0, "visual_concept": "a"}, '
        '{"scene": 1, "visual_concept":'
    )
    assert [
        e["scene"] for e in truncated["scene_visual_map"]
        if str(e.get("visual_concept") or "").strip()
    ] == [0]


# 13b — ScriptGenerator._llm_json_call repairs instead of spending a retry
def test_llm_json_call_repairs_malformed_payload():
    from pipeline.script_generator import ScriptGenerator

    class _Message:
        content = '```json\n{"scene_visual_map": [{"scene": 0, "visual_concept": "a"},]}\n```'

    class _Choice:
        message = _Message()

    class _Response:
        choices = [_Choice()]

    class _Completions:
        def create(self, **kwargs):
            return _Response()

    class _Chat:
        completions = _Completions()

    class _Client:
        chat = _Chat()

    sg = ScriptGenerator.__new__(ScriptGenerator)
    sg._llm_retries = 3
    sg._llm_retry_delay = 0.0
    sg._legacy_client = _Client()
    sg._legacy_client_no_thinking = _Client()
    parsed = sg._llm_json_call(
        thinking=True, model="primary", model_name="primary",
        messages=[{"role": "user", "content": "x"}],
    )
    assert parsed["scene_visual_map"][0]["visual_concept"] == "a"


# 13c — generator stats show recovery instead of padding collapse
def test_visual_bible_generate_reports_recovery_stats():
    calls = {"n": 0}

    def fn(kwargs):
        calls["n"] += 1
        batch = _batch_from_kwargs(kwargs)
        if batch:
            return {"scene_visual_map": _entries(batch)}
        return {
            "visual_universe": "consistent documentary",
            "scene_visual_map": _entries([0, 1]),
        }

    gen = _fake_gen(fn, names=("primary",))
    scene_texts = [{"texto": f"scene {i}"} for i in range(6)]
    bible = gen.generate("guion", num_scenes=6, scene_texts=scene_texts)
    assert len(bible.scene_visual_map) == 6
    assert all(str(e.get("visual_concept") or "").strip()
               for e in bible.scene_visual_map)
    stats = gen.last_stats
    assert stats["requested_entries"] == 6
    assert stats["primary_model_entries"] == 2
    assert stats["recovered_or_repaired_entries"] == 4
    assert stats["padded_entries"] == 0


# 14a — a model timeout falls back to the next model for the same batch
def test_visual_bible_timeout_batch_fallback():
    def fn(kwargs):
        if kwargs.get("model") == "primary":
            raise TimeoutError("timed out")
        return {"scene_visual_map": _entries(_batch_from_kwargs(kwargs))}

    gen = _fake_gen(fn)
    entries, stats = gen._generate_missing_concepts(
        "ESCENA_FINAL 0 [0-5s]: a\nESCENA_FINAL 1 [5-10s]: b", [0, 1], 2,
    )
    assert {e["scene"] for e in entries} == {0, 1}
    assert stats["entries"] == 2
    assert stats["batches"] == 1


# 14b — a failed large batch is split into smaller retries
def test_visual_bible_failed_batch_is_split():
    def fn(kwargs):
        batch = _batch_from_kwargs(kwargs)
        if len(batch) > 2:
            raise TimeoutError("too large")
        return {"scene_visual_map": _entries(batch)}

    gen = _fake_gen(fn, names=("primary",))
    entries, stats = gen._generate_missing_concepts("guion", [0, 1, 2, 3], 4)
    assert {e["scene"] for e in entries} == {0, 1, 2, 3}
    assert stats["failed_batches"] >= 1
    assert stats["batches"] >= 2


# 15 — valid entries from a partial batch are retained
def test_visual_bible_partial_batch_success_preserved():
    def fn(kwargs):
        batch = _batch_from_kwargs(kwargs)
        if len(batch) >= 4:
            return {"scene_visual_map": _entries(batch[:2])}
        return {"scene_visual_map": _entries(batch)}

    gen = _fake_gen(fn, names=("primary",))
    entries, stats = gen._generate_missing_concepts("guion", [0, 1, 2, 3], 4)
    assert {e["scene"] for e in entries} == {0, 1, 2, 3}
    assert stats["entries"] == 4


# 16 — one failing batch does not pad later batches
def test_visual_bible_one_failed_batch_does_not_pad_later(monkeypatch):
    monkeypatch.setenv("VISUAL_BIBLE_GAPFILL_CHUNK", "2")

    def fn(kwargs):
        batch = _batch_from_kwargs(kwargs)
        if batch == [2, 3] or batch == [2] or batch == [3]:
            raise TimeoutError("this batch is broken")
        return {"scene_visual_map": _entries(batch)}

    gen = _fake_gen(fn, names=("primary",))
    entries, stats = gen._generate_missing_concepts("guion", [0, 1, 2, 3, 4, 5], 6)
    assert {e["scene"] for e in entries} == {0, 1, 4, 5}
    assert stats["failed_batches"] >= 1


# 17 — production/legacy defaults remain safe when the remediation is unused
def test_production_defaults_unchanged_and_remediation_unused():
    config = SourcingConfig.from_env({})
    assert config.integrated is False
    assert config.post_failover_min_score is None
    assert config.post_failover_max_degradation is None
    # Existing P05 scope remains off.
    from pipeline.media_semantics.sourcing_pipeline import p04_enabled_from_env

    assert p04_enabled_from_env({}) is False
    assert SourcingConfig.from_env({}).failure_cache_max_entries == 512
