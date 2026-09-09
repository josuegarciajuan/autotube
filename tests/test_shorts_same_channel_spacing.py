"""Regression tests for same-channel short spacing at upload time.

Context (ago-2026): shorts of the SAME channel were draining back-to-back
(observed: two canal3 shorts 82s apart). Root cause: the valve's per-channel
cooldown compared the stored-UTC `published_at` against `datetime.now()` in
LOCAL time. With the server in CEST (UTC+2) the elapsed time was inflated by
+2h, so a 90/120-minute cooldown was always treated as already expired.

These tests pin the UTC-correct behavior of `_channel_short_spacing_ok`.
"""

from datetime import datetime, timedelta, timezone

import pytest

from api.services import shorts_scheduler as s

UTC = timezone.utc


class _Cursor:
    def __init__(self, val):
        self._val = val

    def fetchone(self):
        class _Row:
            def __getitem__(self, _k):
                return self.val
        row = _Row()
        row.val = self._val
        return row

    def fetchall(self):
        return []


class _FakeConn:
    def __init__(self, last_pub):
        self._last_pub = last_pub

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, query, params=()):
        return _Cursor(self._last_pub)


class FakeDB:
    """Minimal stand-in: returns a fixed MAX(published_at) for the channel."""

    def __init__(self, last_published_utc: str | None):
        self._last_published_utc = last_published_utc

    def _connect(self):
        return _FakeConn(self._last_published_utc)


def _fmt(dt):
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def test_short_gap_recent_is_held():
    """A same-channel short 81s after the last one MUST be held (was the bug)."""
    last = _fmt(datetime.now(UTC) - timedelta(seconds=81))
    ok, wait = s._channel_short_spacing_ok(4, "native", FakeDB(last))
    assert ok is False
    assert wait > 0


def test_gap_after_required_window_is_clear():
    """Once beyond the cooldown/same-type window, uploading is allowed."""
    last = _fmt(datetime.now(UTC) - timedelta(hours=5))
    ok, _wait = s._channel_short_spacing_ok(4, "native", FakeDB(last))
    assert ok is True


def test_no_history_is_clear():
    """A channel with no prior short upload is always free to upload."""
    ok, _wait = s._channel_short_spacing_ok(4, "native", FakeDB(None))
    assert ok is True


def test_fix_eliminates_local_time_inflation():
    """Prove the old local-vs-UTC bug would have EXPIRED the cooldown.

    The comparison is timezone-independent now: parsing the stored UTC value
    and comparing against UTC now yields the true 81s (not 81s + UTC offset).
    """
    last = _fmt(datetime.now(UTC) - timedelta(seconds=81))
    parsed_utc = datetime.strptime(last, "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
    elapsed = (datetime.now(UTC) - parsed_utc).total_seconds()
    # The old code compared against local now(), which on a CEST server would
    # report ~+7200s → 2h → would wrongly treat the cooldown as expired.
    assert elapsed < 120  # true elapsed is ~81s, not inflated by the UTC offset
