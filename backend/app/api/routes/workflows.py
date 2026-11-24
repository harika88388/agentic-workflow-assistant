import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, user_zone
from app.database.models import User, WorkflowExecution
from app.database.session import get_db
from app.integrations.calendar.client import CalendarClient
from app.integrations.calendar.service import CalendarService
from app.integrations.gmail.client import GmailClient
from app.integrations.gmail.service import GmailService
from app.schemas.workflow import WorkflowCreate, WorkflowDecision, WorkflowOut
from app.services import auth_service, workflow_service
from app.services.token_service import get_valid_access_token

router = APIRouter(prefix="/workflows", tags=["workflows"])


def _services(db: Session, user: User, execution: WorkflowExecution):
    connected = auth_service.connection_status(auth_service.get_google_credential(db, user.id))
    if not connected.get(execution.service or ""):
        raise HTTPException(403, detail={"code": "google_reconnect_required", "message": "Reconnect the required Google service."})
    token = lambda: get_valid_access_token(db, user.id)
    if execution.service == "gmail":
        return GmailService(GmailClient(token)), None
    return None, CalendarService(CalendarClient(token), user_zone(user))


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
                    user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    try:
        if not body.approve:
            return workflow_service.reject(db, user, execution_id)
        execution = workflow_service._owned_workflow(db, user, execution_id)
        gmail, calendar = _services(db, user, execution)
        return workflow_service.execute(db, user, execution_id, gmail=gmail, calendar=calendar)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.delete("/{execution_id}", response_model=WorkflowOut)
def reject_workflow(execution_id: uuid.UUID, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    try:
        return workflow_service.reject(db, user, execution_id)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
