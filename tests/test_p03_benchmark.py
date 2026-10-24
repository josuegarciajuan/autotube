"""P03 — offline tests for the benchmark harness and P02 regressions.

OFFLINE TESTS ONLY.  Live provider/VLM calls live in
``benchmarks/media_semantics/live_canary.py`` and are never collected by pytest.
"""

from __future__ import annotations

import json
import math
import sqlite3
from pathlib import Path

import pytest
from PIL import Image

from benchmarks.media_semantics import corpus as corpus_mod
from benchmarks.media_semantics import scoring as scoring_mod
from benchmarks.media_semantics import report as report_mod
from pipeline.media_semantics.config import VisualConfig
from pipeline.media_semantics.contracts import Candidate
from pipeline.media_semantics.normalization import normalize_candidate
from pipeline.media_semantics.visual_backends import _feature_tensor
from pipeline.media_semantics.visual_previews import (
    PreviewFetcher,
    _detect_image_mime,
    _is_avif,
)
from pipeline.providers.pixabay import PixabayVideoProvider


# ── corpus ────────────────────────────────────────────────────────

def _make_db(path: Path) -> None:
    con = sqlite3.connect(path)
    con.executescript(
        """
        CREATE TABLE scripts (
            id INTEGER PRIMARY KEY, canal TEXT, titulo_selected TEXT,
            bloques_json TEXT
        );
        CREATE TABLE videos (id INTEGER PRIMARY KEY, script_id INTEGER);
        INSERT INTO videos (id, script_id) VALUES (1, 10), (2, 11), (3, 12);
        """
    )
    blocks = [
        [{"texto": "Un arqueólogo descubre una tumba en el desierto de Egipto en 1922. " * 2,
          "escena_descripcion": "Un arqueólogo excavando una tumba antigua en el desierto.",
          "search_query_en": "archaeologist excavating ancient tomb desert",
          "media_tipo": "video", "media_duracion": 8}],
        [{"texto": "Una familia camina por una ciudad moderna al atardecer. " * 2,
          "escena_descripcion": "Una familia caminando por una calle de la ciudad.",
          "search_query_en": "family walking city street sunset",
          "media_tipo": "image", "media_duracion": 5}],
        [{"texto": "Un telescopio observa una galaxia lejana en el espacio profundo. " * 2,
          "escena_descripcion": "Un telescopio apuntando a una galaxia en el espacio.",
          "search_query_en": "telescope observing distant galaxy space",
          "media_tipo": "image", "media_duracion": 6}],
    ]
    for index, (script_id, canal, items) in enumerate(
        [(10, "canal3", blocks[0]), (11, "canal2", blocks[1]), (12, "canal4", blocks[2])]
    ):
        con.execute(
            "INSERT INTO scripts (id, canal, titulo_selected, bloques_json) VALUES (?,?,?,?)",
            (script_id, canal, f"title {index}", json.dumps(items)),
        )
    con.commit()
    con.close()


def test_corpus_is_real_deterministic_and_keeps_human_label_null(tmp_path):
    db = tmp_path / "test.db"
    _make_db(db)
    out = tmp_path / "corpus.json"
    first = corpus_mod.build_corpus(db, out, tmp_path / "intents.json", limit=3, per_category=1, allow_llm=False)
    second = corpus_mod.build_corpus(db, out, tmp_path / "intents.json", limit=3, per_category=1, allow_llm=False)
    assert [e["scene_id"] for e in first["entries"]] == [e["scene_id"] for e in second["entries"]]
    assert all(e["human_label"] is None for e in first["entries"])
    assert all(e["narration_fragment"] for e in first["entries"])
    assert all(e["query_en"] for e in first["entries"])
    # The 1922 narration must produce a historical era + injected must_avoid.
    historical = [e for e in first["entries"] if "1922" in e["narration_fragment"]]
    assert historical
    spec = historical[0]["scene_spec"]
    assert spec["era"] == "1920s"
    assert "smartphone" in spec["must_avoid"]


