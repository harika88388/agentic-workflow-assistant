import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, get_mcp_gateway_factory
from app.database.models import User
from app.database.session import get_db
from app.schemas.chat import ChatRequest, ChatTurnOut
from app.services.llm import get_llm_router
from app.services.llm.router import LLMRouter
from app.workflows import chat_service

router = APIRouter(tags=["chat"])


@router.post("/conversations/{conversation_id}/messages", response_model=ChatTurnOut,
             status_code=status.HTTP_201_CREATED)
def send_message(conversation_id: uuid.UUID, body: ChatRequest, user: User = Depends(get_current_user),
                 db: Session = Depends(get_db), llm: LLMRouter = Depends(get_llm_router),
                 gateway_factory: chat_service.GatewayFactory = Depends(get_mcp_gateway_factory)) -> ChatTurnOut:
    """Run one turn of the agentic workflow. Read operations execute immediately through MCP; consequential actions
    come back as `awaiting_approval` workflows and run only via POST /workflows/{id}/decision."""
    try:
        turn = chat_service.run_turn(db, user, conversation_id, body.content, llm=llm, gateway_factory=gateway_factory)
    except chat_service.ConversationNotFound:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Conversation not found") from None
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return ChatTurnOut(status=turn.status, user_message=turn.user_message, assistant_message=turn.assistant_message,
                       workflow=turn.workflow)
