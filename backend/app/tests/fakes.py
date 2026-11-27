"""Shared fakes: real GmailService/CalendarService over fake Google clients, a role-aware fake LLM, and an in-process
MCP gateway factory. No network, no credentials."""
import json
from contextlib import contextmanager
from datetime import datetime

from cryptography.fernet import Fernet

from app.core.config import Settings
from app.integrations.calendar.service import CalendarService
from app.integrations.calendar.timeutil import get_zone
from app.integrations.errors import GoogleNotFoundError
from app.integrations.gmail.mime import EmailMessageData, EmailThreadData
from app.integrations.gmail.service import GmailService
from app.mcp.client import MCPGateway, inprocess_factories
from app.services.llm.router import LLMRouter
from app.tests.test_calendar_service import FakeCal

TZ = get_zone("Asia/Kolkata")
NOW = datetime(2026, 11, 24, 12, 0, tzinfo=TZ)  # Tuesday


class FakeGmailClient:
    """Mimics GmailClient: a small mailbox plus recorded sends/drafts."""

    def __init__(self):
        self.messages: dict[str, EmailMessageData] = {}
        self.sent: list[tuple[str, str | None]] = []
        self.drafts: list[str] = []
        self.searches: list[str] = []

    def add(self, id, sender, subject, body, internal_date, thread_id=None, labels=("INBOX",)):
        self.messages[id] = EmailMessageData(
            id=id, thread_id=thread_id or f"t-{id}", sender=sender, to="me@example.com", subject=subject,
            date=internal_date[:10], internal_date=internal_date, snippet=body[:60], labels=list(labels),
            body_text=body, message_id_header=f"<{id}@mail>")

    def search(self, query, max_results=10):
        self.searches.append(query)
        words = [w.strip('"').casefold() for w in query.replace("from:", "").split() if ":" not in w]
        hits = [m for m in self.messages.values()
                if all(w in (m.sender + " " + m.subject + " " + m.body_text).casefold() for w in words)]
        hits.sort(key=lambda m: m.internal_date or "", reverse=True)
        return [EmailMessageData(**{**m.__dict__, "body_text": ""}) for m in hits[:max_results]]

    def get_message(self, mid):
        if mid not in self.messages:
            raise GoogleNotFoundError("not found", 404)
        return self.messages[mid]

    def get_thread(self, tid):
        return EmailThreadData(tid, [m for m in self.messages.values() if m.thread_id == tid])

    def send_raw(self, raw, thread_id=None):
        self.sent.append((raw, thread_id))
        return {"id": f"sent-{len(self.sent)}", "threadId": thread_id or f"t-sent-{len(self.sent)}"}

    def create_draft_raw(self, raw, thread_id=None):
        self.drafts.append(raw)
        return {"id": f"d{len(self.drafts)}", "message": {"id": "dm", "threadId": thread_id or "td"}}

    def get_labels(self, mid):
        return ["SENT"]

    def draft_exists(self, did):
        return True


def gmail_service(client: FakeGmailClient | None = None) -> GmailService:
    return GmailService(client or FakeGmailClient())


def calendar_service(cal: FakeCal | None = None) -> CalendarService:
    return CalendarService(cal or FakeCal(), TZ, now=lambda: NOW)


def _scope(service):
    @contextmanager
    def scope():
        yield service
    return scope


def gateway_factory(gmail: GmailService, calendar: CalendarService):
    """(db, user) -> MCPGateway speaking real MCP to in-process servers wrapping the given services."""
    return lambda db, user: MCPGateway(inprocess_factories(_scope(gmail), _scope(calendar)), transport="inprocess")


class ScriptedLLM:
    """Fake provider. Replies are chosen by the component named in the system prompt, so a test can script the
    intent, summarization and task-automation roles independently. Every call records the model it was routed to."""

    name = "openrouter"

    def __init__(self, intents=(), summary=None, task=None, email_understanding=None):
        self.intents = list(intents)
        self.summary, self.task, self.email_understanding = summary, task, email_understanding
        self.calls: list[dict] = []

    def complete(self, messages, *, model, temperature, max_tokens, json_mode=False):
        system = messages[0].content
        role = ("intent" if "Intent Recognition" in system else
                "email_understanding" if "Email Understanding" in system else
                "summary" if "Summarization" in system else
                "task" if "Task Automation" in system else "other")
        self.calls.append({"role": role, "model": model, "messages": messages})
        if role == "intent":
            reply = self.intents.pop(0)
        elif role == "email_understanding":
            reply = self.email_understanding
        else:
            reply = self.summary if role == "summary" else self.task
        if isinstance(reply, Exception):
            raise reply
        return reply if isinstance(reply, str) else json.dumps(reply)


def settings(**overrides) -> Settings:
    return Settings(jwt_secret="x" * 48, token_encryption_key=Fernet.generate_key().decode(), _env_file=None, **overrides)


def router(provider, **overrides) -> LLMRouter:
    return LLMRouter(settings(**overrides), {"openrouter": provider, "gemini": provider})
