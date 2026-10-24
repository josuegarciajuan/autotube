"""P02 — visual intelligence tests (fully offline, no model/provider calls).

The fake visual backend maps images by dominant RGB channel and text by a few
concept keywords.  It is deliberately deterministic so the tests exercise the
funnel, cache, aggregation and provenance without network, GPU or model
downloads.
"""

from __future__ import annotations

import json
import math
import time
from pathlib import Path

import pytest
from PIL import Image

from pipeline.media_semantics.config import (
    SemanticConfig,
    VisualBudget,
    VisualConfig,
    VisualWeights,
)
from pipeline.media_semantics.contracts import (
    Candidate,
    MetadataStatus,
    SceneSpec,
    SelectionStatus,
)
from pipeline.media_semantics.normalization import normalize_candidate
from pipeline.media_semantics.provenance import decision_record
from pipeline.media_semantics.replay import ReplayHarness
from pipeline.media_semantics.selector import SemanticMediaSelector
from pipeline.media_semantics.visual_backends import (
    NullVisualEmbeddingBackend,
    build_visual_backend,
)
from pipeline.media_semantics.visual_cache import (
    VisualEmbeddingCache,
    visual_cache_key,
)
from pipeline.media_semantics.visual_description import (
    build_visual_description,
    visual_negative_constraints,
)
from pipeline.media_semantics.visual_frames import (
    FrameSamplingResult,
    sample_semantic_frames,
    sample_timestamps,
)
from pipeline.media_semantics.visual_pipeline import VisualIntelligencePipeline
from pipeline.media_semantics.visual_previews import PreviewFetcher
from pipeline.media_semantics.visual_scoring import (
    INSUFFICIENT_VISUAL_EVIDENCE,
    MUST_AVOID_VIOLATION,
    VISUAL_ACTION_MISMATCH,
    VISUAL_ENVIRONMENT_MISMATCH,
    VISUAL_ERA_MISMATCH,
    VISUAL_SUBJECT_MISMATCH,
    aggregate_frame_similarities,
    compute_combined_score,
    cosine_similarity,
    evaluate_visual_constraints,
)
from pipeline.media_semantics.visual_vlm import (
    NullVLMBackend,
    VisualVerificationResult,
    parse_vlm_payload,
    verify_with_backend,
)


# ── fixtures / fakes ───────────────────────────────────────────────

def _image(path: Path, color: tuple[int, int, int]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (24, 24), color).save(path)
    return path


def _spec(**overrides) -> SceneSpec:
    values = dict(
        scene_idx=0, block_idx=0, narration_fragment="an archaeologist excavates an ancient tomb",
        subject="archaeologist", action="excavating", environment="ancient tomb",
        era="ancient egypt", desired_shot="medium shot", visual_concept="burial site",
        must_include=["stone sarcophagus"], must_avoid=[], duration=6.0,
    )
    values.update(overrides)
    return SceneSpec(**values)


def _raw(asset_id: str = "1", *, provider: str = "pexels", good: bool = True, **extra) -> dict:
    raw = {
        "id": asset_id,
        "url": f"https://cdn.example/{provider}/{asset_id}.mp4",
        "download_url": f"https://cdn.example/{provider}/{asset_id}.mp4",
        "page_url": (
            f"https://www.pexels.com/video/archaeologist-excavating-ancient-tomb-{asset_id}/"
            if good else
            f"https://www.pexels.com/video/modern-city-traffic-cars-{asset_id}/"
        ),
        "type": "video",
        "title": "Archaeologist excavating ancient tomb" if good else "Modern city traffic",
        "tags": ["archaeologist", "excavation", "ancient", "tomb"] if good else ["city", "cars"],
        "duration": 8.0,
        "width": 1920,
        "height": 1080,
        "source": provider,
    }
    raw.update(extra)
    return raw


def _cand(raw: dict, *, provider: str | None = None, **kwargs) -> Candidate:
    raw = dict(raw)
    raw.update(kwargs)
    return normalize_candidate(raw, provider=provider or raw.get("source", "pexels"))


