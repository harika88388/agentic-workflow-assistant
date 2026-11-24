from datetime import date, datetime, timedelta, timezone

import pytest

from app.integrations.calendar import timeutil as t

IST = t.get_zone("Asia/Kolkata")
NY = t.get_zone("America/New_York")


def test_localize_naive_and_leave_aware_alone():
    dt = t.localize(datetime(2026, 11, 25, 15, 0), IST)
    assert dt.isoformat() == "2026-11-25T15:00:00+05:30"
    aware = datetime(2026, 1, 1, tzinfo=timezone.utc)
    assert t.localize(aware, IST) is aware


def test_dst_gap_rejected_and_dst_offsets_correct():
    with pytest.raises(t.NonexistentTimeError):
        t.localize(datetime(2026, 3, 8, 2, 30), NY)  # spring-forward gap
    assert t.localize(datetime(2026, 7, 1, 9, 0), NY).utcoffset() == timedelta(hours=-4)
    assert t.localize(datetime(2026, 12, 1, 9, 0), NY).utcoffset() == timedelta(hours=-5)


def test_unknown_timezone():
    for bad in ["Mars/Base", "", "../etc/passwd"]:
        with pytest.raises(ValueError):
            t.get_zone(bad)


def test_rfc3339_requires_aware():
    with pytest.raises(ValueError):
        t.to_rfc3339(datetime(2026, 1, 1))


def test_parse_iso_variants():
    assert t.parse_iso("2026-11-25") == date(2026, 11, 25)
    assert t.parse_iso("2026-11-25T10:00:00Z").utcoffset() == timedelta(0)
    assert t.parse_iso("2026-11-25T10:00:00+05:30").utcoffset() == timedelta(hours=5, minutes=30)
    assert t.parse_iso("2026-11-25T10:00:00", IST).utcoffset() == timedelta(hours=5, minutes=30)


def test_same_instant_across_zones():
    assert t.same_instant(datetime(2026, 1, 1, 10, tzinfo=timezone.utc), t.localize(datetime(2026, 1, 1, 15, 30), IST))


def test_day_bounds_dst_day_is_23_hours():
    s, e = t.day_bounds(date(2026, 3, 8), NY)
    assert e - s == timedelta(hours=23) or (e.astimezone(timezone.utc) - s.astimezone(timezone.utc)) == timedelta(hours=23)


def _d(h, m=0):
    return datetime(2026, 11, 25, h, m, tzinfo=timezone.utc)


def test_free_slots_merge_overlaps_and_clip():
    busy = [(_d(10), _d(11)), (_d(10, 30), _d(12)), (_d(7), _d(9, 30)), (_d(17), _d(19))]
    free = t.compute_free_slots(busy, _d(9), _d(18))
    assert free == [(_d(9, 30), _d(10)), (_d(12), _d(17))]


def test_free_slots_min_length_and_fully_busy_and_empty():
    assert t.compute_free_slots([(_d(9), _d(9, 50)), (_d(10), _d(12))], _d(9), _d(12)) == []  # 10-min gap < 15
    assert t.compute_free_slots([(_d(8), _d(13))], _d(9), _d(12)) == []
    assert t.compute_free_slots([], _d(9), _d(12)) == [(_d(9), _d(12))]
