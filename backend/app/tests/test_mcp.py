import json
import os
import uuid
from contextlib import contextmanager

import pytest
from anyio.from_thread import start_blocking_portal
from mcp import Client
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.config import get_settings
from app.database.base import Base
from app.database.models import User
from app.mcp.calendar_server import build_calendar_server
from app.mcp.client import MCPGateway, gateway_for_user, inprocess_factories, stdio_factories
from app.mcp.errors import MCPToolError
from app.mcp.executors import MCPCalendarExecutor, MCPGmailExecutor
from app.mcp.gmail_server import build_gmail_server
from app.services.token_service import TokenUnavailableError
from app.tests.fakes import FakeCal, FakeGmailClient, calendar_service, gateway_factory, gmail_service

GMAIL_TOOLS = {"search_emails", "read_email", "get_email_thread", "send_email", "reply_email", "create_draft"}
CALENDAR_TOOLS = {"list_events", "get_event", "create_event", "update_event", "delete_event", "check_availability"}


@contextmanager
def scope_of(service):
    yield service


def _gateway(gmail=None, cal=None):
    return gateway_factory(gmail or gmail_service(), cal or calendar_service())(None, None)


# ---- registration (server side, over the protocol) ----
def test_servers_register_exactly_the_specified_tools_with_schemas():
    with start_blocking_portal() as portal:
        for server, expected in [(build_gmail_server(lambda: scope_of(None)), GMAIL_TOOLS),
                                 (build_calendar_server(lambda: scope_of(None)), CALENDAR_TOOLS)]:
            with portal.wrap_async_context_manager(Client(server, mode="legacy")) as c:
                tools = {t.name: t for t in portal.call(c.list_tools).tools}
                assert set(tools) == expected
                for t in tools.values():
                    assert t.description and t.input_schema["type"] == "object"
                    props = json.dumps(t.input_schema).lower()
                    assert "token" not in props.replace("idempotency_key", "") and "user_id" not in props
    send = build_gmail_server(lambda: scope_of(None))._tool_manager.get_tool("send_email")
    assert send.parameters["required"] == ["to", "subject", "body"]
    assert send.annotations.read_only_hint is False


# ---- discovery + invocation through the client ----
def test_gateway_discovers_tools_and_invokes_read_tools():
    client = FakeGmailClient()
    client.add("m1", "Prof <prof@uni.edu>", "Exam schedule", "The exam is on Friday.", "2026-11-23T10:00:00+00:00")
    with _gateway(gmail=gmail_service(client)) as gw:
        tools = gw.list_tools("gmail")
        assert {t.name for t in tools} == GMAIL_TOOLS
        assert {t.name for t in tools if t.read_only} == {"search_emails", "read_email", "get_email_thread"}
        found = gw.call_tool("gmail", "search_emails", {"query": "from:prof", "max_results": 5})
        assert [m["id"] for m in found["messages"]] == ["m1"] and found["messages"][0]["untrusted"] is True
        assert gw.call_tool("gmail", "read_email", {"message_id": "m1"})["message"]["body_text"] == "The exam is on Friday."
        assert gw.calls[-1] == {"server": "gmail", "tool": "read_email", "ok": True, "ms": gw.calls[-1]["ms"]}


def test_calendar_write_tools_keep_service_verification_and_idempotency():
    cal = FakeCal()
    with _gateway(cal=calendar_service(cal)) as gw:
        args = {"title": "Sync", "start": "2026-11-25T15:00:00", "idempotency_key": "wf-1"}
        first = gw.call_tool("calendar", "create_event", args)
        again = gw.call_tool("calendar", "create_event", args)
        assert first["verified"] and not first["duplicate"] and again["duplicate"] and len(cal.events) == 1
        assert first["event"]["start"] == "2026-11-25T15:00:00+05:30"  # naive time localized to the user's zone
        eid = first["event"]["id"]
        moved = gw.call_tool("calendar", "update_event", {"event_id": eid, "new_start": "2026-11-27T10:00:00"})
        assert moved["event"]["start"] == "2026-11-27T10:00:00+05:30" and moved["verified"]
        assert gw.call_tool("calendar", "delete_event", {"event_id": eid})["verified"]


