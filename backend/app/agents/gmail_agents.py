"""Specialized Gmail agents. Reads run immediately through the Gmail MCP server; sends, replies and drafts are
returned as proposals that only run after the user approves them."""
import logging
import re
from datetime import date, datetime
from email.utils import parseaddr, parsedate_to_datetime
from typing import Any

from app.agents.base import (
    AgentContext, AgentOutcome, OperationAgent, Proposal, ToolCall, compact_email, email_address,
)
from app.agents.dates import resolve_date
from app.agents.email_understanding_agent import EmailAnswer, EmailQuestionAgent, EmailUnderstandingAgent, FoundMeeting
from app.agents.summarization_agent import SummarizationAgent
from app.agents.task_agent import TaskAutomationAgent
from app.services.llm.base import LLMConfigError, LLMProviderError, LLMResponseError

RECENT_QUERY = "in:inbox newer_than:7d"
SUMMARY_LIMIT = 5
SEARCH_LIMIT = 10
READ_LIMIT = 5
EXCERPT_CHARS = 1200
MAX_SHOWN = 3
CALENDAR_OFFER = "Would you like me to add this meeting to your Google Calendar?"
_SEARCH_TOKEN = re.compile(r'-?[A-Za-z_]+:"[^"]*"|-?[A-Za-z_]+:\S+|"[^"]*"|\S+')
_OPERATOR = re.compile(r"^-?[A-Za-z_]+:")
log = logging.getLogger(__name__)


def gmail_query(ctx: AgentContext) -> str | None:
    """Gmail search for the request. A sender and topic keywords are combined, never one dropped for the other."""
    query = str(ctx.get("query") or "").strip()
    who = str(ctx.get("recipient_email") or ctx.get("person") or "").strip()
    if who and "from:" not in query.casefold() and who.casefold() not in query.casefold():
        sender = f'from:"{who}"' if " " in who else f"from:{who}"
        query = f"{sender} {query}".strip()
    return query or None


def broader_queries(query: str) -> list[str]:
    """Fallback searches when `query` matches nothing: any one of the keywords, then the sender alone. The bodies
    of whatever they return are read before anything is claimed, so a broader search cannot produce a false hit."""
    tokens = _SEARCH_TOKEN.findall(query)
    operators = [t for t in tokens if _OPERATOR.match(t)]
    words = [t for t in tokens if not _OPERATOR.match(t) and t.upper() not in ("OR", "AND")]
    out = []
    if len(words) > 1 and not any(c in query for c in "{}()"):
        out.append(" ".join([*operators, "{" + " ".join(words) + "}"]))
    if words and any(o.lower().startswith(("from:", "to:")) for o in operators):
        out.append(" ".join(operators))
    return [q for q in dict.fromkeys(out) if q and q != query]


def latest(messages: list[dict[str, Any]]) -> dict[str, Any]:
    return max(messages, key=lambda m: m.get("internal_date") or "")


def _list_lines(messages: list[dict[str, Any]]) -> str:
    return "\n".join(f"{i}. \"{m.get('subject') or '(no subject)'}\" from {m.get('sender') or 'unknown'}"
                     f"{' (' + m['date'] + ')' if m.get('date') else ''}" for i, m in enumerate(messages, 1))


def _target_message(ctx: AgentContext, search_limit: int = 5) -> tuple[dict | None, AgentOutcome | None]:
    """Locate one email by id, or by searching and taking the most recent match; then read it in full."""
    mid = ctx.get("message_id")
    if not mid:
        q = gmail_query(ctx)
        if not q:
            return None, AgentOutcome("clarify", message="Which email do you mean? Tell me the sender, subject or some keywords.",
                                      pending={"awaiting": ["email_reference"]})
        if "search" not in ctx.tool_results:
            return None, AgentOutcome.calls(ToolCall("search", "gmail", "search_emails",
                                                     {"query": q, "max_results": search_limit}))
        found = ctx.tool_results["search"].get("messages") or []
        if not found:
            return None, AgentOutcome("result", message=f"I couldn't find any email matching {q}.",
                                      data={"query": q, "messages": []})
        mid = latest(found)["id"]
    if "read" not in ctx.tool_results:
        return None, AgentOutcome.calls(ToolCall("read", "gmail", "read_email", {"message_id": mid}))
    return ctx.tool_results["read"]["message"], None


