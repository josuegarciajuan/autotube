"""Tests for the balanced anti-spam block policy and runtime migration."""

import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path


def test_spam_block_duration_is_12h_then_24h():
    from api.services.spam_mitigation import resolve_spam_block_duration_hours

    assert resolve_spam_block_duration_hours(1) == 12
    assert resolve_spam_block_duration_hours(2) == 24
    assert resolve_spam_block_duration_hours(99) == 24


def test_new_strikes_use_total_duration_without_legacy_buffer(tmp_path):
    from api.services import shorts_scheduler as ss

    db_path = tmp_path / "spam.db"
    conn = sqlite3.connect(db_path)
    conn.executescript("""
        CREATE TABLE system_state (key TEXT PRIMARY KEY, value TEXT,
                                    updated_at TEXT DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE channels (id INTEGER PRIMARY KEY, slug TEXT, name TEXT,
                               active INTEGER DEFAULT 1,
                               created_at TEXT DEFAULT CURRENT_TIMESTAMP);
    """)
    conn.execute("INSERT INTO channels(id, slug, name, active) VALUES (5, 'canal4', 'Canal 4', 1)")
    conn.commit()
    conn.close()
    from database.db_extended import ExtendedDatabase
    db = ExtendedDatabase(str(db_path))

    ss._record_short_spam_strike(5, "canal4", db=db)
    remaining = float(db.get_system_state("shorts_spam_blocked_until_5")) - time.time()
    assert 11.9 * 3600 < remaining <= 12 * 3600 + 2


def test_runtime_migration_is_dry_run_then_idempotent_and_replans(tmp_path):
    from scripts.migrate_spam_block_policy import migrate_spam_state

    db_path = tmp_path / "runtime.db"
    conn = sqlite3.connect(db_path)
    conn.executescript("""
        CREATE TABLE system_state (key TEXT PRIMARY KEY, value TEXT,
                                    updated_at TEXT DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE channels (id INTEGER PRIMARY KEY, slug TEXT, name TEXT,
                               active INTEGER DEFAULT 1,
                               created_at TEXT DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE videos (
            id INTEGER PRIMARY KEY, channel_id INTEGER, yt_video_id TEXT,
            published_at TEXT, status TEXT, target_public_at TEXT,
            updated_at TEXT
        );
        CREATE TABLE lifecycle_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT, entity_type TEXT,
            entity_id INTEGER, channel_id INTEGER, event TEXT, phase TEXT,
            status TEXT, message TEXT, metadata_json TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
    """)
    now = time.time()
    conn.execute("INSERT INTO channels(id, slug, name, active) VALUES (5, 'canal4', 'Canal 4', 1)")
    conn.execute("INSERT INTO system_state(key, value) VALUES (?, ?)",
                 ("shorts_spam_strikes_5", "1"))
    conn.execute("INSERT INTO system_state(key, value) VALUES (?, ?)",
                 ("shorts_spam_blocked_until_5", str(now + 80 * 3600)))
    conn.execute("INSERT INTO system_state(key, value) VALUES (?, ?)",
                 ("shorts_spam_blocked_until_77", str(now - 60)))
    conn.execute("INSERT INTO videos VALUES (1, 5, 'yt1', NULL, 'uploaded_private', ?, NULL)",
                 (datetime.fromtimestamp(now + 2 * 3600, timezone.utc).isoformat(),))
    conn.commit()
    conn.close()

    from database.db_extended import ExtendedDatabase
    db = ExtendedDatabase(str(db_path))
    preview = migrate_spam_state(db, apply=False, now=now)
    assert preview["changed_blocks"] == 1
    assert preview["expired"] == 1
    assert float(db.get_system_state("shorts_spam_blocked_until_5")) > now + 79 * 3600
    with db._connect() as check:
        assert check.execute("SELECT target_public_at FROM videos WHERE id=1").fetchone()[0].startswith("20")
        assert check.execute("SELECT COUNT(*) FROM lifecycle_events").fetchone()[0] == 0

    applied = migrate_spam_state(db, apply=True, now=now)
    assert applied["changed_blocks"] == 1
    until = float(db.get_system_state("shorts_spam_blocked_until_5"))
    assert now + 11.9 * 3600 < until <= now + 12 * 3600 + 1
    with db._connect() as check:
        target = check.execute("SELECT target_public_at FROM videos WHERE id=1").fetchone()[0]
        audit_count = check.execute("SELECT COUNT(*) FROM lifecycle_events").fetchone()[0]
    assert datetime.fromisoformat(target).timestamp() >= now + 13 * 3600 - 2
    assert audit_count >= 1

    again = migrate_spam_state(db, apply=True, now=now + 60)
    assert again["changed_blocks"] == 0
    assert float(db.get_system_state("shorts_spam_blocked_until_77")) == now - 60