class FakeVisualBackend:
    name = "fake_visual"
    model_version = "fake-v1"

    def __init__(self) -> None:
        self.image_calls = 0
        self.text_calls = 0

    def available(self) -> bool:
        return True

    @staticmethod
    def _text_vec(text: str) -> list[float]:
        t = str(text or "").lower()
        if any(w in t for w in ("tomb", "archaeolog", "desert", "excavat", "sandstone", "sarcophagus")):
            return [1.0, 0.0, 0.0]
        if any(w in t for w in ("woman", "people", "person", "walking")):
            return [0.0, 1.0, 0.0]
        if any(w in t for w in ("forest", "car", "modern building", "city")):
            return [0.0, 0.0, 1.0]
        return [0.577350269, 0.577350269, 0.577350269]

    @staticmethod
    def _image_vec(path: str | Path) -> list[float]:
        with Image.open(path) as img:
            r, g, b = img.convert("RGB").resize((1, 1)).getpixel((0, 0))
        if r >= g and r >= b:
            return [1.0, 0.0, 0.0]
        if g >= r and g >= b:
            return [0.0, 1.0, 0.0]
        return [0.0, 0.0, 1.0]

    def encode_texts(self, texts):
        self.text_calls += 1
        return [self._text_vec(t) for t in texts]

    def encode_images(self, paths):
        self.image_calls += 1
        return [self._image_vec(p) for p in paths]


class FakeVLM:
    name = "fake_vlm"
    model_version = "fake-vlm-v1"

    def __init__(self, result: VisualVerificationResult | None = None, *, raise_exc: Exception | None = None):
        self.result = result or VisualVerificationResult(
            subject_match=0.95, action_match=0.9, environment_match=0.9,
            era_match=0.9, must_include_match=0.9, overall_semantic_match=0.95,
            description_of_actual_visual_content="A red archaeological stone in a tomb.",
            confidence=0.95, decision="accept", status="OK",
        )
        self.raise_exc = raise_exc
        self.calls = 0

    def available(self) -> bool:
        return True

    def verify(self, spec, *, narration, frames, provider_metadata=None, timeout=45.0):
        self.calls += 1
        if self.raise_exc is not None:
            raise self.raise_exc
        return self.result


def _pipeline(tmp_path: Path, *, mode="full_visual", backend=None, vlm=None,
              frame_sampler=None, preview_candidates=3, download_candidates=2,
              vlm_candidates=1, weights=None, threshold=0.0,
              budget_bytes=512 * 1024, cache_enabled=True):
    visual = VisualConfig(
        mode=mode,
        backend="fake",
        model_version="fake-v1",
        cache_enabled=cache_enabled,
        cache_path=str(tmp_path / "visual_cache.json"),
        combined_threshold=threshold,
        budget=VisualBudget(
            preview_candidates=preview_candidates,
            shortlist=preview_candidates,
            download_candidates=download_candidates,
            preview_bytes=budget_bytes,
            frame_count=6,
            vlm_candidates=vlm_candidates,
        ),
        weights=weights or VisualWeights(),
    )
    config = SemanticConfig(mode="semantic", visual=visual)
    selector = SemanticMediaSelector(config=config)
    preview = PreviewFetcher(visual.budget, work_dir=tmp_path / "previews")
    return VisualIntelligencePipeline(
        config=config,
        textual_selector=selector,
        visual_backend=backend or FakeVisualBackend(),
        vlm_backend=vlm or NullVLMBackend(),
        preview_fetcher=preview,
        frame_sampler=frame_sampler,
        cache=VisualEmbeddingCache(path=tmp_path / "visual_cache.json", enabled=cache_enabled),
    )


def _download_assets(tmp_path: Path):
    calls: list[str] = []

    def download(candidate: Candidate) -> dict | None:
        calls.append(candidate.canonical_key)
        # A real file is not required by the injected frame sampler.
        path = tmp_path / f"{candidate.provider_asset_id}.mp4"
        path.write_bytes(b"fake")
        return {"path": str(path), "type": "video", "source": candidate.provider, "width": 1920, "height": 1080}

    return download, calls


# ── 1/2 visual description + must_avoid ───────────────────────────

def test_visual_description_is_derived_from_scene_spec_and_excludes_must_avoid():
    spec = _spec(must_avoid=["modern cars", "logos"])
    text = build_visual_description(spec)
    assert "archaeologist" in text.lower()
    assert "excavat" in text.lower()
    assert "tomb" in text.lower()
    assert "ancient egypt" in text.lower()
    assert "modern" not in text.lower()
    assert "cars" not in text.lower()
    assert "logos" not in text.lower()
    assert "stone sarcophagus" in text.lower()


def test_visual_description_strips_forbidden_words_from_positive_fields():
    spec = _spec(subject="archaeologist with modern car", must_avoid=["modern car"])
    text = build_visual_description(spec).lower()
    assert "modern" not in text
    assert "car" not in text
    assert visual_negative_constraints(spec) == ["modern car"]


# ── 3/4 preview normalization ─────────────────────────────────────

