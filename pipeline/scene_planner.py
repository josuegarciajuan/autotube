"""Central scene pacing planner.

Single source of truth for how long each scene stays on screen.

It decouples duration decisions from both media fetching and prompt building,
so the same policy drives:
  - the initial timeline (``video_editor``),
  - the video→image fallback reconciliation (``media_fetcher``),
  - any caller that needs a duration/number-of-scenes decision.

Semantics (matching ``config/defaults.py``):
  - ``*_MIN`` is a HARD limit: a scene may never fall below it.
  - ``*_MAX`` is a SOFT limit: a scene may exceed it ONLY when splitting
    would produce a sub-scene below ``*_MIN`` (a "soft exception", reported
    in the ``soft_exception`` flag and logged by the caller).
  - a per-phase target is preferred over the generic media default.

This module is deliberately provider- and TTS-agnostic: callers keep their
own word timestamps, media types, providers and dedup.
"""

from __future__ import annotations

import math
import re
from typing import Any, Iterable


def _cget(cfg: Any, key: str, default: Any = None) -> Any:
    """Read a config value from a dict or a SimpleNamespace-like object."""
    if isinstance(cfg, dict):
        return cfg.get(key, default)
    return getattr(cfg, key, default)


def _parse_time_pct(value: Any) -> tuple[float, float] | None:
    """Parse ``"10-20%"`` into (start, end) in 0-100. None when malformed."""
    if not isinstance(value, str):
        return None
    m = re.match(r"^\s*(\d+(?:\.\d+)?)\s*-\s*(\d+(?:\.\d+)?)\s*%?\s*$", value)
    if not m:
        return None
    start, end = float(m.group(1)), float(m.group(2))
    if start < 0 or end > 100 or start >= end:
        return None
    return start, end


_TIPO_TO_PHASE = {
    "hook": "gancho",
    "desarrollo": "desarrollo",
    "climax": "climax",
    "reflexion": "consecuencias",
    "cierre": "cierre",
}


