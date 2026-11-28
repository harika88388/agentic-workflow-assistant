"""Streaming turn endpoint: live LangGraph/MCP progress followed by the persisted result (NDJSON)."""
import json
import logging

import anyio

from app.integrations.gmail import mime
from app.tests.test_chat_workflow import env  # noqa: F401  (end-to-end fixture)
from app.tests.test_email_search_grounding import REG_ANSWER, REG_BODY, REG_QUESTION, regression_payload


def stream(client, env, text, conv=None, headers=None):  # noqa: F811
    r = client.post(f"/api/conversations/{conv or env.conv}/messages/stream", json={"content": text},
                    headers={**env.h, **(headers or {})})
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("application/x-ndjson")
    return r, [json.loads(line) for line in r.text.splitlines() if line.strip()]


def test_stream_reports_progress_tools_and_final_answer(env, client):  # noqa: F811
    env.gmail.messages["r1"] = mime.parse_message(regression_payload())
    env.llm.email_answer = REG_ANSWER
    r, events = stream(client, env, REG_QUESTION, headers={"X-Request-ID": "stream-test-01"})
    assert events[0]["type"] == "started" and events[0]["request_id"] == "stream-test-01"
    assert events[0]["execution_id"] and r.headers["x-request-id"] == "stream-test-01"
    nodes = [e["node"] for e in events if e["type"] == "progress" and e["status"] == "ok"]
    assert nodes[:5] == ["load_context", "select_service", "recognize_intent", "enhance_context",
                         "validate_parameters"] and nodes[-1] == "persist_turn"
    running = [e for e in events if e["type"] == "progress" and e["status"] == "running"]
    assert running[0]["label"] == "Loading conversation context"
    tools = [(e["tool"], e["status"]) for e in events if e["type"] == "tool"]
    assert tools == [("search_emails", "running"), ("search_emails", "ok"), ("read_email", "running"),
                     ("read_email", "ok")]
    assert events[-1]["type"] == "result" and sum(e["type"] in ("result", "error") for e in events) == 1
    turn = events[-1]["turn"]
    assert turn["status"] == "needs_clarification" and REG_BODY in turn["assistant_message"]["content"]
    assert turn["execution_id"] == events[0]["execution_id"]
    # The turn is persisted exactly as the non-streaming endpoint would.
    msgs = client.get(f"/api/conversations/{env.conv}", headers=env.h).json()["messages"]
    assert [m["role"] for m in msgs] == ["user", "assistant"] and msgs[1]["id"] == turn["assistant_message"]["id"]


def test_stream_approval_request_then_decision(env, client):  # noqa: F811
    env.llm.intents = ['{"service":"calendar","intent":"create_event","entities":{"title":"Demo",'
                       '"date":"2026-11-25","time":"15:00"}}']
    _, events = stream(client, env, "Create a Demo event tomorrow at 3 PM")
    turn = events[-1]["turn"]
    assert turn["status"] == "awaiting_approval" and turn["workflow"]["status"] == "awaiting_approval"
    assert "Nothing happens until you approve" in turn["assistant_message"]["content"] and env.cal.events == {}
    assert env.decide(turn["workflow"]["id"]).json()["status"] == "succeeded" and len(env.cal.events) == 1


def test_stream_failures_arrive_as_a_failed_turn(env, client):  # noqa: F811
    from app.services.llm.base import LLMProviderError
    env.llm.intents = [LLMProviderError("provider unavailable")]
    _, events = stream(client, env, "Schedule something with Rahul")
    assert any(e["type"] == "progress" and e["status"] == "error" and e["detail"] == "llm_unavailable" for e in events)
    assert events[-1]["type"] == "result" and events[-1]["turn"]["status"] == "failed"


def test_stream_unexpected_errors_arrive_as_error_events(env, client, monkeypatch):  # noqa: F811
    from app.workflows import chat_service

    def explode(*a, **k):
        raise RuntimeError("boom")
    monkeypatch.setattr(chat_service, "run_turn", explode)
    _, events = stream(client, env, "hello")
    assert events == [{"type": "error", "code": "internal_error", "request_id": events[0]["request_id"],
                       "message": "Something went wrong while processing your request."}]


