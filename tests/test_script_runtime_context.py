import sqlite3

import pytest


def make_db(path):
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE channels (id INTEGER PRIMARY KEY, slug TEXT UNIQUE, active INTEGER, google_account TEXT)"
    )
    conn.executemany(
        "INSERT INTO channels VALUES (?, ?, ?, ?)",
        [(2, "canal2", 1, "tracatrack"), (3, "canal3", 1, "tracatrack"),
         (5, "canal4", 1, "burrianacasa2026"), (9, "off", 0, "other")],
    )
    conn.commit()
    conn.close()


def test_selector_requires_exactly_one_explicit_scope(tmp_path):
    from scripts.runtime_context import resolve_channels, SelectorError

    db = tmp_path / "test.db"
    make_db(db)

    with pytest.raises(SelectorError, match="exactamente un selector"):
        resolve_channels(db_path=db)
    with pytest.raises(SelectorError, match="exactamente un selector"):
        resolve_channels(db_path=db, slug="canal2", channel_id=2)


def test_selectors_resolve_db_channels_and_projects_without_maps(tmp_path):
    from scripts.runtime_context import resolve_channels

    db = tmp_path / "test.db"
    make_db(db)
    project = lambda slug: {"canal2": "project-a", "canal3": "project-a", "canal4": "project-b"}[slug]

    assert [c.slug for c in resolve_channels(db_path=db, channel_id=5, project_resolver=project)] == ["canal4"]
    assert [c.slug for c in resolve_channels(db_path=db, slug="canal2", project_resolver=project)] == ["canal2"]
    assert [c.slug for c in resolve_channels(db_path=db, project="project-a", project_resolver=project)] == ["canal2", "canal3"]
    assert [c.slug for c in resolve_channels(db_path=db, all_channels=True, yes=True, project_resolver=project)] == ["canal2", "canal3", "canal4"]


def test_all_requires_confirmation_before_resolution(tmp_path):
    from scripts.runtime_context import resolve_channels, SelectorError

    db = tmp_path / "test.db"
    make_db(db)
    with pytest.raises(SelectorError, match="--yes"):
        resolve_channels(db_path=db, all_channels=True, yes=False)
