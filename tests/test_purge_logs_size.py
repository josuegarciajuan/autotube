"""Tests for the size-based log retention in ``scripts/purge_logs_and_backups.py``."""

import importlib.util
import os
import time
from pathlib import Path

import pytest


@pytest.fixture
def purge_mod(tmp_path, monkeypatch):
    repo_root = Path(__file__).resolve().parent.parent
    spec = importlib.util.spec_from_file_location(
        "purge_logs_and_backups",
        repo_root / "scripts" / "purge_logs_and_backups.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.ROOT = tmp_path
    module._tracked_files = lambda: set()
    return module


def _write(path: Path, size_mb: float, age_days: float):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * int(size_mb * 1024 * 1024))
    ts = time.time() - age_days * 86400
    os.utime(path, (ts, ts))


def test_size_cap_prefers_oldest_non_active(purge_mod):
    logs = purge_mod.ROOT / "logs"
    _write(logs / "a.log", 1, age_days=2)
    _write(logs / "b.log.1", 1, age_days=3)
    _write(logs / "obs" / "obs.log.2026-01-01", 1, age_days=5)
    _write(logs / "api.log", 1, age_days=0)  # active today

    to_delete, remaining, before = purge_mod._plan_size_purge(2.0)

    assert before == 4 * 1024 * 1024
    assert remaining <= 2 * 1024 * 1024
    names = {p.name for p in to_delete}
    assert "api.log" not in names, "active file must only be deleted as last resort"


def test_size_cap_deletes_active_as_last_resort(purge_mod):
    logs = purge_mod.ROOT / "logs"
    _write(logs / "a.log", 1, age_days=2)
    _write(logs / "api.log", 1, age_days=0)

    to_delete, remaining, _ = purge_mod._plan_size_purge(0.5)
    assert remaining <= 0.5 * 1024 * 1024
    assert any(p.name == "api.log" for p in to_delete)

    # The actual purge deletes the planned files.
    for path in to_delete:
        path.unlink()
    assert remaining == 0


def test_size_cap_noop_when_under_budget(purge_mod):
    logs = purge_mod.ROOT / "logs"
    _write(logs / "a.log", 1, age_days=2)
    to_delete, remaining, before = purge_mod._plan_size_purge(1000.0)
    assert to_delete == []
    assert remaining == before


def test_size_cap_never_deletes_tracked(purge_mod):
    logs = purge_mod.ROOT / "logs"
    _write(logs / "a.log", 1, age_days=2)
    tracked = {str((logs / "a.log").resolve())}
    purge_mod._tracked_files = lambda: tracked
    to_delete, remaining, _ = purge_mod._plan_size_purge(0.1)
    assert to_delete == []
    assert (logs / "a.log").exists()
    # `remaining` only accounts for the (empty) unprotected set.
    assert remaining == 0
