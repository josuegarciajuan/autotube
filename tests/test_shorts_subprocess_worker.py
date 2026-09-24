"""Tests for routing shorts (native/clip/standalone) through a subprocess worker.

Regression context (sep 2026): shorts ran in-process (asyncio.to_thread) with no
worker_pid, so any API restart killed the render and ``scripts/deploy_safety.py``
blocked every deploy while a short rendered. Now a detached worker
(``api/services/shorts_worker.py``) owns generation + finalization and survives
restarts. These tests cover the pieces that decide/branch on that behaviour.
"""

import argparse
import sqlite3
import subprocess

import pytest


# ═══════════════════════════════════════════════════════════════
# deploy_safety — shorts workers count as subprocess (survive restart)
# ═══════════════════════════════════════════════════════════════

def test_deploy_safety_pattern_matches_shorts_worker():
    import scripts.deploy_safety as ds

    assert "shorts_worker" in ds._WORKER_PATTERN
    assert "full_pipeline_worker" in ds._WORKER_PATTERN


def test_deploy_safety_shorts_job_proceeds():
    import scripts.deploy_safety as ds

    decision, reason = ds.decide(
        job_ids=[11072], use_subprocess=True, kill_mode="process",
        pid_fn=lambda jid: 451823,  # shorts_worker PID found
    )
    assert decision == "proceed"
    assert "subprocess" in reason


# ═══════════════════════════════════════════════════════════════
# DB counts — standalone shorts are NOT long-form
# ═══════════════════════════════════════════════════════════════

def _db_with_jobs(tmp_path, rows):
    db = tmp_path / "counts.db"
    conn = sqlite3.connect(str(db))
    conn.execute(
        "CREATE TABLE generation_jobs ("
        "id INTEGER PRIMARY KEY, channel_id INTEGER, action TEXT, status TEXT)"
    )
    conn.executemany(
        "INSERT INTO generation_jobs (channel_id, action, status) VALUES (?, ?, ?)",
        rows,
    )
    conn.commit()
    conn.close()
    return str(db)


def test_standalone_excluded_from_longform_count(tmp_path):
    from database.db_extended import ExtendedDatabase

    db = _db_with_jobs(tmp_path, [
        (7, "generate_standalone_short", "running"),
        (7, "generate_native_short", "running"),
        (7, "generate_clip_short", "queued"),
    ])
    x = ExtendedDatabase(db)
    assert x.count_active_longform_jobs() == 0
    assert x.count_active_shorts_jobs() == 3


def test_longform_counted_and_shorts_separate(tmp_path):
    from database.db_extended import ExtendedDatabase

    db = _db_with_jobs(tmp_path, [
        (3, "generate_only", "running"),
        (7, "generate_standalone_short", "running"),
    ])
    x = ExtendedDatabase(db)
    assert x.count_active_longform_jobs() == 1
    assert x.count_active_shorts_jobs() == 1


# ═══════════════════════════════════════════════════════════════
# _finalize_short_dispatch — worker-owned finalization
# ═══════════════════════════════════════════════════════════════

def _db_for_finalize(tmp_path):
    db = tmp_path / "finalize.db"
    conn = sqlite3.connect(str(db))
    conn.execute(
        "CREATE TABLE generation_jobs ("
        "id INTEGER PRIMARY KEY, channel_id INTEGER, action TEXT, status TEXT, "
        "error_msg TEXT, result_short_id INTEGER, finished_at TIMESTAMP)"
    )
    conn.execute(
        "CREATE TABLE shorts_planned_slots ("
        "id INTEGER PRIMARY KEY, status TEXT, short_id INTEGER, retry_count INTEGER, "
        "error_message TEXT, job_id INTEGER, scheduled_at TIMESTAMP, "
        "updated_at TIMESTAMP)"
    )
    conn.commit()
    conn.close()
    return str(db)


def _patch_finalize_env(monkeypatch, db_path):
    monkeypatch.setattr("config.settings.DATABASE_PATH", db_path)
    # Avoid touching the alert subsystem (needs more tables).
    import api.services.shorts_scheduler as ss
    monkeypatch.setattr(ss, "_alert_standalone_failed", lambda *a, **k: None)
    monkeypatch.setattr(ss, "_alert_short_dispatch_failed", lambda *a, **k: None)
    monkeypatch.setattr(ss, "_defer_slot_pacing_reason", lambda *a, **k: "")


def test_finalize_standalone_success_sets_result_short_id(tmp_path, monkeypatch):
    db = _db_for_finalize(tmp_path)
    import sqlite3 as s
    conn = s.connect(db)
    conn.execute(
        "INSERT INTO generation_jobs (id, channel_id, action, status) "
        "VALUES (11072, 7, 'generate_standalone_short', 'running')"
    )
    conn.commit()
    conn.close()
    _patch_finalize_env(monkeypatch, db)

    from api.services.shorts_scheduler import _finalize_short_dispatch
    _finalize_short_dispatch(None, 11072, 7, short_id=555, exc=None, generate_only=True)

    conn = s.connect(db)
    row = conn.execute(
        "SELECT status, result_short_id FROM generation_jobs WHERE id = 11072"
    ).fetchone()
    conn.close()
    assert row[0] == "completed"
    assert row[1] == 555


