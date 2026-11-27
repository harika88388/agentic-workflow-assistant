"""Structured analysis of one complete, untrusted Gmail message."""
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.agents.dates import extract_time
from app.services.llm.base import ChatMessage
from app.services.llm.prompts import EMAIL_UNDERSTANDING_SYSTEM, EMAIL_UNDERSTANDING_USER
from app.services.llm.router import LLMRole, LLMRouter
from app.services.llm.structured import complete_structured


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
        return result.model_copy(update={"subject": subject, "meeting_details": details})
