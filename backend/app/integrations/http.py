"""Minimal Google REST client: bearer auth, error mapping, safe retries."""
import logging
import time
from collections.abc import Callable
from typing import Any
from urllib.parse import urlparse

import httpx

from app.core.logging_context import current_correlation_id
from app.integrations.errors import (
    GoogleAPIError,
    GoogleAuthError,
    GoogleBadRequestError,
    GoogleConflictError,
    GoogleNotFoundError,
    GoogleRateLimitError,
    GoogleServiceDisabledError,
    GoogleTransientError,
)

RETRY_STATUS = {429, 500, 502, 503, 504}
RATE_REASONS = {"rateLimitExceeded", "userRateLimitExceeded", "quotaExceeded", "dailyLimitExceeded"}
log = logging.getLogger(__name__)


def to_error(resp: httpx.Response) -> GoogleAPIError:
    try:
        err = resp.json().get("error", {})
    except (ValueError, AttributeError):
        err = {}
    if not isinstance(err, dict):
        err = {}
    message = err.get("message") or f"Google API returned HTTP {resp.status_code}"
    errors = err.get("errors") or [{}]
    reason = errors[0].get("reason") if isinstance(errors[0], dict) else None
    s = resp.status_code
    if s == 401:
        return GoogleAuthError(message, s, reason)
    if s == 403:
        if reason == "accessNotConfigured":
            return GoogleServiceDisabledError(message, s, reason)
        return (GoogleRateLimitError if reason in RATE_REASONS else GoogleAuthError)(message, s, reason)
    if s in (404, 410):
        return GoogleNotFoundError(message, s, reason)
    if s == 409:
        return GoogleConflictError(message, s, reason)
    if s == 429:
        return GoogleRateLimitError(message, s, reason)
    if s >= 500:
        return GoogleTransientError(message, s, reason)
    return GoogleBadRequestError(message, s, reason)


class GoogleHTTP:
    """Retry policy: idempotent calls (GET/DELETE or idempotent=True) retry on 429/5xx/network errors.
    Non-idempotent calls (e.g. sending mail) retry ONLY on 429, where Google rejected the request, so a
    timeout can never cause a duplicate send."""

    def __init__(
        self,
        base_url: str,
        token_provider: Callable[[], str],
        *,
        transport: httpx.BaseTransport | None = None,
        timeout: float = 15.0,
        max_retries: int = 3,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.base_url = base_url.rstrip("/")
        self._token = token_provider
        self._transport = transport
        self._timeout = timeout
        self._max_retries = max_retries
        self._sleep = sleep
        host = urlparse(self.base_url).hostname or ""
        self._service = "gmail" if host == "gmail.googleapis.com" else \
            "calendar" if "calendar" in self.base_url else "google"

    def request(
        self, method: str, path: str, *, params: dict | None = None, json: Any = None, idempotent: bool | None = None
    ) -> Any:
        if idempotent is None:
            idempotent = method in ("GET", "DELETE")
        started = time.perf_counter()
        correlation_id = current_correlation_id()
        resource = path.strip("/").split("/", 1)[0] or "root"
        attempt = 0
        while True:
            retry_after: float | None = None
            try:
                with httpx.Client(transport=self._transport, timeout=self._timeout) as client:
                    resp = client.request(
                        method, self.base_url + path, params=params, json=json,
                        headers={"Authorization": f"Bearer {self._token()}"},
                    )
            except httpx.TransportError as exc:  # timeouts, connection errors
                err: GoogleAPIError = GoogleTransientError(f"network error: {type(exc).__name__}")
                retryable = idempotent
                status_code = "network_error"
            else:
                if resp.status_code < 400:
                    log.info("Google API operation request_id=%s service=%s method=%s resource=%s status=%d "
                             "duration_ms=%d", correlation_id, self._service, method, resource, resp.status_code,
                             round((time.perf_counter() - started) * 1000))
                    return None if resp.status_code == 204 or not resp.content else resp.json()
                err = to_error(resp)
                retryable = (idempotent and resp.status_code in RETRY_STATUS) or isinstance(err, GoogleRateLimitError)
                status_code = resp.status_code
                try:
                    retry_after = float(resp.headers.get("Retry-After", ""))
                except ValueError:
                    retry_after = None
            if retryable and attempt < self._max_retries:
                log.debug("retrying Google API operation request_id=%s service=%s method=%s resource=%s "
                          "attempt=%d status=%s", correlation_id, self._service, method, resource,
                          attempt + 1, status_code)
                self._sleep(min(retry_after if retry_after is not None else 2**attempt, 30.0))
                attempt += 1
                continue
            log.error("Google API operation failed request_id=%s service=%s method=%s resource=%s status=%s "
                      "exception_type=%s duration_ms=%d", correlation_id, self._service, method, resource,
                      status_code, type(err).__name__, round((time.perf_counter() - started) * 1000))
            raise err
