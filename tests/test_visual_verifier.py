"""Tests for pipeline.visual_verifier — Fase 4b (modo observación, fail-open).

Run:  python3 -m pytest tests/test_visual_verifier.py -q
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from unittest.mock import patch  # noqa: E402


def _make_image(tmp_path, name="asset.png", size=(640, 480), color=(120, 120, 120)):
    from PIL import Image

    path = tmp_path / name
    Image.new("RGB", size, color).save(path)
    return path


def _logo_analyzer(path):
    return {
        "ok": True,
        "width": 1280,
        "height": 720,
        "suspicious_corners": ["top-right"],
        "corners": {},
    }


def _clean_analyzer(path):
    return {
        "ok": True,
        "width": 1280,
        "height": 720,
        "suspicious_corners": [],
        "corners": {},
    }


# ── off ──────────────────────────────────────────────────────────────

def test_off_is_noop():
    from pipeline.visual_verifier import (
        CandidateGate,
        select_verified_asset,
        verify_asset,
    )

    asset = {"path": "/does/not/exist.jpg", "type": "image"}
    assert verify_asset(asset, mode="off") is None
    assert CandidateGate(mode="off").consider(asset) is True
    chosen, obs = select_verified_asset([asset], mode="off")
    assert chosen is asset
    assert obs == []


def test_missing_path_returns_none_even_in_observe():
    from pipeline.visual_verifier import verify_asset

    assert verify_asset({"type": "image"}, mode="observe") is None


# ── observe ──────────────────────────────────────────────────────────

def test_observe_records_without_discarding(tmp_path):
    from pipeline.visual_verifier import verify_asset

    img = _make_image(tmp_path)
    asset = {"path": str(img), "type": "image", "width": 1280}

    obs = verify_asset(
        asset, scene_idx=2, mode="observe", image_analyzer=_logo_analyzer,
    )

    assert obs is not None
    assert obs.scene_idx == 2
    assert obs.frames_checked == 1
    assert obs.logo_suspected is True
    assert obs.logo_corners == ["top-right"]
    assert obs.rejected is False, "observe must never discard"
    assert obs.mode == "observe"


def test_observe_candidate_gate_accepts_and_records(tmp_path):
    from pipeline.visual_verifier import CandidateGate

    img = _make_image(tmp_path)
    seen = []
    gate = CandidateGate(
        mode="observe", budget=3, on_observation=seen.append,
        image_analyzer=_logo_analyzer,
    )
    assert gate.consider({"path": str(img), "type": "image"}) is True
    assert gate.attempts == 1
    assert len(seen) == 1 and seen[0].logo_suspected is True
    assert gate.observations[0].rejected is False


# ── fail-open ────────────────────────────────────────────────────────

def test_frame_extraction_error_is_fail_open(tmp_path):
    from pipeline.visual_verifier import verify_asset

    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"\x00" * 16)  # exists, but ffmpeg fake will raise

    def boom(*args, **kwargs):
        raise RuntimeError("ffmpeg exploded")

    asset = {"path": str(clip), "type": "video"}
    obs = verify_asset(asset, mode="observe", frame_extractor=boom)

    assert obs is not None
    assert obs.frames_checked == 0
    assert obs.rejected is False
    assert obs.error  # recorded, not raised


def test_analyzer_exception_is_fail_open(tmp_path):
    from pipeline.visual_verifier import verify_asset

    img = _make_image(tmp_path)
    asset = {"path": str(img), "type": "image"}

    def exploding_analyzer(path):
        raise ValueError("bad frame")

    obs = verify_asset(asset, mode="enforce", image_analyzer=exploding_analyzer)
    assert obs is not None
    assert obs.rejected is False
    assert obs.error


# ── enforce ──────────────────────────────────────────────────────────

def test_enforce_rejects_confirmed_logo_when_enabled(tmp_path):
    # Logo rejection is opt-in (VISUAL_VERIFY_REJECT_LOGO); the heuristic had a
    # high false-positive rate, so by default it is advisory only.
    from pipeline.visual_verifier import verify_asset

    img = _make_image(tmp_path)
    asset = {"path": str(img), "type": "image", "width": 1280}
    obs = verify_asset(
        asset, mode="enforce", image_analyzer=_logo_analyzer, reject_on_logo=True,
    )
    assert obs.rejected is True
    assert "logo_overlay" in obs.reason


def test_enforce_logo_is_advisory_by_default(tmp_path):
    from pipeline.visual_verifier import verify_asset

    img = _make_image(tmp_path)
    asset = {"path": str(img), "type": "image", "width": 1280}
    obs = verify_asset(asset, mode="enforce", image_analyzer=_logo_analyzer)
    assert obs.logo_suspected is True
    assert obs.rejected is False
    assert "advisory" in obs.reason


def test_enforce_rejects_clearly_low_resolution(tmp_path):
    from pipeline.visual_verifier import verify_asset

    img = _make_image(tmp_path, size=(320, 240))
    asset = {"path": str(img), "type": "image", "width": 320}
    obs = verify_asset(asset, mode="enforce", image_analyzer=_clean_analyzer)
    assert obs.rejected is True
    assert "low_resolution" in obs.reason


def test_enforce_accepts_clean_candidate(tmp_path):
    from pipeline.visual_verifier import verify_asset

    img = _make_image(tmp_path)
    asset = {"path": str(img), "type": "image", "width": 1280}
    obs = verify_asset(asset, mode="enforce", image_analyzer=_clean_analyzer)
    assert obs.rejected is False


def test_candidate_gate_discards_within_budget():
    from pipeline.visual_verifier import CandidateGate, VisualObservation

    def fake_verify(asset, scene_idx=0, mode="enforce", **kwargs):
        bad = "bad" in asset["path"]
        return VisualObservation(
            scene_idx=scene_idx,
            path=asset["path"],
            mode=mode,
            logo_suspected=bad,
            logo_corners=["top-left"] if bad else [],
            rejected=bad,
            reason="logo_overlay:top-left" if bad else "",
        )

    gate = CandidateGate(scene_idx=1, mode="enforce", budget=3, verify=fake_verify)
    assert gate.consider({"path": "bad1"}) is False
    assert gate.consider({"path": "bad2"}) is False
    assert gate.consider({"path": "good"}) is True
    assert gate.attempts == 3
    # Budget exhausted → the next candidate is accepted without verification.
    assert gate.consider({"path": "bad3"}) is True
    assert gate.attempts == 3
    assert len(gate.observations) == 3


def test_select_verified_asset_picks_next_within_budget():
    from pipeline.visual_verifier import VisualObservation, select_verified_asset

    def fake_verify(asset, **kwargs):
        bad = "bad" in asset["path"]
        return VisualObservation(
            path=asset["path"], rejected=bad,
            logo_suspected=bad, reason="logo_overlay:x" if bad else "",
        )

    candidates = [{"path": "bad1"}, {"path": "bad2"}, {"path": "good"}]
    chosen, obs = select_verified_asset(
        candidates, mode="enforce", budget=3, verify=fake_verify,
    )
    assert chosen is candidates[2]
    assert len(obs) == 3


# ── degrade without numpy / ffmpeg ───────────────────────────────────

def test_degrades_without_numpy(tmp_path, monkeypatch):
    import pipeline.visual_verifier as vv

    img = _make_image(tmp_path)
    monkeypatch.setitem(sys.modules, "numpy", None)  # simulate absence
    assert vv.measure_sharpness(img) is None
    # The rest of the observation still works (logo heuristic is PIL-only).
    obs = vv.verify_asset(
        {"path": str(img), "type": "image"}, mode="observe",
        image_analyzer=_logo_analyzer,
    )
    assert obs is not None and obs.rejected is False


def test_degrades_without_ffmpeg(tmp_path, monkeypatch):
    import pipeline.visual_verifier as vv

    monkeypatch.setattr(vv.shutil, "which", lambda name: None)
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"\x00" * 16)

    assert vv.extract_video_frame(clip, tmp_path) is None
    obs = vv.verify_asset({"path": str(clip), "type": "video"}, mode="observe")
    assert obs is not None
    assert obs.frames_checked == 0
    assert obs.rejected is False


# ── extension point ──────────────────────────────────────────────────

def test_model_hook_is_explicit_noop():
    from pipeline.visual_verifier import _classify_frames_with_model

    assert _classify_frames_with_model([], None) is None


# ── shared logic is imported by the CLI script (no duplication) ──────

def test_cli_script_reuses_shared_analyzer():
    import scripts.check_asset_logos as cli
    import pipeline.visual_verifier as vv

    assert cli.analyze_image is vv.analyze_image
    assert cli.extract_video_frame is vv.extract_video_frame
    assert cli.CORNERS == vv.CORNERS
