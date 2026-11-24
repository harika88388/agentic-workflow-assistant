import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class WorkflowCreate(BaseModel):
    conversation_id: uuid.UUID
    service: Literal["gmail", "calendar"]
    intent: str = Field(min_length=1, max_length=48)
    request: dict[str, Any] = Field(min_length=1)
    idempotency_key: str = Field(min_length=1, max_length=128)


class WorkflowDecision(BaseModel):
    approve: bool = True


class WorkflowOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    conversation_id: uuid.UUID
    status: str
    service: str | None
    intent: str | None
    idempotency_key: str
    request: dict[str, Any] | None
    result: dict[str, Any] | None
    error: str | None
    created_at: datetime
    updated_at: datetime
