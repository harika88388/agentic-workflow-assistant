"""Stdlib-only validators shared by schemas and integrations."""
import re

EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+'-]+@(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z]{2,}$")
_NAMED_TIMES = {"noon": "12:00", "midnight": "00:00"}


def is_valid_email(value: str | None) -> bool:
    if not value or len(value) > 254 or ".." in value:
        return False
    local = value.split("@", 1)[0]
    if local.startswith(".") or local.endswith("."):
        return False
    return EMAIL_RE.fullmatch(value) is not None


def normalize_time(value: object) -> str | None:
    """'3 PM' / '3:30pm' / '15:00' / 'noon' -> 'HH:MM' (24h). Unparseable -> None."""
    if value is None:
        return None
    s = str(value).strip().lower()
    if not s:
        return None
    if s in _NAMED_TIMES:
        return _NAMED_TIMES[s]
    m = re.fullmatch(r"(\d{1,2}):(\d{2})(?::\d{2})?", s)
    if m:
        h, mi = int(m.group(1)), int(m.group(2))
        return f"{h:02d}:{mi:02d}" if h <= 23 and mi <= 59 else None
    m = re.fullmatch(r"(\d{1,2})(?::(\d{2}))?\s*([ap])\.?m\.?", s)
    if m:
        h, mi, ap = int(m.group(1)), int(m.group(2) or 0), m.group(3)
        if not 1 <= h <= 12 or mi > 59:
            return None
        if ap == "p" and h != 12:
            h += 12
        if ap == "a" and h == 12:
            h = 0
        return f"{h:02d}:{mi:02d}"
    return None
