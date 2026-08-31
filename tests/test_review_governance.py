import json
import sqlite3
from types import SimpleNamespace

from api.services.review_governance import (
    find_exact_duplicate_groups,
    schedule_review_tasks,
)
from pipeline.editorial_guard import validate_new_content


class DB:
    def __init__(self, path):
        self.path = path

    def _connect(self):
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def get_system_state(self, key):
        return "2026-08-01T00:00:00+00:00"

    def get_channel_by_slug(self, slug):
        return {"id": 5, "slug": slug}


def test_review_schedule_is_idempotent(tmp_path):
    db = DB(str(tmp_path / "db.sqlite"))
    with db._connect() as c:
        c.executescript("""
        CREATE TABLE videos(id INTEGER PRIMARY KEY, channel_id INTEGER, titulo_final TEXT, status TEXT);
        CREATE TABLE channels(id INTEGER PRIMARY KEY);
        CREATE TABLE video_review_tasks(
          id INTEGER PRIMARY KEY AUTOINCREMENT, video_id INTEGER, channel_id INTEGER,
          review_kind TEXT, due_at TEXT, status TEXT DEFAULT 'pending',
          UNIQUE(video_id, review_kind));
        """)
        c.commit()
    assert schedule_review_tasks(db, 10, 5, "2026-08-02T00:00:00+00:00") == 5
    assert schedule_review_tasks(db, 10, 5, "2026-08-02T00:00:00+00:00") == 0
    with db._connect() as c:
        assert c.execute("SELECT COUNT(*) FROM video_review_tasks").fetchone()[0] == 5


def test_v50_migration_is_idempotent(tmp_path):
    from database.db_extended import _migrate_v50
    conn = sqlite3.connect(tmp_path / "migration.sqlite")
    conn.executescript("CREATE TABLE channels(id INTEGER PRIMARY KEY); CREATE TABLE videos(id INTEGER PRIMARY KEY);")
    _migrate_v50(conn, __import__("logging").getLogger("test"))
    _migrate_v50(conn, __import__("logging").getLogger("test"))
    cols = {row[1] for row in conn.execute("PRAGMA table_info(videos)")}
    assert {"yt_visibility", "yt_checked_at", "yt_checked_source", "actual_published_at"} <= cols
    assert conn.execute("SELECT COUNT(*) FROM video_review_tasks").fetchone()[0] == 0


def test_duplicate_preview_requires_complete_exact_evidence(tmp_path):
    db = DB(str(tmp_path / "db.sqlite"))
    thumb = tmp_path / "thumb.jpg"
    thumb.write_bytes(b"same-thumbnail")
    with db._connect() as c:
        c.executescript("""
        CREATE TABLE videos(id INTEGER PRIMARY KEY, channel_id INTEGER, yt_video_id TEXT,
          titulo_final TEXT, description TEXT, tags_json TEXT, thumbnail_path TEXT,
          privacy_status TEXT, status TEXT);
        """)
        row = (1, 5, "a", "Exact title", "Exact description", json.dumps(["tag"]), str(thumb), "public", "published")
        c.execute("INSERT INTO videos VALUES (?,?,?,?,?,?,?,?,?)", row)
        c.execute("INSERT INTO videos VALUES (?,?,?,?,?,?,?,?,?)", (2, *row[1:]))
        c.commit()
    groups = find_exact_duplicate_groups(db, 5)
    assert len(groups) == 1
    assert {v["id"] for v in groups[0]} == {1, 2}


def test_editorial_guard_is_opt_in_and_activation_scoped(tmp_path):
    db = DB(str(tmp_path / "db.sqlite"))
    with db._connect() as c:
        c.execute("CREATE TABLE videos(id INTEGER PRIMARY KEY, channel_id INTEGER, titulo_final TEXT, status TEXT)")
        c.commit()
    cfg = SimpleNamespace(REVIEW_GOVERNANCE_NICHE_GUARD_ENABLED=True,
                          REVIEW_GOVERNANCE_ENABLED=True,
                          REVIEW_GOVERNANCE_NICHE_KEYWORDS=["síndrome"])
    db.get_channel_by_slug = lambda slug: {"id": 5, "slug": slug}
    assert not validate_new_content({"titulo": "Otro tema", "guion": "texto"}, cfg, db, "canal5").allowed