def test_corpus_entities_include_era_and_category_metadata(tmp_path):
    db = tmp_path / "test.db"
    _make_db(db)
    payload = corpus_mod.build_corpus(db, tmp_path / "c.json", tmp_path / "i.json", limit=3, allow_llm=False)
    diversity = corpus_mod.corpus_diversity(payload)
    assert diversity["scenes"] == 3
    assert diversity["categories"]
    assert diversity["channels"] == {"canal2": 1, "canal3": 1, "canal4": 1}


# ── scoring ───────────────────────────────────────────────────────

class FakeBackend:
    name = "fake"
    model_version = "fake-v1"

    def available(self) -> bool:
        return True

    def encode_texts(self, texts):
        out = []
        for text in texts:
            low = str(text).lower()
            if "red" in low:
                out.append([1.0, 0.0])
            elif "blue" in low:
                out.append([0.0, 1.0])
            else:
                out.append([0.5, 0.5])
        return out

    def encode_images(self, paths):
        out = []
        for path in paths:
            with Image.open(path) as image:
                r, g, b = image.convert("RGB").getpixel((10, 10))
            out.append([1.0, 0.0] if r > b else [0.0, 1.0])
        return out


def _image(path: Path, color) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (32, 32), color).save(path)
    return str(path)


def _candidate(asset_id: str, title: str, tags: list[str], provider: str = "pexels") -> Candidate:
    return normalize_candidate(
        {
            "id": asset_id,
            "url": f"https://cdn.example/{asset_id}.mp4",
            "download_url": f"https://cdn.example/{asset_id}.mp4",
            "page_url": f"https://provider.example/video/{title.replace(' ', '-')}-{asset_id}/",
            "title": title,
            "tags": tags,
            "type": "video",
            "duration": 8,
            "width": 1920,
            "height": 1080,
        },
        provider=provider,
    )


def _entry() -> dict:
    spec = {
        "scene_idx": 0, "block_idx": 0, "origin": "block",
        "narration_fragment": "a red hut in a green field",
        "subject": "red hut", "action": "standing", "environment": "green field",
        "era": "", "desired_shot": "wide shot", "visual_concept": "red hut",
        "must_include": [], "must_avoid": [], "duration": 6.0,
        "media_type": "video", "depiction_mode": "documentary",
        "source": "deterministic", "confidence": 0.6, "extra": {},
    }
    return {
        "scene_id": "s1-b0", "category": "nature", "era": "", "media_type": "video",
        "scene_spec": spec, "query_en": "red hut green field",
    }


def test_three_way_scoring_uses_the_same_pool_and_preview_evidence(tmp_path):
    good = _candidate("1", "red hut green field", ["red", "hut", "field"])
    bad = _candidate("2", "blue ocean waves", ["blue", "ocean"])
    entry = _entry()
    pool = {"scene_id": "s1-b0", "queries": ["red hut"], "candidates": [good.to_dict(), bad.to_dict()]}
    previews = {
        good.canonical_key: {
            "status": "PREVIEW_AVAILABLE",
            "path": _image(tmp_path / "good.jpg", (255, 0, 0)),
            "bytes": 100,
        },
        bad.canonical_key: {
            "status": "PREVIEW_AVAILABLE",
            "path": _image(tmp_path / "bad.jpg", (0, 0, 255)),
            "bytes": 100,
        },
    }
    config = scoring_mod.build_semantic_config(
        {"p01_min_relevance_score": 0.0, "visual_combined_threshold": 0.0}
    )
    decision = scoring_mod.score_scene(
        entry, pool, previews, backend=FakeBackend(), config=config
    )
    assert decision["pool_size"] == 2
    assert decision["legacy"]["candidates_considered"] == 2
    assert decision["semantic_text"]["candidates_considered"] == 2
    assert decision["semantic_visual"]["rows"]
    # The visual arm must pick the red still for a red-hut scene.
    assert decision["semantic_visual"]["winner"]["provider_asset_id"] == "1"
    assert decision["semantic_visual"]["preview_meta"]["previews_used"] == 2