def test_preview_normalization_reads_pexels_image_and_video_pictures():
    raw = _raw("p1", image="https://images.pexels.com/v1/poster.jpg", video_pictures=[
        {"nr": 0, "picture": "https://images.pexels.com/v1/frame0.jpg"},
    ])
    cand = _cand(raw)
    assert cand.thumbnail_url == "https://images.pexels.com/v1/poster.jpg"
    assert cand.preview_url == "https://images.pexels.com/v1/frame0.jpg"
    assert cand.preview_type == "image"
    assert cand.preview_status == "PREVIEW_AVAILABLE"
    assert cand.visual_source == "pexels"
    assert cand.has_preview is True


def test_preview_unavailable_is_explicit_and_not_negative_evidence():
    cand = _cand(_raw("p2", thumbnail_url="", preview_url="", image=""))
    assert cand.preview_status == "PREVIEW_UNAVAILABLE"
    assert cand.has_preview is False
    result = PreviewFetcher(VisualBudget(), work_dir="/tmp/p02-none").fetch(cand)
    assert result.status == "PREVIEW_UNAVAILABLE"
    assert result.error == "no_provider_preview"
    assert result.available is False


# ── 5/6/7 backend + cache ─────────────────────────────────────────

def test_visual_backend_abstraction_and_null_backend():
    null = build_visual_backend(VisualConfig(backend="none"))
    assert isinstance(null, NullVisualEmbeddingBackend)
    assert null.available() is False
    assert null.encode_texts(["tomb"]) is None
    fake = FakeVisualBackend()
    assert fake.available() is True
    assert len(fake.encode_images([])) == 0


def test_unavailable_visual_backend_degrades_to_text_only(tmp_path):
    spec = _spec()
    good = _cand(_raw("1"), thumbnail_url=str(_image(tmp_path / "red.jpg", (255, 0, 0))))
    pipeline = _pipeline(tmp_path, backend=NullVisualEmbeddingBackend(), vlm=NullVLMBackend())
    download, calls = _download_assets(tmp_path)
    decision = pipeline.select_and_download(spec, [good], download)
    assert decision.degradation_level == "TEXT_ONLY"
    assert decision.selected is True
    assert calls
    assert decision.visual_verification_status == "NOT_RUN"


def test_cache_key_includes_backend_and_model_version():
    base = dict(provider="pexels", asset_id="1", representation="abc")
    a = visual_cache_key(**base, backend="clip", model_version="v1")
    b = visual_cache_key(**base, backend="clip", model_version="v2")
    c = visual_cache_key(**base, backend="siglip", model_version="v1")
    assert len({a, b, c}) == 3
    cache = VisualEmbeddingCache(path=None, enabled=True)
    cache.set(a, [1.0, 0.0])
    assert cache.get(a) == [1.0, 0.0]
    assert cache.get(b) is None
    assert cache.stats()["entries"] == 1


# ── 8/9/10/11/12/13/14 similarity / frames / aggregation ─────────

def test_visual_similarity_and_batch_scoring(tmp_path):
    red = _image(tmp_path / "red.jpg", (255, 0, 0))
    blue = _image(tmp_path / "blue.jpg", (0, 0, 255))
    backend = FakeVisualBackend()
    vectors = backend.encode_images([red, blue])
    assert cosine_similarity([1, 0, 0], vectors[0]) == pytest.approx(1.0)
    assert cosine_similarity([1, 0, 0], vectors[1]) == pytest.approx(0.0)


def test_frame_sampling_even_inset_and_very_short_video(tmp_path):
    points = sample_timestamps(10.0, 6)
    assert len(points) == 6
    assert 0.0 < points[0] < points[-1] < 10.0
    assert sample_timestamps(0.3, 6) == [0.15]
    video = tmp_path / "short.mp4"
    video.write_bytes(b"not really a video")
    calls = []

    def extractor(path, tmp, ss="", tag=""):
        calls.append(ss)
        return _image(Path(tmp) / f"f{len(calls)}.jpg", (255, 0, 0))

    result = sample_semantic_frames(video, duration=0.3, max_frames=6, tmp_dir=tmp_path / "frames", extractor=extractor)
    assert result.ok
    assert len(result.frames) == 1
    assert len(calls) == 1


def test_frame_extraction_failure_is_reported(tmp_path):
    video = tmp_path / "broken.mp4"
    video.write_bytes(b"x")
    result = sample_semantic_frames(video, duration=5.0, max_frames=3, tmp_dir=tmp_path / "frames", extractor=lambda *a, **k: None)
    assert not result.ok
    assert result.error == "frame_extraction_failed"
    assert result.failed == 3


def test_static_image_is_single_frame(tmp_path):
    image = _image(tmp_path / "still.jpg", (255, 0, 0))
    result = sample_semantic_frames({"path": str(image), "type": "image"}, max_frames=6)
    assert result.ok and result.is_static_image
    assert result.frames == [str(image)]


