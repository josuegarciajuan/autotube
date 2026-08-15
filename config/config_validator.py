"""Config validation — prevents silent visual regressions in video rendering.

Runs on API startup (``api/main.py``) and pre-render (``VideoEditor.__init__``).
Validates per-channel visual parameters against safe ranges.  Values outside
range are forced to safe defaults with a WARNING log, never silently accepted.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Tuple

logger = logging.getLogger("config_validator")

# ── Safe range definitions ──────────────────────────────────────
# (param_name, min_val, max_val, default_if_out_of_range, description)

_RANGE_RULES: List[Tuple[str, float, float, float, str]] = [
    ("KEN_BURNS_ZOOM_MIN",   3.0,  15.0,  5.0,  "Ken Burns minimum zoom %"),
    ("KEN_BURNS_ZOOM_MAX",   3.0,  18.0, 10.0,  "Ken Burns maximum zoom %"),
    ("VIGNETTE_RADIUS_FACTOR", 0.55, 0.95, 0.72, "Vignette radius factor"),
    ("VIGNETTE_INTENSITY",   5.0,  50.0, 15.0,  "Vignette intensity"),
    ("FILM_GRAIN_OPACITY",   0.0,  20.0,  5.0,  "Film grain opacity"),
    ("MAX_CLIP_EXTEND_SEC", 10.0,  60.0, 25.0,  "Max clip extend seconds"),
    ("SCENE_DURATION_MIN",   2.0,  10.0,  5.0,  "Minimum scene duration (s)"),
    ("SCENE_DURATION_MAX",   5.0,  30.0, 20.0,  "Maximum scene duration (s)"),
    ("IMAGE_SCENE_DURATION_MIN", 1.0, 7.0, 4.0, "Image scene minimum duration (s)"),
    ("IMAGE_SCENE_DURATION_MAX", 4.0, 7.0, 7.0, "Image scene maximum duration (s)"),
    ("VIDEO_SCENE_DURATION_MIN", 1.0, 10.0, 6.0, "Video scene minimum duration (s)"),
    ("VIDEO_SCENE_DURATION_MAX", 6.0, 10.0, 10.0, "Video scene maximum duration (s)"),
    ("SCENE_SYNC_TOLERANCE_SEC", 0.01, 1.0, 0.15, "Scene/audio sync tolerance (s)"),
    ("BACKGROUND_MUSIC_VOLUME", -35.0, -5.0, -18.0, "Background music volume (dB)"),
    ("BACKGROUND_MUSIC_DUCK_VOLUME", -40.0, -10.0, -28.0, "Ducked music volume (dB)"),
]

_MEDIA_STRATEGY_RULES: List[Tuple[str, float, float, float, str]] = [
    ("ken_burns_zoom_min",     3.0,  15.0,  5.0,  "media_strategy ken_burns_zoom_min"),
    ("ken_burns_zoom_max",     3.0,  18.0, 10.0,  "media_strategy ken_burns_zoom_max"),
    ("crossfade_min",          0.1,   1.0,  0.3,  "media_strategy crossfade_min"),
    ("crossfade_max",          0.3,   2.0,  0.7,  "media_strategy crossfade_max"),
    ("target_video_pct",      10.0,  90.0, 50.0,  "media_strategy target_video_pct"),
    ("max_video_blocks_pct",  10.0,  90.0, 50.0,  "media_strategy max_video_blocks_pct"),
    ("max_placeholder_pct",    0.0,  50.0,  0.0,  "media_strategy max_placeholder_pct"),
]

# Minimum sum of RGB channels for COLOR_PALETTE.secondary
# to prevent fully-opaque black vignette overlay.
_MIN_SECONDARY_RGB_SUM = 30


def validate_channel_config(slug: str, config: Dict[str, Any]) -> List[str]:
    """Validate visual parameters for one channel config dict.

    Args:
        slug: Channel slug for log messages (e.g. "canal4").
        config: Dict or SimpleNamespace of channel configuration.

    Returns:
        List of warning messages.  Empty list means all values are safe.
    """
    # Convert SimpleNamespace → dict if needed
    if not isinstance(config, dict):
        config = {k: v for k, v in vars(config).items() if not k.startswith('_')}
    warnings: List[str] = []

    # ── Range checks ──────────────────────────────────────────
    for param, lo, hi, default, desc in _RANGE_RULES:
        val = config.get(param)
        if val is None:
            continue  # not set — will use VideoEditor default, which is safe
        try:
            fval = float(val)
        except (TypeError, ValueError):
            warnings.append(
                f"[{slug}] {param}={val!r} is not numeric — forcing {default} ({desc})"
            )
            config[param] = default
            continue
        if fval < lo or fval > hi:
            warnings.append(
                f"[{slug}] {param}={fval} out of range [{lo}, {hi}] — forcing {default} ({desc})"
            )
            config[param] = default

    # ── Cross-parameter validation: zoom_min < zoom_max ────────
    zoom_min = config.get("KEN_BURNS_ZOOM_MIN", 5)
    zoom_max = config.get("KEN_BURNS_ZOOM_MAX", 10)
    if zoom_min >= zoom_max:
        warnings.append(
            f"[{slug}] KEN_BURNS_ZOOM_MIN ({zoom_min}) >= ZOOM_MAX ({zoom_max}) — "
            f"forcing ZOOM_MAX={zoom_min + 5}"
        )
        config["KEN_BURNS_ZOOM_MAX"] = zoom_min + 5

    scene_min = config.get("SCENE_DURATION_MIN", 5)
    scene_max = config.get("SCENE_DURATION_MAX", 20)
    if scene_min >= scene_max:
        warnings.append(
            f"[{slug}] SCENE_DURATION_MIN ({scene_min}) >= SCENE_DURATION_MAX ({scene_max}) — "
            f"forcing MAX={scene_min + 5}"
        )
        config["SCENE_DURATION_MAX"] = scene_min + 5
    elif scene_max < 5:
        # Enforce sanity floor: max < 5s would produce excessive sub-scenes.
        # (Oct 2025: lowered floor from 12 → 5 to allow ~10s scene pacing.)
        warnings.append(
            f"[{slug}] SCENE_DURATION_MAX ({scene_max}) is too low — "
            f"forcing MAX=10 to avoid excessive scene splitting"
        )
        config["SCENE_DURATION_MAX"] = 10

    # Media-specific limits intentionally have independent pacing.  They are
    # validated separately so a channel can tune image and video cadence
    # without changing legacy SCENE_DURATION_* consumers.
    for kind in ("IMAGE", "VIDEO"):
        min_key = f"{kind}_SCENE_DURATION_MIN"
        max_key = f"{kind}_SCENE_DURATION_MAX"
        minimum = config.get(min_key)
        maximum = config.get(max_key)
        if minimum is not None and maximum is not None and minimum >= maximum:
            fallback_max = 7.0 if kind == "IMAGE" else 10.0
            fallback_min = 4.0 if kind == "IMAGE" else 6.0
            warnings.append(
                f"[{slug}] {min_key} ({minimum}) >= {max_key} ({maximum}) — "
                f"forcing {min_key}={fallback_min}, {max_key}={fallback_max}"
            )
            config[min_key] = fallback_min
            config[max_key] = fallback_max

    # ── Color palette: prevent solid-black vignette ────────────
    color_pal = config.get("COLOR_PALETTE")
    if isinstance(color_pal, dict):
        secondary = color_pal.get("secondary")
        if isinstance(secondary, (tuple, list)) and len(secondary) == 3:
            rgb_sum = sum(secondary)
            if rgb_sum == 0:
                warnings.append(
                    f"[{slug}] COLOR_PALETTE.secondary={secondary} is pure black — "
                    f"forcing (15, 25, 45)"
                )
                color_pal["secondary"] = (15, 25, 45)
            elif rgb_sum < _MIN_SECONDARY_RGB_SUM:
                warnings.append(
                    f"[{slug}] COLOR_PALETTE.secondary={secondary} RGB sum={rgb_sum} < "
                    f"{_MIN_SECONDARY_RGB_SUM} — very dark, consider lightening"
                )

    # ── Media strategy nested params ───────────────────────────
    media = config.get("MEDIA_STRATEGY")
    if isinstance(media, dict):
        for param, lo, hi, default, desc in _MEDIA_STRATEGY_RULES:
            val = media.get(param)
            if val is None:
                continue
            try:
                fval = float(val)
            except (TypeError, ValueError):
                warnings.append(
                    f"[{slug}] MEDIA_STRATEGY.{param}={val!r} is not numeric — forcing {default} ({desc})"
                )
                media[param] = default
                continue
            if fval < lo or fval > hi:
                warnings.append(
                    f"[{slug}] MEDIA_STRATEGY.{param}={fval} out of range [{lo}, {hi}] — forcing {default} ({desc})"
                )
                media[param] = default

    # ── Log results ────────────────────────────────────────────
    if warnings:
        for w in warnings:
            logger.warning(w)
    else:
        logger.info("[%s] Config validated — all %d visual params within safe ranges",
                     slug, len(_RANGE_RULES) + len(_MEDIA_STRATEGY_RULES) + 2)

    return warnings


def validate_all_channels() -> List[str]:
    """Validate all configured channels.  Called during API startup.

    Returns combined warning list across all channels.
    """
    from config.config_bridge import get_all_channel_configs

    all_warnings: List[str] = []
    channels = get_all_channel_configs()
    for slug, config in channels.items():
        try:
            w = validate_channel_config(slug, config)
            all_warnings.extend(w)
        except Exception as exc:
            logger.exception("Config validation crashed for %s: %s", slug, exc)
            all_warnings.append(f"[{slug}] validator crashed: {exc}")
    return all_warnings
