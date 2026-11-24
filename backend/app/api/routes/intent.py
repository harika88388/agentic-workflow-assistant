from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.agents.intent_agent import MAX_CONTEXT_CHARS, MAX_MESSAGE_CHARS, IntentRecognitionAgent
from app.api.deps import get_current_user
from app.database.models import User
from app.schemas.intent import IntentResult
from app.services.llm import get_llm_router
from app.services.llm.router import LLMRouter

router = APIRouter(tags=["intent"])


class AnalyzeRequest(BaseModel):
    message: str = Field(min_length=1, max_length=MAX_MESSAGE_CHARS)
    context: str | None = Field(default=None, max_length=MAX_CONTEXT_CHARS)


@router.post("/intent/analyze", response_model=IntentResult)
def analyze(body: AnalyzeRequest, user: User = Depends(get_current_user),
            llm: LLMRouter = Depends(get_llm_router)) -> IntentResult:
    """Classify a message. Performs NO external action (no Gmail/Calendar call)."""
    try:
        tz = ZoneInfo(user.timezone)
    except Exception:  # noqa: BLE001
        tz = ZoneInfo("UTC")
    try:
        return IntentRecognitionAgent(llm).recognize(
            body.message, reference_date=datetime.now(tz).date(), timezone=tz.key, conversation_context=body.context)
    except ValueError as exc:  # blank / oversized message (input validation, not an LLM failure)
        raise HTTPException(422, str(exc)) from exc


@router.get("/llm/config")
def llm_config(_: User = Depends(get_current_user), llm: LLMRouter = Depends(get_llm_router)) -> dict:
    """Which provider/model serves each responsibility (no secrets)."""
    return llm.describe()
