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
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Optional

logger = logging.getLogger(__name__)

# Cache: slug → config object (cleared on re-sync)
_config_cache: dict[str, SimpleNamespace] = {}


def _load_defaults_module() -> object | None:
    """Import ``config.defaults`` — universal fallback for all channels."""
    try:
        return importlib.import_module("config.defaults")
    except ImportError:
        logger.warning("Cannot load config.defaults — universal defaults unavailable")
        return None


def _load_python_module(slug: str) -> object | None:
    """Import ``config.{slug}_config`` as a Python module.

    Forces a reload if the module was already imported — this ensures
    ``sync_config_to_db`` always picks up the latest on-disk values
    instead of stale cached module attributes.
    """
    module_name = f"config.{slug}_config"
    try:
        mod = importlib.import_module(module_name)
        # If already cached, reload to pick up file changes since last import
        if module_name in sys.modules:
            mod = importlib.reload(mod)
        return mod
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


def _clean_duplicate_keys(db_config: dict, py_safe: dict) -> dict:
    """Remove duplicate normalized keys from DB config, keeping canonical casing.

    When the DB has both ``PROD_SCRIPT_BLOCKS_MAX=18`` (from sync) and
    ``prod_script_blocks_max=6`` (from UI panel), this detects the collision
    via case-insensitive matching and removes the non-canonical duplicate,
    preferring the UPPER_SNAKE_CASE variant (matching Python config naming).

    Args:
        db_config: The raw DB ``config_json`` dict.
        py_safe: Python-safe config fields (UPPER_SNAKE_CASE keys).

    Returns:
        Cleaned dict with no duplicate normalized keys.
    """
    if not db_config:
        return db_config

    # Group keys by normalized (upper) form
    groups: dict[str, list[str]] = {}
    for key in db_config:
        upper = key.upper()
        if upper not in groups:
            groups[upper] = []
        groups[upper].append(key)

    keys_to_remove: list[str] = []
    for upper, keys in groups.items():
        if len(keys) <= 1:
            continue  # No collision

        # Prefer the key that matches Python's UPPER_SNAKE_CASE
        canonical = None
        for k in keys:
            if k in py_safe:
                canonical = k
                break

        if canonical is None:
            # Python doesn't have this key — keep the UPPER_SNAKE_CASE variant
            for k in keys:
                if k == k.upper() and "_" in k:
                    canonical = k
                    break

        if canonical is None:
            canonical = keys[0]  # fallback: keep first

        for k in keys:
            if k != canonical:
                keys_to_remove.append(k)

    if keys_to_remove:
        cleaned = dict(db_config)
        for k in keys_to_remove:
            del cleaned[k]
        return cleaned

    return db_config


def _deep_merge_dicts(base: dict, overlay: dict) -> dict:
    """Recursively merge mappings, with values in *overlay* taking precedence."""
    result = dict(base)
    for key, value in overlay.items():
        if isinstance(result.get(key), dict) and isinstance(value, dict):
            result[key] = _deep_merge_dicts(result[key], value)
        else:
            result[key] = value
    return result


def _merge_policy_dict(defaults: object, db_value: object, python_value: object) -> object:
    """Merge policy dictionaries while keeping explicitly defined Python keys.

    DB may add or tune nested policy fields, but a value explicitly supplied by
    the channel's Python module remains authoritative at that same nesting level.
    """
    if not any(isinstance(value, dict) for value in (defaults, db_value, python_value)):
        return python_value if python_value is not None else db_value
    merged: dict = {}
    for value in (defaults, db_value, python_value):
        if isinstance(value, dict):
            merged = _deep_merge_dicts(merged, value)
    return merged


