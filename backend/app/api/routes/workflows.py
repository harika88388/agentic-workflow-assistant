import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, get_mcp_gateway_factory
from app.database.models import User, WorkflowExecution
from app.database.session import get_db
from app.mcp.executors import MCPCalendarExecutor, MCPGmailExecutor
from app.schemas.workflow import WorkflowCreate, WorkflowDecision, WorkflowOut
from app.services import auth_service, workflow_service
from app.workflows import chat_service

router = APIRouter(prefix="/workflows", tags=["workflows"])


def _require_connected(db: Session, user: User, execution: WorkflowExecution) -> None:
    connected = auth_service.connection_status(auth_service.get_google_credential(db, user.id))
    if not connected.get(execution.service or ""):
        raise HTTPException(403, detail={"code": "google_reconnect_required", "message": "Reconnect the required Google service."})


@router.post("", response_model=WorkflowOut, status_code=status.HTTP_201_CREATED)
def create_workflow(body: WorkflowCreate, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    try:
        return workflow_service.propose(db, user, conversation_id=body.conversation_id, service=body.service,
                                        intent=body.intent, request=body.request, idempotency_key=body.idempotency_key)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.get("", response_model=list[WorkflowOut])
def list_workflows(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return db.scalars(select(WorkflowExecution).where(WorkflowExecution.user_id == user.id).order_by(WorkflowExecution.created_at.desc())).all()


@router.get("/{execution_id}", response_model=WorkflowOut)
def get_workflow(execution_id: uuid.UUID, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    try:
        return workflow_service._owned_workflow(db, user, execution_id)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.post("/{execution_id}/decision", response_model=WorkflowOut)
def decide_workflow(execution_id: uuid.UUID, body: WorkflowDecision = WorkflowDecision(),
                    user: User = Depends(get_current_user), db: Session = Depends(get_db),
                    gateway_factory: chat_service.GatewayFactory = Depends(get_mcp_gateway_factory)):
    """Approve (execute through the Gmail/Calendar MCP servers) or reject a proposed action."""
    try:
        if not body.approve:
            execution = workflow_service.reject(db, user, execution_id)
        else:
            execution = workflow_service._owned_workflow(db, user, execution_id)
            _require_connected(db, user, execution)
            with gateway_factory(db, user) as gateway:
                execution = workflow_service.execute(db, user, execution_id, gmail=MCPGmailExecutor(gateway),
                                                     calendar=MCPCalendarExecutor(gateway))
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    chat_service.record_decision(db, execution)
    return execution


@router.delete("/{execution_id}", response_model=WorkflowOut)
def reject_workflow(execution_id: uuid.UUID, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    try:
        execution = workflow_service.reject(db, user, execution_id)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    chat_service.record_decision(db, execution)
    return execution
