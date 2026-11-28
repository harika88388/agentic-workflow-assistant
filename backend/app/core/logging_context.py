"""Request-scoped log context (request/correlation id, user id, execution id) shared by backend and MCP logs.

The context is a mutable dict held in a ContextVar. FastAPI runs sync dependencies and endpoints in copies of the
request's context, so a value bound in a dependency (e.g. the authenticated user id) must mutate the shared dict
rather than set the ContextVar, or it would be invisible to the endpoint and to the request-completion log.
"""
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar, Token
from typing import Any

_context: ContextVar[dict[str, Any] | None] = ContextVar("log_context", default=None)


def current_context() -> dict[str, Any]:
    return dict(_context.get() or {})


def current_correlation_id() -> str:
    return str((_context.get() or {}).get("request_id") or "-")


def bind(**values: Any) -> None:
    """Add fields to the current log context (visible to every log line for the rest of the request)."""
    ctx = _context.get()
    if ctx is None:
        _context.set({k: v for k, v in values.items() if v is not None})
        return
    ctx.update({k: v for k, v in values.items() if v is not None})


@contextmanager
def log_context(**values: Any) -> Iterator[dict[str, Any]]:
    """A fresh context layered on top of the current one (restored on exit)."""
    ctx = {**(_context.get() or {}), **{k: v for k, v in values.items() if v is not None}}
    token: Token = _context.set(ctx)
    try:
        yield ctx
    finally:
        _context.reset(token)


@contextmanager
def correlation_context(correlation_id: str) -> Iterator[None]:
    with log_context(request_id=correlation_id):
        yield
