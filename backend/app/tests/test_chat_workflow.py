"""End-to-end: HTTP -> LangGraph -> agents -> MCP (real protocol, in-process servers) -> real Gmail/Calendar services
over fake Google clients -> approval -> execution through MCP. Only the LLM and Google's HTTP layer are faked."""
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import pytest

from app.api.deps import get_mcp_gateway_factory
from app.core.security import encrypt_secret
from app.database.models import Message, OAuthCredential, SessionContext, WorkflowExecution
from app.main import app
from app.mcp.client import MCPGateway, inprocess_factories
from app.services.llm import get_llm_router
from app.services.llm.base import LLMConfigError
from app.services.token_service import TokenUnavailableError
from app.tests.conftest import ALL_SCOPES
from app.tests.fakes import NOW, FakeCal, FakeGmailClient, ScriptedLLM, calendar_service, gateway_factory, gmail_service, router
from app.workflows import chat_service


@pytest.fixture()
def env(client, make_user, db_session, monkeypatch):
    monkeypatch.setattr(chat_service, "clock", lambda tz: NOW)
    user, headers = make_user()
    user.timezone = "Asia/Kolkata"
    db_session.add(OAuthCredential(user_id=user.id, access_token_enc=encrypt_secret("tok"), scopes=ALL_SCOPES,
                                   expires_at=datetime.now(timezone.utc) + timedelta(hours=1)))
    db_session.commit()

    class Env:
        gmail = FakeGmailClient()
        cal = FakeCal()
        llm = ScriptedLLM()

        def __init__(self):
            self.user, self.h = user, headers
            app.dependency_overrides[get_llm_router] = lambda: router(self.llm)
            app.dependency_overrides[get_mcp_gateway_factory] = \
                lambda: gateway_factory(gmail_service(self.gmail), calendar_service(self.cal))
            self.conv = client.post("/api/conversations", json={}, headers=headers).json()["id"]

        def say(self, text, conv=None):
            r = client.post(f"/api/conversations/{conv or self.conv}/messages", json={"content": text}, headers=self.h)
            assert r.status_code == 201, r.text
            return r.json()

        def decide(self, wf_id, approve=True):
            return client.post(f"/api/workflows/{wf_id}/decision", json={"approve": approve}, headers=self.h)

    e = Env()
    e.gmail, e.cal, e.llm = FakeGmailClient(), FakeCal(), ScriptedLLM()
    return e


def nodes(turn):
    return [t["node"] for t in turn["assistant_message"]["meta"]["trace"]]


# ---------- Example 1: multi-turn reschedule ----------
def test_move_meeting_with_rahul_across_three_turns_then_approve(env, db_session):
    att = {"attendees": [{"email": "rahul@x.com", "displayName": "Rahul"}]}
    env.cal.add("e1", "Project review", "2026-11-26T10:00:00+05:30", "2026-11-26T11:00:00+05:30", **att)
    env.cal.add("e2", "Weekly sync", "2026-11-25T16:00:00+05:30", "2026-11-25T16:30:00+05:30", **att)
    env.llm.intents = [
        '{"service":"calendar","intent":"reschedule_event","entities":{"person":"Rahul","date":"Friday"}}',
        '{"intent":"unknown","entities":{"title":"project review"},"is_follow_up":true}',
        '{"intent":"unknown","entities":{"time":"3 PM"},"is_follow_up":true}',
    ]
    t1 = env.say("Move my meeting with Rahul to Friday.")
    assert t1["status"] == "needs_clarification" and "I found 2 meetings with Rahul" in t1["assistant_message"]["content"]
    meta = t1["assistant_message"]["meta"]
    assert meta["selection"]["service"] == "calendar" and meta["mcp_calls"][0]["tool"] == "list_events"
    assert nodes(t1) == ["load_context", "select_service", "recognize_intent", "enhance_context", "validate_parameters",
                         "calendar_agent", "mcp_tools", "calendar_agent", "generate_response"]

    t2 = env.say("The project review.")
    assert t2["status"] == "needs_clarification"
    assert t2["assistant_message"]["content"].startswith('What time should I move "Project review"')
    assert "selected event_id from the options shown" in t2["assistant_message"]["meta"]["context_notes"]

    t3 = env.say("3 PM.")
    assert t3["status"] == "awaiting_approval" and t3["workflow"]["status"] == "awaiting_approval"
    assert t3["workflow"]["request"]["event_id"] == "e1"
    assert t3["workflow"]["request"]["new_start"] == "2026-11-27T15:00:00+05:30"  # Friday in the user's timezone
    assert env.cal.events["e1"]["start"]["dateTime"] == "2026-11-26T10:00:00+05:30"  # nothing changed before approval

    done = env.decide(t3["workflow"]["id"])
    assert done.status_code == 200 and done.json()["status"] == "succeeded"
    assert env.cal.events["e1"]["start"]["dateTime"] == "2026-11-27T15:00:00+05:30"
    assert env.cal.events["e1"]["end"]["dateTime"] == "2026-11-27T16:00:00+05:30"  # duration kept
    conv_id = uuid.UUID(t3["workflow"]["conversation_id"])
    last = db_session.query(Message).filter_by(conversation_id=conv_id).order_by(Message.created_at).all()[-1]
    assert last.content.startswith('Moved "Project review"') and last.meta["status"] == "succeeded"


