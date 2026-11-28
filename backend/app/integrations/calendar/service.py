"""Calendar operations. Nothing is reported as done unless Google Calendar confirms it by re-reading."""
import hashlib
import logging
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from app.core.validation import is_valid_email
from app.integrations.calendar import matching
from app.integrations.calendar.client import CalendarClient
from app.integrations.calendar.models import CalendarEvent
from app.integrations.calendar.timeutil import (
    compute_free_slots, day_bounds, localize, parse_iso, same_instant, to_rfc3339,
)
from app.integrations.errors import GoogleConflictError, GoogleNotFoundError

log = logging.getLogger(__name__)

class CalendarError(Exception):
    pass


class CalendarVerificationError(CalendarError):
    """Google did not confirm the change."""


class CalendarUnsupportedError(CalendarError):
    pass


@dataclass
class CreateResult:
    event: CalendarEvent
    duplicate: bool  # True if this idempotency key had already created the event
    verified: bool


@dataclass
class RescheduleResult:
    event: CalendarEvent
    previous_start: str
    previous_end: str
    verified: bool


@dataclass
class CancelResult:
    event: CalendarEvent  # as it was before deletion
    verified: bool


@dataclass
class Availability:
    is_free: bool
    busy: list[tuple[str, str]]
    free_slots: list[tuple[str, str]]


def event_id_from_key(key: str) -> str:
    """Deterministic Google event id (hex is valid base32hex): duplicate creates return 409 instead of a 2nd event."""
    return hashlib.sha1(key.encode()).hexdigest()