def test_tool_errors_are_mapped_and_never_reported_as_success():
    with _gateway() as gw:
        with pytest.raises(MCPToolError) as e:
            gw.call_tool("gmail", "read_email", {"message_id": "missing"})
        assert e.value.code == "not_found" and gw.calls[-1]["ok"] is False
        with pytest.raises(MCPToolError) as e:
            gw.call_tool("gmail", "send_email", {"to": ["Rahul"], "subject": "x", "body": "y"})
        assert e.value.code == "invalid_arguments"
        with pytest.raises(MCPToolError) as e:
            gw.call_tool("calendar", "list_events", {"days": 500})  # schema bound (le=90) enforced by the server
        assert e.value.code == "invalid_arguments"


def test_undiscovered_tools_and_servers_are_refused():
    with _gateway() as gw:
        with pytest.raises(MCPToolError) as e:
            gw.call_tool("gmail", "delete_all_mail", {})
        assert e.value.code == "unknown_tool"
        with pytest.raises(MCPToolError) as e:
            gw.call_tool("drive", "x", {})
        assert e.value.code == "unknown_server"


def test_disconnected_google_account_is_a_reconnect_error():
    @contextmanager
    def not_connected():
        raise TokenUnavailableError("Gmail is not connected")
        yield  # pragma: no cover

    gw = MCPGateway(inprocess_factories(not_connected, not_connected))
    with gw:
        with pytest.raises(MCPToolError) as e:
            gw.call_tool("gmail", "search_emails", {"query": "x"})
    assert e.value.code == "google_reconnect_required"


def test_executors_run_approved_actions_through_mcp():
    client = FakeGmailClient()
    with _gateway(gmail=gmail_service(client)) as gw:
        sent = MCPGmailExecutor(gw).send_email(["a@example.com"], "Hi", "Hello")
        assert sent["verified"] and len(client.sent) == 1
        created = MCPCalendarExecutor(gw).create_event(
            title="Sync", start=__import__("datetime").datetime(2026, 11, 25, 9, 0), idempotency_key="k")
        assert created["verified"]
        assert [c["tool"] for c in gw.calls] == ["send_email", "create_event"]


def test_gateway_for_user_selects_transport(db_session):
    s = get_settings()
    assert gateway_for_user(db_session, uuid.uuid4(), s).transport == "inprocess"
    stdio = s.model_copy(update={"mcp_transport": "stdio"})
    factories = stdio_factories(stdio, uuid.uuid4())
    assert set(factories) == {"gmail", "calendar"}


# ---- real subprocess over stdio ----
def test_stdio_servers_run_as_subprocesses_and_bind_to_one_user(tmp_path):
    """Spawns `python -m app.mcp.gmail_server` / `calendar_server`, discovers tools over stdin/stdout and invokes
    one. The user has no Google credential, so the call must come back as a reconnect error: proving the server
    resolved the user from MCP_USER_ID and looked up credentials itself (no token in any argument)."""
    db_url = f"sqlite:///{(tmp_path / 'mcp.db').as_posix()}"
    engine = create_engine(db_url)
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine)() as db:
        user = User(google_sub="s", email="u@example.com", timezone="Asia/Kolkata")
        db.add(user)
        db.commit()
        uid = user.id
    engine.dispose()
    settings = get_settings().model_copy(update={"database_url": db_url, "mcp_transport": "stdio",
                                                 "mcp_timeout_seconds": 30})
    gw = MCPGateway(stdio_factories(settings, uid), timeout_seconds=30, transport="stdio")
    with gw:
        assert {t.name for t in gw.list_tools("gmail")} == GMAIL_TOOLS
        assert {t.name for t in gw.list_tools("calendar")} == CALENDAR_TOOLS
        with pytest.raises(MCPToolError) as e:
            gw.call_tool("calendar", "list_events", {"days": 3})
        assert e.value.code == "google_reconnect_required"
    assert os.environ.get("MCP_USER_ID") is None  # the user id goes only to the child process


def test_google_api_disabled_is_not_reported_as_reconnect_required():
    from app.integrations.errors import GoogleServiceDisabledError
    from app.mcp.errors import tool_errors

    with pytest.raises(Exception) as exc:
        with tool_errors():
            raise GoogleServiceDisabledError("Gmail API is not enabled", 403, "accessNotConfigured")
    assert "[google_api_not_enabled]" in str(exc.value)
    assert "reconnect" not in str(exc.value).lower()
