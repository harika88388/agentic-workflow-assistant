"""Specialized agents driven step by step: each step either asks for MCP tool calls or returns an outcome."""
from dataclasses import asdict

from app.agents.base import AgentContext
from app.agents.calendar_agents import build_calendar_agents
from app.agents.gmail_agents import build_gmail_agents
from app.agents.summarization_agent import SummarizationAgent
from app.agents.task_agent import TaskAutomationAgent
from app.integrations.gmail.mime import EmailMessageData
from app.tests.fakes import NOW, TZ, ScriptedLLM, router

SUMMARY = {"summary": "The exam moved to Friday.", "key_points": ["Exam on Friday"], "action_items": ["Revise"]}
TASK = {"subject": "Missing tomorrow's lecture", "body": "Dear Professor,\n\nI will miss tomorrow's lecture.\n\nAsha"}


def agents(summary=SUMMARY, task=TASK):
    llm = router(ScriptedLLM(summary=summary, task=task))
    log: list = []
    return {**build_gmail_agents(SummarizationAgent(llm, log), TaskAutomationAgent(llm, log)), **build_calendar_agents()}, log


def ctx(intent, entities, message="", results=None, **kw):
    return AgentContext(intent=intent, entities=entities, message=message, tz=TZ, now=NOW,
                        tool_results=results or {}, user_name="Asha", **kw)


def email(id, sender="Prof <prof@uni.edu>", subject="Exam", date="2026-11-23", body="The exam moved to Friday."):
    return asdict(EmailMessageData(id=id, thread_id="t" + id, sender=sender, subject=subject, internal_date=date,
                                   body_text=body))


def event(id, summary, start, end, attendees=()):
    return {"id": id, "summary": summary, "start": start, "end": end, "status": "confirmed", "all_day": False,
            "attendees": [{"email": a, "name": a.split("@")[0].title(), "response_status": ""} for a in attendees]}


def test_registry_has_one_agent_per_operation():
    reg, _ = agents()
    assert set(reg) == {"send_email", "search_emails", "read_email", "reply_email", "summarize_emails", "create_draft",
                        "create_event", "get_events", "reschedule_event", "cancel_event", "check_availability"}
    assert all(a.service == ("gmail" if i in {"send_email", "search_emails", "read_email", "reply_email",
                                              "summarize_emails", "create_draft"} else "calendar") for i, a in reg.items())


# ---- Gmail ----
def test_search_agent_calls_mcp_then_lists_and_remembers_results():
    a = agents()[0]["search_emails"]
    step = a.step(ctx("search_emails", {"person": "professor"}))
    assert step.kind == "tool_calls" and step.tool_calls[0].tool == "search_emails"
    assert step.tool_calls[0].arguments == {"query": "from:professor", "max_results": 10}
    done = a.step(ctx("search_emails", {"person": "professor"}, results={"search": {"messages": [email("m1"), email("m2")]}}))
    assert done.kind == "result" and "2 matching" in done.message and [e["id"] for e in done.memory["emails"]] == ["m1", "m2"]


def test_summarization_agent_reads_emails_then_uses_summarization_role():
    reg, log = agents()
    a = reg["summarize_emails"]
    assert a.step(ctx("summarize_emails", {"message_id": "m2"})).tool_calls[0].arguments == {"message_id": "m2"}
    done = a.step(ctx("summarize_emails", {"message_id": "m2"}, results={"read:0": {"message": email("m2")}}))
    assert done.kind == "result" and "The exam moved to Friday." in done.message and "- Revise" in done.message
    assert log == [{"role": "summarization", "provider": "openrouter", "model": "deepseek/deepseek-r1"}]
    # no reference -> recent inbox mail, every match read before summarizing
    step = a.step(ctx("summarize_emails", {}))
    assert step.tool_calls[0].arguments["query"] == "in:inbox newer_than:7d"
    reads = a.step(ctx("summarize_emails", {}, results={"search": {"messages": [email("a"), email("b")]}}))
    assert [c.key for c in reads.tool_calls] == ["read:0", "read:1"]


def test_send_agent_proposes_with_task_composition_and_fixed_recipient():
    reg, log = agents()
    out = reg["send_email"].step(ctx("send_email", {"recipient_email": "professor@example.com",
                                                    "body": "I'll miss tomorrow's lecture"}))
    assert out.kind == "proposal" and out.proposal.request == {"to": ["professor@example.com"], **TASK}
    assert log[-1]["role"] == "task_automation" and out.data["composition_source"] == "task_model"


def test_task_model_cannot_change_recipient_and_bad_output_falls_back_to_user_words():
    reg, _ = agents(task={"to": "evil@x.com", "subject": "Hi", "body": "ok"})
    out = reg["create_draft"].step(ctx("create_draft", {"recipient_email": "a@example.com", "body": "ok"}))
    assert out.proposal.request["to"] == ["a@example.com"]
    reg, _ = agents(task="not json at all")
    out = reg["send_email"].step(ctx("send_email", {"recipient_email": "a@example.com", "body": "running late"}))
    assert out.proposal.request["body"] == "running late" and out.data["composition_source"] == "user_wording"


