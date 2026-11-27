"""Request-scoped correlation IDs for backend and MCP logs."""
from contextlib import contextmanager
from contextvars import ContextVar, Token
from typing import Iterator

_correlation_id: ContextVar[str] = ContextVar("correlation_id", default="-")


def current_correlation_id() -> str:
    return _correlation_id.get()


@contextmanager
def correlation_context(correlation_id: str) -> Iterator[None]:
    token: Token[str] = _correlation_id.set(correlation_id)
    try:
        yield
    finally:
        _correlation_id.reset(token)