class CalendarService:
    def __init__(self, client: CalendarClient, tz: ZoneInfo, now=None):
        self.client, self.tz = client, tz
        self._now = now or (lambda: datetime.now(tz))

    # --- read ---
    def list_events(self, days: int = 7, query: str | None = None, max_results: int = 50) -> list[CalendarEvent]:
        now = self._now()
        events = self.client.list_events(now, now + timedelta(days=days), q=query, max_results=max_results)
        log.info("calendar event search completed window_days=%d has_query=%s result_count=%d",
                 days, bool(query), len(events))
        return events

    def events_on(self, d: date) -> list[CalendarEvent]:
        s, e = day_bounds(d, self.tz)
        events = self.client.list_events(s, e)
        log.info("calendar event search completed on_date=%s result_count=%d", d.isoformat(), len(events))
        return events

    def next_event(self) -> CalendarEvent | None:
        for ev in self.client.list_events(self._now(), None, max_results=10):
            if ev.status != "cancelled" and not ev.all_day:
                return ev
        return None

    def check_availability(self, start: datetime, end: datetime, min_slot_minutes: int = 15) -> Availability:
        start, end = localize(start, self.tz), localize(end, self.tz)
        if end <= start:
            raise ValueError("end must be after start")
        busy = self.client.freebusy(start, end, self.tz.key)
        slots = compute_free_slots(busy, start, end, min_slot_minutes)
        fmt = lambda a, b: (a.astimezone(self.tz).isoformat(), b.astimezone(self.tz).isoformat())  # noqa: E731
        log.info("calendar availability checked window_minutes=%d is_free=%s busy_blocks=%d free_slots=%d",
                 round((end - start).total_seconds() / 60), not busy, len(busy), len(slots))
        return Availability(not busy, [fmt(s, e) for s, e in busy], [fmt(s, e) for s, e in slots])

    def find_event(self, *, person: str | None = None, title: str | None = None, on_date: date | None = None,
                   at_time: time | None = None, window_days: int = 30) -> matching.Resolution:
        """Locate the event the user means. Returns 'ambiguous' / 'none' instead of guessing."""
        if on_date:
            events = self.events_on(on_date)
        else:
            events = self.client.list_events(self._now(), self._now() + timedelta(days=window_days), max_results=250)
        return matching.resolve(matching.find_candidates(
            events, self.tz, person=person, title=title, on_date=on_date, at_time=at_time))

    # --- write ---
    def create_event(self, *, title: str, start: datetime, end: datetime | None = None, duration_minutes: int = 60,
                     attendees: list[str] | None = None, description: str | None = None, location: str | None = None,
                     idempotency_key: str | None = None, send_updates: str = "none") -> CreateResult:
        if not title or not title.strip():
            raise ValueError("title must not be empty")
        start = localize(start, self.tz)
        end = localize(end, self.tz) if end else start + timedelta(minutes=duration_minutes)
        if end <= start:
            raise ValueError("end must be after start")
        emails = list(attendees or [])
        bad = [a for a in emails if not is_valid_email(a)]
        if bad:
            raise ValueError(f"invalid attendee email(s): {bad}")
        body: dict = {"summary": title.strip(), "start": self._when(start), "end": self._when(end)}
        if emails:
            body["attendees"] = [{"email": a} for a in emails]
        if description:
            body["description"] = description
        if location:
            body["location"] = location
        if idempotency_key:
            body["id"] = event_id_from_key(idempotency_key)

        duplicate = False
        try:
            created = self.client.create_event(body, send_updates)
        except GoogleConflictError:
            if not idempotency_key:
                raise
            created, duplicate = self.client.get_event(body["id"]), True
            if created.status == "cancelled":
                raise CalendarError("This action's event id was used by an event that has since been cancelled.") from None
        fetched = self.client.get_event(created.id)
        verified = (fetched.status != "cancelled" and fetched.summary == body["summary"]
                    and same_instant(parse_iso(fetched.start, self.tz), start))  # type: ignore[arg-type]
        if not verified:
            log.error("calendar event creation not verified event_id=%s", created.id)
            raise CalendarVerificationError("Google Calendar did not confirm the event was created as requested.")
        log.info("calendar event created event_id=%s attendees=%d duplicate=%s verified=true",
                 fetched.id, len(emails), duplicate)
        return CreateResult(fetched, duplicate, True)

    def reschedule_event(self, event_id: str, new_start: datetime, new_end: datetime | None = None,
                         send_updates: str = "none") -> RescheduleResult:
        ev = self.client.get_event(event_id)
        if ev.status == "cancelled":
            raise CalendarError("That event has been cancelled.")
        if ev.all_day:
            raise CalendarUnsupportedError("Rescheduling all-day events is not supported yet.")
        old_s, old_e = parse_iso(ev.start, self.tz), parse_iso(ev.end, self.tz)
        new_start = localize(new_start, self.tz)
        new_end = localize(new_end, self.tz) if new_end else new_start + (old_e - old_s)  # type: ignore[operator]
        if new_end <= new_start:
            raise ValueError("end must be after start")
        self.client.update_event(event_id, {"start": self._when(new_start), "end": self._when(new_end)}, send_updates)
        after = self.client.get_event(event_id)
        if not (same_instant(parse_iso(after.start, self.tz), new_start)  # type: ignore[arg-type]
                and same_instant(parse_iso(after.end, self.tz), new_end)):  # type: ignore[arg-type]
            log.error("calendar event update not verified event_id=%s", event_id)
            raise CalendarVerificationError("Google Calendar did not confirm the new time.")
        log.info("calendar event rescheduled event_id=%s verified=true", event_id)
        return RescheduleResult(after, ev.start, ev.end, True)

    def cancel_event(self, event_id: str, send_updates: str = "none") -> CancelResult:
        ev = self.client.get_event(event_id)
        if ev.status == "cancelled":
            raise CalendarError("That event is already cancelled.")
        self.client.delete_event(event_id, send_updates)
        try:
            gone = self.client.get_event(event_id).status == "cancelled"
        except GoogleNotFoundError:
            gone = True
        if not gone:
            log.error("calendar event cancellation not verified event_id=%s", event_id)
            raise CalendarVerificationError("Google Calendar did not confirm the event was deleted.")
        log.info("calendar event cancelled event_id=%s verified=true", event_id)
        return CancelResult(ev, True)

    def _when(self, dt: datetime) -> dict:
        return {"dateTime": to_rfc3339(dt), "timeZone": getattr(dt.tzinfo, "key", None) or self.tz.key}
