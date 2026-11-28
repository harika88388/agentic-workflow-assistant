"""Centralised logging: redaction, structured output, request correlation and failure safety."""
import io
import json
import logging
import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.log_config import (
    REDACTED, JsonFormatter, RequestContextMiddleware, TextFormatter, configure_logging, redact,
)
from app.core.logging_context import bind, current_correlation_id, log_context
from app.integrations.gmail import mime
from app.integrations.gmail.client import GmailClient
from app.tests.test_chat_workflow import env  # noqa: F401  (end-to-end fixture)
from app.tests.test_email_search_grounding import REG_ANSWER, REG_BODY, REG_QUESTION, regression_payload


@pytest.fixture()
def json_logs():
    """Route the real application handler (JSON format) into a buffer; yields a function returning parsed lines."""
    root = logging.getLogger()
    previous_handlers, previous_level = list(root.handlers), root.level
    buf = io.StringIO()
    configure_logging(level="DEBUG", fmt="json", stream=buf)

    def lines() -> list[dict]:
        return [json.loads(line) for line in buf.getvalue().splitlines() if line.strip()]
    yield lines
    for h in list(root.handlers):
        if h not in previous_handlers:
            root.removeHandler(h)
    root.setLevel(previous_level)


# ---------- redaction ----------
SECRETS = {
    "bearer": "Authorization: Bearer ya29.a0AfH6SMBx-secret-access-token",
    "google_access": "token ya29.a0AfH6SMBxSECRETvalue123",
    "google_refresh": "refresh 1//0gLongRefreshTokenValue-abcdefghijk",
    "jwt": "jwt eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTYifQ.c2lnbmF0dXJlLXZhbHVl",
    "openrouter": "key sk-or-v1-0123456789abcdef0123456789abcdef",
    "google_api_key": "AIzaSyA1234567890abcdefghijklmnopqrstu",
    "client_secret": "GOCSPX-AbCdEf123456_secretvalue",
    "neon": "postgresql://neondb_owner:npg_jbVeC5DH4Qsn@ep-host.neon.tech/neondb",
    "fernet": "key=" + "A" * 43 + "=",
    "oauth_code": "/api/auth/google/callback?code=4/0AbCdSECRETCODE&state=xyz.state",
    "kv": "client_secret=topsecret password: hunter2 api_key=abc123",
}


@pytest.mark.parametrize("name", SECRETS)
def test_secrets_are_redacted_from_strings(name):
    text = redact(SECRETS[name])
    for leaked in ("ya29.a0", "1//0gLong", "eyJzdWIi", "sk-or-v1-0123", "AIzaSyA123", "GOCSPX-AbC", "npg_jbVe",
                   "A" * 43, "SECRETCODE", "topsecret", "hunter2", "abc123"):
        assert leaked not in text, (name, text)
    assert REDACTED in text


def test_email_addresses_are_masked_and_dict_secrets_blanked():
    assert redact("from aa3corner@gmail.com") == "from a***@gmail.com"
    data = redact({"access_token": "x", "refresh_token": "y", "Authorization": "Bearer z", "body_text": "private",
                   "jwt_secret": "s", "nested": {"api_key": "k", "ok": "fine"}, "prompt_tokens": 12,
                   "total_tokens": 30, "tool": "search_emails"})
    assert data["access_token"] == data["refresh_token"] == data["Authorization"] == data["body_text"] == REDACTED
    assert data["jwt_secret"] == REDACTED and data["nested"] == {"api_key": REDACTED, "ok": "fine"}
    assert data["prompt_tokens"] == 12 and data["total_tokens"] == 30 and data["tool"] == "search_emails"


# ---------- structured output ----------
def _record(msg, *args, level=logging.INFO, exc_info=None, **extra):
    record = logging.LogRecord("app.test", level, __file__, 1, msg, args, exc_info)
    record.__dict__.update(extra)
    return record


def test_json_lines_have_utc_timestamp_level_context_and_extras():
    with log_context(request_id="req-12345678"):
        bind(user_id="u-1")
        line = json.loads(JsonFormatter().format(_record("gmail search completed result_count=%d", 3,
                                                         http={"status": 200})))
    assert line["timestamp"].endswith("Z") and "T" in line["timestamp"]
    assert line["level"] == "INFO" and line["logger"] == "app.test"
    assert line["message"] == "gmail search completed result_count=3"
    assert line["request_id"] == "req-12345678" and line["user_id"] == "u-1" and line["http"] == {"status": 200}


