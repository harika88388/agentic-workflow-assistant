"""Structured analysis of complete, untrusted Gmail messages: one email (EmailUnderstandingAgent) or a question
answered across several search matches (EmailQuestionAgent). Model output is grounded against the email text."""
import logging
import re
from email.utils import parseaddr
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.agents.dates import extract_time, extract_times
from app.core.validation import is_valid_email
from app.services.llm.base import ChatMessage
from app.services.llm.prompts import (
    EMAIL_QA_SYSTEM, EMAIL_QA_USER, EMAIL_UNDERSTANDING_SYSTEM, EMAIL_UNDERSTANDING_USER,
)
from app.services.llm.router import LLMRole, LLMRouter
from app.services.llm.structured import complete_structured

MAX_QA_EMAILS = 5
MAX_QA_EMAIL_CHARS = 8_000
MAX_QA_TOTAL_CHARS = 32_000
MEETING_STATUSES = {"confirmed", "proposed", "cancelled", "unclear"}
_MEETING_URL = re.compile(
    r"https?://(?:[\w-]+\.)*(?:meet\.google\.com|zoom\.us|teams\.microsoft\.com|teams\.live\.com|webex\.com)"
    r"/[^\s)>\]\"']*", re.I)
_ADDRESS = re.compile(r"[\w.%+'-]+@[\w-]+(?:\.[\w-]+)+")
log = logging.getLogger(__name__)


class MeetingDetails(BaseModel):
    model_config = ConfigDict(extra="ignore")

    scheduled: bool = False
    date: str | None = None
    time_description: str | None = None
    exact_time: str | None = None


class EmailUnderstanding(BaseModel):
    model_config = ConfigDict(extra="ignore")

    subject: str = ""
    main_topic: str = Field(min_length=1, max_length=2000)
    important_information: list[str] = Field(default_factory=list)
    meeting_mentioned: bool = False
    meeting_details: MeetingDetails = Field(default_factory=MeetingDetails)
    deadlines: list[str] = Field(default_factory=list)
    requests: list[str] = Field(default_factory=list)
    instructions: list[str] = Field(default_factory=list)
    questions: list[str] = Field(default_factory=list)
    action_items: list[str] = Field(default_factory=list)
    entities: list[str] = Field(default_factory=list)
    summary: str = Field(min_length=1, max_length=2000)


class EmailUnderstandingAgent:
    def __init__(self, llm: LLMRouter, call_log: list[dict] | None = None):
        self.llm = llm
        self.call_log = call_log if call_log is not None else []

    def analyze(self, message: dict[str, Any]) -> EmailUnderstanding:
        body = (message.get("body_text") or "").strip()
        subject = message.get("subject") or ""
        if not body:
            return EmailUnderstanding(
                subject=subject,
                main_topic="No readable email body was available.",
                summary="No readable email body was available.",
            )

        messages = [
            ChatMessage("system", EMAIL_UNDERSTANDING_SYSTEM.text),
            ChatMessage("user", EMAIL_UNDERSTANDING_USER.render(
                sender=str(message.get("sender") or ""),
                to=str(message.get("to") or ""),
                date=str(message.get("date") or message.get("internal_date") or ""),
                subject=subject,
                body=body,
            )),
        ]
        self.call_log.append(self.llm.call_record(LLMRole.EMAIL_UNDERSTANDING))
        result = complete_structured(self.llm, LLMRole.EMAIL_UNDERSTANDING, messages, EmailUnderstanding)
        details = result.meeting_details
        if details.date and details.date.casefold() not in body.casefold():
            details = details.model_copy(update={"date": None})
        if details.time_description and details.time_description.casefold() not in body.casefold():
            details = details.model_copy(update={"time_description": None})
        source_time = extract_time(body)
        reported_time = extract_time(details.exact_time)
        if source_time is None or reported_time != source_time:
            details = details.model_copy(update={"exact_time": None})
        log.info("email understanding completed message_id=%s body_chars=%d meeting_mentioned=%s "
                 "has_date=%s has_exact_time=%s", message.get("id", "-"), len(body), result.meeting_mentioned,
                 bool(details.date), bool(details.exact_time))
        return result.model_copy(update={"subject": subject, "meeting_details": details})


