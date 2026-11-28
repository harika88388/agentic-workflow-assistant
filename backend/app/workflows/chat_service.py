"""Runs one conversation turn through the LangGraph workflow, and records approval decisions in the conversation."""
import logging
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agents.calendar_agents import build_calendar_agents
from app.agents.context_agent import ContextEnhancementAgent
from app.agents.gmail_agents import build_gmail_agents
from app.agents.intent_agent import MAX_MESSAGE_CHARS, IntentRecognitionAgent
from app.agents.service_selector import ServiceSelectorAgent
from app.agents.summarization_agent import SummarizationAgent
from app.agents.task_agent import TaskAutomationAgent
from app.api.deps import user_zone
from app.core.config import get_settings
from app.core.logging_context import current_correlation_id, log_context
from app.database.models import Conversation, Message, User, WorkflowExecution
from app.mcp.client import MCPGateway
from app.services import execution_history
from app.services.llm.base import llm_time_budget
from app.services.llm.router import LLMRouter, collect_llm_metrics
from app.workflows.graph import get_graph
from app.workflows.state import WorkflowDeps

GatewayFactory = Callable[[Session, User], MCPGateway]
ProgressCallback = Callable[[dict[str, Any]], None]
DEFAULT_TITLES = {"New conversation", "New workspace"}
log = logging.getLogger(__name__)


class ConversationNotFound(Exception):
    pass


def clock(tz) -> datetime:
    return datetime.now(tz)


@dataclass
class TurnResult:
    user_message: Message
    assistant_message: Message
    status: str
    workflow: WorkflowExecution | None
    execution_id: uuid.UUID | None = None


def owned_conversation(db: Session, user: User, conversation_id: uuid.UUID) -> Conversation:
    conv = db.scalar(select(Conversation).where(Conversation.id == conversation_id, Conversation.user_id == user.id))
    if conv is None:
        raise ConversationNotFound()
    return conv


def _elapsed(started: float) -> int:
    return round((time.perf_counter() - started) * 1000)


def run_turn(db: Session, user: User, conversation_id: uuid.UUID, content: str, *, llm: LLMRouter,
             gateway_factory: GatewayFactory, now: datetime | None = None,
             on_event: ProgressCallback | None = None) -> TurnResult:
    """Run one turn through the LangGraph workflow. `on_event` (optional) receives progress events as nodes and
    MCP tools run, for streaming to the UI; it must not raise (failures are logged and ignored)."""
    started = time.perf_counter()
    content = (content or "").strip()
    if not content or len(content) > MAX_MESSAGE_CHARS:
        raise ValueError(f"message must be 1-{MAX_MESSAGE_CHARS} characters")
    conversation = owned_conversation(db, user, conversation_id)
    user_msg = Message(conversation_id=conversation.id, role="user", content=content)
    db.add(user_msg)
    if conversation.title in DEFAULT_TITLES:
        conversation.title = content[:60] + ("..." if len(content) > 60 else "")
    conversation.updated_at = datetime.now(timezone.utc)
    db.commit()

    tz = user_zone(user)
    llm_calls: list[dict[str, Any]] = []
    existing_id = current_correlation_id()
    request_id = existing_id if existing_id != "-" else str(user_msg.id)
    emit = _safe_emitter(on_event)
    with log_context(request_id=request_id, user_id=str(user.id), conversation_id=str(conversation.id)) as ctx, \
            collect_llm_metrics() as llm_metrics:
        run = execution_history.start(db, user, conversation_id=conversation.id, user_message_id=user_msg.id,
                                      request_id=request_id, request_text=content)
        if run is not None:
            ctx["execution_id"] = str(run.id)
        log.info("user request submitted request_id=%s message_chars=%d", request_id, len(content))
        log.info("workflow execution started request_id=%s", request_id)
        emit({"type": "started", "request_id": request_id, "execution_id": str(run.id) if run else None,
              "user_message_id": str(user_msg.id)})
        gateway: MCPGateway | None = None
        try:
            with gateway_factory(db, user) as gateway:
                deps = WorkflowDeps(
                    db=db, user=user, conversation=conversation, llm=llm, gateway=gateway, tz=tz,
                    now=(now or clock(tz)).astimezone(tz), selector=ServiceSelectorAgent(),
                    context_agent=ContextEnhancementAgent(), intent_agent=IntentRecognitionAgent(llm),
                    agents={**build_gmail_agents(SummarizationAgent(llm, llm_calls),
                                                 TaskAutomationAgent(llm, llm_calls)),
                            **build_calendar_agents()},
                    llm_calls=llm_calls, user_message_id=user_msg.id, progress=emit,
                )
                with llm_time_budget(get_settings().llm_turn_budget_seconds):
                    state = get_graph().invoke({"message": content, "message_id": str(user_msg.id), "trace": []},
                                               config={"configurable": {"deps": deps}, "recursion_limit": 60})
        except Exception as exc:
            log.error("workflow execution raised request_id=%s exception_type=%s duration_ms=%d",
                      request_id, type(exc).__name__, _elapsed(started), exc_info=True)
            execution_history.fail(db, run, code="internal_error", message=type(exc).__name__,
                                   duration_ms=_elapsed(started), mcp_calls=gateway.calls if gateway else [],
                                   llm_metrics=list(llm_metrics))
            raise
        if not state.get("assistant_message_id"):
            db.rollback()
            log.error("workflow result missing assistant message request_id=%s duration_ms=%d",
                      request_id, _elapsed(started))
            execution_history.fail(db, run, code="persist_failed", message="the conversation turn could not be saved",
                                   duration_ms=_elapsed(started), mcp_calls=gateway.calls, llm_metrics=list(llm_metrics))
            raise RuntimeError("the conversation turn could not be saved")
        assistant = db.get(Message, uuid.UUID(state["assistant_message_id"]))
        workflow = db.get(WorkflowExecution, uuid.UUID(state["workflow"]["id"])) if state.get("workflow") else None
        intent = (state.get("request") or {}).get("intent")
        agent = type(deps.agents[intent]).__name__ if intent in deps.agents and any(
            t["node"] in ("gmail_agent", "calendar_agent") for t in state.get("trace", [])) else None
        execution_history.finish(db, run, state=state, agent=agent, mcp_calls=gateway.calls,
                                 llm_metrics=list(llm_metrics), duration_ms=_elapsed(started))
        log.info("workflow execution finished request_id=%s status=%s workflow_id=%s duration_ms=%d",
                 request_id, state["status"], workflow.id if workflow else "-", _elapsed(started))
        return TurnResult(user_msg, assistant, state["status"], workflow, run.id if run else None)  # type: ignore[arg-type]