def _focus_memory(m: dict[str, Any]) -> dict[str, Any]:
    mem: dict[str, Any] = {"last_email_id": m["id"], "last_thread_id": m.get("thread_id")}
    if addr := email_address(m.get("sender", "")):
        mem["last_contact_email"] = addr
    return mem


def _excerpt(body: str) -> str:
    body = re.sub(r"\n{3,}", "\n\n", body.strip())
    return body if len(body) <= EXCERPT_CHARS else body[:EXCERPT_CHARS].rstrip() + " …"


def _meeting_sentence(m: FoundMeeting) -> str:
    lead = {"confirmed": "The email says a meeting has been scheduled",
            "proposed": "The email proposes a meeting",
            "cancelled": "The email says the meeting has been cancelled"}.get(m.status, "The email mentions a meeting")
    if m.title:
        lead += f" ({m.title})"
    if m.time_text:
        lead += f" for {m.time_text}"
    if m.date_text and not _norm_in(m.date_text, m.time_text):
        lead += f" on {m.date_text}"
    if m.exact_time and not (m.time_text and m.exact_time in (m.time_text or "")):
        lead += f" at {m.exact_time}"
    if m.timezone:
        lead += f" ({m.timezone})"
    lead += "." if m.exact_time else ", but it does not specify an exact time."
    details = [lead]
    if m.location:
        details.append(f"Location: {m.location}")
    if m.meeting_link and m.meeting_link != m.location:
        details.append(f"Meeting link: {m.meeting_link}")
    if m.organizer:
        details.append(f"Organizer: {m.organizer}" + (f" <{m.organizer_email}>" if m.organizer_email
                                                       and m.organizer_email not in m.organizer else ""))
    if m.attendees:
        details.append("Attendees: " + ", ".join(m.attendees))
    return "\n".join(details)


def _norm_in(needle: str | None, haystack: str | None) -> bool:
    return bool(needle and haystack and needle.casefold() in haystack.casefold())


def _email_date(m: dict[str, Any], ctx: AgentContext) -> date | None:
    try:
        if m.get("internal_date"):
            return datetime.fromisoformat(str(m["internal_date"])).astimezone(ctx.tz).date()
        if m.get("date"):
            return parsedate_to_datetime(str(m["date"])).astimezone(ctx.tz).date()
    except (TypeError, ValueError):
        return None
    return None


def calendar_offer(m: dict[str, Any], meeting: FoundMeeting, ctx: AgentContext) -> dict[str, Any] | None:
    """create_event entities taken from the email (relative dates resolved against the email's own date), or None
    when the meeting is cancelled or already in the past. Missing values stay missing so the user is asked."""
    if not meeting.mentioned or meeting.status == "cancelled":
        return None
    sender_name, sender_addr = parseaddr(str(m.get("sender") or ""))
    who = meeting.organizer or sender_name or sender_addr
    entities: dict[str, Any] = {"title": meeting.title or (f"Meeting with {who}" if who else "Meeting")}
    ref = _email_date(m, ctx)
    when = meeting.date_text or meeting.time_text
    if ref and when and (d := resolve_date(when, ref)):
        if d < ctx.now.date():
            return None
        entities["date"] = d.isoformat()
    if meeting.exact_time:
        entities["time"] = meeting.exact_time
    if meeting.location or meeting.meeting_link:
        entities["location"] = meeting.location or meeting.meeting_link
    entities["description"] = (f"From the email \"{m.get('subject') or '(no subject)'}\" sent by "
                               f"{m.get('sender') or 'unknown sender'}."
                               + (f" Link: {meeting.meeting_link}" if meeting.meeting_link
                                  and meeting.meeting_link != entities.get("location") else ""))
    log.info("meeting extracted from email message_id=%s status=%s has_date=%s has_time=%s has_location=%s "
             "attendees=%d", m.get("id", "-"), meeting.status, "date" in entities, "time" in entities,
             "location" in entities, len(meeting.attendees))
    return entities


