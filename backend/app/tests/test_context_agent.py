import uuid
from datetime import date

import pytest

from app.agents.context_agent import ContextEnhancementAgent, ConversationContext, select_candidate
from app.agents.dates import extract_time, resolve_date
from app.database.models import Conversation, Message, SessionContext
from app.schemas.intent import IntentResult

REF = date(2026, 11, 24)  # Tuesday
agent = ContextEnhancementAgent()


def ir(**d):
    return IntentResult.model_validate(d)


def ctx(memory=None, pending=None):
    return ConversationContext(uuid.uuid4(), [], memory or {}, pending, REF, "Asia/Kolkata")


# ---- dates in the user's reference frame ----
@pytest.mark.parametrize("text,expected", [
    ("today", date(2026, 11, 24)), ("tomorrow", date(2026, 11, 25)), ("day after tomorrow", date(2026, 11, 26)),
    ("Friday", date(2026, 11, 27)), ("next Monday", date(2026, 11, 30)), ("Tuesday", date(2026, 12, 1)),
    ("this Tuesday", date(2026, 11, 24)), ("28 Nov", date(2026, 11, 28)), ("January 5", date(2027, 1, 5)),
    ("2026-12-03", date(2026, 12, 3)), ("in 3 days", date(2026, 11, 27)), ("next week", date(2026, 11, 30)),
])
def test_resolve_date(text, expected):
    assert resolve_date(text, REF) == expected


def test_unresolvable_dates_and_times():
    assert resolve_date("sometime soon", REF) is None and resolve_date("31 February", REF) is None
    assert extract_time("move it to 3 PM please") == "15:00" and extract_time("no time here") is None


# ---- follow-ups ----
def test_spec_send_email_then_pronoun_follow_up_resolves_recipient():
    pending = {"intent": "send_email", "service": "gmail", "entities": {"recipient_email": "professor@example.com"},
               "awaiting": ["body"]}
    r = agent.enhance(ir(intent="send_email", entities={"body": "I'll miss tomorrow's lecture"}, is_follow_up=True),
                      "Tell him I'll miss tomorrow's lecture", ctx(pending=pending))
    assert r.continued_pending and r.intent == "send_email"
    assert r.entities == {"recipient_email": "professor@example.com", "body": "I'll miss tomorrow's lecture"}


def test_pronoun_resolves_from_memory_when_no_pending_request():
    r = agent.enhance(ir(intent="send_email", entities={"person": "him", "body": "running late"}),
                      "Email him that I'm running late", ctx(memory={"last_contact_email": "prof@example.com"}))
    assert r.entities["recipient_email"] == "prof@example.com" and "person" not in r.entities


def test_named_person_is_not_replaced_by_remembered_address():
    r = agent.enhance(ir(intent="send_email", entities={"person": "Alice", "body": "hi"}), "Email Alice hi",
                      ctx(memory={"last_contact_email": "prof@example.com"}))
    assert "recipient_email" not in r.entities


def test_fragment_answer_continues_pending_meeting_and_resolves_dates():
    pending = {"intent": "create_event", "service": "calendar", "entities": {"person": "Rahul Sharma"},
               "awaiting": ["date", "time"]}
    r = agent.enhance(ir(intent="unknown", entities={"date": "tomorrow", "time": "3 PM"}), "Tomorrow at 3 PM.",
                      ctx(pending=pending))
    assert r.intent == "create_event" and r.service == "calendar"
    assert r.entities == {"person": "Rahul Sharma", "date": "2026-11-25", "time": "15:00"}


def test_time_is_extracted_from_bare_answer_when_model_returns_nothing():
    pending = {"intent": "reschedule_event", "service": "calendar", "awaiting": ["time"],
               "entities": {"person": "Rahul", "date": "2026-11-27", "event_id": "e1"}}
    r = agent.enhance(ir(intent="unknown"), "3 PM.", ctx(pending=pending))
    assert r.entities["time"] == "15:00" and r.entities["event_id"] == "e1" and r.entities["date"] == "2026-11-27"


def test_choosing_one_of_the_listed_meetings_selects_its_id():
    pending = {"intent": "reschedule_event", "service": "calendar", "awaiting": ["event_selection"],
               "entities": {"person": "Rahul", "date": "2026-11-27"}, "candidate_type": "event",
               "candidates": [{"id": "e1", "summary": "Project review", "start": "x"},
                              {"id": "e2", "summary": "Weekly sync", "start": "y"}]}
    # even if the model mislabels the fragment, picking an option continues the pending request
    r = agent.enhance(ir(intent="get_events", entities={"title": "project review"}), "The project review.", ctx(pending=pending))
    assert r.intent == "reschedule_event" and r.entities["event_id"] == "e1"
    assert agent.enhance(ir(intent="unknown"), "the second one", ctx(pending=pending)).entities["event_id"] == "e2"