def _merge_configs(
    py_mod: object,
    db_config: dict | None,
    defaults_mod: object | None = None,
) -> SimpleNamespace:
    """Merge defaults + channel module + DB config into a single namespace.

    Priority order (highest wins):
      1. DB ``channels.config_json``  (UI edits, immediate effect)
      2. ``config.{slug}_config.py``  (per-channel overrides)
      3. ``config.defaults.py``       (universal fallback)

    Keys are matched case-insensitively.
    """
    # Build merged dict, starting with defaults as the base layer
    merged: dict[str, object] = {}

    # ── Layer 1: Universal defaults ────────────────────────────
    if defaults_mod is not None:
        for name, value in vars(defaults_mod).items():
            if name.startswith("_"):
                continue
            if inspect.ismodule(value) or inspect.isfunction(value) or inspect.isclass(value):
                continue
            if isinstance(value, (dict, list, str, int, float, bool, tuple, type(None))):
                merged[name] = value

    # ── Layer 2: Per-channel Python module ─────────────────────
    for name, value in vars(py_mod).items():
        if name.startswith("_"):
            continue
        if inspect.ismodule(value) or inspect.isfunction(value) or inspect.isclass(value):
            continue
        if isinstance(value, (dict, list, str, int, float, bool, tuple, type(None))):
            merged[name] = value

    if db_config:
        # Build a lookup: UPPER name → original key
        py_upper: dict[str, str] = {k.upper(): k for k in merged}
        # Track which normalized keys we've already applied — prevents
        # duplicate keys (e.g. PROD_SCRIPT_BLOCKS_MAX=18 from sync + 
        # prod_script_blocks_max=6 from UI) from overwriting each other.
        seen_normalized: set[str] = set()

        for db_key, db_value in db_config.items():
            # Policy dictionaries are merged below so DB can add nested fields.
            if db_key.upper() in {"MEDIA_STRATEGY", "CROSS_PLATFORM"}:
                continue
            upper_key = db_key.upper()

            # ── Detect and skip duplicate normalized keys ──────
            # When a channel has both uppercase (from sync) and lowercase
            # (from UI panel) versions of the same config key, the first
            # one wins. This prevents the UI's lowercase variant from
            # overwriting the authoritative uppercase value.
            if upper_key in seen_normalized:
                logger.warning(
                    "Config bridge: skipping duplicate key '%s' (normalized='%s') — "
                    "already set from earlier DB entry. Remove duplicate keys from "
                    "channels.config_json to silence this warning.",
                    db_key, upper_key,
                )
                continue
            seen_normalized.add(upper_key)

            # Try exact match first, then case-insensitive
            if db_key in merged:
                merged[db_key] = db_value
            elif upper_key in py_upper:
                merged[py_upper[upper_key]] = db_value
            else:
                # Unknown field — store as-is (preserves casing from DB)
                merged[db_key] = db_value

        # Preserve Python's authority only for keys it explicitly defines;
        # defaults and DB values still contribute missing nested policy fields.
        for policy_key in ("MEDIA_STRATEGY", "CROSS_PLATFORM"):
            db_key = next((key for key in db_config if key.upper() == policy_key), None)
            if db_key is None:
                continue
            merged[policy_key] = _merge_policy_dict(
                vars(defaults_mod).get(policy_key) if defaults_mod else None,
                db_config[db_key],
                vars(py_mod).get(policy_key),
            )

    # Channel identity fields must never be overridden from DB —
    # the Python module is the authoritative source.
    for identity_key in ("CANAL_DISPLAY_NAME", "CANAL_NAME"):
        if identity_key in vars(py_mod):
            merged[identity_key] = vars(py_mod)[identity_key]
        elif defaults_mod is not None and identity_key in vars(defaults_mod):
            merged[identity_key] = vars(defaults_mod)[identity_key]

    # Add pseudo-attributes so code that expects module-style access works
    # (e.g. cfg.REDDIT_SUBREDDITS and cfg.reddit_subreddits)
    for key in list(merged.keys()):
        if key.isupper() and key.lower() not in merged:
            merged[key.lower()] = merged[key]

    # JSON round-trip turns tuples into lists. Restore all tuple-valued
    # config entries using the Python module + defaults as the authoritative reference.
    for key in list(merged.keys()):
        # Prefer channel module value, fall back to defaults
        py_val = vars(py_mod).get(key)
        if py_val is None and defaults_mod is not None:
            py_val = vars(defaults_mod).get(key)
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
    2. Python module ``config.{slug}_config`` (per-channel overrides)
    3. ``config.defaults.py`` (universal fallback for all channels)

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

    defaults_mod = _load_defaults_module()
    db_config = _load_db_config(slug)
    merged = _merge_configs(py_mod, db_config, defaults_mod)

    _config_cache[cache_key] = merged
    logger.debug(
        "Config bridge loaded: slug=%s py_fields=%d db_override=%s defaults=%s",
        slug,
        len(vars(merged)),
        "yes" if db_config else "no",
        "yes" if defaults_mod else "no",
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

        # ── Clean duplicate keys from existing DB config ─────────
        # When the DB has both UPPER_SNAKE_CASE and lowercase versions
        # of the same config key (e.g. PROD_SCRIPT_BLOCKS_MAX=18 and
        # prod_script_blocks_max=6), remove the lowercase duplicate.
        # Normalizes to a single UPPER_SNAKE_CASE key per canonical name.
        cleaned_db = _clean_duplicate_keys(existing_db, safe)
        if cleaned_db != existing_db:
            logger.info(
                "sync_config_to_db: cleaned %d duplicate keys from %s config",
                len(existing_db) - len(cleaned_db), slug,
            )
            existing_db = cleaned_db

        if merge_mode and existing_db:
            # Merge: start with DB values (preserve all user edits),
            # only add NEW Python fields that don't exist in DB yet.
            merged = dict(existing_db)
            for key, value in safe.items():
                if key not in existing_db:
                    merged[key] = value
            # Policy mappings are merged recursively: Python wins on keys it
            # defines, while DB-only nested settings remain available.
            for policy_key in ("MEDIA_STRATEGY", "CROSS_PLATFORM"):
                if policy_key in safe:
                    merged[policy_key] = _merge_policy_dict(
                        None, existing_db.get(policy_key), safe[policy_key]
                    )
            # Preserve DB-only planning keys
            for key in ("videos_per_day", "planning_enabled",
                        "alternate_pattern", "alternate_offset"):
                if key in existing_db and key not in merged:
                    merged[key] = existing_db[key]
            safe = merged
        else:
            # Full replace: Python values overwrite everything (manual sync).
            # Keep DB-only planning keys that don't exist in Python configs.
            for key in ("videos_per_day", "planning_enabled",
                        "alternate_pattern", "alternate_offset"):
                if key in existing_db and key not in safe:
                    safe[key] = existing_db[key]
            for policy_key in ("MEDIA_STRATEGY", "CROSS_PLATFORM"):
                if policy_key in safe and policy_key in existing_db:
                    safe[policy_key] = _merge_policy_dict(
                        None, existing_db[policy_key], safe[policy_key]
                    )

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
        # ── First: clean duplicate keys from all channels ──────
        _clean_duplicate_configs_in_db(db)
        for ch in db.get_channels(active_only=True):
            slug = ch["slug"]
            if sync_config_to_db(slug, merge_mode=True) is not None:
                synced.append(slug)
    except Exception as exc:
        logger.error("sync_all_configs_to_db failed: %s", exc)
    return synced


def _clean_duplicate_configs_in_db(db) -> int:
    """Remove lowercase duplicate config keys from ALL channels in the DB.

    Called at startup to ensure the DB is clean of stale duplicate keys
    that cause the config bridge collision bug where lowercase values
    (from UI panel edits) overwrite uppercase values (from Python sync).

    Returns count of channels cleaned.
    """
    cleaned_count = 0
    try:
        for ch in db.get_channels(active_only=True):
            slug = ch.get("slug", "?")
            try:
                existing = json.loads(ch.get("config_json", "{}"))
            except (json.JSONDecodeError, TypeError):
                continue

            # Build py_safe: the Python config's UPPER_SNAKE_CASE keys
            py_mod = _load_python_module(slug)
            if py_mod is None:
                continue
            py_safe = {}
            for name, value in vars(py_mod).items():
                if name.startswith("_"):
                    continue
                if isinstance(value, (dict, list, str, int, float, bool, tuple, type(None))):
                    py_safe[name] = value

            cleaned = _clean_duplicate_keys(existing, py_safe)
            if cleaned != existing:
                db.update_channel(ch["id"], config=cleaned)
                cleaned_count += 1
                logger.info(
                    "Cleaned %d duplicate config keys from channel %s (%s)",
                    len(existing) - len(cleaned), ch.get("name", slug), slug,
                )
    except Exception as exc:
        logger.error("_clean_duplicate_configs_in_db failed: %s", exc)
    return cleaned_count


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
