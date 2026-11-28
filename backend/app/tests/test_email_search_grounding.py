"""Gmail questions are answered from complete email bodies, never from subjects or snippets.

Mocked Gmail mailbox, real MIME parsing (`parse_message` on Gmail API payloads), real GmailService and the real
SearchEmailAgent; only the LLM is scripted. Grounding is checked against a model that sometimes misbehaves.
"""
import base64
from dataclasses import asdict
from datetime import datetime, timezone

import pytest

from app.agents.base import AgentContext
from app.agents.email_understanding_agent import UNCONFIRMED_ANSWER, EmailAnswer, ground_answer
from app.agents.gmail_agents import CALENDAR_OFFER, broader_queries, build_gmail_agents, gmail_query
from app.agents.summarization_agent import SummarizationAgent
from app.agents.task_agent import TaskAutomationAgent
from app.integrations.gmail import mime
from app.integrations.gmail.client import GmailClient
from app.tests.fakes import NOW, TZ, FakeGmailClient, ScriptedLLM, gmail_service, router
from app.tests.test_chat_workflow import env  # noqa: F401  (end-to-end fixture)


def _b64(s: str) -> str:
    return base64.urlsafe_b64encode(s.encode()).decode().rstrip("=")


def _ms(iso: str) -> str:
    return str(int(datetime.fromisoformat(iso).astimezone(timezone.utc).timestamp() * 1000))


def gmail_payload(id, sender, subject, payload, sent="2026-11-24T06:00:00+00:00", snippet=""):
    """A Gmail API `messages.get(format=full)` response."""
    headers = [{"name": "From", "value": sender}, {"name": "To", "value": "Asha <me@example.com>"},
               {"name": "Subject", "value": subject}, {"name": "Date", "value": "Tue, 24 Nov 2026 11:30:00 +0530"}]
    return {"id": id, "threadId": f"t-{id}", "snippet": snippet, "labelIds": ["INBOX"], "internalDate": _ms(sent),
            "payload": {**payload, "headers": [*headers, *payload.get("headers", [])]}}


def mailbox(*raws) -> FakeGmailClient:
    gmail = FakeGmailClient()
    for raw in raws:
        gmail.messages[raw["id"]] = mime.parse_message(raw)
    return gmail


def plain(text):
    return {"mimeType": "text/plain", "body": {"data": _b64(text)}}


def finding(email, relevant=True, evidence=None, reason="", **meeting):
    out = {"email": email, "relevant": relevant, "reason": reason, "evidence": evidence}
    if meeting:
        out["meeting"] = {"mentioned": True, **meeting}
    return out


def ask(gmail, question, entities, answer):
    """Drive the agent through its MCP tool calls against the mocked mailbox. Returns (outcome, llm, tool calls)."""
    llm = ScriptedLLM(email_answer=answer)
    r, log = router(llm), []
    agent = build_gmail_agents(SummarizationAgent(r, log), TaskAutomationAgent(r, log))["search_emails"]
    svc, results, calls = gmail_service(gmail), {}, []
    for _ in range(8):
        out = agent.step(AgentContext(intent="search_emails", entities=entities, message=question, tz=TZ, now=NOW,
                                      tool_results=dict(results), user_name="Asha"))
        if out.kind != "tool_calls":
            return out, llm, calls
        for c in out.tool_calls:
            calls.append((c.tool, c.arguments))
            if c.tool == "search_emails":
                results[c.key] = {"messages": [asdict(m) for m in svc.search_emails(**c.arguments)]}
            else:
                results[c.key] = {"message": asdict(svc.read_email(**c.arguments))}
    raise AssertionError("agent did not finish")


def qa_prompt(llm) -> str:
    return next(c["messages"][1].content for c in llm.calls if c["role"] == "email_answer")


