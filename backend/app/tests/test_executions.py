"""Execution history (Activity view): persisted per request, updated by approval decisions, isolated per user,
and free of email content and credentials."""
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.api.routes.health import EXPECTED_DB_REVISION
from app.database.models import AgentRun
from app.integrations.gmail import mime
from app.services import execution_history
from app.services.llm.base import LLMConfigError
from app.tests.test_chat_workflow import env  # noqa: F401  (end-to-end fixture)
from app.tests.test_email_search_grounding import REG_ANSWER, REG_BODY, REG_QUESTION, regression_payload

CREATE_EVENT = '{"service":"calendar","intent":"create_event","entities":{"title":"Demo","date":"2026-11-25","time":"15:00"}}'


def test_each_request_is_recorded_with_intent_agent_tools_models_and_request_id(env, client):  # noqa: F811
    env.gmail.messages["r1"] = mime.parse_message(regression_payload())
    env.llm.email_answer = REG_ANSWER
    r = client.post(f"/api/conversations/{env.conv}/messages", json={"content": REG_QUESTION},
                    headers={**env.h, "X-Request-ID": "exec-test-0001"})
    turn = r.json()
    [summary] = client.get("/api/executions", headers=env.h).json()
    assert summary["id"] == turn["execution_id"] == turn["assistant_message"]["meta"]["execution_id"]
    assert summary["request_id"] == "exec-test-0001" and summary["request_text"] == REG_QUESTION
    assert summary["status"] == "needs_clarification" and summary["intent"] == "search_emails"
    assert summary["service"] == "gmail" and summary["agent"] == "SearchEmailAgent" and summary["tool_count"] == 2
    assert summary["approval_status"] is None and summary["duration_ms"] >= 0 and summary["completed_at"]

    detail = client.get(f"/api/executions/{summary['id']}", headers=env.h).json()
    assert [s["node"] for s in detail["steps"]][:3] == ["load_context", "select_service", "recognize_intent"]
    assert all(isinstance(s["ms"], int) for s in detail["steps"])
    assert [(t["server"], t["tool"], t["ok"], t["phase"]) for t in detail["tools"]] == [
        ("gmail", "search_emails", True, "workflow"), ("gmail", "read_email", True, "workflow")]
    assert [(c["role"], c["provider"], c["ok"]) for c in detail["llm_calls"]] == [("email_understanding", "openrouter", True)]
    assert detail["llm_calls"][0]["model"] and isinstance(detail["llm_calls"][0]["duration_ms"], int)
    assert detail["user_message_id"] == turn["user_message"]["id"]
    assert detail["assistant_message_id"] == turn["assistant_message"]["id"]
    # Metadata only: the email body never enters execution history.
    assert REG_BODY not in json.dumps(detail) and "inform you" not in json.dumps(detail)


def test_approval_lifecycle_is_reflected_in_the_execution(env, client):  # noqa: F811
    env.llm.intents = [CREATE_EVENT]
    t = env.say("Create a Demo event tomorrow at 3 PM")
    run = client.get(f"/api/executions/{t['execution_id']}", headers=env.h).json()
    assert run["status"] == "awaiting_approval" and run["approval_status"] == "pending"
    assert run["workflow_id"] == t["workflow"]["id"] and run["agent"] == "CreateEventAgent"
    assert env.decide(t["workflow"]["id"]).json()["status"] == "succeeded"
    run = client.get(f"/api/executions/{t['execution_id']}", headers=env.h).json()
    assert run["status"] == "completed" and run["approval_status"] == "approved" and run["action_status"] == "succeeded"
    assert ("calendar", "create_event", "approval") in [(x["server"], x["tool"], x["phase"]) for x in run["tools"]]


def test_rejected_approval_is_recorded(env, client):  # noqa: F811
    env.llm.intents = [CREATE_EVENT]
    t = env.say("Create a Demo event tomorrow at 3 PM")
    env.decide(t["workflow"]["id"], approve=False)
    run = client.get(f"/api/executions/{t['execution_id']}", headers=env.h).json()
    assert run["status"] == "rejected" and run["approval_status"] == "rejected" and run["action_status"] is None
    assert env.cal.events == {}


