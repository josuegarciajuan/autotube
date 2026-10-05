"""Tests for Fase 4b aggregated visual-verify alerting.

Covers:
  * ``VisualObservation.reject_reason`` + ``CandidateGate`` counters.
  * Per-video accumulator in ``MediaFetcher``.
  * Emitted critical alerts (``visual_verify_over_rejection`` /
    ``visual_verify_scene_no_asset``) and their metadata.
  * Tolerant config kill-switch and fail-open behaviour.

Run:  python3 -m pytest tests/test_visual_verify_alerts.py -q
"""

import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from unittest.mock import patch  # noqa: E402


# ── Helpers ──────────────────────────────────────────────────────────

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


def _make_fetcher(**cfg_overrides):
    """Build a MediaFetcher without running the heavy ``__init__``."""
    from pipeline.media_fetcher import MediaFetcher

    fetcher = object.__new__(MediaFetcher)
    cfg = {
        "VISUAL_VERIFY_ALERT_ENABLED": True,
        "VISUAL_VERIFY_ALERT_REJECT_RATIO": 0.5,
        "VISUAL_VERIFY_ALERT_MIN_CANDIDATES": 5,
        "VISUAL_VERIFY_ALERT_SCENE_ALL_FAILED": True,
        "CANAL_NAME": "canal2",
    }
    cfg.update(cfg_overrides)
    fetcher._config = types.SimpleNamespace(**cfg)
    fetcher._vv_stats = fetcher._new_visual_verify_stats()
    fetcher._vv_rejected_scenes = set()
    fetcher._current_scenes = [{} for _ in range(10)]
    fetcher._current_scene_idx = -1
    return fetcher


class _Recorder:
    def __init__(self):
        self.calls = []

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))

    def alert_types(self):
        return [args[0] for args, _ in self.calls]

    def first(self, alert_type):
        for args, kwargs in self.calls:
            if args and args[0] == alert_type:
                return kwargs
        return None


def _stats(verified, rejected, accepted=None, reasons=None, no_asset=None,
           fallback=None):
    if accepted is None:
        accepted = max(0, verified - rejected)
    return {
        "verified": verified,
        "rejected": rejected,
        "reasons": dict(reasons or {}),
        "accepted": accepted,
        "scenes_with_no_asset": list(no_asset or []),
        "scenes_fallback_image_after_reject": list(fallback or []),
    }


def _emit(fetcher, context=None):
    """Run alert emission capturing obs_alert/obs_event (module-level patch)."""
    alerts = _Recorder()
    events = _Recorder()
    ctx = {"video_id": 123, "channel": 7} if context is None else context
    with patch("pipeline.observability.obs_alert", alerts), \
            patch("pipeline.observability.obs_event", events), \
            patch("pipeline.observability.get_context", lambda: ctx):
        fetcher._emit_visual_verify_alerts()
    return alerts, events


# ── VisualObservation.reject_reason ──────────────────────────────────

def test_verify_asset_populates_reject_reason_logo(tmp_path):
    from pipeline.visual_verifier import verify_asset

    img = _make_image(tmp_path)
    asset = {"path": str(img), "type": "image", "width": 1280}
    obs = verify_asset(asset, mode="enforce", image_analyzer=_logo_analyzer)
    assert obs.rejected is True
    assert obs.reject_reason == "logo"
    assert "logo_overlay" in obs.reason


def test_verify_asset_populates_reject_reason_low_res(tmp_path):
    from pipeline.visual_verifier import verify_asset

    img = _make_image(tmp_path, size=(320, 240))
    asset = {"path": str(img), "type": "image", "width": 320}
    obs = verify_asset(asset, mode="enforce", image_analyzer=_clean_analyzer)
    assert obs.rejected is True
    assert obs.reject_reason == "low_res"


def test_verify_asset_error_is_fail_open_with_reason(tmp_path):
    from pipeline.visual_verifier import verify_asset

    img = _make_image(tmp_path)
    asset = {"path": str(img), "type": "image", "width": 1280}

    def exploding_analyzer(path):
        raise ValueError("bad frame")

    obs = verify_asset(asset, mode="enforce", image_analyzer=exploding_analyzer)
    assert obs.rejected is False  # fail-open keeps the asset
    assert obs.reject_reason == "error"
    assert obs.error


# ── CandidateGate counters ───────────────────────────────────────────

