"""Tests for batch-upload collapse normalization in upload_scheduler.

Covers:
  - _next_free_slot: picks a window slot, >= BATCH_MIN_GAP_MIN from used times,
    and before target_public_at - warmup.
  - _normalize_collapsed_batch_uploads: identical scheduled_upload_at collisions
    get re-staggered; channels without collisions are left untouched.

Run: python3 -m pytest tests/test_upload_batch_stagger.py -v
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from datetime import datetime, timedelta, timezone

from api.services import upload_scheduler as us


# ── _next_free_slot (pure, no DB) ───────────────────────────────

class TestNextFreeSlot:
    def test_returns_window_slot_before_deadline(self):
        windows = [{"start": 10, "end": 13}]  # Madrid 10-13 = UTC 08-11
        original = datetime(2026, 9, 10, 8, 0, 0, tzinfo=timezone.utc)  # 10:00 Madrid
        target = "2026-09-12T00:00:00+00:00"
        res = us._next_free_slot(
            original, target, windows, warmup_min=60,
            used_times=[original], now=datetime(2026, 9, 9, tzinfo=timezone.utc),
        )
        assert res is not None
        # dentro de la ventana (UTC 08-11) y separado >= 45 min del usado
        assert 8 <= res.hour < 11, res
        gap = (res - original).total_seconds() / 60
        assert gap >= us.BATCH_MIN_GAP_MIN - 1, gap
        # antes del deadline (publish - warmup)
        deadline = datetime(2026, 9, 12, 0, 0, tzinfo=timezone.utc) - timedelta(minutes=60)
        assert res < deadline

    def test_none_when_deadline_passed(self):
        windows = [{"start": 10, "end": 13}]
        original = datetime(2026, 9, 10, 8, 0, 0, tzinfo=timezone.utc)
        target = "2026-09-10T09:00:00+00:00"  # deadline ~08:00, ya pasado
        res = us._next_free_slot(
            original, target, windows, warmup_min=60,
            used_times=[], now=datetime(2026, 9, 9, tzinfo=timezone.utc),
        )
        # solo hay hueco tras el deadline -> None
        assert res is None or res < datetime(2026, 9, 10, 8, 0, 0, tzinfo=timezone.utc)


# ── _normalize_collapsed_batch_uploads (stub DB) ─────────────────

class _StubConn:
    def __init__(self, rows):
        self._rows = rows
    def execute(self, sql, params=None):
        return _StubResult(self._rows)


class _StubResult:
    def __init__(self, rows):
        self.rows = rows
    def fetchall(self):
        return list(self.rows)


class _StubDb:
    def __init__(self, rows):
        self.rows = rows
        self.updated = []
    def _connect(self):
        return _Ctx(self.rows)
    def update_video(self, video_id, **kw):
        self.updated.append((video_id, kw))


class _Ctx:
    def __init__(self, rows):
        self.rows = rows
    def __enter__(self):
        return _StubConn(self.rows)
    def __exit__(self, *a):
        return False


def _row(vid, ch, sched, target, windows_json):
    return {
        "id": vid,
        "channel_id": ch,
        "scheduled_upload_at": sched,
        "target_public_at": target,
        "config_json": windows_json,
    }


CFG = '{"UPLOAD_WINDOWS": [{"start": 10, "end": 13}], "PUBLISH_WARMUP_MIN": 60}'


class TestNormalizeCollapsed:
    def test_no_collision_does_nothing(self):
        rows = [
            _row(101, 4, "2026-09-10 08:00:00", "2026-09-12T00:00:00+00:00", CFG),
            _row(102, 4, "2026-09-10 09:15:00", "2026-09-12T00:00:00+00:00", CFG),
            _row(103, 5, "2026-09-10 08:00:00", "2026-09-12T00:00:00+00:00", CFG),
        ]
        db = _StubDb(rows)
        changed = us._normalize_collapsed_batch_uploads(db)
        assert changed == 0
        assert db.updated == []

    def test_identical_time_collision_gets_staggered(self):
        rows = [
            _row(101, 4, "2026-09-10 08:00:00", "2026-09-12T00:00:00+00:00", CFG),
            _row(102, 4, "2026-09-10 08:00:00", "2026-09-12T00:00:00+00:00", CFG),
        ]
        db = _StubDb(rows)
        changed = us._normalize_collapsed_batch_uploads(db)
        assert changed == 1
        # solo se movió el segundo (mayor id); el primero queda a su hora
        moved = dict(db.updated)
        assert 101 not in moved
        new_t = moved[102]["scheduled_upload_at"]
        assert new_t != "2026-09-10 08:00:00"
        new_dt = datetime.strptime(new_t[:19], "%Y-%m-%d %H:%M:%S")
        assert 8 <= new_dt.hour < 11  # dentro de la ventana (UTC)
