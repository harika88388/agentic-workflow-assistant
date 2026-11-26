"""Summarization Agent: condenses email content with the SUMMARIZATION model role."""
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.services.llm.base import ChatMessage
from app.services.llm.prompts import SUMMARY_SYSTEM, SUMMARY_USER
from app.services.llm.router import LLMRole, LLMRouter
from app.services.llm.structured import complete_structured

MAX_EMAILS = 5
MAX_EMAIL_CHARS = 3000
MAX_TOTAL_CHARS = 12000


class EmailSummary(BaseModel):
    model_config = ConfigDict(extra="ignore")

    summary: str = Field(min_length=1, max_length=2000)
    key_points: list[str] = Field(default_factory=list, max_length=8)
    action_items: list[str] = Field(default_factory=list, max_length=8)

    @field_validator("summary", mode="before")
    @classmethod
    def _strip(cls, v: Any) -> Any:
        return v.strip() if isinstance(v, str) else v

    @field_validator("key_points", "action_items", mode="before")
    @classmethod
    def _items(cls, v: Any) -> list[str]:
        if v is None:
            return []
        if isinstance(v, str):
            v = [v]
        return [str(i).strip()[:300] for i in v if str(i).strip()]


def _render(emails: list[dict[str, Any]]) -> str:
    blocks, total = [], 0
    for i, e in enumerate(emails[:MAX_EMAILS], 1):
        body = (e.get("body_text") or e.get("snippet") or "")[:MAX_EMAIL_CHARS]
        block = (f"--- email {i} ---\nFrom: {e.get('sender', '')}\nTo: {e.get('to', '')}\n"
                 f"Date: {e.get('date', '')}\nSubject: {e.get('subject', '')}\n\n{body}\n")
        if total + len(block) > MAX_TOTAL_CHARS:
            break
        blocks.append(block)
        total += len(block)
    return "\n".join(blocks)


class SummarizationAgent:
    def __init__(self, llm: LLMRouter, call_log: list[dict] | None = None):
        self.llm, self.call_log = llm, call_log if call_log is not None else []

    def summarize(self, emails: list[dict[str, Any]], *, focus: str | None = None) -> EmailSummary:
        if not emails:
            raise ValueError("no emails to summarize")
        focus_line = f"The user asked: {focus.strip()[:300]}\n" if focus and focus.strip() else ""
        messages = [
            ChatMessage("system", SUMMARY_SYSTEM.render(focus_line=focus_line)),
            ChatMessage("user", SUMMARY_USER.render(emails=_render(emails))),
        ]
        self.call_log.append(self.llm.call_record(LLMRole.SUMMARIZATION))
        return complete_structured(self.llm, LLMRole.SUMMARIZATION, messages, EmailSummary)