def test_candidate_gate_populates_counters():
    from pipeline.visual_verifier import CandidateGate, VisualObservation

    def fake_verify(asset, scene_idx=0, mode="enforce", **kwargs):
        bad = "bad" in asset["path"]
        return VisualObservation(
            scene_idx=scene_idx,
            path=asset["path"],
            mode=mode,
            rejected=bad,
            reject_reason="logo" if bad else "",
            reason="logo_overlay:top-left" if bad else "",
        )

    gate = CandidateGate(scene_idx=1, mode="enforce", budget=3, verify=fake_verify)
    assert gate.consider({"path": "bad1"}) is False
    assert gate.consider({"path": "good"}) is True
    assert gate.consider({"path": "bad2"}) is False

    assert gate.attempts == 3
    assert gate.rejected_count == 2
    assert gate.accepted_count == 1
    assert gate.rejected_reasons == {"logo": 2}
    assert gate.accepted_path == "good"
    # Budget exhausted → accepted without verification, no counter change.
    assert gate.consider({"path": "bad3"}) is True
    assert gate.attempts == 3
    assert gate.rejected_count == 2


def test_candidate_gate_normalises_legacy_reason():
    from pipeline.visual_verifier import CandidateGate, VisualObservation

    def fake_verify(asset, **kwargs):
        return VisualObservation(
            path=asset["path"], rejected=True,
            reject_reason="", reason="low_resolution:320px",
        )

    gate = CandidateGate(mode="enforce", budget=2, verify=fake_verify)
    assert gate.consider({"path": "x"}) is False
    assert gate.rejected_reasons == {"low_res": 1}


def test_candidate_gate_off_mode_does_not_count():
    from pipeline.visual_verifier import CandidateGate

    gate = CandidateGate(mode="off")
    assert gate.consider({"path": "anything"}) is True
    assert gate.attempts == 0
    assert gate.accepted_count == 0
    assert gate.rejected_count == 0


# ── Per-video accumulator ────────────────────────────────────────────

def test_record_visual_observation_accumulates_stats():
    from pipeline.visual_verifier import VisualObservation

    fetcher = _make_fetcher()
    fetcher._record_visual_observation(VisualObservation(
        scene_idx=3, path="/x/a.jpg", mode="enforce", rejected=True,
        reject_reason="low_res", reason="low_resolution:320px",
    ))
    fetcher._record_visual_observation(VisualObservation(
        scene_idx=4, path="/x/b.jpg", mode="enforce", rejected=False,
    ))

    stats = fetcher._vv_stats
    assert stats["verified"] == 2
    assert stats["rejected"] == 1
    assert stats["accepted"] == 1
    assert stats["reasons"] == {"low_res": 1}
    assert 3 in fetcher._vv_rejected_scenes


def test_track_scene_outcomes_only_after_reject():
    fetcher = _make_fetcher()
    fetcher._vv_rejected_scenes = {5, 6}
    fetcher._track_visual_scene_outcome(5, {"path": None, "type": "placeholder"})
    fetcher._track_visual_scene_outcome(6, {"path": "/x/img.jpg", "type": "image"})
    # No rejection at scene 7 → ignored.
    fetcher._track_visual_scene_outcome(7, {"path": "/x/img2.jpg", "type": "image"})

    assert fetcher._vv_stats["scenes_with_no_asset"] == [5]
    assert fetcher._vv_stats["scenes_fallback_image_after_reject"] == [6]


# ── Alert emission ───────────────────────────────────────────────────

def test_over_rejection_emits_with_metadata():
    fetcher = _make_fetcher()
    fetcher._vv_stats = _stats(10, 6, reasons={"logo": 4, "low_res": 2})

    alerts, events = _emit(fetcher)

    assert "visual_verify_over_rejection" in alerts.alert_types()
    call = alerts.first("visual_verify_over_rejection")
    assert call["severity"] == "critical"
    assert call["entity_type"] == "video"
    assert call["entity_id"] == 123
    assert call["channel_id"] == 7

    md = call["metadata"]
    assert md["video_id"] == 123
    assert md["channel"] == 7
    assert md["scenes"] == 10
    assert md["verified"] == 10
    assert md["rejected"] == 6
    assert md["accepted"] == 4
    assert md["ratio"] == 0.6
    assert md["reasons"] == {"logo": 4, "low_res": 2}
    assert md["scenes_with_no_asset"] == []
    assert md["scenes_fallback_image_after_reject"] == []

    # Detail event always written for study.
    assert "visual_verify_summary" in events.alert_types()


