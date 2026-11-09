"""Regression: long-form replan must never reuse a `running` slot target.

The unique index ``uq_active_planned_public_target`` covers pending+running
rows. On 2026-11-09 canal4's replan assigned a pending slot the same
``target_public_at`` as a running slot and aborted the whole replan.
"""

from datetime import datetime, timezone


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def test_target_shifted_past_running_slot():
    from api.services.optimal_slots_calculator import resolve_noncolliding_target

    base = datetime(2026, 11, 12, 8, 0, tzinfo=timezone.utc)
    hard = {_iso(base)}

    when, iso, shift = resolve_noncolliding_target(base, hard, set())

    assert shift == 1
    assert iso == _iso(datetime(2026, 11, 12, 8, 3, tzinfo=timezone.utc))
    assert iso not in hard
    assert when == datetime(2026, 11, 12, 8, 3, tzinfo=timezone.utc)


def test_target_shifted_past_already_assigned_targets():
    from api.services.optimal_slots_calculator import resolve_noncolliding_target

    base = datetime(2026, 11, 12, 8, 0, tzinfo=timezone.utc)
    used = {_iso(base)}

    _when, iso, shift = resolve_noncolliding_target(base, set(), used)

    assert shift == 1
    assert iso == _iso(datetime(2026, 11, 12, 8, 3, tzinfo=timezone.utc))


def test_target_skips_chain_of_collisions():
    from api.services.optimal_slots_calculator import resolve_noncolliding_target

    base = datetime(2026, 11, 12, 8, 0, tzinfo=timezone.utc)
    hard = {
        _iso(datetime(2026, 11, 12, 8, 0, tzinfo=timezone.utc)),
        _iso(datetime(2026, 11, 12, 8, 3, tzinfo=timezone.utc)),
    }
    used = {_iso(datetime(2026, 11, 12, 8, 6, tzinfo=timezone.utc))}

    _when, iso, shift = resolve_noncolliding_target(base, hard, used)

    assert shift == 3
    assert iso == _iso(datetime(2026, 11, 12, 8, 9, tzinfo=timezone.utc))


def test_no_shift_when_target_is_free():
    from api.services.optimal_slots_calculator import resolve_noncolliding_target

    base = datetime(2026, 11, 12, 8, 0, tzinfo=timezone.utc)
    when, iso, shift = resolve_noncolliding_target(base, set(), set())

    assert shift == 0
    assert when == base
    assert iso == _iso(base)