# ---------- question answering across several emails ----------
class FoundMeeting(BaseModel):
    model_config = ConfigDict(extra="ignore")

    mentioned: bool = False
    status: str = "unclear"
    title: str | None = None
    date_text: str | None = None
    time_text: str | None = None
    exact_time: str | None = None
    timezone: str | None = None
    location: str | None = None
    meeting_link: str | None = None
    organizer: str | None = None
    organizer_email: str | None = None
    attendees: list[str] = Field(default_factory=list)

    @field_validator("attendees", mode="before")
    @classmethod
    def _list(cls, v: Any) -> list[str]:
        if v is None:
            return []
        return [str(a) for a in (v if isinstance(v, list) else [v])]


class EmailFinding(BaseModel):
    model_config = ConfigDict(extra="ignore")

    email: int
    relevant: bool = False
    reason: str = ""
    evidence: str | None = None
    meeting: FoundMeeting = Field(default_factory=FoundMeeting)

    @field_validator("meeting", mode="before")
    @classmethod
    def _meeting(cls, v: Any) -> Any:
        return v or {}


class EmailAnswer(BaseModel):
    model_config = ConfigDict(extra="ignore")

    answer: str = Field(min_length=1, max_length=2000)
    findings: list[EmailFinding] = Field(default_factory=list)
    missing_information: list[str] = Field(default_factory=list)

    @field_validator("missing_information", mode="before")
    @classmethod
    def _missing(cls, v: Any) -> list[str]:
        if v is None:
            return []
        return [str(i) for i in (v if isinstance(v, list) else [v])]


def _norm(text: Any) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip().casefold()


def _grounded(fragment: Any, *sources: Any) -> str | None:
    """`fragment` if it appears verbatim (ignoring case, whitespace and surrounding quotes) in one of `sources`.
    A quote elided with "..." is grounded when every piece is."""
    if not isinstance(fragment, str):
        return None
    value = fragment.strip().strip("\"'“”‘’").strip()
    pieces = [_norm(p.strip(" .\"'“”‘’")) for p in re.split(r"\.\.\.|…", value)]
    pieces = [p for p in pieces if p]
    if not pieces:
        return None
    haystacks = [_norm(s) for s in sources]
    return value if all(any(p in h for h in haystacks) for p in pieces) else None


def render_emails(emails: list[dict[str, Any]]) -> tuple[str, int]:
    """Prompt blocks for the emails that fit the budget, and how many were included."""
    blocks: list[str] = []
    total = 0
    for i, e in enumerate(emails[:MAX_QA_EMAILS], 1):
        name, addr = parseaddr(str(e.get("sender") or ""))
        body = (e.get("body_text") or "").strip()[:MAX_QA_EMAIL_CHARS] or "[No readable body text.]"
        attachments = ", ".join(a.get("filename", "") for a in e.get("attachments") or []) or "none"
        cc = f"Cc: {e['cc']}\n" if e.get("cc") else ""
        block = (f"=== Email {i} ===\nMessage ID: {e.get('id', '')}\nThread ID: {e.get('thread_id', '')}\n"
                 f"From name: {name}\nFrom address: {addr}\nTo: {e.get('to', '')}\n{cc}"
                 f"Date: {e.get('date') or e.get('internal_date') or ''}\nSubject: {e.get('subject', '')}\n"
                 f"Attachments (content not included): {attachments}\nBody:\n{body}\n=== End of email {i} ===\n")
        if blocks and total + len(block) > MAX_QA_TOTAL_CHARS:
            break
        blocks.append(block)
        total += len(block)
    return "\n".join(blocks), len(blocks)