# ---------- Example 2: search then "summarize the latest one" ----------
def test_find_professor_emails_then_summarize_latest_one(env):
    env.gmail.add("m1", "Professor Rao <rao@uni.edu>", "Syllabus", "Week 1 covers sets.", "2026-11-20T09:00:00+00:00")
    env.gmail.add("m2", "Professor Rao <rao@uni.edu>", "Exam moved", "The exam moves to Friday 10am.", "2026-11-23T09:00:00+00:00")
    env.gmail.add("m3", "Shop <deals@shop.com>", "Sale", "50% off", "2026-11-24T09:00:00+00:00")
    env.llm.intents = ['{"intent":"search_emails","entities":{"person":"professor","query":"from:professor"}}',
                       '{"intent":"summarize_emails","entities":{},"is_follow_up":true}']
    env.llm.summary = {"summary": "The exam has moved to Friday at 10am.", "key_points": ["Exam Friday 10am"],
                       "action_items": ["Update your calendar"]}
    t1 = env.say("Find emails from my professor.")
    assert t1["status"] == "completed" and "Here are the 2 matching email(s)" in t1["assistant_message"]["content"]
    assert "Sale" not in t1["assistant_message"]["content"]

    t2 = env.say("Summarize the latest one.")
    assert t2["status"] == "completed" and "The exam has moved to Friday at 10am." in t2["assistant_message"]["content"]
    meta = t2["assistant_message"]["meta"]
    assert meta["entities"] == {"message_id": "m2"}
    assert [c["tool"] for c in meta["mcp_calls"]] == ["read_email"]
    assert [c["role"] for c in meta["llm_calls"]] == ["intent", "summarization"]
    summary_prompt = [c for c in env.llm.calls if c["role"] == "summary"][0]["messages"][1].content
    assert "The exam moves to Friday" in summary_prompt and "Week 1" not in summary_prompt


# ---------- Example 3: recipient carried over, pronoun resolved, approval gate ----------
def test_send_email_then_tell_him_resolves_recipient_and_waits_for_approval(env):
    env.llm.intents = ['{"intent":"send_email","entities":{"recipient_email":"professor@example.com"}}',
                       '{"intent":"send_email","entities":{"body":"I\'ll miss tomorrow\'s lecture"},"is_follow_up":true}']
    env.llm.task = {"subject": "Missing tomorrow's lecture", "body": "Dear Professor,\n\nI will miss tomorrow's lecture.\n\nA"}
    t1 = env.say("Send an email to professor@example.com.")
    assert t1["status"] == "needs_clarification" and t1["assistant_message"]["content"] == "What should the message say?"
    t2 = env.say("Tell him I'll miss tomorrow's lecture.")
    assert t2["status"] == "awaiting_approval"
    assert t2["workflow"]["request"]["to"] == ["professor@example.com"]
    assert t2["workflow"]["request"]["subject"] == "Missing tomorrow's lecture"
    assert [c["role"] for c in t2["assistant_message"]["meta"]["llm_calls"]] == ["intent", "task_automation"]
    assert env.gmail.sent == []  # nothing is sent without approval

    done = env.decide(t2["workflow"]["id"]).json()
    assert done["status"] == "succeeded" and done["result"]["value"]["verified"] is True
    assert len(env.gmail.sent) == 1
    assert env.decide(t2["workflow"]["id"]).status_code == 409  # cannot be executed twice


def test_rejected_action_never_executes(env):
    env.llm.intents = ['{"intent":"create_event","entities":{"person":"Rahul","date":"tomorrow","time":"3 PM"}}']
    t = env.say("Schedule a meeting with Rahul tomorrow at 3 PM")
    assert t["status"] == "awaiting_approval" and t["workflow"]["request"]["start"] == "2026-11-25T15:00:00+05:30"
    assert env.decide(t["workflow"]["id"], approve=False).json()["status"] == "rejected"
    assert env.cal.events == {}