def test_multiframe_aggregation_and_one_good_frame_does_not_dominate():
    one_good = aggregate_frame_similarities([1.0, 0.0, 0.0, 0.0, 0.0, 0.0], min_similarity=0.2)
    consistent = aggregate_frame_similarities([0.8, 0.8, 0.8, 0.8, 0.8, 0.8], min_similarity=0.2)
    assert one_good["coverage"] < consistent["coverage"]
    assert one_good["median"] < consistent["median"]
    assert one_good["score"] < consistent["score"]
    assert consistent["consistency"] >= 0.99


# ── 15/16/17 VLM structured output ────────────────────────────────

def test_vlm_structured_result_and_validation():
    payload = {
        "subject_match": 0.9, "action_match": 0.8, "environment_match": 1.0,
        "era_match": 0.7, "must_include_match": 0.6,
        "must_avoid_violation": [], "overall_semantic_match": 0.9,
        "description_of_actual_visual_content": "stone ruins",
        "confidence": 0.9, "decision": "accept", "recommendation": "ok",
    }
    parsed = parse_vlm_payload(payload)
    assert parsed.status == "OK" and parsed.usable
    assert parsed.subject_match == 0.9


def test_malformed_vlm_output_is_never_pass():
    for payload in (
        "not json",
        {"subject_match": 0.9},
        {**{"subject_match": 0.9, "action_match": 0.8, "environment_match": 1.0,
            "overall_semantic_match": 0.9, "description_of_actual_visual_content": "x",
            "confidence": 2.0, "decision": "accept"}},
        {**{"subject_match": 0.9, "action_match": 0.8, "environment_match": 1.0,
            "overall_semantic_match": 0.9, "description_of_actual_visual_content": "x",
            "confidence": 0.9, "decision": "maybe"}},
    ):
        result = parse_vlm_payload(payload)
        assert result.status in ("MALFORMED", "VLM_FAILED")
        assert result.usable is False
        assert result.decision in ("uncertain",)


def test_vlm_timeout_or_error_is_failed_not_pass():
    backend = FakeVLM(raise_exc=TimeoutError("timed out"))
    result = verify_with_backend(backend, _spec(), narration="x", frames=["/tmp/nope.jpg"])
    assert result.status == "VLM_FAILED"
    assert result.usable is False
    assert "timeout" in result.error


# ── 18-22 constraints ────────────────────────────────────────────

def _verify(**overrides) -> VisualVerificationResult:
    base = dict(
        subject_match=0.9, action_match=0.9, environment_match=0.9,
        era_match=0.9, must_include_match=0.9, must_avoid_violation=[],
        overall_semantic_match=0.9, description_of_actual_visual_content="tomb",
        confidence=0.9, decision="accept", status="OK",
    )
    base.update(overrides)
    return VisualVerificationResult(**base)


def test_visual_subject_mismatch_rejects():
    result = evaluate_visual_constraints(
        _spec(), _verify(subject_match=0.1, action_match=0.1, environment_match=1.0,
                         overall_semantic_match=0.3, decision="reject"),
    )
    assert result.rejection_reason == VISUAL_SUBJECT_MISMATCH


def test_visual_action_mismatch_rejects():
    result = evaluate_visual_constraints(
        _spec(), _verify(subject_match=0.9, action_match=0.1, environment_match=0.9,
                         overall_semantic_match=0.4, decision="reject"),
    )
    assert result.rejection_reason == VISUAL_ACTION_MISMATCH


def test_visual_environment_mismatch_rejects():
    result = evaluate_visual_constraints(
        _spec(), _verify(subject_match=0.9, action_match=0.9, environment_match=0.1,
                         overall_semantic_match=0.4, decision="reject"),
    )
    assert result.rejection_reason == VISUAL_ENVIRONMENT_MISMATCH


def test_visual_must_avoid_violation_rejects():
    result = evaluate_visual_constraints(
        _spec(must_avoid=["people"]),
        _verify(must_avoid_violation=["people"], decision="reject"),
    )
    assert result.rejection_reason == MUST_AVOID_VIOLATION
    assert result.negative_penalty == 1.0


def test_visual_era_mismatch_rejects():
    result = evaluate_visual_constraints(
        _spec(era="medieval"), _verify(era_match=0.1, decision="reject"),
    )
    assert result.rejection_reason == VISUAL_ERA_MISMATCH
    assert result.era_penalty >= 0.8


def test_unavailable_visual_evidence_is_not_negative_evidence():
    result = evaluate_visual_constraints(
        _spec(), VisualVerificationResult(status="VLM_FAILED", decision="uncertain"),
    )
    assert result.rejection_reason == ""
    assert result.evidence_unavailable is True
    assert result.negative_penalty == 0.0


