"""Validated structured output: extract JSON, validate with Pydantic, feed errors back to the model once or twice."""
import logging
from typing import TypeVar

from pydantic import BaseModel, ValidationError

from app.services.llm.base import ChatMessage, LLMResponseError
from app.services.llm.jsonutil import extract_json, strip_reasoning
from app.services.llm.router import LLMRole, LLMRouter

log = logging.getLogger(__name__)
T = TypeVar("T", bound=BaseModel)


def parse_structured(text: str, schema: type[T]) -> T:
    return schema.model_validate(extract_json(text))


def _summarize(exc: Exception) -> str:
    if isinstance(exc, ValidationError):
        return "; ".join(f"{'.'.join(map(str, e['loc'])) or 'root'}: {e['msg']}" for e in exc.errors())[:500]
    return str(exc)[:500]


def complete_structured(router: LLMRouter, role: LLMRole, messages: list[ChatMessage], schema: type[T],
                        *, max_attempts: int = 3) -> T:
    msgs, last = list(messages), None
    for attempt in range(1, max_attempts + 1):
        raw = router.complete(role, msgs, json_mode=True)
        try:
            return parse_structured(raw, schema)
        except ValueError as exc:  # JSONExtractionError and pydantic ValidationError are both ValueErrors
            last = exc
            log.warning("invalid structured output attempt=%d/%d exception_type=%s",
                        attempt, max_attempts, type(exc).__name__)
            msgs = msgs + [
                ChatMessage("assistant", strip_reasoning(raw)[:4000]),
                ChatMessage("user", f"Your reply was invalid: {_summarize(exc)}. Reply again with ONLY the corrected JSON object."),
            ]
    raise LLMResponseError(f"model output failed validation after {max_attempts} attempts: {_summarize(last)}")  # type: ignore[arg-type]