def test_stream_lifecycle_is_logged(env, client, caplog):  # noqa: F811
    env.gmail.messages["r1"] = mime.parse_message(regression_payload())
    env.llm.email_answer = REG_ANSWER
    with caplog.at_level(logging.INFO, logger="app.api.routes.chat"):
        stream(client, env, REG_QUESTION, headers={"X-Request-ID": "stream-log-01"})
    lines = [r.getMessage() for r in caplog.records if r.name == "app.api.routes.chat"]
    assert lines[0].startswith("stream started request_id=stream-log-01")
    assert lines[1].startswith("stream first event sent request_id=stream-log-01 type=started")
    assert lines[-1].startswith("stream completed request_id=stream-log-01 outcome=result")
    assert "progress_events=" in lines[-1] and not any("disconnected" in line for line in lines)


def _post_stream_and_drop_connection(env, text: str, request_id: str) -> list[dict]:  # noqa: F811
    """Drive the ASGI app directly: the client vanishes as soon as the first body chunk is written."""
    from app.main import app
    body = json.dumps({"content": text}).encode()
    path = f"/api/conversations/{env.conv}/messages/stream"
    received = {"done": False}
    sent: list[dict] = []

    async def receive():
        if not received["done"]:
            received["done"] = True
            return {"type": "http.request", "body": body, "more_body": False}
        await anyio.sleep(30)
        return {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)
        if message["type"] == "http.response.body" and message.get("body"):
            raise OSError("connection reset by peer")

    scope = {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.4"}, "http_version": "1.1",
             "method": "POST", "scheme": "https", "path": path, "raw_path": path.encode(), "query_string": b"",
             "root_path": "", "client": ("203.0.113.9", 4000), "server": ("testserver", 443),
             "headers": [(b"host", b"testserver"), (b"content-type", b"application/json"),
                         (b"x-request-id", request_id.encode()),
                         (b"authorization", env.h["Authorization"].encode())]}
    anyio.run(app, scope, receive, send)
    return sent


def test_turn_is_persisted_and_recoverable_after_the_client_disconnects(env, client, caplog):  # noqa: F811
    env.gmail.messages["r1"] = mime.parse_message(regression_payload())
    env.llm.email_answer = REG_ANSWER
    with caplog.at_level(logging.INFO):
        sent = _post_stream_and_drop_connection(env, REG_QUESTION, "dropped-stream-01")
    assert sent[0]["type"] == "http.response.start" and sent[0]["status"] == 200
    lines = [r.getMessage() for r in caplog.records]
    assert any(line.startswith("stream client disconnected request_id=dropped-stream-01 reason=") for line in lines)
    assert "turn finished after client disconnect request_id=dropped-stream-01 completed=True" in lines
    assert not any(r.levelno >= logging.ERROR for r in caplog.records)

    # The client recovers the outcome by its own request id instead of resubmitting.
    [run] = client.get("/api/executions", params={"request_id": "dropped-stream-01"}, headers=env.h).json()
    assert run["status"] == "needs_clarification" and run["request_text"] == REG_QUESTION
    msgs = client.get(f"/api/conversations/{env.conv}", headers=env.h).json()["messages"]
    assert [m["role"] for m in msgs] == ["user", "assistant"] and REG_BODY in msgs[1]["content"]
    assert client.get("/api/executions", params={"request_id": "someone-elses-id"}, headers=env.h).json() == []


def test_executions_request_id_filter_is_validated(env, client):  # noqa: F811
    assert client.get("/api/executions", params={"request_id": "bad id!"}, headers=env.h).status_code == 422


def test_stream_requires_auth_and_ownership(env, client, make_user):  # noqa: F811
    _, other = make_user("b@example.com", "sub-b")
    url = f"/api/conversations/{env.conv}/messages/stream"
    assert client.post(url, json={"content": "hi"}).status_code == 401
    assert client.post(url, json={"content": "hi"}, headers=other).status_code == 404
    assert client.post(url, json={"content": ""}, headers=env.h).status_code == 422
