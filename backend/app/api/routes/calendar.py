from dataclasses import asdict
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query

from app.api.deps import get_calendar_service
from app.integrations.calendar.service import CalendarService
from app.integrations.calendar.timeutil import localize

router = APIRouter(prefix="/calendar", tags=["calendar"])
# Read-only on purpose. Create/reschedule/cancel go through the approval-gated workflow (Phase 9).


@router.get("/events")
def list_events(days: int = Query(7, ge=1, le=90), q: str | None = Query(None, max_length=200),
                cal: CalendarService = Depends(get_calendar_service)) -> list[dict]:
    return [asdict(e) for e in cal.list_events(days=days, query=q)]


@router.get("/next")
def next_event(cal: CalendarService = Depends(get_calendar_service)) -> dict:
    ev = cal.next_event()
    return {"event": asdict(ev) if ev else None}


@router.get("/availability")
def availability(start: datetime, end: datetime, cal: CalendarService = Depends(get_calendar_service)) -> dict:
    """Naive datetimes are interpreted in the user's timezone."""
    try:
        return asdict(cal.check_availability(localize(start, cal.tz), localize(end, cal.tz)))
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
