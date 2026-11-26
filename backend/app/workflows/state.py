"""LangGraph state and per-run dependencies for the chat workflow."""
import operator
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Annotated, Any, Literal, TypedDict
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from app.agents.base import OperationAgent
from app.agents.context_agent import ContextEnhancementAgent, ConversationContext
from app.agents.intent_agent import IntentRecognitionAgent
from app.agents.service_selector import ServiceSelectorAgent
from app.database.models import Conversation, User
from app.mcp.client import MCPGateway
from app.services.llm.router import LLMRouter

Status = Literal["running", "needs_clarification", "awaiting_approval", "completed", "failed"]


class WorkflowState(TypedDict, total=False):
    message: str
    message_id: str
    selection: dict[str, Any]  # Service Selector output (pre-intent, then reconciled)
    intent: dict[str, Any]  # Intent Recognition output (IntentResult)
    request: dict[str, Any]  # Context Enhancement output (EnhancedRequest), normalized by validation
    service: str
    missing: list[str]
    tool_results: dict[str, Any]  # MCP results keyed by the agent's ToolCall.key
    iterations: int
    outcome: dict[str, Any]  # last AgentOutcome
    workflow: dict[str, Any] | None  # approval record, when one was proposed
    status: Status
    response: str
    error: dict[str, Any] | None
    assistant_message_id: str
    trace: Annotated[list[dict[str, Any]], operator.add]  # execution status of every node, in order


@dataclass
class WorkflowDeps:
    db: Session
    user: User
    conversation: Conversation
    llm: LLMRouter
    gateway: MCPGateway
    tz: ZoneInfo
    now: datetime
    selector: ServiceSelectorAgent
    context_agent: ContextEnhancementAgent
    intent_agent: IntentRecognitionAgent
    agents: dict[str, OperationAgent]
    llm_calls: list[dict[str, Any]]
    ctx: ConversationContext | None = None
    user_message_id: uuid.UUID | None = None
    extra: dict[str, Any] = field(default_factory=dict)