def test_errors_include_redacted_stack_traces():
    try:
        raise RuntimeError("token exchange failed for Bearer ya29.leakedTOKEN")
    except RuntimeError:
        import sys
        line = json.loads(JsonFormatter().format(_record("boom", level=logging.ERROR, exc_info=sys.exc_info())))
    assert line["exception"]["type"] == "RuntimeError" and "Traceback" in line["exception"]["stack_trace"]
    assert "ya29.leaked" not in json.dumps(line)
    text = TextFormatter().format(_record("boom", level=logging.ERROR, exc_info=(RuntimeError, RuntimeError("x"), None)))
    assert "RuntimeError" in text


def test_a_broken_log_call_does_not_raise():
    bad = _record("count=%d", "not-a-number")  # getMessage() raises TypeError
    assert "log formatting failed" in JsonFormatter().format(bad)
    assert "log formatting failed" in TextFormatter().format(bad)


def test_configured_handler_redacts_everything_it_emits(json_logs):
    logging.getLogger("app.test").warning("refresh failed %s", "refresh_token=1//0gLongRefreshTokenValue-abcdefghijk",
                                          extra={"authorization": "Bearer abc"})
    [line] = [entry for entry in json_logs() if entry["logger"] == "app.test"]
    assert "1//0gLong" not in json.dumps(line) and line["authorization"] == REDACTED


# ---------- HTTP request logging and correlation ----------
def test_request_id_is_generated_returned_and_logged_without_query_string(client, json_logs):
    r = client.get("/api/auth/google/callback?code=SECRETCODE123&state=abc")
    rid = r.headers["x-request-id"]
    assert len(rid) == 32
    received, line = [entry for entry in json_logs() if entry["logger"] == "app.http"]
    assert received["message"].startswith("http request received") and received["request_id"] == rid
    assert line["request_id"] == rid and line["http"]["method"] == "GET" and line["http"]["status"] == 302
    assert line["http"]["path"] == "/api/auth/google/callback" and isinstance(line["http"]["duration_ms"], int)
    assert line["http"]["route"] == "/api/auth/google/callback"
    assert "SECRETCODE123" not in json.dumps(json_logs())


def test_incoming_request_id_is_reused_and_malformed_ones_replaced(client):
    assert client.get("/api/health", headers={"X-Request-ID": "client-trace-0001"}).headers["x-request-id"] == \
        "client-trace-0001"
    assert client.get("/api/health", headers={"X-Request-ID": "bad id\n"}).headers["x-request-id"] != "bad id\n"


def test_unhandled_exception_is_logged_with_stack_trace_and_returns_request_id(json_logs):
    app = FastAPI()
    app.add_middleware(RequestContextMiddleware)

    @app.get("/boom")
    def boom():
        raise RuntimeError("database password=hunter2 exploded")

    r = TestClient(app).get("/boom")
    assert r.status_code == 500 and r.json()["detail"]["request_id"] == r.headers["x-request-id"]
    lines = json_logs()
    err = next(e for e in lines if e["message"].startswith("unhandled exception"))
    assert err["level"] == "ERROR" and "Traceback" in err["exception"]["stack_trace"]
    assert err["request_id"] == r.headers["x-request-id"] and "hunter2" not in json.dumps(lines)
    done = next(e for e in lines if e["message"].startswith("http request completed"))
    assert done["level"] == "ERROR" and done["http"]["status"] == 500


def test_authenticated_requests_log_the_user_id(client, make_user, json_logs):
    user, headers = make_user()
    client.get("/api/conversations", headers=headers)
    [line] = [e for e in json_logs() if e["logger"] == "app.http" and "completed" in e["message"]]
    assert line["user_id"] == str(user.id)


def test_route_template_is_logged_instead_of_ids(client, make_user, json_logs):
    user, headers = make_user()
    client.get(f"/api/conversations/{uuid.uuid4()}", headers=headers)
    [line] = [e for e in json_logs() if e["logger"] == "app.http" and "completed" in e["message"]]
    assert line["http"]["route"] == "/api/conversations/{conversation_id}" and line["http"]["status"] == 404


def test_authentication_failures_are_logged_with_a_reason(client, make_user, json_logs):
    from app.core import security
    user, _ = make_user()
    expired = security._encode({"sub": str(user.id), "type": "access"}, -1)
    assert client.get("/api/auth/me").json()["detail"] == "Not authenticated"
    client.get("/api/auth/me", headers={"Authorization": "Bearer not-a-jwt"})
    r = client.get("/api/auth/me", headers={"Authorization": f"Bearer {expired}"})
    assert r.status_code == 401 and r.json()["detail"]["code"] == "session_expired"
    reasons = [e["message"] for e in json_logs() if e["logger"] == "app.auth"]
    assert reasons == ["authentication failed reason=missing_token", "authentication failed reason=invalid_token",
                       "authentication failed reason=session_expired"]
    assert "not-a-jwt" not in json.dumps(json_logs()) and expired not in json.dumps(json_logs())


