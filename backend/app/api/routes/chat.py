import contextvars
import json
import logging
import queue
import threading
import time
import uuid
from collections.abc import AsyncIterator

import anyio
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session, sessionmaker
from starlette.requests import ClientDisconnect

from app.api.deps import get_current_user, get_mcp_gateway_factory
from app.core.logging_context import current_correlation_id
from app.database.models import User
from app.database.session import get_db
from app.schemas.chat import ChatRequest, ChatTurnOut
from app.services.llm import get_llm_router
from app.services.llm.router import LLMRouter
from app.workflows import chat_service

router = APIRouter(tags=["chat"])
log = logging.getLogger(__name__)
HEARTBEAT_SECONDS = 5.0
# A turn whose client disconnected keeps the invocation alive until it is persisted; bounded by the
# function's maxDuration (vercel.json).
DISCONNECTED_TURN_GRACE_SECONDS = 270.0


def _turn_out(turn: chat_service.TurnResult) -> ChatTurnOut:
    return ChatTurnOut(status=turn.status, user_message=turn.user_message, assistant_message=turn.assistant_message,
                       workflow=turn.workflow, execution_id=turn.execution_id)


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
    return _turn_out(turn)


@router.post("/conversations/{conversation_id}/messages/stream")
def stream_message(conversation_id: uuid.UUID, body: ChatRequest, user: User = Depends(get_current_user),
                   db: Session = Depends(get_db), llm: LLMRouter = Depends(get_llm_router),
                   gateway_factory: chat_service.GatewayFactory = Depends(get_mcp_gateway_factory)) -> StreamingResponse:
    """Same turn as POST /messages, streamed as NDJSON so the UI can show live workflow progress:
    `started`, then `progress` (LangGraph node) and `tool` (MCP call) events, then exactly one `result` or `error`.
    `heartbeat` lines every few seconds let the client detect a stalled connection. The turn runs to completion
    and is persisted even if the client disconnects, so the client can recover the result from
    GET /executions instead of resubmitting."""
    try:
        chat_service.owned_conversation(db, user, conversation_id)
    except chat_service.ConversationNotFound:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Conversation not found") from None

    # The turn runs in a worker thread with its own session, so the request-scoped one is released now.
    make_session = sessionmaker(bind=db.get_bind(), autoflush=False, expire_on_commit=False)
    user_id, request_id = user.id, current_correlation_id()
    db.close()
    events: queue.Queue = queue.Queue()
    done = object()

    def worker() -> None:
        session = make_session()
        try:
            worker_user = session.get(User, user_id)
            if worker_user is None:
                raise PermissionError("user no longer exists")
            turn = chat_service.run_turn(session, worker_user, conversation_id, body.content, llm=llm,
                                         gateway_factory=gateway_factory, on_event=events.put)
            events.put({"type": "result", "turn": _turn_out(turn).model_dump(mode="json")})
        except ValueError as exc:
            events.put({"type": "error", "code": "invalid_request", "message": str(exc), "request_id": request_id})
        except Exception as exc:  # noqa: BLE001 - run_turn already logged the stack trace
            log.error("streamed turn failed exception_type=%s", type(exc).__name__)
            events.put({"type": "error", "code": "internal_error", "request_id": request_id,
                        "message": "Something went wrong while processing your request."})
        finally:
            session.close()
            events.put(done)

    thread = threading.Thread(target=contextvars.copy_context().run, args=(worker,), daemon=True,
                              name=f"turn-{request_id[:12]}")

    progress = {"began": time.perf_counter(), "events": 0, "progress": 0, "heartbeats": 0, "outcome": None}

    async def stream() -> AsyncIterator[bytes]:
        log.info("stream started request_id=%s conversation_id=%s", request_id, conversation_id)
        thread.start()
        try:
            while True:
                try:
                    item = await anyio.to_thread.run_sync(events.get, True, HEARTBEAT_SECONDS)
                except queue.Empty:
                    progress["heartbeats"] += 1
                    yield b'{"type":"heartbeat"}\n'
                    continue
                if item is done:
                    break
                kind = item.get("type")
                if progress["events"] == 0:
                    log.info("stream first event sent request_id=%s type=%s after_ms=%d", request_id, kind,
                             _elapsed_ms(progress))
                progress["events"] += 1
                progress["progress"] += kind in ("progress", "tool")
                if kind in ("result", "error"):
                    progress["outcome"] = kind
                yield (json.dumps(item, default=str) + "\n").encode()
        except Exception as exc:
            log.error("stream failed request_id=%s exception_type=%s events_sent=%d", request_id,
                      type(exc).__name__, progress["events"], exc_info=True)
            raise
        log.info("stream completed request_id=%s outcome=%s events=%d progress_events=%d heartbeats=%d "
                 "duration_ms=%d", request_id, progress["outcome"] or "none", progress["events"],
                 progress["progress"], progress["heartbeats"], _elapsed_ms(progress))

    return TurnStreamingResponse(stream(), worker=thread, request_id=request_id, progress=progress,
                                 media_type="application/x-ndjson",
                                 headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"})


def _elapsed_ms(progress: dict) -> int:
    return round((time.perf_counter() - progress["began"]) * 1000)


class TurnStreamingResponse(StreamingResponse):
    """If the client goes away mid-stream, logs it and keeps the request open until the turn's worker has
    persisted the result, so a serverless invocation is never ended with the workflow half-done."""

    def __init__(self, content, *, worker: threading.Thread, request_id: str, progress: dict, **kwargs):
        super().__init__(content, **kwargs)
        self.worker, self.request_id, self.progress = worker, request_id, progress

    async def __call__(self, scope, receive, send) -> None:
        disconnected = False
        try:
            await super().__call__(scope, receive, send)
        except (ClientDisconnect, OSError) as exc:
            disconnected = True
            self._log_disconnect(exc)
        except anyio.get_cancelled_exc_class() as exc:
            disconnected = True
            self._log_disconnect(exc)
            raise
        finally:
            if self.worker.is_alive():
                with anyio.CancelScope(shield=True):
                    await anyio.to_thread.run_sync(self.worker.join, DISCONNECTED_TURN_GRACE_SECONDS)
                if disconnected:
                    log.info("turn finished after client disconnect request_id=%s completed=%s",
                             self.request_id, not self.worker.is_alive())

    def _log_disconnect(self, exc: BaseException) -> None:
        log.warning("stream client disconnected request_id=%s reason=%s events_sent=%d outcome=%s duration_ms=%d",
                    self.request_id, type(exc).__name__, self.progress["events"],
                    self.progress["outcome"] or "pending", _elapsed_ms(self.progress))