# ── 23/24 combined score ─────────────────────────────────────────

def test_combined_score_and_configurable_weights():
    a = compute_combined_score(text_relevance=0.9, preview_relevance=0.8, frame_relevance=0.8, vlm_relevance=0.8)
    b = compute_combined_score(text_relevance=0.2, preview_relevance=0.0, frame_relevance=0.0, vlm_relevance=0.0)
    assert a["combined_score"] > b["combined_score"]
    text_only = VisualWeights(text=1.0, preview=0.0, frame=0.0, vlm=0.0, technical_quality=0.0)
    weighted = compute_combined_score(
        text_relevance=1.0, preview_relevance=0.0, frame_relevance=0.0,
        vlm_relevance=0.0, technical_quality=0.0, weights=text_only,
    )
    assert weighted["combined_score"] == pytest.approx(1.0)
    assert weighted["components"]["text"] == 1.0


# ── 25/26/27 funnel budgets ──────────────────────────────────────

def test_preview_shortlist_and_download_are_bounded(tmp_path):
    spec = _spec()
    candidates = []
    for i in range(6):
        image = _image(tmp_path / f"c{i}.jpg", (255, 0, 0) if i == 0 else (0, 0, 255))
        candidates.append(_cand(_raw(str(i)), thumbnail_url=str(image)))
    downloaded, calls = _download_assets(tmp_path)
    frames = FrameSamplingResult(frames=[str(_image(tmp_path / "frame.jpg", (255, 0, 0)))], timestamps=[1.0], duration=5.0)
    pipeline = _pipeline(
        tmp_path, preview_candidates=3, download_candidates=2, vlm_candidates=1,
        frame_sampler=lambda asset, max_frames: frames,
    )
    decision = pipeline.select_and_download(spec, candidates, downloaded)
    assert decision.visual_cost["preview_candidates"] == 3
    assert len(calls) <= 2
    assert decision.visual_cost["downloads"] <= 2


def test_vlm_budget_only_runs_for_configured_top_n(tmp_path):
    spec = _spec()
    candidates = [
        _cand(_raw("1"), thumbnail_url=str(_image(tmp_path / "a.jpg", (255, 0, 0)))),
        _cand(_raw("2"), thumbnail_url=str(_image(tmp_path / "b.jpg", (255, 0, 0)))),
    ]
    downloaded, _ = _download_assets(tmp_path)
    frames = FrameSamplingResult(frames=[str(_image(tmp_path / "f.jpg", (255, 0, 0)))], timestamps=[1.0], duration=5.0)
    vlm = FakeVLM()
    pipeline = _pipeline(
        tmp_path, vlm=vlm, preview_candidates=2, download_candidates=2,
        vlm_candidates=1, frame_sampler=lambda asset, max_frames: frames,
    )
    decision = pipeline.select_and_download(spec, candidates, downloaded)
    assert vlm.calls == 1
    assert decision.visual_cost["vlm_calls"] == 1


# ── 28/29/30 degradation ─────────────────────────────────────────

def test_full_visual_mode_uses_preview_frames_and_vlm(tmp_path):
    spec = _spec()
    good = _cand(_raw("1"), thumbnail_url=str(_image(tmp_path / "good.jpg", (255, 0, 0))))
    bad = _cand(_raw("2"), thumbnail_url=str(_image(tmp_path / "bad.jpg", (0, 0, 255))))
    downloaded, _ = _download_assets(tmp_path)
    red_frame = str(_image(tmp_path / "red_frame.jpg", (255, 0, 0)))
    blue_frame = str(_image(tmp_path / "blue_frame.jpg", (0, 0, 255)))

    def sampler(asset, max_frames):
        color = red_frame if "1" in str(asset.get("path")) else blue_frame
        return FrameSamplingResult(frames=[color], timestamps=[1.0], duration=5.0)

    vlm = FakeVLM()
    pipeline = _pipeline(tmp_path, vlm=vlm, frame_sampler=sampler)
    decision = pipeline.select_and_download(spec, [bad, good], downloaded)
    assert decision.degradation_level == "FULL_VISUAL"
    assert decision.status == SelectionStatus.SELECTED
    assert decision.winner.provider_asset_id == "1"
    assert decision.visual_cost["previews_available"] == 2
    assert decision.frames_sampled == 1
    assert decision.visual_verification_status == "OK"


def test_embedding_only_mode_skips_frames_and_vlm(tmp_path):
    spec = _spec()
    good = _cand(_raw("1"), thumbnail_url=str(_image(tmp_path / "good.jpg", (255, 0, 0))))
    downloaded, _ = _download_assets(tmp_path)
    vlm = FakeVLM()
    pipeline = _pipeline(tmp_path, mode="embedding_only", vlm=vlm)
    decision = pipeline.select_and_download(spec, [good], downloaded)
    assert decision.degradation_level == "EMBEDDING_ONLY"
    assert decision.frames_sampled == 0
    assert vlm.calls == 0
    assert decision.visual_verification_status == "NOT_RUN"


