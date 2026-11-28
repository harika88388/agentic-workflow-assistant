"""Specialized Google Calendar agents. Events are located with the existing matching module (unique / ambiguous /
none, never a guess); create, reschedule and cancel are returned as proposals that run only after approval."""
import logging
from datetime import date, datetime, time, timedelta
from typing import Any

from app.agents.base import (
    AgentContext, AgentOutcome, OperationAgent, Proposal, ToolCall, compact_event, fmt_when,
)
from app.integrations.calendar import matching
from app.integrations.calendar.models import Attendee, CalendarEvent
from app.integrations.calendar.timeutil import NonexistentTimeError, localize, parse_iso

SEARCH_WINDOW_DAYS = 30
MAX_OPTIONS = 5
WORKDAY = (time(9, 0), time(18, 0))
log = logging.getLogger(__name__)


def to_event(d: dict[str, Any]) -> CalendarEvent:
    fields = {k: v for k, v in d.items() if k in CalendarEvent.__dataclass_fields__ and k != "attendees"}
    return CalendarEvent(**fields, attendees=[Attendee(**a) for a in d.get("attendees") or []])


def _date(ctx: AgentContext) -> date | None:
    return date.fromisoformat(ctx.get("date")) if ctx.get("date") else None


def _time(value: str | None) -> time | None:
    return time.fromisoformat(value) if value else None


def _at(d: date, t: time, ctx: AgentContext) -> datetime:
    return localize(datetime.combine(d, t), ctx.tz)


def _describe(e: dict[str, Any] | CalendarEvent, ctx: AgentContext) -> str:
    summary = e.summary if isinstance(e, CalendarEvent) else e.get("summary")
    start = e.start if isinstance(e, CalendarEvent) else e.get("start", "")
    return f"\"{summary}\" ({fmt_when(start, ctx.tz)})"


def _local_date(e: CalendarEvent, ctx: AgentContext) -> date:
    v = parse_iso(e.start, ctx.tz)
    return v.astimezone(ctx.tz).date() if isinstance(v, datetime) else v


def _option(e: CalendarEvent) -> dict[str, Any]:
    return {"id": e.id, "summary": e.summary, "start": e.start}


def locate_event(ctx: AgentContext, *, date_identifies_event: bool) -> tuple[CalendarEvent | None, AgentOutcome | None]:
    """Find the one event the user means. For reschedule the stated date is the NEW date, so it is not used to
    narrow the search (`date_identifies_event=False`)."""
    r = ctx.tool_results
    if ctx.get("event_id"):
        if "event" not in r:
            return None, AgentOutcome.calls(ToolCall("event", "calendar", "get_event", {"event_id": ctx.get("event_id")}))
        ev = to_event(r["event"]["event"])
        if ev.status == "cancelled":
            return None, AgentOutcome.error("operation_failed", f"{_describe(ev, ctx)} has already been cancelled.")
        return ev, None

    on_date = _date(ctx) if date_identifies_event else None
    if "candidates" not in r:
        args = {"on_date": on_date.isoformat()} if on_date else {"days": SEARCH_WINDOW_DAYS, "max_results": 250}
        return None, AgentOutcome.calls(ToolCall("candidates", "calendar", "list_events", args))
    events = [to_event(e) for e in r["candidates"].get("events") or []]
    res = matching.resolve(matching.find_candidates(
        events, ctx.tz, person=ctx.get("person"), title=ctx.get("title"), on_date=on_date,
        at_time=_time(ctx.get("time")) if date_identifies_event else None))
    who = ctx.get("title") or (f"with {ctx.get('person')}" if ctx.get("person") else "")
    keep = {k: v for k, v in ctx.entities.items() if k != "event_id"}
    if res.status == "unique":
        return res.event, None
    if res.status == "none":
        window = f"on {fmt_when(on_date.isoformat(), ctx.tz)}" if on_date else f"in the next {SEARCH_WINDOW_DAYS} days"
        return None, AgentOutcome("clarify", message=f"I couldn't find a meeting {who} {window}. Which event do you mean?",
                                  pending={"entities": {k: v for k, v in keep.items() if k not in ("person", "title")},
                                           "awaiting": ["event_reference"]})
    options = [c.event for c in res.candidates[:MAX_OPTIONS]]
    lines = "\n".join(f"{i}. {_describe(e, ctx)}" for i, e in enumerate(options, 1))
    return None, AgentOutcome("clarify", message=f"I found {len(options)} meetings {who}:\n{lines}\nWhich one do you mean?",
                              pending={"entities": keep, "awaiting": ["event_selection"],
                                       "candidates": [_option(e) for e in options], "candidate_type": "event"},
                              memory={"events": [compact_event(e.__dict__ | {"attendees": []}) for e in options]})