class SearchEmailAgent(OperationAgent):
    service, intent = "gmail", "search_emails"
    description = "Search Gmail, read the complete matching emails and answer the user's question from their bodies."

    def __init__(self, qa: EmailQuestionAgent):
        self.qa = qa

    def step(self, ctx: AgentContext) -> AgentOutcome:
        q = gmail_query(ctx)
        if not q:
            return AgentOutcome("clarify", message="What should I search for?", pending={"awaiting": ["query"]})
        r = ctx.tool_results
        queries = [q, *broader_queries(q)]
        found: list[dict[str, Any]] = []
        used = q
        for i, query in enumerate(queries):
            key = "search" if i == 0 else f"search:{i}"
            if key not in r:
                return AgentOutcome.calls(ToolCall(key, "gmail", "search_emails",
                                                   {"query": query, "max_results": SEARCH_LIMIT}))
            found = r[key].get("messages") or []
            if found:
                used = query
                break
        if not found:
            tried = f" I also tried {' and '.join(queries[1:])}." if len(queries) > 1 else ""
            return AgentOutcome("result", message=f"No emails matched {q}.{tried}",
                                data={"query": q, "queries": queries, "messages": []}, memory={"emails": []})

        listed = [compact_email(m) for m in found]
        top = found[:READ_LIMIT]
        missing = [ToolCall(f"read:{m['id']}", "gmail", "read_email", {"message_id": m["id"]})
                   for m in top if f"read:{m['id']}" not in r]
        if missing:
            return AgentOutcome.calls(*missing)
        emails = [r[f"read:{m['id']}"]["message"] for m in top]
        memory: dict[str, Any] = {"emails": listed}
        try:
            answer, covered = self.qa.answer(ctx.message or q, emails)
        except (LLMConfigError, LLMProviderError, LLMResponseError) as exc:
            log.warning("email content analysis unavailable; listing matches only emails=%d exception_type=%s",
                        len(emails), type(exc).__name__)
            if len(listed) == 1:
                memory |= _focus_memory(listed[0])
            return AgentOutcome("result", memory=memory,
                                message=(f"I couldn't analyze the email contents right now, so I can only list what "
                                         f"matched. Here are the {len(listed)} matching email(s) for {used}:\n"
                                         f"{_list_lines(listed)}"),
                                data={"query": used, "messages": listed, "analysis_error": type(exc).__name__})
        return self._answer(ctx, used, listed, emails[:covered], answer, memory)

    def _answer(self, ctx: AgentContext, query: str, listed: list[dict], emails: list[dict], answer: EmailAnswer,
                memory: dict[str, Any]) -> AgentOutcome:
        relevant = [(f, emails[f.email - 1]) for f in answer.findings if f.relevant]
        cards = []
        for f in answer.findings:
            m = emails[f.email - 1]
            body = (m.get("body_text") or "").strip()
            cards.append({**compact_email(m), "to": m.get("to", ""), "relevant": f.relevant, "reason": f.reason,
                          "evidence": f.evidence, "has_body": bool(body), "body_excerpt": _excerpt(body),
                          "attachments": m.get("attachments", []),
                          "meeting": f.meeting.model_dump() if f.meeting.mentioned else None})
        parts = [answer.answer]
        for f, m in relevant[:MAX_SHOWN]:
            body = (m.get("body_text") or "").strip()
            block = [f"From: {m.get('sender') or 'unknown sender'}",
                     f"Subject: {m.get('subject') or '(no subject)'}",
                     f"Date: {m.get('date') or m.get('internal_date') or 'unknown'}",
                     f"Email content: {_excerpt(body)}"]
            if f.meeting.mentioned:
                block.append(_meeting_sentence(f.meeting))
            parts.append("\n".join(block))
        if len(relevant) > MAX_SHOWN:
            parts.append(f"{len(relevant) - MAX_SHOWN} more relevant email(s) are listed in the results below.")
        if not relevant:
            parts.append(f"I read {len(emails)} email(s) matching {query} and none of them answer that:\n"
                         f"{_list_lines(listed[:len(emails)])}")
        if answer.missing_information:
            parts.append("Not stated in the email: " + "; ".join(answer.missing_information) + ".")
        empty = sum(1 for c in cards if not c["has_body"])
        if empty:
            parts.append(f"{empty} matching email(s) had no readable body text, so I couldn't check their content.")

        data: dict[str, Any] = {"query": query, "answer": answer.answer, "messages": cards,
                                "missing_information": answer.missing_information}
        if len(relevant) == 1:
            memory |= _focus_memory(relevant[0][1])
        meetings = [(f, m) for f, m in relevant if f.meeting.mentioned]
        offer = calendar_offer(meetings[0][1], meetings[0][0].meeting, ctx) if len(meetings) == 1 else None
        if offer is None:
            return AgentOutcome("result", message="\n\n".join(parts), data=data, memory=memory)
        parts.append(CALENDAR_OFFER)
        data["calendar_offer"] = offer
        return AgentOutcome("clarify", message="\n\n".join(parts), data=data, memory=memory,
                            pending={"intent": "create_event", "service": "calendar", "entities": offer,
                                     "awaiting": ["confirmation"]})