def test_finalize_standalone_failure_marks_failed(tmp_path, monkeypatch):
    db = _db_for_finalize(tmp_path)
    import sqlite3 as s
    conn = s.connect(db)
    conn.execute(
        "INSERT INTO generation_jobs (id, channel_id, action, status) "
        "VALUES (11073, 7, 'generate_standalone_short', 'running')"
    )
    conn.commit()
    conn.close()
    _patch_finalize_env(monkeypatch, db)

    from api.services.shorts_scheduler import _finalize_short_dispatch
    _finalize_short_dispatch(None, 11073, 7, short_id=None,
                             exc=RuntimeError("timeout after 499s"), generate_only=True)

    conn = s.connect(db)
    row = conn.execute(
        "SELECT status, error_msg FROM generation_jobs WHERE id = 11073"
    ).fetchone()
    conn.close()
    assert row[0] == "failed"
    assert "timeout" in (row[1] or "")


def test_finalize_slot_success_links_slot(tmp_path, monkeypatch):
    db = _db_for_finalize(tmp_path)
    import sqlite3 as s
    conn = s.connect(db)
    conn.execute(
        "INSERT INTO generation_jobs (id, channel_id, action, status) "
        "VALUES (1, 7, 'generate_native_short', 'running')"
    )
    conn.execute(
        "INSERT INTO shorts_planned_slots (id, status, retry_count) "
        "VALUES (900, 'running', 0)"
    )
    conn.commit()
    conn.close()
    _patch_finalize_env(monkeypatch, db)

    from api.services.shorts_scheduler import _finalize_short_dispatch
    _finalize_short_dispatch(900, 1, 7, short_id=42, exc=None, generate_only=True)

    conn = s.connect(db)
    slot = conn.execute(
        "SELECT status, short_id FROM shorts_planned_slots WHERE id = 900"
    ).fetchone()
    job = conn.execute(
        "SELECT status, result_short_id FROM generation_jobs WHERE id = 1"
    ).fetchone()
    conn.close()
    assert slot == ("generated", 42)
    assert job == ("completed", 42)


# ═══════════════════════════════════════════════════════════════
# _spawn_short_worker — builds a detached subprocess
# ═══════════════════════════════════════════════════════════════

def test_spawn_short_worker_builds_cmd(monkeypatch, tmp_path):
    captured = {}

    class _FakePopen:
        def __init__(self, cmd, **kwargs):
            captured["cmd"] = cmd
            captured["kwargs"] = kwargs
            self.pid = 4242

    monkeypatch.setattr(subprocess, "Popen", _FakePopen)
    monkeypatch.setattr("config.settings.LOGS_DIR", tmp_path)

    from api.services.shorts_scheduler import _spawn_short_worker
    proc = _spawn_short_worker(
        short_type="standalone", channel_id=7, channel_slug="canal5",
        job_id=99, generate_only=True,
    )
    assert proc.pid == 4242
    cmd = captured["cmd"]
    assert "--standalone" in cmd
    assert "--job-id" in cmd and "99" in cmd
    assert "--generate-only" in cmd
    assert captured["kwargs"].get("start_new_session") is True


def test_shorts_subprocess_enabled_default(monkeypatch):
    monkeypatch.setattr("config.settings.SHORTS_USE_SUBPROCESS_WORKER", True)
    from api.services.shorts_scheduler import _shorts_subprocess_enabled
    assert _shorts_subprocess_enabled() is True


# ═══════════════════════════════════════════════════════════════
# shorts_worker — argument → dispatcher routing
# ═══════════════════════════════════════════════════════════════

def _args(**over):
    base = dict(
        standalone=False, clip=False, channel_id=7, channel_slug="canal5",
        slot_rank=0, job_id=5, target_upload_at=None, generate_only=True,
        source_video_id=None, pre_rendered_short_id=None, slot_id=0,
    )
    base.update(over)
    return argparse.Namespace(**base)


def test_worker_dispatch_standalone(monkeypatch):
    import api.services.shorts_worker as sw
    seen = {}

    def _fake_standalone(channel_id, slug, **kw):
        seen.update(kw)
        return 77

    monkeypatch.setattr(
        "api.services.shorts_scheduler._dispatch_standalone_short", _fake_standalone
    )
    assert sw._dispatch(_args(standalone=True)) == 77
    assert seen.get("generate_only") is True


def test_worker_dispatch_native_default(monkeypatch):
    import api.services.shorts_worker as sw
    seen = {}

    def _fake_native(channel_id, slug, **kw):
        seen.update(kw)
        return 88

    monkeypatch.setattr(
        "api.services.shorts_scheduler._dispatch_native_short", _fake_native
    )
    assert sw._dispatch(_args(slot_id=900)) == 88
    assert seen.get("slot_id") == 900


def test_worker_dispatch_clip_passes_source(monkeypatch):
    import api.services.shorts_worker as sw
    seen = {}

    def _fake_clip(channel_id, slug, source_video_id, **kw):
        seen["source_video_id"] = source_video_id
        return 99

    monkeypatch.setattr(
        "api.services.shorts_scheduler._dispatch_clip_short", _fake_clip
    )
    assert sw._dispatch(_args(clip=True, source_video_id=2478)) == 99
    assert seen["source_video_id"] == 2478