def test_text_only_mode_preserves_p01_selection(tmp_path):
    spec = _spec()
    good = _cand(_raw("1"))
    downloaded, calls = _download_assets(tmp_path)
    pipeline = _pipeline(tmp_path, mode="text_only")
    decision = pipeline.select_and_download(spec, [good], downloaded)
    assert decision.degradation_level == "TEXT_ONLY"
    assert decision.selected and calls


# ── 31 provenance ────────────────────────────────────────────────

def test_visual_provenance_is_serialisable_and_has_no_blobs(tmp_path):
    spec = _spec()
    good = _cand(_raw("1"), thumbnail_url=str(_image(tmp_path / "good.jpg", (255, 0, 0))))
    downloaded, _ = _download_assets(tmp_path)
    frames = FrameSamplingResult(frames=[str(_image(tmp_path / "frame.jpg", (255, 0, 0)))], timestamps=[1.0], duration=5.0)
    decision = _pipeline(tmp_path, frame_sampler=lambda asset, max_frames: frames).select_and_download(
        spec, [good], downloaded,
    )
    data = decision.to_dict()
    assert data["visual_mode"] == "full_visual"
    assert data["visual_backend"] == "fake_visual"
    assert data["degradation_level"] in ("FULL_VISUAL", "FRAME_EMBEDDING_ONLY")
    assert data["visual_similarity"] is not None
    assert data["frames_sampled"] >= 1
    assert data["visual_cost"]["downloads"] >= 1
    assert "base64" not in json.dumps(data).lower()
    record = decision_record(decision, video_id=1, channel_id=1)
    assert json.loads(record["visual_json"])["visual_backend"] == "fake_visual"
    assert record["degradation_level"]


# ── 32 offline replay ────────────────────────────────────────────

def test_offline_visual_replay_with_local_fixtures(tmp_path):
    spec = _spec()
    thumb = _image(tmp_path / "thumb.jpg", (255, 0, 0))
    frame = _image(tmp_path / "frame.jpg", (255, 0, 0))
    candidate = _cand(_raw("1"), thumbnail_url=str(thumb))
    harness = ReplayHarness()
    harness.add_scene("video:s0", spec, candidates=[candidate])
    results = harness.replay_visual(
        visual_backend=FakeVisualBackend(),
        preview_fetcher=PreviewFetcher(VisualBudget(), work_dir=tmp_path / "previews"),
        frame_sampler=lambda asset, max_frames: FrameSamplingResult(
            frames=[str(frame)], timestamps=[1.0], duration=5.0,
        ),
        vlm_backend=FakeVLM(),
        source_loader=lambda c: {"path": str(tmp_path / "clip.mp4"), "type": "video", "duration": 5.0},
    )
    assert results[0]["status"] == SelectionStatus.SELECTED.value
    assert results[0]["visual"]["visual_backend"] == "fake_visual"
    assert results[0]["detail"]["timing_ms"] == 0.0



# ── additional provider-contract / fallback coverage ─────────────

def test_provider_preview_contracts_without_network():
    from pipeline.providers.pexels import PexelsVideoProvider
    from pipeline.providers.pixabay import PixabayVideoProvider
    from pipeline.providers.mixkit import MixkitVideoProvider
    from pipeline.providers.coverr import CoverrVideoProvider
    from bs4 import BeautifulSoup

    pexels = PexelsVideoProvider._visual_fields({
        "image": "https://images.pexels.com/poster.jpg",
        "video_pictures": [{"picture": "https://images.pexels.com/frame.jpg"}],
    })
    assert pexels["thumbnail_url"].endswith("poster.jpg")
    assert pexels["preview_url"].endswith("frame.jpg")
    pixabay = PixabayVideoProvider._visual_fields({"id": 1})
    assert pixabay["preview_status"] == "PREVIEW_UNAVAILABLE"
    soup = BeautifulSoup(
        '<div class="video-card"><a href="/free-stock-video/x/"><img src="/thumb.jpg"></a></div>',
        "html.parser",
    )
    card = MixkitVideoProvider._parse_video_cards(soup)[0]
    assert card["thumbnail"].endswith("/thumb.jpg")
    coverr_thumb = CoverrVideoProvider._link_thumbnail(
        BeautifulSoup('<a href="/videos/x"><img data-src="/c.jpg"></a>', "html.parser").find("a")
    )
    assert coverr_thumb.endswith("/c.jpg")


