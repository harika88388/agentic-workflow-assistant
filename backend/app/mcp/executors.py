"""Approved actions executed through MCP.

These objects expose the method names `workflow_service.execute` already calls on GmailService/CalendarService, but
each call becomes an MCP `tools/call`. A tool error raises MCPToolError, which `execute` records as `failed`.
"""
from datetime import datetime
from typing import Any

from app.mcp.client import MCPGateway


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


class MCPGmailExecutor:
    def __init__(self, gateway: MCPGateway):
        self.gateway = gateway

    def send_email(self, to: list[str], subject: str, body: str, cc: list[str] | None = None,
                   bcc: list[str] | None = None) -> dict[str, Any]:
        return self.gateway.call_tool("gmail", "send_email",
                                      {"to": to, "subject": subject, "body": body, "cc": cc, "bcc": bcc})

    def create_draft(self, to: list[str], subject: str, body: str, thread_id: str | None = None) -> dict[str, Any]:
        return self.gateway.call_tool("gmail", "create_draft",
                                      {"to": to, "subject": subject, "body": body, "thread_id": thread_id})

    def reply_email(self, body: str, *, message_id: str | None = None, thread_id: str | None = None) -> dict[str, Any]:
        return self.gateway.call_tool("gmail", "reply_email",
                                      {"body": body, "message_id": message_id, "thread_id": thread_id})


class MCPCalendarExecutor:
    def __init__(self, gateway: MCPGateway):
        self.gateway = gateway

    def create_event(self, *, title: str, start: datetime, end: datetime | None = None, duration_minutes: int = 60,
                     attendees: list[str] | None = None, description: str | None = None,
                     location: str | None = None, idempotency_key: str | None = None) -> dict[str, Any]:
        return self.gateway.call_tool("calendar", "create_event", {
            "title": title, "start": _iso(start), "end": _iso(end), "duration_minutes": duration_minutes,
            "attendees": attendees, "description": description, "location": location,
            "idempotency_key": idempotency_key,
        })

    def reschedule_event(self, event_id: str, new_start: datetime, new_end: datetime | None = None) -> dict[str, Any]:
        return self.gateway.call_tool("calendar", "update_event",
                                      {"event_id": event_id, "new_start": _iso(new_start), "new_end": _iso(new_end)})

    def cancel_event(self, event_id: str) -> dict[str, Any]:
        return self.gateway.call_tool("calendar", "delete_event", {"event_id": event_id})
