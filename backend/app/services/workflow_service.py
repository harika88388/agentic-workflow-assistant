import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database.models import Conversation, User, WorkflowExecution
from app.integrations.calendar.service import CalendarService
from app.integrations.gmail.service import GmailService

GMAIL_INTENTS = {"send_email", "create_draft", "reply_email"}
CALENDAR_INTENTS = {"create_event", "reschedule_event", "cancel_event"}


def _owned_workflow(db: Session, user: User, execution_id: uuid.UUID) -> WorkflowExecution:
    execution = db.scalar(select(WorkflowExecution).where(
        WorkflowExecution.id == execution_id, WorkflowExecution.user_id == user.id
    ))
    if execution is None:
        raise ValueError("workflow not found")
    return execution


def propose(db: Session, user: User, *, conversation_id: uuid.UUID, service: str, intent: str,
            request: dict[str, Any], idempotency_key: str) -> WorkflowExecution:
    if service == "gmail" and intent not in GMAIL_INTENTS:
        raise ValueError("unsupported Gmail action")
    if service == "calendar" and intent not in CALENDAR_INTENTS:
        raise ValueError("unsupported Calendar action")
    conversation = db.scalar(select(Conversation).where(
        Conversation.id == conversation_id, Conversation.user_id == user.id
    ))
    if conversation is None:
        raise ValueError("conversation not found")
    existing = db.scalar(select(WorkflowExecution).where(
        WorkflowExecution.user_id == user.id, WorkflowExecution.idempotency_key == idempotency_key
    ))
    if existing is not None:
        return existing
    execution = WorkflowExecution(
        user_id=user.id, conversation_id=conversation_id, service=service, intent=intent,
        idempotency_key=idempotency_key, request=request, status="awaiting_approval"
    )
    db.add(execution)
    db.commit()
    db.refresh(execution)
    return execution


def reject(db: Session, user: User, execution_id: uuid.UUID) -> WorkflowExecution:
    execution = _owned_workflow(db, user, execution_id)
    if execution.status != "awaiting_approval":
        raise ValueError("workflow is no longer awaiting approval")
    execution.status = "rejected"
    db.commit()
    db.refresh(execution)
    return execution


def _result(value: Any) -> dict[str, Any]:
    if hasattr(value, "__dataclass_fields__"):
        from dataclasses import asdict
        return asdict(value)
    return {"value": value}


def execute(db: Session, user: User, execution_id: uuid.UUID, *, gmail: GmailService | None = None,
            calendar: CalendarService | None = None) -> WorkflowExecution:
    execution = _owned_workflow(db, user, execution_id)
    if execution.status != "awaiting_approval":
        raise ValueError("workflow is no longer awaiting approval")
    execution.status = "running"
    db.commit()
    payload = execution.request or {}
    try:
        if execution.service == "gmail":
            if gmail is None:
                raise ValueError("Gmail service is required")
            if execution.intent == "send_email":
                value = gmail.send_email(payload["to"], payload.get("subject", ""), payload["body"], payload.get("cc"), payload.get("bcc"))
            elif execution.intent == "create_draft":
                value = gmail.create_draft(payload["to"], payload.get("subject", ""), payload["body"], payload.get("thread_id"))
            else:
                value = gmail.reply_email(payload["body"], message_id=payload.get("message_id"), thread_id=payload.get("thread_id"))
        else:
            if calendar is None:
                raise ValueError("Calendar service is required")
            if execution.intent == "create_event":
                value = calendar.create_event(
                    title=payload["title"], start=datetime.fromisoformat(payload["start"]),
                    end=datetime.fromisoformat(payload["end"]) if payload.get("end") else None,
                    duration_minutes=payload.get("duration_minutes", 60), attendees=payload.get("attendees"),
                    description=payload.get("description"), location=payload.get("location"),
                    idempotency_key=str(execution.id),
                )
            elif execution.intent == "reschedule_event":
                value = calendar.reschedule_event(payload["event_id"], datetime.fromisoformat(payload["new_start"]),
                                                  datetime.fromisoformat(payload["new_end"]) if payload.get("new_end") else None)
            else:
                value = calendar.cancel_event(payload["event_id"])
        execution.status = "succeeded"
        execution.result = _result(value)
        execution.error = None
    except Exception as exc:  # noqa: BLE001
        execution.status = "failed"
        execution.error = str(exc)
        execution.result = None
    db.commit()
    db.refresh(execution)
    return execution