def test_frame_sampling_unknown_duration_and_corrupt_frame(tmp_path):
    video = tmp_path / "unknown.mp4"
    video.write_bytes(b"x")
    calls = []

    def extractor(path, tmp, ss="", tag=""):
        calls.append(ss)
        if len(calls) == 1:
            return None
        return _image(Path(tmp) / f"{tag}.jpg", (255, 0, 0))

    result = sample_semantic_frames(
        video, max_frames=3, tmp_dir=tmp_path / "frames",
        extractor=extractor, probe=lambda path: None,
    )
    assert result.ok
    assert result.duration == 5.0
    assert result.error == "duration_unknown_default"
    assert result.failed == 1


def test_pipeline_vlm_rejection_marks_no_good_candidate(tmp_path):
    spec = _spec(must_avoid=["people"])
    candidate = _cand(_raw("1"), thumbnail_url=str(_image(tmp_path / "red.jpg", (255, 0, 0))))
    downloaded, _ = _download_assets(tmp_path)
    frames = FrameSamplingResult(
        frames=[str(_image(tmp_path / "f.jpg", (255, 0, 0)))],
        timestamps=[1.0], duration=5.0,
    )
    vlm = FakeVLM(VisualVerificationResult(
        subject_match=0.9, action_match=0.9, environment_match=0.9,
        era_match=0.9, must_include_match=0.9,
        must_avoid_violation=["people"], overall_semantic_match=0.2,
        description_of_actual_visual_content="A person in a tomb",
        confidence=0.9, decision="reject", status="OK",
    ))
    pipeline = _pipeline(tmp_path, vlm=vlm, frame_sampler=lambda asset, max_frames: frames)
    decision = pipeline.select_and_download(spec, [candidate], downloaded)
    assert decision.status == SelectionStatus.NO_GOOD_CANDIDATE
    assert decision.visual_rejection_reason == MUST_AVOID_VIOLATION
    assert any(r.reason == MUST_AVOID_VIOLATION for r in decision.rejected)


def test_pipeline_malformed_vlm_is_unavailable_not_rejection(tmp_path):
    spec = _spec()
    candidate = _cand(_raw("1"), thumbnail_url=str(_image(tmp_path / "red.jpg", (255, 0, 0))))
    downloaded, _ = _download_assets(tmp_path)
    frames = FrameSamplingResult(
        frames=[str(_image(tmp_path / "f.jpg", (255, 0, 0)))],
        timestamps=[1.0], duration=5.0,
    )
    vlm = FakeVLM(VisualVerificationResult(status="MALFORMED", decision="uncertain"))
    pipeline = _pipeline(tmp_path, vlm=vlm, frame_sampler=lambda asset, max_frames: frames)
    decision = pipeline.select_and_download(spec, [candidate], downloaded)
    assert decision.selected is True
    assert decision.visual_rejection_reason == ""
    assert decision.visual_verification_status == "MALFORMED"


def test_pipeline_download_failure_uses_next_downloaded_candidate(tmp_path):
    spec = _spec()
    good = _cand(_raw("1"), thumbnail_url=str(_image(tmp_path / "a.jpg", (255, 0, 0))))
    second = _cand(_raw("2"), thumbnail_url=str(_image(tmp_path / "b.jpg", (255, 0, 0))))
    calls = []

    def download(candidate):
        calls.append(candidate.provider_asset_id)
        if candidate.provider_asset_id == "1":
            return None
        path = tmp_path / "second.mp4"
        path.write_bytes(b"x")
        return {"path": str(path), "type": "video", "width": 1920, "height": 1080}

    frame = str(_image(tmp_path / "frame.jpg", (255, 0, 0)))
    pipeline = _pipeline(
        tmp_path, download_candidates=2,
        frame_sampler=lambda asset, max_frames: FrameSamplingResult(
            frames=[frame], timestamps=[1.0], duration=5.0,
        ),
    )
    decision = pipeline.select_and_download(spec, [good, second], download)
    assert calls == ["1", "2"]
    assert decision.winner.provider_asset_id == "2"
    assert decision.status in (SelectionStatus.SELECTED, SelectionStatus.FALLBACK_SELECTED)

# ── 33/34/35 legacy/text and no network ──────────────────────────

def test_legacy_default_is_unchanged_and_semantic_textual_works_without_visual():
    cfg = SemanticConfig.from_media_strategy({}, env={})
    assert cfg.mode == "legacy"
    assert cfg.visual.enabled is False
    semantic = SemanticConfig.from_media_strategy({}, env={"MEDIA_SELECTOR": "semantic"})
    assert semantic.visual.mode == "off"
    selector = SemanticMediaSelector(config=semantic)
    decision = selector.rank(
        _spec(), [_cand(_raw("1")), _cand(_raw("2", good=False))],
    )
    assert decision.status == SelectionStatus.SELECTED


