"""Identify which calendar event the user means BEFORE modifying anything. Never guesses between close matches."""
import re
from dataclasses import dataclass, field
from datetime import date, time
from zoneinfo import ZoneInfo

from app.integrations.calendar.models import CalendarEvent
from app.integrations.calendar.timeutil import parse_iso

AMBIGUITY_MARGIN = 1.5


@dataclass
class ScoredEvent:
    event: CalendarEvent
    score: float


@dataclass
class Resolution:
    status: str  # "unique" | "ambiguous" | "none"
    event: CalendarEvent | None = None
    candidates: list[ScoredEvent] = field(default_factory=list)


def _tokens(text: str) -> set[str]:
    return set(re.findall(r"\w+", (text or "").casefold()))


def _frac(needed: set[str], haystack: set[str]) -> float:
    return len(needed & haystack) / len(needed) if needed else 0.0


def _local_start(ev: CalendarEvent, tz: ZoneInfo):
    v = parse_iso(ev.start, tz)
    return v if isinstance(v, date) and not hasattr(v, "hour") else v.astimezone(tz)


def score_event(ev: CalendarEvent, tz: ZoneInfo, person=None, title=None, on_date: date | None = None,
                at_time: time | None = None) -> float | None:
    """None = excluded (cancelled / wrong day). Otherwise a score >= 0."""
    if ev.status == "cancelled":
        return None
    start = _local_start(ev, tz)
    start_date = start if not hasattr(start, "hour") else start.date()
    if on_date and start_date != on_date:
        return None
    score = 0.0
    if person:
        p = _tokens(person)
        att = set()
        for a in ev.attendees:
            att |= _tokens(a.name) | _tokens(a.email.split("@")[0])
        score += 3 * _frac(p, att) + 0.5 * _frac(p, _tokens(ev.summary) | _tokens(ev.description))  # attendee = real signal
    if title:
        score += 3 * _frac(_tokens(title), _tokens(ev.summary))
    if at_time and hasattr(start, "hour") and (start.hour, start.minute) == (at_time.hour, at_time.minute):
        score += 2  # an explicit time is strong evidence: enough to break a tie (>= AMBIGUITY_MARGIN)
    if (person or title) and score <= 0:
        return None
    return score if (person or title or at_time) else 0.5


def find_candidates(events: list[CalendarEvent], tz: ZoneInfo, *, person=None, title=None,
                    on_date: date | None = None, at_time: time | None = None) -> list[ScoredEvent]:
    scored = []
    for ev in events:
        s = score_event(ev, tz, person, title, on_date, at_time)
        if s is not None:
            scored.append(ScoredEvent(ev, s))
    scored.sort(key=lambda x: (-x.score, x.event.start))
    return scored


def resolve(candidates: list[ScoredEvent]) -> Resolution:
    if not candidates:
        return Resolution("none")
    if len(candidates) == 1 or candidates[0].score - candidates[1].score >= AMBIGUITY_MARGIN:
        return Resolution("unique", candidates[0].event, candidates)
    return Resolution("ambiguous", None, candidates)
