"""Canonical timestamp handling for API, schedulers and SQLite."""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

UTC = timezone.utc
MADRID = ZoneInfo("Europe/Madrid")


def parse_timestamp(value: str | datetime | None, *, naive_tz: str = "UTC") -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        raw = str(value).strip().replace(" ", "T", 1)
        if raw.endswith(("Z", "z")):
            raw = raw[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(raw)
        except ValueError:
            try:
                dt = datetime.strptime(str(value).strip(), "%Y-%m-%d %H:%M:%S")
            except ValueError:
                return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ZoneInfo(naive_tz))
    return dt.astimezone(UTC)


def parse_utc(value: str | datetime | None) -> datetime | None:
    """Parse an API/SQLite value; naive values are UTC."""
    return parse_timestamp(value, naive_tz="UTC")


def local_to_utc(value: str | datetime, timezone_name: str = "Europe/Madrid") -> datetime:
    tz = ZoneInfo(timezone_name)
    if isinstance(value, datetime):
        dt = value
    else:
        dt = datetime.fromisoformat(str(value).strip().replace(" ", "T", 1))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz)
    return dt.astimezone(UTC)


def sqlite_utc(value: str | datetime) -> str:
    """Serialize an instant as UTC-naive SQLite text."""
    dt = parse_utc(value)
    if dt is None:
        raise ValueError(f"Invalid timestamp: {value!r}")
    return dt.replace(tzinfo=None).strftime("%Y-%m-%d %H:%M:%S")


def youtube_rfc3339(value: str | datetime) -> str:
    dt = parse_utc(value)
    if dt is None:
        raise ValueError(f"Invalid timestamp: {value!r}")
    return dt.isoformat(timespec="seconds").replace("+00:00", "Z")


def madrid_day_range(day: date | str | None = None) -> tuple[str, str]:
    """UTC-naive SQLite bounds for a Madrid calendar day."""
    if day is None:
        day = datetime.now(UTC).astimezone(MADRID).date()
    elif isinstance(day, str):
        day = date.fromisoformat(day)
    start_local = datetime.combine(day, time.min, tzinfo=MADRID)
    # Build the next wall-clock midnight independently. Adding 24 hours to an
    # aware datetime is not a calendar-day operation across DST transitions.
    next_day = day + timedelta(days=1)
    end_local = datetime.combine(next_day, time.min, tzinfo=MADRID)
    return sqlite_utc(start_local), sqlite_utc(end_local)


def madrid_date(value: str | datetime | None) -> date | None:
    dt = parse_utc(value)
    return dt.astimezone(MADRID).date() if dt else None