def test_migration_replans_retained_chain_after_old_block_and_uses_google_account(tmp_path, monkeypatch):
    """Previously held videos can sit after old_until and must still move safely."""
    import json

    from scripts.migrate_spam_block_policy import migrate_spam_state
    from database.db_extended import ExtendedDatabase

    db_path = tmp_path / "retained.db"
    conn = sqlite3.connect(db_path)
    conn.executescript("""
        CREATE TABLE system_state (key TEXT PRIMARY KEY, value TEXT,
                                    updated_at TEXT DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE channels (id INTEGER PRIMARY KEY, slug TEXT, name TEXT,
                               google_account TEXT, active INTEGER DEFAULT 1,
                               created_at TEXT DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE videos (id INTEGER PRIMARY KEY, channel_id INTEGER,
            yt_video_id TEXT, published_at TEXT, status TEXT,
            target_public_at TEXT, updated_at TEXT);
        CREATE TABLE planned_slots (id INTEGER PRIMARY KEY, video_id INTEGER,
            target_public_at TEXT);
        CREATE TABLE video_lifecycle_actions (id INTEGER PRIMARY KEY,
            video_id INTEGER, action_type TEXT, status TEXT, scheduled_for TEXT);
        CREATE TABLE lifecycle_events (id INTEGER PRIMARY KEY AUTOINCREMENT,
            entity_type TEXT, entity_id INTEGER, channel_id INTEGER, event TEXT,
            phase TEXT, status TEXT, message TEXT, metadata_json TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP);
    """)
    now = 1_800_000_000.0
    old_until = now + 80 * 3600
    conn.execute("INSERT INTO channels(id,slug,name,google_account) VALUES (5,'canal4','Canal 4','burrianacasa2026')")
    conn.executemany("INSERT INTO system_state(key,value) VALUES (?,?)", [
        ("shorts_spam_strikes_5", "1"),
        ("shorts_spam_blocked_until_5", str(old_until)),
    ])
    # The old hold already placed these after old_until. They are a retained
    # chain, not arbitrary future videos.
    for video_id in range(1, 4):
        target = datetime.fromtimestamp(old_until + 3600 + (video_id - 1) * 86400,
                                        timezone.utc).isoformat()
        conn.execute("INSERT INTO videos VALUES (?,?,?,?,?,?,?)",
                     (video_id, 5, f"yt{video_id}", None, "uploaded_private", target, None))
        conn.execute("INSERT INTO planned_slots VALUES (?,?,?)", (video_id, video_id, target))
        conn.execute("INSERT INTO video_lifecycle_actions VALUES (?,?,?,?,?)",
                     (video_id, video_id, "go_public", "pending", target))
    unrelated = datetime.fromtimestamp(old_until + 5 * 3600, timezone.utc).isoformat()
    conn.execute("INSERT INTO videos VALUES (?,?,?,?,?,?,?)",
                 (4, 5, "yt4", None, "uploaded_private", unrelated, None))
    conn.commit()
    conn.close()

    calls = []
    class FakeUploader:
        def __init__(self, **kwargs):
            calls.append(("init", kwargs))
        def set_publish_at(self, video_id, target):
            calls.append((video_id, target))
    monkeypatch.setattr("pipeline.youtube_uploader.YouTubeUploader", FakeUploader)

    db = ExtendedDatabase(str(db_path))
    result = migrate_spam_state(db, apply=True, now=now)
    assert result["replanned"] == 3
    assert [call[0] for call in calls if call[0] != "init"] == ["yt1", "yt2", "yt3"]
    assert calls[0][1]["account_name"] == "burrianacasa2026"
    assert calls[0][1]["channel_slug"] == "canal4"
    with db._connect() as check:
        targets = [r[0] for r in check.execute(
            "SELECT target_public_at FROM videos ORDER BY id").fetchall()]
        slot_targets = [r[0] for r in check.execute(
            "SELECT target_public_at FROM planned_slots ORDER BY id").fetchall()]
        action_targets = [r[0] for r in check.execute(
            "SELECT scheduled_for FROM video_lifecycle_actions ORDER BY id").fetchall()]
    assert targets[:3] == slot_targets == action_targets
    assert datetime.fromisoformat(targets[3]).timestamp() == old_until + 5 * 3600
    assert datetime.fromisoformat(targets[0]).timestamp() == now + 13 * 3600
    assert datetime.fromisoformat(targets[1]).timestamp() - datetime.fromisoformat(targets[0]).timestamp() == 86400


def test_migration_script_runs_as_documented(tmp_path):
    db_path = tmp_path / "cli.db"
    conn = sqlite3.connect(db_path)
    conn.executescript("""
        CREATE TABLE system_state (key TEXT PRIMARY KEY, value TEXT,
                                   updated_at TEXT DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE channels (id INTEGER PRIMARY KEY, slug TEXT, name TEXT,
                               active INTEGER DEFAULT 1,
                               created_at TEXT DEFAULT CURRENT_TIMESTAMP);
    """)
    conn.commit()
    conn.close()

    project_root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [sys.executable, "scripts/migrate_spam_block_policy.py", "--dry-run"],
        cwd=project_root,
        env={**__import__("os").environ, "DATABASE_PATH": str(db_path)},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
