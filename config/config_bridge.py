"""Config bridge: merges DB config_json with Python config modules.

Provides ``get_channel_config(slug)`` — the single source for channel
configuration in the pipeline.  DB values override Python module values,
so UI edits (written to ``channels.config_json``) take immediate effect
on video generation.
"""

import importlib
import inspect
import json
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Optional

logger = logging.getLogger(__name__)

# Cache: slug → config object (cleared on re-sync)
_config_cache: dict[str, SimpleNamespace] = {}


def _load_python_module(slug: str) -> object | None:
    """Import ``config.{slug}_config`` as a Python module."""
    try:
        return importlib.import_module(f"config.{slug}_config")
    except ImportError:
        logger.warning("No Python config module for slug=%s", slug)
        return None


def _load_db_config(slug: str) -> dict | None:
    """Read ``config_json`` from the channels table."""
    try:
        from database.db_extended import ExtendedDatabase
        db = ExtendedDatabase()
        ch = db.get_channel_by_slug(slug)
        if not ch:
            return None
        raw = ch.get("config_json")
        if not raw or raw == "{}":
            return None
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        if isinstance(raw, str):
            return json.loads(raw)
        return raw
    except Exception as exc:
        logger.warning("Cannot read DB config for slug=%s: %s", slug, exc)
        return None


def _normalize_field_name(name: str) -> str:
    """Convert camelCase/snake_case to UPPER_SNAKE_CASE for matching."""
    return name.upper()


def _merge_configs(py_mod: object, db_config: dict | None) -> SimpleNamespace:
    """Merge Python module attributes with DB config overrides.

    DB keys take priority.  Keys are matched case-insensitively
    (both ``CANAL_TONE`` and ``canal_tone`` are recognised).
    """
    # Start with all non-private, non-callable module-level attributes
    merged: dict[str, object] = {}
    for name, value in vars(py_mod).items():
        if name.startswith("_"):
            continue
        if inspect.ismodule(value) or inspect.isfunction(value) or inspect.isclass(value):
            continue
        # Only serialisable types — the bridge won't carry arbitrary objects
        if isinstance(value, (dict, list, str, int, float, bool, tuple, type(None))):
            merged[name] = value

    if db_config:
        # Build a lookup: UPPER name → original key
        py_upper: dict[str, str] = {k.upper(): k for k in merged}

        for db_key, db_value in db_config.items():
            upper_key = db_key.upper()
            # Try exact match first, then case-insensitive
            if db_key in merged:
                merged[db_key] = db_value
            elif upper_key in py_upper:
                merged[py_upper[upper_key]] = db_value
            else:
                # Unknown field — store as-is (preserves casing from DB)
                merged[db_key] = db_value

    # Channel identity fields must never be overridden from DB —
    # the Python module is the authoritative source.
    for identity_key in ("CANAL_DISPLAY_NAME", "CANAL_NAME"):
        if identity_key in vars(py_mod):
            merged[identity_key] = vars(py_mod)[identity_key]

    # Add pseudo-attributes so code that expects module-style access works
    # (e.g. cfg.REDDIT_SUBREDDITS and cfg.reddit_subreddits)
    for key in list(merged.keys()):
        if key.isupper() and key.lower() not in merged:
            merged[key.lower()] = merged[key]

    # JSON round-trip turns tuples into lists. Restore all tuple-valued
    # config entries using the Python module as the authoritative reference.
    for key in list(merged.keys()):
        py_val = vars(py_mod).get(key)
        if py_val is None:
            continue
        # Top-level tuples (e.g. VIDEO_RESOLUTION, INTRO_BG_COLOR)
        if isinstance(py_val, tuple) and isinstance(merged.get(key), list):
            merged[key] = tuple(merged[key])
        # Nested tuples inside dicts (e.g. COLOR_PALETTE values)
        elif isinstance(py_val, dict) and isinstance(merged.get(key), dict):
            for sub_key, sub_val in py_val.items():
                merged_sub = merged[key].get(sub_key)
                if isinstance(sub_val, tuple) and isinstance(merged_sub, list):
                    merged[key][sub_key] = tuple(merged_sub)

    return SimpleNamespace(**merged)