# ---------- conditional branching, errors, isolation ----------
def test_unknown_request_asks_for_clarification_without_touching_google(env):
    env.llm.intents = ['{"intent":"unknown","clarification_question":"I can help with Gmail and Google Calendar. What would you like to do?"}']
    t = env.say("What's the weather?")
    assert t["status"] == "needs_clarification" and "gmail_agent" not in nodes(t) and "calendar_agent" not in nodes(t)
    assert t["assistant_message"]["meta"]["mcp_calls"] == []


def test_missing_parameters_stop_before_specialized_agents(env):
    env.llm.intents = ['{"intent":"create_event","entities":{"person":"Rahul Sharma"}}',
                       '{"intent":"unknown","entities":{"date":"tomorrow"}}']
    t1 = env.say("Schedule a meeting with Rahul Sharma.")
    assert t1["status"] == "needs_clarification" and t1["assistant_message"]["content"] == "Which date? What time?"
    assert nodes(t1)[-1] == "generate_response" and "calendar_agent" not in nodes(t1)
    t2 = env.say("Tomorrow.")
    assert t2["assistant_message"]["content"] == "What time?"
    assert t2["assistant_message"]["meta"]["entities"] == {"person": "Rahul Sharma", "date": "2026-11-25"}


def test_llm_not_configured_is_reported_and_pending_request_survives(env, db_session):
    env.llm.intents = ['{"intent":"send_email","entities":{"recipient_email":"a@example.com"}}',
                       LLMConfigError("OPENROUTER_API_KEY is not set")]
    env.say("Email a@example.com")
    t = env.say("say hello")
    assert t["status"] == "failed" and "isn't configured" in t["assistant_message"]["content"]
    assert t["assistant_message"]["meta"]["trace"][-2] == {"node": "recognize_intent", "status": "error",
                                                           "detail": "llm_not_configured"}
    sc = db_session.query(SessionContext).one()
    assert sc.pending_intent["intent"] == "send_email"


def test_mcp_tool_failure_propagates_as_failed_turn(env):
    @contextmanager
    def disconnected():
        raise TokenUnavailableError("Gmail is not connected")
        yield  # pragma: no cover

    app.dependency_overrides[get_mcp_gateway_factory] = \
        lambda: (lambda db, user: MCPGateway(inprocess_factories(disconnected, disconnected)))
    env.llm.intents = ['{"intent":"search_emails","entities":{"query":"invoice"}}']
    t = env.say("find invoice emails")
    assert t["status"] == "failed" and "reconnect" in t["assistant_message"]["content"].lower()
    assert t["assistant_message"]["meta"]["error"]["code"] == "google_reconnect_required"


def test_conversations_are_isolated_between_users(env, client, make_user):
    _, other = make_user("b@example.com", "sub-b")
    r = client.post(f"/api/conversations/{env.conv}/messages", json={"content": "hi"}, headers=other)
    assert r.status_code == 404


def test_context_does_not_leak_between_conversations(env, client):
    env.llm.intents = ['{"intent":"send_email","entities":{"recipient_email":"prof@example.com"}}',
                       '{"intent":"send_email","entities":{"body":"hello"},"is_follow_up":true}']
    env.say("Email prof@example.com")
    other_conv = client.post("/api/conversations", json={}, headers=env.h).json()["id"]
    t = env.say("Tell him hello", conv=other_conv)
    assert t["status"] == "needs_clarification" and "email address" in t["assistant_message"]["content"]


def test_mcp_tool_discovery_endpoint(env, client):
    r = client.get("/api/mcp/tools", headers=env.h).json()
    assert r["transport"] == "inprocess"
    assert len(r["servers"]["gmail"]["tools"]) == 6 and len(r["servers"]["calendar"]["tools"]) == 6


def test_workflow_records_reference_the_conversation(env, db_session):
    env.llm.intents = ['{"intent":"cancel_event","entities":{"event_id":"e9"}}']
    env.cal.add("e9", "Dentist", "2026-11-28T10:00:00+05:30", "2026-11-28T11:00:00+05:30")
    t = env.say("Cancel event e9")
    wf = db_session.get(WorkflowExecution, uuid.UUID(t["workflow"]["id"]))
    assert wf.intent == "cancel_event" and str(wf.conversation_id) == env.conv and wf.idempotency_key.startswith("chat:")
    assert env.decide(t["workflow"]["id"]).json()["status"] == "succeeded" and "e9" not in env.cal.events