class CreateEventAgent(OperationAgent):
    service, intent = "calendar", "create_event"
    description = "Build a new event (title, time, attendees), check for conflicts and propose creating it."

    def step(self, ctx: AgentContext) -> AgentOutcome:
        d, t = _date(ctx), _time(ctx.get("time"))
        assert d and t  # guaranteed by parameter validation
        try:
            start = _at(d, t, ctx)
        except NonexistentTimeError:
            return AgentOutcome("clarify", message="That time doesn't exist on that day (daylight-saving change). Which time?",
                                pending={"entities": {k: v for k, v in ctx.entities.items() if k != "time"}, "awaiting": ["time"]})
        if start <= ctx.now:
            return AgentOutcome("clarify", message=f"{fmt_when(start.isoformat(), ctx.tz)} has already passed. When should the meeting be?",
                                pending={"entities": {k: v for k, v in ctx.entities.items() if k not in ("date", "time")},
                                         "awaiting": ["date", "time"]})
        end_t = _time(ctx.get("end_time"))
        end = _at(d, end_t, ctx) if end_t and _at(d, end_t, ctx) > start else None
        duration = int(ctx.get("duration_minutes") or 60)
        effective_end = end or start + timedelta(minutes=duration)
        r = ctx.tool_results
        calls = []
        if "availability" not in r:
            calls.append(ToolCall("availability", "calendar", "check_availability",
                                  {"start": start.isoformat(), "end": effective_end.isoformat()}))
        if "existing" not in r:
            calls.append(ToolCall("existing", "calendar", "list_events", {"on_date": d.isoformat()}))
        if calls:
            return AgentOutcome.calls(*calls)
        person = ctx.get("person")
        title = ctx.get("title") or (f"Meeting with {person}" if person else "Meeting")
        attendees = list(dict.fromkeys([*(ctx.get("attendees") or []), *([ctx.get("recipient_email")] if ctx.get("recipient_email") else [])]))
        if dup := _duplicate(r["existing"].get("events") or [], start, title, attendees, ctx):
            log.info("calendar duplicate event detected event_id=%s; not proposing a new event", dup.id)
            return AgentOutcome("result", message=f"{_describe(dup, ctx)} is already in your calendar at that time, "
                                                  f"so I haven't created another one.",
                                data={"duplicate": compact_event(dup.__dict__ | {"attendees": []})},
                                memory={"last_event_id": dup.id})
        request = {"title": title, "start": start.isoformat(), "end": end.isoformat() if end else None,
                   "duration_minutes": duration, "attendees": attendees}
        for key in ("location", "description"):
            if ctx.get(key):
                request[key] = str(ctx.get(key))
        notes = []
        if ctx.get("location"):
            notes.append(f"Location: {ctx.get('location')}")
        if ctx.get("description"):
            notes.append(f"Notes: {ctx.get('description')}")
        conflict = not r["availability"].get("is_free", True)
        if conflict:
            notes.append("Note: you already have something in your calendar at that time.")
        if person and not attendees:
            notes.append(f"{person} won't be invited because I don't have their email address.")
        preview = (f"Create \"{title}\" on {fmt_when(start.isoformat(), ctx.tz)}–{effective_end.astimezone(ctx.tz):%H:%M}"
                   f"{' with ' + ', '.join(attendees) if attendees else ''}?" + ("\n" + "\n".join(notes) if notes else ""))
        return AgentOutcome("proposal", proposal=Proposal("calendar", self.intent, request, preview),
                            data={"conflict": conflict})


