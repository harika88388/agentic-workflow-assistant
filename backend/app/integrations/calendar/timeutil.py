"""Stdlib-only time zone helpers. Naive datetimes are never sent to Google; they are localized first."""
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

UTC = timezone.utc


class NonexistentTimeError(ValueError):
    """Wall-clock time that does not exist (skipped by a DST jump)."""


def get_zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, KeyError) as exc:
        raise ValueError(f"unknown timezone: {name!r}") from exc


def localize(dt: datetime, tz: ZoneInfo) -> datetime:
    if dt.tzinfo is not None:
        return dt
    aware = dt.replace(tzinfo=tz)
    if aware.astimezone(UTC).astimezone(tz).replace(tzinfo=None) != dt:
        raise NonexistentTimeError(f"{dt.isoformat()} does not exist in {tz.key} (daylight-saving gap)")
    return aware


def to_rfc3339(dt: datetime) -> str:
    if dt.tzinfo is None:
        raise ValueError("datetime must be timezone-aware")
    return dt.isoformat()


def parse_iso(value: str, tz: ZoneInfo | None = None) -> datetime | date:
    """Date-only strings (all-day events) -> date; otherwise an aware datetime."""
    if len(value) == 10:
        return date.fromisoformat(value)
    v = value[:-1] + "+00:00" if value.endswith("Z") else value
    dt = datetime.fromisoformat(v)
    return localize(dt, tz or ZoneInfo("UTC")) if dt.tzinfo is None else dt


def same_instant(a: datetime, b: datetime) -> bool:
    return a.astimezone(UTC) == b.astimezone(UTC)


def day_bounds(d: date, tz: ZoneInfo) -> tuple[datetime, datetime]:
    start = datetime.combine(d, time.min, tzinfo=tz)
    return start, datetime.combine(d + timedelta(days=1), time.min, tzinfo=tz)


def merge_intervals(intervals: list[tuple[datetime, datetime]]) -> list[tuple[datetime, datetime]]:
    out: list[tuple[datetime, datetime]] = []
    for s, e in sorted(intervals):
        if out and s <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], e))
        else:
            out.append((s, e))
    return out


def compute_free_slots(
    busy: list[tuple[datetime, datetime]], start: datetime, end: datetime, min_minutes: int = 15
) -> list[tuple[datetime, datetime]]:
    clipped = [(max(s, start), min(e, end)) for s, e in busy if e > start and s < end]
    free, cursor = [], start
    for s, e in merge_intervals(clipped):
        if s - cursor >= timedelta(minutes=min_minutes):
            free.append((cursor, s))
        cursor = max(cursor, e)
    if end - cursor >= timedelta(minutes=min_minutes):
        free.append((cursor, end))
    return free
