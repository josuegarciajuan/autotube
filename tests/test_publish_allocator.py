"""Tests del asignador único de publicaciones (Fase 1)."""

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from api.services.publish_allocator import (
    Franja,
    allocate_publish_times,
    select_daily_franjas,
)

TZ = ZoneInfo("Europe/Madrid")


def _dt(day: str, hour: int, minute: int = 0) -> datetime:
    y, m, d = (int(x) for x in day.split("-"))
    return datetime(y, m, d, hour, minute, tzinfo=TZ)


def test_select_daily_franjas_respects_rank_not_clock_order():
    franjas = [
        Franja(19, 0, rank=1),
        Franja(13, 0, rank=2),
        Franja(3, 0, rank=3),
    ]
    selected = select_daily_franjas(franjas, 2)
    assert [(f.hour, f.minute) for f in selected] == [(13, 0), (19, 0)]


def test_select_daily_franjas_dedupes_clock_time():
    franjas = [Franja(9, 30, rank=2), Franja(9, 30, rank=1)]
    selected = select_daily_franjas(franjas, 2)
    assert len(selected) == 1
    assert selected[0].rank == 1


def test_one_per_day_keeps_best_rank_across_days():
    franjas = [Franja(19, 0, rank=1), Franja(13, 0, rank=2)]
    times = allocate_publish_times(
        franjas,
        [(date(2026, 10, 18), 2)],
        tz=TZ,
        min_gap_minutes=24 * 60,
        daily_cap=1,
    )
    assert times == [_dt("2026-10-18", 19), _dt("2026-10-19", 19)]


def test_two_per_day_are_spread_within_the_day():
    franjas = [Franja(20, 0, rank=1), Franja(6, 0, rank=2)]
    times = allocate_publish_times(
        franjas,
        [(date(2026, 10, 18), 2)],
        tz=TZ,
        min_gap_minutes=6 * 60,
        daily_cap=2,
    )
    assert times == [_dt("2026-10-18", 6), _dt("2026-10-18", 20)]


def test_daily_cap_carries_overflow_to_next_day():
    franjas = [Franja(6, 0, rank=1), Franja(20, 0, rank=2)]
    times = allocate_publish_times(
        franjas,
        [(date(2026, 10, 18), 3)],
        tz=TZ,
        min_gap_minutes=0,
        daily_cap=2,
    )
    assert times == [
        _dt("2026-10-18", 6),
        _dt("2026-10-18", 20),
        _dt("2026-10-19", 6),
    ]


def test_gap_moves_to_next_day_instead_of_clamping_at_2359():
    franjas = [Franja(21, 0, rank=1), Franja(23, 30, rank=2)]
    times = allocate_publish_times(
        franjas,
        [(date(2026, 10, 18), 2)],
        tz=TZ,
        min_gap_minutes=24 * 60,
        daily_cap=2,
    )
    assert len(times) == 2
    assert times[1] - times[0] >= timedelta(hours=24)
    assert times[0].hour != 23 or times[0].minute < 30


def test_earliest_drops_past_candidates_and_carries():
    franjas = [Franja(6, 0, rank=1), Franja(20, 0, rank=2)]
    times = allocate_publish_times(
        franjas,
        [(date(2026, 10, 18), 2)],
        tz=TZ,
        earliest=_dt("2026-10-18", 10),
        min_gap_minutes=0,
        daily_cap=2,
    )
    assert times == [_dt("2026-10-18", 20), _dt("2026-10-19", 6)]