def _duplicate(events: list[dict[str, Any]], start: datetime, title: str, attendees: list[str],
               ctx: AgentContext) -> CalendarEvent | None:
    """An existing event at the same start that looks like the same meeting (similar title or a shared attendee)."""
    words = {w for w in title.casefold().split() if len(w) > 2 and w not in ("meeting", "with", "call")}
    for e in (to_event(x) for x in events):
        if e.status == "cancelled" or e.all_day:
            continue
        s = parse_iso(e.start, ctx.tz)
        if not isinstance(s, datetime) or s.astimezone(ctx.tz) != start.astimezone(ctx.tz):
            continue
        theirs = {w for w in (e.summary or "").casefold().split() if len(w) > 2}
        same_title = (e.summary or "").strip().casefold() == title.strip().casefold() or bool(words & theirs)
        shared = bool(set(attendees) & {a.email.lower() for a in e.attendees if a.email})
        if same_title or shared:
            return e
    return None


class GetEventsAgent(OperationAgent):
    service, intent = "calendar", "get_events"
    description = "List events on a date, the next days, or those matching a person/title."

    def step(self, ctx: AgentContext) -> AgentOutcome:
        d = _date(ctx)
        filtered = bool(ctx.get("person") or ctx.get("title"))
        if "events" not in ctx.tool_results:
            args = {"on_date": d.isoformat()} if d else {"days": SEARCH_WINDOW_DAYS if filtered else 7}
            return AgentOutcome.calls(ToolCall("events", "calendar", "list_events", args))
        events = [to_event(e) for e in ctx.tool_results["events"].get("events") or [] if e.get("status") != "cancelled"]
        if d:
            events = [e for e in events if _local_date(e, ctx) == d]
        if filtered:
            events = [c.event for c in matching.find_candidates(events, ctx.tz, person=ctx.get("person"), title=ctx.get("title"))]
            events.sort(key=lambda e: e.start)
        if "next" in str(ctx.get("query") or ctx.message).casefold():
            events = [e for e in events if not e.all_day][:1]
        scope = f"on {fmt_when(d.isoformat(), ctx.tz)}" if d else ("coming up" if not filtered else "in the next 30 days")
        if not events:
            return AgentOutcome("result", message=f"You have no matching events {scope}.", data={"events": []},
                                memory={"events": []})
        compact = [compact_event(e.__dict__ | {"attendees": [a.__dict__ for a in e.attendees]}) for e in events]
        lines = "\n".join(f"{i}. {_describe(e, ctx)}" for i, e in enumerate(events[:20], 1))
        memory: dict[str, Any] = {"events": compact}
        if len(events) == 1:
            memory["last_event_id"] = events[0].id
        return AgentOutcome("result", message=f"Your events {scope}:\n{lines}", data={"events": compact}, memory=memory)


