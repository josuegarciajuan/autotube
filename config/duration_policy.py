"""Single source of truth for channel video duration.

The panel objective is authoritative::

    VIDEO_AVERAGE_DURATION_MIN ± VIDEO_DURATION_DISCREPANCY_MIN

``PROD_VIDEO_DURATION_MIN`` / ``PROD_VIDEO_DURATION_MAX`` are legacy fields.
They are only used when the objective is missing, and are kept aligned by
:func:`apply_range_to_config` so no consumer can observe a contradictory range.

Consumers must import this module instead of re-implementing the arithmetic:

* ``pipeline/script_generator.py`` — word target from duration
* ``pipeline/video_validator.py`` — pre/post validation range
* ``api/services/channel_analyzer.py`` — context fed to Insight AI
* ``api/routers/insights.py`` — validation of duration recommendations
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


def _get(cfg: Any, key: str, default=None):
    """Read a value from a dict or a module/namespace config."""
    if isinstance(cfg, dict):
        return cfg.get(key, default)
    return getattr(cfg, key, default)


def _as_float(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


@dataclass(frozen=True)
class DurationPolicy:
    """Resolved duration range for one channel."""

    mean_min: float
    discrepancy_min: float
    min_min: float
    max_min: float
    source: str  # "objective" | "legacy" | "default"
    issues: tuple[str, ...] = field(default_factory=tuple)

    @property
    def min_sec(self) -> int:
        return int(self.min_min * 60)

    @property
    def max_sec(self) -> int:
        return int(self.max_min * 60)


def range_to_mean_disc(min_min: float, max_min: float) -> tuple[float, float]:
    """Convert an inclusive range into objective ± discrepancy."""
    lo, hi = sorted((float(min_min), float(max_min)))
    if lo <= 0 or hi < lo:
        raise ValueError("invalid duration range")
    mean = round((lo + hi) / 2.0, 1)
    disc = round((hi - lo) / 2.0, 1)
    return mean, disc


def resolve_duration_range(cfg: Any, slug: str | None = None) -> DurationPolicy:
    """Resolve the effective duration policy for *cfg*.

    Priority:
      1. objective (``VIDEO_AVERAGE_DURATION_MIN`` ± discrepancy)
      2. legacy range (``PROD_VIDEO_DURATION_MIN`` / ``MAX``)
      3. conservative default (15 ± 3 min)
    """
    issues: list[str] = []
    mean = _as_float(_get(cfg, "VIDEO_AVERAGE_DURATION_MIN"))
    disc = _as_float(_get(cfg, "VIDEO_DURATION_DISCREPANCY_MIN"))
    legacy_min = _as_float(_get(cfg, "PROD_VIDEO_DURATION_MIN"))
    legacy_max = _as_float(_get(cfg, "PROD_VIDEO_DURATION_MAX"))

    legacy_valid = bool(
        legacy_min is not None and legacy_max is not None
        and legacy_min > 0 and legacy_max >= legacy_min
    )

    if mean is not None and mean > 0:
        if disc is None:
            if legacy_valid:
                disc = round((legacy_max - legacy_min) / 2.0, 1)
                issues.append(
                    "VIDEO_DURATION_DISCREPANCY_MIN missing — derived from legacy range"
                )
            else:
                disc = 3.0
                issues.append(
                    "VIDEO_DURATION_DISCREPANCY_MIN missing — using 3 min"
                )
        disc = abs(disc)
        source = "objective"
        lo = max(0.5, mean - disc)
        hi = mean + disc
        if legacy_valid:
            legacy_lo, legacy_hi = legacy_min, legacy_max
            if abs(legacy_lo - lo) > 0.51 or abs(legacy_hi - hi) > 0.51:
                issues.append(
                    "legacy PROD_VIDEO_DURATION range "
                    f"{legacy_lo:g}-{legacy_hi:g} does not match objective "
                    f"{lo:g}-{hi:g}"
                )
        return DurationPolicy(
            mean_min=round(mean, 1),
            discrepancy_min=round(disc, 1),
            min_min=round(lo, 1),
            max_min=round(hi, 1),
            source=source,
            issues=tuple(issues),
        )

    if legacy_valid:
        mean, disc = range_to_mean_disc(legacy_min, legacy_max)
        issues.append(
            "objective missing — derived from legacy PROD_VIDEO_DURATION range"
        )
        return DurationPolicy(
            mean_min=mean,
            discrepancy_min=disc,
            min_min=round(legacy_min, 1),
            max_min=round(legacy_max, 1),
            source="legacy",
            issues=tuple(issues),
        )

    issues.append("no duration configured — using default 15 ± 3 min")
    return DurationPolicy(
        mean_min=15.0,
        discrepancy_min=3.0,
        min_min=12.0,
        max_min=18.0,
        source="default",
        issues=tuple(issues),
    )


def validate_duration_config(cfg: Any) -> list[str]:
    """Return human-readable problems with the duration configuration."""
    return list(resolve_duration_range(cfg).issues)


def apply_range_to_config(config: dict, min_min: float, max_min: float) -> dict:
    """Return a copy of *config* with a consistent duration block.

    Writes the authoritative objective and aligns the legacy fields so any
    remaining legacy consumer sees the same range.
    """
    mean, disc = range_to_mean_disc(min_min, max_min)
    updated = dict(config or {})
    updated["VIDEO_AVERAGE_DURATION_MIN"] = mean
    updated["VIDEO_DURATION_DISCREPANCY_MIN"] = disc
    updated["PROD_VIDEO_DURATION_MIN"] = int(round(min_min))
    updated["PROD_VIDEO_DURATION_MAX"] = int(round(max_min))
    # Drop case-variant duplicates so the write is canonical.
    for key in list(updated):
        if key.upper() in {
            "VIDEO_AVERAGE_DURATION_MIN",
            "VIDEO_DURATION_DISCREPANCY_MIN",
            "PROD_VIDEO_DURATION_MIN",
            "PROD_VIDEO_DURATION_MAX",
        } and key not in {
            "VIDEO_AVERAGE_DURATION_MIN",
            "VIDEO_DURATION_DISCREPANCY_MIN",
            "PROD_VIDEO_DURATION_MIN",
            "PROD_VIDEO_DURATION_MAX",
        }:
            updated.pop(key, None)
    return updated


def word_targets(cfg: Any) -> dict[str, int]:
    """Word counts for the min/target/max duration using the real voice rate."""
    from config.voice_timing import words_for_duration

    policy = resolve_duration_range(cfg)
    return {
        "words_min": words_for_duration(cfg, policy.min_min),
        "words_target": words_for_duration(cfg, policy.mean_min),
        "words_max": words_for_duration(cfg, policy.max_min),
        "duration_min": policy.min_min,
        "duration_target": policy.mean_min,
        "duration_max": policy.max_min,
    }


def snapshot(cfg: Any) -> dict:
    """Serialisable view for APIs, logs and Insight AI context."""
    policy = resolve_duration_range(cfg)
    return {
        "mean_min": policy.mean_min,
        "discrepancy_min": policy.discrepancy_min,
        "min_min": policy.min_min,
        "max_min": policy.max_min,
        "source": policy.source,
        "issues": list(policy.issues),
    }
