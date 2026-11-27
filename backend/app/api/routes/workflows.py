import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, get_mcp_gateway_factory
from app.core.logging_context import correlation_context
from app.database.models import User, WorkflowExecution
from app.database.session import get_db
from app.mcp.executors import MCPCalendarExecutor, MCPGmailExecutor
from app.schemas.workflow import WorkflowCreate, WorkflowDecision, WorkflowOut
from app.services import auth_service, workflow_service
from app.workflows import chat_service

router = APIRouter(prefix="/workflows", tags=["workflows"])
log = logging.getLogger(__name__)


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
        existing = workflow_service._owned_workflow(db, user, execution_id)
        with correlation_context(str(existing.id)):
            log.info("approval decision requested workflow_id=%s decision=%s",
                     existing.id, "approved" if body.approve else "rejected")
            try:
                if not body.approve:
                    execution = workflow_service.reject(db, user, execution_id)
                else:
                    _require_connected(db, user, existing)
                    with gateway_factory(db, user) as gateway:
                        execution = workflow_service.execute(db, user, execution_id, gmail=MCPGmailExecutor(gateway),
                                                             calendar=MCPCalendarExecutor(gateway))
                chat_service.record_decision(db, execution)
                log.info("approval decision completed workflow_id=%s status=%s", execution.id, execution.status)
            except HTTPException as exc:
                log.warning("approval decision blocked workflow_id=%s http_status=%s", existing.id, exc.status_code)
                raise
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return execution


@router.delete("/{execution_id}", response_model=WorkflowOut)
def reject_workflow(execution_id: uuid.UUID, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    try:
        existing = workflow_service._owned_workflow(db, user, execution_id)
        with correlation_context(str(existing.id)):
            log.info("approval decision requested workflow_id=%s decision=rejected", existing.id)
            execution = workflow_service.reject(db, user, execution_id)
            chat_service.record_decision(db, execution)
            log.info("approval decision completed workflow_id=%s status=%s", execution.id, execution.status)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return execution
