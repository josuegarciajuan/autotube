"""Unit tests for the central scene pacing planner (ScenePlanner)."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.scene_planner import ScenePlanner


def _cfg(**overrides):
    cfg = {
        "IMAGE_SCENE_DURATION_MIN": 5.0,
        "IMAGE_SCENE_DURATION_MAX": 7.0,   # SOFT
        "VIDEO_SCENE_DURATION_MIN": 4.0,
        "VIDEO_SCENE_DURATION_MAX": 6.0,   # SOFT
        "IMAGE_SCENE_DEFAULT_TARGET": 5.5,
        "VIDEO_SCENE_DEFAULT_TARGET": 5.0,
        "SCRIPT_STRUCTURE": [
            {"id": "gancho", "time_pct": "0-10%", "scene_pacing": {"image_target_sec": 5.0, "video_target_sec": 4.5}},
            {"id": "desarrollo", "time_pct": "10-50%", "scene_pacing": {"image_target_sec": 6.0, "video_target_sec": 5.5}},
            {"id": "consecuencias", "time_pct": "50-90%", "scene_pacing": {"image_target_sec": 6.5, "video_target_sec": 6.0}},
            {"id": "cierre", "time_pct": "90-100%", "scene_pacing": {"image_target_sec": 7.0, "video_target_sec": 6.0}},
        ],
    }
    cfg.update(overrides)
    return cfg


# ── partition: the core bug (6.01s → 3.005s flashes) ──────────────

@pytest.mark.parametrize("dur,expected_count,expected_exception", [
    (4.2, 1, False),    # image <= soft_max → single
    (6.01, 1, False),   # no longer split into 3.005s flashes
    (8.0, 1, True),     # soft exception: 8 within (soft_max, 2*hard_min)
    (10.0, 2, False),   # 5+5
    (12.0, 2, False),   # 6+6
    (18.0, 3, False),   # 6+6+6
])
def test_image_partition_respects_hard_min(dur, expected_count, expected_exception):
    plan = ScenePlanner(_cfg())
    count, parts, exc = plan.partition(dur, "image", "desarrollo")
    assert count == expected_count
    assert exc is expected_exception
    assert len(parts) == count
    assert abs(sum(parts) - dur) < 1e-3
    if count > 1:
        assert all(p >= 5.0 - 1e-6 for p in parts)
    else:
        assert parts[0] == pytest.approx(dur)


def test_video_partition_never_creates_flash_below_hard_min():
    plan = ScenePlanner(_cfg())
    # video hard_min=4, soft_max=6
    assert plan.partition(7.0, "video", "desarrollo") == (1, [7.0], True)
    assert plan.partition(8.0, "video", "desarrollo")[1] == [4.0, 4.0]
    assert plan.partition(6.5, "video", "desarrollo")[2] is True  # stays single


def test_partition_prefers_target_count():
    # 10s image, target 6.0 → parts 6+4 would break hard min(4? no, 4 ok)
    # target 5.0 prefers 5+5; ensure chosen count minimizes |part - target|
    plan = ScenePlanner(_cfg())
    count, parts, _ = plan.partition(12.0, "image", "desarrollo")
    # 12 image: possible n=2 (6) only (n=3 → 4 < hard 5). So n=2.
    assert count == 2
    assert parts == [6.0, 6.0]


def test_soft_exception_preserves_coverage_and_marks_flag():
    plan = ScenePlanner(_cfg())
    _, parts, exc = plan.partition(8.2, "image", "gancho")
    assert exc is True
    assert parts == [8.2]
    assert sum(parts) == pytest.approx(8.2)


# ── limits / target ──────────────────────────────────────────────

def test_limits_read_config():
    plan = ScenePlanner(_cfg())
    assert plan.limits("image") == (5.0, 7.0)
    assert plan.limits("video") == (4.0, 6.0)
    assert plan.limits("IMAGEN") == (5.0, 7.0)  # normalizes Spanish


def test_target_from_phase_and_default():
    plan = ScenePlanner(_cfg())
    assert plan.target("gancho", "image") == 5.0
    assert plan.target("desarrollo", "video") == 5.5
    assert plan.target("cierre", "image") == 7.0
    # unknown phase → media default
    assert plan.target("missing", "image") == 5.5


def test_resolve_phase_by_position_and_tipo():
    plan = ScenePlanner(_cfg())
    # explicit phase_id wins
    assert plan.resolve_phase({"phase_id": "cierre"}, 100) == "cierre"
    # position at 30% → desarrollo (10-50%); at 60% → consecuencias (50-90%)
    assert plan.resolve_phase({"start": 30.0}, 100) == "desarrollo"
    assert plan.resolve_phase({"start": 60.0}, 100) == "consecuencias"
    # tipo fallback
    assert plan.resolve_phase({"start": 0, "tipo": "hook"}, None) == "gancho"
    assert plan.resolve_phase({"start": 0, "tipo": "reflexion"}, None) == "consecuencias"
    assert plan.resolve_phase({"start": 0, "tipo": "cierre"}, None) == "cierre"


# ── plan: merge + split on a synthetic timeline ──────────────────

def test_plan_merges_short_and_splits_long():
    plan = ScenePlanner(_cfg())
    ranges = [
        {"start": 0.0, "end": 4.2, "duration": 4.2, "media_tipo": "imagen", "tipo": "gancho"},
        {"start": 4.2, "end": 14.2, "duration": 10.0, "media_tipo": "imagen", "tipo": "desarrollo"},
        {"start": 14.2, "end": 26.2, "duration": 12.0, "media_tipo": "video", "tipo": "desarrollo"},
    ]
    out = plan.plan(ranges)
    # first 4.2s short scene merges into the 10s → 14.2s, which splits
    # into two image scenes; the 12s video splits into 3 video scenes (4s each).
    # Coverage is exact and contiguous.
    assert out[0]["start"] == 0.0
    assert out[-1]["end"] == pytest.approx(26.2)
    for a, b in zip(out, out[1:]):
        assert a["end"] == pytest.approx(b["start"])
    # no scene below hard minimum for its media type
    for s in out:
        hard_min, _ = plan.limits(s["media_tipo"])
        assert s["duration"] >= hard_min - 1e-6
    # every scene has a phase_id
    assert all(s.get("phase_id") for s in out)