def test_below_threshold_does_not_emit():
    fetcher = _make_fetcher()
    fetcher._vv_stats = _stats(10, 3, reasons={"logo": 3})

    alerts, events = _emit(fetcher)

    assert alerts.alert_types() == []
    # Summary detail still emitted (no alert).
    assert "visual_verify_summary" in events.alert_types()


def test_below_min_candidates_does_not_emit_by_ratio():
    fetcher = _make_fetcher()
    fetcher._vv_stats = _stats(4, 4, reasons={"logo": 4})

    alerts, _events = _emit(fetcher)

    assert "visual_verify_over_rejection" not in alerts.alert_types()


def test_scene_no_asset_emits_when_flag_active():
    fetcher = _make_fetcher()
    fetcher._vv_stats = _stats(10, 6, no_asset=[7])

    alerts, _events = _emit(fetcher)

    assert "visual_verify_scene_no_asset" in alerts.alert_types()
    call = alerts.first("visual_verify_scene_no_asset")
    assert call["severity"] == "critical"
    assert call["metadata"]["scenes_with_no_asset"] == [7]


def test_scene_no_asset_suppressed_when_flag_off():
    fetcher = _make_fetcher(VISUAL_VERIFY_ALERT_SCENE_ALL_FAILED=False)
    fetcher._vv_stats = _stats(10, 6, no_asset=[7])

    alerts, _events = _emit(fetcher)

    assert "visual_verify_scene_no_asset" not in alerts.alert_types()


def test_alerts_disabled_is_noop():
    fetcher = _make_fetcher(VISUAL_VERIFY_ALERT_ENABLED=False)
    fetcher._vv_stats = _stats(10, 10, reasons={"logo": 10}, no_asset=[1])

    alerts, events = _emit(fetcher)

    assert alerts.alert_types() == []
    assert events.calls == []


def test_ratio_threshold_clamped_and_respected():
    fetcher = _make_fetcher(VISUAL_VERIFY_ALERT_REJECT_RATIO=1.0)
    fetcher._vv_stats = _stats(10, 6)  # 0.6 < 1.0

    alerts, _events = _emit(fetcher)

    assert "visual_verify_over_rejection" not in alerts.alert_types()


def test_no_context_falls_back_to_channel_slug_alert_type():
    fetcher = _make_fetcher(CANAL_NAME="canal3")
    fetcher._vv_stats = _stats(10, 10, reasons={"logo": 10})

    alerts, _events = _emit(fetcher, context={})

    call = alerts.first("visual_verify_over_rejection_canal3")
    assert call is not None
    assert call["entity_type"] == "system"
    assert call["entity_id"] is None
    assert call["metadata"]["channel"] == "canal3"


def test_fail_open_when_obs_alert_raises():
    fetcher = _make_fetcher()
    fetcher._vv_stats = _stats(10, 10, reasons={"logo": 10}, no_asset=[2])

    def _boom(*args, **kwargs):
        raise RuntimeError("alert backend down")

    events = _Recorder()
    with patch("pipeline.observability.obs_alert", _boom), \
            patch("pipeline.observability.obs_event", events), \
            patch("pipeline.observability.get_context",
                  lambda: {"video_id": 123, "channel": 7}):
        # Must not raise.
        fetcher._emit_visual_verify_alerts()


def test_config_validator_tolerates_bad_alert_flags():
    from config.config_validator import validate_channel_config

    cfg = {
        "VISUAL_VERIFY_ALERT_ENABLED": "yes",
        "VISUAL_VERIFY_ALERT_REJECT_RATIO": 3.5,
        "VISUAL_VERIFY_ALERT_MIN_CANDIDATES": -4,
        "VISUAL_VERIFY_ALERT_SCENE_ALL_FAILED": 0,
    }
    # validate returns warnings; the config dict is mutated in place.
    warnings = validate_channel_config("canal2", cfg)
    assert cfg["VISUAL_VERIFY_ALERT_ENABLED"] is True
    assert cfg["VISUAL_VERIFY_ALERT_REJECT_RATIO"] == 1.0  # clamped
    assert cfg["VISUAL_VERIFY_ALERT_MIN_CANDIDATES"] == 0
    assert cfg["VISUAL_VERIFY_ALERT_SCENE_ALL_FAILED"] is True
    assert warnings  # at least one tolerance warning