class ReadEmailAgent(OperationAgent):
    service, intent = "gmail", "read_email"
    description = "Open one email (by id, reference or most recent search match) and show its content."

    def __init__(self, understanding: EmailUnderstandingAgent):
        self.understanding = understanding

    def step(self, ctx: AgentContext) -> AgentOutcome:
        m, early = _target_message(ctx)
        if early:
            return early
        assert m is not None
        body = (m.get("body_text") or "").strip()
        analysis = self.understanding.analyze(m)
        lines = [
            f"Email from {m.get('sender') or 'unknown sender'}",
            f"Subject: {m.get('subject') or '(no subject)'}",
            f"Date: {m.get('date') or 'unknown'}",
            f"Summary: {analysis.summary}",
        ]
        if analysis.meeting_mentioned:
            details = analysis.meeting_details
            meeting = "A meeting is mentioned"
            if details.scheduled:
                meeting = "The email says a meeting is scheduled"
            if details.time_description:
                meeting += f" for {details.time_description}"
            if details.date:
                meeting += f" on {details.date}"
            if details.time_description and not details.exact_time:
                meeting += "; no exact time is specified"
            elif details.exact_time:
                meeting += f" at {details.exact_time}"
            lines.append(meeting + ".")
        if analysis.important_information:
            lines.append("Important information:\n" + "\n".join(f"- {item}" for item in analysis.important_information))
        if analysis.action_items:
            lines.append("Action items:\n" + "\n".join(f"- {item}" for item in analysis.action_items))
        if analysis.requests:
            lines.append("Requests:\n" + "\n".join(f"- {item}" for item in analysis.requests))
        if analysis.instructions:
            lines.append("Instructions:\n" + "\n".join(f"- {item}" for item in analysis.instructions))
        if analysis.questions:
            lines.append("Questions:\n" + "\n".join(f"- {item}" for item in analysis.questions))
        return AgentOutcome("result", message="\n\n".join(lines),
                            data={"message": {**compact_email(m), "body_text": body[:5000],
                                              "attachments": m.get("attachments", [])},
                                  "understanding": analysis.model_dump()},
                            memory=_focus_memory(m))


class SummarizeEmailsAgent(OperationAgent):
    service, intent = "gmail", "summarize_emails"
    description = "Summarize one email, the matches of a search, or recent inbox mail (SUMMARIZATION model)."

    def __init__(self, summarizer: SummarizationAgent):
        self.summarizer = summarizer

    def step(self, ctx: AgentContext) -> AgentOutcome:
        r = ctx.tool_results
        if ctx.get("message_id"):
            if "read:0" not in r:
                return AgentOutcome.calls(ToolCall("read:0", "gmail", "read_email", {"message_id": ctx.get("message_id")}))
            q = None
        else:
            q = gmail_query(ctx) or RECENT_QUERY
            if "search" not in r:
                return AgentOutcome.calls(ToolCall("search", "gmail", "search_emails",
                                                   {"query": q, "max_results": SUMMARY_LIMIT}))
            found = (r["search"].get("messages") or [])[:SUMMARY_LIMIT]
            if not found:
                return AgentOutcome("result", message=f"There are no emails matching {q} to summarize.",
                                    data={"query": q, "messages": []})
            missing = [ToolCall(f"read:{i}", "gmail", "read_email", {"message_id": m["id"]})
                       for i, m in enumerate(found) if f"read:{i}" not in r]
            if missing:
                return AgentOutcome.calls(*missing)
        emails = [r[k]["message"] for k in sorted(r) if k.startswith("read:")]
        summary = self.summarizer.summarize(emails, focus=ctx.message)
        log.info("email summarization completed emails=%d key_points=%d action_items=%d",
                 len(emails), len(summary.key_points), len(summary.action_items))
        parts = [f"Summary of {len(emails)} email(s):" if len(emails) > 1 else
                 f"Summary of \"{emails[0].get('subject') or '(no subject)'}\" from {emails[0].get('sender')}:",
                 summary.summary]
        if summary.key_points:
            parts.append("Key points:\n" + "\n".join(f"- {p}" for p in summary.key_points))
        if summary.action_items:
            parts.append("Action items:\n" + "\n".join(f"- {a}" for a in summary.action_items))
        compact = [compact_email(e) for e in emails]
        memory: dict[str, Any] = {"emails": compact}
        if len(emails) == 1:
            memory |= _focus_memory(emails[0])
        return AgentOutcome("result", message="\n\n".join(parts), memory=memory,
                            data={"query": q, "summary": summary.model_dump(), "messages": compact,
                                  "model": self.summarizer.call_log[-1] if self.summarizer.call_log else None})