class RescheduleEventAgent(OperationAgent):
    service, intent = "calendar", "reschedule_event"
    description = "Identify the event, work out the new time (keeping its duration) and propose moving it."

    def step(self, ctx: AgentContext) -> AgentOutcome:
        ev, early = locate_event(ctx, date_identifies_event=False)
        if early:
            return early
        assert ev is not None
        if ev.all_day:
            return AgentOutcome.error("unsupported", "Rescheduling all-day events is not supported yet.")
        keep = {**ctx.entities, "event_id": ev.id}
        t = _time(ctx.get("time"))
        if t is None:
            return AgentOutcome("clarify", message=f"What time should I move {_describe(ev, ctx)} to?",
                                pending={"entities": keep, "awaiting": ["time"]}, memory={"last_event_id": ev.id})
        old_start = parse_iso(ev.start, ctx.tz)
        assert isinstance(old_start, datetime)
        new_date = _date(ctx) or old_start.astimezone(ctx.tz).date()
        try:
            new_start = _at(new_date, t, ctx)
        except NonexistentTimeError:
            return AgentOutcome("clarify", message="That time doesn't exist on that day (daylight-saving change). Which time?",
                                pending={"entities": {k: v for k, v in keep.items() if k != "time"}, "awaiting": ["time"]})
        if new_start <= ctx.now:
            return AgentOutcome("clarify", message=f"{fmt_when(new_start.isoformat(), ctx.tz)} has already passed. When should it move to?",
                                pending={"entities": {k: v for k, v in keep.items() if k not in ("date", "time")},
                                         "awaiting": ["new_date_or_time"]})
        end_t = _time(ctx.get("end_time"))
        new_end = _at(new_date, end_t, ctx) if end_t and _at(new_date, end_t, ctx) > new_start else None
        request = {"event_id": ev.id, "new_start": new_start.isoformat(), "new_end": new_end.isoformat() if new_end else None,
                   "event_summary": ev.summary, "previous_start": ev.start}
        preview = f"Move \"{ev.summary}\" from {fmt_when(ev.start, ctx.tz)} to {fmt_when(new_start.isoformat(), ctx.tz)}?"
        return AgentOutcome("proposal", proposal=Proposal("calendar", self.intent, request, preview),
                            memory={"last_event_id": ev.id})


class CancelEventAgent(OperationAgent):
    service, intent = "calendar", "cancel_event"
    description = "Identify the event and propose cancelling it."

    def step(self, ctx: AgentContext) -> AgentOutcome:
        ev, early = locate_event(ctx, date_identifies_event=True)
        if early:
            return early
        assert ev is not None
        request = {"event_id": ev.id, "event_summary": ev.summary, "event_start": ev.start}
        return AgentOutcome("proposal", proposal=Proposal("calendar", self.intent, request, f"Cancel {_describe(ev, ctx)}?"),
                            memory={"last_event_id": ev.id})


class CheckAvailabilityAgent(OperationAgent):
    service, intent = "calendar", "check_availability"
    description = "Report free/busy time for a date (working hours) or a specific time window."

    def step(self, ctx: AgentContext) -> AgentOutcome:
        d = _date(ctx)
        assert d is not None  # guaranteed by parameter validation
        t = _time(ctx.get("time"))
        try:
            start = _at(d, t or WORKDAY[0], ctx)
            end_t = _time(ctx.get("end_time"))
            end = (_at(d, end_t, ctx) if end_t else start + timedelta(minutes=int(ctx.get("duration_minutes") or 60))) \
                if t else _at(d, WORKDAY[1], ctx)
        except NonexistentTimeError:
            return AgentOutcome.error("invalid_arguments", "That time doesn't exist on that day (daylight-saving change).")
        if end <= start:
            return AgentOutcome.error("invalid_arguments", "The end of the window must be after its start.")
        if "availability" not in ctx.tool_results:
            return AgentOutcome.calls(ToolCall("availability", "calendar", "check_availability",
                                               {"start": start.isoformat(), "end": end.isoformat()}))
        a = ctx.tool_results["availability"]
        window = f"{fmt_when(start.isoformat(), ctx.tz)}–{end.astimezone(ctx.tz):%H:%M}"
        hm = lambda iso: parse_iso(iso, ctx.tz).astimezone(ctx.tz).strftime("%H:%M")  # noqa: E731
        if a.get("is_free"):
            text = f"You're free {window}."
        else:
            busy = ", ".join(f"{hm(s)}–{hm(e)}" for s, e in a.get("busy", []))
            free = ", ".join(f"{hm(s)}–{hm(e)}" for s, e in a.get("free_slots", [])) or "none"
            text = f"You're busy during {window}: {busy}.\nFree slots: {free}."
        return AgentOutcome("result", message=text, data={"window": [start.isoformat(), end.isoformat()], **a})


def build_calendar_agents() -> dict[str, OperationAgent]:
    agents: list[OperationAgent] = [CreateEventAgent(), GetEventsAgent(), RescheduleEventAgent(), CancelEventAgent(),
                                    CheckAvailabilityAgent()]
    return {a.intent: a for a in agents}
