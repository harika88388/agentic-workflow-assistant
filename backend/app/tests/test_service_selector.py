import pytest

from app.agents.service_selector import ServiceSelection, ServiceSelectorAgent
from app.schemas.intent import IntentResult

sel = ServiceSelectorAgent()


@pytest.mark.parametrize("message,service", [
    ("Send an email to professor@example.com", "gmail"),
    ("Find my upcoming meetings", "calendar"),
    ("Summarize my recent emails", "gmail"),
    ("Reschedule my meeting with Rahul", "calendar"),
    ("Am I free tomorrow afternoon?", "calendar"),
    ("Reply to the latest message from HR", "gmail"),
    ("Tell him I'll miss tomorrow's lecture", "gmail"),
    ("Cancel the standup", "calendar"),
])
def test_spec_examples_select_the_right_service(message, service):
    assert sel.select(message).service == service


def test_meeting_invite_with_email_address_stays_calendar():
    assert sel.select("Schedule a meeting with rahul@example.com tomorrow at 3").service == "calendar"


def test_fragment_without_signals_falls_back_to_pending_request():
    s = sel.select("Tomorrow at 3 PM", pending_service="calendar")
    assert s.service == "calendar" and s.source == "pending_context"
    assert sel.select("Rahul Sharma").service == "unknown"


def test_recognised_intent_decides_final_route_and_disagreement_is_explained():
    pre = sel.select("Send the agenda for the meeting")  # mixed vocabulary
    ir = IntentResult.model_validate({"intent": "send_email", "entities": {}})
    final = sel.reconcile(pre, ir)
    assert final.service == "gmail" and final.source == "intent"
    wrong = ServiceSelection("calendar", 0.7, "signals", "x")
    assert "overrides calendar" in sel.reconcile(wrong, ir).reason


def test_unknown_intent_uses_continued_pending_service_or_signals():
    unknown = IntentResult.model_validate({"intent": "unknown"})
    assert sel.reconcile(ServiceSelection("unknown", 0, "none", ""), unknown, continued_service="calendar").service == "calendar"
    assert sel.reconcile(ServiceSelection("gmail", 0.8, "signals", ""), unknown).service == "gmail"
