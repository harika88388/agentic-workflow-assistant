"""Stdlib-only Gmail MIME helpers: build outgoing messages safely, parse incoming ones."""
import base64
import html
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.message import EmailMessage
from email.parser import HeaderParser
from email.policy import SMTP
from email.utils import parseaddr
from html.parser import HTMLParser

from app.core.validation import is_valid_email

MAX_BODY_CHARS = 20_000
MAX_SUBJECT = 998


@dataclass
class EmailMessageData:
    """An email as retrieved from Gmail. `untrusted` is always True: the content is attacker-controllable
    and must never be treated as instructions or as authorization for any action."""

    id: str
    thread_id: str
    sender: str = ""
    to: str = ""
    cc: str = ""
    subject: str = ""
    date: str = ""
    internal_date: str | None = None
    snippet: str = ""
    labels: list[str] = field(default_factory=list)
    body_text: str = ""
    message_id_header: str = ""
    in_reply_to: str = ""
    references: str = ""
    attachments: list[dict] = field(default_factory=list)
    untrusted: bool = True


@dataclass
class EmailThreadData:
    id: str
    messages: list[EmailMessageData]
    untrusted: bool = True


# ---------- outgoing ----------

def validate_address(raw: str) -> str:
    """Return a bare, valid email address or raise ValueError. Blocks header injection and address lists."""
    if not isinstance(raw, str) or re.search(r"[\r\n,;]", raw):
        raise ValueError(f"invalid email address: {raw!r}")
    _, addr = parseaddr(raw.strip())
    if not is_valid_email(addr):
        raise ValueError(f"invalid email address: {raw!r}")
    return addr


def _clean_header(value: str, name: str) -> str:
    if re.search(r"[\r\n]", value):
        raise ValueError(f"{name} must not contain line breaks")
    return value.strip()


def build_raw_message(
    *,
    to: list[str],
    subject: str,
    body: str,
    cc: list[str] | None = None,
    bcc: list[str] | None = None,
    in_reply_to: str | None = None,
    references: str | None = None,
) -> str:
    """Build an RFC 5322 message and return it base64url-encoded, as the Gmail API expects."""
    if not to:
        raise ValueError("at least one recipient is required")
    if not body or not body.strip():
        raise ValueError("email body must not be empty")
    subject = _clean_header(subject or "", "subject") or "(no subject)"
    if len(subject) > MAX_SUBJECT:
        raise ValueError("subject too long")
    msg = EmailMessage(policy=SMTP)
    msg["To"] = ", ".join(validate_address(a) for a in to)
    if cc:
        msg["Cc"] = ", ".join(validate_address(a) for a in cc)
    if bcc:
        msg["Bcc"] = ", ".join(validate_address(a) for a in bcc)
    msg["Subject"] = subject
    if in_reply_to:
        msg["In-Reply-To"] = _clean_header(in_reply_to, "In-Reply-To")
    if references:
        msg["References"] = _clean_header(references, "References")
    msg.set_content(body, charset="utf-8")
    return base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii")


def reply_subject(subject: str) -> str:
    s = (subject or "").strip()
    return s if re.match(r"(?i)^re:\s", s) else f"Re: {s}".strip()


def reply_headers(target: EmailMessageData) -> tuple[str, str]:
    """(In-Reply-To, References) for replying to `target`."""
    refs = " ".join(x for x in (target.references, target.message_id_header) if x).strip()
    return target.message_id_header, refs


# ---------- incoming ----------

def decode_part(data: str) -> str:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode("utf-8", errors="replace")


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._skip += 1
        elif tag in ("br", "p", "div", "li", "tr"):
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)


def html_to_text(markup: str) -> str:
    p = _TextExtractor()
    p.feed(markup)
    p.close()
    return re.sub(r"\n{3,}", "\n\n", html.unescape("".join(p.parts))).strip()


def _walk(part: dict):
    yield part
    for child in part.get("parts") or []:
        yield from _walk(child)


def extract_text(payload: dict) -> str:
    plain: list[str] = []
    markup: list[str] = []
    for p in _walk(payload):
        if p.get("filename"):
            continue  # attachment
        data = (p.get("body") or {}).get("data")
        if not data:
            continue
        mt = p.get("mimeType", "")
        if mt == "text/plain":
            plain.append(decode_part(data))
        elif mt == "text/html":
            markup.append(decode_part(data))
    text = "\n".join(plain).strip() or html_to_text("\n".join(markup))
    return text if len(text) <= MAX_BODY_CHARS else text[:MAX_BODY_CHARS] + "\n[truncated]"


def _header(payload: dict, name: str) -> str:
    for h in payload.get("headers") or []:
        if h.get("name", "").lower() == name.lower():
            return h.get("value", "")
    return ""


def _attachments(payload: dict) -> list[dict]:
    out = []
    for p in _walk(payload):
        if p.get("filename") and (p.get("body") or {}).get("attachmentId"):
            out.append({"filename": p["filename"], "mime_type": p.get("mimeType", ""), "size": p["body"].get("size", 0)})
    return out


def parse_message(raw: dict, include_body: bool = True) -> EmailMessageData:
    payload = raw.get("payload") or {}
    internal = None
    if raw.get("internalDate"):
        internal = datetime.fromtimestamp(int(raw["internalDate"]) / 1000, tz=timezone.utc).isoformat()
    return EmailMessageData(
        id=raw["id"],
        thread_id=raw.get("threadId", ""),
        sender=_header(payload, "From"),
        to=_header(payload, "To"),
        cc=_header(payload, "Cc"),
        subject=_header(payload, "Subject"),
        date=_header(payload, "Date"),
        internal_date=internal,
        snippet=html.unescape(raw.get("snippet", "")),
        labels=list(raw.get("labelIds") or []),
        body_text=extract_text(payload) if include_body else "",
        message_id_header=_header(payload, "Message-ID") or _header(payload, "Message-Id"),
        in_reply_to=_header(payload, "In-Reply-To"),
        references=_header(payload, "References"),
        attachments=_attachments(payload) if include_body else [],
    )
