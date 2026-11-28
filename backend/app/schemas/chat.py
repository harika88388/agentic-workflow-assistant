import uuid

from pydantic import BaseModel, Field

from app.agents.intent_agent import MAX_MESSAGE_CHARS
from app.schemas.conversation import MessageOut
from app.schemas.workflow import WorkflowOut


class ChatRequest(BaseModel):
    content: str = Field(min_length=1, max_length=MAX_MESSAGE_CHARS)


class ChatTurnOut(BaseModel):
    status: str  # needs_clarification | awaiting_approval | completed | failed
    user_message: MessageOut
    assistant_message: MessageOut
    workflow: WorkflowOut | None = None
    execution_id: uuid.UUID | None = None


class DecisionOut(BaseModel):
    workflow: WorkflowOut
    assistant_message: MessageOut