def test_combined_score_renormalizes_over_available_components():
    from pipeline.media_semantics.visual_scoring import compute_combined_score

    full = compute_combined_score(
        text_relevance=0.5, preview_relevance=0.5, technical_quality=0.0,
        available_components={"text": True, "preview": True, "frame": False, "vlm": False, "technical_quality": False},
    )
    assert full["combined_score"] == pytest.approx(0.5)
    legacy = compute_combined_score(
        text_relevance=0.5, preview_relevance=0.5, technical_quality=0.0,
    )
    assert legacy["combined_score"] < full["combined_score"]


def test_preview_priority_candidates_covers_legacy_and_text(tmp_path):
    entry = _entry()
    candidates = [
        _candidate(str(i), f"candidate {i} red hut", ["red", "hut", "candidate", str(i)])
        for i in range(20)
    ]
    pool = {"scene_id": "s1-b0", "queries": ["q"], "candidates": [c.to_dict() for c in candidates]}
    config = scoring_mod.build_semantic_config({})
    priority = scoring_mod.preview_priority_candidates(entry, pool, config, limit=12)
    keys = {c.canonical_key for c in priority}
    assert len(priority) <= 12
    legacy = scoring_mod.legacy_decision(scoring_mod._spec(entry), candidates)
    for key in (legacy.extra or {}).get("legacy_order", [])[:3]:
        assert key in keys


# ── metrics / failure set / calibration ───────────────────────────

def _decision(scene_id: str, arm_scores: dict[str, float], era: str = "") -> dict:
    payload = {"scene_id": scene_id, "category": "test", "era": era, "media_type": "video"}
    for arm, score in arm_scores.items():
        payload[arm] = {
            "winner": _candidate("1", "x", ["x"]).to_dict(),
            "winner_score": score,
            "status": "SELECTED",
            "latency_ms": 10,
            "candidate_rows": [
                {"candidate_key": _candidate("1", "x", ["x"]).canonical_key, "score": score, "provider": "pexels",
                 "subject_match": 0.5, "action_match": 0.5, "environment_match": 0.5,
                 "semantic": 0.5, "lexical": 0.5, "metadata_status": "MATCH"}
            ],
        }
    return payload


def _auto(scene_id: str, arm: str, overall: float, key: str | None = None) -> dict:
    return {
        "scene_id": scene_id,
        "arm": arm,
        "candidate_key": key or _candidate("1", "x", ["x"]).canonical_key,
        "auto_evaluation": {
            "status": "OK", "subject_match": overall, "action_match": overall,
            "environment_match": overall, "era_match": None, "must_avoid": [],
            "overall_relevance": overall, "confidence": 0.8, "reason": "test",
        },
    }


def test_metrics_failure_set_and_calibration_are_explainable():
    decisions = [
        _decision("s1", {"legacy": 0.2, "semantic_text": 0.8, "semantic_visual": 0.9}),
        _decision("s2", {"legacy": 0.8, "semantic_text": 0.2, "semantic_visual": 0.3}),
        _decision("s3", {"legacy": 0.1, "semantic_text": 0.2, "semantic_visual": 0.3}),
        _decision("s4", {"legacy": 0.8, "semantic_text": 0.2, "semantic_visual": 0.9}),
    ]
    auto = []
    for scene, values in {
        "s1": {"legacy": 0.2, "semantic_text": 0.8, "semantic_visual": 0.9},
        "s2": {"legacy": 0.8, "semantic_text": 0.2, "semantic_visual": 0.3},
        "s3": {"legacy": 0.1, "semantic_text": 0.2, "semantic_visual": 0.3},
        "s4": {"legacy": 0.8, "semantic_text": 0.2, "semantic_visual": 0.9},
    }.items():
        for arm, value in values.items():
            auto.append(_auto(scene, arm, value))
    metrics = scoring_mod.aggregate_metrics(decisions, auto, {"requests": 0})
    assert metrics["arms"]["semantic_visual"]["top1_relevance_mean"] > metrics["arms"]["legacy"]["top1_relevance_mean"]
    failures = scoring_mod.failure_set(decisions, auto)
    assert len(failures["LEGACY_BAD_TEXT_GOOD"]) >= 1
    assert len(failures["TEXT_BAD_VISUAL_GOOD"]) >= 1
    assert len(failures["ALL_BAD"]) == 1
    calibration = scoring_mod.propose_calibration(
        decisions, auto, current_p01_threshold=0.26, current_visual_threshold=0.26
    )
    assert calibration["p01_min_relevance_score"]["after"] <= 0.26
    assert calibration["visual_combined_threshold"]["after"] <= 0.26
    assert calibration["notes"]