def test_select_candidate_refuses_to_guess():
    c = [{"id": "1", "summary": "Project review"}, {"id": "2", "summary": "Project kickoff"}]
    assert select_candidate("the project one", c) is None
    assert select_candidate("2", c)["id"] == "2"


def test_latest_email_reference_uses_emails_shown_earlier():
    emails = [{"id": "m1", "sender": "Prof <prof@uni.edu>", "subject": "Exam", "internal_date": "2026-11-20"},
              {"id": "m2", "sender": "Prof <prof@uni.edu>", "subject": "Grades", "internal_date": "2026-11-23"}]
    r = agent.enhance(ir(intent="summarize_emails", entities={"query": "latest"}, is_follow_up=True),
                      "Summarize the latest one", ctx(memory={"emails": emails}))
    assert r.entities == {"message_id": "m2"}
    assert agent.enhance(ir(intent="read_email"), "open the first one", ctx(memory={"emails": emails})).entities["message_id"] == "m1"


def test_time_ranges_are_not_mistaken_for_references():
    emails = [{"id": "m1", "sender": "a@x.com", "subject": "s", "internal_date": "1"}]
    r = agent.enhance(ir(intent="summarize_emails", entities={"query": "newer_than:7d"}), "Summarize emails from last week",
                      ctx(memory={"emails": emails}))
    assert "message_id" not in r.entities


def test_reference_scoped_to_named_sender():
    emails = [{"id": "m1", "sender": "Bob <bob@x.com>", "subject": "s", "internal_date": "2"}]
    r = agent.enhance(ir(intent="read_email", entities={"person": "Alice"}), "read the latest from Alice",
                      ctx(memory={"emails": emails}))
    assert "message_id" not in r.entities


@pytest.mark.parametrize("message", ["Who sent it?", "What time is the meeting?"])
def test_email_detail_followups_reuse_the_last_email_instead_of_calendar(message):
    context = ctx(memory={"last_email_id": "mail-1"}, )
    context.history = [{"role": "assistant", "content": "Email from aacorner\nSubject: You are gay\n"
                                                      "Summary: A meeting is scheduled tonight."}]
    r = agent.enhance(ir(intent="get_events", service="calendar"), message, context)
    assert r.intent == "read_email" and r.service == "gmail"
    assert r.entities == {"message_id": "mail-1"}
    assert "last email discussed" in r.notes[0]


def test_new_unrelated_request_drops_pending_and_abandon_is_detected():
    pending = {"intent": "create_event", "service": "calendar", "entities": {"person": "Rahul"}, "awaiting": ["date"]}
    r = agent.enhance(ir(intent="search_emails", entities={"query": "invoice"}), "find invoice emails", ctx(pending=pending))
    assert not r.continued_pending and r.entities == {"query": "invoice"}
    assert agent.enhance(ir(intent="unknown"), "never mind", ctx(pending=pending)).abandon


def test_unresolvable_date_is_flagged_not_guessed():
    r = agent.enhance(ir(intent="check_availability", entities={"date": "sometime soon"}), "am I free sometime soon", ctx())
    assert r.date_unresolved and "date" not in r.entities


# ---- retrieval from the database is per conversation ----
def test_load_reads_only_this_conversation(make_user, db_session):
    user, _ = make_user()
    a, b = Conversation(user_id=user.id), Conversation(user_id=user.id)
    db_session.add_all([a, b])
    db_session.flush()
    db_session.add_all([Message(conversation_id=a.id, role="user", content="email prof@example.com"),
                        Message(conversation_id=b.id, role="user", content="unrelated secret"),
                        SessionContext(conversation_id=a.id, entities={"last_contact_email": "prof@example.com"}),
                        SessionContext(conversation_id=b.id, entities={"last_contact_email": "other@example.com"})])
    db_session.commit()
    c = agent.load(db_session, a, reference_date=REF, timezone="UTC")
    assert [h["content"] for h in c.history] == ["email prof@example.com"]
    assert c.memory == {"last_contact_email": "prof@example.com"}
    assert "unrelated" not in agent.render_for_intent(c) and "prof@example.com" in agent.render_for_intent(c)