def test_visual_preview_fetch_uses_no_network_for_local_fixture(tmp_path, monkeypatch):
    import requests

    def fail(*args, **kwargs):
        raise AssertionError("network must not be used")

    monkeypatch.setattr(requests, "get", fail)
    image = _image(tmp_path / "local.jpg", (255, 0, 0))
    candidate = _cand(_raw("1"), thumbnail_url=str(image))
    result = PreviewFetcher(VisualBudget(), work_dir=tmp_path / "previews").fetch(candidate)
    assert result.available is True
    assert result.path == str(image)


def test_semantic_selector_routes_enabled_visual_mode_without_replacing_rank():
    from pipeline.media_semantics.contracts import SelectionDecision

    visual = VisualConfig(mode="full_visual")
    config = SemanticConfig(mode="semantic", visual=visual)
    sentinel = SelectionDecision(scene_spec=_spec(), status=SelectionStatus.SELECTED)
    sentinel.visual_mode = "full_visual"

    class StubPipeline:
        def __init__(self):
            self.calls = 0

        def select_and_download(self, spec, candidates, download_fn, **kwargs):
            self.calls += 1
            return sentinel

    stub = StubPipeline()
    selector = SemanticMediaSelector(config=config, visual_pipeline=stub)
    result = selector.select_and_download(_spec(), [], lambda c: None)
    assert result is sentinel
    assert stub.calls == 1
    # rank() itself remains the textual P01 path (no visual recursion).
    assert selector.rank(_spec(), [_cand(_raw("1"))]).status == SelectionStatus.SELECTED


def test_production_preview_fetcher_rejects_local_paths_without_replay_opt_in(tmp_path):
    image = _image(tmp_path / "secret.jpg", (255, 0, 0))
    candidate = _cand(_raw("1"), thumbnail_url=str(image))
    # No explicit work_dir/allow_local_paths => local filesystem metadata is
    # not trusted.  It degrades to PREVIEW_UNAVAILABLE, not a file read.
    result = PreviewFetcher(VisualBudget()).fetch(candidate)
    assert result.status == "PREVIEW_UNAVAILABLE"
    assert result.error in ("unsupported_preview_scheme", "not_an_image", "unsafe_or_unsupported_preview_url")


def test_full_visual_without_previews_degrades_to_frame_embedding_only(tmp_path):
    spec = _spec()
    candidate = _cand(_raw("1"))  # no thumbnail/preview fields
    downloaded, _ = _download_assets(tmp_path)
    frame = str(_image(tmp_path / "frame.jpg", (255, 0, 0)))
    pipeline = _pipeline(
        tmp_path,
        frame_sampler=lambda asset, max_frames: FrameSamplingResult(
            frames=[frame], timestamps=[1.0], duration=5.0,
        ),
    )
    decision = pipeline.select_and_download(spec, [candidate], downloaded)
    assert decision.degradation_level == "FRAME_EMBEDDING_ONLY"
    assert decision.selected is True
    assert decision.visual_cost["previews_available"] == 0
    assert decision.frames_sampled == 1


def test_visual_verifier_semantic_hook_is_wired_and_fail_open(tmp_path):
    from pipeline.visual_verifier import (
        _classify_frames_with_model,
        set_semantic_frame_verifier,
    )

    frame = _image(tmp_path / "frame.jpg", (255, 0, 0))
    calls = {}

    def fake_verifier(spec, narration, frames, scene_context):
        calls["spec"] = spec
        calls["narration"] = narration
        calls["frames"] = list(frames)
        return {"overall_semantic_match": 0.9, "decision": "accept"}

    set_semantic_frame_verifier(fake_verifier)
    try:
        result = _classify_frames_with_model([frame], _spec())
        assert result["decision"] == "accept"
        assert calls["frames"] == [frame]
        assert calls["narration"].startswith("an archaeologist")
        set_semantic_frame_verifier(lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
        assert _classify_frames_with_model([frame], _spec()) is None
    finally:
        set_semantic_frame_verifier(None)


def test_video_only_preview_url_is_not_treated_as_a_still():
    cand = _cand(_raw("v1", preview_url="https://cdn.example/preview.mp4", thumbnail_url=""))
    assert cand.preview_type == "video"
    assert cand.has_preview is False
    result = PreviewFetcher(VisualBudget(), work_dir="/tmp/p02-video-preview").fetch(cand)
    assert result.status == "PREVIEW_UNAVAILABLE"
    assert result.error == "no_provider_preview"