def test_reply_agent_finds_latest_match_reads_it_then_proposes():
    a = agents()[0]["reply_email"]
    e = {"person": "professor", "body": "Thanks, noted."}
    assert a.step(ctx("reply_email", e)).tool_calls[0].tool == "search_emails"
    found = {"search": {"messages": [email("old", date="2026-11-01"), email("new", date="2026-11-23")]}}
    assert a.step(ctx("reply_email", e, results=found)).tool_calls[0].arguments == {"message_id": "new"}
    out = a.step(ctx("reply_email", e, results={**found, "read": {"message": email("new")}}))
    assert out.kind == "proposal" and out.proposal.request["message_id"] == "new"


def test_read_agent_reports_no_match_without_inventing_one():
    a = agents()[0]["read_email"]
    out = a.step(ctx("read_email", {"query": "invoice"}, results={"search": {"messages": []}}))
    assert out.kind == "result" and "couldn't find" in out.message


# ---- Calendar ----
RAHUL = ("rahul@x.com",)
EVENTS = [event("e1", "Project review", "2026-11-26T10:00:00+05:30", "2026-11-26T11:00:00+05:30", RAHUL),
          event("e2", "Weekly sync", "2026-11-25T16:00:00+05:30", "2026-11-25T16:30:00+05:30", RAHUL)]


def test_reschedule_ambiguous_then_asks_time_then_proposes_keeping_duration():
    a = build_calendar_agents()["reschedule_event"]
    e = {"person": "Rahul", "date": "2026-11-27"}
    assert a.step(ctx("reschedule_event", e)).tool_calls[0].arguments == {"days": 30, "max_results": 250}
    amb = a.step(ctx("reschedule_event", e, results={"candidates": {"events": EVENTS}}))
    assert amb.kind == "clarify" and "2 meetings" in amb.message and [c["id"] for c in amb.pending["candidates"]] == ["e2", "e1"]
    picked = {**e, "event_id": "e1"}
    ask = a.step(ctx("reschedule_event", picked, results={"event": {"event": EVENTS[0]}}))
    assert ask.kind == "clarify" and ask.message.startswith("What time") and ask.pending["entities"]["event_id"] == "e1"
    out = a.step(ctx("reschedule_event", {**picked, "time": "15:00"}, results={"event": {"event": EVENTS[0]}}))
    assert out.kind == "proposal"
    assert out.proposal.request["new_start"] == "2026-11-27T15:00:00+05:30" and out.proposal.request["event_id"] == "e1"


def test_reschedule_into_the_past_is_questioned():
    a = build_calendar_agents()["reschedule_event"]
    out = a.step(ctx("reschedule_event", {"event_id": "e1", "date": "2026-11-24", "time": "09:00"},
                     results={"event": {"event": EVENTS[0]}}))
    assert out.kind == "clarify" and "already passed" in out.message


def test_cancel_uses_date_to_identify_event_and_unknown_event_is_not_guessed():
    a = build_calendar_agents()["cancel_event"]
    e = {"person": "Rahul", "date": "2026-11-25"}
    assert a.step(ctx("cancel_event", e)).tool_calls[0].arguments == {"on_date": "2026-11-25"}
    out = a.step(ctx("cancel_event", e, results={"candidates": {"events": EVENTS}}))
    assert out.kind == "proposal" and out.proposal.request["event_id"] == "e2"
    none = a.step(ctx("cancel_event", {"person": "Nobody"}, results={"candidates": {"events": EVENTS}}))
    assert none.kind == "clarify" and "couldn't find" in none.message


def test_create_event_checks_conflicts_before_proposing():
    a = build_calendar_agents()["create_event"]
    e = {"person": "Rahul Sharma", "date": "2026-11-25", "time": "15:00"}
    step = a.step(ctx("create_event", e))
    assert step.tool_calls[0].tool == "check_availability"
    assert step.tool_calls[0].arguments == {"start": "2026-11-25T15:00:00+05:30", "end": "2026-11-25T16:00:00+05:30"}
    out = a.step(ctx("create_event", e, results={"availability": {"is_free": False}}))
    assert out.kind == "proposal" and out.proposal.request["title"] == "Meeting with Rahul Sharma"
    assert "already have something" in out.proposal.preview and "won't be invited" in out.proposal.preview


def test_availability_and_get_events_report_mcp_results():
    reg = build_calendar_agents()
    avail = reg["check_availability"]
    step = avail.step(ctx("check_availability", {"date": "2026-11-25"}))
    assert step.tool_calls[0].arguments["start"] == "2026-11-25T09:00:00+05:30"
    busy = {"is_free": False, "busy": [["2026-11-25T10:30:00+05:30", "2026-11-25T11:00:00+05:30"]],
            "free_slots": [["2026-11-25T09:00:00+05:30", "2026-11-25T10:30:00+05:30"]]}
    assert "10:30–11:00" in avail.step(ctx("check_availability", {"date": "2026-11-25"}, results={"availability": busy})).message
    get = reg["get_events"]
    out = get.step(ctx("get_events", {"date": "2026-11-26"}, results={"events": {"events": EVENTS}}))
    assert out.kind == "result" and "Project review" in out.message and "Weekly sync" not in out.message
    assert out.memory["last_event_id"] == "e1"
