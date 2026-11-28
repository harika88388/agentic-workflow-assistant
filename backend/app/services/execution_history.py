"""Execution history (AgentRun rows) for the Activity view.

Best effort by design: a failure to record history is logged and never fails the user's request. Records hold
metadata only (steps, tool names, timings, models, outcome); email content, tokens and credentials never enter them.
"""
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.log_config import redact
from app.database.models import AgentRun, User, WorkflowExecution

log = logging.getLogger(__name__)
REQUEST_PREVIEW_CHARS = 500
TERMINAL_TURN_STATUSES = {"completed", "needs_clarification", "awaiting_approval", "failed"}
STALE_RUN_SECONDS = 330  # the function's 300 s maxDuration (vercel.json) plus a margin


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _safe_commit(db: Session, what: str) -> bool:
    try:
        db.commit()
        return True
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        log.error("execution history %s failed exception_type=%s", what, type(exc).__name__, exc_info=True)
        return False


def start(db: Session, user: User, *, conversation_id: uuid.UUID, user_message_id: uuid.UUID, request_id: str,
          request_text: str) -> AgentRun | None:
    run = AgentRun(user_id=user.id, conversation_id=conversation_id, user_message_id=user_message_id,
                   request_id=request_id[:64], request_text=request_text[:REQUEST_PREVIEW_CHARS], status="running")
    db.add(run)
    if not _safe_commit(db, "start"):
        return None
    log.info("execution started execution_id=%s", run.id)
    return run


def _steps(trace: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{k: v for k, v in t.items() if k in ("node", "status", "detail", "ms")} for t in trace]


def _tools(calls: list[dict[str, Any]], phase: str) -> list[dict[str, Any]]:
    return [{"server": c.get("server"), "tool": c.get("tool"), "ok": c.get("ok"), "duration_ms": c.get("ms"),
             "error": c.get("error"), "phase": phase} for c in calls]


def finish(db: Session, run: AgentRun | None, *, state: dict[str, Any], agent: str | None,
           mcp_calls: list[dict[str, Any]], llm_metrics: list[dict[str, Any]], duration_ms: int) -> None:
    if run is None:
        return
    req = state.get("request") or {}
    err = state.get("error") or {}
    workflow = state.get("workflow") or {}
    status = state.get("status") if state.get("status") in TERMINAL_TURN_STATUSES else "failed"
    run.status = status
    run.intent = req.get("intent") or (state.get("intent") or {}).get("intent")
    run.service = state.get("service") or req.get("service")
    run.agent = agent
    run.assistant_message_id = uuid.UUID(state["assistant_message_id"]) if state.get("assistant_message_id") else None
    run.workflow_id = uuid.UUID(workflow["id"]) if workflow.get("id") else None
    run.approval_status = "pending" if status == "awaiting_approval" else None
    run.steps = _steps(state.get("trace") or [])
    run.tools = _tools(mcp_calls, "workflow")
    run.llm_calls = llm_metrics
    run.error_code = err.get("code")
    run.error_message = redact(str(err.get("message") or ""))[:500] or None
    run.duration_ms = duration_ms
    run.completed_at = _now()
    if _safe_commit(db, "finish"):
        log.info("execution finished execution_id=%s status=%s intent=%s service=%s agent=%s tools=%d llm_calls=%d "
                 "duration_ms=%d", run.id, run.status, run.intent, run.service, agent or "-", len(mcp_calls),
                 len(llm_metrics), duration_ms)


def fail(db: Session, run: AgentRun | None, *, code: str, message: str, duration_ms: int,
         mcp_calls: list[dict[str, Any]] | None = None, llm_metrics: list[dict[str, Any]] | None = None) -> None:
    if run is None:
        return
    try:
        db.rollback()  # the turn's own failed transaction must not block recording the failure
        run = db.merge(run)
        run.status, run.error_code = "failed", code
        run.error_message = redact(message)[:500]
        run.tools = _tools(mcp_calls or [], "workflow")
        run.llm_calls = llm_metrics or []
        run.duration_ms, run.completed_at = duration_ms, _now()
    except Exception as exc:  # noqa: BLE001
        log.error("execution history fail-record failed exception_type=%s", type(exc).__name__, exc_info=True)
        return
    if _safe_commit(db, "fail"):
        log.info("execution finished execution_id=%s status=failed error_code=%s duration_ms=%d", run.id, code,
                 duration_ms)


def record_decision(db: Session, execution: WorkflowExecution, *, approved: bool,
                    mcp_calls: list[dict[str, Any]] | None = None, duration_ms: int | None = None) -> None:
    """Attach the human decision (and, if approved, the action's verified outcome) to the originating run."""
    try:
        run = db.scalar(select(AgentRun).where(AgentRun.workflow_id == execution.id,
                                               AgentRun.user_id == execution.user_id))
    except Exception as exc:  # noqa: BLE001
        log.error("execution history lookup failed exception_type=%s", type(exc).__name__)
        return
    if run is None:
        return
    run.approval_status = "approved" if approved else "rejected"
    if not approved:
        run.status = "rejected"
    else:
        run.action_status = "succeeded" if execution.status == "succeeded" else "failed"
        run.status = "completed" if execution.status == "succeeded" else "failed"
        if execution.status == "failed":
            run.error_code = "action_failed"
            run.error_message = redact(execution.error or "")[:500] or None
    run.tools = [*(run.tools or []), *_tools(mcp_calls or [], "approval")]
    if duration_ms is not None:
        run.duration_ms = (run.duration_ms or 0) + duration_ms
    run.completed_at = _now()
    if _safe_commit(db, "decision"):
        log.info("execution approval recorded execution_id=%s workflow_id=%s approval_status=%s action_status=%s",
                 run.id, execution.id, run.approval_status, run.action_status or "-")


def _expire_stale(db: Session, runs: list[AgentRun]) -> list[AgentRun]:
    """A run still "running" past the function's hard limit was killed mid-turn; report it as failed, not running."""
    cutoff = _now() - timedelta(seconds=STALE_RUN_SECONDS)
    stale = [r for r in runs if r.status == "running"
             and (r.created_at if r.created_at.tzinfo else r.created_at.replace(tzinfo=timezone.utc)) < cutoff]
    for run in stale:
        run.status, run.error_code = "failed", "timed_out"
        run.error_message = "The request stopped before it finished (server time limit). Please try again."
    if stale and _safe_commit(db, "expire"):
        for run in stale:
            log.warning("execution marked timed out execution_id=%s", run.id)
    return runs


def list_runs(db: Session, user: User, *, limit: int = 50, offset: int = 0,
              request_id: str | None = None) -> list[AgentRun]:
    query = select(AgentRun).where(AgentRun.user_id == user.id)
    if request_id:
        query = query.where(AgentRun.request_id == request_id)
    return _expire_stale(db, list(db.scalars(query.order_by(AgentRun.created_at.desc()).limit(limit).offset(offset))))


def get_run(db: Session, user: User, run_id: uuid.UUID) -> AgentRun | None:
    run = db.scalar(select(AgentRun).where(AgentRun.id == run_id, AgentRun.user_id == user.id))
    return _expire_stale(db, [run])[0] if run else None