def _safe_emitter(on_event: ProgressCallback | None) -> ProgressCallback:
    def emit(event: dict[str, Any]) -> None:
        if on_event is None:
            return
        try:
            on_event(event)
        except Exception as exc:  # noqa: BLE001 - a broken progress consumer must not fail the turn
            log.warning("progress event delivery failed exception_type=%s", type(exc).__name__)
    return emit


def _result_text(execution: WorkflowExecution) -> str:
    req = execution.request or {}
    if execution.status == "rejected":
        return "Okay, I won't do that. The action was cancelled."
    if execution.status == "failed":
        return f"That didn't go through: {execution.error}. Nothing was reported as done."
    value = (execution.result or {}).get("value", execution.result or {})
    verified = value.get("verified", False)
    caveat = "" if verified else f" However, Google could not confirm it ({value.get('note') or 'unverified'})."
    if execution.intent == "send_email":
        return f"Email sent to {', '.join(value.get('to') or req.get('to', []))}.{caveat}"
    if execution.intent == "reply_email":
        return f"Reply sent to {', '.join(value.get('to') or [])}.{caveat}"
    if execution.intent == "create_draft":
        return f"Draft saved for {', '.join(value.get('to') or req.get('to', []))}.{caveat}"
    event = value.get("event") or {}
    if execution.intent == "create_event":
        return f"Created \"{event.get('summary')}\" starting {event.get('start')}." + (" (It already existed.)" if value.get("duplicate") else "")
    if execution.intent == "reschedule_event":
        return f"Moved \"{event.get('summary')}\" to {event.get('start')}."
    if execution.intent == "cancel_event":
        return f"Cancelled \"{event.get('summary') or req.get('event_summary')}\"."
    return "Done."


def record_decision(db: Session, execution: WorkflowExecution) -> Message:
    """Append the real outcome of an approval decision to the conversation and update its memory."""
    from app.agents.context_agent import ConversationContext
    from app.database.models import SessionContext

    value = (execution.result or {}).get("value", execution.result or {})
    memory: dict[str, Any] = {}
    if execution.status == "succeeded" and execution.service == "calendar" and (value.get("event") or {}).get("id"):
        memory["last_event_id"] = value["event"]["id"]
    if execution.status == "succeeded" and execution.service == "gmail" and value.get("id"):
        memory["last_sent_message_id"] = value["id"]
    sc = db.scalar(select(SessionContext).where(SessionContext.conversation_id == execution.conversation_id))
    if memory:
        ctx = ConversationContext(execution.conversation_id, [], {}, sc.pending_intent if sc else None,
                                  sc.reference_date if sc and sc.reference_date else datetime.now().date(),
                                  sc.timezone if sc and sc.timezone else "UTC")
        ContextEnhancementAgent.save(db, ctx, memory_updates=memory, pending=ctx.pending)
    msg = Message(conversation_id=execution.conversation_id, role="assistant", content=_result_text(execution),
                  meta={"status": execution.status, "service": execution.service, "intent": execution.intent,
                        "workflow": {"id": str(execution.id), "status": execution.status},
                        "result": execution.result, "error": execution.error})
    db.add(msg)
    db.commit()
    return msg
