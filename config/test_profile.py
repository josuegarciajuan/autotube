"""Unified test profile — single source of truth for all test-mode settings.

Both ``test_video.py`` (CLI) and ``generation_service.py`` (API) use
``apply_test_profile()`` to ensure IDENTICAL behavior in all test paths.

Rationale:
    Before this module, the CLI and API had DIFFERENT test defaults
    (e.g. MAX_SCRIPT_BLOCKS=12 vs 5, IMAGE_PROCESSING_DISABLED=False vs True).
    This caused bugs to pass in one path but fail in the other. Unifying
    them guarantees that a green test in CLI = green test in API.

Usage::

    from config.test_profile import apply_test_profile
    apply_test_profile(config, mode="fast")  # or "default", "quarter", "quick"
"""
from __future__ import annotations
from typing import Any


# ── Test modes ───────────────────────────────────────────────────
# Each mode is a dict of config overrides. Modes are additive:
# "quarter" derives from PROD_*, "quick" uses QUICK_TEST_*, etc.
# The "fast" mode is the API-friendly fast-path for CI/dashboard testing.

TEST_PROFILES: dict[str, dict[str, Any]] = {
    # ── Default test mode (~3.5 min, full resolution, full effects) ──
    "default": {
        "TEST_MODE": True,
        "MAX_SCRIPT_BLOCKS": 12,
        "IMAGE_PROCESSING_DISABLED": False,
        # Word/block/duration targets come from the channel's TEST_SCRIPT_* config
    },

    # ── Fast test mode (~2-4 min, low res, no effects, no upload) ──
    # Used by both CLI --fast-test and API test_mode=True
    "fast": {
        "TEST_MODE": True,
        "VIDEO_RESOLUTION": (480, 270),
        "FFMPEG_PRESET": "ultrafast",
        "FILM_GRAIN_OPACITY": 0,
        "KEN_BURNS_ZOOM_MIN": 0,
        "KEN_BURNS_ZOOM_MAX": 0,
        "CROSSFADE_MIN": 0.1,
        "CROSSFADE_MAX": 0.2,
        "MAX_SCRIPT_BLOCKS": 8,      # unified: was 12 (CLI) / 5 (API)
        "SCENE_DURATION_MAX": 20.0,   # unified: was not-set (CLI) / 15.0 (API)
        "IMAGE_PROCESSING_DISABLED": True,
        "SKIP_UPLOAD": True,
        "SKIP_SCRAPE_IF_CONTENT": True,
    },

    # ── Quarter mode: ~25% of production duration ──
    "quarter": {
        "TEST_MODE": True,
        "MAX_SCRIPT_BLOCKS": 12,
        "IMAGE_PROCESSING_DISABLED": False,
        # Word/block targets are derived from PROD_* in test_video.py
    },

    # ── Quick mode (~30s, ultra-short) ──
    "quick": {
        "TEST_MODE": True,
        "MAX_SCRIPT_BLOCKS": 3,
        "MAX_SCRIPT_BLOCKS_MAX": 5,
        "IMAGE_PROCESSING_DISABLED": False,
    },

    # ── Production mode (explicit) ──
    "prod": {
        "TEST_MODE": False,
        "MAX_SCRIPT_BLOCKS": 0,       # 0 = no limit
        "IMAGE_PROCESSING_DISABLED": False,
    },
}


def apply_test_profile(config, mode: str = "fast") -> dict[str, Any]:
    """Apply a unified test profile to a config object (SimpleNamespace or module).

    Returns the profile dict for caller to apply additional mode-specific
    overrides (word targets, skip flags, etc.).

    Args:
        config: Config object with __dict__ or setattr support.
        mode: One of "default", "fast", "quarter", "quick", "prod".

    Returns:
        The profile dict that was applied.
    """
    profile = TEST_PROFILES.get(mode, TEST_PROFILES["default"])

    if hasattr(config, "__dict__"):
        for key, value in profile.items():
            setattr(config, key, value)
    elif hasattr(config, "__setattr__"):
        for key, value in profile.items():
            config.__setattr__(key, value)  # type: ignore[union-attr]
    else:
        # Fallback: dict-style assignment
        for key, value in profile.items():
            config[key] = value  # type: ignore[index]

    return profile


# ── Convenience: apply + return word targets for mode ───────────

def get_test_word_targets(config, mode: str = "default") -> tuple[int, int, int, int, float]:
    """Return (words_min, words_max, blocks_min, blocks_max, duration_minutes)
    for the given test mode based on the channel config.

    Used by test_video.py to derive --quarter and --quick targets from
    the channel's PROD_* constants.
    """
    if mode == "quick":
        words_min = getattr(config, "QUICK_TEST_SCRIPT_WORDS_MIN", 80)
        words_max = getattr(config, "QUICK_TEST_SCRIPT_WORDS_MAX", 120)
        blocks_min = getattr(config, "QUICK_TEST_SCRIPT_BLOCKS_MIN", 2)
        blocks_max = getattr(config, "QUICK_TEST_SCRIPT_BLOCKS_MAX", 3)
        duration_target = getattr(config, "QUICK_TEST_VIDEO_DURATION_TARGET", 0.5)
    elif mode == "quarter":
        prod_wmin = getattr(config, "PROD_SCRIPT_WORDS_MIN", 2000)
        prod_wmax = getattr(config, "PROD_SCRIPT_WORDS_MAX", 3500)
        prod_bmin = getattr(config, "PROD_SCRIPT_BLOCKS_MIN", 10)
        prod_bmax = getattr(config, "PROD_SCRIPT_BLOCKS_MAX", 18)
        prod_dur = getattr(config, "VIDEO_AVERAGE_DURATION_MIN", 8)
        words_min = max(300, prod_wmin // 4)
        words_max = max(500, prod_wmax // 4)
        blocks_min = max(3, prod_bmin // 3)
        blocks_max = max(5, prod_bmax // 3)
        duration_target = prod_dur / 4
    else:  # "default" or "fast"
        words_min = getattr(config, "TEST_SCRIPT_WORDS_MIN", 200)
        words_max = getattr(config, "TEST_SCRIPT_WORDS_MAX", 600)
        blocks_min = getattr(config, "TEST_SCRIPT_BLOCKS_MIN", 3)
        blocks_max = getattr(config, "TEST_SCRIPT_BLOCKS_MAX", 6)
        duration_target = getattr(config, "TEST_VIDEO_DURATION_TARGET", 2.0)

    return words_min, words_max, blocks_min, blocks_max, duration_target