# ---------- query building ----------
def test_sender_and_keywords_are_combined_and_broadened_only_when_nothing_matches():
    ctx = lambda **e: AgentContext(intent="search_emails", entities=e, message="", tz=TZ, now=NOW)  # noqa: E731
    assert gmail_query(ctx(person="aa3corner", query="meeting")) == "from:aa3corner meeting"
    assert gmail_query(ctx(person="Rahul Sharma", query="invoice")) == 'from:"Rahul Sharma" invoice'
    assert gmail_query(ctx(person="professor", query="from:professor")) == "from:professor"
    assert gmail_query(ctx(person="aa3corner", query="aa3corner meeting")) == "aa3corner meeting"
    assert broader_queries("from:aa3corner meeting tonight") == ["from:aa3corner {meeting tonight}", "from:aa3corner"]
    assert broader_queries("from:aa3corner") == [] and broader_queries("invoice") == []

    gmail = mailbox(gmail_payload("m1", "aa3corner <alerts@aa3corner.com>", "Hi", plain("We will meet tonight.")))
    out, _, calls = ask(gmail, "any meeting email from aa3corner?", {"query": "from:aa3corner meeting scheduled"},
                        {"answer": "Yes.", "findings": [finding(1, evidence="We will meet tonight.")]})
    assert [a["query"] for t, a in calls if t == "search_emails"] == [
        "from:aa3corner meeting scheduled", "from:aa3corner {meeting scheduled}", "from:aa3corner"]
    assert out.data["query"] == "from:aa3corner" and ("read_email", {"message_id": "m1"}) in calls


# ---------- 1. the subject does not describe the body ----------
def test_misleading_subject_is_not_treated_as_proof():
    gmail = mailbox(gmail_payload("m1", "Priya <priya@a.com>", "Meeting tonight at 7",
                                  plain("Ignore my previous note - nothing is planned. Sharing the trip photos instead.")))
    # a model that answers from the subject line
    out, llm, _ = ask(gmail, "Do I have a meeting tonight?", {"query": "meeting"}, {
        "answer": "Yes, you have a meeting tonight at 7 PM.",
        "findings": [finding(1, evidence="Meeting tonight at 7", status="confirmed", time_text="tonight at 7",
                             exact_time="19:00")]})
    assert "Subject: Meeting tonight at 7" in qa_prompt(llm) and "nothing is planned" in qa_prompt(llm)
    assert out.kind == "result" and out.message.startswith(UNCONFIRMED_ANSWER)
    assert "7 PM" not in out.message and "19:00" not in out.message and CALENDAR_OFFER not in out.message
    card = out.data["messages"][0]
    assert card["relevant"] is False and card["meeting"] is None and card["evidence"] is None


# ---------- 2. the information exists only in the body ----------
def test_information_only_in_the_body_is_read_in_full():
    body = ("Hi Asha,\n\nThanks for signing up. A few logistics for next week are below.\n\n"
            "The workshop will be held in Hall B on Thursday 26 November at 10:00 AM.\n\nSee you there,\nMeera")
    gmail = mailbox(gmail_payload("m1", "Meera <meera@uni.edu>", "Hello!", plain(body), snippet=body[:40]))
    out, llm, calls = ask(gmail, "Where is the workshop?", {"person": "Meera"}, {
        "answer": "The workshop is in Hall B on Thursday 26 November at 10:00 AM.",
        "findings": [finding(1, evidence="The workshop will be held in Hall B on Thursday 26 November at 10:00 AM.",
                             status="confirmed", title="Workshop", date_text="Thursday 26 November",
                             time_text="10:00 AM", exact_time="10:00", location="Hall B")]})
    assert gmail.searches[0] == "from:Meera" and calls[-1] == ("read_email", {"message_id": "m1"})
    prompt = qa_prompt(llm)
    assert "held in Hall B" in prompt and "From name: Meera" in prompt and "From address: meera@uni.edu" in prompt
    assert "Message ID: m1" in prompt and "Thread ID: t-m1" in prompt and "To: Asha <me@example.com>" in prompt
    assert "Subject: Hello!" in out.message and "Email content: Hi Asha," in out.message
    assert "Location: Hall B" in out.message and out.data["calendar_offer"] == {
        "title": "Workshop", "date": "2026-11-26", "time": "10:00", "location": "Hall B",
        "description": 'From the email "Hello!" sent by Meera <meera@uni.edu>.'}


