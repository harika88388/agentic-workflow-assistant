from app.agents.email_understanding_agent import EmailUnderstandingAgent
from app.tests.fakes import ScriptedLLM, router


EXAMPLE_BODY = "This is to inform you that a meeting has been scheduled tonight to discuss if u are gay."


def test_understanding_reads_body_instead_of_trusting_misleading_subject_or_snippet():
    response = {
        "subject": "You are gay",
        "main_topic": "A meeting has been scheduled to discuss whether the recipient is gay.",
        "meeting_mentioned": True,
        "meeting_details": {
            "scheduled": True, "date": "tomorrow", "time_description": "tonight", "exact_time": "6:30 PM",
        },
        "action_items": [],
        "summary": ("The sender informs the recipient that a meeting has been scheduled for tonight "
                    "to discuss whether the recipient is gay."),
    }
    llm = ScriptedLLM(email_understanding=response)
    agent = EmailUnderstandingAgent(router(llm))
    result = agent.analyze({
        "subject": "You are gay",
        "sender": "aacorner <sender@example.com>",
        "to": "recipient@example.com",
        "date": "Fri, 27 Nov 2026 10:00:00 +0000",
        "snippet": "You are gay",
        "body_text": EXAMPLE_BODY,
    })

    prompt = llm.calls[0]["messages"][1].content
    assert EXAMPLE_BODY in prompt and "Subject: You are gay" in prompt
    system = llm.calls[0]["messages"][0].content
    assert "never invent missing dates, times" in system.casefold() and "google calendar" in system.casefold()
    assert result.subject == "You are gay"
    assert result.meeting_mentioned and result.meeting_details.scheduled
    assert result.meeting_details.time_description == "tonight"
    assert result.meeting_details.exact_time is None and result.meeting_details.date is None


def test_understanding_extracts_explicit_meeting_date_and_time():
    llm = ScriptedLLM(email_understanding={
        "subject": "Review",
        "main_topic": "A review meeting is scheduled.",
        "meeting_mentioned": True,
        "meeting_details": {
            "scheduled": True, "date": "November 28, 2026",
            "time_description": "November 28 at 6:30 PM", "exact_time": "6:30 PM",
        },
        "summary": "The review meeting is scheduled for November 28 at 6:30 PM.",
    })
    result = EmailUnderstandingAgent(router(llm)).analyze({
        "subject": "Review", "body_text": "The review meeting is scheduled for November 28, 2026 at 6:30 PM.",
    })
    assert result.meeting_details.date == "November 28, 2026"
    assert result.meeting_details.exact_time == "6:30 PM"


def test_empty_body_does_not_fall_back_to_snippet_or_call_the_model():
    llm = ScriptedLLM()
    result = EmailUnderstandingAgent(router(llm)).analyze({
        "subject": "You are gay", "snippet": "A meeting is tonight.", "body_text": "",
    })
    assert result.summary == "No readable email body was available."
    assert result.meeting_mentioned is False and llm.calls == []
