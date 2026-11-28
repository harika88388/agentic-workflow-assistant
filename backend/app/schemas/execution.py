import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict


class ExecutionSummaryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    conversation_id: uuid.UUID
    request_id: str
    request_text: str
    status: str
    intent: str | None
    service: str | None
    agent: str | None
    approval_status: str | None
    action_status: str | None
    workflow_id: uuid.UUID | None
    error_code: str | None
    duration_ms: int | None
    tool_count: int = 0
    created_at: datetime
    completed_at: datetime | None


class ExecutionDetailOut(ExecutionSummaryOut):
    user_message_id: uuid.UUID | None
    assistant_message_id: uuid.UUID | None
    error_message: str | None
    steps: list[dict[str, Any]]
    tools: list[dict[str, Any]]
    llm_calls: list[dict[str, Any]]