# ---------- 3. multipart/alternative: plain text preferred over HTML ----------
def test_multipart_plain_and_html_sends_plain_text_to_the_model():
    payload = {"mimeType": "multipart/alternative", "parts": [
        plain("Dinner is booked for Friday at 8 PM at Olive."),
        {"mimeType": "text/html", "body": {"data": _b64("<p>Dinner is booked for <b>Friday at 8 PM</b> at Olive.</p>")}}]}
    gmail = mailbox(gmail_payload("m1", "Ravi <ravi@x.com>", "Plans", payload))
    out, llm, _ = ask(gmail, "When is dinner?", {"query": "dinner"}, {
        "answer": "Dinner is on Friday at 8 PM at Olive.",
        "findings": [finding(1, evidence="Dinner is booked for Friday at 8 PM at Olive.")]})
    prompt = qa_prompt(llm)
    assert "Dinner is booked for Friday at 8 PM at Olive." in prompt and "<p>" not in prompt and "<b>" not in prompt
    assert out.message.startswith("Dinner is on Friday at 8 PM at Olive.")


def test_html_only_email_is_converted_to_text_with_links_kept():
    html = ("<html><head><title>x</title><style>p{color:red}</style></head><body>"
            "<p>Project sync tomorrow at 4:30 PM.</p><p>Join <a href='https://meet.google.com/abc-defg-hij'>here</a></p>"
            "<table><tr><td>Room</td><td>4B</td></tr></table></body></html>")
    text = mime.parse_message({"id": "1", "payload": {"mimeType": "text/html", "body": {"data": _b64(html)}}}).body_text
    assert text.splitlines()[0] == "Project sync tomorrow at 4:30 PM."
    assert "Join here (https://meet.google.com/abc-defg-hij)" in text and "Room 4B" in text and "color" not in text


# ---------- 4. nested MIME with attachments ----------
def test_nested_mime_reads_the_body_and_never_treats_attachments_as_body():
    payload = {"mimeType": "multipart/mixed", "parts": [
        {"mimeType": "multipart/related", "parts": [
            {"mimeType": "multipart/alternative", "parts": [
                plain("Agenda attached. The review is confirmed for Wednesday 25 November at 2 PM via "
                      "https://zoom.us/j/123456789"),
                {"mimeType": "text/html", "body": {"data": _b64("<p>Agenda attached.</p>")}}]},
            {"mimeType": "image/png", "filename": "logo.png", "body": {"attachmentId": "img", "size": 10}}]},
        {"mimeType": "text/plain", "filename": "notes.txt",
         "headers": [{"name": "Content-Disposition", "value": "attachment; filename=notes.txt"}],
         "body": {"data": _b64("ATTACHMENT TEXT: the review is cancelled")}},
        {"mimeType": "application/pdf", "filename": "agenda.pdf", "body": {"attachmentId": "pdf", "size": 2048}}]}
    m = mime.parse_message(gmail_payload("m1", "Lead <lead@x.com>", "Review", payload))
    assert m.body_text.startswith("Agenda attached. The review is confirmed") and "ATTACHMENT TEXT" not in m.body_text
    assert [a["filename"] for a in m.attachments] == ["logo.png", "notes.txt", "agenda.pdf"]

    gmail = mailbox(gmail_payload("m1", "Lead <lead@x.com>", "Review", payload))
    out, llm, _ = ask(gmail, "Is the review confirmed?", {"query": "review"}, {
        "answer": "Yes, the review is confirmed for Wednesday 25 November at 2 PM.",
        "findings": [finding(1, evidence="The review is confirmed for Wednesday 25 November at 2 PM",
                             status="confirmed", title="Review", date_text="Wednesday 25 November", time_text="2 PM",
                             exact_time="14:00")]})
    prompt = qa_prompt(llm)
    assert "ATTACHMENT TEXT" not in prompt and "Attachments (content not included): logo.png, notes.txt, agenda.pdf" in prompt
    meeting = out.data["messages"][0]["meeting"]
    assert meeting["meeting_link"] == "https://zoom.us/j/123456789" and meeting["exact_time"] == "14:00"
    assert "Meeting link: https://zoom.us/j/123456789" in out.message


