"""Contract shared by the specialized Gmail and Calendar agents.

An agent never touches Google directly. Each `step()` looks at the request and at the MCP results gathered so far,
and returns either more MCP tool calls (executed by the workflow's MCP node) or a final outcome: a read result, a
clarification question, an approval proposal for a consequential action, or an error.
"""
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from email.utils import parseaddr
from typing import Any, ClassVar, Literal
from zoneinfo import ZoneInfo

from app.integrations.calendar.timeutil import parse_iso

OutcomeKind = Literal["tool_calls", "result", "clarify", "proposal", "error"]


@dataclass
class ToolCall:
    key: str  # where the result is stored in AgentContext.tool_results
    server: Literal["gmail", "calendar"]
    tool: str
    arguments: dict[str, Any]


@dataclass
class Proposal:
    service: str
    intent: str
    request: dict[str, Any]  # validated payload executed after approval
    preview: str  # what the user approves


@dataclass
class AgentOutcome:
    kind: OutcomeKind
    message: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    data: dict[str, Any] = field(default_factory=dict)
    proposal: Proposal | None = None
    pending: dict[str, Any] | None = None  # for clarifications: what to carry into the next turn
    memory: dict[str, Any] = field(default_factory=dict)  # conversation memory updates
    error_code: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def calls(cls, *calls: ToolCall) -> "AgentOutcome":
        return cls("tool_calls", tool_calls=list(calls))

    @classmethod
    def error(cls, code: str, message: str) -> "AgentOutcome":
        return cls("error", message=message, error_code=code)


@dataclass
class AgentContext:
    intent: str
    entities: dict[str, Any]
    message: str
    tz: ZoneInfo
    now: datetime
    memory: dict[str, Any] = field(default_factory=dict)
    pending: dict[str, Any] | None = None
    tool_results: dict[str, Any] = field(default_factory=dict)
    user_name: str = ""

    @property
    def reference_date(self) -> date:
        return self.now.date()

    def get(self, key: str) -> Any:
        return self.entities.get(key)


class OperationAgent(ABC):
    service: ClassVar[str]
    intent: ClassVar[str]
    description: ClassVar[str]

    @abstractmethod
    def step(self, ctx: AgentContext) -> AgentOutcome: ...


# ---------- shared formatting ----------
def fmt_when(value: str, tz: ZoneInfo) -> str:
    if not value:
        return ""
    v = parse_iso(value, tz)
    if isinstance(v, datetime):
        return v.astimezone(tz).strftime("%a %d %b, %H:%M")
    return v.strftime("%a %d %b (all day)")


def email_address(raw: str) -> str:
    return parseaddr(raw or "")[1].lower()


def compact_email(m: dict[str, Any]) -> dict[str, Any]:
    return {k: m.get(k, "") for k in ("id", "thread_id", "sender", "subject", "date", "internal_date", "snippet")}


def compact_event(e: dict[str, Any]) -> dict[str, Any]:
    return {"id": e.get("id"), "summary": e.get("summary"), "start": e.get("start"), "end": e.get("end"),
            "attendees": [a.get("email") for a in e.get("attendees") or []][:10], "html_link": e.get("html_link", "")}
