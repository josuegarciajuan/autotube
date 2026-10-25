"""P04-I — deterministic cinematic motion plan for still images.

The renderer already implements Ken Burns (zoom + pan) in
``video_editor._single_ken_burns_clip``.  This module does not render; it
produces a deterministic, configurable plan that the renderer can consume so
the same scene always gets the same motion and excessive zoom is avoided.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any

from .contracts import SceneSpec
from .coherence import shot_class

# Safe ceiling: further zoom would visibly soften stock/archive stills.
DEFAULT_MAX_ZOOM = 6.5
DEFAULT_MIN_ZOOM = 3.0


@dataclass
class MotionPlan:
    profile: str = "horizontal_in"
    zoom_percent: float = 4.0
    zoom_direction: int = 1
    pan_x: int = 1
    pan_y: int = 0
    duration: float = 0.0
    seed: int = 0
    source: str = "deterministic"
    reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "profile": self.profile,
            "zoom_percent": round(float(self.zoom_percent), 3),
            "zoom_direction": int(self.zoom_direction),
            "pan_x": int(self.pan_x),
            "pan_y": int(self.pan_y),
            "duration": round(float(self.duration), 3),
            "seed": int(self.seed),
            "source": self.source,
            "reasons": list(self.reasons),
        }

    def as_ken_burns(self) -> dict[str, Any]:
        """Shape compatible with ``video_editor`` profile dictionaries."""
        zoom = abs(float(self.zoom_percent))
        if self.zoom_direction < 0:
            zoom = -zoom
        return {
            "name": self.profile,
            "zoom_direction": self.zoom_direction,
            "pan_x": self.pan_x,
            "pan_y": self.pan_y,
            "zoom_percent": zoom,
        }


_SHOT_MOTION = {
    "wide": ("horizontal_out", -1, 1, 0),
    "establishing": ("horizontal_out", -1, 1, 0),
    "medium": ("diagonal_in", 1, 1, 0),
    "close": ("vertical_in", 1, 0, 1),
    "detail": ("vertical_in", 1, 0, 1),
}


def _seed_for(spec: SceneSpec, scene_idx: int | None) -> int:
    material = f"{scene_idx if scene_idx is not None else spec.scene_idx}:{spec.narration_fragment[:240]}:{spec.subject}"
    return int(hashlib.sha1(material.encode("utf-8")).hexdigest()[:8], 16)


def plan_image_motion(
    spec: SceneSpec,
    *,
    scene_idx: int | None = None,
    duration: float | None = None,
    min_zoom: float = DEFAULT_MIN_ZOOM,
    max_zoom: float = DEFAULT_MAX_ZOOM,
    last_profile: str | None = None,
) -> MotionPlan:
    """Return a deterministic, conservative motion plan for one still."""
    seed = _seed_for(spec, scene_idx)
    shot = shot_class(spec.desired_shot)
    reasons: list[str] = []
    name, direction, pan_x, pan_y = _SHOT_MOTION.get(
        shot, ("diagonal_in", 1, 1, 0),
    )
    if shot:
        reasons.append(f"shot:{shot}")
    else:
        reasons.append("default_motion")
    if last_profile and last_profile == name:
        # Avoid two adjacent identical motions.  Deterministic alternation.
        alternatives = [
            ("horizontal_in", 1, 1, 0),
            ("horizontal_out", -1, 1, 0),
            ("vertical_in", 1, 0, 1),
            ("diagonal_out", -1, 1, 1),
        ]
        name, direction, pan_x, pan_y = alternatives[seed % len(alternatives)]
        reasons.append("adjacent_motion_alternated")
    if seed & 1:
        pan_x = -pan_x
    if seed & 2:
        pan_y = -pan_y
    span = max(0.0, float(max_zoom) - float(min_zoom))
    zoom = float(min_zoom) + (seed % 1000) / 1000.0 * span
    zoom = max(float(min_zoom), min(float(max_zoom), zoom))
    try:
        dur = float(duration if duration is not None else spec.duration or 0.0)
    except (TypeError, ValueError):
        dur = 0.0
    return MotionPlan(
        profile=name,
        zoom_percent=zoom,
        zoom_direction=direction,
        pan_x=pan_x,
        pan_y=pan_y,
        duration=max(0.0, dur),
        seed=seed,
        reasons=reasons,
    )