def test_large_bodies_stored_as_attachment_parts_are_fetched():
    class FakeHTTP:
        def __init__(self):
            self.paths = []

        def request(self, method, path, params=None):
            self.paths.append(path)
            if path.endswith("/attachments/big"):
                return {"data": _b64("The full body that Gmail stored separately.")}
            return gmail_payload("m1", "A <a@x.com>", "Big", {"mimeType": "multipart/alternative", "parts": [
                {"mimeType": "text/plain", "body": {"attachmentId": "big", "size": 900000}}]})

    client = GmailClient(lambda: "token")
    client.http = FakeHTTP()
    assert client.get_message("m1").body_text == "The full body that Gmail stored separately."
    assert client.http.paths == ["/messages/m1", "/messages/m1/attachments/big"]


# ---------- 5. empty body ----------
def test_empty_body_is_reported_not_answered_from_the_snippet():
    gmail = mailbox(gmail_payload("m1", "Bot <bot@x.com>", "Meeting at 5", {"mimeType": "text/plain", "body": {}},
                                  snippet="Meeting at 5 in room 9"))
    out, llm, _ = ask(gmail, "Is there a meeting at 5?", {"query": "meeting"}, {
        "answer": "Yes, at 5 in room 9.", "findings": [finding(1, evidence="Meeting at 5 in room 9", exact_time="17:00")]})
    prompt = qa_prompt(llm)
    assert "[No readable body text.]" in prompt and "room 9" not in prompt
    assert out.message.startswith(UNCONFIRMED_ANSWER) and "had no readable body text" in out.message
    assert out.data["messages"][0]["has_body"] is False and out.data["messages"][0]["relevant"] is False


# ---------- 6. several matching emails, details never mixed ----------
def test_multiple_matching_senders_keep_details_per_email():
    gmail = mailbox(
        gmail_payload("m1", "Priya <priya@a.com>", "Sync", plain("Can we meet Thursday at 3 PM in Room 4?"),
                      sent="2026-11-24T05:00:00+00:00"),
        gmail_payload("m2", "Priya Nair <pnair@b.com>", "Lunch", plain("Lunch on Friday? The cafe on 5th street works."),
                      sent="2026-11-24T06:00:00+00:00"))
    out, llm, calls = ask(gmail, "What did Priya propose?", {"person": "Priya"}, {
        "answer": "Priya (priya@a.com) proposed meeting Thursday at 3 PM in Room 4; Priya Nair proposed lunch on Friday.",
        "findings": [
            finding(1, evidence="Lunch on Friday?", status="proposed", title="Lunch", date_text="Friday",
                    location="Room 4", exact_time="15:00"),  # details copied from the other email
            finding(2, evidence="Can we meet Thursday at 3 PM in Room 4?", status="proposed", date_text="Thursday",
                    time_text="3 PM", exact_time="15:00", location="Room 4",
                    attendees=["pnair@b.com", "asha@me.com"])]})
    assert [a for t, a in calls if t == "read_email"] == [{"message_id": "m2"}, {"message_id": "m1"}]
    prompt = qa_prompt(llm)
    assert prompt.index("=== Email 1 ===") < prompt.index("Lunch on Friday") < prompt.index("=== Email 2 ===")
    lunch, sync = out.data["messages"]
    assert lunch["id"] == "m2" and lunch["meeting"]["location"] is None and lunch["meeting"]["exact_time"] is None
    assert sync["id"] == "m1" and sync["meeting"]["location"] == "Room 4" and sync["meeting"]["exact_time"] == "15:00"
    assert sync["meeting"]["attendees"] == [] and sync["meeting"]["status"] == "proposed"
    assert out.message.count("From: ") == 2 and "The email proposes a meeting" in out.message
    assert out.kind == "result" and "calendar_offer" not in out.data  # two meetings: no guess which one to add


