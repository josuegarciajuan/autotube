"""Tests for the hard spam filter: title similarity guard + strike/block breaker."""

import time

import pytest


def test_titles_too_similar_detects_near_duplicates():
    from api.services.shorts_scheduler import _titles_too_similar
    # Same topic, slightly reworded → must be flagged
    assert _titles_too_similar(
        "El misterio de la expedición perdida en el Ártico",
        "El misterio de la expedición que se perdió en el Ártico",
    ) is True
    # Different topics → must pass
    assert _titles_too_similar(
        "El barco fantasma de los Sargazos",
        "El desierto que envenenó a sus exploradores",
    ) is False


def test_titles_too_similar_empty_and_short():
    from api.services.shorts_scheduler import _titles_too_similar
    assert _titles_too_similar("", "x") is False
    assert _titles_too_similar("a b c", "a b c") is True


def test_title_similar_guard_never_flags_itself(monkeypatch):
    """Regression: a long-form video must NOT flag itself (title already saved
    in `videos` when the check runs). `exclude_longform_id` skips it."""
    from api.services import shorts_scheduler as ss

    own_title = "Masacre de Pascommuck: la familia que escapó del INFIERNO (REAL)"

    # Simulate the DB already containing the video's own title (saved before
    # the check runs): with exclude_longform_id=2220 it must NOT match itself.
    monkeypatch.setattr(
        ss, "_recent_longform_titles",
        lambda ch, days=30, limit=30, exclude_id=None: (
            [] if exclude_id == 2220 else [(2220, own_title)]
        ),
    )
    similar, what = ss._title_similar_to_recent(
        5, own_title,
        check_shorts=False, check_longform=True, exclude_longform_id=2220,
    )
    assert similar is False, f"video must not flag itself: {what}"

    # Same title WITHOUT exclusion → still detected as similar (guard works)
    similar, what = ss._title_similar_to_recent(
        5, own_title, check_shorts=False, check_longform=True,
    )
    assert similar is True and "long-form #2220" in what


def test_title_similar_guard_detects_other_recent_video(monkeypatch):
    """Regression: exclusion must NOT hide a genuinely similar title from a
    DIFFERENT recent video."""
    from api.services import shorts_scheduler as ss

    monkeypatch.setattr(
        ss, "_recent_longform_titles",
        lambda ch, days=30, limit=30, exclude_id=None: [(100, "El misterio de la expedición perdida en el Ártico")],
    )
    similar, what = ss._title_similar_to_recent(
        5, "El misterio de la expedición que se perdió en el Ártico",
        check_shorts=False, check_longform=True, exclude_longform_id=999,
    )
    assert similar is True and "long-form #100" in what


def _minimal_db(tmp_path, name):
    """Create a minimal SQLite DB with just the system_state table."""
    import sqlite3
    from database.db_extended import ExtendedDatabase
    db_path = tmp_path / name
    conn = sqlite3.connect(str(db_path))
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS system_state (
            key TEXT PRIMARY KEY,
            value TEXT,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS channels (
            id INTEGER PRIMARY KEY,
            slug TEXT,
            name TEXT,
            active INTEGER DEFAULT 1
        );
        """
    )
    conn.commit()
    conn.close()
    return db_path, ExtendedDatabase(str(db_path))


def test_spam_strike_blocks_channel(tmp_path):
    from api.services import shorts_scheduler as ss

    db_path, db = _minimal_db(tmp_path, "spam_test.db")
    with db._connect() as conn:
        conn.execute(
            "INSERT INTO channels (id, slug, name, active) VALUES (77, 'testchan', 'Test', 1)"
        )
        conn.commit()

    # No strike yet → not blocked
    assert ss._channel_shorts_spam_blocked(77, db=db) is False

    # First strike → blocked 72h
    n = ss._record_short_spam_strike(77, "testchan", db=db)
    assert n == 1
    assert ss._channel_shorts_spam_blocked(77, db=db) is True

    # Second strike → escalated (still blocked; longer window)
    n2 = ss._record_short_spam_strike(77, "testchan", db=db)
    assert n2 == 2
    raw = db.get_system_state("shorts_spam_blocked_until_77")
    remaining = float(raw) - time.time()
    assert remaining > 72 * 3600  # escalated beyond 72h


def test_spam_strike_survives_restart(tmp_path):
    """Block state is in system_state (DB), so API restarts don't clear it."""
    from api.services import shorts_scheduler as ss

    db_path, db1 = _minimal_db(tmp_path, "spam_restart.db")
    with db1._connect() as conn:
        conn.execute(
            "INSERT INTO channels (id, slug, name, active) VALUES (78, 'testchan2', 'Test2', 1)"
        )
        conn.commit()
    ss._record_short_spam_strike(78, "testchan2", db=db1)

    # Simulate API restart: new DB handle
    from database.db_extended import ExtendedDatabase
    db2 = ExtendedDatabase(str(db_path))
    assert ss._channel_shorts_spam_blocked(78, db=db2) is True
