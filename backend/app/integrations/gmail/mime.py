"""Stdlib-only Gmail MIME helpers: build outgoing messages safely, parse incoming ones."""
import base64
import binascii
import html
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.message import EmailMessage
from email.message import Message as MIMEHeader
from email.policy import SMTP
from email.utils import parseaddr
from html.parser import HTMLParser

from app.core.validation import is_valid_email

MAX_BODY_CHARS = 20_000
MAX_SUBJECT = 998
log = logging.getLogger(__name__)


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

def decode_part(data: str, charset: str = "utf-8") -> str:
    try:
        decoded = base64.b64decode(data + "=" * (-len(data) % 4), altchars=b"-_", validate=True)
    except (binascii.Error, ValueError):
        log.warning("gmail mime part base64url decode failed data_chars=%d", len(data))
        return ""
    try:
        return decoded.decode(charset or "utf-8", errors="replace")
    except LookupError:
        log.info("gmail mime part has unknown charset=%s; decoded as utf-8", charset)
        return decoded.decode("utf-8", errors="replace")


_BLOCK_TAGS = {"br", "p", "div", "li", "tr", "table", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "hr"}


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0
        self._pre = 0
        self._links: list[tuple[str, int]] = []

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "head", "title"):
            self._skip += 1
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")
        elif tag == "td":
            self.parts.append(" ")
        elif tag == "pre":
            self._pre += 1
        elif tag == "a":
            self._links.append((dict(attrs).get("href") or "", len(self.parts)))

    def handle_endtag(self, tag):
        if tag in ("script", "style", "head", "title") and self._skip:
            self._skip -= 1
        elif tag == "pre" and self._pre:
            self._pre -= 1
        elif tag in ("p", "div", "li", "tr", "table", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote"):
            self.parts.append("\n")
        elif tag == "a" and self._links:
            # Meeting links often exist only in the href, so keep the URL next to its anchor text.
            href, start = self._links.pop()
            if href.startswith(("http://", "https://")) and href not in "".join(self.parts[start:]):
                self.parts.append(f" ({href})")

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data if self._pre else re.sub(r"\s+", " ", data))


def html_to_text(markup: str) -> str:
    p = _TextExtractor()
    p.feed(markup)
    p.close()
    text = re.sub(r"[ \t\r\f\v]+", " ", html.unescape("".join(p.parts)))
    text = "\n".join(line.strip() for line in text.split("\n"))
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _walk(part: dict):
    if not isinstance(part, dict):
        return
    yield part
    for child in part.get("parts") or []:
        yield from _walk(child)


def _charset(part: dict) -> str:
    content_type = _header(part, "Content-Type") or part.get("mimeType", "")
    header = MIMEHeader()
    header["Content-Type"] = content_type if isinstance(content_type, str) else "text/plain"
    return header.get_content_charset() or "utf-8"


def _is_attachment(part: dict) -> bool:
    return bool(part.get("filename")) or _header(part, "Content-Disposition").strip().lower().startswith("attachment")


def referenced_text_parts(payload: dict) -> list[dict]:
    """Body text parts whose content Gmail returned by reference (body.attachmentId, no inline data). Gmail does
    this for large parts; the caller must fetch them before `parse_message` can read the body."""
    if not isinstance(payload, dict):
        return []
    out = []
    for p in _walk(payload):
        body = p.get("body")
        if (p.get("mimeType") in ("text/plain", "text/html") and not _is_attachment(p) and isinstance(body, dict)
                and body.get("attachmentId") and not body.get("data")):
            out.append(p)
    return out


def extract_text(payload: dict) -> str:
    if not isinstance(payload, dict):
        return ""
    plain: list[str] = []
    markup: list[str] = []
    for p in _walk(payload):
        if _is_attachment(p):
            continue
        part_body = p.get("body")
        data = part_body.get("data") if isinstance(part_body, dict) else None
        if not isinstance(data, str) or not data:
            continue
        mt = p.get("mimeType", "")
        if mt == "text/plain":
            plain.append(decode_part(data, _charset(p)))
        elif mt == "text/html":
            markup.append(decode_part(data, _charset(p)))
    text = "\n".join(plain).strip() or html_to_text("\n".join(markup))
    return text if len(text) <= MAX_BODY_CHARS else text[:MAX_BODY_CHARS] + "\n[truncated]"


def describe_payload(payload: dict, body_text: str) -> dict:
    """Shape of a parsed message for logs: part counts and which text source produced the body (never content)."""
    parts = list(_walk(payload)) if isinstance(payload, dict) else []
    texts = [p for p in parts if not _is_attachment(p) and isinstance(p.get("body"), dict) and p["body"].get("data")]
    plain = sum(1 for p in texts if p.get("mimeType") == "text/plain")
    markup = sum(1 for p in texts if p.get("mimeType") == "text/html")
    source = "empty" if not body_text else "text/plain" if plain else "text/html" if markup else "unknown"
    return {"mime_type": payload.get("mimeType", "") if isinstance(payload, dict) else "", "mime_parts": len(parts),
            "text_plain_parts": plain, "text_html_parts": markup, "body_source": source,
            "body_chars": len(body_text), "truncated": body_text.endswith("[truncated]"),
            "attachments": sum(1 for p in parts if _is_attachment(p))}


def _header(payload: dict, name: str) -> str:
    for h in payload.get("headers") or []:
        if not isinstance(h, dict):
            continue
        header_name = h.get("name")
        if isinstance(header_name, str) and header_name.lower() == name.lower():
            value = h.get("value")
            return value if isinstance(value, str) else ""
    return ""


def _attachments(payload: dict) -> list[dict]:
    out = []
    for p in _walk(payload):
        part_body = p.get("body")
        if _is_attachment(p) and isinstance(part_body, dict) and (part_body.get("attachmentId") or part_body.get("data")):
            out.append({"filename": p.get("filename") or "(unnamed attachment)", "mime_type": p.get("mimeType", ""),
                        "size": part_body.get("size", 0)})
    return out


def parse_message(raw: dict, include_body: bool = True) -> EmailMessageData:
    payload = raw.get("payload") or {}
    if not isinstance(payload, dict):
        payload = {}
    internal = None
    if raw.get("internalDate"):
        try:
            internal = datetime.fromtimestamp(int(raw["internalDate"]) / 1000, tz=timezone.utc).isoformat()
        except (OverflowError, TypeError, ValueError):
            pass
    return EmailMessageData(
        id=str(raw.get("id") or ""),
        thread_id=raw.get("threadId", ""),
        sender=_header(payload, "From"),
        to=_header(payload, "To"),
        cc=_header(payload, "Cc"),
        subject=_header(payload, "Subject"),
        date=_header(payload, "Date"),
        internal_date=internal,
        snippet=html.unescape(str(raw.get("snippet") or "")),
        labels=list(raw.get("labelIds") or []) if isinstance(raw.get("labelIds") or [], list) else [],
        body_text=extract_text(payload) if include_body else "",
        message_id_header=_header(payload, "Message-ID") or _header(payload, "Message-Id"),
        in_reply_to=_header(payload, "In-Reply-To"),
        references=_header(payload, "References"),
        attachments=_attachments(payload) if include_body else [],
    )
