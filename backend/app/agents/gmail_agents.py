"""Specialized Gmail agents. Reads run immediately through the Gmail MCP server; sends, replies and drafts are
returned as proposals that only run after the user approves them."""
from typing import Any

from app.agents.base import (
    AgentContext, AgentOutcome, OperationAgent, Proposal, ToolCall, compact_email, email_address,
)
from app.agents.summarization_agent import SummarizationAgent
from app.agents.task_agent import TaskAutomationAgent

RECENT_QUERY = "in:inbox newer_than:7d"
SUMMARY_LIMIT = 5


def gmail_query(ctx: AgentContext) -> str | None:
    if ctx.get("query"):
        return str(ctx.get("query"))
    who = ctx.get("recipient_email") or ctx.get("person")
    if who:
        return f'from:"{who}"' if " " in str(who) else f"from:{who}"
    return None


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


class SearchEmailAgent(OperationAgent):
    service, intent = "gmail", "search_emails"
    description = "Find emails with Gmail search syntax and list them."

    def step(self, ctx: AgentContext) -> AgentOutcome:
        q = gmail_query(ctx)
        if not q:
            return AgentOutcome("clarify", message="What should I search for?", pending={"awaiting": ["query"]})
        if "search" not in ctx.tool_results:
            return AgentOutcome.calls(ToolCall("search", "gmail", "search_emails", {"query": q, "max_results": 10}))
        found = [compact_email(m) for m in ctx.tool_results["search"].get("messages") or []]
        if not found:
            return AgentOutcome("result", message=f"No emails matched {q}.", data={"query": q, "messages": []},
                                memory={"emails": []})
        memory: dict[str, Any] = {"emails": found}
        if len(found) == 1:
            memory |= _focus_memory(found[0])
        return AgentOutcome("result", message=f"Here are the {len(found)} matching email(s) for {q}:\n{_list_lines(found)}",
                            data={"query": q, "messages": found}, memory=memory)


class ReadEmailAgent(OperationAgent):
    service, intent = "gmail", "read_email"
    description = "Open one email (by id, reference or most recent search match) and show its content."

    def step(self, ctx: AgentContext) -> AgentOutcome:
        m, early = _target_message(ctx)
        if early:
            return early
        assert m is not None
        body = (m.get("body_text") or m.get("snippet") or "").strip()
        text = (f"From: {m.get('sender')}\nSubject: {m.get('subject') or '(no subject)'}\nDate: {m.get('date')}\n\n"
                f"{body[:1500]}{'...' if len(body) > 1500 else ''}")
        return AgentOutcome("result", message=text, data={"message": {**compact_email(m), "body_text": body[:5000],
                                                                      "attachments": m.get("attachments", [])}},
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
    agents: list[OperationAgent] = [SendEmailAgent(task), SearchEmailAgent(), ReadEmailAgent(), ReplyEmailAgent(task),
                                    SummarizeEmailsAgent(summarizer), CreateDraftAgent(task)]
    return {a.intent: a for a in agents}