def test_findings_with_bad_indexes_or_duplicates_are_ignored():
    emails = [{"id": "a", "body_text": "Standup moved to 9:30."}, {"id": "b", "body_text": "Nothing here."}]
    grounded = ground_answer(EmailAnswer.model_validate({"answer": "x", "findings": [
        {"email": 7, "relevant": True, "evidence": "Standup"}, {"email": 1, "relevant": True, "evidence": "Standup moved"},
        {"email": 1, "relevant": False}]}), emails)
    assert [(f.email, f.relevant) for f in grounded.findings] == [(1, True), (2, False)]


# ---------- 7. "meeting tonight" with no exact time, then the calendar handoff ----------
TONIGHT = "This is to inform you that a meeting has been scheduled tonight to discuss the launch."
TONIGHT_ANSWER = {
    "answer": "Yes. aa3corner emailed you that a meeting has been scheduled for tonight to discuss the launch.",
    "findings": [finding(1, evidence="a meeting has been scheduled tonight", reason="The body announces the meeting.",
                         status="confirmed", title="Launch discussion", date_text="tonight", time_text="tonight",
                         exact_time="19:00", organizer="aa3corner")],
    "missing_information": ["the exact meeting time", "the meeting location"]}


def test_meeting_tonight_without_time_never_gets_an_invented_time():
    gmail = mailbox(gmail_payload("m1", "aa3corner <alerts@aa3corner.com>", "Re: weekend", plain(TONIGHT)))
    out, _, _ = ask(gmail, "Did I receive any emails from aa3corner about a meeting scheduled tonight?",
                    {"person": "aa3corner", "query": "from:aa3corner meeting"}, TONIGHT_ANSWER)
    assert out.kind == "clarify" and out.message.endswith(CALENDAR_OFFER)
    assert "From: aa3corner <alerts@aa3corner.com>" in out.message and "Subject: Re: weekend" in out.message
    assert f"Email content: {TONIGHT}" in out.message
    assert "scheduled (Launch discussion) for tonight, but it does not specify an exact time." in out.message
    assert "19:00" not in out.message and "Not stated in the email: the exact meeting time" in out.message
    meeting = out.data["messages"][0]["meeting"]
    assert meeting["exact_time"] is None and meeting["organizer_email"] == "alerts@aa3corner.com"
    assert out.pending == {"intent": "create_event", "service": "calendar", "awaiting": ["confirmation"], "entities": {
        "title": "Launch discussion", "date": "2026-11-24",
        "description": 'From the email "Re: weekend" sent by aa3corner <alerts@aa3corner.com>.'}}


def test_meeting_tonight_end_to_end_asks_for_time_checks_calendar_and_waits_for_approval(env):  # noqa: F811
    env.gmail.messages["m1"] = mime.parse_message(
        gmail_payload("m1", "aa3corner <alerts@aa3corner.com>", "Re: weekend", plain(TONIGHT)))
    env.llm.email_answer = TONIGHT_ANSWER
    env.llm.intents = [  # the first message is classified deterministically, without the intent model
        '{"intent":"unknown","is_follow_up":true}',
        '{"intent":"unknown","entities":{"time":"7:30 PM"},"is_follow_up":true}',
    ]
    t1 = env.say("Did I receive any emails from aa3corner about a meeting scheduled tonight?")
    assert t1["status"] == "needs_clarification" and CALENDAR_OFFER in t1["assistant_message"]["content"]
    meta = t1["assistant_message"]["meta"]
    assert meta["entities"] == {"query": "from:aa3corner meeting"} and env.gmail.searches == ["from:aa3corner meeting"]
    assert [c["tool"] for c in meta["mcp_calls"]] == ["search_emails", "read_email"]
    assert [c["role"] for c in meta["llm_calls"]] == ["email_understanding"]
    assert meta["data"]["calendar_offer"]["date"] == "2026-11-24" and env.cal.events == {}

    t2 = env.say("Yes")
    assert t2["status"] == "needs_clarification" and t2["assistant_message"]["content"] == "What time?"
    assert t2["assistant_message"]["meta"]["intent"] == "create_event" and env.cal.events == {}

    t3 = env.say("7:30 PM")
    assert t3["status"] == "awaiting_approval" and env.cal.events == {}
    assert [c["tool"] for c in t3["assistant_message"]["meta"]["mcp_calls"]] == ["check_availability", "list_events"]
    req = t3["workflow"]["request"]
    assert req["title"] == "Launch discussion" and req["start"] == "2026-11-24T19:30:00+05:30"
    assert "Re: weekend" in req["description"]

    done = env.decide(t3["workflow"]["id"])
    assert done.status_code == 200 and done.json()["status"] == "succeeded"
    [created] = env.cal.events.values()
    assert created["summary"] == "Launch discussion" and created["start"]["dateTime"] == "2026-11-24T19:30:00+05:30"


