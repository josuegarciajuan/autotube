"""Tests for the quota-free publish verification via channel-wall scraping.

Covers:
  - pipeline.youtube_wall_scraper.fetch_channel_public_video_ids (RSS parse)
  - database.db_extended._parse_utc_datetime
  - database.db_extended._verify_published_status_bg:
      * public → mark published
      * not public + within grace → schedule 20-min retry, no alert
      * not public + beyond grace → raise system alert + retry
      * missing channel_id → retry, no crash

Run: python3 -m pytest tests/test_publish_verify_scrape.py -v
"""

import sys
sys.path.insert(0, "/root/autotube")

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

# ── Wall scraper (RSS) ─────────────────────────────────────────

_SAMPLE_RSS = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns:yt="http://www.youtube.com/xml/schemas/2015"
      xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <yt:videoId>abc123</yt:videoId>
    <published>2026-08-15T00:14:21+00:00</published>
  </entry>
  <entry>
    <yt:videoId>def456</yt:videoId>
    <published>2026-08-14T21:14:21+00:00</published>
  </entry>
</feed>
"""


class _FakeResp:
    def __init__(self, content, status_code=200):
        self.content = content
        self.status_code = status_code


def test_fetch_channel_public_video_ids_parses_feed(monkeypatch):
    import pipeline.youtube_wall_scraper as scraper

    def fake_get(url, timeout, headers):
        return _FakeResp(_SAMPLE_RSS.encode("utf-8"))

    monkeypatch.setattr("pipeline.youtube_wall_scraper.requests.get", fake_get)
    ids = scraper.fetch_channel_public_video_ids("UCxxx")
    assert ids == {
        "abc123": "2026-08-15T00:14:21+00:00",
        "def456": "2026-08-14T21:14:21+00:00",
    }


def test_fetch_channel_public_video_ids_empty_channel():
    import pipeline.youtube_wall_scraper as scraper
    assert scraper.fetch_channel_public_video_ids("") == {}


def test_fetch_channel_public_video_ids_http_error(monkeypatch):
    import pipeline.youtube_wall_scraper as scraper

    def fake_get(url, timeout, headers):
        return _FakeResp(b"", status_code=500)

    monkeypatch.setattr("pipeline.youtube_wall_scraper.requests.get", fake_get)
    assert scraper.fetch_channel_public_video_ids("UCxxx") == {}


def test_is_video_public_true(monkeypatch):
    import pipeline.youtube_wall_scraper as scraper

    def fake_fetch(channel_id, timeout=30):
        return {"abc123": "2026-08-15T00:14:21+00:00"}

    monkeypatch.setattr(scraper, "fetch_channel_public_video_ids", fake_fetch)
    assert scraper.is_video_public("UCxxx", "abc123") is True
    assert scraper.is_video_public("UCxxx", "missing") is False


# ── _parse_utc_datetime ───────────────────────────────────────

def test_parse_utc_datetime():
    from database.db_extended import _parse_utc_datetime

    dt = _parse_utc_datetime("2026-08-15T00:14:00+00:00")
    assert dt is not None
    assert dt.tzinfo is not None
    assert dt.utcoffset().total_seconds() == 0

    assert _parse_utc_datetime("2026-08-15T00:14:00Z") is not None
    assert _parse_utc_datetime(None) is None
    assert _parse_utc_datetime("not-a-date") is None


# ── _verify_published_status_bg ───────────────────────────────

class _FakeVerifyDB:
    """Minimal DB stub for the verify function's direct calls."""
    def __init__(self, video=None, channel=None):
        self._video = video or {}
        self._channel = channel or {}
        self.updated = []

    def get_video(self, video_id):
        return self._video

    def get_channel_by_slug(self, slug):
        return self._channel

    def update_video(self, video_id, **kwargs):
        self.updated.append((video_id, kwargs))


def _patch_db(fake_db):
    return patch("database.db_extended.ExtendedDatabase", return_value=fake_db)


def test_verify_marks_published_when_in_feed():
    from database import db_extended as de

    fake_db = _FakeVerifyDB(
        video={"id": 1, "target_public_at": "2026-08-15T00:14:00+00:00"},
        channel={"yt_channel_id": "UCxxx"},
    )

    with _patch_db(fake_db), \
         patch("pipeline.youtube_wall_scraper.fetch_channel_public_video_ids",
               return_value={"vid123": "2026-08-15T00:14:21+00:00"}), \
         patch("database.db_extended._mark_video_published") as mark:
        de._verify_published_status_bg(1, "canal2", "vid123")

    mark.assert_called_once()
    assert mark.call_args[0][0] == 1
    assert mark.call_args[1].get("published_at") == "2026-08-15T00:14:21+00:00"


def test_verify_schedules_retry_within_grace():
    from database import db_extended as de

    now = datetime.now(timezone.utc)
    ref = (now - timedelta(minutes=10)).isoformat()

    fake_db = _FakeVerifyDB(
        video={"id": 1, "target_public_at": ref},
        channel={"yt_channel_id": "UCxxx"},
    )

    with _patch_db(fake_db), \
         patch("pipeline.youtube_wall_scraper.fetch_channel_public_video_ids",
               return_value={"other": "..."}), \
         patch("database.db_extended._get_retry_count", return_value=0), \
         patch("database.db_extended._schedule_publish_retry") as sched, \
         patch("database.db_extended._raise_publish_not_detected_alert") as alert:
        de._verify_published_status_bg(1, "canal2", "vid123")

    sched.assert_called_once_with(1, de._PUBLISH_RETRY_MINUTES)
    alert.assert_not_called()


def test_verify_raises_alert_beyond_grace():
    from database import db_extended as de

    now = datetime.now(timezone.utc)
    ref = (now - timedelta(minutes=300)).isoformat()  # 5h past

    fake_db = _FakeVerifyDB(
        video={"id": 1, "channel_id": 3, "titulo_final": "Mi video",
               "target_public_at": ref},
        channel={"yt_channel_id": "UCxxx"},
    )

    with _patch_db(fake_db), \
         patch("pipeline.youtube_wall_scraper.fetch_channel_public_video_ids",
               return_value={"other": "..."}), \
         patch("database.db_extended._get_retry_count", return_value=2), \
         patch("database.db_extended._schedule_publish_retry") as sched, \
         patch("database.db_extended._raise_publish_not_detected_alert") as alert:
        de._verify_published_status_bg(1, "canal2", "vid123")

    alert.assert_called_once()
    sched.assert_called_once_with(1, de._PUBLISH_RETRY_MINUTES)
    args = alert.call_args[0]
    # signature: (video_id, channel_slug, yt_video_id, channel_id, ref_str,
    #             elapsed_min, retry_count, feed_entries)
    assert args[0] == 1
    assert args[2] == "vid123"
    assert args[5] >= de._PUBLISH_GRACE_MINUTES


def test_verify_retries_when_no_channel_id():
    from database import db_extended as de

    fake_db = _FakeVerifyDB(
        video={"id": 1, "target_public_at": "2026-08-15T00:14:00+00:00"},
        channel={"yt_channel_id": None},
    )

    with _patch_db(fake_db), \
         patch("pipeline.youtube_wall_scraper.fetch_channel_public_video_ids") as fetch, \
         patch("database.db_extended._schedule_publish_retry") as sched:
        de._verify_published_status_bg(1, "canal2", "vid123")

    fetch.assert_not_called()
    sched.assert_called_once_with(1, de._PUBLISH_RETRY_MINUTES)
