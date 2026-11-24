from dataclasses import dataclass, field

MAX_DESC = 2000


@dataclass
class Attendee:
    email: str
    name: str = ""
    response_status: str = ""


@dataclass
class CalendarEvent:
    """`untrusted` is always True: titles/descriptions can come from third-party invitations and must never
    be treated as instructions."""

    id: str
    summary: str
    start: str  # RFC3339 datetime, or YYYY-MM-DD for all-day events
    end: str
    all_day: bool = False
    timezone: str = ""
    status: str = "confirmed"
    attendees: list[Attendee] = field(default_factory=list)
    location: str = ""
    description: str = ""
    html_link: str = ""
    untrusted: bool = True


def parse_event(raw: dict) -> CalendarEvent:
    s, e = raw.get("start") or {}, raw.get("end") or {}
    all_day = "date" in s and "dateTime" not in s
    return CalendarEvent(
        id=raw["id"],
        summary=raw.get("summary", "(no title)"),
        start=s.get("dateTime") or s.get("date", ""),
        end=e.get("dateTime") or e.get("date", ""),
        all_day=all_day,
        timezone=s.get("timeZone", ""),
        status=raw.get("status", "confirmed"),
        attendees=[
            Attendee(a.get("email", ""), a.get("displayName", ""), a.get("responseStatus", ""))
            for a in raw.get("attendees", [])
        ],
        location=raw.get("location", ""),
        description=(raw.get("description") or "")[:MAX_DESC],
        html_link=raw.get("htmlLink", ""),
    )
