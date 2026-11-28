"""Intent recognition schemas. Deterministic code (not the LLM) decides what is missing."""
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.core.validation import is_valid_email, normalize_time

GMAIL_INTENTS = ("send_email", "search_emails", "read_email", "reply_email", "summarize_emails", "create_draft")
CALENDAR_INTENTS = ("create_event", "get_events", "reschedule_event", "cancel_event", "check_availability")
INTENT_TO_SERVICE = {**{i: "gmail" for i in GMAIL_INTENTS}, **{i: "calendar" for i in CALENDAR_INTENTS}}

Service = Literal["gmail", "calendar", "unknown"]
Intent = Literal[
    "send_email", "search_emails", "read_email", "reply_email", "summarize_emails", "create_draft",
    "create_event", "get_events", "reschedule_event", "cancel_event", "check_availability", "unknown",
]

_SERVICE_ALIASES = {"email": "gmail", "mail": "gmail", "google_calendar": "calendar", "gcal": "calendar"}

# intent -> [(missing-parameter label, entity keys any one of which satisfies it)]
REQUIREMENTS: dict[str, list[tuple[str, tuple[str, ...]]]] = {
    "send_email": [("recipient_email", ("recipient_email",)), ("body", ("body",))],
    "create_draft": [("recipient_email", ("recipient_email",)), ("body", ("body",))],
    "reply_email": [("email_reference", ("message_id", "query", "person", "recipient_email")), ("body", ("body",))],
    "search_emails": [("query", ("query", "person", "recipient_email"))],
    "read_email": [("email_reference", ("message_id", "query", "person", "recipient_email"))],
    "summarize_emails": [],
    "create_event": [("title_or_person", ("title", "person", "attendees")), ("date", ("date",)), ("time", ("time",))],
    "get_events": [],
    "reschedule_event": [("event_reference", ("person", "title", "event_id")), ("new_date_or_time", ("date", "time"))],
    "cancel_event": [("event_reference", ("person", "title", "event_id"))],
    "check_availability": [("date", ("date",))],
}

QUESTIONS = {
    "recipient_email": "What is the recipient's full email address?",
    "body": "What should the message say?",
    "query": "What should I search for?",
    "email_reference": "Which email do you mean? Tell me the sender, subject or some keywords.",
    "title_or_person": "Who is the meeting with, or what is it called?",
    "date": "Which date?",
    "time": "What time?",
    "event_reference": "Which event do you mean? Give me the person, title or date.",
    "new_date_or_time": "When should it be moved to?",
}


class Entities(BaseModel):
    model_config = ConfigDict(extra="ignore")

    person: str | None = None
    recipient_email: str | None = None
    attendees: list[str] = Field(default_factory=list)
    subject: str | None = None
    body: str | None = None
    title: str | None = None
    date: str | None = None
    time: str | None = None
    end_time: str | None = None
    duration_minutes: int | None = Field(default=None, ge=1, le=24 * 60)
    location: str | None = Field(default=None, max_length=500)
    description: str | None = Field(default=None, max_length=2000)
    query: str | None = None
    message_id: str | None = None
    event_id: str | None = None

    @field_validator("*", mode="before")
    @classmethod
    def _blank_to_none(cls, v):
        if isinstance(v, str):
            v = v.strip()
            return v or None
        return v

    @field_validator("recipient_email")
    @classmethod
    def _email(cls, v):
        return v.lower() if v and is_valid_email(v) else None  # a name in this field is dropped, never trusted

    @field_validator("attendees", mode="before")
    @classmethod
    def _attendees(cls, v):
        if v is None:
            return []
        if isinstance(v, str):
            v = [v]
        return [a.strip().lower() for a in v if isinstance(a, str) and is_valid_email(a.strip())]

    @field_validator("time", "end_time", mode="before")
    @classmethod
    def _time(cls, v):
        return normalize_time(v)

    @field_validator("duration_minutes", mode="before")
    @classmethod
    def _duration(cls, v):
        try:
            return int(v) if v not in (None, "") else None
        except (TypeError, ValueError):
            return None

    def present(self, key: str) -> bool:
        val = getattr(self, key)
        return bool(val)


class IntentResult(BaseModel):
    model_config = ConfigDict(extra="ignore")

    service: Service = "unknown"
    intent: Intent = "unknown"
    entities: Entities = Field(default_factory=Entities)
    missing_parameters: list[str] = Field(default_factory=list)
    requires_clarification: bool = False
    clarification_question: str | None = None
    is_follow_up: bool = False
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)

    @model_validator(mode="before")
    @classmethod
    def _normalise(cls, data):
        if not isinstance(data, dict):
            raise ValueError("intent result must be a JSON object")
        d = dict(data)
        intent = str(d.get("intent") or "unknown").strip().lower()
        d["intent"] = intent if intent in INTENT_TO_SERVICE else "unknown"
        service = str(d.get("service") or "unknown").strip().lower()
        d["service"] = _SERVICE_ALIASES.get(service, service)
        d["entities"] = d.get("entities") or {}
        if d["intent"] in INTENT_TO_SERVICE:  # the intent is more specific than the service: derive it
            d["service"] = INTENT_TO_SERVICE[d["intent"]]
        elif d["service"] not in ("gmail", "calendar"):
            d["service"] = "unknown"
        if d["intent"] == "unknown":
            d["service"] = "unknown"
        if isinstance(d.get("confidence"), (int, float)):
            d["confidence"] = max(0.0, min(1.0, float(d["confidence"])))
        elif d.get("confidence") is not None:
            d["confidence"] = None
        return d

    def enforce_requirements(self) -> "IntentResult":
        """Recompute missing parameters from the entities; ignore the LLM's own claim about what is missing."""
        if self.intent == "unknown":
            return self.model_copy(update={
                "missing_parameters": [], "requires_clarification": True,
                "clarification_question": self.clarification_question
                or "I can help with Gmail and Google Calendar. What would you like to do?",
            })
        missing = [label for label, keys in REQUIREMENTS[self.intent]
                   if not any(self.entities.present(k) for k in keys)]
        question = self.clarification_question
        if missing:
            question = question if (question and self.requires_clarification) else " ".join(QUESTIONS[m] for m in missing)
        elif not (self.requires_clarification and question):
            question = None
        return self.model_copy(update={
            "missing_parameters": missing,
            "requires_clarification": bool(missing) or bool(self.requires_clarification and question),
            "clarification_question": question,
        })
