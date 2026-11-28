import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Literal, Protocol

import httpx

_last_usage: ContextVar[dict[str, int] | None] = ContextVar("llm_last_usage", default=None)
_deadline: ContextVar[float | None] = ContextVar("llm_deadline", default=None)
MIN_ATTEMPT_SECONDS = 5.0


@contextmanager
def llm_time_budget(seconds: float) -> Iterator[None]:
    """Cap the total time all LLM calls (attempts, retries, re-asks) may take inside the block.

    The serverless function is killed at a hard limit; running out of budget instead fails the call cleanly so the
    turn can still be answered and recorded."""
    token = _deadline.set(time.monotonic() + seconds)
    try:
        yield
    finally:
        _deadline.reset(token)


def _attempt_timeout(timeout: float) -> float:
    deadline = _deadline.get()
    return timeout if deadline is None else min(timeout, deadline - time.monotonic())


def record_usage(prompt: Any = None, completion: Any = None, total: Any = None) -> None:
    """Called by a provider after a successful completion; read once by the router for logs and traces."""
    usage = {k: int(v) for k, v in (("prompt_tokens", prompt), ("completion_tokens", completion),
                                    ("total_tokens", total)) if isinstance(v, (int, float))}
    _last_usage.set(usage or None)


def take_usage() -> dict[str, int] | None:
    usage = _last_usage.get()
    _last_usage.set(None)
    return usage


class LLMError(Exception): """Base class."""
class LLMConfigError(LLMError): """Missing API key / bad configuration."""
class LLMProviderError(LLMError): """Provider unreachable or returned an error after retries."""
class LLMResponseError(LLMError): """Provider answered but output was empty / blocked / failed validation."""


@dataclass(frozen=True)
class ChatMessage:
    role: Literal["system", "user", "assistant"]
    content: str


class LLMProvider(Protocol):
    name: str

    def complete(self, messages: list[ChatMessage], *, model: str, temperature: float, max_tokens: int,
                 json_mode: bool = False) -> str: ...


def post_json(url: str, *, headers: dict, payload: dict, timeout: float, max_retries: int,
              transport: httpx.BaseTransport | None = None, sleep: Callable[[float], None] | None = None) -> dict:
    """POST with retry on 429/5xx/timeouts, within any `llm_time_budget`. Error text never includes request headers."""
    last = "unknown error"
    for attempt in range(max_retries + 1):
        attempt_timeout = _attempt_timeout(timeout)
        if attempt_timeout < MIN_ATTEMPT_SECONDS:
            raise LLMProviderError(f"provider too slow: time budget for this request used up ({last})")
        try:
            with httpx.Client(transport=transport, timeout=attempt_timeout) as c:
                resp = c.post(url, headers=headers, json=payload)
        except httpx.TransportError as exc:
            last = f"network error: {type(exc).__name__}"
        else:
            if resp.status_code < 400:
                try:
                    return resp.json()
                except ValueError as exc:
                    raise LLMProviderError("provider returned non-JSON response") from exc
            last = f"HTTP {resp.status_code}"
            if resp.status_code in (401, 403):
                raise LLMConfigError(f"provider rejected credentials ({last})")
            if resp.status_code not in (429, 500, 502, 503, 504):
                raise LLMProviderError(f"provider error ({last})")
        if attempt < max_retries:
            (sleep or time.sleep)(min(2**attempt, 8))  # resolved at call time so tests can patch it
    raise LLMProviderError(f"provider unavailable after {max_retries + 1} attempts ({last})")
