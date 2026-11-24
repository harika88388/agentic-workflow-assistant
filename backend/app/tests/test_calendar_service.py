from datetime import datetime, timedelta

import pytest

from app.integrations.calendar.models import parse_event
from app.integrations.calendar.service import (
    CalendarError, CalendarService, CalendarUnsupportedError, CalendarVerificationError, event_id_from_key,
)
from app.integrations.calendar.timeutil import get_zone
from app.integrations.errors import GoogleConflictError, GoogleNotFoundError

TZ = get_zone("Asia/Kolkata")
NOW = datetime(2026, 11, 24, 12, 0, tzinfo=TZ)


class FakeCal:
    def __init__(self):
        self.events, self.n = {}, 0
        self.ignore_update = self.ignore_delete = False
        self.hide_deleted = True
        self.last_body = None
        self.busy = []

    def add(self, id, summary, start, end, **extra):
        raw = {"id": id, "summary": summary, "start": {"dateTime": start}, "end": {"dateTime": end}, **extra}
        self.events[id] = raw
        return raw

    def list_events(self, time_min, time_max=None, q=None, max_results=100):
        return [parse_event(e) for e in self.events.values()]

    def get_event(self, id):
        if id not in self.events:
            raise GoogleNotFoundError("nf", 404)
        return parse_event(self.events[id])

    def create_event(self, body, send_updates="none"):
        self.last_body = body
        eid = body.get("id") or f"ev{self.n}"
        self.n += 1
        if eid in self.events:
            raise GoogleConflictError("dup", 409)
        self.events[eid] = {**body, "id": eid, "status": "confirmed"}
        return parse_event(self.events[eid])

    def update_event(self, id, patch, send_updates="none"):
        if not self.ignore_update:
            self.events[id].update(patch)
        return parse_event(self.events[id])

    def delete_event(self, id, send_updates="none"):
        if self.ignore_delete:
            return
        if self.hide_deleted:
            del self.events[id]
        else:
            self.events[id]["status"] = "cancelled"

    def freebusy(self, start, end, tz_name):
        return self.busy


def svc(cal):
    return CalendarService(cal, TZ, now=lambda: NOW)


def test_create_localizes_naive_start_and_verifies():
    cal = FakeCal()
    r = svc(cal).create_event(title="Sync", start=datetime(2026, 11, 25, 15, 0), attendees=["rahul@example.com"])
    assert cal.last_body["start"] == {"dateTime": "2026-11-25T15:00:00+05:30", "timeZone": "Asia/Kolkata"}
    assert cal.last_body["end"]["dateTime"] == "2026-11-25T16:00:00+05:30"
    assert r.verified and not r.duplicate and r.event.attendees[0].email == "rahul@example.com"


def test_idempotency_key_prevents_duplicate_events():
    cal, s = FakeCal(), None
    s = svc(cal)
    a = s.create_event(title="Sync", start=datetime(2026, 11, 25, 15, 0), idempotency_key="exec-1")
    b = s.create_event(title="Sync", start=datetime(2026, 11, 25, 15, 0), idempotency_key="exec-1")
    assert not a.duplicate and b.duplicate and len(cal.events) == 1
    assert a.event.id == b.event.id == event_id_from_key("exec-1")


def test_create_validation():
    s = svc(FakeCal())
    t = datetime(2026, 11, 25, 15, 0)
    with pytest.raises(ValueError):
        s.create_event(title=" ", start=t)
    with pytest.raises(ValueError):
        s.create_event(title="x", start=t, end=t - timedelta(hours=1))
    with pytest.raises(ValueError):
        s.create_event(title="x", start=t, attendees=["Rahul"])


def test_create_not_confirmed_raises_instead_of_claiming_success():
    class Lying(FakeCal):
        def get_event(self, id):
            ev = super().get_event(id)
            ev.start = "2030-01-01T00:00:00+05:30"
            return ev
    with pytest.raises(CalendarVerificationError):
        svc(Lying()).create_event(title="Sync", start=datetime(2026, 11, 25, 15, 0))