class _ComposeAgent(OperationAgent):
    def __init__(self, task: TaskAutomationAgent):
        self.task = task

    def _compose(self, ctx: AgentContext, recipient: str, original: dict | None = None):
        return self.task.compose_email(intent=self.intent, instruction=str(ctx.get("body") or ""), recipient=recipient,
                                       sender_name=ctx.user_name, subject=ctx.get("subject"), original=original)


class SendEmailAgent(_ComposeAgent):
    service, intent = "gmail", "send_email"
    description = "Compose a new email (TASK model) and propose sending it for approval."

    def step(self, ctx: AgentContext) -> AgentOutcome:
        to = str(ctx.get("recipient_email"))
        c = self._compose(ctx, to)
        preview = f"Send this email to {to}?\nSubject: {c.subject or '(no subject)'}\n\n{c.body}"
        return AgentOutcome("proposal", proposal=Proposal("gmail", self.intent, {"to": [to], "subject": c.subject, "body": c.body},
                                                          preview),
                            data={"composition_source": c.source}, memory={"last_contact_email": to})


class CreateDraftAgent(_ComposeAgent):
    service, intent = "gmail", "create_draft"
    description = "Compose an email (TASK model) and propose saving it as a Gmail draft."

    def step(self, ctx: AgentContext) -> AgentOutcome:
        to = str(ctx.get("recipient_email"))
        c = self._compose(ctx, to)
        preview = f"Save this draft to {to}?\nSubject: {c.subject or '(no subject)'}\n\n{c.body}"
        return AgentOutcome("proposal", proposal=Proposal("gmail", self.intent, {"to": [to], "subject": c.subject, "body": c.body},
                                                          preview),
                            data={"composition_source": c.source}, memory={"last_contact_email": to})


class ReplyEmailAgent(_ComposeAgent):
    service, intent = "gmail", "reply_email"
    description = "Find the email being answered, compose the reply (TASK model) and propose sending it."

    def step(self, ctx: AgentContext) -> AgentOutcome:
        m, early = _target_message(ctx)
        if early:
            return early
        assert m is not None
        sender = email_address(m.get("sender", "")) or m.get("sender", "")
        c = self._compose(ctx, sender, original=m)
        preview = f"Reply to {m.get('sender')} in \"{m.get('subject') or '(no subject)'}\"?\n\n{c.body}"
        return AgentOutcome("proposal", proposal=Proposal("gmail", self.intent, {"message_id": m["id"], "body": c.body,
                                                                                 "in_reply_to_subject": m.get("subject", "")},
                                                          preview),
                            data={"composition_source": c.source}, memory=_focus_memory(m))


def build_gmail_agents(summarizer: SummarizationAgent, task: TaskAutomationAgent) -> dict[str, OperationAgent]:
    understanding = EmailUnderstandingAgent(summarizer.llm, summarizer.call_log)
    qa = EmailQuestionAgent(summarizer.llm, summarizer.call_log)
    agents: list[OperationAgent] = [SendEmailAgent(task), SearchEmailAgent(qa), ReadEmailAgent(understanding), ReplyEmailAgent(task),
                                    SummarizeEmailsAgent(summarizer), CreateDraftAgent(task)]
    return {a.intent: a for a in agents}