def test_context_helpers_default_and_restore():
    assert current_correlation_id() == "-"
    with log_context(request_id="abc-12345"):
        assert current_correlation_id() == "abc-12345"
    assert current_correlation_id() == "-"


# ---------- agent, MCP, Gmail and Calendar logging share one correlation id ----------
def test_one_request_id_correlates_api_workflow_agent_mcp_and_gmail_logs(env, client, json_logs):  # noqa: F811
    env.gmail.messages["r1"] = mime.parse_message(regression_payload())
    env.llm.email_answer = REG_ANSWER
    rid = "corr-test-0001"
    r = client.post(f"/api/conversations/{env.conv}/messages", json={"content": REG_QUESTION},
                    headers={**env.h, "X-Request-ID": rid})
    assert r.status_code == 201 and r.headers["x-request-id"] == rid
    lines = json_logs()
    by_logger = {}
    for e in lines:
        by_logger.setdefault(e["logger"], []).append(e)
    for logger in ("app.http", "app.workflows.chat_service", "app.workflows.graph", "app.mcp.client",
                   "app.services.execution_history", "app.agents.email_understanding_agent",
                   "app.agents.gmail_agents"):
        assert logger in by_logger, logger
        assert all(e.get("request_id") == rid for e in by_logger[logger]), logger
    messages = [e["message"] for e in lines]
    assert any("intent detected" in m and "intent=search_emails" in m for m in messages)
    assert any("agent selected" in m and "agent=SearchEmailAgent" in m for m in messages)
    assert any("mcp tool invocation" in m and "tool=gmail.search_emails" in m for m in messages)
    assert any("mcp tool invocation" in m and "tool=gmail.read_email" in m for m in messages)
    assert any("email answer grounded" in m and "grounded_relevant=1" in m for m in messages)
    assert any("meeting extracted from email" in m and "has_time=False" in m for m in messages)
    run_lines = by_logger["app.services.execution_history"]
    assert all(e.get("execution_id") for e in run_lines[1:])
    # Neither the email body nor the user's request text reaches the logs.
    dump = json.dumps(lines)
    assert REG_BODY not in dump and "inform you that a meeting" not in dump and REG_QUESTION not in dump


def test_gmail_full_message_retrieval_logs_mime_parsing_results(caplog):
    class FakeHTTP:
        def request(self, method, path, params=None):
            return regression_payload()

    client = GmailClient(lambda: "token")
    client.http = FakeHTTP()
    with caplog.at_level(logging.INFO, logger="app.integrations.gmail.client"):
        assert client.get_message("r1").body_text == REG_BODY
    [msg] = [r.getMessage() for r in caplog.records if "gmail full message parsed" in r.getMessage()]
    assert "format=full body_source=text/plain" in msg and f"body_chars={len(REG_BODY)}" in msg
    assert "text_plain_parts=1 text_html_parts=1" in msg and REG_BODY not in caplog.text


def test_undecodable_mime_part_is_logged_not_raised(caplog):
    with caplog.at_level(logging.WARNING, logger="app.integrations.gmail.mime"):
        m = mime.parse_message({"id": "x", "payload": {"mimeType": "text/plain", "body": {"data": "!!!*not base64*"}}})
    assert m.body_text == "" and "base64url decode failed" in caplog.text


def test_calendar_and_approval_operations_are_logged(env, caplog, json_logs):  # noqa: F811
    env.llm.intents = ['{"service":"calendar","intent":"create_event","entities":{"title":"Demo","date":"2026-11-25",'
                       '"time":"15:00"}}']
    with caplog.at_level(logging.INFO):
        t = env.say("Create a Demo event tomorrow at 3 PM")
        env.decide(t["workflow"]["id"])
    # Calendar logs are written inside the in-process MCP server; they still carry the HTTP request id.
    cal = [e for e in json_logs() if e["logger"] == "app.integrations.calendar.service"]
    assert cal and all(len(e.get("request_id", "")) == 32 for e in cal)
    text = caplog.text
    assert "calendar availability checked" in text and "calendar event search completed on_date=2026-11-25" in text
    assert "approval requested" in text and "approval decision requested" in text and "decision=approved" in text
    assert "calendar event created" in text and "verified=true" in text
    assert "execution approval recorded" in text and "approval_status=approved action_status=succeeded" in text


def test_llm_failures_are_logged_with_provider_and_model(env, caplog):  # noqa: F811
    from app.services.llm.base import LLMProviderError
    env.llm.intents = [LLMProviderError("provider unavailable after 3 attempts (HTTP 503)")]
    with caplog.at_level(logging.INFO):
        t = env.say("Schedule something with Rahul")
    assert t["status"] == "failed"
    assert "LLM execution failed" in caplog.text and "provider=openrouter" in caplog.text
    assert "workflow node failed" in caplog.text and "code=llm_unavailable" in caplog.text
