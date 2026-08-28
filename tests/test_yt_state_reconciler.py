"""Unit tests for the shorts real-publication reconciler (v48).

Covers:
- classify_video_visibility: yt-dlp classification (public/private/age/removed)
- _migrate_v48: adds derived columns idempotently
- reconcile_recent_shorts: updates yt_visibility, sets published_at when public,
  and raises a 'short_publish_stuck' alert for private shorts past publish_at.
"""

import logging
from datetime import datetime, timedelta, timezone

import pytest
import yt_dlp

from api.services import yt_state_reconciler as rec
from database.db_extended import _migrate_v48


# ── classify_video_visibility ───────────────────────────────────
class _FakeYDL:
    def __init__(self, result=None, error_msg=""):
        self._result = result
        self._error_msg = error_msg

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def extract_info(self, *a, **k):
        if self._error_msg:
            raise Exception(self._error_msg)
        return self._result


@pytest.mark.parametrize("err,expected", [
    ("", "public"),
    ("ERROR: Private video. Sign in if you've been granted access", "private"),
    ("ERROR: Sign in to confirm your age. This video may be inappropriate", "age_restricted"),
    ("ERROR: Video unavailable. This video isn't available anymore", "removed"),
    ("ERROR: This video has been removed for violating YouTube's Terms", "removed"),
    ("ERROR: some weird network thing", "error"),
])
def test_classify(monkeypatch, err, expected):
    if err:
        monkeypatch.setattr(yt_dlp, "YoutubeDL", lambda *a, **k: _FakeYDL(error_msg=err))
    else:
        monkeypatch.setattr(yt_dlp, "YoutubeDL", lambda *a, **k: _FakeYDL(result={"title": "x"}))
    assert rec.classify_video_visibility("ABC123") == expected


# ── migration ───────────────────────────────────────────────────
def test_migrate_v48_idempotent():
    import sqlite3
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE shorts (id INTEGER PRIMARY KEY, status TEXT)")
    _migrate_v48(conn, logging.getLogger("test"))
    cols = {r[1] for r in conn.execute("PRAGMA table_info('shorts')")}
    for c in ("publish_at", "yt_visibility", "yt_checked_at", "yt_checked_source"):
        assert c in cols, f"missing column {c}"
    _migrate_v48(conn, logging.getLogger("test"))  # idempotent
    conn.close()


# ── reconciler with a fake DB ───────────────────────────────────
class FakeDB:
    def __init__(self, shorts, channels, feed_map=None):
        self.shorts = shorts
        self.channels = channels
        self.feed_map = feed_map or {}
        self.writes = []

    def _connect(self):
        return _Ctx(self)

    def _execute_write(self, sql, params):
        self.writes.append((sql, params))

    def set_system_state(self, key, value):
        pass


class _Ctx:
    def __init__(self, fake):
        self.fake = fake

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=()):
        low = sql.lower()
        if "from shorts" in low:
            return _Rows(self.fake.shorts)
        if "from channels" in low:
            if params and params[0] is not None:
                return _Rows([c for c in self.fake.channels if c.get("id") == params[0]])
            return _Rows(self.fake.channels)
        return _Rows([])


class _Rows:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None


def _short(short_id, channel_id, yt_id, visibility="", publish_at=None, published_at=None, checked_at=None):
    return {
        "id": short_id, "channel_id": channel_id, "youtube_id": yt_id,
        "status": "published", "publish_at": publish_at, "published_at": published_at,
        "yt_visibility": visibility, "yt_checked_at": checked_at,
    }


def test_reconcile_public_sets_published_at(monkeypatch):
    now = datetime.now(timezone.utc)
    shorts = [_short(1, 5, "AAA", visibility="scheduled", publish_at=now.isoformat())]
    db = FakeDB(shorts=shorts, channels=[{"id": 5, "yt_channel_id": "UCx"}], feed_map={5: {"AAA": now.isoformat()}})
    monkeypatch.setattr(rec, "classify_video_visibility", lambda yt: "public")
    monkeypatch.setattr(rec, "_feed_public_ids", lambda db_, cid: {"AAA": now.isoformat()})
    summary = rec.reconcile_recent_shorts(db)
    assert summary["public"] == 1
    any_write = any("yt_visibility" in w[0] and w[1][0] == "public" for w in db.writes)
    assert any_write, "expected an update writing yt_visibility='public'"


def test_reconcile_stuck_private_alerts(monkeypatch):
    past = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    shorts = [_short(2, 5, "BBB", visibility="scheduled", publish_at=past)]
    db = FakeDB(shorts=shorts, channels=[{"id": 5, "yt_channel_id": "UCx", "slug": "canal4"}], feed_map={})
    monkeypatch.setattr(rec, "classify_video_visibility", lambda yt: "private")
    monkeypatch.setattr(rec, "_feed_public_ids", lambda db_, cid: {})

    captured = {}
    def fake_alert(db_, **kwargs):
        captured.update(kwargs)
        return 1
    monkeypatch.setattr("api.services.lifecycle_monitor.create_alert", fake_alert)

    summary = rec.reconcile_recent_shorts(db)
    assert summary["stuck"] == 1
    assert captured.get("alert_type") == rec.ALERT_TYPE_STUCK


def test_reconcile_skips_recently_checked(monkeypatch):
    now = datetime.now(timezone.utc).isoformat()
    shorts = [_short(3, 5, "CCC", visibility="private", checked_at=now)]
    db = FakeDB(shorts=shorts, channels=[{"id": 5, "yt_channel_id": "UCx"}], feed_map={})
    monkeypatch.setattr(rec, "classify_video_visibility", lambda yt: "public")
    summary = rec.reconcile_recent_shorts(db)
    assert summary["checked"] == 0  # cooldown respected