# ── P02 regression tests discovered during P03 ────────────────────

def test_feature_tensor_accepts_transformers5_output():
    class Output:
        pooler_output = "EMBEDDING"

    assert _feature_tensor(Output()) == "EMBEDDING"
    assert _feature_tensor("TENSOR") == "TENSOR"
    assert _feature_tensor(type("O", (), {"pooler_output": None})()) is not None


def test_pixabay_video_poster_is_available_and_prefers_small_rendition():
    hit = {
        "videos": {
            "large": {"thumbnail": "https://cdn/large.jpg", "width": 3840, "height": 2160},
            "medium": {"thumbnail": "https://cdn/medium.jpg", "width": 2560, "height": 1440},
            "small": {"thumbnail": "https://cdn/small.jpg", "width": 1920, "height": 1080},
            "tiny": {"thumbnail": "https://cdn/tiny.jpg", "width": 1280, "height": 720},
        }
    }
    fields = PixabayVideoProvider._visual_fields(hit)
    assert fields["preview_status"] == "PREVIEW_AVAILABLE"
    assert fields["thumbnail_url"] == "https://cdn/tiny.jpg"
    assert fields["preview_width"] == 1280


def test_preview_fetcher_does_not_advertise_avif_and_rejects_avif():
    assert _is_avif(b"\x00\x00\x00\x1cftypavif\x00\x00\x00\x00")
    assert _detect_image_mime(b"\x00\x00\x00\x1cftypavif") == ""

    captured = {}

    class Response:
        status_code = 200
        headers = {"Content-Type": "image/avif"}

        def iter_content(self, size):
            yield b"\x00\x00\x00\x1cftypavif\x00\x00\x00\x00"

        def close(self):
            pass

    def fake_get(url, **kwargs):
        captured.update(kwargs)
        return Response()

    fetcher = PreviewFetcher(http_get=fake_get, work_dir=None)
    from pipeline.media_semantics.contracts import Candidate

    candidate = Candidate(
        provider="pexels",
        provider_asset_id="1",
        thumbnail_url="https://images.example/a.jpg",
        preview_type="image",
        preview_status="PREVIEW_AVAILABLE",
        visual_source="pexels:image",
    )
    result = fetcher.fetch(candidate)
    assert "avif" not in captured["headers"]["Accept"].lower()
    assert result.available is False
    assert result.error == "avif_unsupported"


def test_review_pack_writes_html_and_null_human_labels(tmp_path):
    entry = _entry()
    decision = _decision("s1-b0", {"legacy": 0.2, "semantic_text": 0.8, "semantic_visual": 0.9})
    decision["legacy"]["winner"] = _candidate("1", "red hut", ["red"]).to_dict()
    decision["semantic_text"]["winner"] = _candidate("1", "red hut", ["red"]).to_dict()
    decision["semantic_visual"]["winner"] = _candidate("1", "red hut", ["red"]).to_dict()
    auto = [_auto("s1-b0", arm, 0.8) for arm in scoring_mod.ARMS]
    corpus = {"entries": [entry]}
    metrics = scoring_mod.aggregate_metrics([decision], auto, {"requests": 0})
    html_path, labels_path = report_mod.write_review_pack(
        corpus, [decision], auto, {}, {}, metrics, tmp_path
    )
    text = html_path.read_text(encoding="utf-8")
    assert "HUMAN LABEL" in text
    assert "AUTO_EVALUATION" in text
    labels = json.loads(labels_path.read_text(encoding="utf-8"))
    assert labels["entries"][0]["human_label"] is None
    assert labels["entries"][0]["human_scores"]["overall_relevance"] is None
