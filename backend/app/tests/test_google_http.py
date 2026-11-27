import logging

import httpx
import pytest

from app.core.logging_context import correlation_context
from app.integrations.calendar.client import CalendarClient
from app.integrations.errors import (
    GoogleAuthError, GoogleBadRequestError, GoogleConflictError, GoogleNotFoundError,
    GoogleRateLimitError, GoogleServiceDisabledError, GoogleTransientError,
)
from app.integrations.gmail.client import GmailClient
from app.integrations.http import GoogleHTTP


def make(handler, **kw):
    sleeps = []
    http = GoogleHTTP("https://api.test/v1", lambda: "tok", transport=httpx.MockTransport(handler),
                      sleep=sleeps.append, **kw)
    return http, sleeps


def err(status, reason=None, headers=None):
    body = {"error": {"code": status, "message": f"err {status}", "errors": [{"reason": reason}] if reason else []}}
    return httpx.Response(status, json=body, headers=headers)


def test_sends_bearer_token_and_parses_json_and_204():
    seen = []
    def h(req):
        seen.append(req.headers["authorization"])
        return httpx.Response(204) if req.method == "DELETE" else httpx.Response(200, json={"ok": 1})
    http, _ = make(h)
    assert http.request("GET", "/x") == {"ok": 1} and http.request("DELETE", "/x") is None
    assert seen == ["Bearer tok", "Bearer tok"]


def test_google_api_operation_log_has_correlation_without_request_secrets(caplog):
    private_marker = "PRIVATE_EMAIL_BODY_MARKER"
    http = GoogleHTTP(
        "https://gmail.googleapis.com/gmail/v1/users/me",
        lambda: "secret-access-token",
        transport=httpx.MockTransport(lambda req: httpx.Response(200, json={"id": "sent"})),
    )
    with correlation_context("request-42"), caplog.at_level(logging.INFO, logger="app.integrations.http"):
        http.request("POST", "/messages/send", json={"body": private_marker})

    assert "request_id=request-42" in caplog.text
    assert "service=gmail" in caplog.text and "resource=messages" in caplog.text
    assert "secret-access-token" not in caplog.text and private_marker not in caplog.text


def test_get_retries_5xx_then_succeeds():
    calls = []
    def h(req):
        calls.append(1)
        return err(503) if len(calls) < 3 else httpx.Response(200, json={"ok": True})
    http, sleeps = make(h)
    assert http.request("GET", "/x") == {"ok": True} and len(calls) == 3 and len(sleeps) == 2


def test_post_is_never_retried_on_5xx_or_timeout_so_no_duplicate_sends():
    calls = []
    def h(req):
        calls.append(1)
        return err(500)
    http, _ = make(h)
    with pytest.raises(GoogleTransientError):
        http.request("POST", "/send", json={})
    assert len(calls) == 1

    def boom(req):
        calls.append(1)
        raise httpx.ReadTimeout("slow")
    http, _ = make(boom)
    calls.clear()
    with pytest.raises(GoogleTransientError):
        http.request("POST", "/send", json={})
    assert len(calls) == 1


def test_post_retried_on_429_and_retry_after_respected():
    calls = []
    def h(req):
        calls.append(1)
        return err(429, headers={"Retry-After": "3"}) if len(calls) == 1 else httpx.Response(200, json={"id": "m"})
    http, sleeps = make(h)
    assert http.request("POST", "/send", json={}) == {"id": "m"} and sleeps == [3.0]


def test_idempotent_post_flag_allows_retry_and_get_retries_network_errors():
    calls = []
    def h(req):
        calls.append(1)
        if len(calls) == 1:
            raise httpx.ConnectError("down")
        return httpx.Response(200, json={"ok": 1})
    http, _ = make(h)
    assert http.request("GET", "/x") == {"ok": 1} and len(calls) == 2


def test_retries_exhausted_raises_last_error():
    http, sleeps = make(lambda r: err(503), max_retries=2)
    with pytest.raises(GoogleTransientError):
        http.request("GET", "/x")
    assert len(sleeps) == 2


def test_error_mapping():
    cases = [(401, None, GoogleAuthError), (403, "insufficientPermissions", GoogleAuthError),
             (403, "rateLimitExceeded", GoogleRateLimitError), (404, None, GoogleNotFoundError),
             (409, None, GoogleConflictError), (400, None, GoogleBadRequestError)]
    for status, reason, exc in cases:
        http, _ = make(lambda r, s=status, rs=reason: err(s, rs), max_retries=0)
        with pytest.raises(exc):
            http.request("GET", "/x")


def test_access_not_configured_is_distinct_from_reconnect_required():
    http, _ = make(lambda r: err(403, "accessNotConfigured"), max_retries=0)
    with pytest.raises(GoogleServiceDisabledError) as exc:
        http.request("GET", "/x")
    assert exc.value.reason == "accessNotConfigured"
    assert exc.value.status == 403


def test_non_json_error_body_is_handled():
    http, _ = make(lambda r: httpx.Response(502, text="<html>bad gateway</html>"), max_retries=0)
    with pytest.raises(GoogleTransientError):
        http.request("GET", "/x")


def test_gmail_search_fetches_metadata_and_skips_deleted():
    def h(req):
        p = req.url.path
        if p.endswith("/messages") and req.method == "GET":
            return httpx.Response(200, json={"messages": [{"id": "a"}, {"id": "gone"}, {"id": "b"}]})
        if p.endswith("/messages/gone"):
            return err(404)
        mid = p.rsplit("/", 1)[1]
        return httpx.Response(200, json={"id": mid, "threadId": "t", "payload": {"headers": [{"name": "Subject", "value": f"S-{mid}"}]}})
    c = GmailClient(lambda: "tok", transport=httpx.MockTransport(h), sleep=lambda s: None)
    assert [m.subject for m in c.search("from:x", max_results=99)] == ["S-a", "S-b"]


def test_gmail_ids_are_url_quoted_and_empty_rejected():
    seen = []
    def h(req):
        seen.append(req.url.raw_path.decode())
        return httpx.Response(200, json={"id": "x", "payload": {}})
    c = GmailClient(lambda: "tok", transport=httpx.MockTransport(h))
    c.get_message("../drafts/1")
    assert "%2F" in seen[0] and "/../" not in seen[0]
    with pytest.raises(ValueError):
        c.get_message(" ")


def test_calendar_freebusy_parsing_and_naive_datetime_rejected():
    from datetime import datetime, timezone
    def h(req):
        return httpx.Response(200, json={"calendars": {"primary": {"busy": [{"start": "2026-11-25T05:00:00Z", "end": "2026-11-25T05:30:00Z"}]}}})
    c = CalendarClient(lambda: "tok", transport=httpx.MockTransport(h))
    s, e = datetime(2026, 11, 25, tzinfo=timezone.utc), datetime(2026, 11, 26, tzinfo=timezone.utc)
    busy = c.freebusy(s, e, "UTC")
    assert busy[0][0].hour == 5 and busy[0][1].minute == 30
    with pytest.raises(ValueError):
        c.list_events(datetime(2026, 11, 25))
