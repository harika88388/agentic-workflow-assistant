"""Execution history for the Activity view. Every query is scoped to the authenticated user."""
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.database.models import AgentRun, User
from app.database.session import get_db
from app.schemas.execution import ExecutionDetailOut, ExecutionSummaryOut
from app.services import execution_history

router = APIRouter(prefix="/executions", tags=["executions"])


def _summary(run: AgentRun) -> dict:
    return {**{c: getattr(run, c) for c in ExecutionSummaryOut.model_fields if hasattr(run, c)},
            "tool_count": len(run.tools or [])}


@router.get("", response_model=list[ExecutionSummaryOut])
def list_executions(limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0),
                    request_id: str | None = Query(None, max_length=64, pattern=r"^[A-Za-z0-9._-]+$"),
                    user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """`request_id` finds the run started by a specific request (the X-Request-ID the client sent), which lets
    the client recover a turn whose response stream was lost."""
    return [_summary(r) for r in execution_history.list_runs(db, user, limit=limit, offset=offset,
                                                             request_id=request_id)]


@router.get("/{execution_id}", response_model=ExecutionDetailOut)
def get_execution(execution_id: uuid.UUID, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    run = execution_history.get_run(db, user, execution_id)
    if run is None:  # another user's execution is indistinguishable from a missing one
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Execution not found")
    return {**_summary(run), "user_message_id": run.user_message_id, "assistant_message_id": run.assistant_message_id,
            "error_message": run.error_message, "steps": run.steps or [], "tools": run.tools or [],
            "llm_calls": run.llm_calls or []}