def _ground_meeting(meeting: FoundMeeting, email: dict[str, Any], body: str) -> FoundMeeting:
    headers = " ".join(str(email.get(k) or "") for k in ("sender", "to", "cc"))
    sender = str(email.get("sender") or "")
    sender_name, sender_addr = parseaddr(sender)
    exact = extract_time(meeting.exact_time)
    link = _grounded(meeting.meeting_link, body)
    if not link and (found := _MEETING_URL.search(body)):
        link = found.group(0)
    organizer = _grounded(meeting.organizer, body, headers)
    organizer_email = None
    if organizer:
        address = _ADDRESS.search(organizer)
        if address and is_valid_email(address.group(0)):
            organizer_email = address.group(0).lower()
        elif sender_addr and _norm(organizer) in _norm(sender):
            organizer_email = sender_addr.lower()
    attendees = [a.strip().lower() for a in meeting.attendees
                 if is_valid_email(a.strip()) and _grounded(a, body, headers)]
    return FoundMeeting(
        mentioned=True,
        status=meeting.status if meeting.status in MEETING_STATUSES else "unclear",
        title=(meeting.title or "").strip()[:120] or None,
        date_text=_grounded(meeting.date_text, body),
        time_text=_grounded(meeting.time_text, body),
        exact_time=exact if exact and exact in extract_times(body) else None,
        timezone=_grounded(meeting.timezone, body),
        location=_grounded(meeting.location, body),
        meeting_link=link,
        organizer=organizer or None,
        organizer_email=organizer_email,
        attendees=list(dict.fromkeys(attendees)),
    )


UNCONFIRMED_ANSWER = "I couldn't confirm that from the content of the matching emails."


def ground_answer(answer: EmailAnswer, emails: list[dict[str, Any]]) -> EmailAnswer:
    """Keep only what the emails support: one finding per email (by its own index), relevance backed by evidence
    quoted from that email's body, meeting facts present in that body. An email without a readable body is never
    relevant. If every relevance claim fails these checks, the model's answer is replaced rather than repeated."""
    by_index: dict[int, EmailFinding] = {}
    for f in answer.findings:
        if 1 <= f.email <= len(emails) and f.email not in by_index:
            by_index[f.email] = f
    findings, claimed = [], 0
    for i, email in enumerate(emails, 1):
        body = (email.get("body_text") or "").strip()
        f = by_index.get(i) or EmailFinding(email=i, reason="Not assessed.")
        claimed += f.relevant
        if not body:
            findings.append(EmailFinding(email=i, reason="No readable body text."))
            continue
        evidence = _grounded(f.evidence, body)
        relevant = f.relevant and evidence is not None
        reason = f.reason.strip()[:300] if relevant or not f.relevant else "Not supported by the email's content."
        meeting = _ground_meeting(f.meeting, email, body) if relevant and f.meeting.mentioned else FoundMeeting()
        findings.append(f.model_copy(update={"relevant": relevant, "reason": reason, "evidence": evidence,
                                             "meeting": meeting}))
    text = answer.answer.strip()
    grounded = sum(1 for f in findings if f.relevant)
    if claimed and not grounded:
        text = UNCONFIRMED_ANSWER
    log.info("email answer grounded emails=%d empty_bodies=%d claimed_relevant=%d grounded_relevant=%d "
             "meetings=%d answer_replaced=%s", len(emails),
             sum(1 for e in emails if not (e.get("body_text") or "").strip()), claimed, grounded,
             sum(1 for f in findings if f.meeting.mentioned), bool(claimed and not grounded))
    missing = [m.strip()[:200] for m in answer.missing_information if m.strip()][:5]
    return answer.model_copy(update={"answer": text, "findings": findings, "missing_information": missing})


class EmailQuestionAgent:
    def __init__(self, llm: LLMRouter, call_log: list[dict] | None = None):
        self.llm = llm
        self.call_log = call_log if call_log is not None else []

    def answer(self, question: str, emails: list[dict[str, Any]]) -> tuple[EmailAnswer, int]:
        """Answer `question` from the complete `emails`. Returns the grounded answer and how many emails it covers."""
        rendered, count = render_emails(emails)
        messages = [
            ChatMessage("system", EMAIL_QA_SYSTEM.text),
            ChatMessage("user", EMAIL_QA_USER.render(question=question.strip()[:2000], emails=rendered)),
        ]
        self.call_log.append(self.llm.call_record(LLMRole.EMAIL_UNDERSTANDING))
        result = complete_structured(self.llm, LLMRole.EMAIL_UNDERSTANDING, messages, EmailAnswer)
        return ground_answer(result, emails[:count]), count