def test_failures_are_recorded_with_error_details(env, client):  # noqa: F811
    env.llm.intents = [LLMConfigError("OPENROUTER_API_KEY is not set")]
    env.say("Schedule something with Rahul")
    [run] = client.get("/api/executions", headers=env.h).json()
    assert run["status"] == "failed" and run["error_code"] == "llm_not_configured"
    detail = client.get(f"/api/executions/{run['id']}", headers=env.h).json()
    assert "OPENROUTER_API_KEY" in detail["error_message"]
    assert detail["llm_calls"][0]["ok"] is False and detail["llm_calls"][0]["error"] == "LLMConfigError"


def test_users_cannot_see_each_others_execution_history(env, client, make_user):  # noqa: F811
    env.llm.intents = [CREATE_EVENT]
    t = env.say("Create a Demo event tomorrow at 3 PM")
    _, other = make_user("b@example.com", "sub-b")
    assert client.get("/api/executions", headers=other).json() == []
    assert client.get(f"/api/executions/{t['execution_id']}", headers=other).status_code == 404
    assert client.get("/api/executions", headers=env.h).status_code == 200
    assert client.get("/api/executions").status_code == 401


def test_history_failure_never_breaks_the_conversation(env, monkeypatch):  # noqa: F811
    from app.services import execution_history

    def broken_commit(db, what):
        raise_ = what == "start"
        if raise_:
            db.rollback()
            return False
        return True
    monkeypatch.setattr(execution_history, "_safe_commit", broken_commit)
    env.llm.intents = [CREATE_EVENT]
    t = env.say("Create a Demo event tomorrow at 3 PM")
    assert t["status"] == "awaiting_approval" and t["execution_id"] is None


def test_deleting_a_conversation_removes_its_history(env, client, db_session):  # noqa: F811
    env.llm.intents = [CREATE_EVENT]
    env.say("Create a Demo event tomorrow at 3 PM")
    assert client.delete(f"/api/conversations/{env.conv}", headers=env.h).status_code == 204
    assert db_session.query(AgentRun).count() == 0


def test_a_run_killed_mid_turn_is_reported_as_timed_out_not_running(env, client, db_session):  # noqa: F811
    env.llm.intents = [CREATE_EVENT]
    env.say("Create a Demo event tomorrow at 3 PM")
    run = db_session.query(AgentRun).one()
    fresh = AgentRun(user_id=run.user_id, conversation_id=run.conversation_id, request_id="still-running-01",
                     request_text="x", status="running")
    run.status, run.error_code = "running", None
    run.created_at = datetime.now(timezone.utc) - timedelta(seconds=execution_history.STALE_RUN_SECONDS + 5)
    db_session.add(fresh)
    db_session.commit()

    by_request = {r["request_id"]: r for r in client.get("/api/executions", headers=env.h).json()}
    assert by_request["still-running-01"]["status"] == "running"
    stale = by_request[run.request_id]
    assert stale["status"] == "failed" and stale["error_code"] == "timed_out"
    assert client.get(f"/api/executions/{stale['id']}", headers=env.h).json()["status"] == "failed"


def test_llm_calls_inside_a_turn_run_under_the_turn_time_budget(env, monkeypatch):  # noqa: F811
    import time

    from app.agents.intent_agent import IntentRecognitionAgent
    from app.services.llm import base
    remaining, recognize = [], IntentRecognitionAgent.recognize

    def spy(self, *args, **kwargs):
        remaining.append(base._deadline.get() and base._deadline.get() - time.monotonic())
        return recognize(self, *args, **kwargs)
    monkeypatch.setattr(IntentRecognitionAgent, "recognize", spy)
    env.llm.intents = [CREATE_EVENT]
    env.say("Create a Demo event tomorrow at 3 PM")
    assert remaining and 200 < remaining[0] <= 240


def test_expected_revision_matches_the_latest_migration():
    versions = Path(__file__).resolve().parents[2] / "alembic" / "versions"
    revisions = {line.split("=")[1].strip().strip('"') for f in versions.glob("*.py")
                 for line in f.read_text().splitlines() if line.startswith("revision =")}
    downs = {line.split("=")[1].strip().strip('"') for f in versions.glob("*.py")
             for line in f.read_text().splitlines() if line.startswith("down_revision =")}
    assert revisions - downs == {EXPECTED_DB_REVISION}
