"""Map integration/LLM exceptions to clean HTTP responses (no stack traces or secrets to the client)."""
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.integrations.errors import (
    GoogleAPIError, GoogleAuthError, GoogleBadRequestError, GoogleConflictError,
    GoogleNotFoundError, GoogleRateLimitError, GoogleServiceDisabledError,
)
from app.services.llm.base import LLMConfigError, LLMProviderError, LLMResponseError
from app.services.token_service import TokenUnavailableError


def _json(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"detail": {"code": code, "message": message}})


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(TokenUnavailableError)
    async def _token(_: Request, exc: TokenUnavailableError):
        return _json(403, "google_reconnect_required", str(exc))

    @app.exception_handler(GoogleAPIError)
    async def _google(_: Request, exc: GoogleAPIError):
        table = [
            (GoogleAuthError, 403, "google_reconnect_required"),
            (GoogleNotFoundError, 404, "not_found"),
            (GoogleConflictError, 409, "conflict"),
            (GoogleRateLimitError, 429, "google_rate_limited"),
            (GoogleBadRequestError, 400, "bad_request"),
            (GoogleServiceDisabledError, 503, "service_disabled"),
        ]
        for cls, status, code in table:
            if isinstance(exc, cls):
                return _json(status, code, exc.message)
        return _json(502, "google_unavailable", "Google is temporarily unavailable. Please try again.")

    @app.exception_handler(LLMConfigError)
    async def _llm_cfg(_: Request, exc: LLMConfigError):
        return _json(503, "llm_not_configured", str(exc))

    @app.exception_handler(LLMProviderError)
    async def _llm_prov(_: Request, exc: LLMProviderError):
        return _json(502, "llm_unavailable", str(exc))

    @app.exception_handler(LLMResponseError)
    async def _llm_resp(_: Request, exc: LLMResponseError):
        return _json(502, "llm_invalid_response", str(exc))