# ---------- 8. regression: the user's exact report (subject unrelated to the body) ----------
REG_SENDER = "aa3corner@gmail.com"
REG_SUBJECT = "You are gay"
REG_BODY = "This is to inform you that a meeting has been scheduled tonight to discuss if u are gay."
REG_QUESTION = "Did I get any mails from aa3corner for a meeting scheduled tonight?"
# What a model might reply; it invents an exact time, which grounding must remove.
REG_ANSWER = {
    "answer": "Yes. aa3corner emailed you that a meeting has been scheduled tonight.",
    "findings": [finding(1, evidence="a meeting has been scheduled tonight", reason="The body announces a meeting.",
                         status="confirmed", title="Discussion", date_text="tonight", time_text="tonight",
                         exact_time="21:00", organizer="aa3corner@gmail.com")],
    "missing_information": ["the exact meeting time"]}


def regression_payload():
    """Gmail API format=full: multipart/alternative, base64url text/plain + text/html parts, subject unrelated."""
    return gmail_payload("r1", REG_SENDER, REG_SUBJECT, {"mimeType": "multipart/alternative", "parts": [
        {"mimeType": "text/plain", "headers": [{"name": "Content-Type", "value": 'text/plain; charset="UTF-8"'}],
         "body": {"data": _b64(REG_BODY), "size": len(REG_BODY)}},
        {"mimeType": "text/html", "headers": [{"name": "Content-Type", "value": 'text/html; charset="UTF-8"'}],
         "body": {"data": _b64(f"<div dir=\"ltr\">{REG_BODY}</div>")}}]}, snippet="This is to inform you that")


def test_regression_mime_decoding_separates_subject_from_body():
    m = mime.parse_message(regression_payload())
    assert m.subject == REG_SUBJECT and m.sender == REG_SENDER and m.body_text == REG_BODY
    assert "meeting" not in m.subject.casefold() and "meeting" in m.body_text


def test_regression_question_is_classified_as_a_gmail_search_by_sender_and_content():
    from app.agents.intent_agent import _direct_email_intent
    ir = _direct_email_intent(REG_QUESTION)
    assert ir is not None and ir.intent == "search_emails" and ir.entities.query == "from:aa3corner meeting"


@pytest.mark.parametrize("question,query", [
    ("Did aa3corner@gmail.com email me about a meeting?", "from:aa3corner@gmail.com meeting"),
    ("did aa3corner send me an email about the meeting tonight?", "from:aa3corner meeting"),
    ("Has Rahul Sharma sent me an email about the invoice?", 'from:"Rahul Sharma" invoice'),
    ("Did aa3corner email me?", "from:aa3corner"),
])
def test_a_sender_named_as_the_subject_is_kept_in_the_search(question, query):
    from app.agents.intent_agent import _direct_email_intent
    ir = _direct_email_intent(question)
    assert ir is not None and ir.intent == "search_emails" and ir.entities.query == query


@pytest.mark.parametrize("question", ["Did I email aa3corner about the meeting?", "Has my professor emailed me?",
                                      "Did anyone email me about the meeting?"])
def test_pronouns_and_determiners_are_not_taken_as_senders(question):
    from app.agents.intent_agent import _direct_email_intent
    ir = _direct_email_intent(question)
    assert ir is None or not (ir.entities.query or "").startswith("from:")


