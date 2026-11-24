"""Google Calendar API v3 client (primary calendar)."""
from collections.abc import Callable
from datetime import datetime
from urllib.parse import quote

from app.integrations.calendar.models import CalendarEvent, parse_event
from app.integrations.calendar.timeutil import to_rfc3339
from app.integrations.http import GoogleHTTP

BASE = "https://www.googleapis.com/calendar/v3"
EVENTS = "/calendars/primary/events"


def _id(value: str) -> str:
    if not value or not value.strip():
        raise ValueError("event id must not be empty")
    return quote(value, safe="")


class CalendarClient:
    def __init__(self, token_provider: Callable[[], str], **http_kwargs):
        self.http = GoogleHTTP(BASE, token_provider, **http_kwargs)

    def list_events(self, time_min: datetime, time_max: datetime | None = None, q: str | None = None,
                    max_results: int = 100) -> list[CalendarEvent]:
        params: dict = {"timeMin": to_rfc3339(time_min), "singleEvents": "true", "orderBy": "startTime",
                        "maxResults": max(1, min(max_results, 250))}
        if time_max:
            params["timeMax"] = to_rfc3339(time_max)
        if q:
            params["q"] = q
        data = self.http.request("GET", EVENTS, params=params) or {}
        return [parse_event(e) for e in data.get("items", [])]

    def get_event(self, event_id: str) -> CalendarEvent:
        return parse_event(self.http.request("GET", f"{EVENTS}/{_id(event_id)}"))

    def create_event(self, body: dict, send_updates: str = "none") -> CalendarEvent:
        return parse_event(self.http.request("POST", EVENTS, params={"sendUpdates": send_updates}, json=body))

    def update_event(self, event_id: str, patch: dict, send_updates: str = "none") -> CalendarEvent:
        return parse_event(self.http.request(
            "PATCH", f"{EVENTS}/{_id(event_id)}", params={"sendUpdates": send_updates}, json=patch, idempotent=True))

    def delete_event(self, event_id: str, send_updates: str = "none") -> None:
        self.http.request("DELETE", f"{EVENTS}/{_id(event_id)}", params={"sendUpdates": send_updates})

    def freebusy(self, start: datetime, end: datetime, tz_name: str) -> list[tuple[datetime, datetime]]:
        data = self.http.request(
            "POST", "/freeBusy", idempotent=True,
            json={"timeMin": to_rfc3339(start), "timeMax": to_rfc3339(end), "timeZone": tz_name,
                  "items": [{"id": "primary"}]},
        ) or {}
        busy = (data.get("calendars", {}).get("primary", {}) or {}).get("busy", [])
        out = []
        for b in busy:
            out.append((datetime.fromisoformat(b["start"].replace("Z", "+00:00")),
                        datetime.fromisoformat(b["end"].replace("Z", "+00:00"))))
        return out