def test_reschedule_keeps_duration_and_verifies():
    cal = FakeCal()
    cal.add("e1", "Sync", "2026-11-25T10:00:00+05:30", "2026-11-25T10:45:00+05:30")
    r = svc(cal).reschedule_event("e1", datetime(2026, 11, 27, 15, 0))
    assert r.event.start == "2026-11-27T15:00:00+05:30" and r.event.end == "2026-11-27T15:45:00+05:30"
    assert r.previous_start == "2026-11-25T10:00:00+05:30" and r.verified


def test_reschedule_not_applied_is_reported_as_failure():
    cal = FakeCal()
    cal.add("e1", "Sync", "2026-11-25T10:00:00+05:30", "2026-11-25T11:00:00+05:30")
    cal.ignore_update = True
    with pytest.raises(CalendarVerificationError):
        svc(cal).reschedule_event("e1", datetime(2026, 11, 27, 15, 0))


def test_reschedule_rejects_all_day_cancelled_and_missing():
    cal = FakeCal()
    cal.events["h"] = {"id": "h", "summary": "Holiday", "start": {"date": "2026-11-25"}, "end": {"date": "2026-11-26"}}
    cal.add("c", "Old", "2026-11-25T10:00:00+05:30", "2026-11-25T11:00:00+05:30", status="cancelled")
    s = svc(cal)
    with pytest.raises(CalendarUnsupportedError):
        s.reschedule_event("h", datetime(2026, 11, 27, 15, 0))
    with pytest.raises(CalendarError):
        s.reschedule_event("c", datetime(2026, 11, 27, 15, 0))
    with pytest.raises(GoogleNotFoundError):
        s.reschedule_event("zzz", datetime(2026, 11, 27, 15, 0))


def test_cancel_verified_both_ways_and_failure_detected():
    for hide in (True, False):
        cal = FakeCal()
        cal.hide_deleted = hide
        cal.add("e1", "Sync", "2026-11-25T10:00:00+05:30", "2026-11-25T11:00:00+05:30")
        r = svc(cal).cancel_event("e1")
        assert r.verified and r.event.summary == "Sync"
    cal = FakeCal()
    cal.add("e1", "Sync", "2026-11-25T10:00:00+05:30", "2026-11-25T11:00:00+05:30")
    cal.ignore_delete = True
    with pytest.raises(CalendarVerificationError):
        svc(cal).cancel_event("e1")


def test_find_event_resolves_or_reports_ambiguity():
    cal = FakeCal()
    att = {"attendees": [{"email": "rahul@x.com", "displayName": "Rahul"}]}
    cal.add("a", "Sync", "2026-11-25T10:00:00+05:30", "2026-11-25T11:00:00+05:30", **att)
    cal.add("b", "Standup", "2026-11-26T10:00:00+05:30", "2026-11-26T11:00:00+05:30", **att)
    s = svc(cal)
    assert s.find_event(person="Rahul").status == "ambiguous"
    assert s.find_event(person="Rahul", title="Sync").event.id == "a"
    assert s.find_event(person="Nobody").status == "none"


def test_availability_free_and_busy():
    cal = FakeCal()
    s = svc(cal)
    start, end = datetime(2026, 11, 25, 9, 0), datetime(2026, 11, 25, 12, 0)
    assert s.check_availability(start, end).is_free
    cal.busy = [(datetime(2026, 11, 25, 5, 0, tzinfo=get_zone("UTC")), datetime(2026, 11, 25, 5, 30, tzinfo=get_zone("UTC")))]  # 10:30-11:00 IST
    a = s.check_availability(start, end)
    assert not a.is_free and len(a.free_slots) == 2 and a.busy[0][0].startswith("2026-11-25T10:30:00+05:30")
    with pytest.raises(ValueError):
        s.check_availability(end, start)


def test_next_event_skips_all_day():
    cal = FakeCal()
    cal.events["h"] = {"id": "h", "summary": "Holiday", "start": {"date": "2026-11-25"}, "end": {"date": "2026-11-26"}}
    assert svc(cal).next_event() is None
    cal.add("e", "Call", "2026-11-25T10:00:00+05:30", "2026-11-25T11:00:00+05:30")
    assert svc(cal).next_event().id == "e"