def get_channel_config(slug: str, force_reload: bool = False) -> SimpleNamespace:
    """Return the merged channel configuration for *slug*.

    Priority order (highest wins):
    1. DB ``channels.config_json`` (can be edited via UI)
    2. Python module ``config.{slug}_config`` (authoritative defaults)

    Results are cached per slug.  Pass ``force_reload=True`` to bypass
    the cache (used after a config sync).

    Args:
        slug: Channel slug (e.g. ``"canal2"``).
        force_reload: If True, reload from sources even if cached.

    Returns:
        ``SimpleNamespace`` with all config attributes.
    """
    cache_key = slug
    if not force_reload and cache_key in _config_cache:
        return _config_cache[cache_key]

    py_mod = _load_python_module(slug)
    if py_mod is None:
        raise ImportError(f"Cannot load config for slug '{slug}'")

    db_config = _load_db_config(slug)
    merged = _merge_configs(py_mod, db_config)

    _config_cache[cache_key] = merged
    logger.debug(
        "Config bridge loaded: slug=%s py_fields=%d db_override=%s",
        slug,
        len(vars(merged)),
        "yes" if db_config else "no",
    )
    return merged


def sync_config_to_db(slug: str, merge_mode: bool = False) -> dict | None:
    """Write the Python module config into the ``channels.config_json`` column.

    Args:
        slug: Channel slug (e.g. ``"canal2"``).
        merge_mode: If True, only add NEW Python fields that don't already
            exist in the DB — preserves all existing user edits. If False
            (default), replace the entire DB config with Python values
            (used by the "Sync Python" button for full override).

    Returns the updated channel dict or None on failure.
    """
    py_mod = _load_python_module(slug)
    if py_mod is None:
        return None

    safe: dict[str, object] = {}
    for name, value in vars(py_mod).items():
        if name.startswith("_"):
            continue
        if inspect.ismodule(value) or inspect.isfunction(value) or inspect.isclass(value):
            continue
        if isinstance(value, (dict, list, str, int, float, bool, tuple, type(None))):
            safe[name] = value

    try:
        from database.db_extended import ExtendedDatabase
        db = ExtendedDatabase()
        ch = db.get_channel_by_slug(slug)
        if not ch:
            logger.warning("sync_config_to_db: channel slug=%s not in DB", slug)
            return None

        # Read existing DB config
        try:
            existing_db = json.loads(ch.get("config_json", "{}"))
        except (json.JSONDecodeError, TypeError):
            existing_db = {}

        if merge_mode and existing_db:
            # Merge: start with DB values (preserve all user edits),
            # only add NEW Python fields that don't exist in DB yet.
            merged = dict(existing_db)
            for key, value in safe.items():
                if key not in existing_db:
                    merged[key] = value
            # Preserve DB-only planning keys
            for key in ("videos_per_day", "planning_enabled"):
                if key in existing_db and key not in merged:
                    merged[key] = existing_db[key]
            safe = merged
        else:
            # Full replace: Python values overwrite everything (manual sync).
            # Keep DB-only planning keys that don't exist in Python configs.
            for key in ("videos_per_day", "planning_enabled"):
                if key in existing_db and key not in safe:
                    safe[key] = existing_db[key]

        # Update name from display name
        display_name = safe.get("CANAL_DISPLAY_NAME") or safe.get("canal_display_name")
        if display_name:
            db.update_channel(ch["id"], name=display_name)
        db.update_channel(ch["id"], config=safe)

        # Invalidate cache
        _config_cache.pop(slug, None)

        return db.get_channel(ch["id"])
    except Exception as exc:
        logger.error("sync_config_to_db failed for slug=%s: %s", slug, exc)
        return None


def sync_all_configs_to_db() -> list[str]:
    """Sync all *active* DB channels from their Python config modules.

    Only channels with active=1 are synced to prevent test/disabled channels
    from polluting the planning engine. Called on API startup.

    Returns the list of synced slugs.
    """
    synced: list[str] = []
    try:
        from database.db_extended import ExtendedDatabase
        db = ExtendedDatabase()
        for ch in db.get_channels(active_only=True):
            slug = ch["slug"]
            if sync_config_to_db(slug, merge_mode=True) is not None:
                synced.append(slug)
    except Exception as exc:
        logger.error("sync_all_configs_to_db failed: %s", exc)
    return synced


def get_all_channel_configs() -> dict[str, object]:
    """Return dict slug → SimpleNamespace for all *active* channels."""
    from database.db_extended import ExtendedDatabase
    result: dict[str, object] = {}
    try:
        db = ExtendedDatabase()
        for ch in db.get_channels(active_only=True):
            slug = ch["slug"]
            config = get_channel_config(slug)
            if config is not None:
                result[slug] = config
    except Exception as exc:
        logger.error("get_all_channel_configs failed: %s", exc)
    return result
