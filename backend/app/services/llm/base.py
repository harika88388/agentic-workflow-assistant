import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal, Protocol

import httpx


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
    """POST with retry on 429/5xx/timeouts. Error text never includes request headers (API keys)."""
    last = "unknown error"
    for attempt in range(max_retries + 1):
        try:
            with httpx.Client(transport=transport, timeout=timeout) as c:
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
