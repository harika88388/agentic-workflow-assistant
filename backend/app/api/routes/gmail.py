from dataclasses import asdict

from fastapi import APIRouter, Depends, Query

from app.api.deps import get_gmail_service
from app.integrations.gmail.service import GmailService

router = APIRouter(prefix="/gmail", tags=["gmail"])
# Read-only on purpose. Sending/drafting goes through the approval-gated workflow (Phase 9).


@router.get("/messages")
def search_messages(q: str = Query(min_length=1, max_length=500), max_results: int = Query(10, ge=1, le=25),
                    gmail: GmailService = Depends(get_gmail_service)) -> list[dict]:
    return [asdict(m) for m in gmail.search_emails(q, max_results)]


@router.get("/messages/{message_id}")
def read_message(message_id: str, gmail: GmailService = Depends(get_gmail_service)) -> dict:
    return asdict(gmail.read_email(message_id))


@router.get("/threads/{thread_id}")
def read_thread(thread_id: str, gmail: GmailService = Depends(get_gmail_service)) -> dict:
    return asdict(gmail.get_thread(thread_id))
