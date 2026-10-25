"""P04 — offline tests for source expansion, fallback and coherence.

No network calls are permitted.  Provider APIs are exercised through fake
sessions; selection is exercised through injected retrieval/download
functions.  The live canary lives in ``benchmarks/media_semantics/p04_canary.py``
and is never collected by pytest.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from PIL import Image
import numpy as np

from pipeline.media_semantics.ai_policy import (
    AIGenerationRequest,
    build_ai_prompt,
    build_ai_request,
    should_attempt_ai,
)
from pipeline.media_semantics.archive_providers import (
    NASAImageLibraryProvider,
    WikimediaCommonsProvider,
)
from pipeline.media_semantics.asset_strategy import build_asset_strategy
from pipeline.media_semantics.attribution import (
    build_attribution_manifest,
    provenance_for_candidate,
    write_attribution_manifest,
)
from pipeline.media_semantics.coherence import SelectionCoherence, shot_class
from pipeline.media_semantics.config import SemanticConfig, VisualConfig
from pipeline.media_semantics.contracts import (
    Candidate,
    SceneSpec,
    SelectionDecision,
    SelectionStatus,
)
from pipeline.media_semantics.dedup import (
    PerceptualDuplicateIndex,
    embedding_near_duplicate,
    image_hash,
    url_fingerprint,
)
from pipeline.media_semantics.image_motion import plan_image_motion
from pipeline.media_semantics.normalization import normalize_candidate
from pipeline.media_semantics.replay import ReplayHarness
from pipeline.media_semantics.selector import SemanticMediaSelector
from pipeline.media_semantics.source_router import (
    AIPolicy,
    AssetType,
    SceneCategory,
    SourceRouter,
)
from pipeline.media_semantics.sourcing_pipeline import (
    SourcedMediaSelector,
    SourcingConfig,
    p04_enabled_from_env,
)


# ── offline guard ─────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """Any real HTTP request in this module fails loudly."""
    import requests

    def _blocked(*args, **kwargs):
        raise AssertionError("network access is forbidden in P04 offline tests")

    monkeypatch.setattr(requests.Session, "request", _blocked)
    monkeypatch.setattr(requests, "get", _blocked)


# ── helpers ───────────────────────────────────────────────────────

def _candidate(
    asset_id: str,
    title: str,
    tags: list[str] | None = None,
    *,
    provider: str = "pexels",
    media_type: str = "video",
    asset_type: str = "",
    **extra,
) -> Candidate:
    raw = {
        "id": asset_id,
        "url": f"https://cdn.example/{provider}/{asset_id}.{'mp4' if media_type == 'video' else 'jpg'}",
        "download_url": f"https://cdn.example/{provider}/{asset_id}.{'mp4' if media_type == 'video' else 'jpg'}",
        "page_url": f"https://provider.example/{provider}/{asset_id}",
        "title": title,
        "tags": tags or [],
        "type": media_type,
        "media_type": media_type,
        "width": 1920,
        "height": 1080,
        "duration": 8 if media_type == "video" else None,
        "source": provider,
        "asset_type": asset_type,
    }
    raw.update(extra)
    return normalize_candidate(raw, provider=provider, media_type=media_type)


def _spec(**overrides) -> SceneSpec:
    values = dict(
        scene_idx=0,
        block_idx=0,
        narration_fragment="an ancient Egyptian tomb is excavated in the desert",
        subject="ancient Egyptian tomb excavation",
        action="",
        environment="desert",
        era="1922",
        desired_shot="wide establishing shot",
        visual_concept="",
        must_include=[],
        must_avoid=[],
        depiction_mode="documentary",
        duration=8.0,
    )
    values.update(overrides)
    return SceneSpec(**values)


def _selector(
    search,
    *,
    download=True,
    ai_generator=None,
    ai_max=0,
    available=None,
) -> SourcedMediaSelector:
    def _download(candidate):
        if not download:
            return None
        return {
            "path": f"/tmp/p04/{candidate.provider_asset_id}.jpg",
            "type": candidate.media_type,
            "source": candidate.provider,
        }

    return SourcedMediaSelector(
        config=SourcingConfig(ai_max_per_video=ai_max, ai_enabled=ai_max > 0),
        provider_search=search,
        download_fn=_download,
        ai_generator=ai_generator,
        available_providers=available,
        available_ai_backends=["fake_ai"] if ai_generator else [],
    )


class _FakeResponse:
    def __init__(self, payload):
        self.status_code = 200
        self.headers = {}
        self._payload = payload
        self.content = json.dumps(payload).encode("utf-8")

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _FakeSession:
    def __init__(self, payload):
        self.payload = payload
        self.headers = {}
        self.calls = []

    def get(self, url, params=None, timeout=None):
        self.calls.append((url, params))
        return _FakeResponse(self.payload)

    def headers_update(self, values):
        self.headers.update(values)


# ── 1-5: router categories & deterministic fallback ───────────────

@pytest.mark.parametrize(
    "overrides,expected",
    [
        ({"subject": "woman walking", "environment": "city street", "era": "", "narration_fragment": "a woman walks down a city street"}, SceneCategory.CITY),
        ({"subject": "wolf", "environment": "forest", "era": "", "narration_fragment": "a wolf hunts in the forest"}, SceneCategory.ANIMALS),
        ({"subject": "server", "environment": "data center", "era": "", "narration_fragment": "an algorithm runs on a server"}, SceneCategory.TECHNOLOGY),
        ({"subject": "brain surgery", "environment": "hospital", "era": "", "narration_fragment": "a surgeon performs brain surgery"}, SceneCategory.MEDICAL),
        ({"subject": "mars rover", "environment": "mars", "era": "", "narration_fragment": "a rover explores the surface of Mars"}, SceneCategory.SPACE),
        ({"subject": "dna", "environment": "laboratory", "era": "", "narration_fragment": "a microscope reveals dna in a laboratory"}, SceneCategory.SCIENCE),
        ({"subject": "map", "environment": "continent", "era": "", "narration_fragment": "a map of the continent and its borders"}, SceneCategory.GEOGRAPHY),
        ({"subject": "grief", "environment": "memory", "era": "", "narration_fragment": "grief becomes an abstract memory of light"}, SceneCategory.ABSTRACT),
        ({"subject": "thing", "environment": "somewhere", "era": "", "narration_fragment": "something happens here"}, SceneCategory.GENERIC),
    ],
)
def test_source_router_categories(overrides, expected):
    overrides.setdefault("action", "")
    spec = _spec(**overrides)
    route = SourceRouter().route(spec)
    assert route.scene_type == expected.value


def test_source_router_is_deterministic_and_has_reasons():
    spec = _spec(subject="ancient Egyptian tomb", environment="desert", era="1922")
    first = SourceRouter().route(spec)
    second = SourceRouter().route(spec)
    assert first.to_dict() == second.to_dict()
    assert first.confidence > 0.5
    assert first.reasons
    assert first.mode == "deterministic"


def test_history_routing_prefers_archives():
    route = SourceRouter().route(_spec())
    assert route.scene_type == SceneCategory.HISTORY.value
    assert route.historical_priority is True
    assert route.fallback_order[0] == AssetType.ARCHIVAL_IMAGE.value
    assert "wikimedia_commons" in route.preferred_providers


def test_space_routing_prefers_nasa_and_scientific_image():
    spec = _spec(subject="mars rover", environment="mars", era="", action="", narration_fragment="a rover on mars")
    route = SourceRouter().route(spec)
    assert route.scene_type == SceneCategory.SPACE.value
    assert route.fallback_order[0] == AssetType.SCIENTIFIC_IMAGE.value
    assert "nasa_image_library" in route.preferred_providers


def test_generic_routing_starts_with_stock_video():
    spec = _spec(subject="thing", environment="somewhere", era="", narration_fragment="something happens")
    route = SourceRouter().route(spec)
    assert route.scene_type == SceneCategory.GENERIC.value
    assert route.fallback_order[0] == AssetType.STOCK_VIDEO.value


# ── 6: provider skip ──────────────────────────────────────────────

def test_provider_skip_is_recorded_when_unavailable():
    def search(provider, query, asset_type, limit):
        return []

    selector = _selector(search, available=["pexels"])
    result = selector.select(_spec())
    skipped = result.routing_budget["providers_skipped"]
    assert skipped.get("wikimedia_commons") == "provider_unavailable"
    assert "pexels" in result.routing_budget["providers_considered"]


# ── 7-8: Wikimedia ────────────────────────────────────────────────

def _wikimedia_payload():
    return {
        "query": {
            "pages": [
                {
                    "pageid": 11,
                    "title": "File:Tomb excavation 1922.jpg",
                    "imageinfo": [{
                        "mime": "image/jpeg",
                        "width": 3000,
                        "height": 2000,
                        "url": "https://upload.wikimedia.org/tomb.jpg",
                        "thumburl": "https://upload.wikimedia.org/tomb_thumb.jpg",
                        "extmetadata": {
                            "ImageDescription": {"value": "<p>An <b>Egyptian</b> tomb excavation</p>"},
                            "Artist": {"value": "<a href='#'>John Doe</a>"},
                            "LicenseShortName": {"value": "Public domain"},
                            "LicenseUrl": {"value": "https://creativecommons.org/publicdomain/mark/1.0/"},
                            "DateTimeOriginal": {"value": "1922-01-01"},
                            "Categories": {"value": "Ancient Egypt|Tombs"},
                        },
                    }],
                },
                {
                    "pageid": 12,
                    "title": "File:Digging.mp4",
                    "imageinfo": [{
                        "mime": "video/mp4",
                        "width": 1280,
                        "height": 720,
                        "duration": 12,
                        "url": "https://upload.wikimedia.org/digging.mp4",
                        "thumburl": "https://upload.wikimedia.org/digging.jpg",
                        "extmetadata": {
                            "LicenseShortName": {"value": "CC BY-SA 4.0"},
                            "LicenseUrl": {"value": "https://creativecommons.org/licenses/by-sa/4.0/"},
                            "Artist": {"value": "Jane Roe"},
                        },
                    }],
                },
                {
                    "pageid": 13,
                    "title": "File:Unknown licence.jpg",
                    "imageinfo": [{
                        "mime": "image/jpeg", "width": 1000, "height": 800,
                        "url": "https://upload.wikimedia.org/unknown.jpg",
                        "thumburl": "",
                        "extmetadata": {},
                    }],
                },
            ]
        }
    }


def test_wikimedia_normalization_and_provenance():
    provider = WikimediaCommonsProvider(session=_FakeSession(_wikimedia_payload()))
    results = provider.search("tomb", media_type="image", limit=5)
    assert results
    image = results[0]
    assert image["asset_type"] == "ARCHIVAL_IMAGE"
    assert image["type"] == "image"
    assert image["description"] == "An Egyptian tomb excavation"
    assert image["creator"] == "John Doe"
    assert image["public_domain"] is True
    assert image["attribution_required"] is False
    assert image["license_url"].endswith("mark/1.0/")
    assert image["date"] == "1922-01-01"
    assert "Ancient Egypt" in image["categories"]
    assert image["page_url"].startswith("https://commons.wikimedia.org/wiki/File:")

    video = provider.search("digging", media_type="video", limit=5)
    assert video and video[0]["type"] == "video"
    assert video[0]["asset_type"] == "ARCHIVAL_VIDEO"
    assert video[0]["public_domain"] is False
    assert video[0]["attribution_required"] is True
    assert video[0]["duration"] == 12

    unknown = results[-1]
    assert unknown["license"] == ""
    assert unknown["public_domain"] is None
    assert unknown["attribution_required"] is None


# ── 9-10: NASA ────────────────────────────────────────────────────

def _nasa_payload():
    return {
        "collection": {
            "items": [{
                "href": "https://images-api.nasa.gov/asset/PIA12345",
                "data": [{
                    "nasa_id": "PIA12345",
                    "title": "Mars Panorama",
                    "description": "Panorama of the Martian surface",
                    "keywords": ["Mars", "rover", "surface"],
                    "date_created": "2021-02-18T00:00:00Z",
                    "media_type": "image",
                    "center": "JPL",
                }],
                "links": [{
                    "href": "https://images-assets.nasa.gov/image/PIA12345/PIA12345~thumb.jpg",
                    "rel": "preview", "render": "image",
                }],
            }]
        }
    }


def test_nasa_normalization_and_routing():
    provider = NASAImageLibraryProvider(session=_FakeSession(_nasa_payload()))
    results = provider.search("mars surface", media_type="image", limit=3)
    assert len(results) == 1
    item = results[0]
    assert item["id"] == "PIA12345"
    assert item["asset_type"] == "SCIENTIFIC_IMAGE"
    assert item["title"] == "Mars Panorama"
    assert item["creator"] == "JPL"
    assert item["public_domain"] is None  # unknown, never assumed
    assert item["license_url"].startswith("https://www.nasa.gov/")
    assert item["thumbnail_url"].endswith("~thumb.jpg")

    route = SourceRouter().route(_spec(subject="mars surface", environment="mars", era="", narration_fragment="mars"))
    assert route.scene_type == SceneCategory.SPACE.value
    assert AssetType.SCIENTIFIC_IMAGE.value in route.preferred_asset_types
    assert "nasa_image_library" in route.preferred_providers


# ── 11-15: asset strategy, image first-class, ladder ─────────────

def test_asset_strategy_contract():
    spec = _spec(subject="Napoleon", environment="snowy pass", era="1800", narration_fragment="Napoleon crosses the Alps")
    route = SourceRouter(generation_allowed=True).route(spec)
    strategy = build_asset_strategy(spec, route)
    assert strategy.historical_priority is True
    assert strategy.ai_policy == AIPolicy.AI_IMAGE_FORBIDDEN.value
    assert strategy.allows(AssetType.ARCHIVAL_IMAGE.value)
    assert not strategy.allows(AssetType.AI_IMAGE.value)
    assert strategy.forbidden_asset_types


def test_fallback_ladder_differs_by_category():
    history = SourceRouter().route(_spec())
    nature = SourceRouter().route(_spec(subject="forest", environment="forest", era="", narration_fragment="a forest"))
    assert history.fallback_order[0] == AssetType.ARCHIVAL_IMAGE.value
    assert nature.fallback_order[0] == AssetType.STOCK_VIDEO.value
    assert history.fallback_order != nature.fallback_order


def test_image_can_beat_a_bad_video():
    spec = _spec(
        subject="red hut", action="standing", environment="green field", era="",
        narration_fragment="a red hut in a green field", desired_shot="wide shot",
    )

    def search(provider, query, asset_type, limit):
        if asset_type == AssetType.STOCK_VIDEO.value:
            return [_candidate("bad-v", "modern city traffic cars", ["city", "cars"])]
        if asset_type == AssetType.STOCK_IMAGE.value:
            return [_candidate(
                "good-i", "red hut green field", ["red", "hut", "field"],
                provider="pixabay_photo", media_type="image",
            )]
        return []

    result = _selector(search).select(spec)
    assert result.decision.status == SelectionStatus.SELECTED
    assert result.decision.asset_type == AssetType.STOCK_IMAGE.value
    assert result.decision.winner is not None
    assert result.decision.winner.provider_asset_id == "good-i"
    assert result.asset["type"] == "image"


def test_no_good_source_moves_to_next_ladder_stage():
    spec = _spec(
        subject="red hut", environment="green field", era="",
        narration_fragment="a red hut in a green field",
    )

    def search(provider, query, asset_type, limit):
        if asset_type == AssetType.STOCK_VIDEO.value:
            return []
        if asset_type == AssetType.STOCK_IMAGE.value:
            return [_candidate(
                "img", "red hut green field", ["red", "hut", "field"],
                provider="pixabay_photo", media_type="image",
            )]
        return []

    result = _selector(search, available=["pexels", "pixabay_photo"]).select(spec)
    assert result.decision.asset_type == AssetType.STOCK_IMAGE.value
    assert result.decision.winner.provider_asset_id == "img"


def test_historical_authentic_archive_preferred_over_modern_video():
    spec = _spec()

    def search(provider, query, asset_type, limit):
        if asset_type == AssetType.ARCHIVAL_IMAGE.value:
            return [_candidate(
                "archive-1", "ancient Egyptian tomb excavation 1922",
                ["ancient", "egypt", "tomb", "excavation", "1922"],
                provider="wikimedia_commons", media_type="image",
                asset_type="ARCHIVAL_IMAGE",
            )]
        if asset_type == AssetType.STOCK_VIDEO.value:
            return [_candidate("modern-1", "modern city skyline", ["city", "modern"])]
        return []

    result = _selector(
        search, available=["wikimedia_commons", "pexels"],
    ).select(spec)
    assert result.decision.asset_type == AssetType.ARCHIVAL_IMAGE.value
    assert result.route.historical_priority is True


# ── 16-20: AI policy / prompt / conceptual ──────────────────────

def test_ai_policy_and_trigger():
    abstract = _spec(subject="grief", environment="memory", era="", narration_fragment="grief as abstract memory")
    route = SourceRouter(generation_allowed=True).route(abstract)
    assert route.ai_policy == AIPolicy.AI_IMAGE_PREFERRED.value
    allowed, reason = should_attempt_ai(route, abstract, no_good_real_candidate=True)
    assert allowed and "NO_GOOD" in reason
    allowed, _ = should_attempt_ai(route, abstract, no_good_real_candidate=False)
    assert not allowed

    historical = _spec(subject="Napoleon", environment="Alps", era="1800", narration_fragment="Napoleon crosses the Alps")
    route = SourceRouter(generation_allowed=True).route(historical)
    assert route.ai_policy == AIPolicy.AI_IMAGE_FORBIDDEN.value
    assert not should_attempt_ai(route, historical, no_good_real_candidate=True)[0]


def test_ai_prompt_from_scene_spec_preserves_must_avoid():
    spec = _spec(
        action="archaeologist uncovering a tomb",
        must_avoid=["smartphone", "modern car"], desired_shot="close detail shot",
    )
    positive, negative = build_ai_prompt(spec)
    assert "ancient egyptian tomb" in positive.lower()
    assert "archaeologist" in positive.lower()
    assert "desert" in positive.lower()
    assert "1922" in positive
    assert "close detail shot" in positive
    assert "smartphone" in negative
    assert "modern car" in negative
    assert "watermark" in negative


def test_ai_fallback_only_after_real_sources_and_is_labelled():
    spec = _spec(subject="abstract grief", environment="memory", era="", narration_fragment="grief as abstract memory")

    def search(provider, query, asset_type, limit):
        return []

    generated = {}

    def ai_gen(request, spec_):
        generated["request"] = request
        return {"path": "/tmp/p04/ai.jpg", "source": "fake_ai", "type": "image"}

    selector = _selector(search, ai_generator=ai_gen, ai_max=1)
    selector.config.ai_backend = "fake_ai"
    result = selector.select(spec)
    assert result.decision.status == SelectionStatus.FALLBACK_SELECTED
    assert result.decision.fallback_type == "AI_IMAGE"
    assert result.decision.asset_type == AssetType.AI_IMAGE.value
    assert result.asset["ai_generated"] is True
    assert result.decision.provenance["generation_backend"] == "fake_ai"
    assert result.decision.provenance["license"] == "AI_GENERATED"
    assert generated["request"].prompt


def test_conceptual_fallback_is_explicitly_labeled():
    spec = _spec(
        subject="grief", environment="memory", era="", narration_fragment="grief becomes an inner light",
        depiction_mode="symbolic",
    )

    def search(provider, query, asset_type, limit):
        if asset_type == AssetType.CONCEPTUAL_STOCK.value:
            return [_candidate(
                "concept-1", "abstract grief memory light",
                ["abstract", "grief", "memory", "light"],
                provider="pixabay_photo", media_type="image",
            )]
        return []

    result = _selector(search, available=["pixabay_photo"]).select(spec)
    assert result.decision.status == SelectionStatus.FALLBACK_SELECTED
    assert result.decision.fallback_type == "CONCEPTUAL"
    assert result.decision.conceptual_used is True
    assert result.decision.literal_match is False
    assert result.decision.asset_type == AssetType.CONCEPTUAL_STOCK.value


def test_generic_fallback_is_labeled():
    spec = _spec(subject="thing", environment="somewhere", era="", narration_fragment="something happens")

    def search(provider, query, asset_type, limit):
        if asset_type == AssetType.STOCK_IMAGE.value and "atmospheric" in str(query):
            return [_candidate("gen-1", "atmospheric documentary landscape", ["landscape"], provider="pixabay_photo", media_type="image")]
        return []

    result = _selector(search, available=["pixabay_photo"]).select(spec)
    assert result.decision.fallback_type == "GENERIC"
    assert result.decision.literal_match is False


# ── 21-24: duplicate detection & recent context ─────────────────

def _image(path: Path, color=(120, 80, 40), size=(128, 128)) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, color).save(path)
    return path


def test_phash_dhash_perceptual_duplicate(tmp_path):
    rng = np.random.default_rng(4)
    original = tmp_path / "a.png"
    Image.fromarray(rng.integers(0, 255, (128, 128, 3), dtype=np.uint8)).save(original)
    resized = tmp_path / "b.png"
    Image.open(original).resize((64, 64)).save(resized)
    # A genuinely different texture must not be flagged.
    rng2 = np.random.default_rng(99)
    different = tmp_path / "c.png"
    Image.fromarray(rng2.integers(0, 255, (128, 128), dtype=np.uint8).repeat(3, axis=-1)).save(different)

    index = PerceptualDuplicateIndex()
    index.add(key="provider:1", hash_value=image_hash(original))
    match = index.check(key="provider:2", hash_value=image_hash(resized))
    assert match.is_duplicate
    assert match.reason in ("perceptual_near_duplicate",)
    assert not index.check(key="provider:3", hash_value=image_hash(different)).is_duplicate


def test_same_asset_under_different_url_is_duplicate():
    index = PerceptualDuplicateIndex()
    url = "https://cdn.example/photo.jpg?token=abc"
    index.add(key="provider:1", urls=[url])
    assert index.check(key="provider:2", urls=["https://cdn.example/photo.jpg?token=zzz"]).is_duplicate
    assert index.check(key="provider:3", urls=["https://cdn.example/other.jpg"]).is_duplicate is False
    assert url_fingerprint("https://cdn.example/a.jpg?x=1") == url_fingerprint("https://cdn.example/a.jpg?x=2")


def test_embedding_near_duplicate():
    assert embedding_near_duplicate([1.0, 0.0, 0.0], [0.99, 0.01, 0.0])
    assert not embedding_near_duplicate([1.0, 0.0, 0.0], [0.0, 1.0, 0.0])
    index = PerceptualDuplicateIndex()
    index.add(key="a", embedding=[1.0, 0.0])
    assert index.check(key="b", embedding=[0.99, 0.01]).is_duplicate


def test_recent_scene_context_penalises_repetition(tmp_path):
    ctx = SelectionCoherence()
    spec = _spec(subject="ancient tomb", desired_shot="wide establishing shot")
    first = _candidate("a", "ancient tomb excavation", ["ancient", "tomb"], media_type="image", provider="pixabay_photo")
    ctx.record(first, spec=spec, asset_type=AssetType.STOCK_IMAGE.value, media_type="image")
    same = _candidate("a2", "ancient tomb", ["ancient", "tomb"], media_type="image", provider="pixabay_photo")
    signals = ctx.evaluate(same, spec=spec, asset_type=AssetType.STOCK_IMAGE.value, media_type="image")
    assert signals.total_penalty > 0
    assert any("subject" in r or "provider" in r or "consecutive" in r for r in signals.reasons)
    different = _candidate("b", "modern city street", ["city"], media_type="video", provider="pexels")
    other_spec = _spec(subject="modern city", desired_shot="medium shot")
    signals2 = ctx.evaluate(different, spec=other_spec, asset_type=AssetType.STOCK_VIDEO.value, media_type="video")
    assert signals2.total_bonus >= 0


def test_repeated_clip_and_provider_penalties():
    ctx = SelectionCoherence()
    spec = _spec(subject="tomb", desired_shot="wide establishing shot")
    c = _candidate("same", "tomb", ["tomb"], media_type="image", provider="pixabay_photo")
    for _ in range(3):
        ctx.record(c, spec=spec, asset_type=AssetType.STOCK_IMAGE.value, media_type="image")
    signals = ctx.evaluate(c, spec=spec, asset_type=AssetType.STOCK_IMAGE.value, media_type="image")
    assert "same_clip" in signals.penalties
    assert signals.total_penalty > 0


# ── 25-27: shot diversity / relevance > diversity / style ───────

def test_shot_diversity_alternates_and_relevance_still_wins():
    ctx = SelectionCoherence()
    wide = _spec(subject="tomb", desired_shot="wide establishing shot")
    first = _candidate("a", "tomb", ["tomb"], media_type="image", provider="pixabay_photo")
    ctx.record(first, spec=wide, asset_type=AssetType.STOCK_IMAGE.value, media_type="image")
    close = _spec(subject="tomb detail", desired_shot="close detail shot")
    second = _candidate("b", "tomb detail", ["tomb", "detail"], media_type="image", provider="pixabay_photo")
    signals = ctx.evaluate(second, spec=close, asset_type=AssetType.STOCK_IMAGE.value, media_type="image")
    assert "shot_diversity" in signals.bonuses
    repeat = ctx.evaluate(first, spec=wide, asset_type=AssetType.STOCK_IMAGE.value, media_type="image")
    assert repeat.total_bonus < 0.10  # diversity is a small nudge, never a veto


def test_style_coherence_penalises_generated_in_documentary():
    ctx = SelectionCoherence()
    spec = _spec(depiction_mode="documentary", era="1922", subject="tomb")
    previous = _candidate("prev", "documentary photograph of tomb", ["documentary", "photograph"], media_type="image", provider="wikimedia_commons")
    ctx.record(previous, spec=spec, asset_type=AssetType.ARCHIVAL_IMAGE.value, media_type="image")
    ai = _candidate("ai", "tomb rendering", ["illustration"], media_type="image", provider="ai_local_sd")
    signals = ctx.evaluate(ai, spec=spec, asset_type=AssetType.AI_IMAGE.value, media_type="image")
    assert "generated_in_documentary" in signals.penalties


# ── 28-30: provenance / UNKNOWN / manifest ──────────────────────

def test_provenance_and_unknown_license():
    known = _candidate(
        "1", "tomb", ["tomb"], provider="wikimedia_commons", media_type="image",
        creator="John Doe", license="CC BY-SA 4.0",
        license_url="https://creativecommons.org/licenses/by-sa/4.0/",
        attribution_required=True, public_domain=False,
    )
    prov = provenance_for_candidate(known)
    assert prov["creator"] == "John Doe"
    assert prov["license"].startswith("CC BY")
    assert prov["public_domain"] is False

    unknown = _candidate("2", "tomb", ["tomb"], provider="pixabay_photo", media_type="image")
    prov = provenance_for_candidate(unknown)
    assert prov["license"] == "UNKNOWN"
    assert prov["public_domain"] is None
    assert prov["attribution_required"] is None


def test_attribution_manifest_json_and_markdown(tmp_path):
    spec = _spec()
    decision = SelectionDecision(
        scene_spec=spec,
        status=SelectionStatus.SELECTED,
        asset_type=AssetType.ARCHIVAL_IMAGE.value,
        scene_category=SceneCategory.HISTORY.value,
        winner=_candidate(
            "1", "tomb", ["tomb"], provider="wikimedia_commons", media_type="image",
            creator="John Doe", license="Public domain", license_url="x",
            public_domain=True, attribution_required=False,
        ),
        winner_score=0.42,
    )
    manifest = build_attribution_manifest([decision], video_id=7, channel="canal3", script_title="Tombs")
    assert manifest["summary"]["scenes"] == 1
    assert manifest["summary"]["public_domain"] == 1
    assert manifest["scenes"][0]["provenance"]["creator"] == "John Doe"
    paths = write_attribution_manifest(manifest, tmp_path)
    assert Path(paths["json"]).exists() and Path(paths["markdown"]).exists()
    loaded = json.loads(Path(paths["json"]).read_text(encoding="utf-8"))
    assert loaded["scenes"][0]["attribution"].startswith("John Doe")
    assert "Attribution manifest" in Path(paths["markdown"]).read_text(encoding="utf-8")


# ── 31-35: replay, offline, non-regression ─────────────────────

def test_offline_replay_still_works_with_p04_imported():
    harness = ReplayHarness()
    spec = _spec()
    candidate = _candidate("1", "ancient tomb excavation", ["ancient", "tomb"], media_type="image", provider="pixabay_photo")
    harness.add_scene("s0", spec, ["ancient tomb"], [candidate])
    results = harness.replay(config=SemanticConfig(mode="semantic", min_relevance_score=0.0))
    assert results and results[0]["same_candidates"] is True
    assert results[0]["semantic"]["status"] in ("SELECTED", "NO_GOOD_CANDIDATE", "RETRIEVAL_FAILED")


def test_no_network_used_by_standard_modules(tmp_path):
    # Reaching this assertion means the autouse network guard was never hit.
    spec = _spec()
    route = SourceRouter().route(spec)
    assert route.scene_type
    asset = _image(tmp_path / "x.jpg")
    assert image_hash(asset)


def test_p04_is_off_by_default_and_legacy_selector_unchanged():
    assert p04_enabled_from_env({}) is False
    assert p04_enabled_from_env({"P04_SOURCE_ROUTER": "0"}) is False
    assert p04_enabled_from_env({"P04_SOURCE_ROUTER": "1"}) is True
    config = SemanticConfig.from_media_strategy({}, env={})
    assert config.mode == "legacy"
    assert config.visual.enabled is False
    assert VisualConfig().mode == "off"


def test_semantic_text_selector_unaffected_by_p04():
    spec = _spec()
    candidates = [
        _candidate("1", "ancient tomb excavation", ["ancient", "tomb"], media_type="image", provider="pixabay_photo"),
        _candidate("2", "modern city cars", ["city", "cars"], media_type="video"),
    ]
    selector = SemanticMediaSelector(config=SemanticConfig(mode="semantic", min_relevance_score=0.0))
    decision = selector.rank(spec, candidates)
    assert decision.status == SelectionStatus.SELECTED
    assert decision.winner.provider_asset_id == "1"


def test_image_motion_is_deterministic_and_clamped():
    spec = _spec(desired_shot="detail shot")
    first = plan_image_motion(spec, scene_idx=3)
    second = plan_image_motion(spec, scene_idx=3)
    assert first.to_dict() == second.to_dict()
    assert 3.0 <= first.zoom_percent <= 6.5
    assert shot_class("close-up detail") == "close"
    assert first.as_ken_burns()["name"] == first.profile


def test_routing_budget_compares_stages():
    spec = _spec(subject="forest", environment="forest", era="", narration_fragment="a forest")

    def search(provider, query, asset_type, limit):
        if asset_type == AssetType.STOCK_VIDEO.value:
            return [_candidate("v", "forest trees", ["forest", "trees"])]
        return []

    result = _selector(search, available=["pexels"]).select(spec)
    budget = result.routing_budget
    assert budget["providers_queried"] == ["pexels"]
    assert budget["requests"] >= 1
    assert budget["stages"] >= 1
    assert "providers_considered" in budget


def test_media_fetcher_p04_wrapper_returns_sourced_asset(monkeypatch):
    """The opt-in MediaFetcher hook records the P04 decision and asset."""
    from types import SimpleNamespace
    from pipeline.media_fetcher import MediaFetcher
    from pipeline.media_semantics.source_router import SourceRoute
    from pipeline.media_semantics.asset_strategy import AssetStrategy
    from pipeline.media_semantics.sourcing_pipeline import SourcingResult

    spec = _spec(subject="forest", environment="forest", era="", narration_fragment="a forest")
    winner = _candidate("w", "forest trees", ["forest"], media_type="image", provider="wikimedia_commons")
    decision = SelectionDecision(
        scene_spec=spec,
        status=SelectionStatus.SELECTED,
        asset_type=AssetType.ARCHIVAL_IMAGE.value,
        scene_category=SceneCategory.NATURE.value,
        winner=winner,
        winner_score=0.55,
        provenance=provenance_for_candidate(winner),
    )
    result = SourcingResult(
        decision=decision,
        route=SourceRoute(scene_type="NATURE"),
        strategy=AssetStrategy(),
        asset={"path": "/tmp/x.jpg", "type": "image", "source": "wikimedia_commons"},
        routing_budget={"providers_queried": ["wikimedia_commons"]},
    )

    class FakeSelector:
        def select(self, spec, scene_idx=None, allow_video=True):
            return result

    mf = object.__new__(MediaFetcher)
    mf._p04_selector = FakeSelector()
    mf._pending_selection_decisions = []
    mf._last_selection_decision = None
    mf._current_scene_idx = 0
    mf._p04_current_scene = {}
    mf._p04_current_target_dur = 5.0
    mf._semantic_config = SemanticConfig()
    mf._scene_context = lambda scene, scene_idx=0: SimpleNamespace()
    scene = {"scene_idx": 0, "scene_spec": spec.to_dict(), "duration": 5}
    asset = mf._fetch_p04_asset(scene, 0, 5, None)
    assert asset is not None
    assert asset["asset_type"] == AssetType.ARCHIVAL_IMAGE.value
    assert asset["selector"] == "p04_source_router"
    assert mf._pending_selection_decisions
