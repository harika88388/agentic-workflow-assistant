"""Resolve the user's date wording ('tomorrow', 'next Friday', '28 Nov') against their reference date."""
import re
from datetime import date, timedelta

from app.core.validation import normalize_time

_WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
_MONTHS = ["january", "february", "march", "april", "may", "june", "july", "august", "september",
           "october", "november", "december"]
_WD = {d: i for i, d in enumerate(_WEEKDAYS)} | {d[:3]: i for i, d in enumerate(_WEEKDAYS)}
_MON = {m: i + 1 for i, m in enumerate(_MONTHS)} | {m[:3]: i + 1 for i, m in enumerate(_MONTHS)} | {"sept": 9}
_TIME_RE = re.compile(r"\b(\d{1,2}(?::\d{2})?\s*[ap]\.?m\.?|\d{1,2}:\d{2}|noon|midnight)\b", re.I)


def _safe(y: int, m: int, d: int) -> date | None:
    try:
        return date(y, m, d)
    except ValueError:
        return None


def resolve_date(text: str | None, ref: date) -> date | None:
    """Return the calendar date meant by `text`, or None if it cannot be determined unambiguously.

    A bare or 'next' weekday means its next occurrence after today; 'this <weekday>' may be today.
    A day/month without a year means its next occurrence on or after today."""
    if not text:
        return None
    s = re.sub(r"[,.]", " ", text.strip().casefold())
    s = re.sub(r"\s+", " ", s).strip()
    try:
        return date.fromisoformat(s[:10]) if re.fullmatch(r"\d{4}-\d{2}-\d{2}.*", s) else _relative(s, ref)
    except ValueError:
        return None


def _relative(s: str, ref: date) -> date | None:
    if re.search(r"\bday after tomorrow\b", s):
        return ref + timedelta(days=2)
    if re.search(r"\b(today|tonight|this (morning|afternoon|evening))\b", s):
        return ref
    if re.search(r"\btomorrow\b", s):
        return ref + timedelta(days=1)
    if m := re.search(r"\bin (\d{1,3}) days?\b", s):
        return ref + timedelta(days=int(m.group(1)))
    if re.search(r"\bnext week\b", s) and not any(w in s for w in _WD):
        return ref + timedelta(days=7 - ref.weekday())  # Monday of next week
    if m := re.search(r"\b(this|next|coming)?\s*(" + "|".join(sorted(_WD, key=len, reverse=True)) + r")\b", s):
        target = _WD[m.group(2)]
        delta = (target - ref.weekday()) % 7
        if delta == 0 and m.group(1) != "this":
            delta = 7
        return ref + timedelta(days=delta)
    month_names = "|".join(sorted(_MON, key=len, reverse=True))
    m = re.search(rf"\b(\d{{1,2}})(?:st|nd|rd|th)?(?: of)? ({month_names})\b(?: (\d{{4}}))?", s) or None
    if m:
        day, month, year = int(m.group(1)), _MON[m.group(2)], m.group(3)
    else:
        m = re.search(rf"\b({month_names}) (\d{{1,2}})(?:st|nd|rd|th)?\b(?: (\d{{4}}))?", s)
        if not m:
            return None
        day, month, year = int(m.group(2)), _MON[m.group(1)], m.group(3)
    if year:
        return _safe(int(year), month, day)
    candidate = _safe(ref.year, month, day)
    if candidate and candidate < ref:
        candidate = _safe(ref.year + 1, month, day)
    return candidate


def extract_time(text: str | None) -> str | None:
    """First clock time in free text ('at 3 PM' -> '15:00'); None if there is none."""
    if not text:
        return None
    m = _TIME_RE.search(text)
    return normalize_time(m.group(1).replace(" ", "")) if m else None
