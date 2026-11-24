from datetime import date, time

from app.integrations.calendar import matching as m
from app.integrations.calendar.models import Attendee, CalendarEvent
from app.integrations.calendar.timeutil import get_zone

TZ = get_zone("Asia/Kolkata")


def ev(id, summary, start, end="2026-11-25T11:00:00+05:30", attendees=(), status="confirmed", all_day=False):
    return CalendarEvent(id=id, summary=summary, start=start, end=end, status=status, all_day=all_day,
                         attendees=[Attendee(*a) for a in attendees])


E1 = ev("1", "Sync with Rahul", "2026-11-25T10:00:00+05:30", attendees=[("rahul@x.com", "Rahul Sharma", "accepted")])
E2 = ev("2", "Budget review", "2026-11-25T14:00:00+05:30", attendees=[("meera@x.com", "Meera", "accepted")])
E3 = ev("3", "Standup", "2026-11-26T09:00:00+05:30", attendees=[("rahul@x.com", "Rahul Sharma", "accepted")])


def test_unique_match_by_person():
    r = m.resolve(m.find_candidates([E1, E2], TZ, person="Rahul"))
    assert r.status == "unique" and r.event.id == "1"


def test_person_matches_attendee_email_localpart_and_full_name():
    assert m.resolve(m.find_candidates([E1, E2], TZ, person="Rahul Sharma")).event.id == "1"
    only_email = ev("9", "1:1", "2026-11-25T10:00:00+05:30", attendees=[("priya.nair@x.com", "", "")])
    assert m.resolve(m.find_candidates([only_email, E2], TZ, person="Priya")).event.id == "9"


def test_same_person_two_events_is_ambiguous_until_date_given():
    r = m.resolve(m.find_candidates([E1, E3], TZ, person="Rahul"))
    assert r.status == "ambiguous" and len(r.candidates) == 2
    r2 = m.resolve(m.find_candidates([E1, E3], TZ, person="Rahul", on_date=date(2026, 11, 26)))
    assert r2.status == "unique" and r2.event.id == "3"


def test_none_when_nobody_matches_and_cancelled_excluded():
    assert m.resolve(m.find_candidates([E1, E2], TZ, person="Zed")).status == "none"
    cancelled = ev("4", "Sync with Rahul", "2026-11-25T10:00:00+05:30", status="cancelled")
    assert m.resolve(m.find_candidates([cancelled], TZ, title="Sync")).status == "none"


def test_title_and_time_scoring():
    a = ev("a", "Design review", "2026-11-25T10:00:00+05:30")
    b = ev("b", "Design review", "2026-11-25T15:00:00+05:30")
    r = m.resolve(m.find_candidates([a, b], TZ, title="design review", at_time=time(15, 0)))
    assert r.status == "unique" and r.event.id == "b"


def test_date_filter_uses_user_timezone_not_utc():
    late = ev("l", "Late call", "2026-11-25T23:30:00+05:30")  # 18:00 UTC same day, but test a boundary
    early = ev("e", "Early call", "2026-11-26T00:30:00+05:30")  # still 25 Nov in UTC, 26 Nov in IST
    assert [c.event.id for c in m.find_candidates([late, early], TZ, title="call", on_date=date(2026, 11, 26))] == ["e"]


def test_all_day_events_match_by_date():
    hol = ev("h", "Diwali holiday", "2026-11-25", "2026-11-26", all_day=True)
    assert m.resolve(m.find_candidates([hol, E2], TZ, title="holiday", on_date=date(2026, 11, 25))).event.id == "h"