class ScenePlanner:
    """Computes per-scene durations respecting hard/soft limits and phase targets."""

    def __init__(self, cfg: Any):
        self._cfg = cfg

    # ── Structure / phase helpers ─────────────────────────────
    def structure(self) -> list[dict]:
        return list(_cget(self._cfg, "SCRIPT_STRUCTURE", []) or [])

    def phase_ids(self) -> list[str]:
        return [p.get("id") for p in self.structure() if p.get("id")]

    @staticmethod
    def _normalize_media(media_type: str) -> str:
        mtype = str(media_type or "imagen").lower()
        return "video" if mtype in ("video", "clip") else "image"

    def resolve_phase(
        self,
        scene: dict,
        total_duration: float | None = None,
        structure: Iterable[dict] | None = None,
    ) -> str:
        """Resolve the narrative phase for a scene.

        Priority: explicit ``phase_id`` on the scene → position in runtime →
        block ``tipo`` mapping → first phase id → ``"default"``.
        """
        structure = list(structure) if structure is not None else self.structure()

        pid = scene.get("phase_id")
        if pid and any(p.get("id") == pid for p in structure):
            return str(pid)

        if total_duration:
            pos = float(scene.get("start", 0) or 0)
            pct = (pos / total_duration) * 100.0
            for p in structure:
                span = _parse_time_pct(p.get("time_pct"))
                if span and span[0] <= pct < span[1]:
                    return str(p.get("id"))

        tipo = str(scene.get("tipo", "")).lower()
        mapped = _TIPO_TO_PHASE.get(tipo)
        if mapped and any(p.get("id") == mapped for p in structure):
            return mapped

        for p in structure:
            if p.get("id"):
                return str(p["id"])
        return "default"

    # ── Limits ────────────────────────────────────────────────
    def limits(self, media_type: str) -> tuple[float, float]:
        """Return (hard_min, soft_max) for a media type."""
        mtype = self._normalize_media(media_type)
        prefix = "VIDEO" if mtype == "video" else "IMAGE"
        default_min = 4.0 if mtype == "video" else 5.0
        default_max = 6.0 if mtype == "video" else 7.0
        hard_min = float(_cget(self._cfg, f"{prefix}_SCENE_DURATION_MIN", default_min))
        soft_max = float(_cget(self._cfg, f"{prefix}_SCENE_DURATION_MAX", default_max))
        return hard_min, soft_max

    def target(self, phase_id: str | None, media_type: str) -> float:
        """Preferred average duration for a phase+media (fallback to default)."""
        mtype = self._normalize_media(media_type)
        default_key = "VIDEO_SCENE_DEFAULT_TARGET" if mtype == "video" else "IMAGE_SCENE_DEFAULT_TARGET"
        hard_min, soft_max = self.limits(mtype)
        default = float(
            _cget(self._cfg, default_key, (hard_min + soft_max) / 2.0)
        )
        for p in self.structure():
            if p.get("id") == phase_id:
                pacing = p.get("scene_pacing") or {}
                key = "video_target_sec" if mtype == "video" else "image_target_sec"
                val = pacing.get(key)
                if val is not None:
                    try:
                        return float(val)
                    except (TypeError, ValueError):
                        break
                break
        return default

    # ── Core partition decision ───────────────────────────────
    def partition(
        self,
        duration: float,
        media_type: str,
        phase_id: str | None = None,
    ) -> tuple[int, list[float], bool]:
        """Decide how many scenes a narration region should be split into.

        Returns (count, durations, soft_exception).

        - If ``duration <= soft_max`` → one scene.
        - Else find a count ``n`` such that every part is within
          ``[hard_min, soft_max]``, preferring the ``n`` whose parts are
          closest to the phase target.
        - If no such ``n`` exists (e.g. ``soft_max < duration < 2*hard_min``)
          → keep ONE scene and set ``soft_exception=True`` (never create a
          flash below ``hard_min``).

        Durations always sum exactly to ``duration``.
        """
        mtype = self._normalize_media(media_type)
        hard_min, soft_max = self.limits(mtype)
        duration = float(duration)
        if duration <= soft_max + 1e-9:
            return 1, [duration], False

        target = self.target(phase_id, mtype)

        n_min = max(2, math.ceil(duration / soft_max - 1e-9))
        n_max = math.floor(duration / hard_min + 1e-9)
        best_n: int | None = None
        best_dist = float("inf")
        for n in range(n_min, n_max + 1):
            part = duration / n
            dist = abs(part - target)
            if dist < best_dist - 1e-9:
                best_dist = dist
                best_n = n

        if best_n is None:
            # No valid split respecting the hard minimum → soft exception.
            return 1, [duration], True

        parts = [round(duration / best_n, 4)] * best_n
        parts[-1] = round(parts[-1] + (duration - sum(parts)), 4)
        return best_n, parts, False

    # ── Whole-timeline plan (merge + split, proportional) ─────
    def plan(
        self,
        block_ranges: list[dict],
        structure: Iterable[dict] | None = None,
    ) -> list[dict]:
        """Merge short scenes and split long ones, assigning phase/target/exception.

        Proportional split is used when no word timestamps are available;
        callers with TTS timestamps should drive semantic cuts from the
        ``partition`` count instead (see ``video_editor``).
        """
        structure = list(structure) if structure is not None else self.structure()
        ranges = [dict(r) for r in block_ranges]
        total = sum(float(r.get("duration", 0) or 0) for r in ranges) or 1.0

        for r in ranges:
            r["phase_id"] = self.resolve_phase(r, total, structure)
            mtype = r.get("media_tipo", "imagen")
            hard_min, soft_max = self.limits(mtype)
            r["_hard_min"] = hard_min
            r["_soft_max"] = soft_max
            r["target_dur"] = self.target(r["phase_id"], mtype)

        # Phase A: merge short scenes forward (repeat until stable).
        changed = True
        safety = 0
        while changed and safety < len(ranges) + 3:
            changed = False
            safety += 1
            out: list[dict] = []
            i = 0
            while i < len(ranges):
                cur = ranges[i]
                if cur["duration"] < cur["_hard_min"] and i + 1 < len(ranges):
                    nxt = ranges[i + 1]
                    cur["end"] = nxt["end"]
                    cur["duration"] = cur["end"] - cur["start"]
                    cur["texto"] = f"{cur.get('texto','')} {nxt.get('texto','')}".strip()
                    cur["search_query_en"] = ", ".join(
                        v for v in (cur.get("search_query_en", ""), nxt.get("search_query_en", "")) if v
                    )
                    i += 2
                    changed = True
                else:
                    i += 1
                out.append(cur)
            ranges = out

        # Backward-merge the last scene if it is still short.
        if len(ranges) >= 2 and ranges[-1]["duration"] < ranges[-1]["_hard_min"]:
            prev, last = ranges[-2], ranges[-1]
            prev["end"] = last["end"]
            prev["duration"] = prev["end"] - prev["start"]
            prev["texto"] = f"{prev.get('texto','')} {last.get('texto','')}".strip()
            prev["search_query_en"] = ", ".join(
                v for v in (prev.get("search_query_en", ""), last.get("search_query_en", "")) if v
            )
            ranges.pop()

        # Phase B: split long scenes proportionally (no timestamps).
        final: list[dict] = []
        for r in ranges:
            if r["duration"] <= r["_soft_max"] + 1e-9:
                r["is_subscene"] = False
                r["soft_exception"] = False
                final.append(r)
                continue
            count, parts, soft_exception = self.partition(
                r["duration"], r.get("media_tipo", "imagen"), r["phase_id"]
            )
            if count == 1:
                r["soft_exception"] = soft_exception
                r["is_subscene"] = False
                final.append(r)
                continue
            for j, part in enumerate(parts):
                sub = dict(r)
                sub["start"] = r["start"] + sum(parts[:j])
                sub["end"] = sub["start"] + part
                sub["duration"] = part
                sub["is_subscene"] = True
                sub["soft_exception"] = False
                sub["media_request_id"] = f"{r.get('asset_idx','scene')}:{j}"
                final.append(sub)

        for r in final:
            r.pop("_hard_min", None)
            r.pop("_soft_max", None)
        return final