def test_composing_to_an_address_is_left_to_the_model():
    from app.agents.intent_agent import _direct_email_intent
    assert _direct_email_intent("Email aa3corner@gmail.com about the meeting tomorrow") is None


def test_regression_answer_comes_from_the_body_not_the_subject():
    gmail = mailbox(regression_payload())
    out, llm, calls = ask(gmail, REG_QUESTION, {"query": "from:aa3corner meeting"}, REG_ANSWER)
    # Found by sender + a word that appears only in the BODY (the subject has no "meeting"), then read in full.
    assert gmail.searches == ["from:aa3corner meeting"]
    assert calls == [("search_emails", {"query": "from:aa3corner meeting", "max_results": 10}),
                     ("read_email", {"message_id": "r1"})]
    prompt = qa_prompt(llm)
    assert f"Subject: {REG_SUBJECT}\n" in prompt and f"Body:\n{REG_BODY}\n" in prompt
    assert "From address: aa3corner@gmail.com" in prompt and "<div" not in prompt
    # The reply states what the body says, distinguishes subject and body, and invents no time.
    assert f"Subject: {REG_SUBJECT}" in out.message and f"Email content: {REG_BODY}" in out.message
    assert "scheduled (Discussion) for tonight, but it does not specify an exact time." in out.message
    assert "21:00" not in out.message and "9 PM" not in out.message
    assert "Not stated in the email: the exact meeting time" in out.message
    card = out.data["messages"][0]
    assert card["relevant"] is True and card["evidence"] == "a meeting has been scheduled tonight"
    assert card["meeting"]["time_text"] == "tonight" and card["meeting"]["exact_time"] is None
    # It offers the calendar, with the date resolved from the email's own date and the time left for the user.
    assert out.kind == "clarify" and out.message.endswith(CALENDAR_OFFER)
    assert out.pending["intent"] == "create_event" and out.pending["entities"]["date"] == "2026-11-24"
    assert "time" not in out.pending["entities"]


def test_regression_end_to_end_asks_for_time_then_creates_only_after_approval(env):  # noqa: F811
    env.gmail.messages["r1"] = mime.parse_message(regression_payload())
    env.llm.email_answer = REG_ANSWER
    env.llm.intents = ['{"intent":"unknown","is_follow_up":true}',
                       '{"intent":"unknown","entities":{"time":"8 PM"},"is_follow_up":true}']
    t1 = env.say(REG_QUESTION)
    assert t1["status"] == "needs_clarification" and REG_BODY in t1["assistant_message"]["content"]
    assert "does not specify an exact time" in t1["assistant_message"]["content"]
    assert [c["tool"] for c in t1["assistant_message"]["meta"]["mcp_calls"]] == ["search_emails", "read_email"]
    t2 = env.say("Yes, add it to my calendar")
    assert t2["assistant_message"]["content"] == "What time?" and env.cal.events == {}
    t3 = env.say("8 PM")
    assert t3["status"] == "awaiting_approval" and env.cal.events == {}
    assert [c["tool"] for c in t3["assistant_message"]["meta"]["mcp_calls"]] == ["check_availability", "list_events"]
    assert t3["workflow"]["request"]["start"] == "2026-11-24T20:00:00+05:30"
    assert env.decide(t3["workflow"]["id"]).json()["status"] == "succeeded"
    [created] = env.cal.events.values()
    assert created["summary"] == "Discussion" and REG_SUBJECT in created["description"]


def test_declining_the_calendar_offer_drops_it(env):  # noqa: F811
    env.gmail.messages["m1"] = mime.parse_message(
        gmail_payload("m1", "aa3corner <alerts@aa3corner.com>", "Re: weekend", plain(TONIGHT)))
    env.llm.email_answer = TONIGHT_ANSWER
    env.llm.intents = ['{"intent":"search_emails","entities":{"person":"aa3corner","query":"meeting"}}',
                       '{"intent":"unknown"}']
    env.say("Any meeting emails from aa3corner?")
    t2 = env.say("No")
    assert t2["status"] == "completed" and "dropped" in t2["assistant_message"]["content"] and env.cal.events == {}
